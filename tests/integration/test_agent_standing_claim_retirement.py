"""Real current-process Agent drainage; synthetic inference, native testimony.

The Agent is the only positive finalization trigger. Existing Session SQLite,
Resources ASGI/physical producer and original HTTP/lease owners are exercised;
no parsed history or empty registry is substituted for actual retirement.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_standing_agent_runtime import ADMIT, REFUSALS, TEXT, entries, submit
from test_standing_agent_runtime import factory as factory
from test_standing_agent_runtime import stack as stack
from test_standing_physical_capture_fences import physical_calls
from test_standing_physical_finalization import (
    Body,
    Gate,
    handoff,
    ready,
    recorded_reservation,
    reports,
    resistant,
)
from test_standing_physical_finalization import physical as physical

from packages.contracts.client import JournalClient
from packages.contracts.context import MAX_CONTEXT_RESPONSE_BYTES
from packages.contracts.models import AppendRequest, SessionRequest, TaskState
from packages.contracts.operation_ownership import ActualOperation, actual_owner, current_owner
from packages.contracts.standing_execution import claim_id
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeRequest,
)
from packages.contracts.standing_work import StandingUnavailable
from packages.providers.text import ProviderCleanupError
from services.resources.standing_client import StandingResourceClient

FINALIZE = "/v1/resources/standing/finalization"
FINAL_LOOKUP = FINALIZE + "/lookup"


async def reached(event):
    await asyncio.wait_for(event.wait(), 5)


async def joined(task, timeout=30):
    return await asyncio.wait_for(asyncio.shield(task), timeout)


class Transport(httpx.AsyncBaseTransport):
    """Gates surround genuine ASGI work, raw response and original closes."""

    def __init__(self, app, *, delegate=None):
        self.delegate = httpx.ASGITransport(app=app) if delegate is None else delegate
        self.requests, self.bodies, self.replies, self.work = [], [], [], []
        self.gate, self.lost, self.lose_final = None, [], False
        self.transform = None
        self.close_entered, self.close_release, self.closed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.close_release.set()
        self.close_calls, self.close_owner = 0, None

    async def handle_async_request(self, request):
        self.requests.append(request)
        self.work.append(asyncio.current_task())
        response = await self.delegate.handle_async_request(request)
        if self.transform is not None:
            raw = await response.aread()
            assert response.is_closed
            raw = self.transform(raw)
            response = httpx.Response(
                response.status_code,
                stream=httpx.ByteStream(raw),
                headers={"content-type": "application/json"},
                request=request,
            )
        gate = self.gate
        targeted = gate is not None and not gate.claimed and request.url.path == gate.path
        if targeted:
            gate.claimed, gate.exchange_task = True, asyncio.current_task()
        body = Body(response.stream, gate if targeted else None)
        response.stream = body
        self.bodies.append(body)
        self.replies.append(response)
        if targeted and gate.phase == "acquire":
            gate.entered.set()
            await resistant(gate.release)
        if self.lose_final and request.url.path == FINALIZE and not self.lost:
            raw = await response.aread()
            original = StandingResourceFinalizeRequest.from_json(request.content)
            receipt = StandingResourceFinalizationReceipt.from_json(raw).validate_request(original)
            assert body.closes == 1 and body.closed and response.is_closed
            self.lost.append(receipt)
            raise httpx.ReadError("Synthetic completed finalization ACK loss", request=request)
        return response

    async def aclose(self):
        self.close_calls += 1
        self.close_owner = asyncio.current_task()
        self.close_entered.set()
        await resistant(self.close_release)
        await self.delegate.aclose()
        self.closed.set()

    def paths(self, path):
        return [request for request in self.requests if request.url.path == path]


class Legacy:
    """Forbidden legacy notification observer, never physical success proof."""

    def __init__(self):
        self.calls = []

    async def task_finished(self, task_id):
        self.calls.append(task_id)

    async def close(self):
        pass


@pytest.fixture
async def claim_chain(stack, physical, monkeypatch, record_property):
    opened = []

    async def make():
        agent = await stack()
        resources = await physical(session=agent.env, claim=agent.claim)
        transport = Transport(resources.http._transport.app)
        client = StandingResourceClient("http://127.0.0.1:8764", "a" * 32, transport=transport)
        journal_transport = Transport(agent.env.app, delegate=agent.transport.delegate)
        agent.transport.delegate = journal_transport
        agent.runtime.standing_resource_client = client
        legacy = Legacy()
        agent.runtime.resource_client = legacy
        value = SimpleNamespace(
            agent=agent,
            resources=resources,
            client=client,
            transport=transport,
            journal_transport=journal_transport,
            legacy=legacy,
            clients=[(client, transport)],
            generation=asyncio.Event(),
            model_close=asyncio.Event(),
            finalize_entered=asyncio.Event(),
            created_ack_observed=asyncio.Event(),
            calls=[],
            receipts=[],
            closures=[],
            retirement_tasks=set(),
            preflights=[],
            public=[],
            gates=[],
            session_gate=None,
            retired=False,
            fixture_closes=[],
            fixture_workspace_release=False,
        )
        value.generation.set()
        value.model_close.set()
        opened.append(value)
        actual_factory = agent.runtime.model_settings.host_factory

        def provider(effort):
            result = actual_factory(effort)
            result.generate_gate, result.close_gate = value.generation, value.model_close
            return result

        monkeypatch.setattr(agent.runtime.model_settings, "host_factory", provider)
        actual_lookup = agent.journal.standing_task_lookup
        actual_admit = agent.journal.standing_admit
        actual_close = agent.journal.standing_close_admission
        actual_bind = client.bind_standing_claim

        async def lookup(original):
            task, scope = asyncio.current_task(), current_owner()
            value.preflights.append(
                (task, scope, isinstance(scope, ActualOperation) and task in scope.handles)
            )
            return await actual_lookup(original)

        async def admit(original, *, capability=None):
            result = await actual_admit(original, capability=capability)
            if result.disposition == "created":
                resources.initial = result.validate_request(original).proof
                value.created_ack_observed.set()
            return result

        async def close(original, *, capability=None):
            value.retirement_tasks.add(asyncio.current_task())
            result = await actual_close(original, capability=capability)
            value.closures.append(result.validate_request(original))
            return result

        async def bind(claim):
            result = await actual_bind(claim)
            assert type(result) is StandingResourceBindingReceipt
            resources.binding = result.validate_claim(claim)
            if value.session_gate is not None:
                journal_transport.gate = value.session_gate
            return result

        monkeypatch.setattr(agent.journal, "standing_task_lookup", lookup)
        monkeypatch.setattr(agent.journal, "standing_admit", admit)
        monkeypatch.setattr(agent.journal, "standing_close_admission", close)
        monkeypatch.setattr(client, "bind_standing_claim", bind)
        for name in (
            "seal_standing_claim",
            "finalize_standing_claim",
            "lookup_standing_finalization",
        ):
            actual = getattr(client, name)

            async def observe(original, _name=name, _actual=actual):
                owner = asyncio.current_task()
                value.retirement_tasks.add(owner)
                resources.request = StandingResourceFinalizeRequest.from_json(
                    original.canonical_bytes()
                )
                value.calls.append((_name, original.canonical_bytes(), owner))
                if _name == "finalize_standing_claim":
                    value.finalize_entered.set()
                result = await _actual(original)
                if type(result) is StandingResourceFinalizationReceipt:
                    value.receipts.append(result.validate_request(original))
                return result

            monkeypatch.setattr(client, name, observe)

        async def retire():
            if value.retired:
                return
            value.generation.set()
            value.model_close.set()
            for gate in value.gates:
                gate.release.set()
            for _, boundary in value.clients:
                if boundary.gate is not None:
                    boundary.gate.release.set()
                boundary.close_release.set()
            if journal_transport.gate is not None:
                journal_transport.gate.release.set()
            for provider in agent.made:
                if provider.close_gate is not None:
                    provider.close_gate.set()
            try:
                await joined(agent.runtime.close())
            except StandingUnavailable:
                pass
            agent.runtime_closed = True
            if value.public:
                await joined(asyncio.gather(*value.public, return_exceptions=True))
            work = {task for task, _, _ in value.preflights}
            work.update(
                rec.worker for rec in agent.runtime._tasks.values() if rec.worker is not None
            )
            if work:
                await joined(asyncio.gather(*work, return_exceptions=True))
            if value.retirement_tasks:
                await joined(asyncio.gather(*value.retirement_tasks, return_exceptions=True))
            for original_client, boundary in value.clients:
                exchanges = tuple(original_client._submit_exchanges)
                tasks = [
                    task
                    for exchange in exchanges
                    for task in (exchange.task, exchange.close_task)
                    if task is not None
                ]
                if tasks:
                    await joined(asyncio.gather(*tasks, return_exceptions=True))
                fallback = original_client._close_task is None
                if fallback:
                    value.fixture_closes.append(id(original_client))
                try:
                    await original_client.close()
                except StandingUnavailable:
                    pass
                await joined(original_client._close_task)
                assert boundary.close_calls == 1 and boundary.closed.is_set()
                assert original_client.http.is_closed and not original_client._submit_exchanges
                assert all(body.closes == 1 and body.closed for body in boundary.bodies)
            if agent.runtime.workspace_operation_state()["cleanup_failed"]:
                # Sticky negative cases retain capability locks. Only fixture
                # descriptor disposal follows the joined originals; no reset
                # or positive runtime retirement certificate is manufactured.
                owner = agent.workspace_owner
                owner.close()
                if not owner._db_closed:
                    owner.db.close()
                    owner._db_closed = True
                    owner._release_descriptors()
                    value.fixture_workspace_release = True
            value.retired = True

        value.retire = retire
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            await value.retire()
            record_property(
                "standing_claim_fixture_cleanup",
                json.dumps(
                    {
                        "retirement_tasks_joined": all(
                            task.done() for task in value.retirement_tasks
                        ),
                        "original_resource_transports_once_closed": all(
                            boundary.close_calls == 1 and boundary.closed.is_set()
                            for _, boundary in value.clients
                        ),
                        "manual_fixture_closer_count": len(value.fixture_closes),
                        "manual_failed_workspace_descriptor_release": value.fixture_workspace_release,
                        "synthetic_generation_calls": sum(
                            len(provider.calls) for provider in value.agent.made
                        ),
                        "actual_inference_calls": 0,
                    },
                    sort_keys=True,
                ),
            )


def launch(value):
    task = asyncio.create_task(submit(value.agent))
    value.public.append(task)
    return task


def expected_request(value):
    return StandingResourceFinalizeRequest(
        schema_version="1.0",
        idempotency_key="agent-standing-finalize:" + claim_id(value.agent.original),
        original=value.agent.original,
        expected_resource_backend=value.resources.binding.request.resource_backend,
        expected_resource_instance=value.resources.binding.request.resource_instance,
    )


def original_scope(value):
    task, scope, retained_before_await = value.preflights[0]
    assert isinstance(scope, ActualOperation) and retained_before_await
    return task, scope


async def automatic_report(
    value, disposition="no-reservation", restoration="not-required", reservation=None
):
    await reached(value.finalize_entered)
    owners = {owner for _, _, owner in value.calls}
    assert owners and not any(owner is task for owner in owners for task, _, _ in value.preflights)
    await joined(asyncio.gather(*value.retirement_tasks, return_exceptions=True))
    async with asyncio.timeout(30):
        while not value.receipts:
            await asyncio.sleep(0.005)
    await joined(asyncio.gather(*value.retirement_tasks, return_exceptions=True))
    assert value.receipts
    request = expected_request(value)
    receipt = value.receipts[-1].validate_request(request)
    report = receipt.finalization
    assert (
        report.context.request == request
        and report.context.binding == value.resources.binding.request
    )
    assert report.context.closure.initial == value.resources.initial
    assert report.context.observation.reservation == reservation
    assert report.resource_disposition == disposition and report.restoration == restoration
    assert all(wire == request.canonical_bytes() for _, wire, _ in value.calls)
    native = await value.resources.agent.standing_resource_finalization_lookup(request)
    assert native is not None and native.finalization == report
    assert len(reports(value.resources)) == 1
    assert await value.resources.agent.standing_unresolved() == value.agent.claim
    assert value.legacy.calls == []
    assert len(value.transport.paths(FINALIZE)) == 1
    assert all(
        "x-kinaiya-standing-capability" not in req.headers for req in value.transport.requests
    )
    assert all(
        body.closes == 1 and body.closed and not body.failed and not body.cancelled
        for body in value.transport.bodies
    )
    assert not value.client._submit_cleanup_failed and not value.client._submit_exchanges
    return receipt


async def test_actual_worker_and_model_cleanup_precede_automatic_native_report(claim_chain):
    value = await claim_chain()
    value.model_close.clear()
    accepted = await launch(value)
    assert accepted.disposition == "created"
    rec, provider = value.agent.record, value.agent.made[0]
    await reached(provider.closing)
    truth = (await value.agent.runtime.wait(value.agent.task_id, timeout=5)).model_dump(mode="json")
    worker, cleanup = rec.worker, rec.model_lease.cleanup
    preflight, scope = original_scope(value)
    assert preflight.done() and not worker.done() and not cleanup.done()
    assert worker in scope.handles and not scope.finished
    assert value.transport.paths(FINALIZE) == [] and reports(value.resources) == []
    assert value.legacy.calls == []
    value.model_close.set()
    await joined(worker)
    assert cleanup.done() and cleanup.exception() is None and rec.model_lease.settled
    receipt = await automatic_report(value)
    assert all(owner is not worker for _, _, owner in value.calls)
    assert scope.finished and not scope.failed
    assert (await value.agent.runtime.status(value.agent.task_id)).model_dump(mode="json") == truth
    assert (
        truth["state"] == "succeeded" and (value.agent.workspace / "result.txt").read_text() == TEXT
    )
    assert receipt.finalization.context.closure.initial == accepted.proof


async def test_bound_never_admitted_cancel_keeps_actual_session_sole_close(claim_chain):
    value = await claim_chain()
    gate = Gate("/v1/sessions/" + value.agent.original.task.task_session.session_id, "close")
    value.session_gate = gate
    value.gates.append(gate)
    public = launch(value)
    await reached(gate.entered)
    preflight, scope = original_scope(value)
    exchanges = [
        exchange
        for exchange in value.agent.journal._submit_exchanges
        if exchange.task is gate.exchange_task
    ]
    assert len(exchanges) == 1
    (exchange,) = exchanges
    assert exchange.actual_owner is scope and exchange in scope.handles
    assert exchange.close_task is gate.close_task and not gate.close_task.done()
    public.cancel()
    with pytest.raises(asyncio.CancelledError):
        await public
    assert value.resources.binding is not None and value.agent.made == []
    assert entries(value.agent) == [] and value.agent.transport.paths(ADMIT) == []
    assert value.transport.paths(FINALIZE) == [] and reports(value.resources) == []
    assert not exchange.task.done() and not scope.finished
    assert not value.agent.journal.http.is_closed and not value.client.http.is_closed
    gate.release.set()
    await joined(asyncio.gather(preflight, return_exceptions=True))
    receipt = await automatic_report(value)
    assert receipt.finalization.context.closure.initial is None
    assert gate.close_task.done() and gate.close_task.exception() is None
    assert exchange.task.done() and not value.agent.journal._submit_cleanup_failed
    assert value.agent.made == [] and value.agent.runtime._tasks == {}
    with pytest.raises(REFUSALS):
        await submit(value.agent)
    assert entries(value.agent) == []


async def test_late_original_created_ack_is_drained_without_resurrecting_worker(claim_chain):
    value = await claim_chain()
    gate = Gate(ADMIT, "close")
    value.journal_transport.gate = gate
    value.gates.append(gate)
    public = launch(value)
    await reached(gate.entered)
    preflight, scope = original_scope(value)
    assert len(entries(value.agent)) == 1 and value.agent.made
    exchange = next(
        item for item in value.agent.journal._submit_exchanges if item.task is gate.exchange_task
    )
    assert exchange.actual_owner is scope and exchange.close_task is gate.close_task
    value.model_close.clear()
    public.cancel()
    with pytest.raises(asyncio.CancelledError):
        await public
    assert value.transport.paths(FINALIZE) == [] and value.agent.runtime._tasks == {}
    gate.release.set()
    await reached(value.created_ack_observed)
    await reached(value.agent.made[0].closing)
    assert value.resources.initial is not None
    assert value.transport.paths(FINALIZE) == [] and not value.agent.made[0].closed
    value.model_close.set()
    await joined(asyncio.gather(preflight, return_exceptions=True))
    receipt = await automatic_report(value)
    assert receipt.finalization.context.closure.initial == value.resources.initial
    assert value.agent.runtime._tasks == {} and value.agent.made[0].calls == []
    assert not (value.agent.workspace / "result.txt").exists()
    assert gate.close_task.done() and gate.close_task.exception() is None


async def test_real_acquisition_and_lost_final_ack_reconcile_without_second_restore(claim_chain):
    value = await claim_chain()
    value.generation.clear()
    value.transport.lose_final = True
    assert (await launch(value)).disposition == "created"
    await reached(value.agent.made[0].generating)
    original_process = await ready(value.resources)
    result = await handoff(value.resources)
    assert result["ok"] is True and original_process.returncode is not None
    reservation = recorded_reservation(value.resources)
    assert value.resources.coordinator.state.record.lease_id == reservation.lease_id
    value.generation.set()
    await joined(value.agent.record.worker)
    receipt = await automatic_report(value, "matching-lease-finalized", "restored", reservation)
    assert (
        len(value.transport.lost) == 1
        and value.transport.lost[0].finalization == receipt.finalization
    )
    assert value.transport.paths(FINAL_LOOKUP)
    restored = value.resources.coordinator.manager.owned_identity("whistle")
    assert restored is not None and value.resources.coordinator.manager.is_current(restored)
    before = physical_calls(value.resources)
    await value.agent.runtime.close()
    value.agent.runtime_closed = True
    assert physical_calls(value.resources) == before
    assert value.agent.record.task.state == TaskState.SUCCEEDED and value.legacy.calls == []


async def test_actual_cleanup_failure_stays_uncertain_without_positive_report(claim_chain):
    value = await claim_chain()
    actual_factory = value.agent.runtime.model_settings.host_factory

    def failing(effort):
        provider = actual_factory(effort)
        provider.close_error = ProviderCleanupError("Synthetic original lease cleanup failure")
        return provider

    value.agent.runtime.model_settings.host_factory = failing
    assert (await launch(value)).disposition == "created"
    await joined(value.agent.record.worker)
    rec, provider = value.agent.record, value.agent.made[0]
    truth = rec.task.model_dump(mode="json")
    assert rec.model_lease.failed and not rec.model_lease.settled
    assert rec.standing_cleanup_uncertain and truth["state"] == "succeeded"
    await provider.close()
    assert provider.closed and rec.model_lease.failed
    close_result = await joined(asyncio.gather(value.agent.runtime.close(), return_exceptions=True))
    assert close_result[0] is None or type(close_result[0]) is StandingUnavailable
    value.agent.runtime_closed = True
    if value.retirement_tasks:
        await joined(asyncio.gather(*value.retirement_tasks, return_exceptions=True))
    assert value.agent.runtime.workspace_operation_state()["cleanup_failed"] > 0
    closure = await value.resources.agent.standing_closure_lookup(value.agent.original)
    assert closure is not None and closure.initial == value.resources.initial
    assert reports(value.resources) == [] and value.transport.paths(FINALIZE) == []
    assert (
        await value.resources.agent.standing_resource_finalization_lookup(expected_request(value))
        is None
    )
    assert await value.resources.agent.standing_unresolved() == value.agent.claim
    assert rec.task.model_dump(mode="json") == truth and value.legacy.calls == []


async def test_shutdown_replacement_keeps_original_entered_finalizer_and_single_closer(claim_chain):
    value = await claim_chain()
    gate = Gate(FINALIZE, "close")
    value.transport.gate = gate
    value.gates.append(gate)
    value.transport.close_release.clear()
    assert (await launch(value)).disposition == "created"
    await reached(gate.entered)
    worker = value.agent.record.worker
    assert worker.done() and value.agent.record.model_lease.settled
    owner = next(task for name, _, task in value.calls if name == "finalize_standing_claim")
    exchange = next(
        item for item in value.client._submit_exchanges if item.task is gate.exchange_task
    )
    assert exchange.close_task is gate.close_task and owner is not worker
    other_transport = Transport(value.resources.http._transport.app)
    other = StandingResourceClient("http://127.0.0.1:8764", "a" * 32, transport=other_transport)
    value.clients.append((other, other_transport))
    value.agent.runtime.standing_resource_client = other
    truth = value.agent.record.task.model_dump(mode="json")
    first = asyncio.create_task(value.agent.runtime.close())
    value.public.append(first)
    first_result = await joined(asyncio.gather(first, return_exceptions=True), 8)
    assert first_result[0] is None or type(first_result[0]) is StandingUnavailable
    assert not gate.close_task.done() and not value.client.http.is_closed
    assert value.transport.close_calls == 0 and other_transport.requests == []
    assert value.agent.runtime.workspace_operation_state()["cleanup_pending"] > 0
    gate.release.set()
    await joined(owner)
    await automatic_report(value)
    await reached(value.transport.close_entered)
    closer = value.transport.close_owner
    assert type(closer) is asyncio.Task and closer is not owner and not closer.done()
    assert value.transport.close_calls == 1 and not value.transport.closed.is_set()
    entered = asyncio.Event()

    async def wait_close():
        entered.set()
        await value.agent.runtime.close()

    waiter = asyncio.create_task(wait_close())
    value.public.append(waiter)
    await reached(entered)
    waiter.cancel()
    await joined(asyncio.gather(waiter, return_exceptions=True))
    assert value.transport.close_owner is closer and value.transport.close_calls == 1
    value.transport.close_release.set()
    await joined(closer)
    await joined(value.agent.runtime.close())
    value.agent.runtime_closed = True
    assert value.transport.closed.is_set() and value.transport.close_calls == 1
    assert other_transport.requests == [] and other_transport.close_calls == 1
    assert other_transport.close_owner is not closer
    assert value.agent.record.task.model_dump(mode="json") == truth
    assert gate.close_task.done() and gate.close_task.exception() is None


METADATA = ("session", "read", "entry", "resolve", "context_history", "context_qualifications")


async def metadata_fixture(factory, *, context=False):
    env = await factory()
    session = env.store.create_session(SessionRequest(channel="converse"))

    def append(kind, payload, key):
        return env.store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind=kind,
                payload=payload,
                idempotency_key=key,
            ),
            "converse",
        )

    base = append(
        "conversation.user",
        {
            "text": "Synthetic base",
            "final": True,
            "revision": 1,
            "content_scope": "public_synthetic",
        },
        "meta-base",
    )
    source = append(
        "conversation.user",
        {
            "text": "Synthetic source",
            "final": True,
            "revision": 1,
            "content_scope": "public_synthetic",
        },
        "meta-source",
    )
    revision = append(
        "conversation.revision",
        {
            "target_event_id": base.event_id,
            "text": "Synthetic correction",
            "revision": 2,
            "content_scope": "public_synthetic",
        },
        "meta-revision",
    )
    transport = Transport(env.app)
    token = ("c" if context else "a") * 32
    client = JournalClient("http://standing.test", token, transport=transport)
    return SimpleNamespace(
        env=env,
        session=session,
        base=base,
        source=source,
        revision=revision,
        transport=transport,
        client=client,
    )


def metadata_call(value, method):
    if method == "session":
        return value.client.session(value.session.session_id)
    if method == "read":
        return value.client.read(session_id=value.session.session_id)
    if method in {"entry", "resolve"}:
        return getattr(value.client, method)(value.base.event_id)
    if method == "context_history":
        return value.client.context_history(source_event_id=value.source.event_id)
    return value.client.context_qualifications(
        source_event_id=value.source.event_id,
        through_sequence=value.revision.sequence,
        base_event_ids=[value.base.event_id],
    )


@pytest.mark.parametrize("method", METADATA)
async def test_selected_metadata_keeps_original_exchange_after_cancel(factory, method):
    value = await metadata_fixture(
        factory, context=method in {"context_history", "context_qualifications"}
    )
    scope = ActualOperation(lambda _: None)
    root = object()
    scope.hold(root)
    path = (
        "/v1/sessions/" + value.session.session_id
        if method == "session"
        else "/v1/transcript/entries/" + value.base.event_id
        if method in {"entry", "resolve"}
        else "/v1/transcript/context-history"
        if method == "context_history"
        else "/v1/transcript/context-qualifications"
        if method == "context_qualifications"
        else "/v1/transcript"
    )
    gate = value.transport.gate = Gate(path, "close")
    with actual_owner(scope):
        public = asyncio.create_task(metadata_call(value, method))
    try:
        await reached(gate.entered)
        exchanges = [
            item for item in value.client._submit_exchanges if item.task is gate.exchange_task
        ]
        assert len(exchanges) == 1
        (exchange,) = exchanges
        assert exchange.actual_owner is scope and exchange in scope.handles
        assert exchange.close_task is gate.close_task and not gate.close_task.done()
        public.cancel()
        with pytest.raises(asyncio.CancelledError):
            await public
        assert exchange in value.client._submit_exchanges and not exchange.task.done()
        assert not value.client.http.is_closed and not scope.finished
        gate.release.set()
        await joined(exchange.task)
        scope.release(root)
        await joined(scope.join())
        assert not scope.failed and exchange.task.exception() is None
        assert gate.close_task.done() and gate.close_task.exception() is None
        assert value.transport.bodies[0].closes == 1 and value.transport.bodies[0].closed
        assert not value.client._submit_exchanges and not value.client._submit_cleanup_failed
        with actual_owner(ActualOperation(lambda _: None)):
            result = await metadata_call(value, method)
        if method == "session":
            assert result == value.session
        elif method in {"entry", "resolve"}:
            assert result.event_id == value.base.event_id
        elif method == "read":
            assert {item.event_id for item in result} == {
                value.base.event_id,
                value.source.event_id,
                value.revision.event_id,
            }
        else:
            expected = value.base if method == "context_history" else value.revision
            assert (
                result.entries == [expected] and result.entries[0].created_at == expected.created_at
            )
    finally:
        gate.release.set()
        await joined(asyncio.gather(public, return_exceptions=True))
        originals = tuple(value.client._submit_exchanges)
        for exchange in originals:
            await joined(asyncio.gather(exchange.task, return_exceptions=True))
        await value.client.close()
        assert value.transport.close_calls == 1 and value.transport.closed.is_set()
        assert all(body.closes == 1 and body.closed for body in value.transport.bodies)


@pytest.mark.parametrize("method", ("context_history", "context_qualifications"))
@pytest.mark.parametrize("fault", ("at-bound", "coercion", "duplicate", "malformed", "over-bound"))
async def test_owned_context_preserves_raw_json_strictness_and_exact_bound(factory, method, fault):
    value = await metadata_fixture(factory, context=True)

    def transform(raw):
        if fault == "coercion":
            data = json.loads(raw)
            data["epoch"] = str(data["epoch"])
            return json.dumps(data).encode()
        if fault == "duplicate":
            return b'{"epoch":1,' + raw[1:]
        if fault == "malformed":
            return raw[:-1]
        limit = MAX_CONTEXT_RESPONSE_BYTES + (fault == "over-bound")
        assert len(raw) < limit
        return raw + b" " * (limit - len(raw))

    value.transport.transform = transform
    scope = ActualOperation(lambda _: None)
    try:
        with actual_owner(scope):
            if fault == "at-bound":
                result = await metadata_call(value, method)
                expected = value.base if method == "context_history" else value.revision
                assert (
                    result.entries == [expected]
                    and result.entries[0].created_at == expected.created_at
                )
            else:
                with pytest.raises(ValueError):
                    await metadata_call(value, method)
        assert not value.client._submit_exchanges and not value.client._submit_cleanup_failed
        assert all(body.closes == 1 and body.closed for body in value.transport.bodies)
    finally:
        await value.client.close()
        assert value.transport.close_calls == 1 and value.transport.closed.is_set()


@pytest.mark.parametrize("method", ("entry", "resolve"))
async def test_review_derived_owned_entry_keeps_uncapped_complete_native_value(factory, method):
    """Source-aware addendum after the independently reported publication-cap bug.

    The original six ownership cases are unchanged by that report. This stored
    value is real SQLite/native ASGI; no widened context or read-page cap follows.
    """
    value = await metadata_fixture(factory)
    text = "Public synthetic complete entry: " + "x" * (1024 * 1024 + 257)
    entry = value.env.store.append(
        AppendRequest(
            session_id=value.session.session_id,
            thread_id=value.session.thread_id,
            epoch=value.session.epoch,
            kind="conversation.user",
            payload={
                "text": text,
                "final": True,
                "revision": 1,
                "content_scope": "public_synthetic",
            },
            idempotency_key="metadata-large-complete",
        ),
        "converse",
    )
    invoke = getattr(value.client, method)
    gate = Gate("/v1/transcript/entries/" + entry.event_id, "close")
    public = None
    try:
        unowned = await invoke(entry.event_id)
        assert unowned == entry and len(unowned.model_dump_json().encode()) > 1024 * 1024
        value.transport.gate = gate
        scope = ActualOperation(lambda _: None)
        with actual_owner(scope):
            public = asyncio.create_task(invoke(entry.event_id))
        await reached(gate.entered)
        exchange = next(
            item for item in value.client._submit_exchanges if item.task is gate.exchange_task
        )
        assert exchange.actual_owner is scope and exchange in scope.handles
        assert exchange.close_task is gate.close_task and not gate.close_task.done()
        assert not public.done() and not scope.finished
        gate.release.set()
        owned = await joined(public)
        await joined(scope.join())
        assert owned == unowned == entry and owned.payload["text"] == text
        assert gate.close_task.done() and gate.close_task.exception() is None
        assert exchange.task.done() and exchange.task.exception() is None
        assert not scope.failed and not value.client._submit_cleanup_failed
        assert all(body.closes == 1 and body.closed for body in value.transport.bodies)
    finally:
        gate.release.set()
        if public is not None:
            await joined(asyncio.gather(public, return_exceptions=True))
        await value.client.close()
        assert value.transport.close_calls == 1 and value.transport.closed.is_set()
