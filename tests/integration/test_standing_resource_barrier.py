"""Independent real SQLite/ASGI Resources binding and actual-owner barrier.

No inference, hardware, live services or physical-finalization producer. Public
responses are checked against the genuine Session owner, never a fabricated ok.
"""

import asyncio
import copy
import json
import sqlite3
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from test_resource_handoff import MiB, action
from test_resource_processes import READY_MEM, file_probe, make_spec, write_script
from test_standing_dispatch_api import AUTH
from test_standing_execution_api import SECRET, original
from test_standing_execution_api import factory as factory
from test_standing_resources_registration import join_client

from packages.contracts.client import JournalClient
from packages.contracts.models import ProviderInfo
from packages.contracts.standing_execution import StandingExecutionClaim, claim_digest
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from packages.providers.text import ScriptedProvider
from packages.resources import WorkerManager
from services.converse.api import create_app as converse_app
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime
from services.resources.api import create_app as resources_app
from services.resources.client import ConverseControl
from services.resources.coordinator import ResourceCoordinator
from services.resources.state import ResourceState
from services.session.store import LocalJournal

PREFIX = "/v1/resources/standing"
ROUTES = {
    "bind_standing_claim": PREFIX + "/binding",
    "lookup_standing_binding": PREFIX + "/binding/lookup",
    "seal_standing_claim": PREFIX + "/seal",
    "lookup_standing_seal": PREFIX + "/seal/lookup",
}
NATIVE_BIND = "/v1/standing/resources/binding"
NATIVE_CONTEXT = "/v1/standing/resources/finalization/context"
NATIVE_RESERVE = "/v1/standing/resources/reserve"
NATIVE_TASK_CONTEXT = "/v1/standing/resources/context"
MARKER = "SYNTHETIC-PRIVATE-RESOURCE-BARRIER-DO-NOT-ECHO"
REFUSALS = (StandingConflict, StandingUnavailable)


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def contains_packet(value, packet):
    if value == packet:
        return True
    if type(value) is dict:
        return any(contains_packet(child, packet) for child in value.values())
    if type(value) is list:
        return any(contains_packet(child, packet) for child in value)
    return False


async def resistant(event):
    while not event.is_set():
        try:
            await event.wait()
        except asyncio.CancelledError:
            pass


