"""Bubblewrap isolation for untrusted selector source.

Fail-closed: if ``bwrap`` or a system Python runtime is unavailable, or the
namespaces cannot be established, raise :class:`SandboxUnavailable`. There
is intentionally no ordinary-subprocess fallback.

Limits (wall timeout, CPU, 256 MiB RAM, output size, process count) are
enforced outside candidate control: wall time and output size by this host
process, CPU/memory/process-count by the trusted in-sandbox runner before
untrusted code loads. NPROC is never constrained on the host user before
``bwrap`` forks, so the initial fork cannot be starved.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
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


def _bind_if_exists(args: list[str], host: str, dest: str | None = None) -> None:
    if os.path.lexists(host):
        args += ["--ro-bind", host, dest or host]


def build_bwrap_argv(
    python: str, cand_dir: str, runner_dir: str, input_dir: str, work_dir: str
) -> list[str]:
    """Assemble the containment argv. No host workspace or secret is bound."""
    argv = [
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
        "--bind",
        work_dir,
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
    return argv


def run_selector(
    source_path: Path | str,
    payload: dict,
    *,
    limits: SandboxLimits | None = None,
) -> list[str]:
    """Execute ``select_context`` in isolation; return ONLY the ``ids`` list.

    Extra keys in candidate output are dropped: forged ``score``/``passed``/
    lifecycle fields never count as evidence. Candidate stdout/stderr are
    discarded by the runner; only the single result document is read.
    """
    check_available()
    limits = limits or SandboxLimits()
    python = find_system_python()
    assert python is not None  # noqa: S101 - guarded by check_available

    raw = json.dumps(payload).encode("utf-8")
    if len(raw) > 4_000_000:
        raise CandidateFailure("payload exceeds sandbox transfer bound")

    with tempfile.TemporaryDirectory(prefix="hx-cand-") as cand_d, tempfile.TemporaryDirectory(
        prefix="hx-in-"
    ) as in_d, tempfile.TemporaryDirectory(prefix="hx-work-") as work_d:
        Path(cand_d, "source.py").write_bytes(Path(source_path).read_bytes())
        Path(in_d, "payload.json").write_bytes(raw)
        argv = build_bwrap_argv(
            python, cand_d, str(RUNNER_FILE.parent), in_d, work_d
        ) + [str(int(limits.wall_timeout_s) + 5), str(limits.memory_mb), str(limits.nproc)]
        # Note: argv CPU arg uses wall+5; RLIMIT_CPU is a backstop, the
        # authoritative wall timeout is enforced below by killing the group.
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out, err = proc.communicate(timeout=limits.wall_timeout_s)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()
                raise CandidateFailure(
                    f"candidate wall timeout after {limits.wall_timeout_s}s"
                ) from None
        except FileNotFoundError as exc:
            raise SandboxUnavailable(f"failed to launch isolation: {exc}") from exc
        if proc.returncode != 0:
            stderr = (err or b"").decode("utf-8", "replace")[-2000:]
            if "bwrap:" in stderr:
                raise SandboxUnavailable(f"isolation setup failed: {stderr}")
            raise CandidateFailure(
                f"isolated runner exited {proc.returncode}: {stderr}"
            )
        out = (out or b"")[: limits.output_bytes]
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
