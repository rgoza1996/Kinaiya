"""Conservative character admission for the explicit OpenRouter speech profile.

Every admitted request stays charged at its UTF-8 byte upper bound, including
cancelled/uncertain calls. This is a local estimate, never provider billing.
"""

import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from packages.providers.speech import SpeechProviderError

MODEL = "hexgrad/kokoro-82m"
RATE = Decimal("0.62")
MAX_RECORDS = 10000
MAX_LEDGER = 2_000_000


def _private(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise ValueError("Unsafe speech budget file")
    return info


class CharacterBudget:
    def __init__(self, path, *, limit_usd=1.0, require_existing=False):
        try:
            self.path = Path(path)
            self.limit = Decimal(str(limit_usd))
            if (
                not self.path.is_absolute()
                or ".." in self.path.parts
                or not self.path.name
                or not self.limit.is_finite()
                or not Decimal("0") < self.limit <= Decimal("25")
                or isinstance(limit_usd, bool)
            ):
                raise ValueError("Invalid speech budget")
            self._directory_identity = None
            if type(require_existing) is not bool:
                raise ValueError("Invalid speech budget")
            self.require_existing = require_existing
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise SpeechProviderError(503) from exc

    @contextmanager
    def _locked(self):
        # Optional Linux host capability, imported only for the selected profile.
        import fcntl

        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            # Walk every component without following aliases. Bind all subsequent
            # operations to this directory, including atomic replacement.
            for part in self.path.parent.parts[1:]:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                os.close(directory)
                directory = child
            parent = os.fstat(directory)
            identity = (parent.st_dev, parent.st_ino)
            if (
                parent.st_uid != os.geteuid()
                or parent.st_mode & 0o077
                or self._directory_identity not in (None, identity)
            ):
                raise ValueError("Unsafe or replaced speech budget directory")
            self._directory_identity = identity
            fd = os.open(
                self.path.name + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                _private(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield directory
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def _read(self, directory):
        try:
            fd = os.open(
                self.path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        except FileNotFoundError:
            if self.require_existing:
                raise ValueError("Retained speech budget is unavailable") from None
            return {
                "version": 1,
                "model": MODEL,
                "rate_usd_per_million": str(RATE),
                "limit_usd": str(self.limit),
                "calls": [],
            }
        try:
            if _private(fd).st_size > MAX_LEDGER:
                raise ValueError("Oversized speech budget")
            raw = os.read(fd, MAX_LEDGER + 1)
            if len(raw) > MAX_LEDGER:
                raise ValueError("Oversized speech budget")
        finally:
            os.close(fd)

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Ambiguous speech budget")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=unique)
        if (
            type(value) is not dict
            or set(value) != {"version", "model", "rate_usd_per_million", "limit_usd", "calls"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["model"] != MODEL
            or value["rate_usd_per_million"] != str(RATE)
            or value["limit_usd"] != str(self.limit)
            or type(value["calls"]) is not list
            or len(value["calls"]) > MAX_RECORDS
        ):
            raise ValueError("Invalid speech budget")
        seen = set()
        for call in value["calls"]:
            if (
                type(call) is not dict
                or set(call) != {"id", "utf8_bytes", "state", "generation_id"}
                or type(call["id"]) is not str
                or not re.fullmatch(r"[0-9a-f]{32}", call["id"])
                or call["id"] in seen
                or type(call["utf8_bytes"]) is not int
                or not 1 <= call["utf8_bytes"] <= 4096
                or call["state"] not in {"reserved", "completed"}
                or not self._generation(call["generation_id"])
            ):
                raise ValueError("Invalid speech budget record")
            seen.add(call["id"])
        return value

    @staticmethod
    def _generation(value):
        return value is None or (
            type(value) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) is not None
        )

    def _save(self, value, directory):
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_LEDGER:
            raise ValueError("Speech budget capacity exhausted")
        name = ".speech-budget-" + uuid.uuid4().hex
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def admit(self, text):
        try:
            if type(text) is not str:
                raise ValueError("Invalid speech text")
            count = len(text.encode("utf-8"))
            if not 1 <= count <= 4096:
                raise ValueError("Speech text exceeds its bound")
            with self._locked() as directory:
                value = self._read(directory)
                total = sum(call["utf8_bytes"] for call in value["calls"]) + count
                if (
                    len(value["calls"]) >= MAX_RECORDS
                    or Decimal(total) * RATE / 1_000_000 > self.limit
                ):
                    raise ValueError("Speech budget exhausted")
                identity = uuid.uuid4().hex
                value["calls"].append(
                    {
                        "id": identity,
                        "utf8_bytes": count,
                        "state": "reserved",
                        "generation_id": None,
                    }
                )
                self._save(value, directory)
                return identity
        except Exception as exc:
            raise SpeechProviderError(503) from exc

    def finish(self, identity, generation_id=None):
        try:
            if not self._generation(generation_id):
                raise ValueError("Invalid speech generation receipt")
            with self._locked() as directory:
                value = self._read(directory)
                call = next(call for call in value["calls"] if call["id"] == identity)
                if call["state"] == "completed" and call["generation_id"] != generation_id:
                    raise ValueError("Conflicting speech generation receipt")
                call.update(state="completed", generation_id=generation_id)
                self._save(value, directory)
        except Exception as exc:
            raise SpeechProviderError(503) from exc
