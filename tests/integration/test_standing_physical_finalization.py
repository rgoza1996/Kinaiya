"""Frozen independent physical Resources producer oracles (#239).

Genuine Session SQLite/ASGI, typed packets, disposable CPU child, selected native
stream registry and original HTTP/COMMIT/reaper owners. No inference, Agent
retirement, Task settlement or global standing-slot release is established.

Source-aware fixture maintenance uses the existing real Whistle native protocol
driver with its synthetic engine. Manifest hello fields are protocol fixtures,
not evidence that model weights were loaded or a transcription was performed.
"""

import asyncio
import copy
import hashlib
import json
import socket
import sqlite3
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from test_managed_whistle import DRIVER
from test_resource_handoff import MiB, action, legacy_choice
from test_resource_processes import make_spec, write_script
from test_standing_dispatch_api import AUTH
from test_standing_execution_api import SECRET, original
from test_standing_execution_api import factory as factory
from test_standing_resource_finalization_api import large_original, post, safe_response
from test_standing_resources_registration import join_client

from packages.contracts.client import JournalClient
from packages.contracts.models import ProviderInfo
from packages.contracts.standing_execution import StandingExecutionIntent
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeRequest,
)
from packages.contracts.standing_resources import (
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    TaskResourceQuery,
    checked_resource,
)
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeReceipt,
    StandingConflict,
    StandingUnavailable,
)
from packages.providers.text import ScriptedProvider
from packages.resources import WorkerManager
from services.converse.api import create_app as converse_app
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime
from services.resources.api import create_app as resources_app
from services.resources.client import ConverseControl
from services.resources.coordinator import ResourceCoordinator
from services.resources.standing_handoff import _Capture
from services.resources.state import ResourceState
from services.resources.whistle import WhistleBroker, WhistleEndpoint
from services.session.store import LocalJournal

BIND = "/v1/standing/resources/binding"
RESERVE = "/v1/standing/resources/reserve"
CONTEXT = "/v1/standing/resources/finalization/context"
REPORT = "/v1/standing/resources/finalization/report"
LOOKUP = "/v1/standing/resources/finalization/lookup"
OLD_COLUMNS = {
    "actions": {"key", "digest", "result"},
    "standing_handoff_meta": {"id", "version", "backend_id", "instance_id"},
    "standing_handoff_intents": {"action_key", "action_digest", "request", "receipt"},
    "standing_resource_claims": {
        "claim_id",
        "task_id",
        "session_backend",
        "resource_backend",
        "instance_id",
        "claim",
        "binding",
        "receipt",
        "seal",
        "context",
        "phase",
    },
}
REFUSALS = (StandingConflict, StandingUnavailable)
MARKER = "SYNTHETIC-PHYSICAL-PRODUCER-FAILURE"
ROOT = Path(__file__).resolve().parents[2]


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(domain, value):
    return hashlib.sha256(domain.encode() + b"\0" + wire(value)).hexdigest()


def contains(value, needle):
    if value == needle:
        return True
    if type(value) is dict:
        return any(contains(v, needle) for v in value.values())
    if type(value) in {list, tuple}:
        return any(contains(v, needle) for v in value)
    if type(value) is str and len(value) <= 819200:
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return False
        if type(decoded) in {dict, list}:
            return contains(decoded, needle)
    return False


def local_rows(value, *, new_only=False):
    # Independent connection visibility proves COMMIT, not this writer's view.
    path = value.path / "control/resource-actions.sqlite"
    with sqlite3.connect(path) as db:
        tables = [
            r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' LIMIT 65")
        ]
        assert len(tables) <= 64
        result = []
        for table in tables:
            if table.startswith("sqlite_"):
                continue
            quoted = table.replace('"', '""')
            columns = [r[1] for r in db.execute(f'PRAGMA table_xinfo("{quoted}")')]
            extra = [
                i for i, name in enumerate(columns) if name not in OLD_COLUMNS.get(table, set())
            ]
            if new_only and not extra:
                continue
            result.extend(
                (table, tuple(row))
                for row in db.execute(f'SELECT * FROM "{quoted}" LIMIT 10001')
                if not new_only or any(row[i] is not None for i in extra)
            )
        return result


def witnesses(value):
    return [
        (table, row)
        for table, row in local_rows(value, new_only=True)
        if contains(row, value.claim.claim_id)
    ]


def reports(value):
    return [body for path, body, _ in value.native.requests if path == REPORT]


async def resistant(event):
    while not event.is_set():
        try:
            await event.wait()
        except asyncio.CancelledError:
            pass


class Gate:
    def __init__(self, path, phase):
        self.path, self.phase = path, phase
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.claimed = False
        self.close_task = None
        self.exchange_task = None


class Body(httpx.AsyncByteStream):
    def __init__(self, inner, gate=None):
        self.inner, self.gate = inner, gate
        self.closes = 0
        self.closed = self.failed = self.cancelled = False

    async def __aiter__(self):
        if self.gate is not None and self.gate.phase == "read":
            self.gate.entered.set()
            await resistant(self.gate.release)
        async for chunk in self.inner:
            yield chunk

    async def aclose(self):
        self.closes += 1
        try:
            if self.gate is not None and self.gate.phase in {
                "close",
                "fail-close",
                "cooperative-close",
            }:
                self.gate.close_task = asyncio.current_task()
                self.gate.entered.set()
                if self.gate.phase == "cooperative-close":
                    await self.gate.release.wait()
                else:
                    await resistant(self.gate.release)
            await self.inner.aclose()
            self.closed = True
            if self.gate is not None and self.gate.phase == "fail-close":
                raise OSError(MARKER)
        except asyncio.CancelledError:
            self.cancelled = True
            await self.inner.aclose()
            self.closed = True
            raise
        except Exception:
            self.failed = True
            raise


