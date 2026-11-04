"""Original Linux cgroup-v2 custody, separate from native admission/budgeting.

An unresolved acquisition remains owned, including every descriptor returned
before failure. Locks have one order: MediaWorkerPool then this pool. A scope
uses its pool's lock and never takes the worker lock or calls user callbacks.
"""

from __future__ import annotations

import asyncio
import ctypes
import fcntl
import os
import re
import secrets
import stat
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MediaCgroupError",
    "MediaCgroupPool",
    "MediaCgroupScope",
    "MediaCgroupSnapshot",
    "MediaCgroupRetirement",
]

_ERROR = "Invalid native media cgroup operation"
_CODES = {"bad_input", "unavailable", "conflict", "capacity", "expired", "unknown"}
_MAGIC = 0x63677270
_LEAF = re.compile(r"kinaiya-media-[0-9a-f]{32}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_U64 = (1 << 64) - 1
_LIMIT = 8192
_POOLS = {}
_CLOSED_POOLS = weakref.WeakValueDictionary()
_LAUNCH_CUSTODY = weakref.WeakValueDictionary()


class MediaCgroupError(ValueError):
    def __init__(self, code="unknown"):
        self.code = code if type(code) is str and code in _CODES else "unknown"
        super().__init__(_ERROR)


def _error(code="unknown"):
    raise MediaCgroupError(code) from None


class _FD:
    """A sole close attempt consumes the number even if its outcome is unknown."""

    def __init__(self):
        self.fd = None
        self.identity = None
        self.closed = False
        self.unknown = False

    def open(self, name, flags, *, parent=None):
        if self.fd is not None or self.unknown or (self.identity is not None and not self.closed):
            _error()
        # Reuse is permitted only after the previous original close was
        # positive. Publish the next observation owner before its open.
        self.closed = False
        self.identity = None
        try:
            self.fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            info = os.fstat(self.fd)
            self.identity = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        except BaseException:
            self.unknown = True
            raise
        return self.fd

    def verify(self):
        if self.fd is None or self.unknown or self.closed or self.identity is None:
            _error()
        try:
            info = os.fstat(self.fd)
        except OSError:
            self.unknown = True
            _error()
        if (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) != self.identity:
            self.unknown = True
            _error()
        return info

    def close(self):
        if self.fd is None:
            return self.closed and not self.unknown
        if self.unknown:
            return False
        try:
            self.verify()
        except MediaCgroupError:
            return False
        fd, self.fd = self.fd, None
        try:
            os.close(fd)
        except BaseException:
            self.unknown = True
            return False
        self.closed = True
        return not self.unknown


class _Statfs(ctypes.Structure):
    _fields_ = [("f_type", ctypes.c_long), ("_pad", ctypes.c_char * 512)]


def _cgroup2(fd):
    buf = _Statfs()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.fstatfs(fd, ctypes.byref(buf)) != 0:
            _error()
    except MediaCgroupError:
        raise
    except Exception:
        _error()
    return buf.f_type == _MAGIC


def _read_path(pool, path, limit):
    # One bounded reusable original is attached before any acquisition,
    # including constructor observations. An uncertain owner is never replaced.
    pool._identity()
    with pool._lock:
        owner = pool._observation
        if owner.fd is not None or not owner.closed or owner.unknown:
            pool._unknown = True
            _error()
        failure = None
        raw = None
        try:
            owner.open(path, os.O_RDONLY)
            raw = os.pread(owner.fd, limit + 1, 0)
            if len(raw) > limit or not raw:
                _error()
        except BaseException as error:
            pool._unknown = True
            failure = MediaCgroupError("unknown") if isinstance(error, Exception) else error
        finally:
            if not owner.close():
                pool._unknown = True
                if failure is None or isinstance(failure, Exception):
                    failure = MediaCgroupError("unknown")
        if failure is not None:
            raise failure from None
        return raw


def _boot_id(pool):
    raw = _read_path(pool, "/proc/sys/kernel/random/boot_id", 37)
    if re.fullmatch(rb"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\n?", raw) is None:
        _error()
    return raw[:36].replace(b"-", b"").decode("ascii")


def _boottime_ns():
    try:
        value = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    except Exception:
        _error()
    if type(value) is not int or not 0 <= value <= _U64:
        _error()
    return value


def _cgroup_rel(raw):
    try:
        text = raw.decode("ascii", "strict")
    except UnicodeError:
        _error()
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        _error()
    value = lines[0][3:]
    if "\x00" in value or any(part in (".", "..") for part in value.split("/")):
        _error()
    return value


def _mount(pool, path):
    # Mount root matters for bind mounts/cgroup namespaces. Decode only the
    # kernel mountinfo escapes, never interpret arbitrary backslash sequences.
    raw = _read_path(pool, "/proc/self/mountinfo", 65536)
    try:
        text = raw.decode("ascii", "strict")
    except UnicodeError:
        _error()
    if "\r" in text or "\x00" in text:
        _error()
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()

    def unescape(value):
        return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), value)

    matches = []
    for line in lines:
        left, sep, right = line.partition(" - ")
        a, b = left.split(), right.split()
        if not sep or len(a) < 6 or len(b) < 3 or b[0] != "cgroup2":
            continue
        mountroot, point = unescape(a[3]), unescape(a[4])
        if path == point or path.startswith(point.rstrip("/") + "/"):
            matches.append((point, mountroot))
    if not matches:
        _error("unavailable")
    point, mountroot = max(matches, key=lambda pair: len(pair[0]))
    suffix = path[len(point) :]
    if not suffix or suffix == "/":
        _error("bad_input")
    return (mountroot.rstrip("/") + suffix).rstrip("/")


