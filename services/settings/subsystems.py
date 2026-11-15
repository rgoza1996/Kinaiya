"""Protected Core next-launch selection, separate from live role availability."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

from packages.contracts.core_subsystems import (
    MAX_CORE_SUBSYSTEM_CHANGES,
    MAX_CORE_SUBSYSTEM_INTEGER,
    CoreSubsystemChange,
    CoreSubsystemLookup,
    CoreSubsystemReceipt,
    CoreSubsystemSelection,
    CoreSubsystemSnapshot,
    core_subsystem_json,
)
from services.common.topology import SUBSYSTEM_MODES, SubsystemTopology
from services.settings.provider_catalog import _storage

_TABLES = {
    "core_subsystem_state": "CREATE TABLE core_subsystem_state (id INTEGER PRIMARY KEY CHECK(id=1), backend_id TEXT NOT NULL, revision INTEGER NOT NULL, authority_generation INTEGER NOT NULL, authority_epoch TEXT NOT NULL, selection TEXT NOT NULL)",
    "core_subsystem_operations": "CREATE TABLE core_subsystem_operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, request TEXT NOT NULL, receipt TEXT NOT NULL)",
}
_MAX_FILE_BYTES = 64 * 1024 * 1024
_NAME = "core-subsystems.sqlite"


class CoreSubsystemConflict(ValueError):
    def __init__(self):
        super().__init__("Core subsystem selection changed or the original request conflicts")


class CoreSubsystemUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Core subsystem selection authority is unavailable")


def _encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _decode(raw):
    if type(raw) is not str:
        raise CoreSubsystemUnavailable
    try:
        return core_subsystem_json(raw.encode())
    except (ValueError, UnicodeError):
        raise CoreSubsystemUnavailable from None


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
        raise CoreSubsystemUnavailable


def _schema(db):
    if db.execute("PRAGMA user_version").fetchone() != (1,):
        raise CoreSubsystemUnavailable
    rows = db.execute(
        "SELECT type,CASE WHEN length(CAST(name AS BLOB))<=128 THEN name END,"
        "CASE WHEN length(CAST(tbl_name AS BLOB))<=128 THEN tbl_name END,"
        "CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN sql END FROM sqlite_schema LIMIT 4"
    ).fetchall()
    expected = {("table", name) for name in _TABLES} | {
        ("index", "sqlite_autoindex_core_subsystem_operations_1")
    }
    if len(rows) != 3 or {(r[0], r[1]) for r in rows} != expected:
        raise CoreSubsystemUnavailable
    for kind, name, table, sql in rows:
        if kind == "index":
            if table != "core_subsystem_operations" or sql is not None:
                raise CoreSubsystemUnavailable
        elif (
            table != name
            or type(sql) is not str
            or (re.sub(r"\s+", "", sql).lower() != re.sub(r"\s+", "", _TABLES[name]).lower())
        ):
            raise CoreSubsystemUnavailable


def _state(db, backend_id=None):
    _schema(db)
    rows = db.execute(
        "SELECT id,CASE WHEN typeof(backend_id)='text' AND length(CAST(backend_id AS BLOB))=32 THEN backend_id END,"
        "CASE WHEN typeof(revision)='integer' THEN revision END,"
        "CASE WHEN typeof(authority_generation)='integer' THEN authority_generation END,"
        "CASE WHEN typeof(authority_epoch)='text' AND length(CAST(authority_epoch AS BLOB))=32 THEN authority_epoch END,"
        "CASE WHEN typeof(selection)='text' AND length(CAST(selection AS BLOB))<=256 THEN selection END "
        "FROM core_subsystem_state LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise CoreSubsystemUnavailable
    identity, backend, revision, generation, epoch, raw = rows[0]
    if (
        identity != 1
        or type(backend) is not str
        or re.fullmatch(r"[a-f0-9]{32}", backend) is None
        or (backend_id is not None and backend != backend_id)
        or type(epoch) is not str
        or re.fullmatch(r"[a-f0-9]{32}", epoch) is None
        or type(revision) is not int
        or not 0 <= revision <= MAX_CORE_SUBSYSTEM_CHANGES
        or type(generation) is not int
        or not 0 <= generation <= MAX_CORE_SUBSYSTEM_INTEGER
    ):
        raise CoreSubsystemUnavailable
    selection = CoreSubsystemSelection.model_validate(_decode(raw))
    return backend, revision, generation, epoch, selection


def _history(db, state):
    rows = db.execute(
        "SELECT CASE WHEN typeof(key)='text' AND length(CAST(key AS BLOB))<=128 THEN key END,"
        "CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 THEN digest END,"
        "CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=4096 THEN request END,"
        "CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=4096 THEN receipt END "
        "FROM core_subsystem_operations LIMIT ?",
        (MAX_CORE_SUBSYSTEM_CHANGES + 1,),
    ).fetchall()
    if len(rows) != state[1]:
        raise CoreSubsystemUnavailable
    operations, revisions = {}, {}
    for key, digest, raw_request, raw_receipt in rows:
        request = CoreSubsystemChange.model_validate(_decode(raw_request))
        receipt = CoreSubsystemReceipt.model_validate(_decode(raw_receipt))
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
            or receipt.host_mode_observed != request.expected_host_mode
            or receipt.revision in revisions
        ):
            raise CoreSubsystemUnavailable
        operations[key] = (digest, receipt.model_dump(mode="json"))
        revisions[receipt.revision] = receipt.selection
    if set(revisions) != set(range(1, state[1] + 1)):
        raise CoreSubsystemUnavailable
    if state[4] != (revisions[state[1]] if state[1] else CoreSubsystemSelection(source="host")):
        raise CoreSubsystemUnavailable
    return operations


def _request(value):
    if type(value) is not CoreSubsystemChange:
        raise CoreSubsystemConflict
    return CoreSubsystemChange.model_validate(value.model_dump(warnings=False))


def _token(state, host_mode):
    return _digest(
        {
            "contract": "core_subsystems.v1",
            "backend": state[0],
            "revision": state[1],
            "generation": state[2],
            "epoch": state[3],
            "selection": state[4].model_dump(mode="json"),
            "host_mode": host_mode,
        }
    )


class CoreSubsystemSettings:
    """One shared native epoch; CLI peers never retire the Session controller."""

    def __init__(self, host_mode, *, path, workspace, startup_mode=None, register=True):
        if startup_mode is not None and startup_mode not in SUBSYSTEM_MODES:
            raise ValueError("Invalid startup subsystem selection")
        if type(register) is not bool or not callable(host_mode):
            raise ValueError("Invalid Core subsystem owner")
        self.host_mode, self.startup_mode = host_mode, startup_mode
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
                    raise CoreSubsystemUnavailable
                for sql in _TABLES.values():
                    self.db.execute(sql)
                self.db.execute(
                    "INSERT INTO core_subsystem_state VALUES (1,?,0,0,?,?)",
                    (
                        secrets.token_hex(16),
                        secrets.token_hex(16),
                        _encode(CoreSubsystemSelection(source="host").model_dump(mode="json")),
                    ),
                )
                self.db.execute("PRAGMA user_version=1")
            state = _state(self.db)
            _history(self.db, state)
            self.backend_id = state[0]
            if register:
                if state[2] == MAX_CORE_SUBSYSTEM_INTEGER:
                    raise CoreSubsystemUnavailable
                self.db.execute(
                    "UPDATE core_subsystem_state SET authority_generation=?,authority_epoch=? WHERE id=1",
                    (state[2] + 1, secrets.token_hex(16)),
                )
            state = _state(self.db, self.backend_id)
            self.generation, self.epoch = state[2], state[3]
            self.db.commit()
        except BaseException:
            self.close()
            raise

    def _usable(self):
        if self.closed or self.failed or self._fd is None:
            raise CoreSubsystemUnavailable
        _identity(self.path, self._fd)

    def _load(self):
        self._usable()
        state = _state(self.db, self.backend_id)
        return state, _history(self.db, state)

    def _host(self):
        try:
            mode = self.host_mode()
            return mode if type(mode) is str and mode in SUBSYSTEM_MODES else None
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
                raise CoreSubsystemUnavailable
            host = self._host()
            return CoreSubsystemSnapshot(
                backend_id=state[0],
                revision=state[1],
                authority_generation=state[2],
                authority_epoch=state[3],
                state_token=_token(state, host),
                selection=state[4],
                host_mode=host,
                selected_mode=host if state[4].source == "host" else state[4].mode,
                startup_mode=self.startup_mode,
                writable=state[1] < MAX_CORE_SUBSYSTEM_CHANGES,
            ).model_dump(mode="json")
        except (sqlite3.Error, ValueError, OSError, TypeError):
            raise CoreSubsystemUnavailable from None
        finally:
            if not self.closed and self.db is not None:
                self.db.rollback()

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
                    raise CoreSubsystemConflict
                self.db.rollback()
                return previous[1]
            host = self._host()
            if (
                not self._active(state)
                or original.backend_id != state[0]
                or original.authority_generation != state[2]
                or original.authority_epoch != state[3]
                or original.expected_revision != state[1]
                or original.state_token != _token(state, host)
                or original.expected_host_mode != host
                or state[1] == MAX_CORE_SUBSYSTEM_CHANGES
                or (original.selection.source == "host" and host is None)
            ):
                raise CoreSubsystemConflict
            receipt = CoreSubsystemReceipt(
                backend_id=state[0],
                authority_generation=state[2],
                authority_epoch=state[3],
                revision=state[1] + 1,
                idempotency_key=original.idempotency_key,
                selection=original.selection,
                host_mode_observed=host,
                selected_mode_observed=host
                if original.selection.source == "host"
                else original.selection.mode,
            ).model_dump(mode="json")
            self.db.execute(
                "INSERT INTO core_subsystem_operations VALUES (?,?,?,?)",
                (
                    original.idempotency_key,
                    digest,
                    _encode(original.model_dump(mode="json")),
                    _encode(receipt),
                ),
            )
            self.db.execute(
                "UPDATE core_subsystem_state SET revision=?,selection=? WHERE id=1",
                (state[1] + 1, _encode(original.selection.model_dump(mode="json"))),
            )
            self.db.commit()
            return receipt
        except CoreSubsystemConflict:
            self.db.rollback()
            raise
        except (sqlite3.Error, ValueError, OSError, TypeError, CoreSubsystemUnavailable):
            self.failed = True
            if self.db is not None:
                self.db.rollback()
            raise CoreSubsystemUnavailable from None

    def lookup(self, original):
        original = _request(original)
        digest = _digest(original.model_dump(mode="json"))
        try:
            self._usable()
            self.db.execute("BEGIN")
            state, operations = self._load()
            if original.backend_id != state[0]:
                raise CoreSubsystemConflict
            previous = operations.get(original.idempotency_key)
            if previous is not None and previous[0] != digest:
                raise CoreSubsystemConflict
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
            return CoreSubsystemLookup(
                backend_id=state[0],
                authority_generation=state[2],
                authority_epoch=state[3],
                request_sha256=digest,
                outcome="saved" if previous else "unobserved",
                receipt=None if previous is None else previous[1],
                mutation_fenced=False if previous else fenced,
            ).model_dump(mode="json")
        except CoreSubsystemConflict:
            raise
        except (sqlite3.Error, ValueError, OSError, TypeError):
            raise CoreSubsystemUnavailable from None
        finally:
            if not self.closed and self.db is not None:
                self.db.rollback()

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


@contextmanager
def _read_existing(data_dir, workspace):
    """Read the exact retained owner without registering, creating or repairing."""
    path = Path(os.path.abspath(Path(data_dir) / _NAME))
    if not os.path.lexists(path):
        yield None
        return
    fd, db = None, None
    try:
        if path != path.resolve() or path.is_relative_to(Path(workspace).resolve()):
            raise CoreSubsystemUnavailable
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise CoreSubsystemUnavailable
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        _identity(path, fd)
        # Bind SQLite's read to the original file capability, not another path
        # lookup. This is a Linux host service, not a cross-platform file API.
        db = sqlite3.connect(f"file:/proc/self/fd/{fd}?mode=ro", uri=True, timeout=0.5)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        state = _state(db)
        operations = _history(db, state)
        _identity(path, fd)
        yield state, operations
    except CoreSubsystemConflict:
        raise
    except (sqlite3.Error, ValueError, OSError, TypeError):
        raise CoreSubsystemUnavailable from None
    finally:
        try:
            if db is not None:
                db.close()
        finally:
            if fd is not None:
                os.close(fd)


def read_selection(data_dir, workspace):
    with _read_existing(data_dir, workspace) as observed:
        return None if observed is None else observed[0][4]


def lookup_stored_change(data_dir, workspace, original):
    original = _request(original)
    with _read_existing(data_dir, workspace) as observed:
        if observed is None:
            raise CoreSubsystemUnavailable
        state, operations = observed
        if original.backend_id != state[0]:
            raise CoreSubsystemConflict
        digest = _digest(original.model_dump(mode="json"))
        previous = operations.get(original.idempotency_key)
        if previous is not None and previous[0] != digest:
            raise CoreSubsystemConflict
        fenced = (
            original.authority_generation < state[2]
            or (original.authority_generation == state[2] and original.authority_epoch != state[3])
            or original.expected_revision < state[1]
        )
        return CoreSubsystemLookup(
            backend_id=state[0],
            authority_generation=state[2],
            authority_epoch=state[3],
            request_sha256=digest,
            outcome="saved" if previous else "unobserved",
            receipt=None if previous is None else previous[1],
            mutation_fenced=False if previous else fenced,
        ).model_dump(mode="json")


def resolve_topology(config, data_dir, workspace, *, developer_all=False):
    selection = read_selection(data_dir, workspace)
    if selection is not None and selection.source == "saved":
        return SubsystemTopology.from_mode(selection.mode)
    return SubsystemTopology.from_config(config, developer_all=developer_all)


def effective_config(config, data_dir, workspace, *, developer_all=False, startup_mode=None):
    """Materialize the selected graph in memory; never rewrite host defaults."""
    topology = (
        resolve_topology(config, data_dir, workspace, developer_all=developer_all)
        if startup_mode is None
        else SubsystemTopology.from_mode(startup_mode)
    )
    distribution = config.get("distribution")
    return {
        **config,
        "distribution": {
            **(distribution if isinstance(distribution, dict) else {}),
            "agent": topology.agent,
            "converse": topology.converse,
            "decisions": topology.decisions,
        },
    }


def host_mode_reader(config_path, workspace):
    path = Path(os.path.abspath(config_path))
    workspace = Path(workspace).resolve()

    def read():
        if path != path.resolve() or path.is_relative_to(workspace):
            raise CoreSubsystemUnavailable
        with path.open("rb") as stream:
            original = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(original.st_mode)
                or original.st_uid != os.geteuid()
                or original.st_mode & 0o022
                or original.st_size > 4 * 1024 * 1024
            ):
                raise CoreSubsystemUnavailable
            raw = stream.read(4 * 1024 * 1024 + 1)
            actual = path.stat(follow_symlinks=False)
            if len(raw) > 4 * 1024 * 1024 or (actual.st_dev, actual.st_ino) != (
                original.st_dev,
                original.st_ino,
            ):
                raise CoreSubsystemUnavailable
        config = json.loads(raw)
        if type(config) is not dict:
            raise CoreSubsystemUnavailable
        return SubsystemTopology.from_config(config).mode

    return read


def _request_directory(data_dir, workspace, *, create):
    path = Path(os.path.abspath(Path(data_dir) / "core-subsystem-requests"))
    if path != path.resolve() or path.is_relative_to(Path(workspace).resolve()):
        raise CoreSubsystemUnavailable
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise CoreSubsystemUnavailable
    return path


def save_local_mode(config_path, data_dir, workspace, mode):
    """Retain the exact CLI request before committing the shared Core selector."""
    if mode not in SUBSYSTEM_MODES:
        raise ValueError("Unsupported subsystem mode")
    owner = CoreSubsystemSettings(
        host_mode_reader(config_path, workspace),
        path=Path(data_dir) / _NAME,
        workspace=workspace,
        register=False,
    )
    key = secrets.token_hex(16)
    try:
        snapshot = owner.snapshot()
        original = CoreSubsystemChange(
            backend_id=snapshot["backend_id"],
            authority_epoch=snapshot["authority_epoch"],
            authority_generation=snapshot["authority_generation"],
            expected_revision=snapshot["revision"],
            state_token=snapshot["state_token"],
            expected_host_mode=snapshot["host_mode"],
            idempotency_key=key,
            selection=CoreSubsystemSelection(source="saved", mode=mode),
        )
        directory = _request_directory(data_dir, workspace, create=True)
        if (
            sum(1 for _ in islice(directory.iterdir(), MAX_CORE_SUBSYSTEM_CHANGES + 1))
            >= MAX_CORE_SUBSYSTEM_CHANGES
        ):
            raise CoreSubsystemUnavailable
        fd = os.open(
            directory / (key + ".json"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write((_encode(original.model_dump(mode="json")) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        try:
            receipt = owner.change(original)
        except CoreSubsystemUnavailable:
            raise CoreSubsystemUnavailableWithRequest(key) from None
        return {
            "ready": False,
            "provider_connection": "not_checked",
            "section": "services",
            "next": "kinaiya serve host",
            "subsystems": {
                "agent": SubsystemTopology.from_mode(mode).agent,
                "converse": SubsystemTopology.from_mode(mode).converse,
                "decisions": SubsystemTopology.from_mode(mode).decisions,
            },
            "selection_source": "core_saved",
            "activation": "restart_required",
            "core_receipt": receipt,
            "original_request_key": key,
        }
    finally:
        owner.close()


class CoreSubsystemUnavailableWithRequest(CoreSubsystemUnavailable):
    def __init__(self, key):
        self.key = key
        super().__init__()


def lookup_local_mode(data_dir, workspace, key):
    if type(key) is not str or re.fullmatch(r"[a-f0-9]{32}", key) is None:
        raise ValueError("Invalid Core subsystem request key")
    directory = _request_directory(data_dir, workspace, create=False)
    path = directory / (key + ".json")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _identity(path, fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(4097)
        original = CoreSubsystemChange.model_validate(core_subsystem_json(raw))
        if original.idempotency_key != key:
            raise CoreSubsystemConflict
        _identity(path, fd)
    finally:
        os.close(fd)
    return lookup_stored_change(data_dir, workspace, original)
