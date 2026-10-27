"""Bubblewrap isolation for untrusted compute-plugin source.

Fail-closed: if ``bwrap`` or a system Python runtime is unavailable, or the
namespaces cannot be established, raise
:class:`PluginSandboxUnavailable`. There is intentionally no
ordinary-subprocess fallback.

Limits (wall/CPU 10s, 256 MiB RAM, output size, file size) are enforced
outside candidate control: wall time and output size by this host process
reading both pipes incrementally (Python-level ``print`` redirection in
the runner is NOT trusted — a candidate bypassing it with saved fds,
``/proc/self/fd``, or introspection still hits the host byte cap, the
process group is killed, and every child is reaped). CPU, memory, and file
size are applied by the trusted in-sandbox runner before untrusted code
loads; a required limit that cannot apply refuses execution. The trusted
runner additionally installs a required kernel syscall allowlist that
denies opening files, creating processes/threads, and creating sockets.
``/work`` is a tmpfs (never a host bind); the syscall filter denies file
creation entirely during candidate execution. RLIMIT_FSIZE is an
additional backstop, not a total tmpfs-memory quota.

Cancellation is polled at <=100ms: when the caller's
:class:`threading.Event` is set, the whole process group is killed and
reaped, not merely abandoned.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.extensions.sandbox import BWRAP_BIN, find_system_python
from core.plugins.errors import PluginCandidateFailure, PluginSandboxUnavailable

RUNNER_FILE = Path(__file__).with_name("_plugin_runner.py")


@dataclass(frozen=True)
class PluginLimits:
    wall_timeout_s: float = 10.0
    cpu_s: int = 10
    memory_mb: int = 256
    output_bytes: int = 65_536
    nproc: int = 32


EVAL_LIMITS = PluginLimits(wall_timeout_s=10.0, cpu_s=10, memory_mb=256)
RUN_LIMITS = PluginLimits(wall_timeout_s=10.0, cpu_s=10, memory_mb=256)


def check_available() -> None:
    """Raise PluginSandboxUnavailable unless isolation can be established."""
    if not (os.path.isfile(BWRAP_BIN) and os.access(BWRAP_BIN, os.X_OK)):
        raise PluginSandboxUnavailable("bubblewrap not installed at /usr/bin/bwrap")
    if shutil.which("bwrap") is None:
        raise PluginSandboxUnavailable("bubblewrap not on PATH")
    if find_system_python() is None:
        raise PluginSandboxUnavailable("no system Python runtime available")
    if not RUNNER_FILE.is_file():
        raise PluginSandboxUnavailable("trusted plugin runner file missing")


def prepare_runner_dir(dst: str | Path) -> Path:
    """Copy ONLY the trusted plugin runner file into ``dst``.

    The sandbox bind-mounts this directory at ``/runner``. The host source
    tree (registry, evaluator, suites) is never bound, so sibling trusted
    modules do not exist inside.
    """
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for leftover in dst.iterdir():
        if leftover.name != "_plugin_runner.py":
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover)
            else:
                leftover.unlink()
    (dst / "_plugin_runner.py").write_bytes(RUNNER_FILE.read_bytes())
    return dst


def build_bwrap_argv(python: str, cand_dir: str, runner_dir: str, input_dir: str) -> list[str]:
    """Assemble the containment argv. No host workspace or secret is bound.

    ``/work`` is a tmpfs (never a host bind); ``/runner`` holds only the
    copied trusted runner file; the candidate sees a single copied source
    plus the input payload. Environment is cleared (no ambient proxy or
    secrets); only PATH/PYTHON* are set.
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
        "/runner/_plugin_runner.py",
        "/input/payload.json",
        "/candidate/source.py",
    ]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _communicate_bounded(
    proc: subprocess.Popen,
    *,
    limit_bytes: int,
    timeout_s: float,
    cancel_event: threading.Event | None = None,
) -> tuple[bytes, bytes]:
    """Read both pipes incrementally; kill the group on overflow/timeout/cancel.

    The cancel event is polled at <=100ms. On cancel the process group is
    killed and reaped, then PluginCandidateFailure is raised. The child is
    always reaped, including descendants via the killed group/session.
    """
    if cancel_event is not None and cancel_event.is_set():
        _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
        raise PluginCandidateFailure("candidate cancelled before start")
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
            if cancel_event is not None and cancel_event.is_set():
                _kill_group(proc)
                raise PluginCandidateFailure("candidate cancelled; group killed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_group(proc)
                raise PluginCandidateFailure(f"candidate wall timeout after {timeout_s}s")
            wait_for = min(remaining, 0.1)
            readable, _, _ = select.select(list(pending), [], [], wait_for)
            if not readable:
                continue
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
                    raise PluginCandidateFailure(
                        f"candidate output exceeded {limit_bytes} bytes; group killed"
                    )
        if cancel_event is not None and cancel_event.is_set():
            _kill_group(proc)
            raise PluginCandidateFailure("candidate cancelled; group killed")
        # Pipe EOF may precede process exit. Wait within the same deadline;
        # killing here would occasionally turn a valid result into SIGKILL.
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                raise PluginCandidateFailure("candidate cancelled; group killed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PluginCandidateFailure(f"candidate wall timeout after {timeout_s}s")
            try:
                proc.wait(timeout=min(remaining, 0.1))
            except subprocess.TimeoutExpired:
                pass
    finally:
        if proc.poll() is None:
            _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
    return bytes(out), bytes(err)


def run_plugin(
    source_bytes: bytes,
    arguments: Any,
    *,
    limits: PluginLimits | None = None,
    cancel_event: threading.Event | None = None,
) -> Any:
    """Execute ``run(arguments)`` in isolation; return ONLY the output JSON.

    ``source_bytes`` are the captured immutable candidate bytes (never a
    mutable host path). Extra keys in candidate output are dropped: forged
    ``success``/score/lifecycle fields never count as evidence. Candidate
    stdout/stderr are discarded by the runner; only the single framed
    result document is read, and both host pipes are byte-bounded
    incrementally.
    """
    check_available()
    limits = limits or PluginLimits()
    python = find_system_python()
    assert python is not None  # noqa: S101 - guarded by check_available

    raw = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > 4_000_000:
        raise PluginCandidateFailure("arguments exceed sandbox transfer bound")
    if len(source_bytes) > 4_000_000:
        raise PluginCandidateFailure("source exceeds sandbox transfer bound")

    with (
        tempfile.TemporaryDirectory(prefix="plug-cand-") as cand_d,
        tempfile.TemporaryDirectory(prefix="plug-in-") as in_d,
        tempfile.TemporaryDirectory(prefix="plug-run-") as run_d,
    ):
        Path(cand_d, "source.py").write_bytes(source_bytes)
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
            raise PluginSandboxUnavailable(f"failed to launch isolation: {exc}") from exc
        out, err = _communicate_bounded(
            proc,
            limit_bytes=limits.output_bytes,
            timeout_s=limits.wall_timeout_s,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise PluginCandidateFailure("candidate cancelled; group killed")
        if proc.returncode != 0:
            stderr = err.decode("utf-8", "replace")[-2000:]
            if proc.returncode == 2 or "hx-limits-unavailable" in stderr:
                raise PluginSandboxUnavailable(f"resource limits unavailable: {stderr}")
            if "bwrap:" in stderr:
                raise PluginSandboxUnavailable(f"isolation setup failed: {stderr}")
            raise PluginCandidateFailure(f"isolated runner exited {proc.returncode}: {stderr}")
        if len(out) > limits.output_bytes:
            raise PluginCandidateFailure("runner returned oversize document")
        try:
            doc = json.loads(out.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PluginCandidateFailure(f"runner returned invalid JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise PluginCandidateFailure("runner returned a non-object document")
        if "error" in doc:
            raise PluginCandidateFailure(str(doc["error"])[:500])
        if "output" not in doc or len(doc) != 1:
            raise PluginCandidateFailure("runner returned malformed framed result")
        return doc["output"]
