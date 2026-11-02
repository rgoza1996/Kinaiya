"""Codex-authored Session Resources reservations; never cleanup or release."""

import time
from contextlib import contextmanager

from packages.contracts.agent_control import MAX_AGENT_CONTROL_INTEGER, AgentTaskIdentity
from packages.contracts.standing_execution import StandingExecutionIntent, claim_id, intent_digest
from packages.contracts.standing_resources import (
    MAX_STANDING_RESOURCE_RESERVATION_BYTES,
    StandingResourceObservation,
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    TaskResourceContext,
    TaskResourceQuery,
    checked_resource,
    standing_resource_digest,
    standing_resource_observation_digest,
    task_resource_context_digest,
)
from packages.contracts.standing_work import (
    StandingConflict,
    StandingUnavailable,
    canonical_bytes,
)
from services.session.standing_closure import _sql_identity
from services.session.standing_execution import _native

_META = "standing_resource_meta"
_ROWS = "standing_resource_reservations"
_TABLES = {
    _META: (
        f"CREATE TABLE {_META}(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,"
        "backend_id TEXT NOT NULL,coverage_sequence INTEGER NOT NULL)",
        (
            ("id", "INTEGER", 0, None, 1, 0),
            ("version", "INTEGER", 1, None, 0, 0),
            ("backend_id", "TEXT", 1, None, 0, 0),
            ("coverage_sequence", "INTEGER", 1, None, 0, 0),
        ),
    ),
    _ROWS: (
        f"CREATE TABLE {_ROWS}(claim_id TEXT PRIMARY KEY,request TEXT NOT NULL)",
        (("claim_id", "TEXT", 0, None, 1, 0), ("request", "TEXT", 1, None, 0, 0)),
    ),
}


