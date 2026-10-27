"""Bubblewrap isolation for untrusted selector source.

Fail-closed: if ``bwrap`` or a system Python runtime is unavailable, or the
namespaces cannot be established, raise :class:`SandboxUnavailable`. There
is intentionally no ordinary-subprocess fallback.

Limits (wall timeout, CPU, 256 MiB RAM, output size, file size) are
enforced outside candidate control: wall time and output size by this host
process reading both pipes incrementally (Python-level ``print``
redirection in the runner is NOT trusted — a candidate bypassing it with
saved fds, ``/proc/self/fd``, or introspection still hits the host byte
cap, the process group is killed, and every child is reaped). CPU, memory,
and file size are applied by the trusted in-sandbox runner before
untrusted code loads; a required limit that cannot apply refuses
execution. NPROC is deliberately not applied inside the sandbox (with the uid-0
mapping the call can leak onto the host user while not reliably
containing the candidate), so no process-count enforcement is claimed:
descendant containment comes from
the PID namespace plus ``--die-with-parent`` plus host group-kill, and
the trusted runner additionally installs a required kernel syscall allowlist
that denies creating processes/threads, opening files and creating sockets.
The registry also rejects process/network imports statically.
``/work`` is a tmpfs (never a host bind); the syscall filter denies file
creation entirely during candidate execution. RLIMIT_FSIZE is an additional
backstop, not a total tmpfs-memory quota. NPROC is never constrained on the host
user before ``bwrap`` forks, so the initial fork cannot be starved.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from core.extensions.errors import CandidateFailure, SandboxUnavailable

RUNNER_FILE = Path(__file__).with_name("_runner.py")

SYSTEM_PYTHONS = ("/usr/bin/python3.12", "/usr/bin/python3.11", "/usr/bin/python3")
BWRAP_BIN = "/usr/bin/bwrap"


@dataclass(frozen=True)
class SandboxLimits:
    wall_timeout_s: float = 10.0
    cpu_s: int = 15
    memory_mb: int = 256
    output_bytes: int = 65_536
    nproc: int = 32


def find_system_python() -> str | None:
    """First usable *system* interpreter; never a venv that sees the repo."""
    for candidate in SYSTEM_PYTHONS:
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if os.path.realpath(candidate).startswith("/home/"):
            continue
        return candidate
    return None


def check_available() -> None:
    """Raise SandboxUnavailable unless isolation can be established."""
    if not (os.path.isfile(BWRAP_BIN) and os.access(BWRAP_BIN, os.X_OK)):
        raise SandboxUnavailable("bubblewrap not installed at /usr/bin/bwrap")
    if shutil.which("bwrap") is None:
        raise SandboxUnavailable("bubblewrap not on PATH")
    if find_system_python() is None:
        raise SandboxUnavailable("no system Python runtime available")
    if not RUNNER_FILE.is_file():
        raise SandboxUnavailable("trusted runner file missing")


def prepare_runner_dir(dst: str | Path) -> Path:
    """Copy ONLY the trusted runner file into ``dst``.

    The sandbox bind-mounts this directory at ``/runner``. The source
    directory (evaluator, registry, criteria, suite helpers) is never
    bound, so ``/runner/evaluator.py`` and siblings do not exist inside.
    """
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for leftover in dst.iterdir():
        if leftover.name != "_runner.py":
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover)
            else:
                leftover.unlink()
    (dst / "_runner.py").write_bytes(RUNNER_FILE.read_bytes())
    return dst


def build_bwrap_argv(python: str, cand_dir: str, runner_dir: str, input_dir: str) -> list[str]:
    """Assemble the containment argv. No host workspace or secret is bound.

    ``/work`` is a tmpfs (never a host bind); ``/runner`` holds only the
    copied trusted runner file; the candidate sees a single copied source
    plus the input payload.
    """
    return [
        BWRAP_BIN,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        cand_dir,
        "/candidate",
        "--ro-bind",
        runner_dir,
        "/runner",
        "--ro-bind",
        input_dir,
        "/input",
        "--tmpfs",
        "/work",
        "--chdir",
        "/work",
        "--uid",
        "0",
        "--gid",
        "0",
        python,
        "/runner/_runner.py",
        "/input/payload.json",
        "/candidate/source.py",
    ]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _communicate_bounded(
    proc: subprocess.Popen, *, limit_bytes: int, timeout_s: float
) -> tuple[bytes, bytes]:
    """Read both pipes incrementally; kill the group on overflow/timeout.

    Never buffers unboundedly: either stream exceeding ``limit_bytes``
    kills the whole process group and raises. The wall timeout likewise
    kills the group. The child is always reaped, including descendants
    via the killed group/session.
    """
    deadline = time.monotonic() + timeout_s
    out = bytearray()
    err = bytearray()
    pending: dict[int, bytearray] = {}
    for fh, buf in ((proc.stdout, out), (proc.stderr, err)):
        assert fh is not None  # noqa: S101 - pipes are always requested
        os.set_blocking(fh.fileno(), False)
        pending[fh.fileno()] = buf
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_group(proc)
                raise CandidateFailure(f"candidate wall timeout after {timeout_s}s")
            readable, _, _ = select.select(list(pending), [], [], remaining)
            if not readable:
                _kill_group(proc)
                raise CandidateFailure(f"candidate wall timeout after {timeout_s}s")
            for fd in readable:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    del pending[fd]
                    continue
                pending[fd].extend(chunk)
                if len(pending[fd]) > limit_bytes:
                    _kill_group(proc)
                    raise CandidateFailure(
                        f"candidate output exceeded {limit_bytes} bytes; group killed"
                    )
    finally:
        if proc.poll() is None:
            _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
    return bytes(out), bytes(err)


def run_selector(
    source_path: Path | str,
    payload: dict,
    *,
    limits: SandboxLimits | None = None,
) -> list[str]:
    """Execute ``select_context`` in isolation; return ONLY the ``ids`` list.

    Extra keys in candidate output are dropped: forged ``score``/``passed``/
    lifecycle fields never count as evidence. Candidate stdout/stderr are
    discarded by the runner; only the single result document is read, and
    both host pipes are byte-bounded incrementally.
    """
    check_available()
    limits = limits or SandboxLimits()
    python = find_system_python()
    assert python is not None  # noqa: S101 - guarded by check_available

    raw = json.dumps(payload).encode("utf-8")
    if len(raw) > 4_000_000:
        raise CandidateFailure("payload exceeds sandbox transfer bound")

    with (
        tempfile.TemporaryDirectory(prefix="hx-cand-") as cand_d,
        tempfile.TemporaryDirectory(prefix="hx-in-") as in_d,
        tempfile.TemporaryDirectory(prefix="hx-run-") as run_d,
    ):
        Path(cand_d, "source.py").write_bytes(Path(source_path).read_bytes())
        Path(in_d, "payload.json").write_bytes(raw)
        prepare_runner_dir(run_d)
        argv = build_bwrap_argv(python, cand_d, run_d, in_d) + [
            str(limits.cpu_s),
            str(limits.memory_mb),
            str(limits.nproc),
        ]
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SandboxUnavailable(f"failed to launch isolation: {exc}") from exc
        out, err = _communicate_bounded(
            proc, limit_bytes=limits.output_bytes, timeout_s=limits.wall_timeout_s
        )
        if proc.returncode != 0:
            stderr = err.decode("utf-8", "replace")[-2000:]
            if proc.returncode == 2 or "hx-limits-unavailable" in stderr:
                raise SandboxUnavailable(f"resource limits unavailable: {stderr}")
            if "bwrap:" in stderr:
                raise SandboxUnavailable(f"isolation setup failed: {stderr}")
            raise CandidateFailure(f"isolated runner exited {proc.returncode}: {stderr}")
        try:
            doc = json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CandidateFailure(f"runner returned invalid JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise CandidateFailure("runner returned a non-object document")
        if "error" in doc:
            raise CandidateFailure(str(doc["error"])[:500])
        ids = doc.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise CandidateFailure("runner returned malformed ids")
        return ids
