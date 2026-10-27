"""Seed Agent tool implementations: real filesystem and subprocess tools.

Every path is scoped by ``ToolPolicy`` (canonicalized, workspace-contained,
protected roots excluded, symlinks resolved). Shell commands run inside
bubblewrap with the workspace mounted read-write at ``/workspace``,
system runtimes read-only, a cleared environment, and full namespace
isolation (user, pid, uts, ipc, cgroup, plus net unless the ``network``
grant explicitly allows it). There is no fallback: if bubblewrap is
missing, or a nested protected root cannot be covered read-only, or the
namespaces cannot be established, shell fails closed instead of running
less isolated. Credentials, home directories, and protected source are
never mounted. Subprocess output is bounded; cancellation kills the whole
process group.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from core.policy import ApprovalError, PolicyDenied, ToolPolicy
from core.supervision import (
    command_evidence,
    file_evidence,
    read_evidence,
    search_evidence,
)
from packages.contracts.models import Evidence

READ_LIMIT = 65536
SEARCH_MATCH_LIMIT = 50
SEARCH_LINE_LIMIT = 500
OUTPUT_LIMIT = 4000
SHELL_DEFAULT_TIMEOUT = 60.0
SHELL_MAX_TIMEOUT = 120.0
# Retained subprocess output is bounded: collection keeps at most this many
# bytes (the rest is drained and noted as truncated), and a hard ceiling
# kills runaway output instead of buffering it without limit.
SHELL_OUTPUT_CAP = 262144
SHELL_OUTPUT_HARD_MAX = 2 * 1024 * 1024


class ToolFailure(Exception):
    """A tool ran but did not succeed (exit code, missing file, bad args)."""


class ShellUnavailable(ToolFailure):
    """Bubblewrap is missing or unusable: shell fails closed, never unprotected."""


_netns_probe: bool | None = None


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def netns_works() -> bool:
    """Diagnostic probe: whether this host can create a network namespace.

    Kept for operational diagnostics only. Shell construction no longer
    consults it: isolation flags are always requested and a kernel denial
    fails the command closed instead of silently dropping the flag.
    """
    global _netns_probe
    if _netns_probe is None:
        exe = bwrap_path()
        if exe is None:
            _netns_probe = False
        else:
            try:
                probe = subprocess.run(
                    [
                        exe,
                        "--unshare-net",
                        "--ro-bind",
                        "/usr",
                        "/usr",
                        "--tmpfs",
                        "/tmp",
                        "--proc",
                        "/proc",
                        "--dev",
                        "/dev",
                        "--die-with-parent",
                        "--clearenv",
                        "true",
                    ],
                    capture_output=True,
                    timeout=15,
                )
                _netns_probe = probe.returncode == 0
            except Exception:
                _netns_probe = False
    return _netns_probe


def _sandbox_path(policy: ToolPolicy, host_path: Path) -> Path:
    """Map a workspace-contained host path to its path inside the sandbox."""
    return Path("/workspace") / host_path.relative_to(policy.workspace)


def build_bwrap_args(
    policy: ToolPolicy, command: str, network_allowed: bool
) -> tuple[list[str], bool]:
    """Bubblewrap argv plus whether the network namespace is isolated.

    Always requests user/pid/uts/ipc/cgroup isolation, plus a network
    namespace unless ``network_allowed`` is an explicit grant. PID isolation
    is mandatory even when networking is granted, so ``/proc`` inside the
    sandbox cannot expose host supervisor environments or ``/proc/PID/root``.
    Nested protected roots are over-mounted read-only; a nested protected
    root that cannot be covered (missing on the host) rejects shell
    construction instead of running with it writable.
    """
    exe = bwrap_path()
    if exe is None:
        raise ShellUnavailable(
            "shell unavailable: bubblewrap (bwrap) is not installed; "
            "refusing to run commands without the workspace sandbox"
        )
    args = [
        exe,
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/bin",
        "/bin",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--bind",
        str(policy.workspace),
        "/workspace",
    ]
    # A protected root nested inside the workspace would otherwise be
    # writable through the workspace bind. Cover each one read-only; the
    # later mount wins over the earlier bind in bubblewrap.
    for root in policy.protected_roots:
        try:
            root.relative_to(policy.workspace)
        except ValueError:
            continue  # outside the workspace: never mounted, nothing to cover
        if not root.exists():
            raise PolicyDenied(
                f"shell unavailable on this layout: protected root {root} "
                "is nested in the workspace but missing on the host, "
                "so it cannot be over-mounted read-only"
            )
        args += ["--ro-bind", str(root), str(_sandbox_path(policy, root))]
    args += [
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--die-with-parent",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-cgroup",
        "--chdir",
        "/workspace",
    ]
    isolated = True
    if network_allowed:
        # Explicit grant: networking stays, every other namespace is still
        # isolated (PID isolation is never optional).
        isolated = False
    else:
        args.append("--unshare-net")
    args += ["/bin/bash", "-c", command]
    return args, isolated


class RunningProc:
    """Handle to a live sandboxed child, killable as a process group."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    async def kill(self) -> None:
        import os
        import signal

        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self._proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (TimeoutError, Exception):
            pass


