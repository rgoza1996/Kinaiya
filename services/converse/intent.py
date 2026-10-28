"""One durable owner choice, independent of temporary worker availability.

Both native owner-control paths use this small record. Writes hold a nonblocking
file lock only for the atomic update, never for model loading or network I/O.
"""

import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_MAX_BYTES = 4096
_MAX_REVISION = (1 << 63) - 1


@dataclass(frozen=True)
class IntentRecord:
    enabled: bool
    revision: int

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.revision) is not int:
            raise TypeError("Owner intent requires a boolean and an integer revision")
        if not 0 <= self.revision <= _MAX_REVISION:
            raise OverflowError("Owner intent revision is outside its bound")


def _private_file(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or info.st_uid != os.geteuid()
    ):
        raise ValueError("Owner intent requires owner-only regular files")
    return info


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate owner intent field")
        result[key] = value
    return result


class ConverseIntent:
    def __init__(self, path: Path | None = None, *, initial_enabled=True):
        if type(initial_enabled) is not bool:
            raise TypeError("Initial owner intent must be a boolean")
        self.path = Path(path) if path is not None else None
        self._record = IntentRecord(initial_enabled, 0)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._locked() as directory:
                try:
                    self._read(directory)
                except FileNotFoundError:
                    self._write(directory, self._record)

    @contextmanager
    def _directory(self):
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError("Owner intent directory must be owned and not writable by others")
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self):
        with self._directory() as directory:
            fd = os.open(
                self.path.name + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                _private_file(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Closing the descriptor releases the per-write lock on every path.
                yield directory
            finally:
                os.close(fd)

    def _read(self, directory):
        fd = os.open(
            self.path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        try:
            info = _private_file(fd)
            if info.st_size > _MAX_BYTES:
                raise ValueError("Owner intent exceeds its size bound")
            raw = bytearray()
            while len(raw) <= _MAX_BYTES:
                chunk = os.read(fd, _MAX_BYTES + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _MAX_BYTES:
                raise ValueError("Owner intent exceeds its size bound")
            try:
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
                if not isinstance(value, dict) or set(value) != {"enabled", "revision"}:
                    raise ValueError("Unexpected owner intent fields")
                return IntentRecord(**value)
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                raise ValueError("Invalid owner intent record") from exc
        finally:
            os.close(fd)

    def _write(self, directory, record):
        name = ".converse-intent-" + secrets.token_hex(16)
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(record.__dict__, separators=(",", ":")).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def read(self):
        if self.path is None:
            return self._record
        with self._directory() as directory:
            return self._read(directory)

    def set(self, enabled):
        if type(enabled) is not bool:
            raise TypeError("Owner intent must be a boolean")
        if self.path is None:
            self._record = IntentRecord(enabled, self._record.revision + 1)
            return self._record
        with self._locked() as directory:
            current = self._read(directory)
            candidate = IntentRecord(enabled, current.revision + 1)
            self._write(directory, candidate)
            return candidate
