"""Codex-authored native owner definitions; no timer or execution producer."""

import hashlib
import math
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps

from pydantic import TypeAdapter

from packages.contracts.agent_control import ControlInstance
from packages.contracts.models import Session
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeLookup,
    StandingChangeReceipt,
    StandingConflict,
    StandingCurrent,
    StandingRecord,
    StandingResponsibilityPage,
    StandingResponsibilitySnapshot,
    StandingUnavailable,
    authorization_digest,
    canonical_bytes,
    record_digest,
    standing_json,
    validate_authorization_at,
)

MAX_STANDING_CHANGES = 10000
MAX_ENABLED_RESPONSIBILITIES = 32
_CONTROL_TABLES = {
    "agent_control_state",
    "agent_control_generations",
    "agent_control_changes",
    "agent_steer_intents",
}
_TABLES = {
    "standing_owner_meta": (
        ("id", "INTEGER", 1),
        ("version", "INTEGER", 0),
        ("backend_id", "TEXT", 0),
    ),
    "standing_owner_changes": (
        ("key", "TEXT", 1),
        ("digest", "TEXT", 0),
        ("request", "TEXT", 0),
        ("receipt", "TEXT", 0),
        ("responsibility_id", "TEXT", 0),
        ("revision", "INTEGER", 0),
    ),
    "standing_authorizations": (
        ("responsibility_id", "TEXT", 1),
        ("revision", "INTEGER", 2),
        ("record", "TEXT", 0),
    ),
    "standing_owner_current": (("responsibility_id", "TEXT", 1), ("revision", "INTEGER", 0)),
}