def _parse(raw, required):
    if not raw or len(raw) > _LIMIT:
        _error()
    try:
        text = raw.decode("ascii", "strict")
    except UnicodeError:
        _error()
    if "\r" in text or "\x00" in text:
        _error()
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    result = {}
    for line in lines:
        parts = line.split(" ")
        if len(parts) != 2:
            _error()
        key, value = parts
        # The kernel emits namespaced keys such as core_sched.force_idle_usec.
        # Keep bounded identifier components; required fields and all numeric
        # and duplicate checks apply equally to the optional kernel fields.
        if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", key) is None or key in result:
            _error()
        if re.fullmatch(r"[0-9]{1,20}", value) is None:
            _error()
        number = int(value)
        if number > _U64:
            _error()
        result[key] = number
    if not required <= result.keys():
        _error()
    return result


def _validate_observation(value, names):
    for name in ("instance_id", "boot_id"):
        item = getattr(value, name)
        if type(item) is not str or _ID.fullmatch(item) is None:
            _error("bad_input")
    for name in names:
        item = getattr(value, name)
        if type(item) is not int or not 0 <= item <= _U64:
            _error("bad_input")


@dataclass(frozen=True, slots=True)
class MediaCgroupSnapshot:
    instance_id: str
    boot_id: str
    device: int
    inode: int
    usage_usec: int
    observed_boottime_ns: int
    populated: bool
    frozen: bool

    def __post_init__(self):
        _validate_observation(self, ("device", "inode", "usage_usec", "observed_boottime_ns"))
        if type(self.populated) is not bool or type(self.frozen) is not bool:
            _error("bad_input")


@dataclass(frozen=True, slots=True)
class MediaCgroupRetirement:
    instance_id: str
    boot_id: str
    device: int
    inode: int
    usage_usec: int
    observed_boottime_ns: int

    def __post_init__(self):
        _validate_observation(self, ("device", "inode", "usage_usec", "observed_boottime_ns"))


class _Acquisition:
    def __init__(self, pool, launch, binding):
        self.pool, self.launch, self.binding = pool, launch, binding
        self.leaf_name = self.instance_id = None
        self.directory = _FD()
        self.attributes = {}
        self.scope = None
        self.failed = False
        self.retired = False


