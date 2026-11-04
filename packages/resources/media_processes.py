"""Private original media-process/descriptor ownership, never media authority.

Only fixed native owner configuration and its required current/dependency gates
may use this pool. A granted outer process is not ready/qualified, and these
internal observations are not public cleanup, admission or decode receipts.
Prepared mode holds the same verified bootstrap before exec for native owner
linking. Its identity capsule contains no CPU/descendant accounting, runtime
qualification or proof that a Session has admitted the eventual decoder.
"""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from packages.contracts.media import (
    MediaConsumerRef,
    MediaContractError,
    MediaFence,
    MediaLimits,
    MediaNativeRef,
    MediaProcessIdentity,
    media_bytes,
    media_json,
)
from packages.resources.processes import (
    DEFAULT_LANG,
    DEFAULT_PATH,
    WorkerManager,
    WorkerSpec,
    _Managed,
    _pidfd_supported,
)

__all__ = [
    "MediaLaunchBinding",
    "MediaWorkerPool",
    "MediaLaunch",
    "MediaChild",
    "MediaPreparedChild",
    "MediaLocalSettlement",
    "MediaProcessError",
]

_ERROR = "Invalid media process operation"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n?\Z")
_METADATA_LIMIT = 1_073_741_824
# Reserve one ordinary bounded metadata envelope per retained original. This is
# a quota reservation, not an eviction threshold or a lifetime child limit.
_REGISTRATION_BYTES = 65_536
# Charge before each descendant pidfd attempt, including refused/uncertain opens.
_MEMBER_BYTES = 65_536
_BLOCK = 65_536
_WAIT = 5.0
_PROC_BYTES = 4096
_ZOMBIES = {"Z", "X", "x"}


class MediaProcessError(ValueError):
    def __init__(self, code: str = "unavailable") -> None:
        self.code = (
            code
            if type(code) is str
            and code in {"bad_packet", "bound", "capacity", "conflict", "expired", "unavailable"}
            else "unavailable"
        )
        super().__init__(_ERROR)


def _error(code="unavailable"):
    raise MediaProcessError(code)


def _snapshot(value, kind):
    if type(value) is not kind:
        _error("bad_packet")
    try:
        return media_json(media_bytes(value), kind)
    except MediaContractError as error:
        raise MediaProcessError(error.code) from None
    except Exception:
        raise MediaProcessError("bad_packet") from None


def _hex(value, pattern, length):
    if type(value) is not str:
        _error("bad_packet")
    if len(value) > length:
        _error("bound")
    if pattern.fullmatch(value) is None:
        _error("bad_packet")
    return value


@dataclass(frozen=True, slots=True)
class MediaLaunchBinding:
    operation_id: str
    child_instance: str
    source: MediaNativeRef
    source_id: str
    consumer: MediaConsumerRef
    fence: MediaFence
    binding_sha256: str

    def __post_init__(self):
        for name in ("operation_id", "child_instance", "source_id"):
            _hex(getattr(self, name), _ID, 32)
        _hex(self.binding_sha256, _HASH, 64)
        for name, kind in (
            ("source", MediaNativeRef),
            ("consumer", MediaConsumerRef),
            ("fence", MediaFence),
        ):
            object.__setattr__(self, name, _snapshot(getattr(self, name), kind))


def _binding(value):
    if type(value) is not MediaLaunchBinding:
        _error("bad_packet")
    try:
        return MediaLaunchBinding(
            value.operation_id,
            value.child_instance,
            value.source,
            value.source_id,
            value.consumer,
            value.fence,
            value.binding_sha256,
        )
    except MediaProcessError:
        raise
    except Exception:
        raise MediaProcessError("bad_packet") from None


def _binding_bytes(value):
    body = {
        "operation_id": value.operation_id,
        "child_instance": value.child_instance,
        "source_id": value.source_id,
        "binding_sha256": value.binding_sha256,
    }
    for name in ("source", "consumer", "fence"):
        body[name] = json.loads(media_bytes(getattr(value, name)))
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


@dataclass(frozen=True, slots=True)
class MediaLocalSettlement:
    state: Literal["settled", "unknown"]
    bindings: tuple[MediaLaunchBinding, ...]
    observations: tuple[tuple[str, bool, bool, bool, bool, bool], ...]

    def __post_init__(self):
        if type(self.state) is not str or self.state not in {"settled", "unknown"}:
            _error("bad_packet")
        if type(self.bindings) is not tuple or type(self.observations) is not tuple:
            _error("bad_packet")
        object.__setattr__(self, "bindings", tuple(_binding(value) for value in self.bindings))
        for row in self.observations:
            if type(row) is not tuple or len(row) != 6:
                _error("bad_packet")
            _hex(row[0], _ID, 32)
            if any(type(value) is not bool for value in row[1:]):
                _error("bad_packet")