class Native(httpx.AsyncBaseTransport):
    def __init__(self, app, *, resources=False):
        self.delegate = httpx.ASGITransport(app=app)
        self.resources, self.value = resources, None
        self.requests, self.bodies = [], []
        self.replaced = []
        self.gate = None
        self.bad_off_ack = False
        self.report_mismatch = False
        self.refuse_reports = False
        self.close_calls = 0

    async def handle_async_request(self, request):
        payload = json.loads(request.content) if request.content else None
        self.requests.append((request.url.path, payload, dict(request.headers)))
        if self.resources:
            assert request.headers["authorization"] == "Bearer " + "r" * 32
            assert "x-kinaiya-standing-capability" not in request.headers
            if request.url.path == BIND:
                assert len(witnesses(self.value)) == 1, (
                    "Cannot identify unique durable prework witness before native bind"
                )
            if request.url.path == REPORT:
                assert any(
                    contains(row, payload) for _, row in local_rows(self.value, new_only=True)
                ), "Native report preceded exact immutable local report COMMIT"
                if self.refuse_reports:
                    raise httpx.ConnectError(MARKER, request=request)
        response = await self.delegate.handle_async_request(request)
        gate = self.gate
        targeted = gate is not None and not gate.claimed and request.url.path == gate.path
        if targeted:
            gate.claimed = True
        if (self.bad_off_ack and payload == {"enabled": False}) or (
            self.report_mismatch and request.url.path == REPORT
        ):
            await response.aread()
            self.replaced.append(response)
            body = response.json()
            if self.bad_off_ack and payload == {"enabled": False}:
                body["resource_enabled"] = True
            else:
                body["finalization"]["policy_revision"] += 1
                changed = body["finalization"]
                changed["finalization_digest"] = digest(
                    "kinaiya.standing.resource-finalization.v1",
                    {k: v for k, v in changed.items() if k != "finalization_digest"},
                )
            raw = wire(body)
            response = httpx.Response(
                response.status_code,
                headers={**response.headers, "content-length": str(len(raw))},
                stream=httpx.ByteStream(raw),
                request=request,
            )
        body = Body(response.stream, gate if targeted else None)
        response.stream = body
        self.bodies.append(body)
        if targeted:
            gate.exchange_task = asyncio.current_task()
        if targeted and gate.phase == "acquire":
            gate.entered.set()
            await resistant(gate.release)
        if targeted and gate.phase == "lost":
            await response.aclose()
            raise httpx.ReadError(MARKER, request=request)
        return response

    async def aclose(self):
        self.close_calls += 1
        await self.delegate.aclose()


class SqlFault:
    def __init__(self, mode, *, report_only=True):
        self.mode, self.report_only = mode, report_only
        self.wrote = self.fired = False
        self.entered, self.release = threading.Event(), threading.Event()
        self.thread = None

    def matches(self, sql, parameters):
        def report(value):
            if type(value) is bytes:
                value = value.decode("utf-8")
            if type(value) is str:
                try:
                    value = json.loads(value)
                except ValueError:
                    return False
            if type(value) is dict:
                return "finalization_digest" in value or any(report(v) for v in value.values())
            return False

        return sql.lstrip().upper().startswith(("INSERT", "UPDATE", "REPLACE")) and (
            not self.report_only or any(report(v) for v in parameters)
        )


class Connection(sqlite3.Connection):
    """Actual connection instrumentation installed before native owners pin it."""

    fault = None

    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if self.fault is not None and self.fault.matches(sql, parameters):
            self.fault.wrote = True
            self._fault_write = self.fault
        return result

    def commit(self):
        fault = self.fault
        if fault is not None and getattr(self, "_fault_write", None) is fault and not fault.fired:
            fault.fired, fault.thread = True, threading.get_ident()
            fault.entered.set()
            if fault.mode == "held":
                assert fault.release.wait(10), "Synthetic COMMIT gate was not released"
            if fault.mode == "before":
                raise OSError(MARKER)
        return super().commit()


def feature(value=None):
    owner = ResourceCoordinator if value is None else value.coordinator
    for name in ("finalize_standing_claim", "lookup_standing_finalization"):
        assert callable(getattr(owner, name, None)), "Frozen physical producer is absent: " + name


def test_frozen_physical_producer_capability_is_present():
    feature()


async def large_physical_original(session):
    # The no-reservation context has fewer copies than the old reservation
    # fixture. Use a new genuine owner grant rather than lowering the byte test.
    value = await large_original(session)
    standing = session.app.state.standing_owner
    before = standing.snapshot(value.responsibility_id).record
    change_raw = before.receipt.request.model_dump(mode="json")
    change_raw.update(
        idempotency_key="physical:large-grant", expected_revision=before.receipt.revision
    )
    change_raw["authorization"]["objective"] = "\0" * 16000
    change_raw["authorization"]["schedule"]["local_time"] = "12:02"
    change = StandingChange.model_validate(change_raw)
    reply = await post(session, "/v1/standing/changes", change_raw, role="owner")
    safe_response(reply, 200)
    StandingChangeReceipt.from_json(reply.content).validate_request(change)
    actual = standing.snapshot(value.responsibility_id).record
    raw = value.model_dump(mode="json")
    raw.update(
        authorization_revision=actual.receipt.revision,
        authorization_digest=actual.receipt.authorization_digest,
        record_digest=actual.record_digest,
    )
    raw["task"]["objective"] = actual.receipt.request.authorization.objective
    raw["occurrence"]["due_utc"] = "2026-11-01T12:02:00Z"
    session.wall.value = datetime(2026, 11, 1, 12, 2, 10, tzinfo=UTC)
    return StandingExecutionIntent.model_validate(raw)