async def _collect_bounded(proc: asyncio.subprocess.Process, timeout: float) -> tuple[bytes, bool]:
    """Drain merged stdout/stderr up to the retention cap within the timeout.

    Returns (retained bytes, truncated). Kills the child on timeout or when
    output exceeds the hard ceiling. Cancellation also kills the group.
    """
    buf = bytearray()
    total = 0
    truncated = False
    try:
        async with asyncio.timeout(timeout):
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                room = SHELL_OUTPUT_CAP - len(buf)
                if room > 0:
                    buf += chunk[:room]
                if total > SHELL_OUTPUT_CAP:
                    truncated = True
                if total > SHELL_OUTPUT_HARD_MAX:
                    await asyncio.shield(RunningProc(proc).kill())
                    raise ToolFailure("shell output exceeded the 2 MiB bound; killed")
            await proc.wait()
    except TimeoutError:
        await asyncio.shield(RunningProc(proc).kill())
        raise ToolFailure(f"shell timed out after {timeout:.0f}s; killed") from None
    return bytes(buf), truncated


async def run_shell(
    policy: ToolPolicy,
    command: str,
    timeout_s: float,
    objective_revision: int,
    *,
    approval_id: str | None = None,
    digest: str | None = None,
    proc_slot: dict[str, RunningProc | None] | None = None,
) -> tuple[str, Evidence]:
    if not isinstance(command, str) or not command.strip():
        raise ToolFailure("shell requires a non-empty command string")
    timeout = max(1.0, min(float(timeout_s or SHELL_DEFAULT_TIMEOUT), SHELL_MAX_TIMEOUT))
    policy.check_shell(
        approval_id=approval_id, digest=digest, objective_revision=objective_revision
    )
    argv, isolated = build_bwrap_args(policy, command, policy.network)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ShellUnavailable(f"shell unavailable: {exc}") from exc
    handle = RunningProc(proc)
    # Publish the handle before awaiting so interrupt/close can kill the
    # group; it is cleared only after the child is reaped or killed below.
    if proc_slot is not None:
        proc_slot["proc"] = handle
    try:
        try:
            raw, truncated = await _collect_bounded(proc, timeout)
        except asyncio.CancelledError:
            # Even when our own await is cancelled, the child group must die
            # before the slot is cleared; shield the kill from the cancel.
            await asyncio.shield(handle.kill())
            raise
        output = raw.decode("utf-8", errors="replace")
        if truncated:
            output += f"\n…[truncated subprocess output to {SHELL_OUTPUT_CAP} bytes]"
        code = proc.returncode if proc.returncode is not None else 124
        evidence = command_evidence(
            command,
            code,
            output,
            objective_revision,
            f"shell exit={code}: {command[:120]}",
            network_isolated=isolated,
        )
        observation = output[-OUTPUT_LIMIT:] if len(output) > OUTPUT_LIMIT else output
        if truncated:
            observation = "[output truncated] " + observation
        if code != 0:
            raise _FailedWithEvidence(f"shell exited {code}: {observation[-1000:]}", evidence)
        return observation, evidence
    finally:
        if proc_slot is not None:
            proc_slot["proc"] = None


class _FailedWithEvidence(ToolFailure):
    """A tool produced supervisor evidence but still failed (e.g. exit != 0)."""

    def __init__(self, message: str, evidence: Evidence) -> None:
        super().__init__(message)
        self.evidence = evidence