class _Descriptor:
    def __init__(self, fd):
        self.fd = fd
        self.closed = False
        self.unknown = False
        self.waiters = {}

    def close(self):
        if self.waiters:
            return False
        if self.fd is None:
            return self.closed and not self.unknown
        fd, self.fd = self.fd, None
        try:
            os.close(fd)
        except BaseException:
            self.unknown = True
            raise
        self.closed = True
        return True


class _Socket(_Descriptor):
    def __init__(self, sock):
        super().__init__(sock.fileno())
        self.sock = sock

    def close(self):
        if self.waiters:
            return False
        if self.fd is None:
            return self.closed and not self.unknown
        # Detach from the Python close owner before the sole observed os.close.
        # On detach failure keep the original wrapper retained, never retry it.
        try:
            self.sock.detach()
        except BaseException:
            self.fd = None
            self.unknown = True
            raise
        return super().close()


class _Directory:
    """One reusable, positively closed streaming observation owner."""

    def __init__(self):
        self.iterator = None
        self.closed = True
        self.unknown = False

    def open(self):
        if not self.closed or self.unknown:
            return None
        self.closed = False
        try:
            self.iterator = os.scandir("/proc")
        except OSError:
            self.closed = True  # No iterator/descriptor was returned.
            return None
        except BaseException:
            self.unknown = True
            raise
        return self.iterator

    def close(self):
        if self.iterator is None:
            return self.closed and not self.unknown
        iterator, self.iterator = self.iterator, None
        try:
            iterator.close()
        except BaseException:
            self.unknown = True
            raise
        self.closed = True
        return True


async def _ready(owner, direction):
    loop = asyncio.get_running_loop()
    fd = owner.fd
    if fd is None:
        _error("expired")
    future = loop.create_future()
    registration = (future, object())
    owner.waiters[direction] = registration

    def ready():
        # A queued callback cannot remove/touch a later occupant of this number.
        if owner.fd == fd and owner.waiters.get(direction) is registration:
            if not future.done():
                future.set_result(None)

    add = loop.add_reader if direction == "read" else loop.add_writer
    remove = loop.remove_reader if direction == "read" else loop.remove_writer
    try:
        add(fd, ready)
        await future
    finally:
        if owner.fd == fd and owner.waiters.get(direction) is registration:
            remove(fd)
            del owner.waiters[direction]


class _Duplex:
    def __init__(self, launch, owner):
        self._launch, self._owner = launch, owner
        self._busy = set()

    async def send_chunk(self, data: bytes) -> None:
        if type(data) is not bytes:
            _error("bad_packet")
        if not 1 <= len(data) <= _BLOCK:
            _error("bound")
        await self._dispatch("write", data)

    async def recv_chunk(self, max_bytes: int) -> bytes:
        if type(max_bytes) is not int:
            _error("bad_packet")
        if not 1 <= max_bytes <= _BLOCK:
            _error("bound")
        return await self._dispatch("read", max_bytes)

    async def _dispatch(self, direction, value):
        launch = self._launch
        launch._pool._loop_now()
        launch._owned()
        if launch._fenced or not launch._granted or self._owner.fd is None:
            _error("expired")
        if direction in self._busy:
            _error("capacity")
        # Hold before create_task, including eager task factories.
        self._busy.add(direction)
        try:
            task = launch._task(self._io(direction, value), io=True)
        except BaseException:
            self._busy.discard(direction)
            raise
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _io(self, direction, value):
        try:
            offset = 0
            while True:
                if self._launch._fenced or self._owner.fd is None:
                    _error("expired")
                try:
                    if direction == "read":
                        return self._owner.sock.recv(value)
                    sent = self._owner.sock.send(memoryview(value)[offset:])
                    if sent <= 0:
                        _error()
                    offset += sent
                    if offset == len(value):
                        return None
                except (BlockingIOError, InterruptedError):
                    await _ready(self._owner, direction)
        except MediaProcessError:
            raise
        except Exception:
            raise MediaProcessError() from None
        finally:
            self._busy.discard(direction)


class MediaChild:
    __slots__ = ("_launch", "_channel")

    def __init__(self, launch, channel):
        self._launch, self._channel = launch, channel

    @property
    def channel(self):
        return self._channel


class MediaPreparedChild:
    """Opaque exact held original; snapshots grant no native authority.

    Metadata remains readable for the retained historical original. A DTO,
    copied capsule or matching numeric PID cannot select an exec release.
    """

    __slots__ = ("_launch",)

    def __init__(self, launch):
        self._launch = launch

    def _original(self):
        launch = getattr(self, "_launch", None)
        if type(launch) is not MediaLaunch:
            _error("conflict")
        launch._owned()
        if launch._prepared is not self or launch._start_mode != "prepared":
            _error("conflict")
        return launch

    @property
    def identity(self):
        return _snapshot(self._original()._prepared_identity, MediaProcessIdentity)

    @property
    def binding(self):
        return _binding(self._original()._binding)


