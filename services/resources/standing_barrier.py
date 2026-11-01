"""Protected prework binding and permanent claim operation fences.

This returns native context after actual-operation drainage. It does not attest
physical restoration, release a reservation, or produce a finalization report.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass

from packages.contracts.operation_ownership import ActualOperation, actual_owner, current_owner
from packages.contracts.standing_execution import StandingExecutionClaim
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceBindingRequest,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    _complete,
    _json,
    checked_finalization,
    finalization_bytes,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable

MAX_OPERATIONS = 64
MAX_CLAIMS = 10000
_TABLE = "standing_resource_claims"
_SQL = f"CREATE TABLE {_TABLE}(claim_id TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE,session_backend TEXT NOT NULL,resource_backend TEXT NOT NULL,instance_id TEXT NOT NULL,claim TEXT NOT NULL,binding TEXT NOT NULL,receipt TEXT,seal TEXT,context TEXT,phase TEXT NOT NULL)"
_COLUMNS = (
    ("claim_id", "TEXT", 0, None, 1, 0),
    ("task_id", "TEXT", 1, None, 0, 0),
    ("session_backend", "TEXT", 1, None, 0, 0),
    ("resource_backend", "TEXT", 1, None, 0, 0),
    ("instance_id", "TEXT", 1, None, 0, 0),
    ("claim", "TEXT", 1, None, 0, 0),
    ("binding", "TEXT", 1, None, 0, 0),
    ("receipt", "TEXT", 0, None, 0, 0),
    ("seal", "TEXT", 0, None, 0, 0),
    ("context", "TEXT", 0, None, 0, 0),
    ("phase", "TEXT", 1, None, 0, 0),
)


def _wire(value):
    return finalization_bytes(value).decode("utf-8")


@dataclass(eq=False)
class _Call:
    barrier: StandingBarrier
    scope: ActualOperation
    gateway: object
    instance: str
    deadline: float
    active: bool = True

    def guard(self):
        barrier, owner = self.barrier, self.barrier.owner
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if (
            not self.active
            or barrier.closed
            or barrier.failed
            or not barrier.ready
            or barrier.gate.failed
            or barrier.gate.closed
            or not barrier.gate.ready
            or owner._closing
            or owner._closed
            or owner.controller_id != self.instance
            or owner.handoff_journal is not self.gateway
            or getattr(self.scope, "cancelled", False)
            or time.monotonic() >= self.deadline
            or task is not None
            and task.cancelling()
            or getattr(self.gateway, "_submit_closed", False)
            or getattr(self.gateway, "_submit_cleanup_failed", False)
            or getattr(getattr(self.gateway, "http", None), "is_closed", False)
        ):
            raise StandingUnavailable


class StandingBarrier:
    def __init__(self, owner):
        self.owner = owner
        self.gate = owner.standing_handoff
        self.ready = self.failed = self.closed = False
        self.operations = set()
        self.calls = set()
        self.fences = set()
        self.seal_requests = {}
        self.busy = set()
        self.uncertain = False
        self.registration = None

    def _settled(self, scope):
        self.operations.discard(scope)
        if scope.failed:
            # A settled failed close/physical action never becomes positive
            # negative-acquisition testimony. No unbounded failed-owner cache.
            self.uncertain = True

    def admit(self, *, task_id=None):
        if (
            self.closed
            or self.owner._closing
            or self.owner._closed
            or len(self.operations) >= MAX_OPERATIONS
        ):
            raise StandingUnavailable
        scope = ActualOperation(self._settled, task_id=task_id)
        scope.hub = self
        scope.deadline = time.monotonic() + 25
        scope.cancelled = False
        scope.ingress = object()
        scope.hold(scope.ingress)
        self.operations.add(scope)
        return scope

    def release_ingress(self, scope):
        scope.release(scope.ingress)

    def sealed(self, task_id):
        return task_id in self.fences

    def fence(self, task_id, request):
        wire = _wire(request)
        if task_id in self.seal_requests and self.seal_requests[task_id] != wire:
            raise StandingConflict
        self.seal_requests[task_id] = wire
        self.fences.add(task_id)
        for capture in tuple(self.gate.attempts.values()):
            if capture.payload["task_id"] == task_id:
                capture.suppress()

    def _schema(self):
        gate, db = self.gate, self.gate.db
        gate._schema()
        path = self.owner.state.path.parent / "resource-actions.sqlite"
        info = path.stat(follow_symlinks=False)
        if (
            (info.st_dev, info.st_ino) != gate.file_identity
            or info.st_mode & 0o077
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise StandingUnavailable
        row = db.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (_TABLE,)).fetchone()
        columns = tuple(
            (r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"], r["hidden"])
            for r in db.execute(f"PRAGMA table_xinfo({_TABLE})")
        )
        if (
            row is None
            or row[0] != "table"
            or type(row[1]) is not str
            or re.sub(r"\s+", "", row[1]).lower() != re.sub(r"\s+", "", _SQL).lower()
            or columns != _COLUMNS
            or list(db.execute(f"PRAGMA foreign_key_list({_TABLE})"))
            or db.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (_TABLE,)
            ).fetchone()
        ):
            raise StandingUnavailable
        indexes = list(db.execute(f"PRAGMA index_list({_TABLE})"))
        indexed = set()
        for index in indexes:
            name = index["name"].replace('"', '""')
            names = tuple(r["name"] for r in db.execute(f'PRAGMA index_info("{name}")'))
            if index["unique"] != 1 or index["partial"] or index["origin"] not in {"pk", "u"}:
                raise StandingUnavailable
            indexed.add((index["origin"], names))
        if len(indexes) != 2 or indexed != {("pk", ("claim_id",)), ("u", ("task_id",))}:
            raise StandingUnavailable
        count = db.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0]
        if type(count) is not int or not 0 <= count <= MAX_CLAIMS:
            raise StandingUnavailable
        # Check scalar types and UTF-8 byte ceilings in SQLite before any full
        # packet is transferred into Python. Never prebuffer a corrupt ledger.
        bounds = {
            "claim_id": (64, 64),
            "task_id": (1, 128),
            "session_backend": (32, 32),
            "resource_backend": (32, 32),
            "instance_id": (36, 36),
            "claim": (1, 147456),
            "binding": (1, StandingResourceBindingRequest.wire_bytes),
            "receipt": (1, StandingResourceBindingReceipt.wire_bytes),
            "seal": (1, StandingResourceFinalizeRequest.wire_bytes),
            "context": (1, StandingResourceFinalizeContext.wire_bytes),
            "phase": (1, 7),
        }
        invalid = []
        for name, (minimum, maximum) in bounds.items():
            clause = f"(typeof({name})!='text' OR length(CAST({name} AS BLOB)) NOT BETWEEN {minimum} AND {maximum})"
            if name in {"receipt", "seal", "context"}:
                clause = f"({name} IS NOT NULL AND {clause})"
            invalid.append(clause)
        if db.execute(f"SELECT 1 FROM {_TABLE} WHERE {' OR '.join(invalid)} LIMIT 1").fetchone():
            raise StandingUnavailable
        for row in db.execute(f"SELECT * FROM {_TABLE} LIMIT {MAX_CLAIMS}"):
            self._checked_row(row)

    def _checked_row(self, row):
        if any(
            type(row[name]) is not str
            for name in (
                "claim_id",
                "task_id",
                "session_backend",
                "resource_backend",
                "instance_id",
                "claim",
                "binding",
                "phase",
            )
        ) or any(
            row[name] is not None and type(row[name]) is not str
            for name in ("receipt", "seal", "context")
        ):
            raise StandingUnavailable
        if len(row["claim"].encode()) > 147456:
            raise StandingUnavailable
        claim = checked_finalization(
            StandingExecutionClaim.from_json(row["claim"].encode()), StandingExecutionClaim
        )
        binding = StandingResourceBindingRequest.from_json(row["binding"].encode())
        if (
            _wire(claim) != row["claim"]
            or _wire(binding) != row["binding"]
            or _wire(binding.claim) != row["claim"]
            or claim.claim_id != row["claim_id"]
            or claim.request.task.task_id != row["task_id"]
            or claim.request.expected_backend != row["session_backend"]
            or binding.resource_backend != row["resource_backend"]
            or binding.resource_backend != self.gate.backend
            or binding.resource_instance != row["instance_id"]
            or row["phase"] not in {"pending", "bound", "sealed", "drained"}
        ):
            raise StandingUnavailable
        receipt = seal = context = None
        if row["receipt"] is not None:
            receipt = StandingResourceBindingReceipt.from_json(
                row["receipt"].encode()
            ).validate_request(binding)
            if _wire(receipt) != row["receipt"]:
                raise StandingUnavailable
        if row["seal"] is not None:
            seal = StandingResourceFinalizeRequest.from_json(row["seal"].encode())
            self._join_request(binding, seal)
            if _wire(seal) != row["seal"]:
                raise StandingUnavailable
        if row["context"] is not None:
            if seal is None:
                raise StandingUnavailable
            context = StandingResourceFinalizeContext.from_json(
                row["context"].encode()
            ).validate_request(seal)
            self._join_context(binding, context)
            if _wire(context) != row["context"]:
                raise StandingUnavailable
        if (
            (row["phase"] == "pending") != (receipt is None)
            or (row["phase"] in {"sealed", "drained"}) != (seal is not None)
            or (seal is not None) != (context is not None)
        ):
            raise StandingUnavailable
        return binding, receipt, seal, context

    @staticmethod
    def _join_request(binding, request):
        if (
            _wire(binding.claim.request) != _wire(request.original)
            or request.expected_resource_backend != binding.resource_backend
            or request.expected_resource_instance != binding.resource_instance
        ):
            raise StandingConflict

    @staticmethod
    def _join_context(binding, context):
        if _wire(context.binding) != _wire(binding):
            raise StandingUnavailable

    def _register(self):
        gate = self.gate
        with gate._transaction():
            gate._schema()
            present = gate.db.execute(
                "SELECT type FROM sqlite_schema WHERE name=?", (_TABLE,)
            ).fetchone()
            if present is None:
                gate.db.execute(_SQL)
            self._schema()
            for row in gate.db.execute(f"SELECT task_id,seal FROM {_TABLE} WHERE seal IS NOT NULL"):
                self.fences.add(row[0])
                self.seal_requests[row[0]] = row[1]
            if self.closed or self.owner._closing:
                raise StandingUnavailable
        self.ready = True

    async def register(self):
        if self.registration is None:
            self.registration = asyncio.create_task(self.gate._database(self._register))
            self.registration.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
        try:
            await asyncio.shield(self.registration)
        except BaseException:
            self.failed = True
            raise StandingUnavailable from None

    def _read(self, call, key):
        call.guard()
        with self.gate._transaction(call.guard):
            self._schema()
            return self.gate.db.execute(
                f"SELECT * FROM {_TABLE} WHERE claim_id=?", (key,)
            ).fetchone()

    def _pending(self, call, binding):
        call.guard()
        with self.gate._transaction(call.guard):
            self._schema()
            if self.gate.db.execute(f"SELECT count(*) FROM {_TABLE}").fetchone()[0] >= MAX_CLAIMS:
                raise StandingUnavailable
            if self.gate.db.execute(
                f"SELECT 1 FROM {_TABLE} WHERE claim_id=? OR task_id=?",
                (binding.claim.claim_id, binding.claim.request.task.task_id),
            ).fetchone():
                raise StandingConflict
            self.gate.db.execute(
                f"INSERT INTO {_TABLE} VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,'pending')",
                (
                    binding.claim.claim_id,
                    binding.claim.request.task.task_id,
                    binding.claim.request.expected_backend,
                    binding.resource_backend,
                    binding.resource_instance,
                    _wire(binding.claim),
                    _wire(binding),
                ),
            )

    def _record(self, call, binding, receipt):
        call.guard()
        with self.gate._transaction(call.guard):
            self._schema()
            row = self.gate.db.execute(
                f"SELECT * FROM {_TABLE} WHERE claim_id=?", (binding.claim.claim_id,)
            ).fetchone()
            if (
                row is None
                or row["binding"] != _wire(binding)
                or row["phase"] != "pending"
                or row["receipt"] is not None
            ):
                raise StandingUnavailable
            self.gate.db.execute(
                f"UPDATE {_TABLE} SET receipt=?,phase='bound' WHERE claim_id=?",
                (_wire(receipt), binding.claim.claim_id),
            )

    def _seal_record(self, call, binding, request, context, *, drained=False):
        call.guard()
        with self.gate._transaction(call.guard):
            self._schema()
            row = self.gate.db.execute(
                f"SELECT * FROM {_TABLE} WHERE claim_id=?", (binding.claim.claim_id,)
            ).fetchone()
            if row is None or row["binding"] != _wire(binding) or row["receipt"] is None:
                raise StandingUnavailable
            if row["seal"] is not None and row["seal"] != _wire(request):
                raise StandingConflict
            if row["context"] is not None and row["context"] != _wire(context):
                raise StandingConflict
            self.gate.db.execute(
                f"UPDATE {_TABLE} SET seal=?,context=?,phase=? WHERE claim_id=?",
                (
                    _wire(request),
                    _wire(context),
                    "drained" if drained else "sealed",
                    binding.claim.claim_id,
                ),
            )

    def _gateway(self):
        gateway = self.owner.handoff_journal
        if (
            getattr(gateway, "actual_ownership_supported", False) is not True
            or getattr(gateway, "_submit_closed", False)
            or getattr(gateway, "_submit_cleanup_failed", False)
            or getattr(getattr(gateway, "http", None), "is_closed", False)
        ):
            raise StandingUnavailable
        return gateway

    async def _binding(self, call, claim, *, lookup):
        key = claim.claim_id
        row = await self.gate._database(lambda: self._read(call, key))
        call.guard()
        if row is not None:
            binding, receipt, _, _ = self._checked_row(row)
            if _wire(binding.claim) != _wire(claim):
                raise StandingConflict
            if receipt is not None:
                if not lookup and (
                    binding.resource_instance != call.instance
                    or self.sealed(claim.request.task.task_id)
                    or row["phase"] in {"sealed", "drained"}
                ):
                    raise StandingUnavailable
                if not lookup:
                    self.owner.require_standing_ownership()
                return receipt.validate_claim(claim)
            # Only the original controller may reconcile an uncertain bind.
            if binding.resource_instance != call.instance or not lookup:
                raise StandingUnavailable
            receipt = await call.gateway.standing_resource_binding_lookup(binding)
            call.guard()
            if type(receipt) is not StandingResourceBindingReceipt:
                raise StandingUnavailable
            receipt = receipt.validate_request(binding)
            await self.gate._database(lambda: self._record(call, binding, receipt))
            call.guard()
            return receipt
        if lookup:
            return None
        if self.sealed(claim.request.task.task_id):
            raise StandingUnavailable
        self.owner.require_standing_ownership()
        binding = StandingResourceBindingRequest(
            schema_version="1.0",
            claim=claim,
            resource_backend=self.gate.backend,
            resource_instance=call.instance,
        )
        # This existing Resources-role read verifies the full genuine native
        # prepared claim even when no binding exists. It requires no Agent
        # credential/capability and performs no native binding write.
        observed = await call.gateway.standing_resource_binding_lookup(binding)
        call.guard()
        if observed is not None:
            if type(observed) is not StandingResourceBindingReceipt:
                raise StandingUnavailable
            observed.validate_request(binding)
            # Missing local pending history cannot adopt a native binding.
            raise StandingUnavailable
        await self.gate._database(lambda: self._pending(call, binding))
        call.guard()
        receipt = await call.gateway.standing_resource_bind(binding)
        call.guard()
        if type(receipt) is not StandingResourceBindingReceipt:
            raise StandingUnavailable
        receipt = receipt.validate_request(binding)
        await self.gate._database(lambda: self._record(call, binding, receipt))
        call.guard()
        return receipt

    async def _join_claim(self, call, task_id):
        while True:
            relevant = [
                scope
                for scope in self.operations
                if scope is not call.scope and scope.task_id == task_id
            ]
            if not relevant:
                break
            await asyncio.gather(*(scope.join() for scope in relevant))
            call.guard()
        if (
            self.uncertain
            or call.scope.failed
            or getattr(call.gateway, "_submit_cleanup_failed", False)
        ):
            raise StandingUnavailable

    async def _seal(self, call, request, *, lookup):
        from packages.contracts.standing_execution import claim_id

        key = claim_id(request.original)
        row = await self.gate._database(lambda: self._read(call, key))
        call.guard()
        if row is None:
            if lookup:
                return None
            raise StandingConflict
        binding, receipt, seal, context = self._checked_row(row)
        self._join_request(binding, request)
        if row["task_id"] in self.seal_requests and self.seal_requests[row["task_id"]] != _wire(
            request
        ):
            raise StandingConflict
        if receipt is None:
            raise StandingUnavailable
        if seal is not None and _wire(seal) != _wire(request):
            raise StandingConflict
        if row["phase"] == "drained":
            return context.validate_request(request)
        if binding.resource_instance != call.instance:
            raise StandingUnavailable
        if lookup and seal is None and not self.sealed(row["task_id"]):
            return None
        # A real native context proves permanent admission closure. It is not
        # an action permit or physical certificate. No coordinator lock here.
        context = await call.gateway.standing_resource_finalize_context(request)
        call.guard()
        if type(context) is not StandingResourceFinalizeContext:
            raise StandingUnavailable
        context = context.validate_request(request)
        self._join_context(binding, context)
        self.fence(row["task_id"], request)
        try:
            await self.gate._database(lambda: self._seal_record(call, binding, request, context))
        except BaseException:
            self.failed = True
            raise
        call.guard()
        self.owner.require_standing_ownership()
        await self._join_claim(call, row["task_id"])
        call.guard()
        fresh = await call.gateway.standing_resource_finalize_context(request)
        call.guard()
        if type(fresh) is not StandingResourceFinalizeContext:
            raise StandingUnavailable
        fresh = fresh.validate_request(request)
        self._join_context(binding, fresh)
        if _wire(fresh) != _wire(context):
            raise StandingConflict
        # The final RPC's sole close has completed before its return. Earlier
        # rejected exchanges remain scope handles and cannot be inferred away.
        if (
            call.scope.failed
            or self.uncertain
            or getattr(call.gateway, "_submit_cleanup_failed", False)
        ):
            raise StandingUnavailable
        await self.gate._database(
            lambda: self._seal_record(call, binding, request, fresh, drained=True)
        )
        call.guard()
        return fresh

    async def _work(self, call, name, original):
        with actual_owner(call.scope):
            call.guard()
            key = (
                original.claim_id
                if type(original) is StandingExecutionClaim
                else original.original.task.task_id
            )
            if key in self.busy:
                raise StandingUnavailable
            self.busy.add(key)
            try:
                if name in {"bind", "binding_lookup"}:
                    return await self._binding(call, original, lookup=name == "binding_lookup")
                return await self._seal(call, original, lookup=name == "seal_lookup")
            finally:
                self.busy.discard(key)

    async def run(self, name, original):
        scope = current_owner()
        own_ingress = getattr(scope, "hub", None) is not self
        if own_ingress:
            scope = self.admit()
        call = None
        try:
            kind = (
                StandingExecutionClaim
                if name in {"bind", "binding_lookup"}
                else StandingResourceFinalizeRequest
            )
            original = checked_finalization(original, kind)
            if len(finalization_bytes(original)) > 147456:
                raise StandingUnavailable
            scope.task_id = (
                original.request.task.task_id
                if kind is StandingExecutionClaim
                else original.original.task.task_id
            )
            if not self.ready or self.failed or self.uncertain:
                raise StandingUnavailable
            gateway = self._gateway()
            call = _Call(self, scope, gateway, self.owner.controller_id, scope.deadline)
            self.calls.add(call)
            task = asyncio.create_task(self._work(call, name, original))
            scope.retain_task(task)
            task.add_done_callback(lambda _: self.calls.discard(call))
            done, _ = await asyncio.wait({task}, timeout=max(0, call.deadline - time.monotonic()))
            if not done or time.monotonic() >= call.deadline:
                raise StandingUnavailable
            return task.result()
        except asyncio.CancelledError:
            if call is not None:
                call.active = False
            raise
        except StandingConflict:
            raise
        except Exception:
            if call is not None:
                call.active = False
            raise StandingUnavailable from None
        finally:
            if own_ingress:
                self.release_ingress(scope)

    def shutdown(self):
        self.closed = True
        for call in tuple(self.calls):
            call.active = False
        for scope in tuple(self.operations):
            scope.cancelled = True
        for capture in tuple(self.gate.attempts.values()):
            capture.suppress()

    async def join_shutdown(self):
        # Settlement is separate from successful proof. Failed cleanup must
        # still reach the independent Manager retirement/retry path.
        while self.operations:
            await asyncio.gather(*(scope.join() for scope in tuple(self.operations)))


class StandingResourcesHTTP:
    """Ingress and facade share the coordinator's actual-operation admission."""

    def __init__(self, coordinator):
        self.owner = coordinator
        self.barrier = coordinator.standing_barrier

    @staticmethod
    def _guard(scope, deadline):
        if scope.cancelled or time.monotonic() >= deadline:
            scope.cancelled = True
            raise StandingUnavailable

    async def _execute(self, request, action, scope):
        from fastapi import HTTPException

        with actual_owner(scope):
            if request.scope.get("query_string") or any(
                name.lower() == b"x-kinaiya-standing-capability"
                for name, _ in request.scope["headers"]
            ):
                raise HTTPException(422, "Invalid standing Resources request")
            content_type = request.headers.get("content-type", "").split(";")
            if (
                content_type[0].strip().lower() != "application/json"
                or any(
                    part.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
                    for part in content_type[1:]
                )
                or request.headers.get("content-encoding", "identity").strip().lower() != "identity"
            ):
                raise HTTPException(415, "Standing Resources requires identity UTF-8 JSON")
            declared = request.headers.get("content-length")
            if declared is not None and (
                not declared.isascii()
                or not declared.isdigit()
                or len(declared) > 8
                or int(declared) > 147456
                or "transfer-encoding" in request.headers
            ):
                raise HTTPException(422, "Invalid standing Resources request")
            raw = bytearray()
            ingress_deadline = min(scope.deadline, time.monotonic() + 5)
            async for chunk in request.stream():
                self._guard(scope, ingress_deadline)
                if type(chunk) is not bytes or len(raw) + len(chunk) > 147456:
                    raise HTTPException(422, "Invalid standing Resources request")
                raw.extend(chunk)
            self._guard(scope, ingress_deadline)
            if declared is not None and int(declared) != len(raw):
                raise HTTPException(422, "Invalid standing Resources request")
            kind = (
                StandingExecutionClaim
                if action in {"bind", "binding_lookup"}
                else StandingResourceFinalizeRequest
            )
            try:
                original = _complete(_json(bytes(raw), 147456), kind)
                original = checked_finalization(original, kind)
            except Exception:
                raise HTTPException(422, "Invalid standing Resources request") from None
            scope.ingress_done.set()

            async def disconnect():
                while True:
                    message = await request.receive()
                    if message["type"] == "http.disconnect":
                        scope.cancelled = True
                        return

            monitor = asyncio.create_task(disconnect())
            scope.retain_task(monitor)
            try:
                result = await self.barrier.run(action, original)
                self._guard(scope, scope.deadline)
                if result is None:
                    if action not in {"binding_lookup", "seal_lookup"}:
                        raise StandingUnavailable
                    return b"null"
                expected = (
                    StandingResourceBindingReceipt
                    if kind is StandingExecutionClaim
                    else StandingResourceFinalizeContext
                )
                result = checked_finalization(result, expected)
                result = (
                    result.validate_claim(original)
                    if kind is StandingExecutionClaim
                    else result.validate_request(original)
                )
                wire = finalization_bytes(result)
                if len(wire) > expected.wire_bytes:
                    raise StandingUnavailable
                self._guard(scope, scope.deadline)
                return wire
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    async def run(self, request, action):
        from fastapi import HTTPException

        scope = self.barrier.admit()
        scope.ingress_done = asyncio.Event()
        task = asyncio.create_task(self._execute(request, action, scope))
        scope.retain_task(task)
        self.barrier.release_ingress(scope)
        ingress = asyncio.create_task(scope.ingress_done.wait())
        try:
            done, _ = await asyncio.wait(
                {task, ingress}, timeout=5, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise StandingUnavailable
            if not task.done():
                done, _ = await asyncio.wait(
                    {task}, timeout=max(0, scope.deadline - time.monotonic())
                )
                if not done:
                    raise StandingUnavailable
            self._guard(scope, scope.deadline)
            return task.result()
        except (StandingConflict, StandingUnavailable, HTTPException, asyncio.CancelledError):
            scope.cancelled = True
            raise
        except Exception:
            scope.cancelled = True
            raise StandingUnavailable from None
        finally:
            ingress.cancel()
            await asyncio.gather(ingress, return_exceptions=True)
