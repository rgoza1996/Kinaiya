"""Protected Converse capture preference, separate from physical capture ownership."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from pathlib import Path

from packages.contracts.capture_modes import (
    CAPTURE_MODES,
    MAX_CAPTURE_MODE_CHANGES,
    MAX_CAPTURE_MODE_INTEGER,
    CaptureModeChange,
    CaptureModeLookup,
    CaptureModeReceipt,
    CaptureModeSelection,
    CaptureModeSnapshot,
    capture_mode_json,
)
from services.settings.provider_catalog import _storage

_TABLES = {
    "capture_mode_state": "CREATE TABLE capture_mode_state (id INTEGER PRIMARY KEY CHECK(id=1), backend_id TEXT NOT NULL, revision INTEGER NOT NULL, authority_generation INTEGER NOT NULL, authority_epoch TEXT NOT NULL, selection TEXT NOT NULL)",
    "capture_mode_operations": "CREATE TABLE capture_mode_operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, request TEXT NOT NULL, receipt TEXT NOT NULL)",
}
_MAX_FILE_BYTES = 64 * 1024 * 1024


def configured_capture_default(config, *, rolling, native):
    """Preserve implicit host behavior; explicit modes never select a fallback."""
    if type(rolling) is not bool or type(native) is not bool:
        raise ValueError("Invalid host capture capability")
    declared = config.get("converse_capture")
    if declared is None:
        return "legacy_silence" if native or not rolling else "rolling_windows"
    if (
        type(declared) is not dict
        or set(declared) != {"mode"}
        or type(declared["mode"]) is not str
        or declared["mode"] not in CAPTURE_MODES
    ):
        raise ValueError("Invalid host capture mode declaration")
    return declared["mode"]


class CaptureModeConflict(ValueError):
    def __init__(self):
        super().__init__(
            "Converse capture mode selection changed or the original request conflicts"
        )


class CaptureModeUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Converse capture mode selection authority is unavailable")


def _encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _decode(raw):
    if type(raw) is not str:
        raise CaptureModeUnavailable
    try:
        return capture_mode_json(raw.encode())
    except (ValueError, UnicodeError):
        raise CaptureModeUnavailable from None


def _identity(path, fd):
    original, actual = os.fstat(fd), path.stat(follow_symlinks=False)
    if (
        (original.st_dev, original.st_ino) != (actual.st_dev, actual.st_ino)
        or not stat.S_ISREG(actual.st_mode)
        or actual.st_uid != os.geteuid()
        or actual.st_nlink != 1
        or actual.st_mode & 0o077
        or actual.st_size > _MAX_FILE_BYTES
    ):
        raise CaptureModeUnavailable


def _schema(db):
    if db.execute("PRAGMA user_version").fetchone() != (1,):
        raise CaptureModeUnavailable
    rows = db.execute(
        "SELECT type,CASE WHEN length(CAST(name AS BLOB))<=128 THEN name END,"
        "CASE WHEN length(CAST(tbl_name AS BLOB))<=128 THEN tbl_name END,"
        "CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN sql END FROM sqlite_schema LIMIT 4"
    ).fetchall()
    expected = {("table", name) for name in _TABLES} | {
        ("index", "sqlite_autoindex_capture_mode_operations_1")
    }
    if len(rows) != 3 or {(r[0], r[1]) for r in rows} != expected:
        raise CaptureModeUnavailable
    for kind, name, table, sql in rows:
        if kind == "index":
            if table != "capture_mode_operations" or sql is not None:
                raise CaptureModeUnavailable
        elif (
            table != name
            or type(sql) is not str
            or (re.sub(r"\s+", "", sql).lower() != re.sub(r"\s+", "", _TABLES[name]).lower())
        ):
            raise CaptureModeUnavailable


def _state(db, backend_id=None):
    _schema(db)
    rows = db.execute(
        "SELECT id,CASE WHEN typeof(backend_id)='text' AND length(CAST(backend_id AS BLOB))=32 THEN backend_id END,"
        "CASE WHEN typeof(revision)='integer' THEN revision END,"
        "CASE WHEN typeof(authority_generation)='integer' THEN authority_generation END,"
        "CASE WHEN typeof(authority_epoch)='text' AND length(CAST(authority_epoch AS BLOB))=32 THEN authority_epoch END,"
        "CASE WHEN typeof(selection)='text' AND length(CAST(selection AS BLOB))<=256 THEN selection END "
        "FROM capture_mode_state LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise CaptureModeUnavailable
    identity, backend, revision, generation, epoch, raw = rows[0]
    if (
        identity != 1
        or type(backend) is not str
        or re.fullmatch(r"[a-f0-9]{32}", backend) is None
        or (backend_id is not None and backend != backend_id)
        or type(epoch) is not str
        or re.fullmatch(r"[a-f0-9]{32}", epoch) is None
        or type(revision) is not int
        or not 0 <= revision <= MAX_CAPTURE_MODE_CHANGES
        or type(generation) is not int
        or not 0 <= generation <= MAX_CAPTURE_MODE_INTEGER
    ):
        raise CaptureModeUnavailable
    selection = CaptureModeSelection.model_validate(_decode(raw))
    return backend, revision, generation, epoch, selection


def _history(db, state):
    rows = db.execute(
        "SELECT CASE WHEN typeof(key)='text' AND length(CAST(key AS BLOB))<=128 THEN key END,"
        "CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 THEN digest END,"
        "CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=4096 THEN request END,"
        "CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=4096 THEN receipt END "
        "FROM capture_mode_operations LIMIT ?",
        (MAX_CAPTURE_MODE_CHANGES + 1,),
    ).fetchall()
    if len(rows) != state[1]:
        raise CaptureModeUnavailable
    operations, revisions = {}, {}
    for key, digest, raw_request, raw_receipt in rows:
        request = CaptureModeChange.model_validate(_decode(raw_request))
        receipt = CaptureModeReceipt.model_validate(_decode(raw_receipt))
        if (
            key != request.idempotency_key
            or key != receipt.idempotency_key
            or digest != _digest(request.model_dump(mode="json"))
            or request.backend_id != state[0]
            or receipt.backend_id != state[0]
            or request.authority_generation > state[2]
            or (request.authority_generation == state[2] and request.authority_epoch != state[3])
            or receipt.authority_generation != request.authority_generation
            or receipt.authority_epoch != request.authority_epoch
            or receipt.revision != request.expected_revision + 1
            or receipt.selection != request.selection
            or receipt.default_mode_observed != request.expected_default_mode
            or receipt.revision in revisions
        ):
            raise CaptureModeUnavailable
        operations[key] = (digest, receipt.model_dump(mode="json"))
        revisions[receipt.revision] = receipt.selection
    if set(revisions) != set(range(1, state[1] + 1)):
        raise CaptureModeUnavailable
    if state[4] != (revisions[state[1]] if state[1] else CaptureModeSelection(source="host")):
        raise CaptureModeUnavailable
    return operations


def _request(value):
    if type(value) is not CaptureModeChange:
        raise CaptureModeConflict
    return CaptureModeChange.model_validate(value.model_dump(warnings=False))


def _token(state, default_mode):
    return _digest(
        {
            "contract": "converse_capture.v1",
            "backend": state[0],
            "revision": state[1],
            "generation": state[2],
            "epoch": state[3],
            "selection": state[4].model_dump(mode="json"),
            "default_mode": default_mode,
        }
    )


class CaptureModeSettings:
    """One native Converse preference owner; saves never acquire a microphone."""

    def __init__(self, default_mode, *, path, workspace):
        if not callable(default_mode):
            raise ValueError("Invalid Converse capture mode owner")
        self.default_mode = default_mode
        self.path = Path(os.path.abspath(path))
        self.backend_id = None
        self.closed = self.failed = False
        self._fd = None
        self.db = None
        try:
            self.db, created = _storage(self.path, workspace, report_created=True)
            self._fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            _identity(self.path, self._fd)
            self.db.execute("PRAGMA busy_timeout=500")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.execute("BEGIN IMMEDIATE")
            if created:
                if (
                    self.db.execute("PRAGMA user_version").fetchone() != (0,)
                    or self.db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is not None
                ):
                    raise CaptureModeUnavailable
                for sql in _TABLES.values():
                    self.db.execute(sql)
                self.db.execute(
                    "INSERT INTO capture_mode_state VALUES (1,?,0,0,?,?)",
                    (
                        secrets.token_hex(16),
                        secrets.token_hex(16),
                        _encode(CaptureModeSelection(source="host").model_dump(mode="json")),
                    ),
                )
                self.db.execute("PRAGMA user_version=1")
            state = _state(self.db)
            _history(self.db, state)
            self.backend_id = state[0]
            if state[2] == MAX_CAPTURE_MODE_INTEGER:
                raise CaptureModeUnavailable
            self.db.execute(
                "UPDATE capture_mode_state SET authority_generation=?,authority_epoch=? WHERE id=1",
                (state[2] + 1, secrets.token_hex(16)),
            )
            state = _state(self.db, self.backend_id)
            self.generation, self.epoch = state[2], state[3]
            self.db.commit()
        except BaseException:
            self.close()
            raise

    def _rollback(self):
        try:
            self.db.rollback()
        except BaseException:
            # An unconfirmed rollback cannot leave a reusable authority handle.
            self.failed = True
            raise CaptureModeUnavailable from None

    def _usable(self):
        if self.closed or self.failed or self._fd is None:
            raise CaptureModeUnavailable
        _identity(self.path, self._fd)

    def _load(self):
        self._usable()
        state = _state(self.db, self.backend_id)
        return state, _history(self.db, state)

    def _default(self):
        try:
            mode = self.default_mode()
            return mode if type(mode) is str and mode in CAPTURE_MODES else None
        except (ValueError, OSError, RuntimeError):
            return None

    def _active(self, state):
        return (self.generation, self.epoch) == (state[2], state[3])

    def snapshot(self):
        try:
            self._usable()
            self.db.execute("BEGIN")
            state, _ = self._load()
            if not self._active(state):
                raise CaptureModeUnavailable
            host = self._default()
            return CaptureModeSnapshot(
                backend_id=state[0],
                revision=state[1],
                authority_generation=state[2],
                authority_epoch=state[3],
                state_token=_token(state, host),
                selection=state[4],
                default_mode=host,
                selected_mode=host if state[4].source == "host" else state[4].mode,
                writable=state[1] < MAX_CAPTURE_MODE_CHANGES,
            ).model_dump(mode="json")
        except (sqlite3.Error, ValueError, OSError, TypeError):
            raise CaptureModeUnavailable from None
        finally:
            if not self.closed and self.db is not None:
                self._rollback()

    def change(self, original):
        original = _request(original)
        digest = _digest(original.model_dump(mode="json"))
        try:
            self._usable()
            self.db.execute("BEGIN IMMEDIATE")
            state, operations = self._load()
            previous = operations.get(original.idempotency_key)
            if previous is not None:
                if previous[0] != digest:
                    raise CaptureModeConflict
                self._rollback()
                return previous[1]
            host = self._default()
            if (
                not self._active(state)
                or original.backend_id != state[0]
                or original.authority_generation != state[2]
                or original.authority_epoch != state[3]
                or original.expected_revision != state[1]
                or original.state_token != _token(state, host)
                or original.expected_default_mode != host
                or state[1] == MAX_CAPTURE_MODE_CHANGES
                or (original.selection.source == "host" and host is None)
            ):
                raise CaptureModeConflict
            receipt = CaptureModeReceipt(
                backend_id=state[0],
                authority_generation=state[2],
                authority_epoch=state[3],
                revision=state[1] + 1,
                idempotency_key=original.idempotency_key,
                selection=original.selection,
                default_mode_observed=host,
                selected_mode_observed=host
                if original.selection.source == "host"
                else original.selection.mode,
            ).model_dump(mode="json")
            self.db.execute(
                "INSERT INTO capture_mode_operations VALUES (?,?,?,?)",
                (
                    original.idempotency_key,
                    digest,
                    _encode(original.model_dump(mode="json")),
                    _encode(receipt),
                ),
            )
            self.db.execute(
                "UPDATE capture_mode_state SET revision=?,selection=? WHERE id=1",
                (state[1] + 1, _encode(original.selection.model_dump(mode="json"))),
            )
            self.db.commit()
            return receipt
        except CaptureModeConflict:
            self._rollback()
            raise
        except (sqlite3.Error, ValueError, OSError, TypeError, CaptureModeUnavailable):
            self.failed = True
            if self.db is not None:
                self._rollback()
            raise CaptureModeUnavailable from None

    def lookup(self, original):
        original = _request(original)
        digest = _digest(original.model_dump(mode="json"))
        try:
            self._usable()
            self.db.execute("BEGIN")
            state, operations = self._load()
            if original.backend_id != state[0]:
                raise CaptureModeConflict
            previous = operations.get(original.idempotency_key)
            if previous is not None and previous[0] != digest:
                raise CaptureModeConflict
            # Host observations and their tokens can change back. Only native
            # monotonic revisions/generations establish this durable fence.
            fenced = (
                original.authority_generation < state[2]
                or (
                    original.authority_generation == state[2]
                    and original.authority_epoch != state[3]
                )
                or original.expected_revision < state[1]
            )
            return CaptureModeLookup(
                backend_id=state[0],
                authority_generation=state[2],
                authority_epoch=state[3],
                request_sha256=digest,
                outcome="saved" if previous else "unobserved",
                receipt=None if previous is None else previous[1],
                mutation_fenced=False if previous else fenced,
            ).model_dump(mode="json")
        except CaptureModeConflict:
            raise
        except (sqlite3.Error, ValueError, OSError, TypeError):
            raise CaptureModeUnavailable from None
        finally:
            if not self.closed and self.db is not None:
                self._rollback()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.db is not None:
                self.db.close()
        finally:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
