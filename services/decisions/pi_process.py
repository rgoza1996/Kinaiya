"""Pinned Bubblewrap mounts and local process custody for a selected Pi SDK."""

import asyncio
import fcntl
import os
import stat
from contextlib import ExitStack
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from core.extensions.sandbox import BWRAP_BIN
from packages.agent_adapters.pi_sdk import MAX_FRAME_BYTES, PI_SDK_PEER


class PiLaunchUnavailable(RuntimeError):
    pass


class PiLaunchProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    profile_id: str = Field(min_length=1, max_length=256)
    node: str
    sdk_root: str
    agent_dir: str
    workspace: str
    allow_network: bool = False


_DIR = os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _open_path(path, *, directory):
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise PiLaunchUnavailable
    descriptor = os.open("/", _DIR)
    try:
        for index, component in enumerate(path.parts[1:], 1):
            is_directory = directory or index < len(path.parts) - 1
            current = os.open(component, _DIR if is_directory else _FILE, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = current
        info = os.fstat(descriptor)
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise PiLaunchUnavailable
        return os.dup(descriptor)
    finally:
        os.close(descriptor)


def _overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def _require_owner_private(descriptor, *, directory):
    info = os.fstat(descriptor)
    if (
        info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
    ):
        raise PiLaunchUnavailable


def _require_owner_controlled(descriptor):
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise PiLaunchUnavailable


def _require_no_alias_files(root):
    # Symlink targets resolve only within the sandbox. Existing hardlinks and
    # special files could bypass the mount boundary, so refuse them before start.
    for _, _, filenames, descriptor in os.fwalk(".", dir_fd=root, follow_symlinks=False):
        for name in filenames:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PiLaunchUnavailable


def _approved_paths(profile, protected):
    paths = tuple(
        Path(value)
        for value in (profile.node, profile.sdk_root, profile.agent_dir, profile.workspace)
    )
    node, sdk, agent, workspace = paths
    if any(_overlaps(path, root) for path in paths for root in protected):
        raise PiLaunchUnavailable
    if any(
        _overlaps(left, right)
        for left, right in ((agent, workspace), (sdk, workspace), (sdk, agent))
    ):
        raise PiLaunchUnavailable
    return node, sdk, agent, workspace


class PiProcess:
    def __init__(self, process, lock_descriptor):
        self.process = process
        self._lock_descriptor = lock_descriptor
        self._retiring = None

    async def close(self, finalize=None):
        if self._retiring is None or (self._retiring.done() and self._lock_descriptor is not None):
            self._retiring = asyncio.create_task(self._close(finalize))
        return await asyncio.shield(self._retiring)

    async def _close(self, finalize):
        process = self.process
        try:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.wait_for(process.wait(), 2)
            return process.returncode is not None
        finally:
            if process.returncode is not None and self._lock_descriptor is not None:
                try:
                    if finalize is not None:
                        await finalize()
                finally:
                    os.close(self._lock_descriptor)
                    self._lock_descriptor = None


async def launch(profile, *, attachment_id, data_dir, protected, expected_session_id):
    node, sdk, agent, workspace = _approved_paths(profile, protected)
    lock = None
    try:
        with ExitStack() as pins:

            def pinned(path, *, directory):
                fd = _open_path(path, directory=directory)
                pins.callback(os.close, fd)
                return fd

            node_fd = pinned(node, directory=False)
            if not os.fstat(node_fd).st_mode & 0o111:
                raise PiLaunchUnavailable
            sdk_fd, agent_fd, work_fd = (
                pinned(path, directory=True) for path in (sdk, agent, workspace)
            )
            _require_owner_controlled(agent_fd)
            _require_owner_controlled(work_fd)
            for directory in (agent_fd, work_fd):
                await asyncio.to_thread(_require_no_alias_files, directory)
            source = pinned(PI_SDK_PEER, directory=False)
            parent = pinned(Path(data_dir), directory=True)
            _require_owner_controlled(parent)
            try:
                os.mkdir("external-agent", 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            registry = os.open("external-agent", _DIR, dir_fd=parent)
            pins.callback(os.close, registry)
            _require_owner_private(registry, directory=True)
            lock = os.open(
                attachment_id + ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=registry,
            )
            _require_owner_private(lock, directory=False)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                os.mkdir(attachment_id, 0o700, dir_fd=registry)
            except FileExistsError:
                pass
            history = os.open(attachment_id, _DIR, dir_fd=registry)
            pins.callback(os.close, history)
            _require_owner_private(history, directory=True)
            flags = _FILE if expected_session_id else _FILE | os.O_CREAT
            session = os.open("session.jsonl", flags, 0o600, dir_fd=history)
            pins.callback(os.close, session)
            _require_owner_private(session, directory=False)
            if expected_session_id and os.fstat(session).st_size == 0:
                raise PiLaunchUnavailable
            # The SDK's supported open() initializes an empty session file
            # before reporting its identity, even before the first prompt.
            await asyncio.to_thread(_require_no_alias_files, history)
            mounts = (
                ("--ro-bind-fd", node_fd, "/node"),
                ("--ro-bind-fd", sdk_fd, "/sdk"),
                ("--ro-bind-fd", source, "/peer.mjs"),
                ("--bind-fd", agent_fd, "/agent"),
                ("--bind-fd", work_fd, "/workspace"),
                ("--bind-fd", history, "/sessions"),
            )
            argv = [
                BWRAP_BIN,
                "--unshare-all",
                "--die-with-parent",
                # Bubblewrap's monitor retains the same flock through sandbox
                # exit even if the Decisions process loses its descriptor.
                "--sync-fd",
                str(lock),
                "--new-session",
                "--ro-bind",
                "/usr",
                "/usr",
                "--ro-bind",
                "/bin",
                "/bin",
                "--ro-bind",
                "/lib",
                "/lib",
                "--ro-bind-try",
                "/lib64",
                "/lib64",
                "--tmpfs",
                "/tmp",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--clearenv",
                "--setenv",
                "PATH",
                "/usr/bin:/bin",
                "--setenv",
                "HOME",
                "/tmp",
            ]
            if profile.allow_network:
                argv += [
                    "--share-net",
                    "--ro-bind",
                    "/etc/resolv.conf",
                    "/etc/resolv.conf",
                    "--ro-bind",
                    "/etc/ssl/certs",
                    "/etc/ssl/certs",
                ]
            for kind, fd, destination in mounts:
                argv += [kind, str(fd), destination]
            argv += [
                "--chdir",
                "/workspace",
                "/node",
                "/peer.mjs",
                "--transcript",
                "/sdk",
                "/workspace",
                "/agent",
                "/sessions",
                "/sessions/session.jsonl",
            ]
            child = await asyncio.create_subprocess_exec(
                *argv,
                pass_fds=(lock, *(fd for _, fd, _ in mounts)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=MAX_FRAME_BYTES,
                env={},
            )
        return PiProcess(child, lock)
    except BaseException:
        if lock is not None:
            os.close(lock)
        raise
