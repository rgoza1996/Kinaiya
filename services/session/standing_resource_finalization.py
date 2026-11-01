"""Native prework Resources bindings and immutable cleanup reports.

The Session owner joins protected history; it does not stop workers, restore a
lease, grant execution, or release standing capacity. Registration is explicit.
"""

import time
from contextlib import contextmanager

from packages.contracts.standing_execution import claim_id, intent_digest
from packages.contracts.standing_resource_finalization import (
    MAX_STANDING_RESOURCE_BINDING_BYTES,
    MAX_STANDING_RESOURCE_FINALIZATION_BYTES,
    StandingResourceBindingReceipt,
    StandingResourceBindingRequest,
    StandingResourceFinalization,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    checked_finalization,
    finalization_bytes,
    resource_binding_digest,
    resource_finalize_context_digest,
)
from packages.contracts.standing_resources import (
    StandingResourceObservation,
    standing_resource_observation_digest,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.session.standing_closure import _sql_identity
from services.session.standing_execution import _native

_META = "standing_resource_finalization_meta"
_BINDINGS = "standing_resource_bindings"
_FINALIZATIONS = "standing_resource_finalizations"
_TABLES = {
    _META: (
        f"CREATE TABLE {_META}(id INTEGER PRIMARY KEY CHECK(id=1),"
        "version INTEGER NOT NULL,backend_id TEXT NOT NULL)",
        (
            ("id", "INTEGER", 0, None, 1, 0),
            ("version", "INTEGER", 1, None, 0, 0),
            ("backend_id", "TEXT", 1, None, 0, 0),
        ),
    ),
    _BINDINGS: (
        f"CREATE TABLE {_BINDINGS}(claim_id TEXT PRIMARY KEY,request TEXT NOT NULL)",
        (("claim_id", "TEXT", 0, None, 1, 0), ("request", "TEXT", 1, None, 0, 0)),
    ),
    _FINALIZATIONS: (
        f"CREATE TABLE {_FINALIZATIONS}(claim_id TEXT PRIMARY KEY,report TEXT NOT NULL)",
        (("claim_id", "TEXT", 0, None, 1, 0), ("report", "TEXT", 1, None, 0, 0)),
    ),
}


class StandingResourceFinalizationOwner:
    """Explicit same-Store knowledge of native Resources binding and cleanup.

    Historical receipts remain readable after scheduler eligibility changes.
    Only the registered current Session writer may create either kind of row.
    """

    def __init__(self, store, control, tasks, resources):
        self.store, self.control, self.tasks, self.resources = store, control, tasks, resources
        self._owners = (store, control, tasks, resources)
        self._connection, self._lock = store.db, store.lock
        self._registered = self._closed = self._failed = False
        self._backend = self._instance = None

    def close(self) -> None:
        self._closed = True

    def _sql(self, statement, args=()):
        if self.store.db is not self._connection or self.store.lock is not self._lock:
            raise StandingUnavailable
        return self._connection.execute(statement, args)

    def _one(self, statement, args=()):
        return self._sql(statement, args).fetchone()

    def _all(self, statement, args=()):
        return self._sql(statement, args).fetchall()

    def _poison(self):
        self._failed = True
        self.store._agent_submit_unavailable = True
        try:
            self._connection.close()
        except BaseException:
            pass

    @contextmanager
    def _transaction(self, before_commit=None):
        self._collaborators()
        with self._lock:
            self._collaborators()
            connection = self._connection
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
            any(
                actual is not original
                for actual, original in zip(
                    (self.store, self.control, self.tasks, self.resources),
                    self._owners,
                    strict=True,
                )
            )
            or self.store.db is not self._connection
            or self.store.lock is not self._lock
            or self.control.store is not self.store
            or self.tasks.store is not self.store
            or self.tasks.control is not self.control
            or self.resources.store is not self.store
            or self.resources.control is not self.control
            or self.resources.tasks is not self.tasks
            or self.resources._finalization_owner is not self
            or self.store._agent_submit_unavailable
        ):
            raise StandingUnavailable
        self.resources._available()

    def _available(self):
        if not self._registered or self._closed or self._failed:
            raise StandingUnavailable
        self._collaborators()

    def _writer(self, meta, *, registering=False):
        self._collaborators()
        self.resources._writer(meta)
        if not registering and self._instance != self.control.instance:
            raise StandingConflict

    def _guard(self, deadline, cancelled):
        if cancelled is not None:
            status = cancelled()
            if type(status) is not bool or status:
                raise StandingUnavailable
        self._available()
        if time.monotonic() >= deadline:
            raise StandingUnavailable

    def _native_state(self):
        # These are transaction-neutral helpers. Calling public observation or
        # Task lookup here would start a nested BEGIN on the same connection.
        self._collaborators()
        meta, generations, claim, initial, coverage, reservation = self.resources._observation()
        if self._backend is not None and meta["backend_id"] != self._backend:
            raise StandingUnavailable
        closure = self.tasks._closures.observe(meta, claim, initial)
        return meta, generations, claim, initial, coverage, reservation, closure

    def _tables(self, meta):
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
            "OR length(CAST(backend_id AS BLOB))!=32 LIMIT 1"
        ):
            raise StandingUnavailable
        if self._one(f"SELECT backend_id FROM {_META}")[0] != meta["backend_id"]:
            raise StandingUnavailable

    def _row(self, table, field, maximum):
        count = self._one(f"SELECT count(*) FROM {table}")[0]
        if type(count) is not int or not 0 <= count <= 1:
            raise StandingUnavailable
        if self._one(
            f"SELECT 1 FROM {table} WHERE typeof(claim_id)!='text' "
            f"OR length(CAST(claim_id AS BLOB))!=64 OR typeof({field})!='text' "
            f"OR length(CAST({field} AS BLOB)) NOT BETWEEN 1 AND ? LIMIT 1",
            (maximum,),
        ):
            raise StandingUnavailable
        return None if not count else self._one(f"SELECT claim_id,{field} FROM {table}")

    def _context(self, request, binding, state):
        meta, generations, claim, initial, coverage, reservation, closure = state
        original = request.original
        if original.expected_backend != meta["backend_id"]:
            raise StandingConflict
        self.tasks.dispatch._known(original.expected_instance, generations)
        if claim is None or finalization_bytes(claim.request) != finalization_bytes(original):
            raise StandingConflict
        if binding is None or closure is None:
            raise StandingConflict
        if (
            request.expected_resource_backend != binding.resource_backend
            or request.expected_resource_instance != binding.resource_instance
        ):
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
        observation = StandingResourceObservation(
            **body, observation_digest=standing_resource_observation_digest(body)
        ).validate_request(original)
        return StandingResourceFinalizeContext(
            schema_version="1.0",
            request=request,
            binding=binding,
            closure=closure,
            observation=observation,
            context_digest=resource_finalize_context_digest(request, binding, closure, observation),
        ).validate_request(request)

    def _schema(self, state):
        meta, generations, claim, initial, _, reservation, _ = state
        self._tables(meta)
        row = self._row(_BINDINGS, "request", MAX_STANDING_RESOURCE_BINDING_BYTES)
        binding = None
        if row is not None:
            binding = StandingResourceBindingRequest.from_json(row["request"].encode("utf-8"))
            if (
                claim is None
                or row["claim_id"] != claim.claim_id
                or finalization_bytes(binding).decode("utf-8") != row["request"]
                or finalization_bytes(binding.claim) != finalization_bytes(claim)
                or binding.claim.request.expected_backend != meta["backend_id"]
            ):
                raise StandingUnavailable
            self.tasks.dispatch._known(
                binding.claim.request.expected_instance, generations, stored=True
            )
            if reservation is not None and (
                reservation.resource_backend != binding.resource_backend
                or reservation.resource_instance != binding.resource_instance
                or initial is None
                or finalization_bytes(reservation.context.standing) != finalization_bytes(initial)
            ):
                raise StandingUnavailable
        row = self._row(_FINALIZATIONS, "report", MAX_STANDING_RESOURCE_FINALIZATION_BYTES)
        report = None
        if row is not None:
            report = StandingResourceFinalization.from_json(row["report"].encode("utf-8"))
            if (
                claim is None
                or binding is None
                or row["claim_id"] != claim.claim_id
                or finalization_bytes(report).decode("utf-8") != row["report"]
            ):
                raise StandingUnavailable
            try:
                expected = self._context(report.context.request, binding, state)
            except StandingConflict:
                raise StandingUnavailable from None
            if finalization_bytes(report.context) != finalization_bytes(expected):
                raise StandingUnavailable
        return binding, report

    def _observation(self):
        self._available()
        state = self._native_state()
        binding, report = self._schema(state)
        return state, binding, report

    def check_reservation(self, original):
        """Join a possible new reservation inside its caller's transaction."""
        self._available()
        if not self._connection.in_transaction:
            raise StandingUnavailable
        state, binding, _ = self._observation()
        if binding is None:
            return  # Existing unbound claims retain their original protocol.
        if (
            original.resource_backend != binding.resource_backend
            or original.resource_instance != binding.resource_instance
            or original.context.standing is None
            or finalization_bytes(original.context.standing.claim)
            != finalization_bytes(binding.claim)
            or finalization_bytes(state[2]) != finalization_bytes(binding.claim)
        ):
            raise StandingConflict

    @_native
    def register(self) -> None:
        if self._closed or self._failed:
            raise StandingUnavailable
        if self._registered:
            raise StandingConflict
        created = False

        def final():
            state = self._native_state()
            self._schema(state)
            if self._closed or self._failed:
                raise StandingUnavailable
            if created:
                self._writer(state[0], registering=True)

        try:
            # Registration is also the native composition boundary. A caller
            # using the declared owners directly gets the same reservation
            # fence as Session's explicit HTTP composition.
            self.resources.register_finalization_owner(self)
            with self._transaction(final):
                state = self._native_state()
                meta = state[0]
                present = self._all(
                    "SELECT name,type FROM sqlite_schema WHERE name IN (?,?,?)",
                    (_META, _BINDINGS, _FINALIZATIONS),
                )
                if not present:
                    try:
                        self._writer(meta, registering=True)
                    except StandingConflict:
                        raise StandingUnavailable from None
                    for sql, _ in _TABLES.values():
                        self._sql(sql)
                    self._sql(f"INSERT INTO {_META} VALUES(1,1,?)", (meta["backend_id"],))
                    created = True
                elif len(present) != 3 or any(row["type"] != "table" for row in present):
                    raise StandingUnavailable
                self._schema(state)
                instance = self.control.instance
            self._backend, self._instance, self._registered = meta["backend_id"], instance, True
        except BaseException:
            self._failed = True
            raise

    def _original_claim(self, original, state):
        meta, generations, claim, *_ = state
        if original.claim.request.expected_backend != meta["backend_id"]:
            raise StandingConflict
        self.tasks.dispatch._known(original.claim.request.expected_instance, generations)
        if claim is None or finalization_bytes(claim) != finalization_bytes(original.claim):
            raise StandingConflict

    def _new_binding(self, original, state):
        self._original_claim(original, state)
        if state[3] is not None or state[6] is not None:
            raise StandingConflict

    @staticmethod
    def _binding_receipt(binding, disposition):
        return StandingResourceBindingReceipt(
            schema_version="1.0",
            disposition=disposition,
            request=binding,
            request_digest=resource_binding_digest(binding),
        ).validate_request(binding)

    @_native
    def bind(
        self, request: StandingResourceBindingRequest, *, deadline=None, cancelled=None
    ) -> StandingResourceBindingReceipt:
        deadline = self.tasks.claims._options(deadline, cancelled)
        original = checked_finalization(request, StandingResourceBindingRequest)
        self._guard(deadline, cancelled)
        created = False

        def final():
            state, retained, _ = self._observation()
            if retained is None or finalization_bytes(retained) != finalization_bytes(original):
                raise StandingUnavailable
            if created:
                try:
                    self._new_binding(original, state)
                except StandingConflict:
                    raise StandingUnavailable from None
            self._guard(deadline, cancelled)
            if created:
                self._writer(state[0])

        with self._transaction(final):
            state, retained, _ = self._observation()
            self._original_claim(original, state)
            if retained is not None:
                if finalization_bytes(retained) != finalization_bytes(original):
                    raise StandingConflict
            else:
                self._new_binding(original, state)
                self._writer(state[0])
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_BINDINGS} VALUES(?,?)",
                    (original.claim.claim_id, finalization_bytes(original).decode("utf-8")),
                )
                created = True
            result = self._binding_receipt(original, "created" if created else "retained")
        self._guard(deadline, cancelled)
        return result

    @_native
    def binding_lookup(
        self, request: StandingResourceBindingRequest
    ) -> StandingResourceBindingReceipt | None:
        original = checked_finalization(request, StandingResourceBindingRequest)
        with self._transaction(self._available):
            state, retained, _ = self._observation()
            self._original_claim(original, state)
            if retained is None:
                return None
            if finalization_bytes(retained) != finalization_bytes(original):
                raise StandingConflict
            return self._binding_receipt(retained, "retained")

    @_native
    def context(
        self, finalize_request: StandingResourceFinalizeRequest
    ) -> StandingResourceFinalizeContext:
        original = checked_finalization(finalize_request, StandingResourceFinalizeRequest)
        with self._transaction(self._available):
            state, binding, _ = self._observation()
            return self._context(original, binding, state)

    @staticmethod
    def _receipt(report, disposition):
        return StandingResourceFinalizationReceipt(
            schema_version="1.0", disposition=disposition, finalization=report
        ).validate_request(report.context.request)

    @_native
    def report(
        self, finalization: StandingResourceFinalization, *, deadline=None, cancelled=None
    ) -> StandingResourceFinalizationReceipt:
        deadline = self.tasks.claims._options(deadline, cancelled)
        original = checked_finalization(finalization, StandingResourceFinalization)
        self._guard(deadline, cancelled)
        created = False

        def final():
            state, binding, retained = self._observation()
            if retained is None or finalization_bytes(retained) != finalization_bytes(original):
                raise StandingUnavailable
            expected = self._context(original.context.request, binding, state)
            if finalization_bytes(expected) != finalization_bytes(original.context):
                raise StandingUnavailable
            self._guard(deadline, cancelled)
            if created:
                self._writer(state[0])

        with self._transaction(final):
            state, binding, retained = self._observation()
            expected = self._context(original.context.request, binding, state)
            if finalization_bytes(expected) != finalization_bytes(original.context):
                raise StandingConflict
            if retained is not None:
                if finalization_bytes(retained) != finalization_bytes(original):
                    raise StandingConflict
            else:
                self._writer(state[0])
                self._guard(deadline, cancelled)
                self._sql(
                    f"INSERT INTO {_FINALIZATIONS} VALUES(?,?)",
                    (binding.claim.claim_id, finalization_bytes(original).decode("utf-8")),
                )
                created = True
            result = self._receipt(original, "created" if created else "retained")
        self._guard(deadline, cancelled)
        return result

    @_native
    def lookup(
        self, finalize_request: StandingResourceFinalizeRequest
    ) -> StandingResourceFinalizationReceipt | None:
        original = checked_finalization(finalize_request, StandingResourceFinalizeRequest)
        with self._transaction(self._available):
            state, binding, retained = self._observation()
            meta, generations, claim, *_ = state
            if original.original.expected_backend != meta["backend_id"]:
                raise StandingConflict
            self.tasks.dispatch._known(original.original.expected_instance, generations)
            if claim is None or finalization_bytes(claim.request) != finalization_bytes(
                original.original
            ):
                raise StandingConflict
            if binding is not None and (
                binding.resource_backend != original.expected_resource_backend
                or binding.resource_instance != original.expected_resource_instance
            ):
                raise StandingConflict
            if retained is None:
                return None
            if finalization_bytes(retained.context.request) != finalization_bytes(original):
                raise StandingConflict
            return self._receipt(retained, "retained")
