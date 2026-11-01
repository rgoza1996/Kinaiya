"""Codex-authored atomic standing initial-task admission; no worker or release."""

import hmac
import time
from contextlib import contextmanager

from packages.contracts.agent_control import MAX_AGENT_CONTROL_INTEGER
from packages.contracts.standing_admission import (
    MAX_STANDING_INITIAL_PROOF_BYTES,
    StandingInitialProof,
    StandingTaskAdmission,
    standing_entry,
    standing_initial_proof,
)
from packages.contracts.standing_execution import (
    StandingExecutionIntent,
    capability_digest,
    standing_initial_request,
)
from packages.contracts.standing_work import (
    StandingConflict,
    StandingUnavailable,
    _checked,
    _utc,
    canonical_bytes,
    standing_json,
)
from services.session.standing_execution import _native

_TABLE = "standing_task_admissions"
_COLUMNS = (
    ("claim_id", "TEXT", 0, None, 1, 0),
    ("event_id", "TEXT", 1, None, 0, 0),
    ("sequence", "INTEGER", 1, None, 0, 0),
    ("proof", "TEXT", 1, None, 0, 0),
)
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "thread_id",
        "epoch",
        "kind",
        "payload",
        "causes",
        "task_refs",
        "idempotency_key",
    }
)
_ENTRY_COLUMNS = "sequence,event_id,session_id,thread_id,epoch,kind,writer,idem,request,value"
# Inspect serialized kind as well as SQL provenance, without importing bodies
# from unrelated tools/outcomes. A changed SQL kind cannot hide an orphan.
_STANDING_ROWS = (
    "kind='agent.standing-command' OR writer='standing-supervisor' "
    "OR CASE WHEN json_valid(request) THEN json_extract(request,'$.kind') END"
    "='agent.standing-command' "
    "OR CASE WHEN json_valid(value) THEN json_extract(value,'$.kind') END"
    "='agent.standing-command'"
)


