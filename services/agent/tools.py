"""Seed Agent tool implementations: real filesystem and subprocess tools.

Every path is scoped by ``ToolPolicy`` (canonicalized, workspace-contained,
protected roots excluded, symlinks resolved). Shell commands run inside
bubblewrap with the workspace mounted read-write at ``/workspace``,
system runtimes read-only, a cleared environment, and no network namespace
by default. Credentials, home directories, and protected source are never
mounted. If bubblewrap itself is missing, shell fails closed.
"""

from __future__ import annotations

import asyncio
import os
import re
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


class ToolFailure(Exception):
    """A tool ran but did not succeed (exit code, missing file, bad args)."""


class ShellUnavailable(ToolFailure):
    """Bubblewrap is missing: shell fails closed, never silently unprotected."""


_netns_probe: bool | None = None


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def netns_works() -> bool:
    """Probe once whether this host allows a network namespace sandbox."""
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
                        "--ro-bind", "/usr", "/usr",
                        "--tmpfs", "/tmp",
                        "--proc", "/proc",
                        "--dev", "/dev",
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


def build_bwrap_args(
    workspace: Path, command: str, network_allowed: bool
) -> tuple[list[str], bool]:
    """Bubblewrap argv plus whether the network namespace is isolated."""
    exe = bwrap_path()
    if exe is None:
        raise ShellUnavailable(
            "shell unavailable: bubblewrap (bwrap) is not installed; "
            "refusing to run commands without the workspace sandbox"
        )
    args = [
        exe,
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--bind", str(workspace), "/workspace",
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--die-with-parent",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--chdir", "/workspace",
    ]
    isolated = True
    if network_allowed:
        isolated = False
    elif netns_works():
        args.append("--unshare-net")
    else:
        # Platform cannot create the namespace (observed in containers
        # without loopback setup rights). Report honestly; do not claim
        # isolation that was not established.
        isolated = False
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
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (TimeoutError, Exception):
            pass


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
    argv, isolated = build_bwrap_args(policy.workspace, command, policy.network)
    if not isolated:
        prefix = "[network namespace unavailable in environment] " if not policy.network else ""
    else:
        prefix = ""
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
    if proc_slot is not None:
        proc_slot["proc"] = handle
    try:
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            await handle.kill()
            raise ToolFailure(f"shell timed out after {timeout:.0f}s; killed") from None
        output = raw.decode("utf-8", errors="replace")
        code = proc.returncode if proc.returncode is not None else 124
        evidence = command_evidence(
            command, code, output, objective_revision,
            f"shell exit={code}: {command[:120]}",
            network_isolated=isolated,
        )
        observation = prefix + (output[-OUTPUT_LIMIT:] if len(output) > OUTPUT_LIMIT else output)
        if code != 0:
            raise _FailedWithEvidence(
                f"shell exited {code}: {observation[-1000:]}", evidence
            )
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
    data = resolved.read_bytes()
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
    parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(data)
    evidence = file_evidence(resolved, data, objective_revision, f"wrote {resolved}")
    return f"wrote {len(data)} bytes to {resolved} sha256={evidence.sha256}", evidence


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
                # Never descend into symlinked dirs; never leave the workspace.
                dirnames[:] = [
                    d for d in dirnames
                    if not (Path(dirpath) / d).is_symlink()
                ]
                for name in filenames:
                    full = Path(dirpath) / name
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