def run_read(
    policy: ToolPolicy, path: str, max_bytes: int, objective_revision: int
) -> tuple[str, Evidence]:
    resolved = policy.check_read(path)
    if not resolved.is_file():
        raise ToolFailure(f"read_file: not a file: {path!s}")
    data = policy.read_bytes(resolved, READ_LIMIT * 4 + 1)
    clipped = data[: max(1, min(int(max_bytes or READ_LIMIT), READ_LIMIT * 4))]
    text = clipped.decode("utf-8", errors="replace")
    note = "" if len(data) <= len(clipped) else f"…[truncated {len(data) - len(clipped)} bytes]"
    evidence = read_evidence(resolved, data, objective_revision, f"read {resolved}")
    return text + note, evidence


def run_write(
    policy: ToolPolicy,
    path: str,
    content: str,
    objective_revision: int,
    *,
    approval_id: str | None = None,
    digest: str | None = None,
) -> tuple[str, Evidence]:
    if not isinstance(content, str):
        raise ToolFailure("write_file requires a string content argument")
    data = content.encode("utf-8")
    if len(data) > READ_LIMIT * 4:
        raise ToolFailure("write_file exceeds the 256 KiB per-action limit")
    resolved = policy.check_write(
        path, approval_id=approval_id, digest=digest, objective_revision=objective_revision
    )
    if resolved.is_dir():
        raise ToolFailure(f"write_file: path is a directory: {path!s}")
    parent = resolved.parent
    try:
        parent.relative_to(policy.workspace)
    except ValueError:
        raise PolicyDenied(f"write parent escapes workspace: {path!s}") from None
    # Atomic replacement never writes through an existing hardlink. Descriptor
    # walks refuse symlink substitutions even after canonical policy checks.
    with policy.scoped_parent(resolved, create=True) as (parent_fd, name, _):
        temporary = ".kinaiya-" + secrets.token_hex(12)
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    evidence = file_evidence(resolved, data, objective_revision, f"wrote {resolved}")
    return f"wrote {len(data)} bytes to {resolved} sha256={evidence.sha256}", evidence


def _prune_walk(policy: ToolPolicy, dirpath: str, dirnames: list[str]) -> None:
    """Drop walk entries that escape the workspace or enter protected roots."""
    keep: list[str] = []
    for name in dirnames:
        full = Path(dirpath) / name
        if full.is_symlink():
            continue
        try:
            resolved = full.resolve()
            resolved.relative_to(policy.workspace)
        except (ValueError, OSError):
            continue
        if policy.is_protected(resolved):
            continue
        keep.append(name)
    dirnames[:] = keep


def run_search(
    policy: ToolPolicy,
    pattern: str,
    path: str | None,
    objective_revision: int,
    max_results: int = SEARCH_MATCH_LIMIT,
) -> tuple[str, Evidence]:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolFailure(f"search: invalid regex: {exc}") from exc
    scope = policy.check_search(path)
    if not scope.exists():
        raise ToolFailure(f"search: scope does not exist: {path!s}")
    hits: list[str] = []
    roots = [scope] if scope.is_dir() else [scope]
    for root in roots:
        if root.is_file():
            candidates = [root]
        else:
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                # Never descend into symlinked dirs, escapes, or protected roots.
                _prune_walk(policy, dirpath, dirnames)
                for name in filenames:
                    full = Path(dirpath) / name
                    try:
                        resolved = full.resolve()
                    except OSError:
                        continue
                    try:
                        resolved.relative_to(policy.workspace)
                    except ValueError:
                        continue
                    if policy.is_protected(resolved):
                        continue
                    if full.is_symlink():
                        try:
                            full.resolve().relative_to(policy.workspace)
                        except (ValueError, OSError):
                            continue
                    candidates.append(full)
        for cand in sorted(candidates):
            if len(hits) >= max_results:
                break
            try:
                if cand.is_symlink() and not cand.exists():
                    continue
                text = cand.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    shown = line[:SEARCH_LINE_LIMIT]
                    try:
                        rel = cand.resolve().relative_to(policy.workspace)
                    except ValueError:
                        continue
                    hits.append(f"{rel}:{lineno}:{shown}")
                    if len(hits) >= max_results:
                        break
    output = "\n".join(hits) if hits else "(no matches)"
    evidence = search_evidence(scope, output, objective_revision, f"search {pattern!r} in {scope}")
    return output, evidence


__all__ = [
    "ApprovalError",
    "PolicyDenied",
    "RunningProc",
    "ShellUnavailable",
    "ToolFailure",
    "build_bwrap_args",
    "bwrap_path",
    "netns_works",
    "run_read",
    "run_search",
    "run_shell",
    "run_write",
]
