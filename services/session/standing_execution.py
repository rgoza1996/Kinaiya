"""Codex-authored native standing claim reservations; no admission or release."""

import math
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from packages.contracts.agent_control import MAX_AGENT_CONTROL_INTEGER
from packages.contracts.standing_execution import (
    MAX_STANDING_EXECUTION_CLAIM_BYTES,
    StandingExecutionClaim,
    StandingExecutionClaimAdmission,
    StandingExecutionIntent,
    claim_digest,
    claim_id,
    intent_digest,
    occurrence_id,
    standing_initial_request,
)
from packages.contracts.standing_work import (
    StandingConflict,
    StandingUnavailable,
    _checked,
    _utc,
    canonical_bytes,
    latest_relevant,
)

_MAX_RECORDS = 10000
_TABLES = {
    "standing_execution_meta": (
        ("id", "INTEGER", 0, 1),
        ("version", "INTEGER", 1, 0),
        ("backend_id", "TEXT", 1, 0),
    ),
    "standing_execution_claims": (
        ("key", "TEXT", 0, 1),
        ("claim_id", "TEXT", 1, 0),
        ("occurrence_id", "TEXT", 1, 0),
        ("responsibility_id", "TEXT", 1, 0),
        ("authorization_revision", "INTEGER", 1, 0),
        ("claim", "TEXT", 1, 0),
    ),
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


class StandingExecutionOwner:
    """Explicit same-Store capability for one durable unresolved reservation.

    Local close and every other lifecycle transition preserve the reservation.
    A claim is preparation evidence only; no method admits or launches a Task.
    """

    def __init__(self, store, control, owner, dispatch, *, clock=None):
        self.store, self.control, self.owner, self.dispatch = store, control, owner, dispatch
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._registered = False
        self._closed = False
        self._failed = False
        self._backend = None
        self._instance = None

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

    def _collaborators(self):
        if (
            self.control.store is not self.store
            or self.owner.store is not self.store
            or self.dispatch.store is not self.store
            or self.owner.control is not self.control
            or self.dispatch.control is not self.control
            or self.owner._closed
            or self.owner._failed
        ):
            raise StandingUnavailable
        self.dispatch._available()

    def _joins(self):
        self._collaborators()
        meta, generations, gate, activation, producer = self.dispatch._observation()
        # At NULL-controller capacity only, an explicitly supplied healthy
        # passive owner may validate existing history without acquiring a writer.
        if self.owner._registered:
            if self.owner._backend != meta["backend_id"]:
                raise StandingUnavailable
        elif meta["instance_id"] is not None:
            raise StandingUnavailable
        owner_meta = self.owner._metadata()
        if dict(owner_meta) != dict(meta):
            raise StandingUnavailable
        self.owner._schema(meta)
        return meta, generations, gate, activation, producer

    def _writer(self, meta, *, registering=False):
        if (
            not self.owner._writable(meta)
            or not self.dispatch._writable(meta)
            or self.control.instance is None
            or self.control.instance != meta["instance_id"]
            or (not registering and self._instance != self.control.instance)
        ):
            raise StandingConflict

    def _historical(self, claim, meta, generations):
        request = claim.request
        self.dispatch._known(request.expected_instance, generations, stored=True)
        record = self.owner._record(request.responsibility_id, request.authorization_revision, meta)
        self._grant(request, record, claim.prepared_utc, stored=True)
        row = self._one(
            "SELECT * FROM standing_dispatch_activations WHERE epoch=?", (request.dispatch_epoch,)
        )
        activation = self.dispatch._activation_row(row, meta, generations)
        if activation is None or (
            activation[1].activation_digest != request.activation_digest
            or activation[1].mode_receipt.revision != request.control_revision
        ):
            raise StandingUnavailable
        row = self._one(
            "SELECT * FROM standing_dispatch_producers WHERE producer_id=?",
            (request.producer.request.producer_id,),
        )
        producer = self.dispatch._producer_row(row, meta, generations)
        if producer is None or (
            producer.request.expected_instance != request.expected_instance
            or producer.request.sequence < request.producer.request.sequence
            or (
                producer.request.sequence == request.producer.request.sequence
                and canonical_bytes(producer) != canonical_bytes(request.producer)
            )
        ):
            raise StandingUnavailable

    @staticmethod
    def _grant(request, record, now, *, stored=False):
        failure = StandingUnavailable if stored else StandingConflict
        authorization = record.receipt.request.authorization
        task = request.task
        if (
            record.receipt.request.responsibility_id != request.responsibility_id
            or record.receipt.revision != request.authorization_revision
            or record.receipt.authorization_digest != request.authorization_digest
            or record.record_digest != request.record_digest
            or record.activation != "armed"
            or record.authorized_from_utc is None
            or not authorization.enabled
            or task.objective != authorization.objective
            or task.content_scope != authorization.content_scope
            or canonical_bytes(task.task_session) != canonical_bytes(authorization.task_session)
            or canonical_bytes(task.workspace_pin) != canonical_bytes(authorization.workspace_pin)
        ):
            raise failure
        occurrence = latest_relevant(authorization.schedule, record.authorized_from_utc, now)
        if occurrence is None or (
            occurrence.local_date != request.occurrence.local_date
            or occurrence.due_utc != request.occurrence.due_utc
        ):
            raise failure
        offset = (
            _utc(occurrence.due_utc)
            .astimezone(ZoneInfo(authorization.schedule.timezone))
            .utcoffset()
        )
        if offset is None or offset.total_seconds() != request.occurrence.utc_offset_seconds:
            raise failure

    def _schema(self, meta, generations):
        for name, columns in _TABLES.items():
            actual = tuple(
                (r["name"], r["type"], r["notnull"], r["pk"])
                for r in self._all(f"PRAGMA table_info({name})")
            )
            if actual != columns:
                raise StandingUnavailable
        unique = set()
        for row in self._all("PRAGMA index_list(standing_execution_claims)"):
            if row["unique"] == 1 and row["partial"] == 0:
                name = row["name"].replace('"', '""')
                unique.add(tuple(r["name"] for r in self._all(f'PRAGMA index_info("{name}")')))
        if not {("key",), ("claim_id",), ("occurrence_id",)} <= unique:
            raise StandingUnavailable
        if self._one("SELECT count(*) FROM standing_execution_meta")[0] != 1:
            raise StandingUnavailable
        if self._one(
            "SELECT 1 FROM standing_execution_meta WHERE typeof(id)!='integer' OR id!=1 "
            "OR typeof(version)!='integer' OR version!=1 OR typeof(backend_id)!='text' "
            "OR length(CAST(backend_id AS BLOB))!=32 LIMIT 1"
        ):
            raise StandingUnavailable
        if self._one("SELECT backend_id FROM standing_execution_meta")[0] != meta["backend_id"]:
            raise StandingUnavailable
        count = self._one("SELECT count(*) FROM standing_execution_claims")[0]
        if type(count) is not int or not 0 <= count <= _MAX_RECORDS or count > 1:
            raise StandingUnavailable
        # Refuse malformed or oversized storage before fetching a full string.
        if self._one(
            "SELECT 1 FROM standing_execution_claims WHERE typeof(key)!='text' "
            "OR length(CAST(key AS BLOB)) NOT BETWEEN 1 AND 128 "
            "OR typeof(claim_id)!='text' OR length(CAST(claim_id AS BLOB))!=64 "
            "OR typeof(occurrence_id)!='text' OR length(CAST(occurrence_id AS BLOB))!=64 "
            "OR typeof(responsibility_id)!='text' OR length(CAST(responsibility_id AS BLOB))!=36 "
            "OR typeof(authorization_revision)!='integer' OR authorization_revision<1 "
            "OR authorization_revision>? OR typeof(claim)!='text' "
            "OR length(CAST(claim AS BLOB)) NOT BETWEEN 1 AND ? LIMIT 1",
            (MAX_AGENT_CONTROL_INTEGER, MAX_STANDING_EXECUTION_CLAIM_BYTES),
        ):
            raise StandingUnavailable
        if not count:
            return None
        row = self._one("SELECT * FROM standing_execution_claims")
        claim = StandingExecutionClaim.from_json(row["claim"].encode("utf-8"))
        request = claim.request
        if (
            canonical_bytes(claim).decode("utf-8") != row["claim"]
            or row["key"] != request.idempotency_key
            or row["claim_id"] != claim.claim_id
            or row["occurrence_id"] != claim.occurrence_id
            or row["responsibility_id"] != request.responsibility_id
            or row["authorization_revision"] != request.authorization_revision
            or request.expected_backend != meta["backend_id"]
        ):
            raise StandingUnavailable
        self._historical(claim, meta, generations)
        return claim

    def _observation(self):
        self._available()
        meta, generations, gate, activation, producer = self._joins()
        if meta["backend_id"] != self._backend:
            raise StandingUnavailable
        claim = self._schema(meta, generations)
        return meta, generations, gate, activation, producer, claim

    @_native
    def register(self) -> None:
        if self._closed or self._failed:
            raise StandingUnavailable
        if self._registered:
            raise StandingConflict
        created = False

        def commit_guard():
            if self._closed or self._failed:
                raise StandingUnavailable
            current = self._joins()[0]
            if created:
                self._writer(current, registering=True)
            if self._closed or self._failed:
                raise StandingUnavailable

        try:
            with self._transaction(commit_guard):
                meta, generations, _, _, _ = self._joins()
                tables = {
                    row["name"]
                    for row in self._all("SELECT name FROM sqlite_schema WHERE type='table'")
                    if row["name"] in _TABLES
                }
                if not tables:
                    try:
                        self._writer(meta, registering=True)
                    except StandingConflict:
                        raise StandingUnavailable from None
                    self._sql(
                        "CREATE TABLE standing_execution_meta(id INTEGER PRIMARY KEY CHECK(id=1),"
                        "version INTEGER NOT NULL,backend_id TEXT NOT NULL)"
                    )
                    self._sql(
                        "CREATE TABLE standing_execution_claims(key TEXT PRIMARY KEY,"
                        "claim_id TEXT NOT NULL UNIQUE,occurrence_id TEXT NOT NULL UNIQUE,"
                        "responsibility_id TEXT NOT NULL,authorization_revision INTEGER NOT NULL,"
                        "claim TEXT NOT NULL)"
                    )
                    self._sql(
                        "INSERT INTO standing_execution_meta VALUES(1,1,?)", (meta["backend_id"],)
                    )
                    created = True
                elif tables != set(_TABLES):
                    raise StandingUnavailable
                self._schema(meta, generations)
                instance = self.control.instance
            self._backend, self._instance, self._registered = meta["backend_id"], instance, True
        except BaseException:
            self._failed = True
            raise

    def close(self) -> None:
        self._closed = True

    @staticmethod
    def _options(deadline, cancelled):
        if deadline is None:
            deadline = time.monotonic() + 25
        if (
            type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or (cancelled is not None and not callable(cancelled))
        ):
            raise StandingUnavailable
        return deadline

    def _guard(self, deadline, cancelled):
        if cancelled is not None:
            value = cancelled()
            if type(value) is not bool or value:
                raise StandingUnavailable
        self._available()
        self._collaborators()
        if time.monotonic() >= deadline:
            raise StandingUnavailable

    def _clock(self):
        instant = self.clock()
        if (
            not isinstance(instant, datetime)
            or instant.tzinfo is None
            or instant.utcoffset() != timedelta(0)
        ):
            raise StandingUnavailable
        return instant.replace(microsecond=0).astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _new_authority(self, request, deadline, cancelled):
        self._guard(deadline, cancelled)
        meta, generations, gate, activation, producer = self._joins()
        self._writer(meta)
        if meta["backend_id"] != request.expected_backend:
            raise StandingConflict
        self.dispatch._known(request.expected_instance, generations)
        if request.expected_instance != meta["instance_id"]:
            raise StandingConflict
        record = self.owner._current_record(request.responsibility_id, meta)
        if record is None or record.receipt.revision != request.authorization_revision:
            raise StandingConflict
        if (
            meta["mode"] != "autonomous"
            or gate["epoch"] != request.dispatch_epoch
            or gate["epoch"] <= gate["stopped_through_epoch"]
            or activation is None
            or activation.activation_digest != request.activation_digest
            or activation.mode_receipt.revision != request.control_revision
            or meta["revision"] != request.control_revision
            or producer is None
            or producer["producer_id"] != request.producer.request.producer_id
            or canonical_bytes(self.dispatch._producer_row(producer, meta, generations))
            != canonical_bytes(request.producer)
        ):
            raise StandingConflict
        self.owner._agent_session(record.receipt.request)
        now = self._clock()
        self._grant(request, record, now)
        if not self.dispatch._live(meta, producer, self.dispatch._sample()):
            raise StandingConflict
        self._writer(meta)
        return now

    @_native
    def prepare(
        self, request: StandingExecutionIntent, *, deadline=None, cancelled=None
    ) -> StandingExecutionClaimAdmission:
        deadline = self._options(deadline, cancelled)
        request = _checked(request, StandingExecutionIntent)
        created = False
        prepared = None

        def commit_guard():
            if not created:
                return
            try:
                current = self._new_authority(request, deadline, cancelled)
                if _utc(current) < _utc(prepared):
                    raise StandingUnavailable
            except StandingConflict:
                raise StandingUnavailable from None
            # All blocking SQL and time validation counts before this last guard.
            self._guard(deadline, cancelled)

        with self._transaction(commit_guard):
            meta, generations, _, _, _, previous = self._observation()
            if request.expected_backend != meta["backend_id"]:
                raise StandingConflict
            if previous is not None:
                if canonical_bytes(previous.request) != canonical_bytes(request):
                    raise StandingConflict
                return StandingExecutionClaimAdmission(
                    schema_version="1.0", disposition="retained", claim=previous
                ).validate_request(request)
            self.dispatch._known(request.expected_instance, generations)
            prepared = self._new_authority(request, deadline, cancelled)
            claim = StandingExecutionClaim(
                schema_version="1.0",
                request=request,
                claim_id=claim_id(request),
                occurrence_id=occurrence_id(request),
                intent_digest=intent_digest(request),
                prepared_utc=prepared,
                claim_digest=claim_digest(request, prepared),
            )
            standing_initial_request(claim)
            self._guard(deadline, cancelled)
            self._sql(
                "INSERT INTO standing_execution_claims VALUES(?,?,?,?,?,?)",
                (
                    request.idempotency_key,
                    claim.claim_id,
                    claim.occurrence_id,
                    request.responsibility_id,
                    request.authorization_revision,
                    canonical_bytes(claim).decode("utf-8"),
                ),
            )
            created = True
        # Only this original, positively acknowledged COMMIT reports created.
        self._guard(deadline, cancelled)
        return StandingExecutionClaimAdmission(
            schema_version="1.0", disposition="created", claim=claim
        ).validate_request(request)

    @_native
    def lookup(self, request: StandingExecutionIntent) -> StandingExecutionClaim | None:
        request = _checked(request, StandingExecutionIntent)
        with self._transaction():
            meta, generations, _, _, _, claim = self._observation()
            if request.expected_backend != meta["backend_id"]:
                raise StandingConflict
            self.dispatch._known(request.expected_instance, generations)
            if claim is None or claim.request.idempotency_key != request.idempotency_key:
                return None
            if canonical_bytes(claim.request) != canonical_bytes(request):
                raise StandingConflict
            return claim.validate_request(request)

    @_native
    def unresolved(self) -> StandingExecutionClaim | None:
        with self._transaction():
            return self._observation()[-1]
