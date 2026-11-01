"""Codex-authored native standing dispatch proofs; no timer or work producer."""

import hashlib
import math
import time
import uuid
from contextlib import contextmanager
from functools import wraps

from pydantic import TypeAdapter

from packages.contracts.agent_control import (
    MAX_AGENT_CONTROL_BYTES,
    MAX_AGENT_CONTROL_INTEGER,
    MAX_AGENT_CONTROL_RESPONSE_BYTES,
    AgentModeChange,
    AgentModeReceipt,
    ControlInstance,
)
from packages.contracts.standing_dispatch import (
    MAX_STANDING_DISPATCH_EPOCH,
    STANDING_PRODUCER_TTL_MS,
    StandingDispatchActivation,
    StandingDispatchActivationLookup,
    StandingDispatchActivationReceipt,
    StandingDispatchState,
    StandingDispatchStop,
    StandingDispatchStopProof,
    StandingProducerReceipt,
    StandingProducerRenewal,
    activation_digest,
)
from packages.contracts.standing_work import (
    StandingConflict,
    StandingUnavailable,
    canonical_bytes,
)

_MAX_RECORDS = 10000
_UUID = TypeAdapter(ControlInstance)
_CONTROL_TABLES = {
    "agent_control_state",
    "agent_control_generations",
    "agent_control_changes",
    "agent_steer_intents",
}
_TABLES = {
    "standing_dispatch_meta": (
        ("id", "INTEGER", 1),
        ("version", "INTEGER", 0),
        ("backend_id", "TEXT", 0),
    ),
    "standing_dispatch_gate": (
        ("id", "INTEGER", 1),
        ("epoch", "INTEGER", 0),
        ("stopped_through_epoch", "INTEGER", 0),
        ("fence_token", "TEXT", 0),
        ("current_producer_id", "TEXT", 0),
    ),
    "standing_dispatch_activations": (
        ("key", "TEXT", 1),
        ("epoch", "INTEGER", 0),
        ("request", "TEXT", 0),
        ("receipt", "TEXT", 0),
    ),
    "standing_dispatch_producers": (
        ("producer_id", "TEXT", 1),
        ("instance_id", "TEXT", 0),
        ("sequence", "INTEGER", 0),
        ("receipt", "TEXT", 0),
        ("retired", "INTEGER", 0),
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


class _ClockUnavailable(Exception):
    pass


class StandingDispatchOwner:
    """Explicit backend-bound capability; opening or closing it registers no writer.

    Only a positively current supplied Session controller can create schema or
    admit a new lease/activation. Established valid schema remains inspectable
    and stoppable without a current writer, including at its generation cap.
    """

    def __init__(self, store, control, *, monotonic_ms=None):
        self.store, self.control = store, control
        self.monotonic_ms = (
            monotonic_ms if monotonic_ms is not None else lambda: time.monotonic_ns() // 1_000_000
        )
        self._registered = False
        self._closed = False
        self._failed = False
        self._backend = None
        self._instance = None
        self._last_clock = None
        self._clock_failed = False

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

    @staticmethod
    def _known(instance, generations, *, stored=False):
        if instance not in generations:
            if stored:
                raise StandingUnavailable
            raise StandingConflict
        return generations[instance]

    def _metadata(self):
        if self.control.store is not self.store:
            raise StandingUnavailable
        tables = {
            row["name"] for row in self._all("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        # Never let AgentControl.metadata migrate through a passive dispatch
        # reader or recover missing bootstrap authority by choosing defaults.
        if not _CONTROL_TABLES <= tables:
            raise StandingUnavailable
        meta = self.control.metadata()
        rows = self._all("SELECT instance_id,retired FROM agent_control_generations")
        if len(rows) > _MAX_RECORDS:
            raise StandingUnavailable
        generations = {}
        for row in rows:
            instance = _UUID.validate_python(row["instance_id"], strict=True)
            if type(row["retired"]) is not int or row["retired"] not in (0, 1):
                raise StandingUnavailable
            generations[instance] = bool(row["retired"])
        self._mode_counts(meta)
        if meta["revision"] == 0:
            if meta["mode"] != meta["initial_mode"]:
                raise StandingUnavailable
        else:
            key = self._one(
                "SELECT key FROM agent_control_changes WHERE json_extract(receipt,'$.revision')=?",
                (meta["revision"],),
            )["key"]
            if self._mode_row(key, meta, generations)[1].request.mode != meta["mode"]:
                raise StandingUnavailable
        return meta, generations

    def _mode_counts(self, meta):
        if self._one(
            "SELECT 1 FROM agent_control_changes WHERE typeof(key)!='text' OR typeof(digest)!='text' "
            "OR typeof(request)!='text' OR typeof(receipt)!='text' "
            "OR length(CAST(request AS BLOB))>? OR length(CAST(receipt AS BLOB))>? "
            "OR NOT json_valid(request) OR NOT json_valid(receipt) LIMIT 1",
            (MAX_AGENT_CONTROL_BYTES, MAX_AGENT_CONTROL_RESPONSE_BYTES),
        ):
            raise StandingUnavailable
        if self._one(
            "SELECT 1 FROM agent_control_changes WHERE json_type(receipt,'$.revision') IS NOT 'integer' "
            "OR json_type(request,'$.expected_revision') IS NOT 'integer' "
            "OR json_extract(receipt,'$.revision')<1 OR json_extract(receipt,'$.revision')>? "
            "OR json_extract(receipt,'$.revision')!=json_extract(request,'$.expected_revision')+1 LIMIT 1",
            (_MAX_RECORDS,),
        ):
            raise StandingUnavailable
        count, distinct, low, high = self._one(
            "SELECT count(*),count(DISTINCT json_extract(receipt,'$.revision')),"
            "min(json_extract(receipt,'$.revision')),max(json_extract(receipt,'$.revision')) "
            "FROM agent_control_changes"
        )
        if (
            count != meta["revision"]
            or count > _MAX_RECORDS
            or (count and (distinct != count or low != 1 or high != count))
        ):
            raise StandingUnavailable

    @staticmethod
    def _decode(kind, text):
        if type(text) is not str:
            raise StandingUnavailable
        value = kind.from_json(text.encode("utf-8"))
        if canonical_bytes(value).decode("utf-8") != text:
            raise StandingUnavailable
        return value

    def _mode_row(self, key, meta, generations):
        row = self._one("SELECT * FROM agent_control_changes WHERE key=?", (key,))
        if row is None:
            return None
        original = self._decode(AgentModeChange, row["request"])
        receipt = self._decode(AgentModeReceipt, row["receipt"]).validate_request(original)
        if (
            type(row["key"]) is not str
            or row["key"] != original.idempotency_key
            or type(row["digest"]) is not str
            or row["digest"] != hashlib.sha256(canonical_bytes(original)).hexdigest()
            or receipt.backend_id != meta["backend_id"]
            or receipt.revision > meta["revision"]
        ):
            raise StandingUnavailable
        self._known(original.expected_instance, generations, stored=True)
        return original, receipt

    def _activation_row(self, row, meta, generations):
        if row is None:
            return None
        original = self._decode(StandingDispatchActivation, row["request"])
        receipt = self._decode(StandingDispatchActivationReceipt, row["receipt"]).validate_request(
            original
        )
        ordinary = self._mode_row(original.mode_change.idempotency_key, meta, generations)
        if (
            type(row["key"]) is not str
            or row["key"] != original.mode_change.idempotency_key
            or type(row["epoch"]) is not int
            or row["epoch"] != receipt.epoch
            or ordinary is None
            or canonical_bytes(ordinary[0]) != canonical_bytes(original.mode_change)
            or canonical_bytes(ordinary[1]) != canonical_bytes(receipt.mode_receipt)
        ):
            raise StandingUnavailable
        return original, receipt

    def _producer_row(self, row, meta, generations):
        if row is None:
            return None
        receipt = self._decode(StandingProducerReceipt, row["receipt"])
        original = receipt.request
        if (
            row["producer_id"] != original.producer_id
            or row["instance_id"] != original.expected_instance
            or type(row["sequence"]) is not int
            or row["sequence"] != original.sequence
            or type(row["retired"]) is not int
            or row["retired"] not in (0, 1)
            or original.expected_backend != meta["backend_id"]
        ):
            raise StandingUnavailable
        self._known(original.expected_instance, generations, stored=True)
        return receipt

    def _unique(self, table):
        result = set()
        for index in self._all(f"PRAGMA index_list({table})"):
            if index["unique"] == 1 and index["partial"] == 0:
                name = index["name"].replace('"', '""')
                result.add(tuple(row["name"] for row in self._all(f'PRAGMA index_info("{name}")')))
        return result

    def _schema(self, meta, generations):
        for name, columns in _TABLES.items():
            if (
                tuple(
                    (r["name"], r["type"], r["pk"]) for r in self._all(f"PRAGMA table_info({name})")
                )
                != columns
            ):
                raise StandingUnavailable
        if not {("key",), ("epoch",)} <= self._unique("standing_dispatch_activations") or (
            ("producer_id",) not in self._unique("standing_dispatch_producers")
        ):
            raise StandingUnavailable
        markers = self._all("SELECT * FROM standing_dispatch_meta")
        if len(markers) != 1 or (
            type(markers[0]["id"]) is not int
            or markers[0]["id"] != 1
            or type(markers[0]["version"]) is not int
            or markers[0]["version"] != 1
            or type(markers[0]["backend_id"]) is not str
            or markers[0]["backend_id"] != meta["backend_id"]
        ):
            raise StandingUnavailable
        rows = self._all("SELECT * FROM standing_dispatch_gate")
        if len(rows) != 1:
            raise StandingUnavailable
        gate = rows[0]
        if (
            type(gate["id"]) is not int
            or gate["id"] != 1
            or type(gate["epoch"]) is not int
            or not 0 <= gate["epoch"] <= MAX_STANDING_DISPATCH_EPOCH
            or type(gate["stopped_through_epoch"]) is not int
            or not 0 <= gate["stopped_through_epoch"] <= gate["epoch"]
        ):
            raise StandingUnavailable
        _UUID.validate_python(gate["fence_token"], strict=True)
        if self._one(
            "SELECT 1 FROM standing_dispatch_activations WHERE typeof(epoch)!='integer' "
            "OR epoch<1 OR epoch>? OR typeof(key)!='text' OR typeof(request)!='text' "
            "OR typeof(receipt)!='text' LIMIT 1",
            (MAX_STANDING_DISPATCH_EPOCH,),
        ):
            raise StandingUnavailable
        count, low, high = self._one(
            "SELECT count(*),min(epoch),max(epoch) FROM standing_dispatch_activations"
        )
        if count != gate["epoch"] or (count and (low != 1 or high != count)):
            raise StandingUnavailable
        activation = None
        if count:
            activation = self._activation_row(
                self._one(
                    "SELECT * FROM standing_dispatch_activations WHERE epoch=?", (gate["epoch"],)
                ),
                meta,
                generations,
            )[1]
        producers = self._all(
            "SELECT producer_id,instance_id,sequence,retired FROM standing_dispatch_producers"
        )
        if len(producers) > _MAX_RECORDS:
            raise StandingUnavailable
        active = []
        for row in producers:
            _UUID.validate_python(row["producer_id"], strict=True)
            _UUID.validate_python(row["instance_id"], strict=True)
            if (
                type(row["sequence"]) is not int
                or not 1 <= row["sequence"] <= MAX_AGENT_CONTROL_INTEGER
                or type(row["retired"]) is not int
                or row["retired"] not in (0, 1)
            ):
                raise StandingUnavailable
            self._known(row["instance_id"], generations, stored=True)
            if not row["retired"]:
                active.append(row["producer_id"])
        pointer = gate["current_producer_id"]
        if pointer is not None:
            _UUID.validate_python(pointer, strict=True)
        if active != ([] if pointer is None else [pointer]):
            raise StandingUnavailable
        producer = (
            self._one("SELECT * FROM standing_dispatch_producers WHERE producer_id=?", (pointer,))
            if pointer is not None
            else None
        )
        self._producer_row(producer, meta, generations)
        return gate, activation, producer

    def _observation(self):
        self._available()
        meta, generations = self._metadata()
        if meta["backend_id"] != self._backend:
            raise StandingUnavailable
        return meta, generations, *self._schema(meta, generations)

    def _writable(self, meta):
        return (
            self._registered
            and not self._closed
            and not self._failed
            and self._instance is not None
            and self._instance == self.control.instance == meta["instance_id"]
        )

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
            if created and (
                self.control.instance is None
                or self.control.instance != self.control.metadata()["instance_id"]
            ):
                raise StandingConflict

        try:
            with self._transaction(commit_guard):
                meta, generations = self._metadata()
                tables = {
                    r["name"]
                    for r in self._all("SELECT name FROM sqlite_schema WHERE type='table'")
                    if r["name"] in _TABLES
                }
                if not tables:
                    if (
                        self.control.instance is None
                        or self.control.instance != meta["instance_id"]
                    ):
                        raise StandingUnavailable
                    self._sql(
                        "CREATE TABLE standing_dispatch_meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,backend_id TEXT NOT NULL)"
                    )
                    self._sql(
                        "CREATE TABLE standing_dispatch_gate(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL,stopped_through_epoch INTEGER NOT NULL,fence_token TEXT NOT NULL,current_producer_id TEXT)"
                    )
                    self._sql(
                        "CREATE TABLE standing_dispatch_activations(key TEXT PRIMARY KEY,epoch INTEGER NOT NULL UNIQUE,request TEXT NOT NULL,receipt TEXT NOT NULL)"
                    )
                    self._sql(
                        "CREATE TABLE standing_dispatch_producers(producer_id TEXT PRIMARY KEY,instance_id TEXT NOT NULL,sequence INTEGER NOT NULL,receipt TEXT NOT NULL,retired INTEGER NOT NULL)"
                    )
                    self._sql(
                        "INSERT INTO standing_dispatch_meta VALUES(1,1,?)", (meta["backend_id"],)
                    )
                    self._sql(
                        "INSERT INTO standing_dispatch_gate VALUES(1,0,0,?,NULL)",
                        (str(uuid.uuid4()),),
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

    def _sample(self):
        if self._clock_failed:
            raise _ClockUnavailable
        try:
            now = self.monotonic_ms()
            if (
                type(now) is not int
                or not 0 <= now <= MAX_AGENT_CONTROL_INTEGER - STANDING_PRODUCER_TTL_MS
                or (self._last_clock is not None and now < self._last_clock)
            ):
                raise ValueError
        except Exception:
            self._clock_failed = True
            raise _ClockUnavailable from None
        self._last_clock = now
        return now

    def _live(self, meta, row, now):
        if not self._writable(meta) or row is None or row["retired"]:
            return False
        receipt = self._decode(StandingProducerReceipt, row["receipt"])
        if receipt.request.expected_instance != meta["instance_id"]:
            return False
        if now < receipt.issued_monotonic_ms:
            self._clock_failed = True
            raise _ClockUnavailable
        return now < receipt.expires_monotonic_ms

    def _state(self, meta, gate, activation, producer):
        current = None
        writable = self._writable(meta)
        if writable and producer is not None:
            try:
                if self._live(meta, producer, self._sample()):
                    current = self._decode(StandingProducerReceipt, producer["receipt"])
            except _ClockUnavailable:
                pass
        revision = activation.mode_receipt.revision if activation is not None else None
        return StandingDispatchState(
            schema_version="1.0",
            backend_id=meta["backend_id"],
            instance_id=meta["instance_id"],
            control_revision=meta["revision"],
            mode=meta["mode"],
            control_writable=writable,
            epoch=gate["epoch"],
            stopped_through_epoch=gate["stopped_through_epoch"],
            fence_token=gate["fence_token"],
            activation_digest=activation.activation_digest if activation is not None else None,
            activation_control_revision=revision,
            producer=current,
            dispatch_ready=writable
            and meta["mode"] == "autonomous"
            and gate["epoch"] > gate["stopped_through_epoch"]
            and revision == meta["revision"]
            and current is not None,
        )

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
        self._available()
        if time.monotonic() >= deadline:
            raise StandingUnavailable
        if cancelled is not None:
            value = cancelled()
            if type(value) is not bool or value:
                raise StandingUnavailable

    def _writer(self, meta):
        if not self._writable(meta):
            raise StandingConflict

    def _retained(self, request, meta, generations):
        saved = self._activation_row(
            self._one(
                "SELECT * FROM standing_dispatch_activations WHERE key=?",
                (request.mode_change.idempotency_key,),
            ),
            meta,
            generations,
        )
        if saved is None:
            return None
        if canonical_bytes(saved[0]) != canonical_bytes(request):
            raise StandingConflict
        return saved[1]

    @_native
    def snapshot(self) -> StandingDispatchState:
        with self._transaction():
            meta, _, gate, activation, producer = self._observation()
            return self._state(meta, gate, activation, producer)

    @_native
    def activate(
        self, request: StandingDispatchActivation, *, deadline=None, cancelled=None
    ) -> StandingDispatchActivationReceipt:
        request = StandingDispatchActivation.from_json(
            canonical_bytes(StandingDispatchActivation.model_validate(request))
        )
        deadline = self._options(deadline, cancelled)
        created = False
        producer = None

        def commit_guard():
            if created:
                self._guard(deadline, cancelled)
                fresh, _ = self._metadata()
                self._writer(fresh)
                if not self._live(fresh, producer, self._sample()):
                    raise StandingUnavailable

        with self._transaction(commit_guard):
            meta, generations, gate, _, producer = self._observation()
            previous = self._retained(request, meta, generations)
            if previous is not None:
                return previous
            original = request.mode_change
            if self._mode_row(original.idempotency_key, meta, generations) is not None:
                raise StandingConflict
            self._known(original.expected_instance, generations)
            self._guard(deadline, cancelled)
            self._writer(meta)
            if (
                original.expected_backend != meta["backend_id"]
                or original.expected_instance != meta["instance_id"]
                or original.expected_revision != meta["revision"]
                or request.expected_epoch != gate["epoch"]
                or request.expected_fence_token != gate["fence_token"]
            ):
                raise StandingConflict
            if gate["epoch"] == MAX_STANDING_DISPATCH_EPOCH or meta["revision"] == _MAX_RECORDS:
                raise StandingUnavailable
            if not self._live(meta, producer, self._sample()):
                raise StandingConflict
            mode_receipt = AgentModeReceipt(
                schema_version="1.0",
                status="mode_saved",
                backend_id=meta["backend_id"],
                service_instance=self._instance,
                revision=meta["revision"] + 1,
                request=original,
            )
            receipt = StandingDispatchActivationReceipt(
                schema_version="1.0",
                request=request,
                mode_receipt=mode_receipt,
                epoch=gate["epoch"] + 1,
                activation_digest=activation_digest(request, gate["epoch"] + 1),
            ).validate_request(request)
            self._sql(
                "UPDATE agent_control_state SET mode='autonomous',revision=? WHERE id=1",
                (mode_receipt.revision,),
            )
            self._sql(
                "INSERT INTO agent_control_changes VALUES(?,?,?,?)",
                (
                    original.idempotency_key,
                    hashlib.sha256(canonical_bytes(original)).hexdigest(),
                    canonical_bytes(original).decode(),
                    canonical_bytes(mode_receipt).decode(),
                ),
            )
            self._sql(
                "INSERT INTO standing_dispatch_activations VALUES(?,?,?,?)",
                (
                    original.idempotency_key,
                    receipt.epoch,
                    canonical_bytes(request).decode(),
                    canonical_bytes(receipt).decode(),
                ),
            )
            self._sql("UPDATE standing_dispatch_gate SET epoch=? WHERE id=1", (receipt.epoch,))
            created = True
            return receipt

    @_native
    def lookup(self, request: StandingDispatchActivation) -> StandingDispatchActivationLookup:
        request = StandingDispatchActivation.from_json(
            canonical_bytes(StandingDispatchActivation.model_validate(request))
        )
        with self._transaction():
            meta, generations, gate, activation, producer = self._observation()
            original = request.mode_change
            if original.expected_backend != meta["backend_id"]:
                raise StandingConflict
            receipt = self._retained(request, meta, generations)
            if (
                receipt is None
                and self._mode_row(original.idempotency_key, meta, generations) is not None
            ):
                raise StandingConflict
            self._known(original.expected_instance, generations)
            if (
                original.expected_revision > meta["revision"]
                or request.expected_epoch > gate["epoch"]
            ):
                raise StandingConflict
            state = self._state(meta, gate, activation, producer)
            return StandingDispatchActivationLookup(
                schema_version="1.0",
                status="recorded" if receipt else "unobserved",
                request=request,
                state=state,
                receipt=receipt,
                mutation_fenced=receipt is not None
                or state.instance_id != original.expected_instance
                or state.control_revision > original.expected_revision
                or state.epoch > request.expected_epoch
                or state.fence_token != request.expected_fence_token,
            ).validate_request(request)

    @_native
    def renew(
        self, request: StandingProducerRenewal, *, deadline=None, cancelled=None
    ) -> StandingProducerReceipt:
        request = StandingProducerRenewal.from_json(
            canonical_bytes(StandingProducerRenewal.model_validate(request))
        )
        deadline = self._options(deadline, cancelled)
        created = False
        new_row = None

        def commit_guard():
            if created:
                self._guard(deadline, cancelled)
                fresh, _ = self._metadata()
                self._writer(fresh)
                if not self._live(fresh, new_row, self._sample()):
                    raise StandingUnavailable

        with self._transaction(commit_guard):
            meta, generations, gate, _, current = self._observation()
            previous = self._one(
                "SELECT * FROM standing_dispatch_producers WHERE producer_id=?",
                (request.producer_id,),
            )
            prior = self._producer_row(previous, meta, generations)
            if prior is not None and request.sequence == prior.request.sequence:
                if canonical_bytes(prior.request) != canonical_bytes(request):
                    raise StandingConflict
                return prior.validate_request(request)
            self._known(request.expected_instance, generations)
            self._guard(deadline, cancelled)
            self._writer(meta)
            if (
                request.expected_backend != meta["backend_id"]
                or request.expected_instance != meta["instance_id"]
            ):
                raise StandingConflict
            if previous is not None:
                if (
                    previous["retired"]
                    or previous["instance_id"] != request.expected_instance
                    or gate["current_producer_id"] != request.producer_id
                    or request.sequence != previous["sequence"] + 1
                ):
                    raise StandingConflict
            elif request.sequence != 1:
                raise StandingConflict
            elif self._one("SELECT count(*) FROM standing_dispatch_producers")[0] == _MAX_RECORDS:
                raise StandingUnavailable
            now = self._sample()
            if previous is None and self._live(meta, current, now):
                raise StandingConflict
            receipt = StandingProducerReceipt(
                schema_version="1.0",
                request=request,
                lease_token=str(uuid.uuid4()),
                issued_monotonic_ms=now,
                expires_monotonic_ms=now + STANDING_PRODUCER_TTL_MS,
            ).validate_request(request)
            if prior is not None and receipt.lease_token == prior.lease_token:
                raise StandingUnavailable
            if previous is None:
                if current is not None:
                    self._sql(
                        "UPDATE standing_dispatch_producers SET retired=1 WHERE producer_id=?",
                        (current["producer_id"],),
                    )
                self._sql(
                    "INSERT INTO standing_dispatch_producers VALUES(?,?,?,?,0)",
                    (
                        request.producer_id,
                        request.expected_instance,
                        request.sequence,
                        canonical_bytes(receipt).decode(),
                    ),
                )
                self._sql(
                    "UPDATE standing_dispatch_gate SET current_producer_id=? WHERE id=1",
                    (request.producer_id,),
                )
            else:
                self._sql(
                    "UPDATE standing_dispatch_producers SET sequence=?,receipt=? WHERE producer_id=?",
                    (
                        request.sequence,
                        canonical_bytes(receipt).decode(),
                        request.producer_id,
                    ),
                )
            new_row = self._one(
                "SELECT * FROM standing_dispatch_producers WHERE producer_id=?",
                (request.producer_id,),
            )
            created = True
            return receipt

    @_native
    def stop(
        self, request: StandingDispatchStop, *, deadline=None, cancelled=None
    ) -> StandingDispatchStopProof:
        request = StandingDispatchStop.from_json(
            canonical_bytes(StandingDispatchStop.model_validate(request))
        )
        deadline = self._options(deadline, cancelled)
        changed = False

        def commit_guard():
            if changed:
                self._guard(deadline, cancelled)

        with self._transaction(commit_guard):
            meta, generations, gate, activation, producer = self._observation()
            if request.expected_backend != meta["backend_id"]:
                raise StandingConflict
            self._known(request.expected_instance, generations)
            if request.issued_epoch > gate["epoch"]:
                raise StandingConflict
            if request.issued_epoch:
                target = self._activation_row(
                    self._one(
                        "SELECT * FROM standing_dispatch_activations WHERE epoch=?",
                        (request.issued_epoch,),
                    ),
                    meta,
                    generations,
                )
                if target is None or target[1].activation_digest != request.activation_digest:
                    raise StandingConflict
            updated = dict(gate)
            if (
                request.issued_epoch == gate["epoch"]
                and request.expected_fence_token == gate["fence_token"]
            ):
                self._guard(deadline, cancelled)
                token = str(uuid.uuid4())
                if token == gate["fence_token"]:
                    raise StandingUnavailable
                updated.update(stopped_through_epoch=gate["epoch"], fence_token=token)
                self._sql(
                    "UPDATE standing_dispatch_gate SET stopped_through_epoch=?,fence_token=? WHERE id=1",
                    (gate["epoch"], token),
                )
                changed = True
            elif request.issued_epoch > gate["stopped_through_epoch"]:
                if request.expected_fence_token != gate["fence_token"]:
                    raise StandingConflict
                self._guard(deadline, cancelled)
                updated["stopped_through_epoch"] = request.issued_epoch
                self._sql(
                    "UPDATE standing_dispatch_gate SET stopped_through_epoch=? WHERE id=1",
                    (request.issued_epoch,),
                )
                changed = True
            return StandingDispatchStopProof(
                schema_version="1.0",
                request=request,
                state=self._state(meta, updated, activation, producer),
                target_fenced=True,
            ).validate_request(request)