class OwnedConnection(sqlite3.Connection):
    """Actual protected connections instrumented at creation, never swapped."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault = None

    def execute(self, sql, parameters=()):
        cursor = super().execute(sql, parameters)
        if self.fault is not None and sql.lstrip().upper().startswith(("INSERT", "UPDATE")):
            self.fault.wrote = True
        return cursor

    def commit(self):
        fault = self.fault
        if fault is not None and fault.wrote:
            fault.entered.set()
            if fault.mode == "held":
                assert fault.release.wait(10), "Synthetic COMMIT gate was not released"
            if fault.mode in {"before", "rollback"}:
                raise OSError(MARKER)
            super().commit()
            if fault.mode == "after":
                raise OSError(MARKER)
            return None
        return super().commit()

    def rollback(self):
        if self.fault is not None and self.fault.mode == "rollback":
            raise OSError(MARKER)
        return super().rollback()


class NativeBody(httpx.AsyncByteStream):
    def __init__(self, inner, owner):
        self.inner, self.owner = inner, owner
        self.close_calls, self.closed, self.cancelled_close = 0, False, False

    async def __aiter__(self):
        if self.owner.phase == "read":
            self.owner.entered.set()
            await resistant(self.owner.release)
        async for chunk in self.inner:
            yield chunk

    async def aclose(self):
        self.close_calls += 1
        try:
            if self.owner.phase in {"close", "cooperative-close"}:
                self.owner.entered.set()
                if self.owner.phase == "close":
                    await resistant(self.owner.release)
                else:
                    await self.owner.release.wait()
            await self.inner.aclose()
            self.closed = True
        except asyncio.CancelledError:
            self.cancelled_close = True
            raise


class NativeBoundary(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.delegate = httpx.ASGITransport(app=app)
        self.requests, self.streams = [], []
        self.path = self.phase = None
        self.reservation_entered = asyncio.Event()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.before = None
        self.coordinator = None

    async def handle_async_request(self, request):
        self.requests.append((request.url.path, json.loads(request.content), dict(request.headers)))
        if request.url.path == NATIVE_RESERVE:
            self.reservation_entered.set()
        if request.url.path == NATIVE_BIND:
            assert request.headers["authorization"] == "Bearer " + "r" * 32
            assert "x-kinaiya-standing-capability" not in request.headers
            # The exact pending request must already be durable in the same
            # protected action file before the first remote native write.
            found = False
            db = self.coordinator.standing_handoff.db
            with self.coordinator.standing_handoff.lock:
                for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
                    name = row[0].replace('"', '""')
                    for values in db.execute(f'SELECT * FROM "{name}"'):
                        for value in values:
                            if type(value) is str:
                                try:
                                    found |= contains_packet(
                                        json.loads(value), json.loads(request.content)
                                    )
                                except (ValueError, TypeError):
                                    pass
            assert found, "Session binding sent before exact local pending request COMMIT"
        if self.before is not None and request.url.path == self.path:
            before, self.before = self.before, None
            await before()
        response = await self.delegate.handle_async_request(request)
        targeted = request.url.path == self.path
        body = NativeBody(response.stream, self if targeted else SimpleNamespace(phase=None))
        response.stream = body
        self.streams.append(body)
        if targeted and self.phase == "acquire":
            self.entered.set()
            await resistant(self.release)
        if targeted and self.phase == "lost":
            await response.aclose()
            raise httpx.ReadError(MARKER, request=request)
        return response

    async def aclose(self):
        await self.delegate.aclose()


@pytest.fixture
async def barriers(factory, tmp_path, record_property):
    opened = []

    async def make(*, session=None, path=None, claim=None, require=True):
        session = session or await factory()
        agent = JournalClient(
            "http://127.0.0.1:8760", "a" * 32, transport=httpx.ASGITransport(app=session.app)
        )
        if claim is None:
            packet = await original(session)
            claim = (await agent.standing_prepare(packet)).claim
        else:
            packet = claim.request
        path = path or tmp_path / f"resources-{len(opened)}"
        path.mkdir(mode=0o700, exist_ok=True)
        # A real WorkerManager requires a nonempty fixed specification. The
        # process stays dormant under the initial owner Off intent, except in
        # tests that explicitly authorize/start this disposable fixture.
        ready = path / "ready"
        script = write_script(path, "worker.py", READY_MEM)
        manager = WorkerManager(
            [make_spec("whistle", [sys.executable, str(script), str(ready), "2"], path)],
            readiness=file_probe(ready),
        )
        state = ResourceState(path / "control/resources-state.json")
        provider = ScriptedProvider([])
        provider.info = ProviderInfo(provider="synthetic-cloud", model="no-inference", local=False)
        runtime = ConverseRuntime(
            LocalJournal(session.store, "converse"),
            provider,
            intent=ConverseIntent(
                state.path.parent / "converse-intent.json", initial_enabled=False
            ),
            resources_managed=True,
        )
        control = ConverseControl(
            "http://127.0.0.1:8762",
            "r" * 32,
            transport=httpx.ASGITransport(app=converse_app(runtime, AUTH)),
        )
        boundary = NativeBoundary(session.app)
        gateway = JournalClient("http://127.0.0.1:8760", "r" * 32, transport=boundary)
        connect = sqlite3.connect

        def connect_owned(*args, **kwargs):
            return connect(*args, **{**kwargs, "factory": OwnedConnection})

        with patch.object(sqlite3, "connect", connect_owned):
            coordinator = ResourceCoordinator(
                manager,
                state,
                control,
                journal=LocalJournal(session.store, "resources"),
                handoff_journal=gateway,
                memory_probe=lambda: 2 * 1024 * MiB,
            )
            boundary.coordinator = coordinator
            value = SimpleNamespace(
                session=session,
                agent=agent,
                claim=claim,
                packet=packet,
                path=path,
                coordinator=coordinator,
                boundary=boundary,
                gateway=gateway,
                provider=provider,
                tasks=[],
                faults=[],
                releases=[],
                closed=False,
            )
            opened.append(value)
            await coordinator.initialize()
        value.app = resources_app(coordinator, AUTH)
        value.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=value.app), base_url="http://127.0.0.1:8764"
        )
        if require:
            for method in ROUTES:
                assert callable(getattr(coordinator, method, None)), (
                    "Frozen ResourceCoordinator binding/barrier method is absent: " + method
                )
        return value

    try:
        yield make
    finally:
        cleanup = []
        for value in reversed(opened):
            value.boundary.release.set()
            for event in value.releases:
                event.set()
            for fault in value.faults:
                fault.release.set()
            db = value.coordinator.standing_handoff.db
            if db is not None:
                db.fault = None
            for task in value.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*value.tasks, return_exceptions=True)
            await value.coordinator.close()
            await value.gateway.close()
            await asyncio.wait_for(join_client(value.gateway), 5)
            await value.agent.close()
            if hasattr(value, "http"):
                await value.http.aclose()
            assert all(stream.close_calls == 1 for stream in value.boundary.streams)
            assert all(stream.closed or stream.cancelled_close for stream in value.boundary.streams)
            for worker in value.coordinator.manager._workers.values():
                if worker.process is not None:
                    await asyncio.wait_for(worker.process.wait(), 3)
                    assert worker.process.returncode is not None
                    assert worker.stop_evidence["reaped"] and worker.stop_evidence["group_empty"]
                    assert worker.leader_fd is None and worker.pgid is None
                    assert worker.pending_spawn is None and worker.pending_cleanup is None
                    assert worker.drains == []
            assert value.provider.calls == [] and value.gateway.http.is_closed
            cleanup.append(
                {
                    "native_http_joined": True,
                    "sole_closes": len(value.boundary.streams),
                    "manager_closed": True,
                    "inference_calls": 0,
                }
            )
        record_property("actual_cleanup", json.dumps(cleanup, sort_keys=True))


def method(value, name):
    invoke = getattr(value.coordinator, name, None)
    assert callable(invoke), "Frozen Resources barrier method is absent: " + name
    return invoke


def begin(value, name, request=None):
    task = asyncio.create_task(method(value, name)(value.claim if request is None else request))
    value.tasks.append(task)
    return task


async def no_seal_success(task):
    if task.done():
        with pytest.raises(StandingUnavailable):
            await task


async def drained_seal(value, task, request):
    try:
        return await asyncio.wait_for(asyncio.shield(task), 8)
    except StandingUnavailable:
        # Same request after positively settled healthy owners; never use a
        # new idempotency key to escape the permanent fence.
        return await begin(value, "seal_standing_claim", request)


async def bind(value):
    result = await begin(value, "bind_standing_claim")
    assert type(result) is StandingResourceBindingReceipt
    assert result.request.claim.canonical_bytes() == value.claim.canonical_bytes()
    assert result.request.resource_instance == value.coordinator.controller_id
    return result


def seal_request(value, binding, key="barrier:seal"):
    return StandingResourceFinalizeRequest(
        schema_version="1.0",
        idempotency_key=key,
        original=value.packet,
        expected_resource_backend=binding.request.resource_backend,
        expected_resource_instance=binding.request.resource_instance,
    )


def capture(value, key="barrier:handoff"):
    task = asyncio.create_task(
        value.coordinator.action(
            {
                "operation": "handoff",
                "idempotency_key": key,
                "task_id": value.packet.task.task_id,
                "required_bytes": 128 * MiB,
            }
        )
    )
    value.tasks.append(task)
    return task


async def assert_native_metadata_only(value, context):
    assert type(context) is StandingResourceFinalizeContext
    assert context.validate_request(context.request) == context
    assert await value.agent.standing_unresolved() == value.claim
    observed = await value.agent.standing_resource_observe(value.packet)
    assert observed == context.observation
    assert not any(
        path.endswith(("/report", "/finalization/lookup")) for path, _, _ in value.boundary.requests
    )
    # No settlement/report row may be fabricated by returning a context.
    assert (
        value.session.store.db.execute(
            "SELECT count(*) FROM standing_resource_finalizations"
        ).fetchone()[0]
        == 0
    )


def observe_fence(value, binding, monkeypatch):
    """Witness real native context completion and the contract's later fence."""
    fenced, completed = asyncio.Event(), set()
    actual_context = value.gateway.standing_resource_finalize_context
    actual_fence = value.coordinator.standing_barrier.fence

    async def context(request):
        result = await actual_context(request)
        assert type(result) is StandingResourceFinalizeContext
        result = result.validate_request(request)
        assert result.binding.canonical_bytes() == binding.request.canonical_bytes()
        assert result.request.original.canonical_bytes() == value.packet.canonical_bytes()
        completed.add(request.canonical_bytes())
        return result

    def fence(task_id, request):
        assert task_id == value.packet.task.task_id
        assert request.canonical_bytes() in completed
        actual_fence(task_id, request)
        fenced.set()

    monkeypatch.setattr(value.gateway, "standing_resource_finalize_context", context)
    monkeypatch.setattr(value.coordinator.standing_barrier, "fence", fence)
    return fenced


