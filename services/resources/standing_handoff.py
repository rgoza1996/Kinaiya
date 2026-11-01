"""Codex-authored pre-effect registration for exact Task-bound handoffs.

The local ledger retains uncertainty; a parsed context or retained Session
receipt is never a new hardware-action permit. Nothing here finalizes a lease.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from packages.contracts.models import new_id
from packages.contracts.operation_ownership import actual_owner, current_owner, retain_task
from packages.contracts.standing_resources import (
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    TaskResourceContext,
    TaskResourceQuery,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable, _tree

MAX_HANDOFF_CAPTURES = 64
MAX_HANDOFF_INTENTS = 10000
HANDOFF_CAPTURE_SECONDS = 25.0
_META = "standing_handoff_meta"
_INTENTS = "standing_handoff_intents"
_DEFINITIONS = {
    _META: f"CREATE TABLE {_META}(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,backend_id TEXT NOT NULL,instance_id TEXT NOT NULL)",
    _INTENTS: f"CREATE TABLE {_INTENTS}(action_key TEXT PRIMARY KEY,action_digest TEXT NOT NULL,request TEXT NOT NULL,receipt TEXT)",
}
_COLUMNS = {
    _META: (
        ("id", "INTEGER", 0, None, 1, 0),
        ("version", "INTEGER", 1, None, 0, 0),
        ("backend_id", "TEXT", 1, None, 0, 0),
        ("instance_id", "TEXT", 1, None, 0, 0),
    ),
    _INTENTS: (
        ("action_key", "TEXT", 0, None, 1, 0),
        ("action_digest", "TEXT", 1, None, 0, 0),
        ("request", "TEXT", 1, None, 0, 0),
        ("receipt", "TEXT", 0, None, 0, 0),
    ),
}


def action_digest(payload):
    # Preserve the existing Resources digest, including omissions and its
    # historical ensure_ascii default. Never add private capture fields.
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _freeze(payload):
    from services.resources.api import ResourceAction

    try:
        if type(payload) is not dict:
            raise ValueError
        _tree(payload)
        body = ResourceAction.model_validate(payload, strict=True)
        if body.operation != "handoff" or body.task_id is None:
            raise ValueError
        detached = body.model_dump(exclude_unset=True)
        if detached.keys() != payload.keys() or action_digest(detached) != action_digest(payload):
            raise ValueError
        return copy.deepcopy(detached)
    except Exception:
        raise StandingUnavailable from None


class _CaptureSuppressed(StandingUnavailable):
    """A trusted fence, facade, or shutdown stopped the next capture phase."""


@dataclass(eq=False)
class _Capture:
    gate: StandingHandoff
    payload: dict
    digest: str
    deadline: float
    instance: str
    started: asyncio.Future = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    task: asyncio.Task | None = None
    active: bool = True
    effects_started: bool = False
    context: TaskResourceContext | None = None
    request: StandingResourceReservation | None = None
    receipt: StandingResourceReservationReceipt | None = None
    gateway: object = None
    ownership: object = None
    suppression: str | None = None

    def suppress(self, *, phase=None):
        if phase is not None:
            if phase not in {"fence", "facade", "shutdown"}:
                raise ValueError("Invalid capture suppression phase")
            self.suppression = phase
        self.active = False

    def guard(self):
        gate, owner = self.gate, self.gate.owner
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None  # Native database work runs in its retained thread.
        # Authority/integrity failures remain uncertain even when a deliberate
        # phase stop also occurred. Only the separate trusted stop below may
        # avoid adding uncertainty; original child/close failures stay sticky.
        if (
            gate.failed
            or not gate.ready
            or not owner.standing_barrier.ready
            or owner.standing_barrier.failed
            or owner.controller_id != self.instance
            or gate.attempts.get(self.payload["idempotency_key"]) is not self
            or self.task is None
            or self.task.cancelling()
            or current is not None
            and current is not self.task
            or self.gateway is not None
            and owner.handoff_journal is not self.gateway
            or action_digest(self.payload) != self.digest
        ):
            self.suppress()
            raise StandingUnavailable
        if (
            not self.active
            or gate.closed
            or owner._closing
            or owner._closed
            or owner.standing_barrier.sealed(self.payload["task_id"])
            or not self.effects_started
            and time.monotonic() >= self.deadline
        ):
            self.suppress()
            if self.suppression is not None:
                raise _CaptureSuppressed
            raise StandingUnavailable

    def begin_effects(self):
        self.guard()
        if self.context is None or (
            self.context.family == "standing"
            and (
                self.request is None
                or self.receipt is None
                or self.receipt.disposition != "created"
            )
        ):
            raise StandingUnavailable
        if not self.effects_started:
            self.effects_started = True
            self.started.set_result(None)


class StandingHandoff:
    """Bound actual capture owners and a protected same-database pending ledger."""

    def __init__(self, owner):
        self.owner = owner
        self.ready = self.failed = self.closed = False
        self.backend = None
        self.retained = False
        self.db = None
        self.lock = threading.RLock()
        self.attempts = {}
        self.operations = set()
        self.native = set()
        self.registration = None
        self.file_identity = None

    @contextmanager
    def _transaction(self, guard=None):
        with self.lock:
            if self.db is None or self.db.in_transaction:
                raise StandingUnavailable
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield
                if guard is not None:
                    guard()
                self.db.commit()
            except BaseException:
                try:
                    self.db.rollback()
                except BaseException:
                    self.failed = True
                    self.db.close()
                    self.db = None
                    raise StandingUnavailable from None
                raise

    def _schema(self, *, current=True):
        db = self.db
        for name, columns in _COLUMNS.items():
            row = db.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()
            actual = tuple(
                (r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"], r["hidden"])
                for r in db.execute(f"PRAGMA table_xinfo({name})")
            )
            if (
                row is None
                or row[0] != "table"
                or type(row[1]) is not str
                or re.sub(r"\s+", "", row[1]).lower()
                != re.sub(r"\s+", "", _DEFINITIONS[name]).lower()
                or actual != columns
                or list(db.execute(f"PRAGMA foreign_key_list({name})"))
                or db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (name,)
                ).fetchone()
            ):
                raise StandingUnavailable
            indexes = list(db.execute(f"PRAGMA index_list({name})"))
            if name == _META:
                if indexes:
                    raise StandingUnavailable
            else:
                if (
                    len(indexes) != 1
                    or indexes[0]["unique"] != 1
                    or indexes[0]["partial"] != 0
                    or indexes[0]["origin"] != "pk"
                ):
                    raise StandingUnavailable
                index = indexes[0]["name"].replace('"', '""')
                if tuple(r["name"] for r in db.execute(f'PRAGMA index_info("{index}")')) != (
                    "action_key",
                ):
                    raise StandingUnavailable
        if (
            db.execute(f"SELECT count(*) FROM {_META}").fetchone()[0] != 1
            or db.execute(
                f"SELECT 1 FROM {_META} WHERE typeof(id)!='integer' OR id!=1 OR typeof(version)!='integer' OR version!=1 "
                "OR typeof(backend_id)!='text' OR length(CAST(backend_id AS BLOB))!=32 OR typeof(instance_id)!='text' "
                "OR length(CAST(instance_id AS BLOB))!=36 LIMIT 1"
            ).fetchone()
        ):
            raise StandingUnavailable
        meta = db.execute(f"SELECT backend_id,instance_id FROM {_META}").fetchone()
        if re.fullmatch(r"[a-f0-9]{32}", meta[0]) is None or (
            self.backend is not None and meta[0] != self.backend
        ):
            raise StandingUnavailable
        # Query's issued UUID grammar is the shared native controller grammar.
        from pydantic import TypeAdapter

        from packages.contracts.agent_control import ControlInstance

        TypeAdapter(ControlInstance).validate_python(meta[1], strict=True)
        if current and meta[1] != self.owner.controller_id:
            raise StandingUnavailable
        count = db.execute(f"SELECT count(*) FROM {_INTENTS}").fetchone()[0]
        if (
            type(count) is not int
            or not 0 <= count <= MAX_HANDOFF_INTENTS
            or db.execute(
                f"SELECT 1 FROM {_INTENTS} WHERE typeof(action_key)!='text' OR length(CAST(action_key AS BLOB)) NOT BETWEEN 1 AND 1024 "
                "OR typeof(action_digest)!='text' OR length(CAST(action_digest AS BLOB))!=64 OR typeof(request)!='text' "
                "OR length(CAST(request AS BLOB)) NOT BETWEEN 1 AND ? OR (receipt IS NOT NULL AND (typeof(receipt)!='text' "
                "OR length(CAST(receipt AS BLOB)) NOT BETWEEN 1 AND ?)) LIMIT 1",
                (
                    StandingResourceReservation.wire_bytes,
                    StandingResourceReservationReceipt.wire_bytes,
                ),
            ).fetchone()
        ):
            raise StandingUnavailable
        for row in db.execute(f"SELECT * FROM {_INTENTS}"):
            request = StandingResourceReservation.from_json(row["request"].encode())
            if (
                len(row["action_key"]) > 256
                or request.resource_backend != meta[0]
                or request.action_digest != row["action_digest"]
                or request.canonical_bytes().decode() != row["request"]
            ):
                raise StandingUnavailable
            if row["receipt"] is not None:
                receipt = StandingResourceReservationReceipt.from_json(
                    row["receipt"].encode()
                ).validate_request(request)
                if receipt.canonical_bytes().decode() != row["receipt"]:
                    raise StandingUnavailable
        return meta

    def _register(self):
        if self.closed or self.failed or self.ready:
            raise StandingUnavailable
        # A separate connection to the already protected action file isolates
        # helper rollback poisoning from owner Off/inspection/cleanup.
        path = self.owner.state.path.parent / "resource-actions.sqlite"
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & 0o077
                or before.st_uid != os.geteuid()
            ):
                raise StandingUnavailable
            self.db = sqlite3.connect(path, timeout=0.5, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            after = path.stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise StandingUnavailable
            self.file_identity = before.st_dev, before.st_ino
        finally:
            os.close(fd)
        with self._transaction():
            rows = self.db.execute(
                "SELECT name,type FROM sqlite_schema WHERE name IN (?,?)", (_META, _INTENTS)
            ).fetchall()
            if not rows:
                for sql in _DEFINITIONS.values():
                    self.db.execute(sql)
                self.db.execute(
                    f"INSERT INTO {_META} VALUES(1,1,?,?)",
                    (secrets.token_hex(16), self.owner.controller_id),
                )
            elif len(rows) != 2 or any(row["type"] != "table" for row in rows):
                raise StandingUnavailable
            meta = self._schema(current=False)
            self.backend = meta[0]
            self.db.execute(
                f"UPDATE {_META} SET instance_id=? WHERE id=1", (self.owner.controller_id,)
            )
            self._schema()
            self.retained = bool(self.db.execute(f"SELECT 1 FROM {_INTENTS} LIMIT 1").fetchone())
            if self.closed or self.owner._closing or self.owner._closed:
                raise StandingUnavailable
        self.ready = True

    async def _database(self, invoke, capture=None):
        task = retain_task(asyncio.create_task(asyncio.to_thread(invoke)))
        self.native.add(task)
        task.add_done_callback(self._native_settled)
        interrupted = False
        while True:
            try:
                value = await asyncio.shield(task)
                if interrupted:
                    raise StandingUnavailable
                return value
            except asyncio.CancelledError:
                if capture is not None:
                    capture.suppress()
                if task.done():
                    raise
                interrupted = True

    def _native_settled(self, task):
        scope = current_owner()
        if self.failed and scope is not None:
            scope.fail()
        self.native.discard(task)
        if not task.cancelled():
            task.exception()
        self._maybe_close()

    async def register(self):
        if self.registration is None:
            self.registration = asyncio.create_task(self._database(self._register))
            self.registration.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        try:
            await asyncio.shield(self.registration)
        except asyncio.CancelledError:
            self.failed = True
            raise
        except BaseException:
            self.failed = True
            raise StandingUnavailable from None

    def require(self, payload, capture):
        if (
            type(capture) is not _Capture
            or capture.gate is not self
            or action_digest(payload) != capture.digest
        ):
            raise StandingUnavailable
        capture.guard()
        return capture

    def _pending(self, capture):
        capture.guard()
        with self._transaction(capture.guard):
            self._schema()
            if self.db.execute(
                f"SELECT 1 FROM {_INTENTS} WHERE action_key=?",
                (capture.payload["idempotency_key"],),
            ).fetchone():
                raise StandingUnavailable
            if (
                self.db.execute(f"SELECT count(*) FROM {_INTENTS}").fetchone()[0]
                >= MAX_HANDOFF_INTENTS
            ):
                raise StandingUnavailable
            self.db.execute(
                f"INSERT INTO {_INTENTS} VALUES(?,?,?,NULL)",
                (
                    capture.payload["idempotency_key"],
                    capture.digest,
                    capture.request.canonical_bytes().decode(),
                ),
            )

    def _recorded(self, capture):
        capture.guard()
        with self._transaction(capture.guard):
            self._schema()
            row = self.db.execute(
                f"SELECT * FROM {_INTENTS} WHERE action_key=?",
                (capture.payload["idempotency_key"],),
            ).fetchone()
            if (
                row is None
                or row["action_digest"] != capture.digest
                or row["request"] != capture.request.canonical_bytes().decode()
                or row["receipt"] is not None
            ):
                raise StandingUnavailable
            self.db.execute(
                f"UPDATE {_INTENTS} SET receipt=? WHERE action_key=?",
                (
                    capture.receipt.canonical_bytes().decode(),
                    capture.payload["idempotency_key"],
                ),
            )

    def _prior(self, capture):
        capture.guard()
        with self._transaction(capture.guard):
            self._schema()
            row = self.db.execute(
                f"SELECT action_digest FROM {_INTENTS} WHERE action_key=?",
                (capture.payload["idempotency_key"],),
            ).fetchone()
            if row is not None:
                if row[0] != capture.digest:
                    raise StandingConflict
                raise StandingUnavailable

    async def _work(self, capture, invoke):
        with actual_owner(capture.ownership):
            return await self._owned_work(capture, invoke)

    async def _owned_work(self, capture, invoke):
        try:
            retain = getattr(self.owner.manager, "retain_operations", None)
            if callable(retain):
                retain(capture.ownership)
            await self._database(lambda: self._prior(capture), capture)
            capture.guard()
            journal = self.owner.handoff_journal
            capture.gateway = journal
            context_call = getattr(journal, "resource_task_context", None)
            reserve = getattr(journal, "standing_resource_reserve", None)
            if not callable(context_call) or not callable(reserve):
                raise StandingUnavailable
            query = TaskResourceQuery(schema_version="1.0", task_id=capture.payload["task_id"])
            context = await context_call(query)
            capture.guard()
            if type(context) is not TaskResourceContext:
                raise StandingUnavailable
            capture.context = context.validate_request(query)
            if capture.context.family == "standing":
                capture.request = StandingResourceReservation(
                    schema_version="1.0",
                    context=capture.context,
                    resource_backend=self.backend,
                    resource_instance=capture.instance,
                    action_digest=capture.digest,
                    lease_id=new_id(),
                )
                await self._database(lambda: self._pending(capture), capture)
                capture.guard()
                receipt = await reserve(capture.request)
                capture.guard()
                if type(receipt) is not StandingResourceReservationReceipt:
                    raise StandingUnavailable
                capture.receipt = receipt.validate_request(capture.request)
                await self._database(lambda: self._recorded(capture), capture)
                capture.guard()
                if capture.receipt.disposition != "created":
                    raise StandingUnavailable
            capture.guard()
            result = await invoke(capture.payload, capture=capture)
            if capture.effects_started and (
                type(result) is not dict or result.get("ok") is not True
            ):
                capture.ownership.fail()
            return result
        except _CaptureSuppressed:
            # This guard adds no physical claim and clears no child failure.
            # Actual operation handles must still positively retire at source.
            raise
        except (StandingConflict, StandingUnavailable):
            if capture.effects_started:
                capture.ownership.fail()
            raise
        except asyncio.CancelledError:
            if capture.effects_started:
                capture.ownership.fail()
            raise
        except Exception:
            if capture.effects_started:
                capture.ownership.fail()
            raise StandingUnavailable from None
        finally:
            capture.suppress()

    async def run(self, payload, invoke):
        started = time.monotonic()
        payload = _freeze(payload)
        if self.owner._closed:
            raise StandingUnavailable
        # Existing durable actions retain their exact old replay/conflict and
        # pending semantics before any new gateway, schema or ownership read.
        if self.owner.db.execute(
            "SELECT 1 FROM actions WHERE key=?", (payload["idempotency_key"],)
        ).fetchone():
            return await self.owner._action(payload)
        if (
            self.closed
            or self.failed
            or not self.ready
            or not self.owner.standing_barrier.ready
            or self.owner.standing_barrier.failed
            or self.owner._closing
            or self.owner._closed
            or self.owner.standing_barrier.sealed(payload["task_id"])
        ):
            raise StandingUnavailable
        key, digest = payload["idempotency_key"], action_digest(payload)
        previous = self.attempts.get(key)
        if previous is not None:
            if previous.digest != digest:
                raise StandingConflict
            raise StandingUnavailable
        ownership = self.owner.standing_barrier.admit(task_id=payload["task_id"])
        capture = _Capture(
            self,
            payload,
            digest,
            started + HANDOFF_CAPTURE_SECONDS,
            self.owner.controller_id,
            ownership=ownership,
        )
        self.attempts[key] = capture
        operation = asyncio.create_task(self._work(capture, invoke))
        ownership.retain_task(operation)
        self.owner.standing_barrier.release_ingress(ownership)
        capture.task = operation
        self.operations.add(operation)

        def settled(task):
            self.operations.discard(task)
            if self.attempts.get(key) is capture:
                self.attempts.pop(key)
            if not task.cancelled():
                task.exception()
            self._maybe_close()

        operation.add_done_callback(settled)
        try:
            done, _ = await asyncio.wait(
                (operation, capture.started),
                timeout=max(0, capture.deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not capture.effects_started and time.monotonic() >= capture.deadline:
                capture.suppress(phase="facade")
                operation.cancel()
                raise StandingUnavailable
            if operation in done:
                return operation.result()
            if capture.started not in done or not capture.effects_started:
                capture.suppress(phase="facade")
                operation.cancel()
                raise StandingUnavailable
            # The capture deadline ends at the first real preemption/effect.
            # Existing hardware action timing and ownership remain unchanged.
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            capture.suppress(phase="facade")
            if not capture.effects_started:
                operation.cancel()
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            raise StandingUnavailable from None

    def _maybe_close(self):
        if (
            self.closed
            and not self.operations
            and not self.native
            and not self.owner.standing_barrier.operations
            and (not self.owner._closing or self.owner._closed)
            and self.db is not None
        ):
            self.db.close()
            self.db = None

    async def close(self):
        self.closed = True
        for capture in self.attempts.values():
            capture.suppress(phase="shutdown")
        operations = tuple(self.operations)
        for capture in tuple(self.attempts.values()):
            if not capture.effects_started and capture.task is not None:
                capture.task.cancel()
        if operations:
            await asyncio.wait(operations, timeout=1)
        self._maybe_close()