def _native(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (StandingConflict, StandingUnavailable):
            raise
        except Exception:
            raise StandingUnavailable from None

    return call


class StandingOwner:
    """Passive until explicit registration by the actual Session API lifecycle.

    Opening this object never creates schema, chooses a clock or changes a
    Session control generation. Close retires only this local capability.
    """

    def __init__(self, store, control, *, clock=None):
        self.store = store
        self.control = control
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._registered = False
        self._closed = False
        self._failed = False
        self._instance = None
        self._backend = None

    def _sql(self, statement, args=()):
        return self.store.db.execute(statement, args)

    def _one(self, statement, args=()):
        return self._sql(statement, args).fetchone()

    def _all(self, statement, args=()):
        return self._sql(statement, args).fetchall()

    def _poison(self):
        self._failed = True
        self.store._agent_submit_unavailable = True
        try:
            self.store.db.close()
        except BaseException:
            pass

    @contextmanager
    def _transaction(self, before_commit=None):
        with self.store.lock:
            connection = self.store.db
            attempted = False
            try:
                if connection.in_transaction:
                    self._poison()
                    raise StandingUnavailable
                attempted = True
                self._sql("BEGIN IMMEDIATE")
                yield
                if before_commit is not None:
                    before_commit()
                connection.commit()
            except BaseException:
                if attempted:
                    try:
                        connection.rollback()
                    except BaseException:
                        self._poison()
                        raise StandingUnavailable from None
                raise

    def _available(self):
        if not self._registered or self._closed or self._failed:
            raise StandingUnavailable

    def _metadata(self):
        if self.control.store is not self.store:
            raise StandingUnavailable
        tables = {
            row["name"] for row in self._all("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        # AgentControl.metadata can migrate an old store. Never enter that path
        # through a standing read or through a failed Session startup.
        if not _CONTROL_TABLES <= tables:
            raise StandingUnavailable
        row = self.control.metadata()
        return row

    def _writable(self, meta):
        return (
            self._registered
            and not self._closed
            and not self._failed
            and self._instance == self.control.instance == meta["instance_id"]
        )

    def _known(self, instance):
        row = self._one(
            "SELECT instance_id,retired FROM agent_control_generations WHERE instance_id=?",
            (instance,),
        )
        if row is None:
            raise StandingConflict
        if (
            row["instance_id"] != instance
            or type(row["retired"]) is not int
            or row["retired"] not in (0, 1)
        ):
            raise StandingUnavailable
        return bool(row["retired"])

    def _counts(self):
        changes = self._one("SELECT count(*) FROM standing_owner_changes")[0]
        authorizations = self._one("SELECT count(*) FROM standing_authorizations")[0]
        if (
            type(changes) is not int
            or type(authorizations) is not int
            or changes != authorizations
            or not 0 <= changes <= MAX_STANDING_CHANGES
        ):
            raise StandingUnavailable
        if (
            self._one(
                "SELECT 1 FROM standing_authorizations a LEFT JOIN standing_owner_changes c "
                "ON c.responsibility_id=a.responsibility_id AND c.revision=a.revision "
                "WHERE c.key IS NULL LIMIT 1"
            )
            or self._one(
                "SELECT 1 FROM standing_owner_current c LEFT JOIN standing_authorizations a "
                "ON a.responsibility_id=c.responsibility_id AND a.revision=c.revision "
                "WHERE a.responsibility_id IS NULL LIMIT 1"
            )
            or self._one(
                "SELECT 1 FROM standing_authorizations a LEFT JOIN standing_owner_current c "
                "ON c.responsibility_id=a.responsibility_id "
                "WHERE c.responsibility_id IS NULL OR a.revision>c.revision LIMIT 1"
            )
        ):
            raise StandingUnavailable
        return changes

    def _schema(self, meta):
        for name, columns in _TABLES.items():
            actual = tuple(
                (row["name"], row["type"], row["pk"])
                for row in self._all(f"PRAGMA table_info({name})")
            )
            if actual != columns:
                raise StandingUnavailable
        rows = self._all("SELECT id,version,backend_id FROM standing_owner_meta")
        if (
            len(rows) != 1
            or type(rows[0]["id"]) is not int
            or rows[0]["id"] != 1
            or type(rows[0]["version"]) is not int
            or rows[0]["version"] != 1
            or type(rows[0]["backend_id"]) is not str
            or rows[0]["backend_id"] != meta["backend_id"]
        ):
            raise StandingUnavailable
        unique = set()
        for row in self._all("PRAGMA index_list(standing_owner_changes)"):
            if row["unique"] == 1 and row["partial"] == 0:
                # Native index names come from SQLite, not from a request.
                name = row["name"].replace('"', '""')
                unique.add(
                    tuple(item["name"] for item in self._all(f'PRAGMA index_info("{name}")'))
                )
        if not {("key",), ("responsibility_id", "revision")} <= unique:
            raise StandingUnavailable
        self._counts()

    @_native
    def register(self) -> None:
        if self._closed or self._failed:
            raise StandingUnavailable
        if self._registered:
            raise StandingConflict
        try:
            with self._transaction():
                meta = self._metadata()
                if self.control.instance is None or self.control.instance != meta["instance_id"]:
                    raise StandingConflict
                tables = {
                    row["name"]
                    for row in self._all("SELECT name FROM sqlite_schema WHERE type='table'")
                    if row["name"] in _TABLES
                }
                if not tables:
                    self._sql(
                        "CREATE TABLE standing_owner_meta(id INTEGER PRIMARY KEY CHECK(id=1),"
                        "version INTEGER NOT NULL,backend_id TEXT NOT NULL)"
                    )
                    self._sql(
                        "CREATE TABLE standing_owner_changes(key TEXT PRIMARY KEY,digest TEXT NOT NULL,"
                        "request TEXT NOT NULL,receipt TEXT NOT NULL,responsibility_id TEXT NOT NULL,"
                        "revision INTEGER NOT NULL,UNIQUE(responsibility_id,revision))"
                    )
                    self._sql(
                        "CREATE TABLE standing_authorizations(responsibility_id TEXT NOT NULL,"
                        "revision INTEGER NOT NULL,record TEXT NOT NULL,"
                        "PRIMARY KEY(responsibility_id,revision))"
                    )
                    self._sql(
                        "CREATE TABLE standing_owner_current(responsibility_id TEXT PRIMARY KEY,"
                        "revision INTEGER NOT NULL)"
                    )
                    self._sql(
                        "INSERT INTO standing_owner_meta VALUES(1,1,?)", (meta["backend_id"],)
                    )
                elif tables != set(_TABLES):
                    raise StandingUnavailable
                self._schema(meta)
                if self._closed or self.control.instance != meta["instance_id"]:
                    raise StandingUnavailable
            self._backend = meta["backend_id"]
            self._instance = meta["instance_id"]
            self._registered = True
        except BaseException:
            self._failed = True
            raise

    def close(self) -> None:
        self._closed = True

    def _observation(self):
        self._available()
        meta = self._metadata()
        if meta["instance_id"] is None:
            raise StandingUnavailable
        self._schema(meta)
        if meta["backend_id"] != self._backend:
            raise StandingUnavailable
        return meta

    @staticmethod
    def _decode(kind, text):
        if type(text) is not str:
            raise StandingUnavailable
        value = kind.from_json(text.encode("utf-8"))
        if canonical_bytes(value).decode("utf-8") != text:
            raise StandingUnavailable
        return value

    def _change_row(self, key, meta):
        row = self._one("SELECT * FROM standing_owner_changes WHERE key=?", (key,))
        if row is None:
            return None
        if any(
            type(row[name]) is not str
            for name in ("key", "digest", "request", "receipt", "responsibility_id")
        ):
            raise StandingUnavailable
        original = self._decode(StandingChange, row["request"])
        receipt = self._decode(StandingChangeReceipt, row["receipt"]).validate_request(original)
        if (
            row["key"] != original.idempotency_key
            or row["digest"] != hashlib.sha256(canonical_bytes(original)).hexdigest()
            or row["responsibility_id"] != original.responsibility_id
            or type(row["revision"]) is not int
            or row["revision"] != receipt.revision
            or receipt.backend_id != meta["backend_id"]
        ):
            raise StandingUnavailable
        try:
            self._known(original.expected_instance)
        except StandingConflict:
            raise StandingUnavailable from None
        return original, receipt

    def _record(self, responsibility_id, revision, meta):
        rows = self._all(
            "SELECT responsibility_id,revision,record FROM standing_authorizations "
            "WHERE responsibility_id=? AND revision=?",
            (responsibility_id, revision),
        )
        if len(rows) != 1:
            raise StandingUnavailable
        row = rows[0]
        record = self._decode(StandingRecord, row["record"])
        if (
            type(row["responsibility_id"]) is not str
            or row["responsibility_id"] != record.receipt.request.responsibility_id
            or type(row["revision"]) is not int
            or row["revision"] != record.receipt.revision
        ):
            raise StandingUnavailable
        proof = self._change_row(record.receipt.request.idempotency_key, meta)
        if proof is None or canonical_bytes(proof[1]) != canonical_bytes(record.receipt):
            raise StandingUnavailable
        matches = self._all(
            "SELECT key FROM standing_owner_changes WHERE responsibility_id=? AND revision=?",
            (responsibility_id, revision),
        )
        if len(matches) != 1 or matches[0]["key"] != record.receipt.request.idempotency_key:
            raise StandingUnavailable
        return record

    def _current_record(self, responsibility_id, meta):
        row = self._one(
            "SELECT responsibility_id,revision FROM standing_owner_current WHERE responsibility_id=?",
            (responsibility_id,),
        )
        history = self._one(
            "SELECT count(*),min(revision),max(revision) FROM standing_authorizations WHERE responsibility_id=?",
            (responsibility_id,),
        )
        if row is None:
            if history[0] != 0:
                raise StandingUnavailable
            return None
        if (
            type(row["responsibility_id"]) is not str
            or row["responsibility_id"] != responsibility_id
            or type(row["revision"]) is not int
            or row["revision"] < 1
            or history[1] != 1
            or history[0] != row["revision"]
            or history[2] != row["revision"]
        ):
            raise StandingUnavailable
        return self._record(responsibility_id, row["revision"], meta)

    def _retained(self, request, meta):
        previous = self._change_row(request.idempotency_key, meta)
        if previous is None:
            return None
        original, receipt = previous
        self._record(original.responsibility_id, receipt.revision, meta)
        current = self._current_record(original.responsibility_id, meta)
        if current is None or current.receipt.revision < receipt.revision:
            raise StandingUnavailable
        if canonical_bytes(original) != canonical_bytes(request):
            raise StandingConflict
        return receipt

    def _current(self, responsibility_id, meta, record):
        return StandingCurrent(
            schema_version="1.0",
            backend_id=meta["backend_id"],
            instance_id=meta["instance_id"],
            responsibility_id=responsibility_id,
            revision=record.receipt.revision if record is not None else 0,
            control_writable=self._writable(meta),
        )

    def _clock(self, *, ceil=False):
        instant = self.clock()
        if (
            not isinstance(instant, datetime)
            or instant.tzinfo is None
            or instant.utcoffset() != timedelta(0)
        ):
            raise StandingUnavailable
        if ceil and instant.microsecond:
            instant += timedelta(seconds=1)
        return instant.replace(microsecond=0).astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _continuing(self, deadline, cancelled):
        if self._closed or self._failed or time.monotonic() >= deadline:
            return False
        if cancelled is not None:
            value = cancelled()
            if type(value) is not bool:
                raise StandingUnavailable
            if value:
                return False
        return True

    def _require_new(self, meta, deadline, cancelled):
        if not self._writable(meta):
            raise StandingConflict
        if not self._continuing(deadline, cancelled):
            raise StandingUnavailable

    def _agent_session(self, request):
        desired = request.authorization.task_session
        row = self._one(
            "SELECT session_id,value FROM sessions WHERE session_id=?", (desired.session_id,)
        )
        if row is None:
            raise StandingConflict
        if type(row["session_id"]) is not str or type(row["value"]) is not str:
            raise StandingUnavailable
        raw = standing_json(row["value"].encode("utf-8"))
        if not {"schema_version", "session_id", "thread_id", "channel", "epoch"} <= raw.keys():
            raise StandingUnavailable
        if type(raw["epoch"]) is not int:
            raise StandingUnavailable
        session = Session.model_validate(raw, strict=True)
        if session.session_id != row["session_id"]:
            raise StandingUnavailable
        if (
            session.session_id != desired.session_id
            or session.thread_id != desired.thread_id
            or session.epoch != desired.epoch
            or session.channel != "agent"
        ):
            raise StandingConflict

    def _enabled(self, meta, deadline, cancelled):
        count = 0
        for row in self._all(
            "SELECT responsibility_id FROM standing_owner_current ORDER BY responsibility_id"
        ):
            self._require_new(meta, deadline, cancelled)
            responsibility_id = TypeAdapter(ControlInstance).validate_python(
                row["responsibility_id"], strict=True
            )
            record = self._current_record(responsibility_id, meta)
            if record is None:
                raise StandingUnavailable
            count += int(record.receipt.request.authorization.enabled)
        if count > MAX_ENABLED_RESPONSIBILITIES:
            raise StandingUnavailable
        return count

    @_native
    def change(
        self, request: StandingChange, *, deadline=None, cancelled=None
    ) -> StandingChangeReceipt:
        self._available()
        if deadline is None:
            deadline = time.monotonic() + 25
        request = StandingChange.from_json(canonical_bytes(StandingChange.model_validate(request)))
        if (
            type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or (cancelled is not None and not callable(cancelled))
        ):
            raise StandingUnavailable
        if not self._continuing(deadline, cancelled):
            raise StandingUnavailable
        created = False
        meta = None

        def commit_guard():
            if created:
                self._require_new(meta, deadline, cancelled)

        with self._transaction(commit_guard):
            meta = self._observation()
            if request.expected_backend != meta["backend_id"]:
                raise StandingConflict
            receipt = self._retained(request, meta)
            if receipt is not None:
                return receipt
            retired = self._known(request.expected_instance)
            self._require_new(meta, deadline, cancelled)
            if retired or request.expected_instance != meta["instance_id"]:
                raise StandingConflict
            current = self._current_record(request.responsibility_id, meta)
            if request.expected_revision != (current.receipt.revision if current else 0):
                raise StandingConflict
            self._agent_session(request)
            clock = self._clock()
            try:
                validate_authorization_at(request.authorization, clock)
            except ValueError:
                raise StandingConflict from None
            enabled = self._enabled(meta, deadline, cancelled)
            enabled += int(request.authorization.enabled) - int(
                current.receipt.request.authorization.enabled if current else False
            )
            changes = self._counts() + 1
            if enabled > MAX_ENABLED_RESPONSIBILITIES or MAX_STANDING_CHANGES - changes < enabled:
                raise StandingUnavailable
            receipt = StandingChangeReceipt(
                schema_version="1.0",
                status="responsibility_saved",
                backend_id=meta["backend_id"],
                service_instance=self._instance,
                revision=request.expected_revision + 1,
                request=request,
                authorization_digest=authorization_digest(request, request.expected_revision + 1),
            )
            pending = StandingRecord(
                schema_version="1.0",
                receipt=receipt,
                activation="pending",
                authorized_from_utc=None,
                record_digest=record_digest(receipt, "pending", None),
            )
            self._require_new(meta, deadline, cancelled)
            self._sql(
                "INSERT INTO standing_owner_changes VALUES(?,?,?,?,?,?)",
                (
                    request.idempotency_key,
                    hashlib.sha256(canonical_bytes(request)).hexdigest(),
                    canonical_bytes(request).decode(),
                    canonical_bytes(receipt).decode(),
                    request.responsibility_id,
                    receipt.revision,
                ),
            )
            self._sql(
                "INSERT INTO standing_authorizations VALUES(?,?,?)",
                (request.responsibility_id, receipt.revision, canonical_bytes(pending).decode()),
            )
            self._sql(
                "INSERT INTO standing_owner_current VALUES(?,?) ON CONFLICT(responsibility_id) DO UPDATE SET revision=excluded.revision",
                (request.responsibility_id, receipt.revision),
            )
            created = True
        self._arm(request, receipt, deadline, cancelled)
        return receipt

    def _arm(self, request, receipt, deadline, cancelled):
        if not self._continuing(deadline, cancelled):
            return
        armed = False
        meta = None

        def commit_guard():
            if armed and (not self._continuing(deadline, cancelled) or not self._writable(meta)):
                raise _ActivationRetired

        try:
            with self._transaction(commit_guard):
                if not self._continuing(deadline, cancelled):
                    return
                self._available()
                meta = self._metadata()
                self._schema(meta)
                if meta["backend_id"] != self._backend:
                    raise StandingUnavailable
                if not self._writable(meta) or request.expected_instance != meta["instance_id"]:
                    return
                current = self._current_record(request.responsibility_id, meta)
                if current is None or canonical_bytes(current.receipt) != canonical_bytes(receipt):
                    return
                if current.activation != "pending" or not self._continuing(deadline, cancelled):
                    return
                try:
                    clock = self._clock(ceil=True)
                    validate_authorization_at(request.authorization, clock)
                except (ValueError, StandingUnavailable):
                    return
                value = StandingRecord(
                    schema_version="1.0",
                    receipt=receipt,
                    activation="armed",
                    authorized_from_utc=clock,
                    record_digest=record_digest(receipt, "armed", clock),
                )
                if not self._continuing(deadline, cancelled):
                    return
                self._sql(
                    "UPDATE standing_authorizations SET record=? WHERE responsibility_id=? AND revision=?",
                    (canonical_bytes(value).decode(), request.responsibility_id, receipt.revision),
                )
                armed = True
        except _ActivationRetired:
            # The first save is already positively committed. Retire only the
            # attempted activation transaction and preserve its pending proof.
            return

    @_native
    def lookup(self, request: StandingChange) -> StandingChangeLookup:
        request = StandingChange.from_json(canonical_bytes(StandingChange.model_validate(request)))
        with self._transaction():
            meta = self._observation()
            if request.expected_backend != meta["backend_id"]:
                raise StandingConflict
            receipt = self._retained(request, meta)
            retired = self._known(request.expected_instance)
            record = self._current_record(request.responsibility_id, meta)
            current = self._current(request.responsibility_id, meta, record)
            return StandingChangeLookup(
                schema_version="1.0",
                status="recorded" if receipt is not None else "unobserved",
                request=request,
                current=current,
                request_instance="retired" if retired else "current",
                receipt=receipt,
                mutation_fenced=False
                if receipt is not None
                else retired or current.revision > request.expected_revision,
            ).validate_request(request)

    @_native
    def snapshot(self, responsibility_id: str) -> StandingResponsibilitySnapshot:
        responsibility_id = TypeAdapter(ControlInstance).validate_python(
            responsibility_id, strict=True
        )
        with self._transaction():
            meta = self._observation()
            record = self._current_record(responsibility_id, meta)
            current = self._current(responsibility_id, meta, record)
            return StandingResponsibilitySnapshot(
                **current.model_dump(mode="python"), record=record
            )

    @_native
    def page(self, *, after=None, limit=20) -> StandingResponsibilityPage:
        if after is not None:
            after = TypeAdapter(ControlInstance).validate_python(after, strict=True)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise StandingConflict
        with self._transaction():
            meta = self._observation()
            rows = self._all(
                "SELECT responsibility_id FROM standing_owner_current WHERE (? IS NULL OR responsibility_id>?) ORDER BY responsibility_id LIMIT ?",
                (after, after, limit + 1),
            )
            records = []
            page = StandingResponsibilityPage(
                schema_version="1.0",
                backend_id=meta["backend_id"],
                instance_id=meta["instance_id"],
                control_writable=self._writable(meta),
                items=(),
                next_cursor=None,
            )
            for index, row in enumerate(rows[:limit]):
                responsibility_id = TypeAdapter(ControlInstance).validate_python(
                    row["responsibility_id"], strict=True
                )
                record = self._current_record(responsibility_id, meta)
                if record is None:
                    raise StandingUnavailable
                more = index + 1 < len(rows)
                try:
                    candidate = StandingResponsibilityPage(
                        schema_version="1.0",
                        backend_id=meta["backend_id"],
                        instance_id=meta["instance_id"],
                        control_writable=self._writable(meta),
                        items=tuple([*records, record]),
                        next_cursor=responsibility_id if more else None,
                    )
                except ValueError:
                    if not records:
                        raise StandingUnavailable from None
                    break
                records.append(record)
                page = candidate
            return page


class _ActivationRetired(Exception):
    pass