async def test_real_prepare_bind_admit_seal_and_exact_knowledge_replays(barriers):
    value = await barriers()
    assert await begin(value, "lookup_standing_binding") is None
    binding = await bind(value)
    assert binding.disposition == "created"
    assert (await begin(value, "lookup_standing_binding")).request == binding.request
    assert (await bind(value)).request == binding.request
    assert sum(path == NATIVE_BIND for path, _, _ in value.boundary.requests) == 1
    admitted = await value.agent.standing_admit(value.packet, capability=SECRET)
    request = seal_request(value, binding)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    assert await begin(value, "lookup_standing_seal", request) is None
    result = await begin(value, "seal_standing_claim", request)
    assert result.closure.initial == admitted.proof
    assert result.observation.reservation is None
    await assert_native_metadata_only(value, result)
    assert await begin(value, "lookup_standing_seal", request) == result
    assert await begin(value, "seal_standing_claim", request) == result
    with pytest.raises(REFUSALS):
        await capture(value)
    changed = request.model_copy(update={"idempotency_key": request.idempotency_key + ":changed"})
    with pytest.raises(REFUSALS):
        await begin(value, "seal_standing_claim", changed)


async def test_valid_binding_and_seal_work_without_admission_or_reservation(barriers):
    value = await barriers()
    binding = await bind(value)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    context = await begin(value, "seal_standing_claim", seal_request(value, binding))
    assert context.closure.initial is None and context.observation.reservation is None
    await assert_native_metadata_only(value, context)
    with pytest.raises(REFUSALS):
        await value.agent.standing_admit(value.packet, capability=SECRET)