@pytest.fixture
async def physical(factory, tmp_path, monkeypatch, record_property):
    opened = []
    actual_effects = _Capture.begin_effects

    def effects(capture):
        value = next(v for v in opened if v.coordinator is capture.gate.owner)
        if value.binding is not None:
            rows = witnesses(value)
            for expected in (
                capture.payload["idempotency_key"],
                capture.digest,
                capture.request.lease_id,
                value.binding.request.resource_backend,
                value.coordinator.controller_id,
            ):
                assert any(contains(row, expected) for _, row in rows), (
                    "Effects started before exact pending physical witness COMMIT",
                    expected,
                )
            value.effect_checks += 1
        else:
            assert not any(path == BIND for path, _, _ in value.native.requests)
        value.effects_entered.set()
        return actual_effects(capture)

    monkeypatch.setattr(_Capture, "begin_effects", effects)

    async def make(*, session=None, claim=None, path=None, large=False):
        path = path or tmp_path / f"physical-{len(opened)}"
        path.mkdir(mode=0o700, exist_ok=True)
        # Keep the real Unix socket path below its native length limit.
        endpoint_directory = Path(tempfile.mkdtemp(prefix="kw239-", dir="/tmp"))
        endpoint = WhistleEndpoint(endpoint_directory)
        session = session or await factory()
        agent = JournalClient(
            "http://127.0.0.1:8760", "a" * 32, transport=httpx.ASGITransport(app=session.app)
        )
        packet = (
            claim.request
            if claim is not None
            else await (large_physical_original(session) if large else original(session))
        )
        claim = claim or (await agent.standing_prepare(packet)).claim
        script = write_script(path, "disposable.py", DRIVER)
        manager = WorkerManager(
            [
                make_spec(
                    "whistle",
                    [sys.executable, str(script), str(endpoint_directory), str(path / "entered")],
                    ROOT,
                    env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
                )
            ],
            readiness=endpoint.probe,
        )
        state = ResourceState(path / "control/resources-state.json")
        intent = ConverseIntent(state.path.parent / "converse-intent.json", initial_enabled=False)
        provider = ScriptedProvider([])
        provider.info = ProviderInfo(provider="synthetic-cloud", model="no-inference", local=False)
        runtime = ConverseRuntime(
            LocalJournal(session.store, "converse"), provider, intent=intent, resources_managed=True
        )
        control_transport = Native(converse_app(runtime, AUTH))
        control = ConverseControl("http://127.0.0.1:8762", "r" * 32, transport=control_transport)
        native = Native(session.app, resources=True)
        gateway = JournalClient("http://127.0.0.1:8760", "r" * 32, transport=native)
        connections, starts, stops, memories = [], [], [], []
        connect = sqlite3.connect

        def connect_owned(*args, **kwargs):
            connection = connect(*args, **{**kwargs, "factory": Connection})
            connections.append(connection)
            return connection

        def memory():
            memories.append(manager.status()["workers"]["whistle"]["state"])
            return 2 * 1024 * MiB

        with patch.object(sqlite3, "connect", connect_owned):
            coordinator = ResourceCoordinator(
                manager,
                state,
                control,
                journal=LocalJournal(session.store, "resources"),
                handoff_journal=gateway,
                memory_probe=memory,
            )
            broker = WhistleBroker(coordinator, endpoint)
            coordinator.register_whistle_lifecycle(abort=broker.abort_active, drain=broker.drain)
            app = resources_app(coordinator, AUTH, whistle=broker)
            value = SimpleNamespace(
                session=session,
                agent=agent,
                packet=packet,
                claim=claim,
                path=path,
                coordinator=coordinator,
                gateway=gateway,
                native=native,
                control=control,
                control_transport=control_transport,
                runtime=runtime,
                provider=provider,
                endpoint=endpoint,
                broker=broker,
                connections=connections,
                starts=starts,
                stops=stops,
                memories=memories,
                tasks=[],
                releases=[],
                faults=[],
                stream_tasks=[],
                writers=[],
                binding=None,
                initial=None,
                request=None,
                effect_checks=0,
                effects_entered=asyncio.Event(),
                reservation_receipts=[],
                original_calls=[],
                context_retirements=[],
                closed=False,
                cleanup_error=None,
            )
            native.value = value
            opened.append(value)
            actual_start, actual_stop = manager.start, manager.stop
            actual_reserve = gateway.standing_resource_reserve
            actual_work = coordinator.standing_barrier._work

            async def reserve(request):
                # Observe the actual Resources-role native ACK before a later
                # capture guard can refuse. No new reservation or authority.
                assert type(request) is StandingResourceReservation
                checked = checked_resource(request, StandingResourceReservation)
                receipt = await actual_reserve(request)
                assert type(receipt) is StandingResourceReservationReceipt
                value.reservation_receipts.append(receipt.validate_request(checked))
                return receipt

            async def work(call, name, request):
                # The task created by the real barrier remains the original
                # root, including operations/sole closes created after capture.
                value.original_calls.append((name, call, asyncio.current_task()))
                return await actual_work(call, name, request)

            async def start(worker_id):
                starts.append(worker_id)
                return await actual_start(worker_id)

            async def stop(worker_id):
                stops.append(worker_id)
                return await actual_stop(worker_id)

            monkeypatch.setattr(manager, "start", start)
            monkeypatch.setattr(manager, "stop", stop)
            monkeypatch.setattr(gateway, "standing_resource_reserve", reserve)
            monkeypatch.setattr(coordinator.standing_barrier, "_work", work)
            await coordinator.initialize()
        value.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8764"
        )
        feature(value)
        return value

    try:
        yield make
    finally:
        cleanup = []
        for value in reversed(opened):
            for boundary in (value.native, value.control_transport):
                if boundary.gate is not None:
                    boundary.gate.release.set()
            for event in value.releases:
                event.set()
            for fault in value.faults:
                fault.release.set()
            for connection in value.connections:
                connection.fault = None
            await asyncio.gather(*value.tasks, return_exceptions=True)
            await asyncio.gather(*value.stream_tasks, return_exceptions=True)
            if not value.closed:
                await close_resources(value)
            await value.agent.close()
            await value.runtime.close()
            await value.http.aclose()
            assert value.gateway.http.is_closed and not value.gateway._submit_exchanges
            assert value.control.http.is_closed
            assert value.native.close_calls == value.control_transport.close_calls == 1
            assert value.coordinator._close_owner.done()
            assert value.coordinator.standing_handoff.db is None
            assert value.coordinator.state._lock is None and value.coordinator.state._dir is None
            for managed in value.coordinator.manager._workers.values():
                if managed.process is not None:
                    await asyncio.wait_for(managed.process.wait(), 3)
                    assert managed.process.returncode is not None
                    assert managed.stop_evidence["reaped"] and managed.stop_evidence["group_empty"]
                assert managed.leader_fd is None and managed.pgid is None
                assert (
                    managed.pending_spawn is managed.pending_cleanup is None
                    and managed.drains == []
                )
            assert not value.endpoint.stream_closes.pending
            value.endpoint.close()
            assert all(
                body.closes == 1 and (body.closed or body.cancelled)
                for body in value.native.bodies + value.control_transport.bodies
            )
            assert all(
                response.is_closed
                for response in value.native.replaced + value.control_transport.replaced
            )
            for writer in value.writers:
                if not writer.is_closing():
                    writer.close()
                await writer.wait_closed()
            assert value.provider.calls == []
            cleanup.append(
                {
                    "original_close_done": True,
                    "historical_cleanup_error": value.cleanup_error,
                    "manager_reaped": True,
                    "native_http_closed_once": True,
                    "control_http_closed_once": True,
                    "actual_stream_tasks_joined": True,
                    "inference_calls": 0,
                }
            )
        record_property("physical_actual_cleanup", json.dumps(cleanup, sort_keys=True))
        record_property(
            "physical_context_retirement",
            json.dumps([entry for value in opened for entry in value.context_retirements]),
        )


async def close_resources(value):
    try:
        await value.coordinator.close()
    except (StandingUnavailable, RuntimeError, OSError) as exc:
        value.cleanup_error = type(exc).__name__
    owner = value.coordinator._close_owner
    assert owner is not None
    await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 8)
    value.closed = True


def begin(value, name="finalize_standing_claim", request=None):
    invoke = getattr(value.coordinator, name)
    task = asyncio.create_task(invoke(value.request if request is None else request))
    value.tasks.append(task)
    return task


async def bound(value, *, admitted=True):
    value.binding = await value.coordinator.bind_standing_claim(value.claim)
    assert type(value.binding) is StandingResourceBindingReceipt
    assert value.binding.request.claim == value.claim
    assert witnesses(value)
    if admitted:
        value.initial = (await value.agent.standing_admit(value.packet, capability=SECRET)).proof
    value.request = StandingResourceFinalizeRequest(
        schema_version="1.0",
        idempotency_key="physical:finalize",
        original=value.packet,
        expected_resource_backend=value.binding.request.resource_backend,
        expected_resource_instance=value.binding.request.resource_instance,
    )
    return value.binding


async def closure(value):
    proof = await value.agent.standing_close_admission(value.packet, capability=SECRET)
    assert proof.initial == value.initial
    return proof


