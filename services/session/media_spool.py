"""Protected local media bytes, without Session authority or cleanup receipts.

The catalog is a bounded fixed-record private ledger, not a second admission
database. Restarted records remain charged and cannot recreate original handles.
The native owner must protect this private namespace: a stat followed by unlink
is not an atomic inode-conditional operation against a hostile privileged actor.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import struct
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from functools import wraps
from pathlib import Path, PosixPath

from packages.contracts.media import MediaLimits, media_bytes, media_digest, media_json

try:
    import fcntl
except ImportError:  # A host without the original OS-lock seam is unavailable.
    fcntl = None

_ERROR = "Invalid media spool operation"
_SAFE = 9_007_199_254_740_991
_METADATA_LIMIT = 1_073_741_824
_BLOCK = 4096
_HEADER = struct.Struct(">8sI32s")
_MAGIC = b"KMSPOOL1"
_CHUNK = struct.Struct(">QQQ32s32s")
_CHUNK_BODY = struct.Struct(">QQQ32s")
_READ = 65_536
# Selected initial helper bound; positive closes free slots, history is retained.
_MAX_READERS = 16
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LOCK = ".media.lock"
_POLICY = ".media.policy"
_CATALOG = ".media.catalog"
_STATES = {
    "allocating",
    "open",
    "writing",
    "sealing",
    "sealed",
    "closing",
    "closed",
    "delete_pending",
    "deleted",
    "unknown",
}
_RECORD_FIELDS = {
    "v",
    "source_id",
    "native_instance",
    "policy_sha256",
    "reserved_bytes",
    "data_identity",
    "ledger_identity",
    "next_offset",
    "chunks",
    "sealed",
    "state",
    "pending",
}


class MediaSpoolError(ValueError):
    """A fixed public classification, with no paths or operating-system text."""

    def __init__(self, code: str = "unavailable") -> None:
        self.code = (
            code
            if type(code) is str
            and code in {"bad_packet", "bound", "capacity", "conflict", "unavailable", "expired"}
            else "unavailable"
        )
        super().__init__(_ERROR)


def _error(code="unavailable"):
    raise MediaSpoolError(code)


def _public(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except MediaSpoolError as error:
            if error.code == "unavailable":
                if isinstance(self, MediaSpool):
                    self._uncertain = True
                elif isinstance(self, SpoolUpload):
                    self._mark_unknown()
                else:
                    self._upload._mark_unknown()
            raise
        except BaseException as error:
            if isinstance(self, MediaSpool):
                self._uncertain = True
            elif isinstance(self, SpoolUpload):
                self._mark_unknown()
            else:
                self._upload._mark_unknown()
            if not isinstance(error, Exception):
                raise
            raise MediaSpoolError() from None

    return call


def _integer(value, *, positive=False):
    if type(value) is not int:
        _error("bad_packet")
    if not (1 if positive else 0) <= value <= _SAFE:
        _error("bound")
    return value


def _identifier(value):
    if type(value) is not str or _ID.fullmatch(value) is None:
        _error("bad_packet")
    return value


def _digest(value):
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _error("bad_packet")
    return value


def _identity(info):
    return [info.st_dev, info.st_ino]


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_nlink != 1
    ):
        _error()


def _write(fd, raw, offset):
    # A short write is uncertain, even if the remaining suffix could be retried.
    if os.pwrite(fd, raw, offset) != len(raw):
        _error()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error()
        result[key] = value
    return result


def _invalid(_):
    _error()


def _record_bytes(record):
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > _BLOCK - _HEADER.size:
        _error("bound")
    return (
        _HEADER.pack(_MAGIC, len(raw), hashlib.sha256(raw).digest())
        + raw
        + bytes(_BLOCK - _HEADER.size - len(raw))
    )


@dataclass(frozen=True, slots=True)
class SpoolProgress:
    index: int
    offset: int
    body_bytes: int
    body_sha256: str
    next_offset: int


@dataclass(slots=True)
class _Directory:
    fd: int | None = None
    _uncertain: bool = False


class MediaSpool:
    """One original exclusive owner of an existing private local directory."""

    def __init__(self, root: Path, limits: MediaLimits) -> None:
        self._mutex = threading.RLock()
        self._root_directory = _Directory()
        self._directory_walk = []
        self._lock_fd = self._policy_fd = self._catalog_fd = None
        self._history_data_fd = self._history_ledger_fd = None
        self._uncertain = False
        self._closing = self._closed = False
        self._historical_unknown = False
        self._uploads = {}
        self._reserved = self._remaining = self._active = self._unsealed = 0
        self._metadata = self._catalog_size = 0
        self._instance = uuid.uuid4().hex
        try:
            if type(root) not in (Path, PosixPath) or fcntl is None:
                _error("bad_packet" if fcntl is not None else "unavailable")
            if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY")):
                _error()
            policy = media_bytes(limits)
            self._limits = media_json(policy, MediaLimits)
            self._policy_digest = media_digest("Limits", self._limits)
            self._policy_bytes = policy
            self._open_root(root)
            info = os.fstat(self._dir_fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                _error()
            self._directory_identity = _identity(info)
            if self._exists(_LOCK):
                self._lock_fd = self._open(_LOCK, os.O_RDWR)
            else:
                with os.scandir(self._dir_fd) as items:
                    if any(items):
                        _error()
                try:
                    self._lock_fd = self._open(_LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL)
                except FileExistsError:
                    _error("conflict")
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    _error("conflict")
                raise
            lock = os.fstat(self._lock_fd)
            if lock.st_size:
                _error()
            self._lock_identity = _identity(lock)
            self._lock_stamp = _stamp(lock)
            self._load(policy)
        except BaseException as error:
            # Each original integer is retired at most once, including startup.
            if not isinstance(error, Exception):
                self._uncertain = True
            try:
                self._startup_close()
            except BaseException:
                if not isinstance(error, Exception):
                    raise error from None
                raise
            if isinstance(error, MediaSpoolError):
                raise
            if not isinstance(error, Exception):
                raise
            code = getattr(error, "code", "unavailable")
            raise MediaSpoolError(code) from None

    @property
    def _dir_fd(self):
        return self._root_directory.fd

    @_dir_fd.setter
    def _dir_fd(self, value):
        self._root_directory.fd = value

    def _open_root(self, root):
        # Relative paths are anchored to one original cwd descriptor. Every
        # component is opened beneath the preceding original directory, without
        # realpath normalization or following any symlink. Parent traversal is
        # rejected; only the final selected private directory must be owner0700.
        parts = root.parts
        if root.anchor not in ("", "/") or ".." in parts:
            _error("bad_packet")
        if len(parts) > 256 or len(os.fsencode(root)) > 4096:
            _error("bound")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = _Directory()
        self._directory_walk.append(current)
        current.fd = os.open("/" if root.is_absolute() else ".", flags)
        for part in parts[1:] if root.is_absolute() else parts:
            child = _Directory()
            self._directory_walk.append(child)
            child.fd = os.open(part, flags, dir_fd=current.fd)
            if not self._retire(current, "fd"):
                _error()
            current = child
        self._root_directory = current

    def _open(self, name, flags):
        fd = os.open(
            name,
            flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=self._dir_fd,
        )
        try:
            _regular(os.fstat(fd))
        except BaseException as error:
            # No retry after an ambiguous close, and no untracked raw descriptor.
            try:
                os.close(fd)
            except BaseException:
                self._uncertain = True
                if not isinstance(error, Exception):
                    raise error from None
                raise
            raise
        return fd

    def _retire(self, owner, attribute):
        fd = getattr(owner, attribute)
        if fd is None:
            return True
        try:
            setattr(owner, attribute, None)
            os.close(fd)
            return True
        except BaseException as error:
            # close may already have reused its integer: never close it again.
            self._uncertain = True
            if owner is not self:
                owner._uncertain = True
            if not isinstance(error, Exception):
                raise
            return False

    def _startup_close(self):
        for attribute in ("_history_data_fd", "_history_ledger_fd", "_catalog_fd", "_policy_fd"):
            self._retire(self, attribute)
        for directory in self._directory_walk:
            if directory is not self._root_directory:
                self._retire(directory, "fd")
        if not self._uncertain:
            self._retire(self, "_dir_fd")
        if not self._uncertain:
            self._retire(self, "_lock_fd")

    def _path_info(self, name, identity=None):
        info = os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
        _regular(info)
        if identity is not None and _identity(info) != identity:
            _error()
        return info

    def _exists(self, name):
        try:
            os.stat(name, dir_fd=self._dir_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _storage(self):
        if self._uncertain or self._closed or self._dir_fd is None:
            _error()
        info = os.fstat(self._dir_fd)
        if (
            _identity(info) != self._directory_identity
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            _error()
        for name, attribute, identity in (
            (_LOCK, "_lock_fd", self._lock_identity),
            (_POLICY, "_policy_fd", self._policy_identity),
            (_CATALOG, "_catalog_fd", self._catalog_identity),
        ):
            fd = getattr(self, attribute)
            if fd is None or _identity(os.fstat(fd)) != identity:
                _error()
            self._path_info(name, identity)
        if _stamp(os.fstat(self._policy_fd)) != self._policy_stamp:
            _error()
        if _stamp(os.fstat(self._catalog_fd)) != self._catalog_stamp:
            _error()
        if _stamp(os.fstat(self._lock_fd)) != self._lock_stamp:
            _error()

    def _available(self, extra_metadata=0, extra_reserved=0, spent=0):
        if self._metadata + extra_metadata > _METADATA_LIMIT:
            _error("capacity")
        fs = os.fstatvfs(self._dir_fd)
        if fs.f_bavail * fs.f_frsize < self._remaining + extra_reserved - spent + extra_metadata:
            _error("capacity")

    def _read_record(self, slot):
        raw = os.pread(self._catalog_fd, _BLOCK, slot * _BLOCK)
        if len(raw) != _BLOCK:
            _error()
        magic, size, digest = _HEADER.unpack(raw[: _HEADER.size])
        if magic != _MAGIC or not 1 <= size <= _BLOCK - _HEADER.size:
            _error()
        body = raw[_HEADER.size : _HEADER.size + size]
        if hashlib.sha256(body).digest() != digest or any(raw[_HEADER.size + size :]):
            _error()
        try:
            record = json.loads(
                body, object_pairs_hook=_pairs, parse_float=_invalid, parse_constant=_invalid
            )
            self._validate_record(record)
            if _record_bytes(record) != raw:
                _error()
            return record
        except (ValueError, TypeError, OverflowError):
            raise MediaSpoolError() from None

    def _validate_record(self, record):
        if type(record) is not dict or record.keys() != _RECORD_FIELDS:
            _error()
        if type(record["v"]) is not int or record["v"] != 1:
            _error()
        _identifier(record["source_id"])
        _identifier(record["native_instance"])
        if record["policy_sha256"] != self._policy_digest:
            _error()
        for name in ("reserved_bytes", "next_offset", "chunks"):
            _integer(record[name], positive=name == "reserved_bytes")
        if (
            record["reserved_bytes"] > self._limits.source.file_bytes
            or record["next_offset"] > record["reserved_bytes"]
            or record["chunks"] > record["next_offset"]
            or type(record["sealed"]) is not bool
            or type(record["state"]) is not str
            or record["state"] not in _STATES
        ):
            _error()
        for name in ("data_identity", "ledger_identity"):
            value = record[name]
            if value is not None and (
                type(value) is not list
                or len(value) != 2
                or any(type(x) is not int or not 0 <= x < 2**64 for x in value)
            ):
                _error()
        pending = record["pending"]
        if record["state"] == "writing":
            if type(pending) is not dict or pending.keys() != {"bytes", "sha256"}:
                _error()
            _integer(pending["bytes"], positive=True)
            _digest(pending["sha256"])
            if pending["bytes"] > min(4_194_304, self._limits.source.upload_chunk_bytes):
                _error()
            if record["next_offset"] + pending["bytes"] > record["reserved_bytes"]:
                _error()
        elif pending is not None:
            _error()
        if record["state"] == "allocating" or any(
            record[x] is None for x in ("data_identity", "ledger_identity")
        ):
            if record["next_offset"] or record["chunks"] or record["sealed"]:
                _error()
            if record["state"] not in ("allocating", "unknown"):
                _error()
        if record["sealed"] and not record["next_offset"]:
            _error()
        if (record["state"] in ("open", "writing", "sealing") and record["sealed"]) or (
            record["state"] == "sealed" and not record["sealed"]
        ):
            _error()

    def _put_record(self, slot, record, *, new=False):
        self._storage()
        self._validate_record(record)
        if new:
            if slot * _BLOCK != self._catalog_size:
                _error()
            self._available(_BLOCK)
        _write(self._catalog_fd, _record_bytes(record), slot * _BLOCK)
        os.fsync(self._catalog_fd)
        if new:
            self._catalog_size += _BLOCK
            self._metadata += _BLOCK
        self._catalog_stamp = _stamp(os.fstat(self._catalog_fd))

    def _find(self, source_id):
        for slot in range(self._catalog_size // _BLOCK):
            if self._read_record(slot)["source_id"] == source_id:
                return slot
        return None

    def _validate_history(self, record):
        """Check acknowledged ledger continuity without recovering an owner."""
        if not record["chunks"]:
            if record["next_offset"]:
                _error()
            return
        primary = None
        try:
            self._history_data_fd = self._open(record["source_id"] + ".data", os.O_RDONLY)
            self._history_ledger_fd = self._open(record["source_id"] + ".chunks", os.O_RDONLY)
            data, ledger = self._history_data_fd, self._history_ledger_fd
            if (
                _identity(os.fstat(data)) != record["data_identity"]
                or _identity(os.fstat(ledger)) != record["ledger_identity"]
            ):
                _error()
            offset = 0
            for index in range(record["chunks"]):
                raw = os.pread(ledger, _CHUNK.size, index * _CHUNK.size)
                if len(raw) != _CHUNK.size:
                    _error()
                number, start, size, expected, checksum = _CHUNK.unpack(raw)
                if (
                    number != index
                    or start != offset
                    or hashlib.sha256(raw[: _CHUNK_BODY.size]).digest() != checksum
                    or not 1 <= size <= min(4_194_304, self._limits.source.upload_chunk_bytes)
                    or offset + size > record["next_offset"]
                ):
                    _error()
                digest = hashlib.sha256()
                for position in range(0, size, _READ):
                    count = min(_READ, size - position)
                    body = os.pread(data, count, offset + position)
                    if len(body) != count:
                        _error()
                    digest.update(body)
                if digest.digest() != expected:
                    _error()
                offset += size
            if offset != record["next_offset"]:
                _error()
        except BaseException as error:
            primary = error
            raise
        finally:
            failed = False
            control = None
            for attribute in ("_history_data_fd", "_history_ledger_fd"):
                try:
                    if not self._retire(self, attribute):
                        failed = True
                except BaseException as error:
                    failed = True
                    control = error
            if primary is not None and not isinstance(primary, Exception):
                raise primary
            if control is not None:
                raise control
            if failed:
                _error()

    def _load(self, policy):
        self._metadata = os.fstat(self._lock_fd).st_size
        if self._metadata > _METADATA_LIMIT:
            _error()
        policy_exists, catalog_exists = self._exists(_POLICY), self._exists(_CATALOG)
        if policy_exists != catalog_exists:
            _error()
        if not policy_exists:
            # Creating infrastructure is allowed only inside the selected private root.
            with os.scandir(self._dir_fd) as items:
                if any(item.name != _LOCK for item in items):
                    _error()
            self._available(len(policy))
            self._policy_fd = self._open(_POLICY, os.O_RDWR | os.O_CREAT | os.O_EXCL)
            _write(self._policy_fd, policy, 0)
            os.fsync(self._policy_fd)
            self._catalog_fd = self._open(_CATALOG, os.O_RDWR | os.O_CREAT | os.O_EXCL)
            os.fsync(self._catalog_fd)
            os.fsync(self._dir_fd)
        else:
            self._policy_fd = self._open(_POLICY, os.O_RDWR)
            self._catalog_fd = self._open(_CATALOG, os.O_RDWR)
            if (
                os.fstat(self._policy_fd).st_size != len(policy)
                or os.pread(self._policy_fd, len(policy) + 1, 0) != policy
            ):
                _error("conflict")
        self._policy_identity = _identity(os.fstat(self._policy_fd))
        self._catalog_identity = _identity(os.fstat(self._catalog_fd))
        self._policy_stamp = _stamp(os.fstat(self._policy_fd))
        self._catalog_stamp = _stamp(os.fstat(self._catalog_fd))
        self._catalog_size = self._catalog_stamp[2]
        if self._catalog_size % _BLOCK or self._catalog_size > _METADATA_LIMIT:
            _error()
        self._metadata = 0
        actual_files = 0
        with os.scandir(self._dir_fd) as items:
            for item in items:
                info = self._path_info(item.name)
                if item.name in (_LOCK, _POLICY, _CATALOG):
                    self._metadata += info.st_size
                elif re.fullmatch(r"[0-9a-f]{32}\.(data|chunks)", item.name):
                    actual_files += 1
                    if item.name.endswith(".chunks"):
                        self._metadata += info.st_size
                else:
                    _error()
        if self._metadata > _METADATA_LIMIT:
            _error()
        expected_files = 0
        seen = set()
        for slot in range(self._catalog_size // _BLOCK):
            record = self._read_record(slot)
            source = record["source_id"]
            if source in seen:
                _error()
            # This temporary identity set is bounded by the fixed metadata quota;
            # no historical record or raw source is retained as a resident cache.
            seen.add(source)
            if record["state"] == "deleted":
                if self._exists(source + ".data") or self._exists(source + ".chunks"):
                    _error()
                continue
            self._historical_unknown = True
            self._reserved += record["reserved_bytes"]
            self._remaining += record["reserved_bytes"] - record["next_offset"]
            self._active += 1
            self._unsealed += 1  # Restart cannot borrow any former original owner.
            for suffix, name in ((".data", "data_identity"), (".chunks", "ledger_identity")):
                identity = record[name]
                if identity is None:
                    if self._exists(source + suffix):
                        _error()
                    continue
                expected_files += 1
                info = self._path_info(source + suffix, identity)
                minimum = (
                    record["next_offset"] if suffix == ".data" else record["chunks"] * _CHUNK.size
                )
                allowance = 0
                if record["state"] == "writing":
                    allowance = record["pending"]["bytes"] if suffix == ".data" else _CHUNK.size
                if not minimum <= info.st_size <= minimum + allowance:
                    _error()
            self._validate_history(record)
        if (
            actual_files != expected_files
            or self._reserved
            > min(self._limits.source.owner_spool_bytes, self._limits.source.global_spool_bytes)
            or self._active > self._limits.source.operations_global
        ):
            _error()

    @property
    def reserved_bytes(self) -> int:
        with self._mutex:
            return self._reserved

    @property
    def active_uploads(self) -> int:
        with self._mutex:
            return self._active

    @_public
    def reserve(self, source_id: str, reserved_bytes: int) -> SpoolUpload:
        source_id = _identifier(source_id)
        reserved_bytes = _integer(reserved_bytes, positive=True)
        with self._mutex:
            self._storage()
            if self._closing:
                _error("expired")
            if self._find(source_id) is not None:
                _error("conflict")
            if reserved_bytes > self._limits.source.file_bytes:
                _error("bound")
            if (
                self._reserved + reserved_bytes
                > min(self._limits.source.owner_spool_bytes, self._limits.source.global_spool_bytes)
                or self._active >= self._limits.source.operations_global
                or self._unsealed >= self._limits.source.unsealed_uploads_per_owner
            ):
                _error("capacity")
            self._available(_BLOCK, reserved_bytes)
            record = {
                "v": 1,
                "source_id": source_id,
                "native_instance": self._instance,
                "policy_sha256": self._policy_digest,
                "reserved_bytes": reserved_bytes,
                "data_identity": None,
                "ledger_identity": None,
                "next_offset": 0,
                "chunks": 0,
                "sealed": False,
                "state": "allocating",
                "pending": None,
            }
            upload = SpoolUpload(self, self._catalog_size // _BLOCK, record)
            self._uploads[source_id] = upload
            # Charge before the first potentially durable allocation record.
            self._reserved += reserved_bytes
            self._remaining += reserved_bytes
            self._active += 1
            self._unsealed += 1
            try:
                self._put_record(upload._slot, record, new=True)
                upload._writer_fd = self._open(
                    source_id + ".data", os.O_RDWR | os.O_CREAT | os.O_EXCL
                )
                upload._ledger_fd = self._open(
                    source_id + ".chunks", os.O_RDWR | os.O_CREAT | os.O_EXCL
                )
                record = dict(
                    record,
                    data_identity=_identity(os.fstat(upload._writer_fd)),
                    ledger_identity=_identity(os.fstat(upload._ledger_fd)),
                    state="open",
                )
                os.fsync(upload._writer_fd)
                os.fsync(upload._ledger_fd)
                os.fsync(self._dir_fd)
                self._put_record(upload._slot, record)
                upload._record = record
                upload._refresh()
                self._available()
                upload._current()
                return upload
            except BaseException as error:
                upload._mark_unknown()
                try:
                    upload._retire_own()
                except BaseException:
                    if not isinstance(error, Exception):
                        raise error from None
                    raise
                if not isinstance(error, Exception):
                    raise
                raise MediaSpoolError() from None

    def _finish_close(self):
        if self._closed:
            return True
        if not self._closing or self._uncertain or self._historical_unknown:
            return False
        if any(not upload._settled() for upload in self._uploads.values()):
            return False
        for attribute in ("_catalog_fd", "_policy_fd", "_dir_fd", "_lock_fd"):
            if not self._retire(self, attribute):
                return False
        self._closed = True
        return True

    @_public
    def close(self) -> bool:
        with self._mutex:
            if self._closed:
                return True
            self._closing = True
            for upload in list(self._uploads.values()):
                upload._close_locked()
            return self._finish_close()


class SpoolUpload:
    """The original handle; neither records nor restart construct a replacement."""

    def __init__(self, spool, slot, record):
        self._spool, self._slot, self._record = spool, slot, record
        self._writer_fd = self._ledger_fd = self._grant_fd = None
        self._readers = set()
        self._recent = OrderedDict()
        self._uncertain = self._closed = self._deleted = self._delete_pending = False
        self._data_stamp = self._ledger_stamp = self._seal_stamp = None

    @property
    def source_id(self) -> str:
        return self._record["source_id"]

    @property
    def reserved_bytes(self) -> int:
        return self._record["reserved_bytes"]

    @property
    def next_offset(self) -> int:
        return self._record["next_offset"]

    @property
    def sealed(self) -> bool:
        return self._record["sealed"]

    @property
    def deleted(self) -> bool:
        return self._deleted

    @property
    def delete_pending(self) -> bool:
        return self._delete_pending and not self._deleted

    def _mark_unknown(self):
        self._uncertain = True
        self._spool._uncertain = True

    def _owned(self):
        if self._deleted:
            return
        if (
            self._spool._uploads.get(self.source_id) is not self
            or self._record["native_instance"] != self._spool._instance
        ):
            _error()

    def _retire_own(self):
        result = True
        for attribute in ("_writer_fd", "_ledger_fd", "_grant_fd"):
            result = self._spool._retire(self, attribute) and result
        return result

    def _settled(self):
        return (
            not self._uncertain
            and not self._readers
            and all(
                getattr(self, name) is None for name in ("_writer_fd", "_ledger_fd", "_grant_fd")
            )
        )

    def _refresh(self):
        data_fd = self._writer_fd if self._writer_fd is not None else self._grant_fd
        if data_fd is not None:
            self._data_stamp = _stamp(os.fstat(data_fd))
        if self._ledger_fd is not None:
            self._ledger_stamp = _stamp(os.fstat(self._ledger_fd))

    def _current(self):
        self._owned()
        self._spool._storage()
        if self._uncertain:
            _error()
        if self._closed or self._delete_pending or self._deleted or self._spool._closing:
            _error("expired")
        data_fd = self._writer_fd if self._writer_fd is not None else self._grant_fd
        if data_fd is None or self._ledger_fd is None:
            _error()
        for suffix, fd, identity, stamp in (
            (".data", data_fd, self._record["data_identity"], self._data_stamp),
            (".chunks", self._ledger_fd, self._record["ledger_identity"], self._ledger_stamp),
        ):
            info = os.fstat(fd)
            _regular(info)
            if _identity(info) != identity or _stamp(info) != stamp:
                self._mark_unknown()
                _error()
            self._spool._path_info(self.source_id + suffix, identity)

    def _set(self, **changes):
        record = dict(self._record, **changes)
        self._spool._put_record(self._slot, record)
        self._record = record

    def _remember(self, progress):
        self._recent[progress.index] = progress
        self._recent.move_to_end(progress.index)
        while len(self._recent) > 64:
            self._recent.popitem(last=False)

    def _old_progress(self, index):
        raw = os.pread(self._ledger_fd, _CHUNK.size, index * _CHUNK.size)
        if len(raw) != _CHUNK.size:
            _error()
        number, offset, size, digest, checksum = _CHUNK.unpack(raw)
        if number != index or hashlib.sha256(raw[: _CHUNK_BODY.size]).digest() != checksum:
            _error()
        if not 1 <= size <= min(4_194_304, self._spool._limits.source.upload_chunk_bytes):
            _error()
        if offset + size > self.next_offset:
            _error()
        return SpoolProgress(number, offset, size, digest.hex(), offset + size)

    @_public
    def append(self, index: int, offset: int, body: bytes, sha256: str) -> SpoolProgress:
        index, offset = _integer(index), _integer(offset)
        sha256 = _digest(sha256)
        if type(body) is not bytes or not body:
            _error("bad_packet")
        if len(body) > min(4_194_304, self._spool._limits.source.upload_chunk_bytes):
            _error("bound")
        if hashlib.sha256(body).hexdigest() != sha256:
            _error("bad_packet")
        with self._spool._mutex:
            self._current()
            if index < self._record["chunks"]:
                try:
                    progress = self._old_progress(index)
                    if (progress.offset, progress.body_bytes, progress.body_sha256) != (
                        offset,
                        len(body),
                        sha256,
                    ):
                        _error("conflict")
                    data_fd = self._writer_fd if self._writer_fd is not None else self._grant_fd
                    for position in range(0, len(body), _READ):
                        expected = body[position : position + _READ]
                        if os.pread(data_fd, len(expected), offset + position) != expected:
                            self._mark_unknown()
                            _error()
                    self._current()
                    self._remember(progress)
                    return progress
                except MediaSpoolError as error:
                    if error.code != "conflict":
                        self._mark_unknown()
                    raise
                except OSError:
                    self._mark_unknown()
                    raise MediaSpoolError() from None
            if self.sealed or index != self._record["chunks"] or offset != self.next_offset:
                _error("conflict")
            if offset + len(body) > self.reserved_bytes:
                _error("bound")
            self._spool._available(_CHUNK.size)
            try:
                self._set(state="writing", pending={"bytes": len(body), "sha256": sha256})
                _write(self._writer_fd, body, offset)
                os.fsync(self._writer_fd)
                proof = _CHUNK_BODY.pack(index, offset, len(body), bytes.fromhex(sha256))
                _write(self._ledger_fd, proof + hashlib.sha256(proof).digest(), index * _CHUNK.size)
                self._spool._metadata += _CHUNK.size
                os.fsync(self._ledger_fd)
                self._spool._available(spent=len(body))
                self._set(
                    next_offset=offset + len(body), chunks=index + 1, state="open", pending=None
                )
                self._spool._remaining -= len(body)
                self._refresh()
                self._current()
                self._spool._available()
                progress = SpoolProgress(index, offset, len(body), sha256, offset + len(body))
                self._remember(progress)
                return progress
            except BaseException as error:
                self._mark_unknown()
                if not isinstance(error, Exception):
                    raise
                raise MediaSpoolError() from None

    @_public
    def seal(self, final_bytes: int, sha256: str) -> None:
        final_bytes, sha256 = _integer(final_bytes, positive=True), _digest(sha256)
        with self._spool._mutex:
            self._current()
            if self.sealed or final_bytes != self.next_offset:
                _error("conflict")
            try:
                before = os.fstat(self._writer_fd)
                if before.st_size != final_bytes:
                    _error()
                digest = hashlib.sha256()
                for position in range(0, final_bytes, _READ):
                    size = min(_READ, final_bytes - position)
                    chunk = os.pread(self._writer_fd, size, position)
                    if len(chunk) != size:
                        _error()
                    digest.update(chunk)
                if _stamp(os.fstat(self._writer_fd)) != _stamp(before):
                    _error()
                self._current()
            except BaseException as error:
                self._mark_unknown()
                if not isinstance(error, Exception):
                    raise
                raise MediaSpoolError() from None
            if digest.hexdigest() != sha256:
                _error("conflict")
            try:
                self._set(state="sealing")
                os.fsync(self._writer_fd)
                os.fchmod(self._writer_fd, 0o400)
                os.fsync(self._writer_fd)
                self._grant_fd = self._spool._open(self.source_id + ".data", os.O_RDONLY)
                if _identity(os.fstat(self._grant_fd)) != self._record["data_identity"]:
                    _error()
                if not self._spool._retire(self, "_writer_fd"):
                    _error()
                self._refresh()
                self._seal_stamp = self._data_stamp
                self._set(sealed=True, state="sealed")
                self._spool._unsealed -= 1
                self._current()
                self._spool._available()
            except BaseException as error:
                self._mark_unknown()
                if not isinstance(error, Exception):
                    raise
                raise MediaSpoolError() from None

    @_public
    def open_reader(self) -> SpoolReader:
        with self._spool._mutex:
            self._current()
            if not self.sealed:
                _error("conflict")
            if len(self._readers) >= _MAX_READERS:
                _error("capacity")
            reader = SpoolReader(self)
            try:
                reader._fd = self._spool._open(self.source_id + ".data", os.O_RDONLY)
                if _stamp(os.fstat(reader._fd)) != self._seal_stamp:
                    _error()
                self._current()
                self._readers.add(reader)
                return reader
            except BaseException as error:
                self._mark_unknown()
                # Register the original before retirement, including a control
                # exception raised by close itself. Unknown originals stay here.
                self._readers.add(reader)
                try:
                    if self._spool._retire(reader, "_fd"):
                        self._readers.discard(reader)
                        reader._closed = True
                except BaseException:
                    if not isinstance(error, Exception):
                        raise error from None
                    raise
                if not isinstance(error, Exception):
                    raise
                raise MediaSpoolError() from None

    def _close_locked(self):
        self._owned()
        if self._deleted:
            return True
        if self._closed and self._settled() and self._record["state"] == "closed":
            return True
        self._closed = True
        try:
            if (
                not self._uncertain
                and not self._spool._uncertain
                and not self._delete_pending
                and self._record["state"] not in ("closing", "closed")
            ):
                self._set(state="closing")
        except (OSError, MediaSpoolError):
            self._mark_unknown()
        self._retire_own()
        if not self._settled():
            return False
        if self._delete_pending:
            return self._delete_finished()
        if self._record["state"] != "closed":
            try:
                self._set(state="closed")
            except (OSError, MediaSpoolError):
                self._mark_unknown()
                return False
        return True

    @_public
    def close(self) -> bool:
        with self._spool._mutex:
            self._owned()
            if self._spool._closed:
                return self._settled()
            return self._close_locked()

    def _delete_finished(self):
        self._owned()
        if self._deleted:
            return True
        if not self._settled() or self._spool._uncertain:
            return False
        try:
            self._spool._storage()
            for suffix, name in ((".data", "data_identity"), (".chunks", "ledger_identity")):
                self._spool._path_info(self.source_id + suffix, self._record[name])
                os.unlink(self.source_id + suffix, dir_fd=self._spool._dir_fd)
            os.fsync(self._spool._dir_fd)
            self._set(state="deleted", pending=None)
            # The durable content-free tombstone precedes every released count.
            self._spool._metadata -= self._record["chunks"] * _CHUNK.size
            self._spool._reserved -= self.reserved_bytes
            self._spool._remaining -= self.reserved_bytes - self.next_offset
            self._spool._active -= 1
            if not self.sealed:
                self._spool._unsealed -= 1
            self._deleted = True
            self._recent.clear()
            self._spool._uploads.pop(self.source_id, None)
            return True
        except BaseException as error:
            self._mark_unknown()
            if not isinstance(error, Exception):
                raise
            return False

    @_public
    def delete(self) -> bool:
        with self._spool._mutex:
            self._owned()
            if self._deleted:
                return True
            if self._delete_pending and self._uncertain:
                return False
            self._spool._storage()
            if not self._delete_pending:
                try:
                    self._set(state="delete_pending", pending=None)
                    self._delete_pending = True
                except BaseException as error:
                    self._mark_unknown()
                    if not isinstance(error, Exception):
                        raise
                    raise MediaSpoolError() from None
            self._closed = True
            self._retire_own()
            return self._delete_finished()


class SpoolReader:
    """A bounded original reader; no path, fileno, dup, or transfer API."""

    def __init__(self, upload):
        self._upload = upload
        self._fd = None
        self._uncertain = self._closed = False

    @_public
    def read(self, size: int) -> bytes:
        size = _integer(size, positive=True)
        if size > _READ:
            _error("bound")
        upload = self._upload
        with upload._spool._mutex:
            upload._owned()
            if self not in upload._readers:
                _error("expired")
            if self._uncertain or upload._uncertain:
                _error()
            if self._closed or upload._closed or upload._delete_pending or upload._spool._closing:
                _error("expired")
            try:
                upload._spool._storage()
                if _stamp(os.fstat(self._fd)) != upload._seal_stamp:
                    _error()
                body = os.read(self._fd, size)
                if _stamp(os.fstat(self._fd)) != upload._seal_stamp:
                    _error()
                return body
            except (OSError, MediaSpoolError):
                upload._mark_unknown()
                raise MediaSpoolError() from None

    @_public
    def close(self) -> None:
        upload = self._upload
        with upload._spool._mutex:
            if self._closed:
                if self._uncertain:
                    _error()
                return
            self._closed = True
            if not upload._spool._retire(self, "_fd"):
                upload._mark_unknown()
                _error()
            upload._readers.discard(self)
            if upload._closed and not upload._uncertain:
                upload._close_locked()
            if upload._spool._closing:
                upload._spool._finish_close()