class MediaCgroupPool:
    def __init__(self, delegated_root: Path, *, capacity: int = 2):
        if type(self) is not MediaCgroupPool or id(self) in _POOLS or id(self) in _CLOSED_POOLS:
            _error("conflict")
        if sys.platform != "linux" or os.uname().machine != "x86_64":
            _error("unavailable")
        if type(capacity) is not int or capacity not in (1, 2):
            _error("bad_input")
        if not isinstance(delegated_root, Path) or not delegated_root.is_absolute():
            _error("bad_input")
        parts = delegated_root.parts
        if "\x00" in str(delegated_root) or any(part == ".." for part in parts):
            _error("bad_input")
        self._creator_pid = os.getpid()
        self._lock = threading.RLock()
        self._capacity = capacity
        self._closed = self._unknown = False
        self._chain = []
        self._observation = _FD()
        self._observation.closed = True
        self._scopes, self._retired_scope, self._acquisitions = {}, {}, {}
        self._orphans = set()
        _POOLS[id(self)] = self
        try:
            previous = None
            # Retain each original parent so replacement/symlink races cannot
            # make a later effect select another directory by a fresh path.
            for part in parts:
                owner = _FD()
                self._chain.append((part, owner))
                owner.open(part, os.O_RDONLY | os.O_DIRECTORY, parent=previous)
                previous = owner.fd
            self._root = self._chain[-1][1]
            info = self._root.verify()
            if info.st_uid != os.getuid() or not _cgroup2(self._root.fd):
                _error("bad_input")
            self._boot = _boot_id(self)
            self._relative = _mount(self, str(delegated_root))
            self._supervisor = self._relative + "/supervisor"
            if _cgroup_rel(_read_path(self, "/proc/self/cgroup", 4096)) != self._supervisor:
                _error("unavailable")
            # Select no app, Chromium, user-manager or init scope. A dedicated
            # service must place us in its immediate explicit supervisor child.
            if (
                parts[-1].endswith(".scope")
                or parts[-1].startswith("user@")
                or parts[-1] in ("app.slice", "init.scope")
            ):
                _error("bad_input")
            supervisor = os.stat("supervisor", dir_fd=self._root.fd, follow_symlinks=False)
            if not stat.S_ISDIR(supervisor.st_mode):
                _error("bad_input")
            self._orphans = {name for name in os.listdir(self._root.fd) if _LEAF.fullmatch(name)}
            self._verify_root()
        except BaseException as error:
            positive = self._observation.close() and not self._unknown
            for _, owner in reversed(self._chain):
                positive = owner.close() and positive
            self._unknown = not positive
            if positive:
                _POOLS.pop(id(self), None)
            if not isinstance(error, Exception):
                raise
            if not positive:
                _error()
            if isinstance(error, MediaCgroupError):
                raise error from None
            _error("bad_input" if isinstance(error, (OSError, ValueError)) else "unknown")

    def _identity(self):
        if (
            type(self) is not MediaCgroupPool
            or os.getpid() != getattr(self, "_creator_pid", None)
            or not (_POOLS.get(id(self)) is self or _CLOSED_POOLS.get(id(self)) is self)
        ):
            _error("conflict")

    def _verify_root(self):
        self._identity()
        if self._closed:
            _error("expired")
        if self._unknown:
            _error()
        try:
            parent = None
            for name, owner in self._chain:
                info = owner.verify()
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
                    info.st_dev,
                    info.st_ino,
                ):
                    _error()
                parent = owner.fd
            if not _cgroup2(self._root.fd) or _boot_id(self) != self._boot:
                _error()
            if _cgroup_rel(_read_path(self, "/proc/self/cgroup", 4096)) != self._supervisor:
                _error()
        except Exception:
            self._unknown = True
            _error()

    def create(self, launch):
        from packages.resources.media_processes import MediaLaunch, MediaWorkerPool, _binding_bytes

        self._identity()
        if (
            type(launch) is not MediaLaunch
            or type(getattr(launch, "_pool", None)) is not MediaWorkerPool
        ):
            _error("conflict")
        try:
            launch._owned()
        except Exception:
            _error("conflict")
        with launch._pool._lock, self._lock:
            self._verify_root()
            try:
                binding = _binding_bytes(launch._binding)
            except Exception:
                _error("conflict")
            previous = _LAUNCH_CUSTODY.get(id(launch))
            if previous is not None:
                if (
                    previous.launch is not launch
                    or previous.pool is not self
                    or previous.binding != binding
                ):
                    _error("conflict")
                if previous.failed:
                    _error()
                if previous.retired:
                    _error("expired")
                if previous.scope is None:
                    _error()
                previous.scope._identity()
                return previous.scope
            if launch._fenced or launch._settled or launch._pool._closed:
                _error("expired")
            if launch._start_requested or launch._granted or launch._start_mode is not None:
                _error("conflict")
            if (
                sum(not op.retired for op in self._acquisitions.values()) + len(self._orphans)
                >= self._capacity
            ):
                _error("capacity")
            # Retain the exact operation BEFORE randomness/mkdir/open. A failed
            # acquisition cannot become another leaf on replay or in another pool.
            op = _Acquisition(self, launch, binding)
            self._acquisitions[id(launch)] = op
            _LAUNCH_CUSTODY[id(launch)] = op
            launch._cgroup_custody_owner = self
            try:
                op.instance_id = secrets.token_hex(16)
                if type(op.instance_id) is not str or _ID.fullmatch(op.instance_id) is None:
                    _error()
                op.leaf_name = "kinaiya-media-" + op.instance_id
                self._verify_root()
                os.mkdir(op.leaf_name, mode=0o700, dir_fd=self._root.fd)
                op.directory.open(op.leaf_name, os.O_RDONLY | os.O_DIRECTORY, parent=self._root.fd)
                info = op.directory.verify()
                if info.st_uid != os.getuid() or not _cgroup2(op.directory.fd):
                    _error()
                for name, access in (
                    ("cpu.stat", os.O_RDONLY),
                    ("cgroup.events", os.O_RDONLY),
                    ("cgroup.freeze", os.O_WRONLY),
                    ("cgroup.kill", os.O_WRONLY),
                    ("cgroup.procs", os.O_WRONLY),
                ):
                    owner = _FD()
                    op.attributes[name] = owner
                    owner.open(name, access, parent=op.directory.fd)
                scope = object.__new__(MediaCgroupScope)
                scope._pool, scope._operation = self, op
                scope._creator_pid, scope._lock = os.getpid(), self._lock
                scope._fenced = scope._unknown = scope._closed = False
                scope._last_usage = scope._last_boottime = None
                scope._empty = scope._retirement = None
                op.scope = scope
                self._scopes[op.leaf_name] = scope
                scope._verify_self()
                return scope
            except BaseException as error:
                op.failed = True
                if op.scope is not None:
                    op.scope._unknown = True
                if not isinstance(error, Exception):
                    raise
                _error()

    def _scope_for_cleanup(self, launch):
        """Resolve even unused custody; never turn uncertain creation into None."""
        self._identity()
        with self._lock:
            op = self._acquisitions.get(id(launch))
            if op is None or op.launch is not launch or _LAUNCH_CUSTODY.get(id(launch)) is not op:
                _error("conflict")
            if op.failed or op.scope is None:
                _error()
            op.scope._launch(launch)
            return op.scope

    def close(self):
        self._identity()
        with self._lock:
            if self._closed:
                return True
            if (
                self._unknown
                or self._orphans
                or any(not op.retired for op in self._acquisitions.values())
            ):
                return False
            try:
                self._verify_root()
            except MediaCgroupError:
                return False
            positive = self._observation.close()
            for _, owner in reversed(self._chain):
                positive = owner.close() and positive
            if not positive:
                self._unknown = True
                return False
            self._closed = True
            _CLOSED_POOLS[id(self)] = self
            del _POOLS[id(self)]
            return True