async def ready(value):
    assert (
        await action(
            value.coordinator, "profile", key="physical:profile", profile="local_work_priority"
        )
    )["ok"]
    assert (await action(value.coordinator, "converse", key="physical:on", enabled=True))["ok"]
    managed = value.coordinator.manager._workers["whistle"]
    assert managed.process is not None and managed.process.returncode is None
    assert value.coordinator.manager.owned_identity("whistle") is not None
    return managed.process


def payload(value, key="physical:handoff:café"):
    return {
        "operation": "handoff",
        "idempotency_key": key,
        "task_id": value.packet.task.task_id,
        "required_bytes": 128 * MiB,
        "reason": "Synthetic exact café handoff",
    }


def handoff(value, body=None):
    value.payload = body or payload(value)
    task = asyncio.create_task(value.coordinator.action(value.payload))
    value.tasks.append(task)
    return task


def recorded_reservation(value):
    # Native observation intentionally requires admission closure. Before that
    # boundary inspect only the exact successful native registration ACK.
    assert len(value.reservation_receipts) == 1
    receipt = value.reservation_receipts[0]
    assert receipt.disposition == "created"
    assert receipt.validate_request(receipt.request) == receipt
    return receipt.request


def context_originals(value, gate):
    originals = [entry for entry in value.original_calls if entry[0] == "finalize"]
    assert len(originals) == 1
    _, call, work = originals[0]
    assert call.gateway is value.gateway and work in call.scope.handles
    exchanges = [
        exchange
        for exchange in value.gateway._submit_exchanges
        if exchange.actual_owner is call.scope and exchange.task is gate.exchange_task
    ]
    assert len(exchanges) == 1
    bodies = [body for body in value.native.bodies if body.gate is gate]
    assert len(bodies) == 1
    return call.scope, work, exchanges[0], bodies[0]


async def join_context_originals(value, originals):
    scope, work, exchange, body = originals
    # Join the captured original root and its dynamic children. A cancelled
    # public facade or an empty current registry cannot prove these completed.
    await asyncio.wait_for(scope.join(), 8)
    assert work.done() and not work.cancelled()
    logical_error = work.exception()
    assert logical_error is None or type(logical_error) is StandingUnavailable
    assert exchange.task.done() and not exchange.task.cancelled()
    assert exchange.task.exception() is None
    close = exchange.close_task
    assert type(close) is asyncio.Task and close.done() and not close.cancelled()
    assert close.exception() is None
    assert body.closes == 1 and body.closed and not body.failed and not body.cancelled
    assert not scope.failed and not value.gateway._submit_cleanup_failed
    value.context_retirements.append(
        {
            "logical_work_error": None if logical_error is None else type(logical_error).__name__,
            "original_work_joined": True,
            "original_exchange_joined": True,
            "original_sole_close_joined": True,
            "scope_failed": scope.failed,
            "client_cleanup_failed": value.gateway._submit_cleanup_failed,
        }
    )