class StandingTaskOwner:
    """Explicit same-Store admission capability for the sticky prepared claim.

    Neither the retained proof nor local close creates a launch opportunity or
    settles work. The raw capability is consumed only for a first admission.
    """

    def __init__(self, store, control, owner, dispatch, claims):
        self.store, self.control, self.owner = store, control, owner
        self.dispatch, self.claims = dispatch, claims
        self._registered = False
        self._closed = False
        self._failed = False
        self._backend = None
        self._instance = None

    def close(self) -> None:
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

    def _available(self):
        if not self._registered or self._closed or self._failed:
            raise StandingUnavailable

    def _collaborators(self):
        if (
            self.control.store is not self.store
            or self.owner.store is not self.store
            or self.dispatch.store is not self.store
            or self.claims.store is not self.store
            or self.claims.control is not self.control
            or self.claims.owner is not self.owner
            or self.claims.dispatch is not self.dispatch
            or self.store._agent_submit_unavailable
        ):
            raise StandingUnavailable
        self.claims._available()
        self.claims._collaborators()

    def _joins(self):
        self._collaborators()
        return self.claims._observation()

    def _writer(self, meta, *, registering=False):
        self.claims._writer(meta)
        if not registering and self._instance != self.control.instance:
            raise StandingConflict

    def _entry(self, event_id):
        # Bound scalar and text storage before fetching either complete body.
        if self._one(
            "SELECT 1 FROM entries WHERE event_id=? AND (typeof(sequence)!='integer' "
            "OR sequence<1 OR sequence>? OR typeof(event_id)!='text' "
            "OR length(CAST(event_id AS BLOB)) NOT BETWEEN 1 AND 512 "
            "OR typeof(session_id)!='text' "
            "OR length(CAST(session_id AS BLOB)) NOT BETWEEN 1 AND 512 "
            "OR typeof(thread_id)!='text' "
            "OR length(CAST(thread_id AS BLOB)) NOT BETWEEN 1 AND 512 "
            "OR typeof(epoch)!='integer' OR epoch<1 OR epoch>? "
            "OR typeof(kind)!='text' OR typeof(writer)!='text' OR typeof(idem)!='text' "
            "OR length(CAST(kind AS BLOB)) NOT BETWEEN 1 AND 128 "
            "OR length(CAST(writer AS BLOB)) NOT BETWEEN 1 AND 128 "
            "OR length(CAST(idem AS BLOB)) NOT BETWEEN 1 AND 1024 "
            "OR typeof(request)!='text' "
            "OR length(CAST(request AS BLOB)) NOT BETWEEN 1 AND 65536 "
            "OR typeof(value)!='text' "
            "OR length(CAST(value AS BLOB)) NOT BETWEEN 1 AND 69632)",
            (event_id, MAX_AGENT_CONTROL_INTEGER, MAX_AGENT_CONTROL_INTEGER),
        ):
            raise StandingUnavailable
        row = self._one(f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE event_id=?", (event_id,))
        if row is None:
            raise StandingUnavailable
        return row

    def _proof(self, row, claim):
        proof = StandingInitialProof.from_json(row["proof"].encode("utf-8"))
        if (
            claim is None
            or canonical_bytes(proof.claim) != canonical_bytes(claim)
            or canonical_bytes(proof).decode("utf-8") != row["proof"]
            or row["claim_id"] != claim.claim_id
            or row["event_id"] != proof.event_id
            or row["sequence"] != proof.sequence
        ):
            raise StandingUnavailable
        native = self._entry(proof.event_id)
        value = standing_json(native["value"].encode("utf-8"), maximum=69632)
        entry = standing_entry(value, claim)
        request = standing_json(native["request"].encode("utf-8"), maximum=65536)
        if request.keys() != _REQUEST_FIELDS:
            raise StandingUnavailable
        standing_entry(
            {
                **request,
                **{key: value[key] for key in ("event_id", "sequence", "origin", "created_at")},
            },
            claim,
        )
        if (
            native["writer"] != "standing-supervisor"
            or native["event_id"] != entry.event_id
            or native["sequence"] != entry.sequence
            or native["session_id"] != entry.session_id
            or native["thread_id"] != entry.thread_id
            or native["epoch"] != entry.epoch
            or native["kind"] != entry.kind
            or native["idem"] != entry.idempotency_key
            or canonical_bytes(standing_initial_proof(claim, entry)) != canonical_bytes(proof)
        ):
            raise StandingUnavailable
        return proof

    def _schema(self, claim):
        columns = tuple(
            (r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"], r["hidden"])
            for r in self._all(f"PRAGMA table_xinfo({_TABLE})")
        )
        if columns != _COLUMNS or self._all(f"PRAGMA foreign_key_list({_TABLE})"):
            raise StandingUnavailable
        unique = set()
        for index in self._all(f"PRAGMA index_list({_TABLE})"):
            if index["unique"] == 1:
                if index["partial"] != 0:
                    raise StandingUnavailable
                name = index["name"].replace('"', '""')
                unique.add(tuple(r["name"] for r in self._all(f'PRAGMA index_info("{name}")')))
        if unique != {("claim_id",), ("event_id",), ("sequence",)}:
            raise StandingUnavailable
        count = self._one(f"SELECT count(*) FROM {_TABLE}")[0]
        if type(count) is not int or not 0 <= count <= 1:
            raise StandingUnavailable
        if self._one(
            f"SELECT 1 FROM {_TABLE} WHERE typeof(claim_id)!='text' "
            "OR length(CAST(claim_id AS BLOB))!=64 OR typeof(event_id)!='text' "
            "OR length(CAST(event_id AS BLOB)) NOT BETWEEN 1 AND 512 "
            "OR typeof(sequence)!='integer' OR sequence<1 OR sequence>? "
            "OR typeof(proof)!='text' OR length(CAST(proof AS BLOB)) NOT BETWEEN 1 AND ? LIMIT 1",
            (MAX_AGENT_CONTROL_INTEGER, MAX_STANDING_INITIAL_PROOF_BYTES),
        ):
            raise StandingUnavailable
        if self._one(f"SELECT count(*) FROM entries WHERE {_STANDING_ROWS}")[0] != count:
            raise StandingUnavailable
        if not count:
            return None
        return self._proof(self._one(f"SELECT * FROM {_TABLE}"), claim)

    def _observation(self):
        self._available()
        meta, generations, gate, activation, producer, claim = self._joins()
        if self._backend != meta["backend_id"]:
            raise StandingUnavailable
        return meta, generations, gate, activation, producer, claim, self._schema(claim)

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
                meta, _, _, _, _, claim = self._joins()
                table = self._one("SELECT type FROM sqlite_schema WHERE name=?", (_TABLE,))
                if table is None:
                    try:
                        self._writer(meta, registering=True)
                    except StandingConflict:
                        raise StandingUnavailable from None
                    if self._one(f"SELECT 1 FROM entries WHERE {_STANDING_ROWS} LIMIT 1"):
                        raise StandingUnavailable
                    self._sql(
                        f"CREATE TABLE {_TABLE}(claim_id TEXT PRIMARY KEY,"
                        "event_id TEXT NOT NULL UNIQUE,sequence INTEGER NOT NULL UNIQUE,"
                        "proof TEXT NOT NULL)"
                    )
                    created = True
                elif table["type"] != "table":
                    raise StandingUnavailable
                self._schema(claim)
                instance = self.control.instance
            self._backend, self._instance, self._registered = meta["backend_id"], instance, True
        except BaseException:
            self._failed = True
            raise

    def _guard(self, deadline, cancelled):
        if cancelled is not None:
            value = cancelled()
            if type(value) is not bool or value:
                raise StandingUnavailable
        self._available()
        self._collaborators()
        if time.monotonic() >= deadline:
            raise StandingUnavailable

    def _new_authority(self, claim, deadline, cancelled):
        self._guard(deadline, cancelled)
        meta, generations, gate, activation, producer, current, _ = self._observation()
        self._writer(meta)
        request = claim.request
        if current is None or canonical_bytes(current) != canonical_bytes(claim):
            raise StandingUnavailable
        if request.expected_backend != meta["backend_id"]:
            raise StandingConflict
        self.dispatch._known(request.expected_instance, generations)
        if request.expected_instance != meta["instance_id"]:
            raise StandingConflict
        record = self.owner._current_record(request.responsibility_id, meta)
        if record is None or record.receipt.revision != request.authorization_revision:
            raise StandingConflict
        latest = self.dispatch._producer_row(producer, meta, generations)
        if (
            meta["mode"] != "autonomous"
            or gate["epoch"] != request.dispatch_epoch
            or gate["epoch"] <= gate["stopped_through_epoch"]
            or activation is None
            or activation.activation_digest != request.activation_digest
            or activation.mode_receipt.revision != request.control_revision
            or meta["revision"] != request.control_revision
            or latest is None
            or latest.request.producer_id != request.producer.request.producer_id
            or latest.request.expected_instance != request.expected_instance
            or latest.request.sequence < request.producer.request.sequence
            or (
                latest.request.sequence == request.producer.request.sequence
                and canonical_bytes(latest) != canonical_bytes(request.producer)
            )
        ):
            raise StandingConflict
        self.owner._agent_session(record.receipt.request)
        now = self.claims._clock()
        if _utc(now) < _utc(claim.prepared_utc):
            raise StandingUnavailable
        self.claims._grant(request, record, now)
        if not self.dispatch._live(meta, producer, self.dispatch._sample()):
            raise StandingConflict
        self._writer(meta)

    def _task_available(self, task_id):
        # Match actual initial native records, not task mentions in model/tool
        # text. Both serialized projections can identify a contradictory row.
        row = self._one(
            "SELECT event_id FROM entries WHERE (kind='agent.command' "
            "OR CASE WHEN json_valid(request) THEN json_extract(request,'$.kind') END"
            "='agent.command' "
            "OR CASE WHEN json_valid(value) THEN json_extract(value,'$.kind') END"
            "='agent.command') AND ("
            "CASE WHEN json_valid(request) THEN json_extract(request,'$.payload.op') END='submit' "
            "OR CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.op') END='submit') "
            "AND (CASE WHEN json_valid(request) THEN json_extract(request,'$.payload.task_id') "
            "END=? OR CASE WHEN json_valid(value) THEN json_extract(value,'$.payload.task_id') "
            "END=?) ORDER BY sequence LIMIT 1",
            (task_id, task_id),
        )
        if row is not None:
            native = self._entry(row["event_id"])
            entry = self.store._agent_submit_check_row(native)
            self.store._agent_submit_winner(
                entry.event_id, entry.session_id, entry.payload["idempotency_key"]
            )
            raise StandingConflict

    @_native
    def admit(
        self, request: StandingExecutionIntent, *, capability: bytes, deadline=None, cancelled=None
    ) -> StandingTaskAdmission:
        deadline = self.claims._options(deadline, cancelled)
        request = _checked(request, StandingExecutionIntent)
        created = False

        def commit_guard():
            if not created:
                return
            try:
                self._new_authority(claim, deadline, cancelled)
            except StandingConflict:
                raise StandingUnavailable from None
            # The last guard follows every potentially blocking SQL/clock read.
            self._guard(deadline, cancelled)

        with self._transaction(commit_guard):
            meta, generations, _, _, _, claim, previous = self._observation()
            if request.expected_backend != meta["backend_id"]:
                raise StandingConflict
            if previous is not None:
                if canonical_bytes(previous.claim.request) != canonical_bytes(request):
                    raise StandingConflict
                return StandingTaskAdmission(
                    schema_version="1.0", disposition="retained", proof=previous
                ).validate_request(request)
            self.dispatch._known(request.expected_instance, generations)
            if claim is None or canonical_bytes(claim.request) != canonical_bytes(request):
                raise StandingConflict
            if type(capability) is not bytes or len(capability) != 32:
                raise StandingConflict
            digest = capability_digest(
                request.expected_backend, request.idempotency_key, request.executor_id, capability
            )
            if not hmac.compare_digest(digest, request.capability_sha256):
                raise StandingConflict
            self._new_authority(claim, deadline, cancelled)
            self._task_available(request.task.task_id)
            initial = standing_initial_request(claim)
            self._guard(deadline, cancelled)
            entry = self.store._append_entry_locked(initial, "standing-supervisor")
            proof = standing_initial_proof(claim, standing_entry(entry, claim))
            self._sql(
                f"INSERT INTO {_TABLE} VALUES(?,?,?,?)",
                (
                    claim.claim_id,
                    proof.event_id,
                    proof.sequence,
                    canonical_bytes(proof).decode("utf-8"),
                ),
            )
            self._schema(claim)
            created = True
        # A late or lost ACK leaves durable proof; it never fabricates created.
        self._guard(deadline, cancelled)
        return StandingTaskAdmission(
            schema_version="1.0", disposition="created", proof=proof
        ).validate_request(request)

    @_native
    def lookup(self, request: StandingExecutionIntent) -> StandingInitialProof | None:
        request = _checked(request, StandingExecutionIntent)
        with self._transaction():
            meta, generations, _, _, _, claim, proof = self._observation()
            if request.expected_backend != meta["backend_id"]:
                raise StandingConflict
            self.dispatch._known(request.expected_instance, generations)
            if claim is not None and claim.request.idempotency_key == request.idempotency_key:
                if canonical_bytes(claim.request) != canonical_bytes(request):
                    raise StandingConflict
                if proof is not None:
                    return proof.validate_request(request)
            return None