@pytest.mark.parametrize("mutation", ["extra", "child_dict", "forged_digest", "foreign_prepared"])
async def test_actual_graph_and_full_preparation_identity_are_required(barriers, mutation):
    value = await barriers()
    saved = await bind(value)
    if mutation == "extra":
        bad = value.claim.model_copy(update={"untrusted_extra": MARKER})
    elif mutation == "child_dict":
        bad = value.claim.model_copy(update={"request": value.packet.model_dump(mode="json")})
    elif mutation == "forged_digest":
        bad = value.claim.model_copy(update={"claim_digest": "f" * 64})
    else:
        raw = value.claim.model_dump(mode="json")
        raw["prepared_utc"] = "2026-11-01T12:00:01Z"
        raw["claim_digest"] = claim_digest(value.packet, raw["prepared_utc"])
        bad = StandingExecutionClaim.model_validate(raw)
    before = len(value.boundary.requests)
    with pytest.raises((StandingConflict, StandingUnavailable, ValueError)):
        await begin(value, "bind_standing_claim", bad)
    assert len(value.boundary.requests) == before
    assert (await begin(value, "lookup_standing_binding")).request == saved.request


@pytest.mark.parametrize("phase", ["acquire", "read", "close"])
async def test_cancelled_binding_facade_never_means_absent_pending_native_owner(barriers, phase):
    value = await barriers()
    value.boundary.path, value.boundary.phase = NATIVE_BIND, phase
    operation = begin(value, "bind_standing_claim")
    await asyncio.wait_for(value.boundary.entered.wait(), 5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    with pytest.raises(REFUSALS):
        await begin(value, "lookup_standing_binding")
    value.boundary.release.set()
    await asyncio.wait_for(join_client(value.gateway), 5)
    value.boundary.phase = None
    # Actual positive sole-close settlement permits exact reconciliation only.
    found = await begin(value, "lookup_standing_binding")
    assert found is not None and found.request.claim == value.claim
    assert sum(path == NATIVE_BIND for path, _, _ in value.boundary.requests) == 1


async def test_lost_ack_keeps_exact_pending_request_and_reconciles_without_rebind(barriers):
    value = await barriers()
    value.boundary.path, value.boundary.phase = NATIVE_BIND, "lost"
    with pytest.raises(REFUSALS):
        await begin(value, "bind_standing_claim")
    value.boundary.phase = None
    receipt = await begin(value, "lookup_standing_binding")
    assert receipt is not None and receipt.request.claim == value.claim
    assert (await bind(value)).request == receipt.request
    assert sum(path == NATIVE_BIND for path, _, _ in value.boundary.requests) == 1


@pytest.mark.parametrize("phase", [NATIVE_TASK_CONTEXT, NATIVE_RESERVE])
@pytest.mark.parametrize("owner_phase", ["acquire", "read", "close"])
async def test_seal_fences_pending_task_capture_and_joins_actual_http_owner(
    barriers, phase, owner_phase, monkeypatch
):
    value = await barriers()
    binding = await bind(value)
    fenced = observe_fence(value, binding, monkeypatch)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    value.boundary.path, value.boundary.phase = phase, owner_phase
    operation = capture(value)
    await asyncio.wait_for(value.boundary.entered.wait(), 5)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    sealed = begin(value, "seal_standing_claim", seal_request(value, binding))
    await asyncio.wait_for(fenced.wait(), 5)
    await asyncio.sleep(0.02)
    await no_seal_success(sealed)
    assert value.coordinator.state.record.lease_id is None
    with pytest.raises(REFUSALS):
        await capture(value, "after-sealing")
    value.boundary.release.set()
    await asyncio.gather(operation, return_exceptions=True)
    result = await drained_seal(value, sealed, seal_request(value, binding))
    assert (
        result.observation.reservation is None
        if phase == NATIVE_TASK_CONTEXT
        else (result.observation.reservation is not None)
    )
    await assert_native_metadata_only(value, result)
    assert value.coordinator.state.record.lease_id is None


async def test_seal_never_joins_capture_while_holding_controller_lock(barriers, monkeypatch):
    value = await barriers()
    binding = await bind(value)
    fenced = observe_fence(value, binding, monkeypatch)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    await value.coordinator.lock.acquire()
    operation = capture(value)
    try:
        await asyncio.wait_for(value.boundary.reservation_entered.wait(), 5)
        assert any(path == NATIVE_RESERVE for path, _, _ in value.boundary.requests)
        await value.agent.standing_close_admission(value.packet, capability=SECRET)
        sealed = begin(value, "seal_standing_claim", seal_request(value, binding))
        await asyncio.wait_for(fenced.wait(), 5)
        await asyncio.sleep(0.02)
        await no_seal_success(sealed)
    finally:
        value.coordinator.lock.release()
    await asyncio.gather(operation, return_exceptions=True)
    result = await drained_seal(value, sealed, seal_request(value, binding))
    await assert_native_metadata_only(value, result)
    assert value.coordinator.state.record.lease_id is None


@pytest.mark.parametrize("mode", ["before", "after", "rollback"])
async def test_binding_persistence_fault_cannot_send_or_invent_absence_and_owner_off_survives(
    barriers, mode
):
    value = await barriers()
    db = value.coordinator.standing_handoff.db
    assert isinstance(db, OwnedConnection)
    fault = SimpleNamespace(
        mode=mode, wrote=False, entered=threading.Event(), release=threading.Event()
    )
    value.faults.append(fault)
    db.fault = fault
    with pytest.raises(REFUSALS):
        await begin(value, "bind_standing_claim")
    assert fault.entered.is_set()
    assert not any(path == NATIVE_BIND for path, _, _ in value.boundary.requests)
    db.fault = None
    # Even unavailable new capability does not revoke the independent owner path.
    assert (await action(value.coordinator, "converse", enabled=False))["ok"]
    assert value.coordinator.intent.read().enabled is False


async def test_commit_thread_remains_owned_across_cancel_and_shutdown(barriers):
    value = await barriers()
    db = value.coordinator.standing_handoff.db
    fault = SimpleNamespace(
        mode="held", wrote=False, entered=threading.Event(), release=threading.Event()
    )
    value.faults.append(fault)
    db.fault = fault
    operation = begin(value, "bind_standing_claim")
    assert await asyncio.to_thread(fault.entered.wait, 5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    closing = asyncio.create_task(value.coordinator.close())
    value.tasks.append(closing)
    await asyncio.sleep(0.02)
    assert not value.gateway.http.is_closed
    assert not value.coordinator._closed
    assert not any(path == NATIVE_BIND for path, _, _ in value.boundary.requests)
    fault.release.set()
    await asyncio.wait_for(asyncio.shield(closing), 8)
    assert value.gateway.http.is_closed and value.coordinator._closed


async def test_foreign_transaction_is_never_committed_or_rolled_back(barriers):
    value = await barriers()
    db = value.coordinator.standing_handoff.db
    with value.coordinator.standing_handoff.lock:
        db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(REFUSALS):
            await begin(value, "bind_standing_claim")
        assert db.in_transaction
    finally:
        db.rollback()
    assert not any(path == NATIVE_BIND for path, _, _ in value.boundary.requests)


@pytest.mark.parametrize("role", ["owner", "resources", "converse", "decisions", None])
@pytest.mark.parametrize("route", ROUTES.values())
async def test_auth_precedes_ingress_and_owner_status_actions_stay_independent(
    barriers, role, route
):
    value = await barriers(require=False)
    reads = []

    async def body():
        reads.append(True)
        yield MARKER.encode()

    headers = {"Content-Type": "application/json"}
    if role is not None:
        headers["Authorization"] = "Bearer " + AUTH.tokens[role]
    response = await value.http.post(route, headers=headers, content=body())
    assert response.status_code == (401 if role is None else 403)
    assert response.headers.get("cache-control") == "no-store"
    assert reads == [] and MARKER not in response.text
    # The independent authenticated owner can still inspect and turn Off.
    status = await value.http.get("/v1/resources", headers={"Authorization": "Bearer " + "o" * 32})
    assert status.status_code == 200


@pytest.mark.parametrize(
    "fault,status",
    [
        ("media", 415),
        ("encoding", 415),
        ("query", 422),
        ("duplicate", 422),
        ("private", 422),
        ("oversize", 422),
        ("depth", 422),
        ("foreign", 409),
    ],
)
async def test_strict_agent_ingress_bounds_and_full_claim_identity(barriers, fault, status):
    value = await barriers()
    headers = {"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"}
    path, raw = ROUTES["bind_standing_claim"], value.claim.canonical_bytes()
    if fault == "media":
        headers["Content-Type"] = "text/plain"
    elif fault == "encoding":
        headers["Content-Encoding"] = "gzip"
    elif fault == "query":
        path += "?private=" + MARKER
    elif fault == "private":
        headers["X-Kinaiya-Standing-Capability"] = SECRET.hex()
    elif fault == "duplicate":
        raw = b'{"schema_version":"1.0",' + raw[1:]
    elif fault == "oversize":
        raw = b" " * 147457
    elif fault == "depth":
        raw = b'{"untrusted":' + b"[" * 17 + b"0" + b"]" * 17 + b"}"
    else:
        packet = value.claim.model_dump(mode="json")
        packet["prepared_utc"] = "2026-11-01T12:00:01Z"
        packet["claim_digest"] = claim_digest(value.packet, packet["prepared_utc"])
        raw = wire(packet)
    response = await value.http.post(path, headers=headers, content=raw)
    assert response.status_code == status, response.text
    assert response.headers.get("cache-control") == "no-store"
    assert len(response.content) < 256 and MARKER not in response.text
    assert not any(path == NATIVE_BIND for path, _, _ in value.boundary.requests)


async def test_shared64_limit_rejects_before_ingress_and_capture_has_no_extra_pool(barriers):
    value = await barriers()
    binding = await bind(value)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    release, reads = asyncio.Event(), []
    all_received = asyncio.Event()

    async def body(index):
        reads.append(index)
        if len(reads) == 64:
            all_received.set()
        await release.wait()
        yield value.claim.canonical_bytes()

    headers = {"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"}
    pending = [
        asyncio.create_task(
            value.http.post(ROUTES["bind_standing_claim"], headers=headers, content=body(index))
        )
        for index in range(64)
    ]
    value.tasks.extend(pending)
    try:
        await asyncio.wait_for(all_received.wait(), 5)
        assert len(reads) == 64
        for route in ROUTES.values():
            extra_reads = []

            async def forbidden_body(observed=extra_reads):
                observed.append(True)
                yield value.claim.canonical_bytes()

            response = await value.http.post(route, headers=headers, content=forbidden_body())
            assert response.status_code == 503 and extra_reads == []
        with pytest.raises(StandingUnavailable):
            await capture(value)
        off = await value.http.post(
            "/v1/resources/actions",
            json={
                "operation": "converse",
                "enabled": False,
                "idempotency_key": "owner-off-under-standing-saturation",
            },
            headers={"Authorization": "Bearer " + "o" * 32},
        )
        assert off.status_code == 200 and off.json()["ok"] is True
        assert value.coordinator.intent.read().enabled is False
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
    assert (await begin(value, "lookup_standing_binding")).request == binding.request


async def test_independent_owner_off_and_legacy_session_admission_need_no_new_gateway(barriers):
    value = await barriers(require=False)
    value.coordinator.handoff_journal = None
    assert (await action(value.coordinator, "converse", enabled=False))["ok"]
    assert value.coordinator.intent.read().enabled is False
    # Native Session's lower-level legacy protocol intentionally remains usable.
    accepted = await value.agent.standing_admit(value.packet, capability=SECRET)
    assert accepted.disposition == "created" and accepted.proof.claim == value.claim
    value.coordinator.handoff_journal = value.gateway


async def test_unclosed_admission_cannot_seal_or_permanently_invent_a_context(barriers):
    value = await barriers()
    binding = await bind(value)
    with pytest.raises(REFUSALS):
        await begin(value, "seal_standing_claim", seal_request(value, binding))
    assert await begin(value, "lookup_standing_seal", seal_request(value, binding)) is None
    assert await value.agent.standing_closure_lookup(value.packet) is None


@pytest.mark.parametrize("field", ["controller", "gateway"])
async def test_binding_identity_is_rechecked_at_actual_native_commit_boundary(barriers, field):
    value = await barriers()
    value.boundary.path, value.boundary.phase = NATIVE_BIND, "acquire"
    operation = begin(value, "bind_standing_claim")
    await asyncio.wait_for(value.boundary.entered.wait(), 5)
    old_controller, old_gateway = value.coordinator.controller_id, value.coordinator.handoff_journal
    if field == "controller":
        value.coordinator.controller_id = "00000000-0000-4000-8000-000000000999"
    else:
        value.coordinator.handoff_journal = copy.copy(value.gateway)
    value.boundary.release.set()
    try:
        with pytest.raises(REFUSALS):
            await operation
    finally:
        value.coordinator.controller_id, value.coordinator.handoff_journal = (
            old_controller,
            old_gateway,
        )
    assert not value.coordinator.state.record.lease_id
    # Even restoring a field cannot convert the interrupted attempt into a new
    # bind/write permit. An exact lookup is the only reconciliation boundary.
    assert sum(path == NATIVE_BIND for path, _, _ in value.boundary.requests) == 1


async def test_seal_suppresses_preemption_before_controller_lock_and_never_uses_stale_context(
    barriers, monkeypatch
):
    value = await barriers()
    binding = await bind(value)
    fenced = observe_fence(value, binding, monkeypatch)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    entered, release = asyncio.Event(), asyncio.Event()
    value.releases.append(release)
    actual = value.coordinator._handoff_may_preempt

    async def preview(payload):
        entered.set()
        await resistant(release)
        return await actual(payload)

    monkeypatch.setattr(value.coordinator, "_handoff_may_preempt", preview)
    operation = capture(value)
    await asyncio.wait_for(entered.wait(), 5)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    sealed = begin(value, "seal_standing_claim", seal_request(value, binding))
    await asyncio.wait_for(fenced.wait(), 5)
    await asyncio.sleep(0.02)
    await no_seal_success(sealed)
    assert value.coordinator._whistle_preemptions == value.coordinator._qwen3_preemptions == 0
    release.set()
    await asyncio.gather(operation, return_exceptions=True)
    context = await drained_seal(value, sealed, seal_request(value, binding))
    assert sum(path == NATIVE_CONTEXT for path, _, _ in value.boundary.requests) >= 2
    await assert_native_metadata_only(value, context)
    assert value.coordinator.state.record.lease_id is None


async def test_seal_and_shutdown_retain_actual_worker_reaper_after_public_capture_cancel(
    barriers, monkeypatch
):
    value = await barriers()
    binding = await bind(value)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    assert (await action(value.coordinator, "profile", profile="local_work_priority"))["ok"]
    assert (await action(value.coordinator, "converse", enabled=True))["ok"]
    manager = value.coordinator.manager
    identity = manager.owned_identity("whistle")
    assert identity is not None
    child = manager._workers["whistle"].process
    assert child is not None and child.returncode is None
    entered, release = asyncio.Event(), asyncio.Event()
    value.releases.append(release)
    actual_reap = manager._wait_reap

    async def held_reap(process):
        assert process is child
        entered.set()
        await resistant(release)
        return await actual_reap(process)

    monkeypatch.setattr(manager, "_wait_reap", held_reap)
    operation = capture(value)
    await asyncio.wait_for(entered.wait(), 5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    sealed = begin(value, "seal_standing_claim", seal_request(value, binding))
    closing = asyncio.create_task(value.coordinator.close())
    value.tasks.append(closing)
    await asyncio.sleep(0.02)
    await no_seal_success(sealed)
    assert not value.gateway.http.is_closed and not value.coordinator._closed
    assert manager._workers["whistle"].leader_fd is not None
    release.set()
    await asyncio.gather(sealed, return_exceptions=True)
    await asyncio.wait_for(asyncio.shield(closing), 8)
    await asyncio.wait_for(child.wait(), 3)
    assert child.returncode is not None and manager.owned_identity("whistle") is None
    assert manager._workers["whistle"].stop_evidence["reaped"]
    assert await value.agent.standing_unresolved() == value.claim


async def test_restart_retains_completed_binding_and_permanent_seal_as_historical_knowledge(
    barriers,
):
    value = await barriers()
    binding = await bind(value)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    request = seal_request(value, binding)
    context = await begin(value, "seal_standing_claim", request)
    await value.coordinator.close()
    successor = await barriers(session=value.session, path=value.path, claim=value.claim)
    assert successor.coordinator.controller_id != binding.request.resource_instance
    found = await begin(successor, "lookup_standing_binding")
    assert found.request == binding.request
    assert await begin(successor, "lookup_standing_seal", request) == context
    with pytest.raises(REFUSALS):
        await capture(successor)
    assert successor.coordinator.state.record.lease_id is None
    assert not any(path == NATIVE_BIND for path, _, _ in successor.boundary.requests)


async def test_restart_cannot_adopt_unfinished_old_controller_binding(barriers):
    value = await barriers()
    value.boundary.path, value.boundary.phase = NATIVE_BIND, "lost"
    with pytest.raises(REFUSALS):
        await begin(value, "bind_standing_claim")
    await value.coordinator.close()
    successor = await barriers(session=value.session, path=value.path, claim=value.claim)
    with pytest.raises(REFUSALS):
        await begin(successor, "bind_standing_claim")
    # Historical knowledge is optional here; absence is explicitly forbidden.
    try:
        result = await begin(successor, "lookup_standing_binding")
    except REFUSALS:
        result = None
    else:
        assert result is not None
        assert result.request.resource_instance == value.coordinator.controller_id
    assert not any(path == NATIVE_BIND for path, _, _ in successor.boundary.requests)
    assert (await action(successor.coordinator, "converse", enabled=False))["ok"]


@pytest.mark.parametrize("mode", ["before", "after", "rollback"])
async def test_failed_seal_commit_keeps_permanent_fence_and_no_false_absence(barriers, mode):
    value = await barriers()
    binding = await bind(value)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    request = seal_request(value, binding)
    db = value.coordinator.standing_handoff.db
    fault = SimpleNamespace(
        mode=mode, wrote=False, entered=threading.Event(), release=threading.Event()
    )
    value.faults.append(fault)
    db.fault = fault
    with pytest.raises(REFUSALS):
        await begin(value, "seal_standing_claim", request)
    assert fault.entered.is_set()
    db.fault = None
    with pytest.raises(REFUSALS):
        await capture(value)
    with pytest.raises(REFUSALS):
        await begin(value, "lookup_standing_seal", request)
    assert value.coordinator.state.record.lease_id is None
    assert await value.agent.standing_unresolved() == value.claim
    assert (await action(value.coordinator, "converse", enabled=False))["ok"]


async def test_cooperative_cancelled_capture_close_never_becomes_positive_after_registry_empty(
    barriers,
):
    value = await barriers()
    binding = await bind(value)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    value.boundary.path, value.boundary.phase = NATIVE_RESERVE, "cooperative-close"
    operation = capture(value)
    await asyncio.wait_for(value.boundary.entered.wait(), 5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    value.boundary.release.set()
    await asyncio.wait_for(join_client(value.gateway), 5)
    assert value.gateway._submit_exchanges == set()
    assert value.gateway._submit_cleanup_failed is True
    assert any(stream.cancelled_close for stream in value.boundary.streams)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    with pytest.raises(StandingUnavailable):
        await begin(value, "seal_standing_claim", seal_request(value, binding))
    assert value.coordinator.state.record.lease_id is None


async def test_healthy_actual_worker_reaper_gate_allows_exact_drained_context_then_ignores_replacement(
    barriers, monkeypatch
):
    value = await barriers()
    binding = await bind(value)
    fenced = observe_fence(value, binding, monkeypatch)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    assert (await action(value.coordinator, "profile", profile="local_work_priority"))["ok"]
    assert (await action(value.coordinator, "converse", enabled=True))["ok"]
    manager = value.coordinator.manager
    original_identity = manager.owned_identity("whistle")
    child = manager._workers["whistle"].process
    assert original_identity is not None and child is not None
    entered, release = asyncio.Event(), asyncio.Event()
    value.releases.append(release)
    actual_reap = manager._wait_reap

    async def held(process):
        assert process is child
        entered.set()
        await resistant(release)
        return await actual_reap(process)

    monkeypatch.setattr(manager, "_wait_reap", held)
    operation = capture(value)
    await asyncio.wait_for(entered.wait(), 5)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    request = seal_request(value, binding)
    sealed = begin(value, "seal_standing_claim", request)
    await asyncio.wait_for(fenced.wait(), 5)
    await asyncio.sleep(0.02)
    await no_seal_success(sealed)
    assert manager._workers["whistle"].leader_fd is not None
    release.set()
    await asyncio.gather(operation, return_exceptions=True)
    context = await drained_seal(value, sealed, request)
    await assert_native_metadata_only(value, context)
    assert child.returncode is not None
    assert manager._workers["whistle"].stop_evidence["reaped"]
    assert manager._workers["whistle"].leader_fd is None
    monkeypatch.setattr(manager, "_wait_reap", actual_reap)
    assert (await manager.start("whistle"))["ready"]
    replacement = manager.owned_identity("whistle")
    assert replacement is not None and replacement != original_identity
    assert await begin(value, "seal_standing_claim", request) == context
    assert manager.is_current(replacement)
    assert manager._workers["whistle"].process.returncode is None


@pytest.mark.parametrize("field", ["expected_resource_backend", "expected_resource_instance"])
async def test_seal_requires_exact_bound_resource_identity_without_forwarding_foreign_request(
    barriers, field
):
    value = await barriers()
    binding = await bind(value)
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    request = seal_request(value, binding)
    bad = request.model_copy(
        update={
            field: "f" * 32 if field.endswith("backend") else "00000000-0000-4000-8000-000000000999"
        }
    )
    before = len(value.boundary.requests)
    with pytest.raises(REFUSALS):
        await begin(value, "seal_standing_claim", bad)
    assert len(value.boundary.requests) == before
    result = await begin(value, "seal_standing_claim", request)
    await assert_native_metadata_only(value, result)


@pytest.mark.parametrize("route", ROUTES.values())
async def test_each_resource_barrier_route_bounds_actual_streamed_ingress(barriers, route):
    value = await barriers()
    before = len(value.boundary.requests)

    async def body():
        for _ in range(37):
            yield b"x" * 4096

    response = await value.http.post(
        route,
        content=body(),
        headers={"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"},
    )
    assert response.status_code == 422 and response.headers.get("cache-control") == "no-store"
    assert len(response.content) < 256 and len(value.boundary.requests) == before


async def test_actual_resource_api_and_standalone_client_preserve_native_full_join(barriers):
    import importlib

    value = await barriers()
    kind = importlib.import_module("services.resources.standing_client").StandingResourceClient
    client = kind("http://127.0.0.1:8764", "a" * 32, transport=httpx.ASGITransport(app=value.app))
    try:
        binding = await client.bind_standing_claim(value.claim)
        assert binding.request.claim == value.claim
        assert await client.lookup_standing_binding(value.claim) is not None
        await value.agent.standing_close_admission(value.packet, capability=SECRET)
        request = seal_request(value, binding)
        context = await client.seal_standing_claim(request)
        assert await client.lookup_standing_seal(request) == context
        await assert_native_metadata_only(value, context)
    finally:
        await client.close()
    assert client.http.is_closed


async def test_new_route_ingress_is_bounded_without_native_effect_and_cleans_its_admission(
    barriers,
):
    value = await barriers()
    entered, release = asyncio.Event(), asyncio.Event()

    async def body():
        entered.set()
        await release.wait()
        yield value.claim.canonical_bytes()

    task = asyncio.create_task(
        value.http.post(
            ROUTES["bind_standing_claim"],
            content=body(),
            headers={"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"},
        )
    )
    value.tasks.append(task)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await asyncio.wait_for(asyncio.shield(task), 7)
        assert response.status_code == 503
        assert response.headers.get("cache-control") == "no-store"
        assert not any(path == NATIVE_BIND for path, _, _ in value.boundary.requests)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    # A healthy subsequent operation proves admission was not leaked/poisoned
    # by an input stream that cooperatively stopped at its five-second bound.
    assert (await bind(value)).request.claim == value.claim