async def acquired(value):
    await bound(value)
    original_process = await ready(value)
    result = await handoff(value)
    assert result["ok"] is True and result["policy"]["phase"] == "local_work"
    reservation = recorded_reservation(value)
    assert (
        reservation is not None and value.coordinator.state.record.lease_id == reservation.lease_id
    )
    assert (
        reservation.action_digest
        == hashlib.sha256(
            json.dumps(value.payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert original_process.returncode is not None
    evidence = value.coordinator.manager._workers["whistle"].stop_evidence
    assert evidence["reaped"] and evidence["group_empty"] and evidence["rss_before_bytes"] > 0
    assert value.effect_checks >= 2 and "stopped" in value.memories
    return reservation


async def completed(value, disposition, restoration, reservation=None):
    receipt = await asyncio.wait_for(begin(value), 8)
    assert type(receipt) is StandingResourceFinalizationReceipt
    receipt = receipt.validate_request(value.request)
    report = receipt.finalization
    assert report.context.binding == value.binding.request
    assert report.context.request == value.request
    assert report.context.closure.initial == value.initial
    assert report.context.observation == await value.agent.standing_resource_observe(value.packet)
    assert report.resource_disposition == disposition and report.restoration == restoration
    assert report.context.observation.reservation == reservation
    if reservation is None:
        assert (
            report.handoff_action_key,
            report.handoff_action_digest,
            report.reserved_lease_id,
        ) == (None, None, None)
    else:
        assert report.handoff_action_key == value.payload["idempotency_key"]
        assert report.handoff_action_digest == reservation.action_digest
        assert report.reserved_lease_id == reservation.lease_id
    raw = report.model_dump(mode="json")
    body = {k: v for k, v in raw.items() if k != "finalization_digest"}
    assert report.finalization_digest == digest("kinaiya.standing.resource-finalization.v1", body)
    context = raw["context"]
    assert context["context_digest"] == digest(
        "kinaiya.standing.resource-finalize-context.v1",
        {k: v for k, v in context.items() if k != "context_digest"},
    )
    assert value.binding.request_digest == digest(
        "kinaiya.standing.resource-binding.v1", value.binding.request.model_dump(mode="json")
    )
    native = await value.agent.standing_resource_finalization_lookup(value.request)
    assert native is not None and native.finalization == report
    assert any(contains(row, raw) for _, row in local_rows(value, new_only=True))
    return receipt


async def no_report_yet(value, task):
    assert reports(value) == []
    if task.done():
        with pytest.raises(REFUSALS):
            await task


def arm_sql(value, mode, *, report_only=True):
    fault = SqlFault(mode, report_only=report_only)
    value.faults.append(fault)
    for connection in value.connections:
        connection.fault = fault
    return fault


async def gate_entered(event, seconds=5):
    if isinstance(event, threading.Event):
        assert await asyncio.wait_for(asyncio.to_thread(event.wait, seconds), seconds + 1)
    else:
        await asyncio.wait_for(event.wait(), seconds)


@pytest.mark.parametrize("admitted", [False, True])
async def test_fresh_native_no_reservation_full_graph_and_immutable_replay(physical, admitted):
    value = await physical()
    await bound(value, admitted=admitted)
    assert await value.coordinator.lookup_standing_finalization(value.request) is None
    await closure(value)
    first = await completed(value, "no-reservation", "not-required")
    assert (
        await action(
            value.coordinator, "profile", key="physical:after-report", profile="local_work_priority"
        )
    )["ok"]
    assert value.coordinator.state.record.revision > first.finalization.policy_revision
    replay = await completed(value, "no-reservation", "not-required")
    assert replay.finalization == first.finalization
    assert all(body == first.finalization.model_dump(mode="json") for body in reports(value))
    count = len(reports(value))
    assert (await begin(value, "lookup_standing_finalization")).finalization == first.finalization
    assert len(reports(value)) == count
    assert await value.agent.standing_unresolved() == value.claim


async def test_registered_positive_before_effect_refusal_is_never_acquired(physical):
    value = await physical()
    await bound(value)
    result = await handoff(value)
    assert result["ok"] is False and value.effect_checks == 0 and value.starts == []
    reservation = recorded_reservation(value)
    assert reservation is not None and value.coordinator.state.record.lease_id is None
    await closure(value)
    await completed(value, "registered-no-lease", "not-required", reservation)


async def test_native_uncovered_legacy_handoff_remains_allowed_but_cannot_finalize(physical):
    value = await physical()
    value.initial = (await value.agent.standing_admit(value.packet, capability=SECRET)).proof
    await ready(value)
    assert (await handoff(value))["ok"] is True
    assert value.binding is None
    await closure(value)
    value.request = StandingResourceFinalizeRequest(
        schema_version="1.0",
        idempotency_key="physical:legacy",
        original=value.packet,
        expected_resource_backend=value.coordinator.standing_handoff.backend,
        expected_resource_instance=value.coordinator.controller_id,
    )
    with pytest.raises(REFUSALS):
        await begin(value)
    assert reports(value) == []
    assert not any(path == BIND for path, _, _ in value.native.requests)
    assert (await action(value.coordinator, "task_finished", task_id=value.packet.task.task_id))[
        "ok"
    ]


async def test_open_native_admission_cannot_be_manufactured_closed_by_resources(physical):
    value = await physical()
    await bound(value)
    with pytest.raises(REFUSALS):
        await begin(value)
    assert reports(value) == []
    assert await value.agent.standing_task_lookup(value.packet) is not None
    await closure(value)
    await completed(value, "no-reservation", "not-required")


@pytest.mark.parametrize(
    "fault", ["missing", "foreign-key", "wrong-type", "oversize", "trigger", "row-cap"]
)
async def test_existing_bound_witness_corruption_never_downgrades_to_legacy(physical, fault):
    value = await physical()
    await bound(value)
    await ready(value)
    rows = witnesses(value)
    assert len(rows) == 1, "Cannot identify unique fresh per-claim witness for corruption control"
    table, row = rows[0]
    quoted = table.replace('"', '""')
    with sqlite3.connect(value.path / "control/resource-actions.sqlite") as db:
        columns = list(db.execute(f'PRAGMA table_xinfo("{quoted}")'))
        private = [
            (r[1], cell)
            for r, cell in zip(columns, row, strict=True)
            if r[1] not in OLD_COLUMNS.get(table, set())
        ]
        key = next(
            (r[1] for r, cell in zip(columns, row, strict=True) if cell == value.claim.claim_id),
            None,
        )
        assert key is not None, "Per-claim witness must retain its exact native claim identity"
        key = key.replace('"', '""')
        db.execute("PRAGMA ignore_check_constraints=ON")
        if fault == "missing":
            if table not in OLD_COLUMNS:
                db.execute(f'DELETE FROM "{quoted}" WHERE "{key}"=?', (value.claim.claim_id,))
            else:
                assert private, "No private witness column is observable on extended baseline table"
                column = private[0][0].replace('"', '""')
                db.execute(
                    f'UPDATE "{quoted}" SET "{column}"=? WHERE "{key}"=?',
                    ("", value.claim.claim_id),
                )
        elif fault == "trigger":
            db.execute(
                f'CREATE TRIGGER physical_untrusted AFTER UPDATE ON "{quoted}" BEGIN SELECT 1; END'
            )
        elif fault == "row-cap":
            unique = set()
            for index in db.execute(f'PRAGMA index_list("{quoted}")'):
                if index[2]:
                    index_name = index[1].replace('"', '""')
                    unique.update(r[2] for r in db.execute(f'PRAGMA index_info("{index_name}")'))
            unique.update(r[1] for r in columns if r[5])
            assert unique
            for number in range(10000):
                clone = list(row)
                for index, info in enumerate(columns):
                    if info[1] in unique:
                        clone[index] = (
                            number + 100
                            if type(row[index]) is int
                            else hashlib.sha256(str((number, index)).encode()).hexdigest()
                        )
                db.execute(f'INSERT INTO "{quoted}" VALUES({",".join("?" for _ in clone)})', clone)
            assert db.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[0] == 10001
        else:
            replacement = {
                "foreign-key": "f" * 64,
                "wrong-type": sqlite3.Binary(b"x" * 64),
                "oversize": "x" * 819201,
            }[fault]
            column = key
            if table in OLD_COLUMNS:
                assert private, "No private witness column is observable on extended baseline table"
                column = private[0][0].replace('"', '""')
                if fault == "foreign-key":
                    assert type(private[0][1]) is str and value.claim.claim_id in private[0][1], (
                        "No encoded private claim identity is observable for foreign-key mutation"
                    )
                    replacement = private[0][1].replace(value.claim.claim_id, "f" * 64)
            db.execute(
                f'UPDATE "{quoted}" SET "{column}"=? WHERE "{key}"=?',
                (replacement, value.claim.claim_id),
            )
    starts = list(value.starts)
    with pytest.raises(REFUSALS):
        await handoff(value)
    await closure(value)
    with pytest.raises(REFUSALS):
        await begin(value)
    assert reports(value) == [] and value.starts == starts and value.effect_checks == 0


@pytest.mark.parametrize("restore", ["task_finished", "restore", "owner-off"])
async def test_acquired_then_enrolled_generic_restore_keeps_history_without_second_effect(
    physical, restore
):
    value = await physical()
    reservation = await acquired(value)
    if restore == "owner-off":
        result = await action(
            value.coordinator, "converse", key="physical:earlier-off", enabled=False
        )
    else:
        result = await action(
            value.coordinator,
            restore,
            key="physical:earlier-restore",
            **({"task_id": value.packet.task.task_id} if restore == "task_finished" else {}),
        )
    assert result["ok"] and value.coordinator.state.record.lease_id is None

    def controls():
        return sum(
            path == "/v1/converse/enabled" for path, _, _ in value.control_transport.requests
        )

    before = (list(value.starts), list(value.stops), controls())
    await closure(value)
    await completed(
        value,
        "matching-lease-finalized",
        "owner-off" if restore == "owner-off" else "restored",
        reservation,
    )
    assert (value.starts, value.stops, controls()) == before


async def test_finalizer_restores_only_exact_original_reserved_lease(physical):
    value = await physical()
    reservation = await acquired(value)
    await closure(value)
    await completed(value, "matching-lease-finalized", "restored", reservation)
    assert (
        value.coordinator.state.record.phase == "ready"
        and value.coordinator.state.record.lease_id is None
    )
    assert value.coordinator.manager.owned_identity("whistle") is not None


async def test_unrelated_manual_lease_is_not_restored_by_no_reservation_report(physical):
    value = await physical()
    await bound(value)
    await ready(value)
    manual = await action(
        value.coordinator, "handoff", key="physical:manual", required_bytes=128 * MiB
    )
    assert manual["ok"] and manual["policy"]["lease_task_id"] is None
    lease = manual["policy"]["lease_id"]
    before = (list(value.starts), list(value.stops))
    await closure(value)
    await completed(value, "no-reservation", "not-required")
    assert value.coordinator.state.record.lease_id == lease
    assert (value.starts, value.stops) == before


@pytest.mark.parametrize("path", [CONTEXT, RESERVE])
@pytest.mark.parametrize("phase", ["acquire", "read", "close"])
async def test_actual_http_originals_survive_facade_cancel_and_only_healthy_retry_reports(
    physical, path, phase
):
    value = await physical()
    await bound(value)
    gate = value.native.gate = Gate(path, phase)
    capture = handoff(value) if path == RESERVE else None
    if capture is not None:
        await gate_entered(gate.entered)
    await closure(value)
    finalizer = begin(value)
    await gate_entered(gate.entered)
    originals = context_originals(value, gate) if path == CONTEXT else None
    finalizer.cancel()
    await asyncio.sleep(0)
    if not finalizer.cancelled():
        await no_report_yet(value, finalizer)
    assert reports(value) == [] and not value.gateway.http.is_closed
    gate.release.set()
    await asyncio.gather(
        finalizer, *([capture] if capture is not None else []), return_exceptions=True
    )
    if originals is not None:
        await join_context_originals(value, originals)
    result = await asyncio.wait_for(begin(value), 8)
    assert result.validate_request(value.request) == result
    if path == RESERVE:
        assert result.finalization.resource_disposition == "registered-no-lease"
    else:
        assert result.finalization.resource_disposition == "no-reservation"
    assert gate.close_task is None or gate.close_task.done()


@pytest.mark.parametrize("ending", ["exception", "cancelled"])
async def test_original_response_close_failure_remains_sticky_after_registry_empty(
    physical, ending
):
    value = await physical()
    await bound(value)
    await closure(value)
    gate = value.native.gate = Gate(
        CONTEXT, "fail-close" if ending == "exception" else "cooperative-close"
    )
    operation = begin(value)
    await gate_entered(gate.entered)
    if ending == "cancelled":
        gate.close_task.cancel()
    else:
        gate.release.set()
    with pytest.raises(REFUSALS):
        await operation
    await asyncio.wait_for(join_client(value.gateway), 5)
    assert not value.gateway._submit_exchanges
    with pytest.raises(REFUSALS):
        await begin(value)
    assert reports(value) == []
    assert gate.close_task.done() and gate.close_task.cancelled() == (ending == "cancelled")
    assert any((body.failed or body.cancelled) and body.closed for body in value.native.bodies)


async def test_actual_worker_reaper_prevents_empty_ids_from_becoming_positive(
    physical, monkeypatch
):
    value = await physical()
    await bound(value)
    child = await ready(value)
    entered, release = asyncio.Event(), asyncio.Event()
    value.releases.append(release)
    actual = value.coordinator.manager._wait_reap

    async def reaper(process):
        if process is child:
            entered.set()
            await resistant(release)
        return await actual(process)

    monkeypatch.setattr(value.coordinator.manager, "_wait_reap", reaper)
    capture = handoff(value)
    await gate_entered(entered)
    await closure(value)
    finalizer = begin(value)
    await asyncio.sleep(0)
    await no_report_yet(value, finalizer)
    assert value.coordinator.state.record.lease_id is None
    assert value.coordinator.manager._workers["whistle"].leader_fd is not None
    release.set()
    await asyncio.gather(capture, finalizer, return_exceptions=True)
    reservation = (await value.agent.standing_resource_observe(value.packet)).reservation
    assert reservation is not None
    await completed(value, "registered-no-lease", "restored", reservation)


async def test_preemption_intent_commits_before_lifecycle_lock_and_requires_restoration(physical):
    value = await physical()
    await bound(value)
    await ready(value)
    await value.coordinator.lock.acquire()
    try:
        capture = handoff(value)
        await gate_entered(value.effects_entered)
        await closure(value)
        finalizer = begin(value)
        await asyncio.sleep(0)
        await no_report_yet(value, finalizer)
    finally:
        value.coordinator.lock.release()
    await asyncio.gather(capture, finalizer, return_exceptions=True)
    reservation = (await value.agent.standing_resource_observe(value.packet)).reservation
    await completed(value, "registered-no-lease", "restored", reservation)


async def test_actual_selected_stream_wait_closed_is_enrolled_until_original_retires(
    physical, monkeypatch
):
    value = await physical()
    await bound(value)
    await ready(value)
    left, right = socket.socketpair()
    _, writer = await asyncio.open_connection(sock=left)
    _, peer = await asyncio.open_connection(sock=right)
    value.writers.extend([writer, peer])
    entered, release = asyncio.Event(), asyncio.Event()
    value.releases.append(release)
    actual_wait = writer.wait_closed

    async def wait_closed():
        await actual_wait()
        entered.set()
        await resistant(release)

    monkeypatch.setattr(writer, "wait_closed", wait_closed)
    original_save = value.coordinator._save
    installed = False

    async def save(**changes):
        nonlocal installed
        result = await original_save(**changes)
        if changes.get("phase") == "unloading" and not installed:
            installed = True
            await value.endpoint.stream_closes.close(writer, timeout=0.01)
            value.stream_tasks.extend(value.endpoint.stream_closes.pending)
        return result

    monkeypatch.setattr(value.coordinator, "_save", save)
    capture = handoff(value)
    await gate_entered(entered)
    await closure(value)
    finalizer = begin(value)
    await asyncio.sleep(0)
    await no_report_yet(value, finalizer)
    assert value.endpoint.stream_closes.pending
    release.set()
    await asyncio.gather(capture, finalizer, return_exceptions=True)
    result = await asyncio.wait_for(begin(value), 8)
    assert result.finalization.restoration == "restored"
    assert not value.endpoint.stream_closes.pending
    assert all(
        task.done() and not task.cancelled() and task.exception() is None
        for task in value.stream_tasks
    )


async def test_local_report_before_commit_fault_sends_no_native_report(physical):
    value = await physical()
    await bound(value)
    await closure(value)
    fault = arm_sql(value, "before")
    with pytest.raises(REFUSALS):
        await begin(value)
    assert fault.fired and reports(value) == []
    assert (
        value.session.store.db.execute(
            "SELECT count(*) FROM standing_resource_finalizations"
        ).fetchone()[0]
        == 0
    )


async def test_original_commit_thread_and_single_dependency_close_survive_caller_cancel(physical):
    value = await physical()
    await bound(value)
    await closure(value)
    fault = arm_sql(value, "held")
    operation = begin(value)
    await gate_entered(fault.entered)
    assert fault.thread != threading.get_ident()
    operation.cancel()
    shutdown = asyncio.create_task(value.coordinator.close())
    value.tasks.append(shutdown)
    await asyncio.sleep(0)
    assert (
        reports(value) == []
        and not value.gateway.http.is_closed
        and not value.control.http.is_closed
    )
    assert value.native.close_calls == value.control_transport.close_calls == 0
    fault.release.set()
    await asyncio.gather(operation, shutdown, return_exceptions=True)
    await close_resources(value)
    assert value.coordinator._close_owner.done()
    assert value.native.close_calls == value.control_transport.close_calls == 1


async def test_lost_native_report_ack_reuses_frozen_bytes_and_never_restores_again(physical):
    value = await physical()
    reservation = await acquired(value)
    assert (await action(value.coordinator, "task_finished", task_id=value.packet.task.task_id))[
        "ok"
    ]
    await closure(value)
    value.native.gate = Gate(REPORT, "lost")
    before = (list(value.starts), list(value.stops))
    first = begin(value)
    await asyncio.gather(first, return_exceptions=True)
    result = await completed(value, "matching-lease-finalized", "restored", reservation)
    bodies = reports(value)
    assert bodies and all(body == bodies[0] for body in bodies)
    assert result.finalization.model_dump(mode="json") == bodies[0]
    assert (value.starts, value.stops) == before
    assert (
        value.session.store.db.execute(
            "SELECT count(*) FROM standing_resource_finalizations"
        ).fetchone()[0]
        == 1
    )
    changed = value.request.model_copy(update={"idempotency_key": "physical:other-finalize"})
    with pytest.raises(REFUSALS):
        await begin(value, request=changed)
    assert all(body == bodies[0] for body in reports(value))


async def test_full_native_report_reply_mismatch_never_becomes_positive(physical):
    value = await physical()
    await bound(value)
    await closure(value)
    value.native.report_mismatch = True
    with pytest.raises(REFUSALS):
        await begin(value)
    assert len(reports(value)) == 1
    actual = await value.agent.standing_resource_finalization_lookup(value.request)
    assert actual is not None and actual.finalization.model_dump(mode="json") == reports(value)[0]


async def test_restart_reads_only_committed_native_history_without_physical_replay(physical):
    value = await physical()
    await bound(value)
    await closure(value)
    receipt = await completed(value, "no-reservation", "not-required")
    await close_resources(value)
    successor = await physical(session=value.session, claim=value.claim, path=value.path)
    successor.request = value.request
    assert successor.coordinator.controller_id != value.coordinator.controller_id
    before = (list(successor.starts), list(successor.stops))
    retained = await begin(successor, "lookup_standing_finalization")
    assert retained.finalization == receipt.finalization
    assert reports(successor) == [] and (successor.starts, successor.stops) == before


async def test_owner_off_wins_restore_race_without_restarting_converse(physical, monkeypatch):
    value = await physical()
    reservation = await acquired(value)
    await closure(value)
    entered, release = asyncio.Event(), asyncio.Event()
    value.releases.append(release)
    actual = value.coordinator.manager.start

    async def start(worker_id):
        entered.set()
        await resistant(release)
        return await actual(worker_id)

    monkeypatch.setattr(value.coordinator.manager, "start", start)
    operation = begin(value)
    await gate_entered(entered)
    await legacy_choice(value.runtime, False)
    release.set()
    await asyncio.gather(operation, return_exceptions=True)
    await completed(value, "matching-lease-finalized", "owner-off", reservation)
    assert value.coordinator.intent.read().enabled is False and value.runtime.enabled is False
    assert value.coordinator.manager.owned_identity("whistle") is None


async def test_failed_off_ack_after_cleared_ids_is_negative_not_owner_off(physical):
    value = await physical()
    await acquired(value)
    value.control_transport.bad_off_ack = True
    result = await action(value.coordinator, "converse", key="physical:failed-off", enabled=False)
    assert result["ok"] is False and value.coordinator.state.record.phase == "off"
    assert value.coordinator.state.record.lease_id is None
    await closure(value)
    with pytest.raises(REFUSALS):
        await begin(value)
    assert reports(value) == []
    value.control_transport.bad_off_ack = False


async def test_shared64_actual_ingress_pool_also_fences_producer_but_not_owner_off(physical):
    value = await physical()
    await bound(value)
    await closure(value)
    entered, release, received = asyncio.Event(), asyncio.Event(), set()
    value.releases.append(release)

    async def body(index):
        received.add(index)
        if len(received) == 64:
            entered.set()
        await release.wait()
        yield value.claim.canonical_bytes()

    pending = [
        asyncio.create_task(
            value.http.post(
                "/v1/resources/standing/binding",
                content=body(i),
                headers={"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"},
            )
        )
        for i in range(64)
    ]
    value.tasks.extend(pending)
    await gate_entered(entered)
    before = len(value.native.requests)
    with pytest.raises(StandingUnavailable):
        await begin(value)
    assert len(value.native.requests) == before and reports(value) == []
    off = await action(value.coordinator, "converse", key="physical:saturated-off", enabled=False)
    assert off["ok"] and not value.coordinator.intent.read().enabled
    release.set()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("field", ["expected_resource_backend", "expected_resource_instance"])
async def test_foreign_full_request_identity_refuses_before_native_or_physical_work(
    physical, field
):
    value = await physical()
    await bound(value)
    await closure(value)
    replacement = "f" * 32 if field.endswith("backend") else "00000000-0000-4000-8000-000000000999"
    request = value.request.model_copy(update={field: replacement})
    before = (len(value.native.requests), list(value.starts), list(value.stops))
    with pytest.raises(REFUSALS):
        await begin(value, request=request)
    assert (value.starts, value.stops) == before[1:]
    assert not any(
        path in {BIND, RESERVE, REPORT} for path, _, _ in value.native.requests[before[0] :]
    )


@pytest.mark.parametrize(
    "mutation", ["extra", "nested-dict", "nested-forgery", "subclass", "raw-dict"]
)
async def test_forged_actual_typed_graph_never_drops_hidden_state(physical, mutation):
    value = await physical()
    await bound(value)
    await closure(value)
    request = copy.deepcopy(value.request)
    if mutation == "extra":
        request = request.model_copy(update={"private_extra": MARKER})
    elif mutation == "nested-dict":
        request = request.model_copy(update={"original": request.original.model_dump(mode="json")})
    elif mutation == "nested-forgery":
        request.original.task.__dict__["hidden_extra"] = MARKER
    elif mutation == "subclass":

        class UntrustedRequest(StandingResourceFinalizeRequest):
            pass

        request = UntrustedRequest.model_construct(
            _fields_set=set(type(request).model_fields), **request.__dict__
        )
    else:
        request = request.model_dump(mode="json")
    before = len(value.native.requests)
    with pytest.raises((*REFUSALS, ValueError)):
        await begin(value, request=request)
    assert len(value.native.requests) == before and reports(value) == []


@pytest.mark.parametrize("field", ["revision", "owner_intent_revision"])
async def test_current_resource_revision_outside_existing_wire_range_is_not_clamped(
    physical, field
):
    value = await physical()
    await bound(value)
    await closure(value)
    value.coordinator.state.record = value.coordinator.state.record.model_copy(
        update={field: 1 << 53}
    )
    value.coordinator.state.path.write_bytes(wire(value.coordinator.state.record.model_dump()))
    if field == "owner_intent_revision":
        choice = value.coordinator.intent.read()
        value.coordinator.intent.path.write_bytes(
            wire({"enabled": choice.enabled, "revision": 1 << 53})
        )
        assert value.coordinator.intent.read().revision == 1 << 53
    with pytest.raises(REFUSALS):
        await begin(value)
    assert reports(value) == []


async def test_controller_replacement_while_actual_context_read_is_held_cannot_certify(physical):
    value = await physical()
    await bound(value)
    await closure(value)
    gate = value.native.gate = Gate(CONTEXT, "read")
    operation = begin(value)
    await gate_entered(gate.entered)
    original_controller = value.coordinator.controller_id
    value.coordinator.controller_id = "00000000-0000-4000-8000-000000000999"
    gate.release.set()
    try:
        with pytest.raises(REFUSALS):
            await operation
        assert reports(value) == []
    finally:
        value.coordinator.controller_id = original_controller


async def test_complete_genuine_large_graph_and_native_reply_exceed_old262144_cap(physical):
    value = await physical(large=True)
    await bound(value)
    await closure(value)
    result = await completed(value, "no-reservation", "not-required")
    assert len(result.canonical_bytes()) > 262144
    assert len(result.canonical_bytes()) <= 819200
    assert len(result.finalization.canonical_bytes()) <= 802816
    assert result.finalization.context.binding.claim == value.claim


async def test_prework_coverage_commit_failure_prevents_genuine_native_binding(physical):
    value = await physical()
    fault = arm_sql(value, "before", report_only=False)
    with pytest.raises(REFUSALS):
        await value.coordinator.bind_standing_claim(value.claim)
    assert fault.fired
    assert not any(path == BIND for path, _, _ in value.native.requests)
    assert value.starts == [] and reports(value) == []


async def test_gap_after_exact_local_work_save_never_becomes_never_acquired(physical, monkeypatch):
    value = await physical()
    await bound(value)
    await ready(value)
    original_save = value.coordinator.state.save
    lease, fault = None, None

    def save(**changes):
        nonlocal lease, fault
        result = original_save(**changes)
        if changes.get("phase") == "local_work":
            lease = value.coordinator.state.record.lease_id
            assert lease is not None
            fault = arm_sql(value, "before", report_only=False)
        return result

    monkeypatch.setattr(value.coordinator.state, "save", save)
    await asyncio.gather(handoff(value), return_exceptions=True)
    assert lease is not None and fault is not None and fault.fired
    for connection in value.connections:
        connection.fault = None
    await action(
        value.coordinator,
        "task_finished",
        key="physical:after-gap",
        task_id=value.packet.task.task_id,
    )
    await closure(value)
    with pytest.raises(REFUSALS):
        await begin(value)
    assert reports(value) == []


@pytest.mark.parametrize("phase", ["read", "close"])
async def test_original_converse_restoration_http_owner_must_settle_before_report(physical, phase):
    value = await physical()
    reservation = await acquired(value)
    await closure(value)
    gate = value.control_transport.gate = Gate("/v1/converse/enabled", phase)
    operation = begin(value)
    await gate_entered(gate.entered)
    operation.cancel()
    await asyncio.sleep(0)
    assert reports(value) == [] and not value.control.http.is_closed
    gate.release.set()
    await asyncio.gather(operation, return_exceptions=True)
    await completed(value, "matching-lease-finalized", "restored", reservation)
    assert gate.close_task is None or gate.close_task.done()


async def test_actual_native_report_sole_close_precedes_positive_and_dependency_retirement(
    physical,
):
    value = await physical()
    await bound(value)
    await closure(value)
    gate = value.native.gate = Gate(REPORT, "close")
    operation = begin(value)
    await gate_entered(gate.entered)
    # Native COMMIT is real and visible, but the producer's original response
    # close is still outstanding and cannot become a successful facade.
    native = await value.agent.standing_resource_finalization_lookup(value.request)
    assert native is not None and not gate.close_task.done()
    if operation.done():
        with pytest.raises(REFUSALS):
            await operation
    operation.cancel()
    shutdown = asyncio.create_task(value.coordinator.close())
    value.tasks.append(shutdown)
    await asyncio.sleep(0)
    assert not value.gateway.http.is_closed and value.native.close_calls == 0
    gate.release.set()
    await asyncio.gather(operation, shutdown, return_exceptions=True)
    await close_resources(value)
    assert gate.close_task.done() and gate.close_task.exception() is None
    assert value.native.close_calls == 1


async def test_current_controller_rechecked_after_original_local_report_commit_wait(physical):
    value = await physical()
    await bound(value)
    await closure(value)
    fault = arm_sql(value, "held")
    operation = begin(value)
    await gate_entered(fault.entered)
    original_controller = value.coordinator.controller_id
    value.coordinator.controller_id = "00000000-0000-4000-8000-000000000999"
    fault.release.set()
    try:
        with pytest.raises(REFUSALS):
            await operation
        assert reports(value) == []
    finally:
        value.coordinator.controller_id = original_controller


async def test_native_reservation_without_original_local_action_cannot_mean_never_acquired(
    physical,
):
    value = await physical()
    await bound(value)
    context = await value.gateway.resource_task_context(
        TaskResourceQuery(schema_version="1.0", task_id=value.packet.task.task_id)
    )
    reservation = StandingResourceReservation(
        schema_version="1.0",
        context=context,
        resource_backend=value.binding.request.resource_backend,
        resource_instance=value.coordinator.controller_id,
        lease_id="00000000-0000-4000-8000-000000000777",
        action_digest=hashlib.sha256(b"synthetic external reservation").hexdigest(),
    )
    created = await value.gateway.standing_resource_reserve(reservation)
    assert created.request == reservation
    assert value.coordinator.state.record.lease_id is None and value.effect_checks == 0
    await closure(value)
    with pytest.raises(REFUSALS):
        await begin(value)
    assert reports(value) == []


async def test_restart_cannot_publish_old_local_only_report_or_manufacture_native_history(physical):
    value = await physical()
    await bound(value)
    await closure(value)
    value.native.refuse_reports = True
    with pytest.raises(REFUSALS):
        await asyncio.wait_for(begin(value), 30)
    assert reports(value)
    assert await value.agent.standing_resource_finalization_lookup(value.request) is None
    assert any(contains(row, reports(value)[0]) for _, row in local_rows(value, new_only=True))
    await close_resources(value)
    successor = await physical(session=value.session, claim=value.claim, path=value.path)
    successor.request = value.request
    before = (list(successor.starts), list(successor.stops))
    try:
        history = await begin(successor, "lookup_standing_finalization")
    except StandingUnavailable:
        history = None  # Unresolved local history is not positive native testimony.
    assert history is None
    with pytest.raises(REFUSALS):
        await begin(successor)
    assert reports(successor) == [] and (successor.starts, successor.stops) == before