class MediaLaunch:
    """Only the exact object retained by its pool may select an original."""

    def __init__(self, pool, binding, key):
        self._pool, self._binding, self._key = pool, binding, key
        self._grant = None
        self._fenced = self._granted = self._settled = False
        self._future_spawn = self._spawn_phase = self._spawn_finished = False
        self._start_requested = False
        self._start_mode = None
        self._spawn_dispatched = False
        self._start_assigned = asyncio.Event()
        self._start_task = self._spawn_task = self._cleanup_task = self._dependency_task = None
        self._release_assigned = asyncio.Event()
        self._release_requested = False
        self._release_task = None
        self._prepared = self._prepared_identity = None
        self._prepared_leader = None
        self._bootstrap_nonce = self._bootstrap_path = None
        self._tasks, self._io_tasks = set(), set()
        self._child = self._control = self._managed = None
        self._process = None
        self._pass_owners, self._all_owners, self._drains = [], [], []
        self._grant_writer = None
        self._parent_source_closed = False
        self._outer_positive = self._io_positive = self._dependencies_positive = False
        self._grant_positive = False
        self._close_unknown = False
        self._metadata_unknown = False
        self._member_reservations = 0
        self._members = {}
        self._leader = None
        self._verified = False
        # Scratch owners are retained before observation acquisition, reused
        # only after positive close; no /proc roster or per-read history grows.
        self._observation = self._owner(None)
        self._observation.closed = True
        self._directory = _Directory()
        self._all_owners.append(self._directory)

    def _owned(self):
        pool = getattr(self, "_pool", None)
        binding = getattr(self, "_binding", None)
        if (
            type(pool) is not MediaWorkerPool
            or binding is None
            or pool._launches.get(binding.child_instance) is not self
        ):
            _error("conflict")

    def _bind_spool_grant(self, grant, source_id):
        from services.session.media_spool import _SpoolChildGrant

        self._owned()
        with self._pool._lock:
            if (
                type(grant) is not _SpoolChildGrant
                or type(source_id) is not str
                or source_id != self._binding.source_id
            ):
                _error("conflict")
            if self._grant is not None:
                _error("conflict")
            if self._fenced or self._start_requested:
                _error("expired")
        # Do not call native validation under the pool lock: the spool caller
        # already owns its mutex. Validation is trusted and read-only.
        if not self._pool._current(self):
            _error("expired")
        with self._pool._lock:
            if self._grant is not None:
                _error("conflict")
            if self._fenced or self._start_requested:
                _error("expired")
            self._grant = grant

    def _spool_grant_can_spawn(self, grant):
        self._owned()
        if not self._pool._current(self):
            return False
        with self._pool._lock:
            return bool(
                grant is self._grant
                and self._spawn_phase
                and not self._spawn_finished
                and not self._fenced
            )

    def _spool_grant_can_close_parent(self, grant):
        self._owned()
        with self._pool._lock:
            return bool(
                grant is self._grant
                and not self._future_spawn
                and (self._spawn_finished or self._fenced)
            )

    def _spool_grant_can_retire(self, grant):
        self._owned()
        with self._pool._lock:
            return bool(
                grant is self._grant
                and self._parent_source_closed
                and not self._future_spawn
                and self._outer_positive
                and self._io_positive
                and self._dependencies_positive
                and not self._close_unknown
                and not self._metadata_unknown
            )

    def _task(self, coro, *, io=False):
        try:
            task = asyncio.create_task(coro)
        except BaseException:
            coro.close()
            raise
        self._tasks.add(task)
        if io:
            self._io_tasks.add(task)

        def done(original):
            self._tasks.discard(original)
            self._io_tasks.discard(original)
            if not original.cancelled():
                original.exception()  # Consume fixed failures without a raw loop log.

        task.add_done_callback(done)
        return task

    def _owner(self, fd, *, passed=False):
        owner = _Descriptor(fd)
        self._all_owners.append(owner)
        if passed:
            self._pass_owners.append(owner)
        return owner

    def _socket(self, sock, *, passed=False):
        owner = _Socket(sock)
        self._all_owners.append(owner)
        if passed:
            self._pass_owners.append(owner)
        return owner

    def _close(self, owner):
        try:
            result = owner.close()
        except BaseException as error:
            self._close_unknown = True
            if not isinstance(error, Exception):
                raise
            return False
        if owner.unknown:
            self._close_unknown = True
        return result

    def _close_parent_source(self):
        if self._grant is None:
            self._parent_source_closed = True
            return True
        try:
            result = self._grant._close_parent_copy(self)
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            return False
        self._parent_source_closed = self._parent_source_closed or (type(result) is bool and result)
        return self._parent_source_closed

    async def _drain(self, owner):
        try:
            while owner.fd is not None:
                try:
                    chunk = os.read(owner.fd, 8192)
                    if not chunk:
                        return
                except (BlockingIOError, InterruptedError):
                    await _ready(owner, "read")
        except Exception:
            return

    def _pipe(self):
        read, write = os.pipe2(os.O_CLOEXEC)
        return self._owner(read), self._owner(write, passed=True)

    async def _spawn_owned(self, manager):
        self._owned()
        try:
            if self._start_mode == "prepared":
                # Publication of the exact original Task precedes all spawn
                # effects, including under an eager/custom task scheduler.
                await self._start_assigned.wait()
                if asyncio.current_task() is not self._start_task:
                    self._metadata_unknown = True
                    _error()
            if self._fenced or not self._pool._current(self):
                _error("expired")
            if not _pidfd_supported():
                _error()
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            self._control = self._socket(parent)
            child_control = self._socket(child, passed=True)
            parent.setblocking(False)
            grant_r, grant_w = os.pipe2(os.O_CLOEXEC)
            grant_read = self._owner(grant_r, passed=True)
            self._grant_writer = self._owner(grant_w)
            nonce = secrets.token_hex(16)
            self._bootstrap_nonce = nonce
            spec = self._pool._spec
            raw = json.dumps(
                {
                    "nonce": nonce,
                    "parent_pid": os.getpid(),
                    "argv": list(spec.argv),
                    "cwd": str(spec.cwd),
                    "env": spec.child_env(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8", "strict")
            if len(raw) > _BLOCK:
                _error("bound")
            config = self._owner(
                os.memfd_create("media-worker-config", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING),
                passed=True,
            )
            offset = 0
            while offset < len(raw):
                count = os.write(config.fd, raw[offset:])
                if count <= 0:
                    _error()
                offset += count
            fcntl.fcntl(
                config.fd,
                fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE,
            )
            os.lseek(config.fd, 0, os.SEEK_SET)
            stdout_read, stdout_write = self._pipe()
            stderr_read, stderr_write = self._pipe()
            os.set_blocking(stdout_read.fd, False)
            os.set_blocking(stderr_read.fd, False)
            self._spawn_phase = True
            source_fd = self._grant._fd_for_spawn(self)
            if type(source_fd) is not int or source_fd <= 2:
                _error()
            bootstrap = str(Path(__file__).with_name("_media_bootstrap.py").resolve())
            self._bootstrap_path = bootstrap
            with self._pool._lock:
                if self._fenced or self._pool._closed:
                    _error("expired")
                self._spawn_dispatched = True
                self._spawn_task = self._task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        bootstrap,
                        str(grant_read.fd),
                        str(config.fd),
                        nonce,
                        str(child_control.fd),
                        str(source_fd),
                        cwd=str(spec.cwd),
                        env={"PATH": DEFAULT_PATH, "LANG": DEFAULT_LANG},
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=stdout_write.fd,
                        stderr=stderr_write.fd,
                        start_new_session=True,
                        pass_fds=(grant_read.fd, config.fd, child_control.fd, source_fd),
                    )
                )
            proc = await asyncio.shield(self._spawn_task)
            if not isinstance(proc, asyncio.subprocess.Process):
                _error()
            self._process = proc
            self._spawn_finished = True
            self._future_spawn = False
            self._managed = _Managed(spec=spec, process=proc, pgid=proc.pid)
            self._drains = [
                self._task(self._drain(stdout_read)),
                self._task(self._drain(stderr_read)),
            ]
            for owner in self._pass_owners:
                self._close(owner)
            if not self._close_parent_source():
                _error()
            leader, start = self._verify_bootstrap(proc.pid, nonce, bootstrap)
            if leader is None:
                _error()
            self._leader = leader
            self._managed.leader_fd, self._managed.leader_starttime = leader.fd, start
            self._verified = True
            if self._start_mode == "prepared":
                raw = self._read_observation("/proc/sys/kernel/random/boot_id", 37)
                if raw is None or _BOOT_ID.fullmatch(raw) is None:
                    _error()
                leader.identity_id = secrets.token_hex(16)
                identity = MediaProcessIdentity(
                    pid=proc.pid,
                    start_tick=start,
                    boot_id=raw[:36].replace(b"-", b"").decode("ascii"),
                    nonce=nonce,
                    pidfd_owner=leader.identity_id,
                )
                self._prepared_identity = _snapshot(identity, MediaProcessIdentity)
                self._prepared_leader = leader
                self._prepared = MediaPreparedChild(self)
                if self._fenced or not self._pool._current(self):
                    _error("expired")
                return self._prepared
            return self._grant_exec()
        except MediaProcessError:
            self._pool.fence(self)
            raise
        except BaseException as error:
            self._pool.fence(self)
            if not isinstance(error, Exception):
                raise
            raise MediaProcessError() from None
        finally:
            # This original task is never cancelled by caller waits. A late
            # subprocess result remains enrolled until this exact await settles.
            if not self._spawn_dispatched:
                self._future_spawn = False
            # A cancelled/failed spawn without the original Process is NOT a
            # positive no-late-child result. Keep every inheritance input live.
            elif self._process is not None:
                self._future_spawn = False
                self._spawn_finished = True
            self._spawn_phase = False
            if self._fenced:
                self._pool._ensure_cleanup(self)

    def _held_current(self):
        managed, owner = self._managed, self._leader
        if (
            managed is None
            or managed.process is not self._process
            or managed.process.returncode is not None
            or not self._verified
            or owner is None
            or owner.fd is None
            or owner.closed
            or owner.unknown
            or self._granted
            or self._prepared_identity is None
            or owner is not self._prepared_leader
            or owner.identity_id != self._prepared_identity.pidfd_owner
        ):
            return False
        observed = self._stat(managed.process.pid)
        return (
            observed is not None
            and observed[0] not in _ZOMBIES
            and observed[1] == os.getpid()
            and observed[2] == observed[3] == managed.process.pid
            and observed[4] == managed.leader_starttime
            and self._pidfd_pid(owner) == managed.process.pid
            and self._command(managed.process.pid, self._bootstrap_nonce, self._bootstrap_path)
        )

    def _grant_exec(self):
        if not self._pool._current(self):
            _error("expired")
        if self._start_mode == "prepared" and not self._held_current():
            _error()
        with self._pool._lock:
            if self._close_unknown or self._fenced:
                _error("expired")
            if os.write(self._grant_writer.fd, b"\x01") != 1:
                _error()
            if not self._close(self._grant_writer):
                _error()
            self._granted = self._managed.granted = True
        self._child = MediaChild(self, _Duplex(self, self._control))
        return self._child

    async def _release_prepared(self):
        try:
            await self._release_assigned.wait()
            if asyncio.current_task() is not self._release_task:
                self._metadata_unknown = True
                _error()
            self._owned()
            if self._fenced or self._pool._closed:
                _error("expired")
            return self._grant_exec()
        except MediaProcessError:
            self._pool.fence(self)
            raise
        except BaseException as error:
            self._pool.fence(self)
            if not isinstance(error, Exception):
                raise
            raise MediaProcessError() from None
        finally:
            if self._fenced:
                self._pool._ensure_cleanup(self)

    def _read_observation(self, path, limit):
        owner = self._observation
        if not owner.closed or owner.unknown:
            return None
        owner.closed = False
        try:
            owner.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError:
            owner.closed = True  # The syscall returned no acquired descriptor.
            return None
        except BaseException:
            owner.unknown = self._close_unknown = True
            raise
        raw = bytearray()
        valid = True
        try:
            while len(raw) <= limit:
                chunk = os.read(owner.fd, limit + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            valid = len(raw) <= limit
        except OSError:
            valid = False
        finally:
            closed = self._close(owner)
        return bytes(raw) if valid and closed else None

    def _stat(self, pid):
        raw = self._read_observation(f"/proc/{pid}/stat", _PROC_BYTES)
        if raw is None:
            return None
        cut = raw.rfind(b")")
        if cut < 0:
            return None
        fields = raw[cut + 2 :].split()
        if len(fields) < 20:
            return None
        try:
            return (
                fields[0].decode("ascii"),
                int(fields[1]),
                int(fields[2]),
                int(fields[3]),
                int(fields[19]),
            )
        except (ValueError, UnicodeError):
            return None

    def _pidfd_pid(self, owner):
        if owner is None or owner.fd is None:
            return None
        raw = self._read_observation(f"/proc/self/fdinfo/{owner.fd}", _PROC_BYTES)
        if raw is not None:
            for line in raw.split(b"\n"):
                if line.startswith(b"Pid:"):
                    try:
                        return int(line.split()[1])
                    except (ValueError, IndexError):
                        return None
        return None

    def _command(self, pid, nonce, bootstrap):
        raw = self._read_observation(f"/proc/{pid}/cmdline", _BLOCK)
        if raw is None:
            return False
        nonce, bootstrap = nonce.encode("utf-8"), bootstrap.encode("utf-8")
        found_nonce = found_bootstrap = False
        offset = 0
        while offset <= len(raw):
            end = raw.find(b"\0", offset)
            if end < 0:
                end = len(raw)
            size = end - offset
            found_nonce |= size == len(nonce) and raw.startswith(nonce, offset, end)
            found_bootstrap |= size == len(bootstrap) and raw.startswith(bootstrap, offset, end)
            offset = end + 1
        return found_nonce and found_bootstrap

    def _pidfd(self, pid):
        # Enroll the close owner BEFORE acquisition or any post-open observer.
        owner = self._owner(None)
        try:
            fd = os.pidfd_open(pid, 0)
            if type(fd) is not int or fd < 0:
                owner.unknown = self._close_unknown = True
                return None
            owner.fd = fd
        except OSError:
            # A native failed syscall returned no acquired descriptor. The
            # original charge stays; cleanup still joins the ungranted child.
            owner.closed = True
            return None
        except BaseException as error:
            owner.unknown = self._close_unknown = True
            if not isinstance(error, Exception):
                raise
            return None
        return owner

    def _verify_bootstrap(self, pid, nonce, bootstrap):
        before = self._stat(pid)
        if (
            before is None
            or before[0] in _ZOMBIES
            or before[1] != os.getpid()
            or before[2] != pid
            or before[3] != pid
            or not self._command(pid, nonce, bootstrap)
        ):
            return None, None
        owner = self._pidfd(pid)
        if owner is None:
            return None, None
        accepted = False
        try:
            after = self._stat(pid)
            accepted = (
                after is not None
                and self._pidfd_pid(owner) == pid
                and self._command(pid, nonce, bootstrap)
                and after[0] not in _ZOMBIES
                and after[1] == os.getpid()
                and after[2] == pid
                and after[3] == pid
                and after[4] == before[4]
            )
            if accepted:
                return owner, before[4]
            return None, None
        finally:
            if not accepted:
                self._close(owner)  # Rejected originals never enter signal lists.

    def _leader_current(self):
        managed = self._managed
        if managed is None or not managed.granted or self._leader is None:
            return False
        stat = self._stat(managed.process.pid)
        return (
            stat is not None
            and self._pidfd_pid(self._leader) == managed.process.pid
            and stat[0] not in _ZOMBIES
            and stat[1] == os.getpid()
            and stat[2] == managed.pgid
            and stat[3] == managed.pgid
            and stat[4] == managed.leader_starttime
        )

    def _adopt_member(self, pid):
        pgid = self._managed.pgid
        before = self._stat(pid)
        if (
            before is None
            or before[0] in _ZOMBIES
            or before[2] != pgid
            or before[3] != pgid
            or not self._leader_current()
        ):
            return None
        owner = self._pidfd(pid)
        if owner is None:
            return None
        accepted = False
        try:
            after = self._stat(pid)
            accepted = (
                after is not None
                and self._pidfd_pid(owner) == pid
                and after[0] not in _ZOMBIES
                and after[2] == pgid
                and after[3] == pgid
                and after[4] == before[4]
                and self._leader_current()
            )
            return owner if accepted else None
        finally:
            if not accepted:
                self._close(owner)

    def _scan_members(self, *, capture):
        # One bounded observation at a time; never allocate an uncharged roster.
        live, complete = False, True
        iterator = self._directory.open()
        if iterator is None:
            return False, False
        try:
            for entry in iterator:
                name = entry.name
                if len(name) > 10 or not name.isascii() or not name.isdecimal():
                    continue
                pid = int(name)
                stat = self._stat(pid)
                if stat is None:
                    complete = False
                    continue
                if stat[2] != self._managed.pgid:
                    continue
                live |= stat[0] not in _ZOMBIES
                if not capture or pid == self._managed.process.pid or pid in self._members:
                    continue
                if not self._pool._reserve_member(self):
                    complete = False
                    break
                owner = self._adopt_member(pid)
                if owner is not None:
                    self._members[pid] = owner
                    self._managed.member_fds[pid] = owner.fd
        except OSError:
            complete = False
        finally:
            closed = self._close(self._directory)
        return live, complete and closed

    def _capture_members(self):
        if self._leader_current():
            self._scan_members(capture=True)

    async def _stop_owned(self, manager):
        if self._managed is None:
            if self._process is not None and not self._granted:
                await self._process.wait()
                return self._process.returncode is not None
            return not self._future_spawn and not self._spawn_dispatched
        proc = self._managed.process
        if not self._verified:
            # Trusted ungranted bootstrap can only exit on original grant EOF.
            await proc.wait()
            return proc.returncode is not None
        self._capture_members()
        manager._media_signal(self, signal.SIGTERM)
        loop = asyncio.get_running_loop()
        deadline, escalated = loop.time() + float(self._pool._spec.stop_timeout_s), False
        while True:
            dead = (
                proc.returncode is not None
                and self._pidfd_pid(self._leader) == -1
                and all(self._pidfd_pid(owner) == -1 for owner in self._members.values())
            )
            if dead:
                live, complete = self._scan_members(capture=False)
                if complete and not live:
                    await proc.wait()
                    return not self._metadata_unknown
            if loop.time() >= deadline:
                if escalated:
                    return False
                self._capture_members()
                manager._media_signal(self, signal.SIGKILL)
                escalated = True
                deadline = loop.time() + 2.0
            await asyncio.sleep(0.01)

    async def _dependencies(self):
        try:
            result = await self._pool._retire_dependencies(_binding(self._binding), self._child)
            return type(result) is bool and result
        except BaseException:
            return False

    async def _cleanup(self):
        try:
            if self._start_requested:
                await self._start_assigned.wait()
                if self._start_task is not None:
                    await asyncio.gather(asyncio.shield(self._start_task), return_exceptions=True)
            if self._future_spawn:
                return False
            if self._release_task is not None:
                await self._release_assigned.wait()
                await asyncio.gather(asyncio.shield(self._release_task), return_exceptions=True)
            if self._grant_writer is not None:
                self._close(self._grant_writer)
            for owner in self._pass_owners:
                self._close(owner)
            self._close_parent_source()
            if not self._outer_positive:
                self._outer_positive = await self._pool._manager._stop_media_owned(self)
            for task in tuple(self._io_tasks):
                task.cancel()
            if self._io_tasks:
                await asyncio.gather(*tuple(self._io_tasks), return_exceptions=True)
            if self._control is not None:
                self._close(self._control)
            if self._dependency_task is None or self._dependency_task.done():
                if self._dependency_task is None or not self._dependencies_positive:
                    self._dependency_task = self._task(self._dependencies())
            self._dependencies_positive = await asyncio.shield(self._dependency_task)
            if self._dependencies_positive and not self._outer_positive:
                # Native inner-owner retirement may make the outer observation
                # positive for this SAME original incarnation.
                self._outer_positive = await self._pool._manager._stop_media_owned(self)
            if not self._outer_positive or not self._dependencies_positive:
                return False
            if self._drains:
                await asyncio.gather(*self._drains, return_exceptions=True)
            for owner in self._all_owners:
                self._close(owner)
            self._io_positive = all(
                owner.closed and not owner.unknown for owner in self._all_owners
            )
            if self._close_unknown or not self._io_positive or not self._parent_source_closed:
                return False
            if self._grant is not None:
                result = self._grant._retire_child_use(self)
                self._grant_positive = type(result) is bool and result
            else:
                self._grant_positive = True
            self._settled = self._grant_positive
            return self._settled
        except BaseException:
            return False


class MediaWorkerPool:
    def __init__(
        self, fixed_spec: WorkerSpec, limits: MediaLimits, *, current, retire_dependencies
    ):
        if (
            type(fixed_spec) is not WorkerSpec
            or not callable(current)
            or not callable(retire_dependencies)
        ):
            _error("bad_packet")
        if inspect.iscoroutinefunction(current):
            _error("bad_packet")
        try:
            self._spec = WorkerSpec(
                fixed_spec.id,
                tuple(fixed_spec.argv),
                Path(fixed_spec.cwd),
                dict(fixed_spec.env) if fixed_spec.env is not None else None,
                fixed_spec.startup_timeout_s,
                fixed_spec.stop_timeout_s,
            )
        except Exception:
            raise MediaProcessError("bad_packet") from None
        self._limits = _snapshot(limits, MediaLimits)
        self._current_callback, self._retire_dependencies = current, retire_dependencies
        self._manager = WorkerManager([self._spec])
        self._launches, self._active_operations = {}, {}
        self._metadata = 0
        self._closed = False
        self._lock = threading.RLock()
        self._loop = None

    def _loop_now(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            _error("conflict")
        self._loop = loop
        return loop

    def _current(self, launch):
        try:
            result = self._current_callback(_binding(launch._binding))
        except Exception:
            raise MediaProcessError() from None
        if type(result) is not bool:
            if inspect.iscoroutine(result):
                result.close()
            _error()
        return result

    def _owned(self, launch):
        if type(launch) is not MediaLaunch or getattr(launch, "_pool", None) is not self:
            _error("conflict")
        launch._owned()

    def _reserve_member(self, launch):
        # No PID/open/owner may be added before its permanent envelope charge.
        # A failed adoption can conceal an ambiguous close; never refund it.
        with self._lock:
            if launch._metadata_unknown or self._metadata + _MEMBER_BYTES > _METADATA_LIMIT:
                launch._metadata_unknown = True
                return False
            self._metadata += _MEMBER_BYTES
            launch._member_reservations += 1
            return True

    def register(self, binding):
        binding = _binding(binding)
        key = _binding_bytes(binding)
        with self._lock:
            previous = self._launches.get(binding.child_instance)
            if previous is not None:
                if previous._key != key:
                    _error("conflict")
                return previous
            if self._closed:
                _error("expired")
            previous = self._active_operations.get(binding.operation_id)
            if previous is not None and not previous._settled:
                _error("capacity")
            active = sum(not original._settled for original in self._launches.values())
            if active >= min(
                self._limits.decode.children_global, self._limits.source.operations_global
            ):
                _error("capacity")
            if self._metadata + _REGISTRATION_BYTES > _METADATA_LIMIT:
                _error("capacity")
            launch = MediaLaunch(self, binding, key)
            self._launches[binding.child_instance] = launch
            self._active_operations[binding.operation_id] = launch
            self._metadata += _REGISTRATION_BYTES
            return launch

    async def start(self, launch, grant):
        self._loop_now()
        self._owned(launch)
        if launch._grant is not grant or grant is None:
            _error("conflict")
        with self._lock:
            if launch._start_mode not in (None, "legacy"):
                _error("conflict")
            create = not launch._start_requested
            if create:
                if launch._fenced or self._closed:
                    _error("expired")
                launch._start_mode = "legacy"
                launch._start_requested = launch._future_spawn = True
        if create:
            try:
                launch._start_task = launch._task(self._manager._start_media_owned(launch))
            except BaseException as error:
                with self._lock:
                    if not launch._spawn_dispatched:
                        launch._future_spawn = False
                self.fence(launch)
                if not isinstance(error, Exception):
                    raise
                raise MediaProcessError() from None
            finally:
                launch._start_assigned.set()
        else:
            await launch._start_assigned.wait()
        if launch._start_task is None:
            _error()
        # This caller observes the retained original; cancelling only its wait
        # is not an explicit fence and cannot change later exact replay.
        return await asyncio.shield(launch._start_task)

    def _prepared_current(self, launch):
        with self._lock:
            if launch._fenced or self._closed:
                _error("expired")
            if launch._metadata_unknown:
                _error()
        if not self._current(launch):
            self.fence(launch)
            self._ensure_cleanup(launch)
            _error("expired")

    def _actual_task(self, task, coroutine):
        return (
            type(task) is asyncio.Task
            and task.get_loop() is self._loop
            and task.get_coro() is coroutine
        )

    async def prepare(self, launch, grant):
        """Retain one verified, ungranted bootstrap for native owner linking.

        This reserves no new budget and grants no exec or Session authority.
        Cancelling a caller only cancels its observation of the original Task.
        """
        self._loop_now()
        self._owned(launch)
        if launch._grant is not grant or grant is None:
            _error("conflict")
        with self._lock:
            if launch._start_mode not in (None, "prepared"):
                _error("conflict")
        self._prepared_current(launch)
        with self._lock:
            create = not launch._start_requested
            if create:
                if launch._fenced or self._closed:
                    _error("expired")
                launch._start_mode = "prepared"
                launch._start_requested = launch._future_spawn = True
        if create:
            coroutine = self._manager._start_media_owned(launch)
            try:
                launch._start_task = launch._task(coroutine)
                if not self._actual_task(launch._start_task, coroutine):
                    _error()
            except BaseException as error:
                launch._metadata_unknown = True
                self.fence(launch)
                self._ensure_cleanup(launch)
                if not isinstance(error, Exception):
                    raise
                raise MediaProcessError() from None
            else:
                launch._start_assigned.set()
        else:
            await launch._start_assigned.wait()
        if launch._start_task is None:
            _error()
        prepared = await asyncio.shield(launch._start_task)
        self._prepared_current(launch)
        if type(prepared) is not MediaPreparedChild or prepared is not launch._prepared:
            _error()
        if not launch._granted and not launch._held_current():
            self.fence(launch)
            self._ensure_cleanup(launch)
            _error()
        return prepared

    async def start_prepared(self, prepared):
        """Release one exact held original after the native parent admits it.

        The parent must already have persisted/validated its linked ChildOwner
        and charged budget. Neither a capsule snapshot nor callback True proves
        that admission; this method only guards the owned process release.
        """
        self._loop_now()
        if type(prepared) is not MediaPreparedChild:
            _error("conflict")
        launch = prepared._original()
        self._owned(launch)
        self._prepared_current(launch)
        with self._lock:
            create = not launch._release_requested
            if create:
                if launch._fenced or self._closed:
                    _error("expired")
                launch._release_requested = True
        if create:
            coroutine = launch._release_prepared()
            try:
                launch._release_task = launch._task(coroutine)
                if not self._actual_task(launch._release_task, coroutine):
                    _error()
            except BaseException as error:
                launch._metadata_unknown = True
                self.fence(launch)
                self._ensure_cleanup(launch)
                if not isinstance(error, Exception):
                    raise
                raise MediaProcessError() from None
            else:
                launch._release_assigned.set()
        else:
            await launch._release_assigned.wait()
        if launch._release_task is None:
            _error()
        child = await asyncio.shield(launch._release_task)
        self._prepared_current(launch)
        if type(child) is not MediaChild or child is not launch._child:
            _error()
        return child

    def fence(self, launch):
        self._owned(launch)
        with self._lock:
            launch._fenced = True
            if launch._grant_writer is not None:
                launch._close(launch._grant_writer)
        for task in tuple(launch._io_tasks):
            if self._loop is not None:
                try:
                    self._loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    launch._close_unknown = True

    def _ensure_cleanup(self, launch):
        if launch._cleanup_task is None or launch._cleanup_task.done():
            launch._cleanup_task = launch._task(launch._cleanup())
        return launch._cleanup_task

    def _observation(self, originals):
        originals = tuple(originals)
        return MediaLocalSettlement(
            "settled" if all(original._settled for original in originals) else "unknown",
            tuple(_binding(original._binding) for original in originals),
            tuple(
                (
                    original._binding.child_instance,
                    original._parent_source_closed,
                    original._outer_positive,
                    original._io_positive,
                    original._dependencies_positive,
                    original._grant_positive,
                )
                for original in originals
            ),
        )

    async def settle(self, launch):
        self._loop_now()
        self._owned(launch)
        self.fence(launch)
        if not launch._settled:
            task = self._ensure_cleanup(launch)
            await asyncio.wait({task}, timeout=_WAIT)
        return self._observation((launch,))

    async def close(self):
        self._loop_now()
        with self._lock:
            self._closed = True
            originals = tuple(self._launches.values())
        for launch in originals:
            self.fence(launch)
        tasks = {self._ensure_cleanup(launch) for launch in originals if not launch._settled}
        if tasks:
            await asyncio.wait(tasks, timeout=_WAIT)
        return self._observation(originals)
