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
import hashlib
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

from core.policy import ApprovalError, PolicyDenied, ToolPolicy
from core.policy.tool_admission import admit_native_tool, check_native_arguments
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


def shell_directory(policy: ToolPolicy, value: str | None = None) -> Path:
    """A shell start directory cannot move the approved workspace or use aliases."""
    value = policy.shell_working_directory if value is None else value
    if (
        type(value) is not str
        or not 1 <= len(value) <= 512
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise PolicyDenied("Shell directory must be a relative workspace directory")
    candidate = policy.workspace / value
    try:
        resolved = policy.resolve_path(candidate)
    except RuntimeError as exc:
        # Path.resolve reports a symlink loop as RuntimeError on Python 3.11/12.
        # This is a refused directory, not an uncertain settings transaction.
        raise PolicyDenied("Shell directory cannot be resolved safely") from exc
    if resolved != Path(os.path.abspath(candidate)) or not resolved.is_dir():
        raise PolicyDenied("Shell directory must exist without symlink aliases")
    return resolved


def _workspace_readonly_roots(policy: ToolPolicy):
    roots = sorted(
        {
            policy.workspace if root in policy.workspace.parents else root
            for root in policy.readonly_roots
            if root == policy.workspace
            or policy.workspace in root.parents
            or root in policy.workspace.parents
        },
        key=lambda root: (len(root.parts), str(root)),
    )
    return tuple(
        root
        for index, root in enumerate(roots)
        if not any(parent == root or parent in root.parents for parent in roots[:index])
    )


@contextmanager
def _readonly_bindings(policy: ToolPolicy):
    """Pin reference sources and ancestor mountpoints until BWrap owns them."""
    roots = _workspace_readonly_roots(policy)
    if not roots:
        yield (), ()
        return
    descriptors = []
    opened = {}
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        workspace = os.open(policy.workspace, flags | os.O_DIRECTORY)
        descriptors.append(workspace)
        opened[policy.workspace] = workspace
        for root in roots:
            current, parent = policy.workspace, workspace
            for part in root.relative_to(policy.workspace).parts:
                current = current / part
                if current not in opened:
                    descriptor = os.open(part, flags, dir_fd=parent)
                    descriptors.append(descriptor)
                    opened[current] = descriptor
                parent = opened[current]
        # Existing hardlink aliases could write through the workspace mount;
        # symlink targets could leave the read-only subtree. Refuse both.
        remaining = 4096

        def validate(descriptor, depth=0):
            nonlocal remaining
            remaining -= 1
            if remaining < 0 or depth > 32:
                raise PolicyDenied("shell read-only reference exceeds its structural bound")
            info = os.fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise PolicyDenied("shell read-only reference has a hardlink alias")
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        child = os.open(entry.name, flags, dir_fd=descriptor)
                        try:
                            validate(child, depth + 1)
                        finally:
                            os.close(child)
            else:
                raise PolicyDenied("shell read-only reference is not regular")

        for root in roots:
            validate(opened[root])
        # Writable self-binds keep ancestor pathnames from being renamed and
        # replaced while preserving unrelated workspace write permissions.
        ancestors = sorted(
            (path for path in opened if path not in roots),
            key=lambda path: (len(path.parts), str(path)),
        )
        bindings = tuple(
            ("--bind-fd", opened[path], _sandbox_path(policy, path)) for path in ancestors
        )
        bindings += tuple(
            ("--ro-bind-fd", opened[root], _sandbox_path(policy, root)) for root in roots
        )
    except (OSError, ValueError) as exc:
        raise PolicyDenied("shell read-only reference cannot be pinned") from exc
    else:
        yield bindings, tuple(descriptors)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def build_bwrap_args(
    policy: ToolPolicy, command: str, network_allowed: bool, *, readonly_bindings=()
) -> tuple[list[str], bool]:
    """Bubblewrap argv plus whether the network namespace is isolated.

    Always requests user/pid/uts/ipc/cgroup isolation, plus a network
    namespace unless ``network_allowed`` is an explicit grant. PID isolation
    is mandatory even when networking is granted, so ``/proc`` inside the
    sandbox cannot expose host supervisor environments or ``/proc/PID/root``.
    Nested protected roots and read-only references are over-mounted read-only; a nested protected
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
        "--ro-bind" if policy.deny_write else "--bind",
        str(policy.workspace),
        "/workspace",
    ]
    nested_readonly = bool(_workspace_readonly_roots(policy))
    if nested_readonly and not readonly_bindings:
        raise PolicyDenied("shell read-only references require pinned mount sources")
    for kind, descriptor, destination in readonly_bindings:
        if kind == "--bind-fd":
            # A descendant self-bind must not reopen writes under a root
            # that the owner's Deny setting mounted read-only.
            args += [
                "--ro-bind-fd" if policy.deny_write else kind,
                str(descriptor),
                str(destination),
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
    for kind, descriptor, destination in readonly_bindings:
        if kind == "--ro-bind-fd":
            args += [kind, str(descriptor), str(destination)]
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
        str(_sandbox_path(policy, shell_directory(policy))),
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
    check_native_arguments(
        policy,
        "shell",
        {
            "command": command,
            "timeout_s": timeout_s,
            "objective_revision": objective_revision,
        },
    )
    cap = policy.shell_timeout_s
    if type(cap) not in {int, float} or not math.isfinite(cap) or not 1 <= cap <= SHELL_MAX_TIMEOUT:
        raise ToolFailure("Native shell timeout policy is unavailable")
    timeout = max(1.0, min(float(timeout_s or SHELL_DEFAULT_TIMEOUT), SHELL_MAX_TIMEOUT, cap))
    policy.check_shell(
        approval_id=approval_id, digest=digest, objective_revision=objective_revision
    )
    try:
        with _readonly_bindings(policy) as (bindings, descriptors):
            argv, isolated = build_bwrap_args(
                policy, command, policy.network, readonly_bindings=bindings
            )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                pass_fds=descriptors,
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


_WRITE_FILE_ALLOWED_KEYS = frozenset({"path", "content", "objective_revision"})


def validate_write_arguments(args: dict, objective_revision: int) -> None:
    """Trusted shape check for write_file only; pure, no I/O or mutation.

    Required keys ``path``/``content`` must both be strict strings; ``path``
    must be nonempty and NUL-free (no strip/coercion); ``content`` may be
    empty but must UTF-8 encode within ``READ_LIMIT * 4`` bytes. Only
    ``path``/``content`` plus optional nonauthoritative ``objective_revision``
    metadata are allowed. When present that metadata must be a strict int
    (bool rejected), positive, and equal to the current trusted revision.
    Never used to set task revision. Raises ``ToolFailure`` on any mismatch.
    """
    if not isinstance(args, dict):
        raise ToolFailure("write_file requires an object of arguments")
    if not args.keys() <= _WRITE_FILE_ALLOWED_KEYS:
        raise ToolFailure("write_file rejects unsupported fields")
    if "path" not in args or "content" not in args:
        raise ToolFailure("write_file requires path and content")
    path = args["path"]
    content = args["content"]
    if not isinstance(path, str):
        raise ToolFailure("write_file requires a string path argument")
    if not isinstance(content, str):
        raise ToolFailure("write_file requires a string content argument")
    if path == "" or "\x00" in path:
        raise ToolFailure("write_file requires a nonempty NUL-free path")
    try:
        data = content.encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        raise ToolFailure("write_file content is not valid UTF-8") from exc
    if len(data) > READ_LIMIT * 4:
        raise ToolFailure("write_file exceeds the 256 KiB per-action limit")
    if "objective_revision" in args:
        meta = args["objective_revision"]
        if type(meta) is not int:
            raise ToolFailure("write_file objective_revision must be an int")
        if meta < 1 or meta != objective_revision:
            raise ToolFailure("write_file objective_revision does not match the current revision")


class _FailedWithEvidence(ToolFailure):
    """A tool produced supervisor evidence but still failed (e.g. exit != 0)."""

    def __init__(self, message: str, evidence: Evidence) -> None:
        super().__init__(message)
        self.evidence = evidence


def run_read(
    policy: ToolPolicy, path: str, max_bytes: int, objective_revision: int
) -> tuple[str, Evidence]:
    """Return a complete stable UTF-8 file or an explicit bounded refusal.

    The larger native bound allows a small-request refusal to identify the
    complete source bytes and suggest an exact larger request. Beyond that
    bound only a stat-version token is available, never a partial content hash.
    """
    maximum = READ_LIMIT * 4
    if type(max_bytes) is not int or not 1 <= max_bytes <= maximum:
        raise ToolFailure("read_file max_bytes must be an integer from 1 to 262144")
    check_native_arguments(
        policy,
        "read_file",
        {
            "path": path,
            "max_bytes": max_bytes,
            "objective_revision": objective_revision,
        },
    )
    try:
        resolved = policy.check_read(path)
        data, observed = _read_file_snapshot(policy, resolved, maximum)
    except ApprovalError:
        raise
    except PolicyDenied:
        raise PolicyDenied("read_file: path is outside the allowed file scope") from None
    except (OSError, ValueError, TypeError, RuntimeError):
        # Native errors may include host paths, invalid bytes or caller text.
        raise ToolFailure("read_file: cannot observe a stable regular workspace file") from None
    if data is None:
        version = hashlib.sha256(repr(_read_stat_identity(observed)).encode("ascii")).hexdigest()
        raise ToolFailure(
            f"read_file: file exceeds native maximum {maximum} bytes; "
            f"size_bytes={observed.st_size}; stat_version={version} "
            "(observed metadata identity, not a content hash). "
            "Choose an authorized source that fits this bound; paging and shell fallback "
            "are unavailable."
        )
    if len(data) > max_bytes:
        digest = hashlib.sha256(data).hexdigest()
        raise ToolFailure(
            f"read_file: complete file exceeds requested max_bytes={max_bytes}; "
            f"size_bytes={len(data)}; source_sha256={digest}. "
            f"Request max_bytes={len(data)} to read the complete file "
            f"(native maximum {maximum})."
        )
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ToolFailure("read_file: complete file is not valid UTF-8") from None
    evidence = read_evidence(resolved, data, objective_revision, f"read complete file {resolved}")
    return text, evidence


def _read_stat_identity(observed):
    """Observed identity/version only; these values are not content proof."""
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _read_file_snapshot(policy, resolved, maximum):
    """Read through scoped FDs and reject changes to the file or its binding."""
    with policy.scoped_parent(resolved) as (parent, name, scoped):
        if scoped != resolved:
            raise ToolFailure("read_file: source changed during observation")
        parent_before = os.fstat(parent)
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=parent,
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ToolFailure("read_file: source is not a regular file")
            if before.st_size < 0:
                raise ToolFailure("read_file: source changed during observation")
            data = None
            if before.st_size <= maximum:
                collected = bytearray()
                while len(collected) <= maximum:
                    chunk = os.read(descriptor, min(READ_LIMIT, maximum + 1 - len(collected)))
                    if not chunk:
                        break
                    collected.extend(chunk)
                data = bytes(collected)
            after = os.fstat(descriptor)
            if _read_stat_identity(before) != _read_stat_identity(after):
                raise ToolFailure("read_file: source changed during observation")
            # Rewalk from the workspace, rather than trusting only the old
            # parent FD if its directory was renamed/replaced during the read.
            with policy.scoped_parent(resolved) as (current_parent, current_name, current_path):
                parent_after = os.fstat(current_parent)
                if (
                    current_path != resolved
                    or current_name != name
                    or (parent_before.st_dev, parent_before.st_ino)
                    != (parent_after.st_dev, parent_after.st_ino)
                    or _read_stat_identity(before)
                    != _read_stat_identity(
                        os.stat(current_name, dir_fd=current_parent, follow_symlinks=False)
                    )
                ):
                    raise ToolFailure("read_file: source changed during observation")
            if data is not None and len(data) != before.st_size:
                raise ToolFailure("read_file: source changed during observation")
            return data, before
        finally:
            os.close(descriptor)


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
    check_native_arguments(
        policy,
        "write_file",
        {
            "path": path,
            "content": content,
            "objective_revision": objective_revision,
        },
    )
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
    *,
    approval_id=None,
    digest=None,
) -> tuple[str, Evidence]:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolFailure(f"search: invalid regex: {exc}") from exc
    check_native_arguments(
        policy,
        "search",
        {
            "pattern": pattern,
            "path": path,
            "max_results": max_results,
            "objective_revision": objective_revision,
        },
    )
    scope = policy.check_search(path)
    admit_native_tool(
        policy, "search", approval_id=approval_id, digest=digest, revision=objective_revision
    )
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
                text = policy.read_bytes(cand, READ_LIMIT).decode("utf-8", errors="strict")
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
    "validate_write_arguments",
]
