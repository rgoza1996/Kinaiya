"""Physical Resources testimony, separate from the native metadata barrier.

A coverage row is created in the binding transaction, before Session.bind. A
standing capture then records pending effects before either synchronous effect
latch. Acquisition is a candidate until the capture's original owners settle;
restoration is a candidate until its original lifecycle task and late children
settle. Only then can one immutable complete report be published to Session.

The protected side table has one row per bound claim, never backfills old
bindings and never evicts history. Policy snapshots and this ledger are separate
durable writes: an interrupted gap remains pending, not proof of no acquisition.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from packages.contracts.operation_ownership import ActualOperation, actual_owner, current_owner
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingRequest,
    StandingResourceFinalization,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeRequest,
    checked_finalization,
    finalization_bytes,
    resource_finalization_digest,
)
from packages.contracts.standing_resources import StandingResourceReservation
from packages.contracts.standing_work import StandingConflict, StandingUnavailable

_MAX_CLAIMS = 10000
_MAX_REVISION = (1 << 53) - 1
_TABLE = "standing_resource_witnesses"
_SQL = f"CREATE TABLE {_TABLE}(claim_id TEXT PRIMARY KEY,binding TEXT NOT NULL,action_key TEXT,reservation TEXT,lease_id TEXT,phase TEXT NOT NULL,acquired INTEGER NOT NULL,effects INTEGER NOT NULL,restoration TEXT NOT NULL,policy_revision INTEGER,owner_intent_revision INTEGER,report TEXT)"
_COLUMNS = (
    ("claim_id", "TEXT", 0, None, 1, 0),
    ("binding", "TEXT", 1, None, 0, 0),
    ("action_key", "TEXT", 0, None, 0, 0),
    ("reservation", "TEXT", 0, None, 0, 0),
    ("lease_id", "TEXT", 0, None, 0, 0),
    ("phase", "TEXT", 1, None, 0, 0),
    ("acquired", "INTEGER", 1, None, 0, 0),
    ("effects", "INTEGER", 1, None, 0, 0),
    ("restoration", "TEXT", 1, None, 0, 0),
    ("policy_revision", "INTEGER", 0, None, 0, 0),
    ("owner_intent_revision", "INTEGER", 0, None, 0, 0),
    ("report", "TEXT", 0, None, 0, 0),
)


def _wire(value):
    return finalization_bytes(value).decode("utf-8")


@dataclass(eq=False)
class _Lifecycle:
    key: str
    scope: ActualOperation
    candidate: tuple | None = None
    observer: asyncio.Task | None = None


class StandingFinalization:
    def __init__(self, owner):
        self.owner = owner
        self.barrier = owner.standing_barrier
        self.gate = owner.standing_handoff
        self.ready = self.failed = False
        self.instance = owner.controller_id
        # Retain exact original collaborators, not parsed/validated authority
        # results. Every use still revalidates protected rows and full graphs.
        self.authorities = {}
        self.observers = set()
        self.pending = {}
        self.captures = {}
        self.lifecycles = {}
        self.active_key = None

    def _guard(self):
        if (
            not self.ready
            or self.failed
            or self.gate.db is None
            or not self.gate.ready
            or self.gate.failed
            or self.owner.controller_id != self.instance
            or self.owner.standing_handoff is not self.gate
            or self.owner.standing_barrier is not self.barrier
        ):
            raise StandingUnavailable

    def _schema(self):
        self.barrier._schema()
        db = self.gate.db
        schema = db.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (_TABLE,)).fetchone()
        columns = tuple(
            (r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"], r["hidden"])
            for r in db.execute(f"PRAGMA table_xinfo({_TABLE})")
        )
        if (
            schema is None
            or schema[0] != "table"
            or type(schema[1]) is not str
            or re.sub(r"\s+", "", schema[1]).lower() != re.sub(r"\s+", "", _SQL).lower()
            or columns != _COLUMNS
            or list(db.execute(f"PRAGMA foreign_key_list({_TABLE})"))
            or db.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (_TABLE,)
            ).fetchone()
        ):
            raise StandingUnavailable
        indexes = list(db.execute(f"PRAGMA index_list({_TABLE})"))
        if len(indexes) != 1:
            raise StandingUnavailable
        index = indexes[0]
        name = index["name"].replace('"', '""')
        if (
            index["unique"] != 1
            or index["partial"] != 0
            or index["origin"] != "pk"
            or tuple(r["name"] for r in db.execute(f'PRAGMA index_info("{name}")')) != ("claim_id",)
        ):
            raise StandingUnavailable
        count = db.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0]
        if type(count) is not int or not 0 <= count <= _MAX_CLAIMS:
            raise StandingUnavailable
        bounds = {
            "claim_id": (64, 64),
            "binding": (1, StandingResourceBindingRequest.wire_bytes),
            "action_key": (1, 1024),
            "reservation": (1, StandingResourceReservation.wire_bytes),
            "lease_id": (36, 36),
            "phase": (1, 9),
            "restoration": (1, 12),
            "report": (1, StandingResourceFinalization.wire_bytes),
        }
        invalid = []
        for name, (minimum, maximum) in bounds.items():
            clause = f"(typeof({name})!='text' OR length(CAST({name} AS BLOB)) NOT BETWEEN {minimum} AND {maximum})"
            if name in {"action_key", "reservation", "lease_id", "report"}:
                clause = f"({name} IS NOT NULL AND {clause})"
            invalid.append(clause)
        for name in ("acquired", "effects"):
            invalid.append(f"(typeof({name})!='integer' OR {name} NOT IN (0,1))")
        for name in ("policy_revision", "owner_intent_revision"):
            invalid.append(
                f"({name} IS NOT NULL AND (typeof({name})!='integer' OR {name} NOT BETWEEN 0 AND {_MAX_REVISION}))"
            )
        if db.execute(f"SELECT 1 FROM {_TABLE} WHERE {' OR '.join(invalid)} LIMIT 1").fetchone():
            raise StandingUnavailable
        for row in db.execute(f"SELECT * FROM {_TABLE} LIMIT {_MAX_CLAIMS}"):
            self._checked(row)

    def _checked(self, row):
        binding = StandingResourceBindingRequest.from_json(row["binding"].encode())
        key = binding.claim.claim_id
        if (
            row["claim_id"] != key
            or _wire(binding) != row["binding"]
            or binding.resource_backend != self.gate.backend
            or row["phase"]
            not in {
                "covered",
                "pending",
                "allocated",
                "acquired",
                "restoring",
                "never",
                "complete",
                "failed",
            }
            or row["restoration"]
            not in {"not-required", "pending", "restored", "owner-off", "uncertain"}
        ):
            raise StandingUnavailable
        native = self.gate.db.execute(
            "SELECT * FROM standing_resource_claims WHERE claim_id=?", (key,)
        ).fetchone()
        if native is None or native["binding"] != row["binding"]:
            raise StandingUnavailable
        self.barrier._checked_row(native)
        reservation = report = None
        if row["reservation"] is not None:
            reservation = checked_finalization(
                StandingResourceReservation.from_json(row["reservation"].encode()),
                StandingResourceReservation,
            )
            if (
                _wire(reservation) != row["reservation"]
                or reservation.context.family != "standing"
                or _wire(reservation.context.standing.claim) != _wire(binding.claim)
                or reservation.resource_backend != binding.resource_backend
                or reservation.resource_instance != binding.resource_instance
                or reservation.lease_id != row["lease_id"]
                or type(row["action_key"]) is not str
                or not 1 <= len(row["action_key"]) <= 256
            ):
                raise StandingUnavailable
            intent = self.gate.db.execute(
                "SELECT * FROM standing_handoff_intents WHERE action_key=?", (row["action_key"],)
            ).fetchone()
            if (
                intent is None
                or intent["request"] != reservation.canonical_bytes().decode()
                or intent["action_digest"] != reservation.action_digest
                or intent["receipt"] is None
            ):
                raise StandingUnavailable
        elif any(row[name] is not None for name in ("lease_id", "action_key")):
            raise StandingUnavailable
        if row["phase"] == "covered":
            if (
                reservation is not None
                or row["acquired"]
                or row["effects"]
                or row["restoration"] != "not-required"
            ):
                raise StandingUnavailable
        elif reservation is None:
            raise StandingUnavailable
        if row["acquired"] and not row["effects"]:
            raise StandingUnavailable
        if row["phase"] in {"allocated", "acquired", "complete"} and not row["acquired"]:
            raise StandingUnavailable
        if row["phase"] == "never" and (
            row["acquired"]
            or row["restoration"]
            not in ({"restored", "owner-off"} if row["effects"] else {"not-required"})
        ):
            raise StandingUnavailable
        if row["phase"] == "complete" and row["restoration"] not in {"restored", "owner-off"}:
            raise StandingUnavailable
        if (row["policy_revision"] is None) != (row["owner_intent_revision"] is None):
            raise StandingUnavailable
        if row["report"] is not None:
            report = StandingResourceFinalization.from_json(row["report"].encode())
            if (
                _wire(report) != row["report"]
                or _wire(report.context.binding) != row["binding"]
                or row["phase"] not in {"covered", "never", "complete"}
                or report.policy_revision != row["policy_revision"]
                or report.owner_intent_revision != row["owner_intent_revision"]
                or report.restoration != row["restoration"]
                or report.handoff_action_key != row["action_key"]
                or (reservation is None) != (report.context.observation.reservation is None)
                or reservation is not None
                and _wire(report.context.observation.reservation) != _wire(reservation)
                or report.resource_disposition
                != (
                    "no-reservation"
                    if reservation is None
                    else "matching-lease-finalized"
                    if row["acquired"]
                    else "registered-no-lease"
                )
            ):
                raise StandingUnavailable
        return binding, reservation, report

    async def register(self):
        def invoke():
            with self.gate._transaction():
                self.barrier._schema()
                if (
                    self.gate.db.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name=?", (_TABLE,)
                    ).fetchone()
                    is None
                ):
                    self.gate.db.execute(_SQL)
                self._schema()
            self.ready = True

        try:
            await self.gate._database(invoke)
        except BaseException:
            self.failed = True
            raise StandingUnavailable from None

    def _authority(self):
        owner = self.owner
        return (
            owner.manager,
            owner.converse,
            owner.handoff_journal,
            owner._text_lifecycle,
            owner._whistle_lifecycle,
            *(owner._standing_composition or (None, None, None)),
        )

    def _current(self, key):
        self._guard()
        previous = self.authorities.get(key)
        if previous is None or any(
            now is not old for now, old in zip(self._authority(), previous, strict=True)
        ):
            raise StandingUnavailable

    def cover(self, binding):
        """Called inside the genuine new binding's pending transaction only."""
        self._guard()
        self._schema()
        if self.gate.db.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0] >= _MAX_CLAIMS:
            raise StandingUnavailable
        self.gate.db.execute(
            f"INSERT INTO {_TABLE} VALUES(?,?,NULL,NULL,NULL,'covered',0,0,'not-required',NULL,NULL,NULL)",
            (binding.claim.claim_id, _wire(binding)),
        )
        self.authorities[binding.claim.claim_id] = self._authority()

    def _read(self, key):
        self._guard()
        with self.gate._transaction(self._guard):
            self._schema()
            row = self.gate.db.execute(
                f"SELECT * FROM {_TABLE} WHERE claim_id=?", (key,)
            ).fetchone()
            if row is None:
                raise StandingUnavailable
            return row

    def _change(self, key, change):
        self._guard()
        with self.gate._transaction(self._guard):
            self._schema()
            row = self.gate.db.execute(
                f"SELECT * FROM {_TABLE} WHERE claim_id=?", (key,)
            ).fetchone()
            if row is None:
                raise StandingUnavailable
            change(row)
            self._schema()

    async def coverage(self, binding):
        self._current(binding.claim.claim_id)
        row = await self.gate._database(lambda: self._read(binding.claim.claim_id))
        if row["binding"] != _wire(binding):
            raise StandingUnavailable

    def _observe(self, coroutine, key):
        # Observers join original scopes; enrolling them in that scope would
        # create a self-join. They remain retained here and by shutdown instead.
        with actual_owner(None):
            task = asyncio.create_task(coroutine)
        self.observers.add(task)
        self.pending.setdefault(key, set()).add(task)

        def settled(done):
            self.observers.discard(done)
            self.pending[key].discard(done)
            if not self.pending[key]:
                self.pending.pop(key)
            if done.cancelled() or done.exception() is not None:
                self.failed = True

        task.add_done_callback(settled)
        return task

    def permit(self, capture):
        if capture.context is None or capture.context.family != "standing":
            return
        if getattr(capture, "witness_legacy", False):
            return
        key = capture.context.standing.claim.claim_id
        self._current(key)
        if self.captures.get(key) is not capture:
            raise StandingUnavailable

    async def begin_capture(self, capture):
        if capture.context.family != "standing":
            return
        key = capture.context.standing.claim.claim_id

        def local_binding():
            with self.gate._transaction(capture.guard):
                self.barrier._schema()
                return self.gate.db.execute(
                    "SELECT 1 FROM standing_resource_claims WHERE claim_id=?", (key,)
                ).fetchone()

        bound = await self.gate._database(local_binding, capture)
        capture.guard()
        if bound is None:
            # Compatibility is only for already-admitted legacy work. It adds
            # no coverage and cannot adopt a known native binding whose local
            # history has gone missing. Existing context/reservation guards and
            # the Agent's mandatory new-first-launch binding remain unchanged.
            binding = StandingResourceBindingRequest(
                schema_version="1.0",
                claim=capture.context.standing.claim,
                resource_backend=self.gate.backend,
                resource_instance=capture.instance,
            )
            known = await capture.gateway.standing_resource_binding_lookup(binding)
            capture.guard()
            if known is not None:
                raise StandingUnavailable
            capture.witness_legacy = True
            return
        self._current(key)

        def pending(row):
            binding, _, _ = self._checked(row)
            if (
                binding.resource_instance != capture.instance
                or _wire(binding.claim) != _wire(capture.context.standing.claim)
                or row["phase"] != "covered"
                or row["report"] is not None
            ):
                raise StandingUnavailable
            self.gate.db.execute(
                f"UPDATE {_TABLE} SET action_key=?,reservation=?,lease_id=?,phase='pending',restoration='pending' WHERE claim_id=?",
                (
                    capture.payload["idempotency_key"],
                    _wire(capture.request),
                    capture.request.lease_id,
                    key,
                ),
            )

        await self.gate._database(lambda: self._change(key, pending), capture)
        capture.guard()
        capture.witness_complete = False
        capture.ownership.physical_claim = key
        self.active_key = key
        self.captures[key] = capture
        capture.witness_observer = self._observe(self._capture_settled(key, capture), key)

    async def acquired(self, capture):
        if (
            capture is None
            or capture.context.family != "standing"
            or getattr(capture, "witness_legacy", False)
        ):
            return
        key = capture.context.standing.claim.claim_id
        self._current(key)

        def acquired(row):
            if (
                row["phase"] != "pending"
                or row["reservation"] != _wire(capture.request)
                or not capture.effects_started
                or self.owner.state.record.phase != "local_work"
                or self.owner.state.record.lease_id != capture.request.lease_id
                or self.owner.state.record.lease_task_id != capture.payload["task_id"]
            ):
                raise StandingUnavailable
            self.gate.db.execute(
                f"UPDATE {_TABLE} SET phase='allocated',acquired=1,effects=1 WHERE claim_id=?",
                (key,),
            )

        await self.gate._database(lambda: self._change(key, acquired), capture)
        capture.guard()

    async def _capture_settled(self, key, capture):
        await capture.ownership.join()
        life = self.lifecycles.get(key)
        if life is not None and life.observer is not None:
            await asyncio.shield(life.observer)

        def finish(row):
            if capture.ownership.failed or not capture.witness_complete:
                phase, restoration = "failed", "uncertain"
            elif row["phase"] in {"never", "complete"}:
                return
            elif row["acquired"]:
                self.gate.db.execute(
                    f"UPDATE {_TABLE} SET phase='acquired' WHERE claim_id=?", (key,)
                )
                return
            elif capture.effects_started:
                phase, restoration = "failed", "uncertain"
            else:
                phase, restoration = "never", "not-required"
            self.gate.db.execute(
                f"UPDATE {_TABLE} SET phase=?,effects=?,restoration=? WHERE claim_id=?",
                (phase, int(capture.effects_started), restoration, key),
            )

        await self.gate._database(lambda: self._change(key, finish))
        if self.captures.get(key) is capture:
            self.captures.pop(key)

    async def _lifecycle_key(self):
        scope = current_owner()
        key = getattr(scope, "physical_claim", None)
        if key is not None:
            return key
        lease = self.owner.state.record.lease_id
        if lease is None:
            # An unrelated ordinary capture must not inherit the last standing
            # lease merely because its own allocation has not happened yet.
            return None if getattr(scope, "hub", None) is self.barrier else self.active_key

        def find():
            self._guard()
            with self.gate._transaction(self._guard):
                self._schema()
                rows = list(
                    self.gate.db.execute(
                        f"SELECT claim_id FROM {_TABLE} WHERE lease_id=? LIMIT 2", (lease,)
                    )
                )
                if len(rows) > 1:
                    raise StandingUnavailable
                return None if not rows else rows[0][0]

        return await self.gate._database(find)

    async def lifecycle(self, invoke):
        """Enroll existing restore/Off without using a standing admission slot.

        This is ownership of the already-authorized ordinary lifecycle, not a
        second admission pool. Its original task runs while the caller retains
        the lifecycle lock, even if that caller is cancelled. Late original
        cleanup remains with the same per-claim owner until it really settles.
        """
        if not self.ready or self.failed:
            return await invoke()
        parent = current_owner()
        if getattr(parent, "physical_lifecycle", False):
            return await invoke()
        try:
            key = await self._lifecycle_key()
            if key is None:
                return await invoke()
            row = await self.gate._database(lambda: self._read(key))
            binding, _, report = self._checked(row)
            self._current(key)
            if binding.resource_instance != self.owner.controller_id or report is not None:
                return await invoke()
        except Exception:
            # An unavailable testimony owner never disables independent Off.
            self.failed = True
            return await invoke()
        life = self.lifecycles.get(key)
        if life is None or life.scope.finished:
            scope = ActualOperation(lambda _: None, task_id=binding.claim.request.task.task_id)
            scope.physical_claim = key
            scope.physical_lifecycle = True
            life = _Lifecycle(key, scope)
            self.lifecycles[key] = life
            self.owner.manager.retain_operations(scope)
        scope = life.scope

        def pending(row):
            if row["phase"] == "failed" or row["report"] is not None:
                raise StandingUnavailable
            self.gate.db.execute(
                f"UPDATE {_TABLE} SET phase='restoring',restoration='pending' WHERE claim_id=?",
                (key,),
            )

        try:
            await self.gate._database(lambda: self._change(key, pending))
        except Exception:
            self.failed = True
            return await invoke()
        root = object()
        scope.hold(root)
        if parent is not None and parent is not scope:
            parent.hold(scope)
            scope.on_retire(lambda: parent.release(scope, failed=scope.failed))

        async def work():
            with actual_owner(scope):
                result = await invoke()
                try:
                    self._current(key)
                    state, choice = self.owner.state.record, self.owner.intent.read()
                    if state.phase == "ready" and choice.enabled:
                        disposition = "restored"
                    elif state.phase == "off" and not choice.enabled:
                        disposition = "owner-off"
                    else:
                        raise StandingUnavailable
                    life.candidate = (disposition, state.revision, choice.revision)
                except Exception:
                    # Refuse testimony without changing the result of an
                    # independently authorized, successfully completed Off.
                    scope.fail()
                return result

        task = asyncio.create_task(work())
        scope.retain_task(task, cleanup=True)
        scope.release(root)
        if life.observer is None or life.observer.done():
            life.observer = self._observe(self._lifecycle_settled(life), key)
        interrupted = False
        while True:
            try:
                result = await asyncio.shield(task)
                if interrupted:
                    raise asyncio.CancelledError
                return result
            except asyncio.CancelledError:
                if task.done():
                    raise
                interrupted = True

    async def _lifecycle_settled(self, life):
        await life.scope.join()

        def finish(row):
            candidate = life.candidate
            if life.scope.failed or candidate is None or row["phase"] == "failed":
                self.gate.db.execute(
                    f"UPDATE {_TABLE} SET phase='failed',restoration='uncertain' WHERE claim_id=?",
                    (life.key,),
                )
                return
            restoration, policy, intent = candidate
            self._revisions(policy, intent)
            capture = self.captures.get(life.key)
            effects = bool(row["effects"] or capture is not None and capture.effects_started)
            if not effects:
                # An explicit Off may still have performed real work even if
                # the handoff's own effect latch was never crossed.
                effects = True
            self.gate.db.execute(
                f"UPDATE {_TABLE} SET phase=?,effects=?,restoration=?,policy_revision=?,owner_intent_revision=? WHERE claim_id=?",
                (
                    "complete" if row["acquired"] else "never",
                    int(effects),
                    restoration,
                    policy,
                    intent,
                    life.key,
                ),
            )

        await self.gate._database(lambda: self._change(life.key, finish))
        if self.lifecycles.get(life.key) is life:
            self.lifecycles.pop(life.key)

    @staticmethod
    def _revisions(policy, intent):
        if any(
            type(value) is not int or not 0 <= value <= _MAX_REVISION for value in (policy, intent)
        ):
            raise StandingUnavailable

    async def _join(self, key):
        while self.pending.get(key):
            tasks = tuple(self.pending[key])
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        self._current(key)

    async def _publish(self, call, request, report):
        retained = await call.gateway.standing_resource_finalization_lookup(request)
        call.guard()
        if retained is None:
            retained = await call.gateway.standing_resource_report(report)
            call.guard()
        if type(retained) is not StandingResourceFinalizationReceipt:
            raise StandingUnavailable
        retained = retained.validate_request(request)
        if _wire(retained.finalization) != _wire(report):
            raise StandingConflict
        return retained

    async def run(self, call, request, *, lookup):
        if type(request) is not StandingResourceFinalizeRequest:
            raise StandingUnavailable
        request = checked_finalization(request, StandingResourceFinalizeRequest)
        if lookup:
            result = await call.gateway.standing_resource_finalization_lookup(request)
            call.guard()
            if result is None:
                return None
            if type(result) is not StandingResourceFinalizationReceipt:
                raise StandingUnavailable
            return result.validate_request(request)
        self._guard()
        call.guard()
        if (
            request.expected_resource_backend != self.gate.backend
            or request.expected_resource_instance != self.owner.controller_id
        ):
            raise StandingUnavailable
        from packages.contracts.standing_execution import claim_id

        key = claim_id(request.original)
        self._current(key)
        row = await self.gate._database(lambda: self._read(key))
        binding, _, report = self._checked(row)
        self.barrier._join_request(binding, request)
        if report is not None:
            report.validate_request(request)
            return await self._publish(call, request, report)
        context = await self.barrier._seal(call, request, lookup=False)
        call.guard()
        await self._join(key)
        call.guard()
        self.owner.require_standing_ownership()
        async with self.owner.lock:
            row = await self.gate._database(lambda: self._read(key))
            binding, reservation, report = self._checked(row)
            self.barrier._join_context(binding, context)
            if row["phase"] == "failed":
                raise StandingUnavailable
            if reservation is None:
                if row["phase"] != "covered" or context.observation.reservation is not None:
                    raise StandingUnavailable
            elif _wire(reservation) != _wire(context.observation.reservation):
                raise StandingUnavailable
            elif row["phase"] == "acquired":
                state = self.owner.state.record
                if (
                    state.phase != "local_work"
                    or state.lease_id != reservation.lease_id
                    or state.lease_task_id != binding.claim.request.task.task_id
                ):
                    raise StandingUnavailable
                self.active_key = key
                await self.owner._restore_locked()
            elif row["phase"] not in {"never", "complete"}:
                raise StandingUnavailable
        await self._join(key)
        call.guard()
        fresh = await call.gateway.standing_resource_finalize_context(request)
        call.guard()
        if _wire(fresh) != _wire(context):
            raise StandingConflict
        async with self.owner.lock:
            state, choice = self.owner.state.record, self.owner.intent.read()
            self._revisions(state.revision, choice.revision)
            self._current(key)
            row = await self.gate._database(lambda: self._read(key))
            binding, reservation, report = self._checked(row)
            if report is None:
                if row["phase"] not in {"covered", "never", "complete"}:
                    raise StandingUnavailable
                if state.lease_id is not None:
                    raise StandingUnavailable
                if (
                    reservation is not None
                    and row["restoration"] != "not-required"
                    and (
                        row["restoration"] != ("restored" if choice.enabled else "owner-off")
                        or state.phase != ("ready" if choice.enabled else "off")
                    )
                ):
                    raise StandingUnavailable
                fields = {
                    "context": context,
                    "resource_disposition": "no-reservation"
                    if reservation is None
                    else "matching-lease-finalized"
                    if row["acquired"]
                    else "registered-no-lease",
                    "handoff_action_key": row["action_key"],
                    "handoff_action_digest": None
                    if reservation is None
                    else reservation.action_digest,
                    "reserved_lease_id": row["lease_id"],
                    "policy_revision": state.revision,
                    "owner_intent_revision": choice.revision,
                    "restoration": row["restoration"],
                }
                report = StandingResourceFinalization(
                    schema_version="1.0",
                    finalization_digest=resource_finalization_digest(**fields),
                    **fields,
                ).validate_request(request)

                def save(row):
                    call.guard()
                    self._current(key)
                    if (
                        row["report"] is not None
                        or self.owner.state.record is not state
                        or self.owner.intent.read() != choice
                    ):
                        raise StandingUnavailable
                    self.gate.db.execute(
                        f"UPDATE {_TABLE} SET report=?,policy_revision=?,owner_intent_revision=? WHERE claim_id=?",
                        (_wire(report), state.revision, choice.revision, key),
                    )

                await self.gate._database(lambda: self._change(key, save))
        call.guard()
        return await self._publish(call, request, report)

    async def join_shutdown(self):
        while self.observers:
            await asyncio.gather(*tuple(self.observers), return_exceptions=True)