class MediaCgroupScope:
    def _identity(self):
        if type(self) is not MediaCgroupScope or os.getpid() != getattr(self, "_creator_pid", None):
            _error("conflict")
        pool, op = getattr(self, "_pool", None), getattr(self, "_operation", None)
        if type(pool) is not MediaCgroupPool or type(op) is not _Acquisition:
            _error("conflict")
        pool._identity()
        registry = pool._retired_scope if op.retired else pool._scopes
        if (
            registry.get(op.leaf_name) is not self
            or op.scope is not self
            or pool._acquisitions.get(id(op.launch)) is not op
            or _LAUNCH_CUSTODY.get(id(op.launch)) is not op
        ):
            _error("conflict")

    def _launch(self, launch):
        from packages.resources.media_processes import MediaLaunch, MediaWorkerPool, _binding_bytes

        self._identity()
        op = self._operation
        if (
            type(launch) is not MediaLaunch
            or op.launch is not launch
            or type(launch._pool) is not MediaWorkerPool
        ):
            _error("conflict")
        try:
            launch._owned()
        except Exception:
            _error("conflict")
        try:
            binding = _binding_bytes(launch._binding)
        except Exception:
            _error("conflict")
        if (
            binding != op.binding
            or getattr(launch, "_cgroup_custody_owner", None) is not self._pool
        ):
            _error("conflict")

    def _verify_self(self):
        self._identity()
        if self._closed or self._operation.retired:
            _error("expired")
        if self._unknown or self._operation.failed:
            _error()
        try:
            self._pool._verify_root()
            op = self._operation
            info = op.directory.verify()
            named = os.stat(op.leaf_name, dir_fd=self._pool._root.fd, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                _error()
            if not _cgroup2(op.directory.fd):
                _error()
            for name, owner in op.attributes.items():
                attr = owner.verify()
                named_attr = os.stat(name, dir_fd=op.directory.fd, follow_symlinks=False)
                if (attr.st_dev, attr.st_ino) != (
                    named_attr.st_dev,
                    named_attr.st_ino,
                ) or not stat.S_ISREG(attr.st_mode):
                    _error()
                access = fcntl.fcntl(owner.fd, fcntl.F_GETFL) & os.O_ACCMODE
                if access != (
                    os.O_RDONLY if name in ("cpu.stat", "cgroup.events") else os.O_WRONLY
                ):
                    _error()
            self._launch(op.launch)
        except Exception:
            self._unknown = True
            _error()

    def _check_bindable_for(self, launch):
        self._identity()
        with self._lock:
            self._launch(launch)
            self._verify_self()
            if self._fenced or launch._fenced or launch._settled or launch._pool._closed:
                _error("expired")

    def _borrow_enrollment_fd(self, launch):
        self._identity()
        with self._lock:
            self._check_bindable_for(launch)
            if getattr(launch, "_cgroup_scope", None) is not self:
                _error("conflict")
            return self._operation.attributes["cgroup.procs"].fd

    def _owns_pid(self, pid):
        self._identity()
        if type(pid) is not int or pid <= 0:
            _error("bad_input")
        with self._lock:
            self._check_bindable_for(self._operation.launch)
            try:
                raw = _read_path(self._pool, f"/proc/{pid}/cgroup", 4096)
                result = _cgroup_rel(raw) == self._pool._relative + "/" + self._operation.leaf_name
                self._verify_self()
                return result
            except Exception:
                self._unknown = True
                _error()

    def snapshot(self):
        self._identity()
        with self._lock:
            self._verify_self()
            try:
                attrs = self._operation.attributes
                cpu = _parse(os.pread(attrs["cpu.stat"].fd, _LIMIT + 1, 0), {"usage_usec"})
                events = _parse(
                    os.pread(attrs["cgroup.events"].fd, _LIMIT + 1, 0), {"populated", "frozen"}
                )
                now = _boottime_ns()
                if events["populated"] not in (0, 1) or events["frozen"] not in (0, 1):
                    _error()
                if self._last_usage is not None and cpu["usage_usec"] < self._last_usage:
                    _error()
                if self._last_boottime is not None and now < self._last_boottime:
                    _error()
                self._verify_self()
                self._last_usage, self._last_boottime = cpu["usage_usec"], now
                dev, ino, _ = self._operation.directory.identity
                return MediaCgroupSnapshot(
                    self._operation.instance_id,
                    self._pool._boot,
                    dev,
                    ino,
                    self._last_usage,
                    now,
                    bool(events["populated"]),
                    bool(events["frozen"]),
                )
            except Exception:
                self._unknown = True
                _error()

    def _write(self, name, value):
        self._verify_self()
        try:
            if os.write(self._operation.attributes[name].fd, value) != len(value):
                _error()
            self._verify_self()
        except Exception:
            self._unknown = True
            _error()

    def request_freeze(self):
        self._identity()
        with self._lock:
            self._write("cgroup.freeze", b"1")

    def request_thaw(self):
        self._identity()
        with self._lock:
            if self._fenced:
                _error("expired")
            self._write("cgroup.freeze", b"0")

    def fence(self):
        self._identity()
        with self._lock:
            self._fenced = True

    def request_kill(self):
        self._identity()
        with self._lock:
            self._fenced = True
            self._empty = None
            self._write("cgroup.kill", b"1")

    @staticmethod
    def _joined_task(task, loop):
        return (
            type(task) is asyncio.Task
            and task.get_loop() is loop
            and task.done()
            and not task.cancelled()
        )

    def _no_future_setup(self, launch):
        from packages.resources.media_processes import MediaLaunch
        from packages.resources.processes import WorkerManager

        self._launch(launch)
        if launch._future_spawn or launch._spawn_phase or not launch._fenced:
            return False
        if launch._start_requested and (
            not launch._start_assigned.is_set()
            or not self._joined_task(launch._start_task, launch._pool._loop)
            or launch._start_task.get_coro().cr_code
            is not WorkerManager._start_media_owned.__code__
        ):
            return False
        if launch._release_requested and (
            not launch._release_assigned.is_set()
            or not self._joined_task(launch._release_task, launch._pool._loop)
            or launch._release_task.get_coro().cr_code is not MediaLaunch._release_prepared.__code__
        ):
            return False
        return True

    def _observe_empty_for_pool(self, launch):
        self._identity()
        with self._lock:
            self._verify_self()
            if not self._no_future_setup(launch) or not launch._outer_positive:
                return None
            snap = self.snapshot()
            if snap.populated:
                return None
            self._empty = snap
            return snap

    def _pool_empty_matches(self, launch, snapshot):
        # Memory/provenance check only: no kernel IO and no opposite mutex.
        try:
            self._identity()
            with self._lock:
                return bool(
                    not self._unknown
                    and not self._pool._unknown
                    and not self._operation.failed
                    and not self._operation.retired
                    and snapshot is not None
                    and snapshot is self._empty
                    and type(snapshot) is MediaCgroupSnapshot
                    and not snapshot.populated
                    and self._no_future_setup(launch)
                    and launch._outer_positive
                )
        except Exception:
            return False

    def close_after_settle(self, launch):
        from packages.resources.media_processes import MediaLaunch

        self._identity()
        with self._lock:
            self._launch(launch)
            if self._retirement is not None:
                return self._retirement
            self._verify_self()
            if not self._no_future_setup(launch):
                _error("expired")
            cleanup = launch._cleanup_task
            if not self._joined_task(cleanup, launch._pool._loop):
                _error("expired")
            if cleanup.get_coro().cr_code is not MediaLaunch._cleanup.__code__:
                _error("conflict")
            try:
                actual_cleanup = cleanup.result()
            except BaseException:
                _error()
            if type(actual_cleanup) is not bool or not actual_cleanup:
                _error("expired")
            if (
                not all(
                    (
                        launch._settled,
                        launch._outer_positive,
                        launch._io_positive,
                        launch._dependencies_positive,
                        launch._grant_positive,
                        launch._parent_source_closed,
                    )
                )
                or launch._close_unknown
                or launch._metadata_unknown
            ):
                _error("expired")
            on_launch = getattr(launch, "_cgroup_scope", None)
            if on_launch is not self and (
                on_launch is not None or launch._start_requested or launch._spawn_dispatched
            ):
                _error("conflict")
            if on_launch is self and not self._pool_empty_matches(
                launch, getattr(launch, "_cgroup_empty_snapshot", None)
            ):
                _error("expired")
            self._fenced = True
            final = self.snapshot()
            if final.populated:
                _error("expired")
            op = self._operation
            self._verify_self()
            try:
                os.rmdir(op.leaf_name, dir_fd=self._pool._root.fd)
            except BaseException as error:
                self._unknown = True
                if not isinstance(error, Exception):
                    raise
                _error()
            # Close every original exactly once; uncertainty retains the whole
            # acquisition charge even if the kernel directory is already gone.
            positive = True
            for owner in (*op.attributes.values(), op.directory):
                positive = owner.close() and positive
            if not positive:
                self._unknown = True
                _error()
            receipt = MediaCgroupRetirement(
                final.instance_id,
                final.boot_id,
                final.device,
                final.inode,
                final.usage_usec,
                final.observed_boottime_ns,
            )
            self._retirement = receipt
            self._closed = op.retired = True
            del self._pool._scopes[op.leaf_name]
            self._pool._retired_scope[op.leaf_name] = self
            return receipt