class StandingResourceOwner:
    """Explicit same-Store registry, independent of TaskOwner registration.

    Historical observations never become a handoff permit. A missing resource
    reservation, even with post-bootstrap coverage, is not a no-lease witness.
    """

    def __init__(self, store, control, tasks):
        self.store, self.control, self.tasks = store, control, tasks
        self._registered = self._closed = self._failed = False
        self._backend = self._instance = None
        self._finalization_owner = None

    def register_finalization_owner(self, owner):
        from services.session.standing_resource_finalization import (
            StandingResourceFinalizationOwner,
        )

        if (
            self._finalization_owner is not None
            and self._finalization_owner is not owner
            or type(owner) is not StandingResourceFinalizationOwner
            or owner.resources is not self
            or owner.store is not self.store
            or owner.control is not self.control
            or owner.tasks is not self.tasks
        ):
            raise StandingUnavailable
        self._finalization_owner = owner

    def close(self):
        self._closed = True

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

    def _collaborators(self):
        if (
            self.control.store is not self.store
            or self.tasks.store is not self.store
            or self.tasks.control is not self.control
            or self.store._agent_submit_unavailable
        ):
            raise StandingUnavailable
        self.tasks._available()
        self.tasks._collaborators()

    def _available(self):
        if not self._registered or self._closed or self._failed:
            raise StandingUnavailable
        self._collaborators()

    def _writer(self, meta, *, registering=False):
        self.tasks._writer(meta)
        if not registering and self._instance != self.control.instance:
            raise StandingConflict

    def _schema(self, meta, claim, initial):
        for name, (sql, expected) in _TABLES.items():
            row = self._one("SELECT type,sql FROM sqlite_schema WHERE name=?", (name,))
            if (
                row is None
                or row["type"] != "table"
                or type(row["sql"]) is not str
                or _sql_identity(row["sql"]) != _sql_identity(sql)
                or self._all(f"PRAGMA foreign_key_list({name})")
                or self._one(
                    "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (name,)
                )
            ):
                raise StandingUnavailable
            columns = tuple(
                (r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"], r["hidden"])
                for r in self._all(f"PRAGMA table_xinfo({name})")
            )
            if columns != expected:
                raise StandingUnavailable
            indexes = self._all(f"PRAGMA index_list({name})")
            if name == _META:
                if indexes:
                    raise StandingUnavailable
            else:
                if len(indexes) != 1:
                    raise StandingUnavailable
                index = indexes[0]
                if index["unique"] != 1 or index["origin"] != "pk" or index["partial"] != 0:
                    raise StandingUnavailable
                escaped = index["name"].replace('"', '""')
                keys = tuple(
                    (r["name"], r["desc"], r["coll"])
                    for r in self._all(f'PRAGMA index_xinfo("{escaped}")')
                    if r["key"] == 1
                )
                if keys != (("claim_id", 0, "BINARY"),):
                    raise StandingUnavailable
        if self._one(f"SELECT count(*) FROM {_META}")[0] != 1 or self._one(
            f"SELECT 1 FROM {_META} WHERE typeof(id)!='integer' OR id!=1 "
            "OR typeof(version)!='integer' OR version!=1 OR typeof(backend_id)!='text' "
            "OR length(CAST(backend_id AS BLOB))!=32 OR typeof(coverage_sequence)!='integer' "
            "OR coverage_sequence<0 OR coverage_sequence>? LIMIT 1",
            (MAX_AGENT_CONTROL_INTEGER,),
        ):
            raise StandingUnavailable
        row = self._one(f"SELECT backend_id,coverage_sequence FROM {_META}")
        if row["backend_id"] != meta["backend_id"]:
            raise StandingUnavailable
        coverage = row["coverage_sequence"]
        count = self._one(f"SELECT count(*) FROM {_ROWS}")[0]
        if type(count) is not int or not 0 <= count <= 1:
            raise StandingUnavailable
        if self._one(
            f"SELECT 1 FROM {_ROWS} WHERE typeof(claim_id)!='text' "
            "OR length(CAST(claim_id AS BLOB))!=64 OR typeof(request)!='text' "
            "OR length(CAST(request AS BLOB)) NOT BETWEEN 1 AND ? LIMIT 1",
            (MAX_STANDING_RESOURCE_RESERVATION_BYTES,),
        ):
            raise StandingUnavailable
        if not count:
            return coverage, None
        if claim is None or initial is None:
            raise StandingUnavailable
        row = self._one(f"SELECT claim_id,request FROM {_ROWS}")
        reservation = StandingResourceReservation.from_json(row["request"].encode("utf-8"))
        if (
            row["claim_id"] != claim.claim_id
            or canonical_bytes(reservation).decode("utf-8") != row["request"]
            or reservation.context.backend_id != meta["backend_id"]
            or canonical_bytes(reservation.context.standing) != canonical_bytes(initial)
        ):
            raise StandingUnavailable
        return coverage, reservation

    def _observation(self):
        self._available()
        meta, generations, _, _, _, claim, initial = self.tasks._observation()
        if meta["backend_id"] != self._backend:
            raise StandingUnavailable
        coverage, reservation = self._schema(meta, claim, initial)
        return meta, generations, claim, initial, coverage, reservation

    @_native
    def register(self):
        if self._closed or self._failed:
            raise StandingUnavailable
        if self._registered:
            raise StandingConflict
        created = False

        def final():
            self._collaborators()
            current, _, _, _, _, claim, initial = self.tasks._observation()
            self._schema(current, claim, initial)
            if created:
                self._writer(current, registering=True)
            if self._closed or self._failed:
                raise StandingUnavailable

        try:
            with self._transaction(final):
                self._collaborators()
                meta, _, _, _, _, claim, initial = self.tasks._observation()
                present = self._all(
                    "SELECT name,type FROM sqlite_schema WHERE name IN (?,?)", (_META, _ROWS)
                )
                if not present:
                    try:
                        self._writer(meta, registering=True)
                    except StandingConflict:
                        raise StandingUnavailable from None
                    maximum = self._one("SELECT coalesce(max(sequence),0) FROM entries")[0]
                    if type(maximum) is not int or not 0 <= maximum <= MAX_AGENT_CONTROL_INTEGER:
                        raise StandingUnavailable
                    for sql, _ in _TABLES.values():
                        self._sql(sql)
                    self._sql(f"INSERT INTO {_META} VALUES(1,1,?,?)", (meta["backend_id"], maximum))
                    created = True
                elif len(present) != 2 or any(row["type"] != "table" for row in present):
                    raise StandingUnavailable
                self._schema(meta, claim, initial)
                instance = self.control.instance
            self._backend, self._instance, self._registered = meta["backend_id"], instance, True
        except BaseException:
            self._failed = True
            raise

    def _ordinary(self, task_id):
        # Inspect both bounded projections, and never treat an invalid possible
        # initial as an absent Task. Fetch at most two complete candidates.
        candidates = self._all(
            "WITH native AS (SELECT sequence,event_id,kind,"
            "EXISTS(SELECT 1 FROM agent_submit_slots WHERE agent_submit_slots.event_id=entries.event_id) AS slotted,"
            "CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB)) BETWEEN 1 AND 65536 "
            "THEN CASE WHEN json_valid(request) THEN request END END AS req,"
            "CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB)) BETWEEN 1 AND 69632 "
            "THEN CASE WHEN json_valid(value) THEN value END END AS val FROM entries) "
            "SELECT event_id FROM native WHERE (slotted OR ((kind='agent.command' "
            "OR json_extract(req,'$.kind')='agent.command' OR json_extract(val,'$.kind')='agent.command') "
            "AND (req IS NULL OR val IS NULL OR json_type(req,'$.payload.op') IS NULL "
            "OR json_type(val,'$.payload.op') IS NULL OR json_extract(req,'$.payload.op')='submit' "
            "OR json_extract(val,'$.payload.op')='submit'))) AND (req IS NULL OR val IS NULL "
            "OR coalesce(json_type(req,'$.payload.task_id'),'')!='text' "
            "OR coalesce(json_type(val,'$.payload.task_id'),'')!='text' "
            "OR json_extract(req,'$.payload.task_id')=? OR json_extract(val,'$.payload.task_id')=? "
            "OR EXISTS(SELECT 1 FROM json_each(req,'$.task_refs') WHERE type='text' AND value=?) "
            "OR EXISTS(SELECT 1 FROM json_each(val,'$.task_refs') WHERE type='text' AND value=?)) "
            "ORDER BY sequence LIMIT 2",
            (task_id,) * 4,
        )
        if len(candidates) > 1:
            raise StandingUnavailable
        if not candidates:
            return None
        row = self.tasks._entry(candidates[0]["event_id"])
        entry = self.store._agent_submit_check_row(row)
        entry = self.store._agent_submit_winner(
            entry.event_id, entry.session_id, entry.payload["idempotency_key"]
        )
        if entry.payload["task_id"] != task_id:
            raise StandingUnavailable
        return AgentTaskIdentity(
            schema_version="1.0",
            session_id=entry.session_id,
            thread_id=entry.thread_id,
            epoch=entry.epoch,
            task_id=entry.payload["task_id"],
            submit_event_id=entry.event_id,
            submit_sequence=entry.sequence,
            submit_idempotency_key=entry.payload["idempotency_key"],
            submit_payload_digest=entry.payload["payload_digest"],
        )

    def _context(self, query, meta, claim, initial):
        ordinary = self._ordinary(query.task_id)
        standing = None
        if claim is not None and claim.request.task.task_id == query.task_id:
            if initial is None or ordinary is not None:
                raise StandingUnavailable
            standing = initial
        if standing is None and ordinary is None:
            raise StandingUnavailable
        family = "standing" if standing is not None else "ordinary"
        return TaskResourceContext(
            schema_version="1.0",
            query=query,
            backend_id=meta["backend_id"],
            family=family,
            standing=standing,
            ordinary=ordinary,
            context_digest=task_resource_context_digest(
                query, meta["backend_id"], family, standing, ordinary
            ),
        ).validate_request(query)

    @_native
    def context(self, query: TaskResourceQuery) -> TaskResourceContext:
        query = checked_resource(query, TaskResourceQuery)
        with self._transaction():
            meta, _, claim, initial, _, _ = self._observation()
            return self._context(query, meta, claim, initial)

    def _guard(self, deadline, cancelled):
        if cancelled is not None:
            status = cancelled()
            if type(status) is not bool or status:
                raise StandingUnavailable
        self._available()
        if time.monotonic() >= deadline:
            raise StandingUnavailable

    def _new_reservation(self, original, meta, claim, initial):
        if claim is None or initial is None:
            raise StandingConflict
        expected = self._context(original.context.query, meta, claim, initial)
        if canonical_bytes(expected) != canonical_bytes(original.context):
            raise StandingConflict
        task = claim.request.task.task_session
        session = self.store.session(task.session_id)
        if session.channel != "agent" or (session.thread_id, session.epoch) != (
            task.thread_id,
            task.epoch,
        ):
            raise StandingConflict
        if self.tasks._closures.observe(meta, claim, initial) is not None:
            raise StandingConflict
        if self._finalization_owner is not None:
            # This runs in the existing reservation transaction both before
            # INSERT and immediately before COMMIT. Never nest a public lookup.
            self._finalization_owner.check_reservation(original)
        elif self._one(
            "SELECT 1 FROM sqlite_schema WHERE lower(name) IN (?,?,?) LIMIT 1",
            (
                "standing_resource_finalization_meta",
                "standing_resource_bindings",
                "standing_resource_finalizations",
            ),
        ):
            # A missing RAM collaborator cannot establish that this native
            # store has no binding. Keep genuine legacy stores independent.
            raise StandingUnavailable

    @_native
    def reserve(
        self, original: StandingResourceReservation, *, deadline=None, cancelled=None
    ) -> StandingResourceReservationReceipt:
        deadline = self.tasks.claims._options(deadline, cancelled)
        original = checked_resource(original, StandingResourceReservation)
        self._guard(deadline, cancelled)
        created = False

        def final():
            meta, _, claim, initial, _, retained = self._observation()
            if retained is None or canonical_bytes(retained) != canonical_bytes(original):
                raise StandingUnavailable
            if created:
                try:
                    self._new_reservation(original, meta, claim, initial)
                    self._writer(meta)
                except StandingConflict:
                    raise StandingUnavailable from None
            self._guard(deadline, cancelled)

        with self._transaction(final):
            meta, _, claim, initial, _, retained = self._observation()
            if retained is not None:
                if canonical_bytes(retained) != canonical_bytes(original):
                    raise StandingConflict
            else:
                self._new_reservation(original, meta, claim, initial)
                self._writer(meta)
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_ROWS} VALUES(?,?)",
                    (claim.claim_id, canonical_bytes(original).decode("utf-8")),
                )
                created = True
            result = StandingResourceReservationReceipt(
                schema_version="1.0",
                disposition="created" if created else "retained",
                request=original,
                request_digest=standing_resource_digest(original),
            ).validate_request(original)
        self._guard(deadline, cancelled)
        return result

    @_native
    def observe(self, original: StandingExecutionIntent) -> StandingResourceObservation:
        original = checked_resource(original, StandingExecutionIntent)
        with self._transaction():
            meta, generations, claim, initial, coverage, reservation = self._observation()
            if original.expected_backend != meta["backend_id"]:
                raise StandingConflict
            self.tasks.dispatch._known(original.expected_instance, generations)
            if claim is None or canonical_bytes(claim.request) != canonical_bytes(original):
                raise StandingConflict
            closure = self.tasks._closures.observe(meta, claim, initial)
            if closure is None:
                raise StandingConflict
            sequence = None if initial is None else initial.sequence
            body = {
                "schema_version": "1.0",
                "intent_digest": intent_digest(original),
                "claim_id": claim_id(original),
                "closure_digest": closure.closure_digest,
                "coverage_sequence": coverage,
                "accepted_sequence": sequence,
                "coverage": "accepted-after-bootstrap"
                if sequence is not None and sequence > coverage
                else "legacy-or-unadmitted",
                "reservation": reservation,
            }
            return StandingResourceObservation(
                **body, observation_digest=standing_resource_observation_digest(body)
            ).validate_request(original)
