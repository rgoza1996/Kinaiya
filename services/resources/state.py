"""Small owner-owned durable policy record; never stores credentials or argv."""

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROFILES = ("conversation_priority", "local_work_priority", "local_only", "configured_hybrid")


class ResourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(default=0, ge=0)
    profile: Literal[
        "conversation_priority", "local_work_priority", "local_only", "configured_hybrid"
    ] = "conversation_priority"
    phase: Literal[
        "initializing",
        "ready",
        "quiescing",
        "unloading",
        "local_work",
        "restoring",
        "off",
        "failed",
    ] = "initializing"
    desired_converse: bool = True
    lease_id: str | None = None
    lease_task_id: str | None = Field(default=None, max_length=128)
    required_bytes: int = Field(default=0, ge=0, le=1 << 46)
    restore_bytes: int = Field(default=0, ge=0, le=1 << 46)
    reason: str = Field(default="", max_length=160)


def read_record(path: Path, default: ResourceRecord | None = None) -> ResourceRecord:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if default is None:
            raise
        return default.model_copy(deep=True)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o077:
            raise ValueError("Resource policy must be an owner-only regular file")
        raw = os.read(fd, 16385)
        if len(raw) > 16384:
            raise ValueError("Resource policy exceeds its bound")
        return ResourceRecord.model_validate(json.loads(raw))
    finally:
        os.close(fd)


class ResourceState:
    def __init__(self, path: Path, *, profile="conversation_priority"):
        import fcntl

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._dir = os.open(
            self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            self._lock = os.open(
                "resources.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=self._dir,
            )
            st = os.fstat(self._lock)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ValueError("Resource controller lock is unsafe")
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.record = read_record(self.path, ResourceRecord(profile=profile))
        except BaseException:
            self.close()
            raise

    def save(self, **changes):
        candidate = ResourceRecord.model_validate(
            {**self.record.model_dump(), **changes, "revision": self.record.revision + 1}
        )
        payload = candidate.model_dump_json().encode()
        name = ".resources-" + secrets.token_hex(16)
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._dir,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path.name, src_dir_fd=self._dir, dst_dir_fd=self._dir)
            os.fsync(self._dir)
            self.record = candidate
        finally:
            try:
                os.unlink(name, dir_fd=self._dir)
            except FileNotFoundError:
                pass
        return candidate

    def close(self):
        for name in ("_lock", "_dir"):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
