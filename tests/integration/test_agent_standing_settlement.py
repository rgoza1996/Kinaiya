"""Independent current-process Agent settlement integration oracles.

Frozen before the new Agent registration/settlement runtime is consulted. The
existing f52 outcome capture, claim-owner fixture and disposable native physical
producer are attributed prerequisites, not independent codec or H64 evidence.
Only inference and explicit transport failures are synthetic. No test constructs
or manually submits a physical report. A real v2 Session composition is required;
the cold 42f source lacks it as well as the new runtime integration.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from test_agent_mode_permissions import bind_workspace
from test_agent_standing_claim_retirement import (
    Gate,
    Transport,
    joined,
    launch,
    original_scope,
    reached,
)
from test_agent_standing_claim_retirement import claim_chain as claim_chain
from test_standing_agent_runtime import (
    ADMIT,
    SOURCE,
    TEXT,
    MeasuredFiles,
    NativeBoundary,
    Stack,
    entries,
    full_model_pin,
    submit,
)
from test_standing_dispatch_api import AUTH
from test_standing_execution_api import SECRET, checked, close_env, headers
from test_standing_execution_api import original as original_packet
from test_standing_physical_capture_fences import physical_calls
from test_standing_physical_finalization import (
    handoff,
    ready,
    recorded_reservation,
    reports,
)
from test_standing_physical_finalization import physical as physical

from core.policy import ToolPolicy
from packages.contracts.client import JournalClient
from packages.contracts.models import InterruptRequest, SessionRequest
from packages.contracts.operation_ownership import ActualOperation, current_owner
from packages.contracts.standing_agent_settlement import (
    StandingAgentRetirementRegistrationRequest,
    StandingAgentSettlementQuery,
    StandingAgentSettlementRequest,
    agent_retirement_capability_digest,
)
from packages.contracts.standing_execution import StandingExecutionIntent, capability_digest
from packages.contracts.standing_work import StandingChange, StandingChangeReceipt
from packages.providers.text import ProviderCleanupError
from services.agent.model_settings import AgentModelSettings
from services.agent.runtime import AgentRuntime
from services.resources.standing_client import StandingResourceClient
from services.session.api import create_app
from services.session.store import JournalError, Store

REGISTER = "/v1/standing/tasks/retirement/register"
REGISTRATION_LOOKUP = "/v1/standing/tasks/retirement/registration/lookup"
SETTLE = "/v1/standing/tasks/settle"
SETTLEMENT_LOOKUP = "/v1/standing/tasks/settlement/lookup"
CAPABILITY = "x-kinaiya-standing-settlement-capability"
BIND = "/v1/resources/standing/binding"


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


@pytest.fixture
async def factory(tmp_path):
    """Explicit v2, genuine Store/control/ASGI lifecycle; no read migration."""
    opened = []

    async def make(path=None):
        path = tmp_path / f"settlement-session-{len(opened)}.sqlite" if path is None else path
        store = Store(path)
        session = store.create_session(SessionRequest(channel="agent"))
        wall = SimpleNamespace(value=datetime(2026, 11, 1, 11, 59, 50, tzinfo=UTC))
        mono = SimpleNamespace(value=1000)
        try:
            app = create_app(
                store,
                AUTH,
                standing_clock=lambda: wall.value,
                standing_monotonic_ms=lambda: mono.value,
                standing_protocol=2,
            )
        except BaseException:
            store.close()
            raise
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://standing.test"
        )
        env = SimpleNamespace(
            path=path,
            store=store,
            session=session,
            wall=wall,
            mono=mono,
            app=app,
            lifespan=lifespan,
            client=client,
            closed=False,
            operations=app.state.standing_owner_operations,
        )
        opened.append(env)
        assert app.state.standing_protocol == 2 and app.state.standing_settlement_initialized
        return env

    try:
        yield make
    finally:
        for env in reversed(opened):
            await close_env(env)


@pytest.fixture
async def stack(factory, tmp_path):
    """Local stack, so an old fixture's explicit v1 never becomes v2 proof."""
    opened = []

    async def make():
        env = await factory()
        workspace = tmp_path / f"settlement-worker-{len(opened)}"
        workspace.mkdir(mode=0o700)
        (workspace / "input.txt").write_text(SOURCE)
        transport = NativeBoundary(env.app)
        journal = JournalClient("http://standing.test", "a" * 32, transport=transport)
        base = MeasuredFiles()
        try:
            runtime = AgentRuntime(
                journal,
                base,
                workspace,
                ToolPolicy(workspace, allow_write=True),
                standing_protocol=2,
            )
        except BaseException:
            await journal.close()
            await base.close()
            raise
        owner = bind_workspace(runtime, tmp_path / f"settlement-workspace-{len(opened)}")
        context = owner.active_context()
        made = []

        def fresh(effort):
            assert effort is None
            provider = MeasuredFiles()
            made.append(provider)
            return provider

        runtime.model_settings = AgentModelSettings(
            base, {}, host_factory=fresh, path=tmp_path / f"settlement-models-{len(opened)}.sqlite"
        )
        value = Stack(env, runtime, journal, transport, base, None, None, workspace, owner, made)
        opened.append(value)
        packet = await original_packet(env)
        record = env.app.state.standing_owner.snapshot(packet.responsibility_id).record
        raw = record.receipt.request.model_dump(mode="json")
        raw.update(
            expected_revision=record.receipt.revision, idempotency_key="settlement-workspace"
        )
        raw["authorization"]["workspace_pin"] = context.pin.model_dump(mode="json")
        grant = StandingChange.model_validate(raw)
        env.wall.value = datetime(2026, 11, 1, 11, 59, 55, tzinfo=UTC)
        response = await env.client.post(
            "/v1/standing/changes",
            content=grant.canonical_bytes(),
            headers=headers(**{"Content-Type": "application/json"}),
        )
        checked(response, 200, StandingChangeReceipt).validate_request(grant)
        record = env.app.state.standing_owner.snapshot(packet.responsibility_id).record
        env.wall.value = datetime(2026, 11, 1, 12, 0, 10, tzinfo=UTC)
        _, pin = runtime.model_settings.selection_info("seed-v1")
        raw = packet.model_dump(mode="json")
        raw.update(
            authorization_revision=record.receipt.revision,
            authorization_digest=record.receipt.authorization_digest,
            record_digest=record.record_digest,
        )
        raw["task"]["workspace_pin"] = context.pin.model_dump(mode="json")
        raw["task"]["model_pin"] = full_model_pin(pin)
        value.original = StandingExecutionIntent.model_validate(raw)
        value.claim = (
            (await journal.standing_prepare(value.original)).validate_request(value.original).claim
        )
        # claim_chain installs the actual complete Resources role client before
        # any submit; there is intentionally no incomplete legacy binder.
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            value.transport.release.set()
            for provider in [value.base, *value.made]:
                if provider.generate_gate is not None:
                    provider.generate_gate.set()
                if provider.close_gate is not None:
                    provider.close_gate.set()
                provider.close_error = None
            try:
                if not value.runtime_closed:
                    await joined(value.runtime.close())
            finally:
                await joined(value.journal.close())
                assert value.journal.http.is_closed and not value.journal._submit_exchanges


@pytest.fixture
async def settlement_chain(claim_chain, monkeypatch, record_property):
    opened = []

    async def make():
        value = await claim_chain()
        value.registrations, value.registration_reads = [], []
        value.settlements, value.settlement_reads = [], []
        value.registered, value.settled = asyncio.Event(), asyncio.Event()
        value.registration_calls, value.settlement_calls = [], []
        value.secret = None
        journal = value.agent.journal
        register = journal.standing_retirement_register
        registration_lookup = journal.standing_retirement_registration_lookup
        settle = journal.standing_settle
        settlement_lookup = journal.standing_settlement_lookup

        async def registration(request):
            frozen = StandingAgentRetirementRegistrationRequest.from_json(request.canonical_bytes())
            task, scope = asyncio.current_task(), current_owner()
            value.registration_calls.append(
                (frozen, task, scope, isinstance(scope, ActualOperation) and task in scope.handles)
            )
            result = await register(request)
            receipt = result.validate_request(frozen)
            value.registrations.append(receipt)
            value.registered.set()
            return result

        async def registration_read(request):
            value.registration_reads.append(request.canonical_bytes())
            result = await registration_lookup(request)
            if result is not None:
                value.registrations.append(result.validate_request(request))
                value.registered.set()
            return result

        async def settlement(request, *, secret32):
            frozen = StandingAgentSettlementRequest.from_json(request.canonical_bytes())
            task, scope = asyncio.current_task(), current_owner()
            value.settlement_calls.append((frozen, task, scope))
            value.retirement_tasks.add(task)
            value.secret = secret32
            result = await settle(request, secret32=secret32)
            value.settlements.append(result.validate_request(frozen))
            value.settled.set()
            return result

        async def settlement_read(query):
            value.settlement_reads.append(query.canonical_bytes())
            result = await settlement_lookup(query)
            if result is not None:
                value.settlements.append(result.validate_query(query))
                value.settled.set()
            return result

        monkeypatch.setattr(journal, "standing_retirement_register", registration)
        monkeypatch.setattr(journal, "standing_retirement_registration_lookup", registration_read)
        monkeypatch.setattr(journal, "standing_settle", settlement)
        monkeypatch.setattr(journal, "standing_settlement_lookup", settlement_read)
        bind = value.client.bind_standing_claim

        async def ordered_bind(claim):
            assert (
                value.registrations and value.registrations[0].registration.request.claim == claim
            )
            assert value.agent.made == []
            return await bind(claim)

        monkeypatch.setattr(value.client, "bind_standing_claim", ordered_bind)
        opened.append(value)
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            await value.retire()
            record_property(
                "agent_settlement_original_cleanup",
                json.dumps(
                    {
                        "original_finalizer_tasks_joined": all(
                            task.done() for task in value.retirement_tasks
                        ),
                        "session_response_original_closes_once": all(
                            body.closes == 1 and body.closed
                            for body in value.journal_transport.bodies
                        ),
                        "session_exchange_registry_settled": not value.agent.journal._submit_exchanges,
                        "physical_report_count": len(reports(value.resources)),
                        "settlement_call_count": len(value.settlement_calls),
                        "actual_inference_calls": 0,
                    },
                    sort_keys=True,
                ),
            )


def gate_for(value, path, phase):
    gate = Gate(path, phase)
    value.journal_transport.gate = gate
    value.gates.append(gate)
    return gate


def exchange_for(value, gate):
    matches = [
        exchange
        for exchange in value.agent.journal._submit_exchanges
        if exchange.task is gate.exchange_task
    ]
    assert len(matches) == 1
    return matches[0]


def native_settlements(value):
    return value.agent.env.store.db.execute(
        "SELECT record FROM standing_agent_settlements ORDER BY claim_id"
    ).fetchall()


async def retired_original(value):
    preflight, scope = original_scope(value)
    await joined(asyncio.gather(preflight, return_exceptions=True))
    owner = scope.standing_claim
    async with asyncio.timeout(30):
        while owner.operation is None:
            await asyncio.sleep(0)
    await joined(asyncio.gather(owner.operation, return_exceptions=True))
    return owner


async def positive(value, kind="launched-outcome-known"):
    await asyncio.wait_for(value.settled.wait(), 30)
    owner = await retired_original(value)
    assert len(value.registration_calls) == len(value.settlement_calls) == 1
    request, finalizer, final_scope = value.settlement_calls[0]
    receipt = value.settlements[-1].validate_request(request)
    registration, child, work_scope, retained_at_entry = value.registration_calls[0]
    preflight, scope = original_scope(value)
    assert registration.claim == value.agent.claim
    assert str(UUID(registration.owner_id)) == registration.owner_id
    assert child is not preflight and retained_at_entry and work_scope is scope
    assert type(value.secret) is bytes and len(value.secret) == 32 and value.secret != SECRET
    assert registration.capability_sha256 == agent_retirement_capability_digest(
        value.agent.claim, registration.owner_id, registration.idempotency_key, value.secret
    )
    assert request.registration == value.registrations[0].registration
    assert request.resources == value.receipts[-1]
    assert request.work.kind == kind
    assert (request.work.outcome is not None) == (kind == "launched-outcome-known")
    assert finalizer is not preflight and finalizer is not owner.worker
    assert isinstance(final_scope, ActualOperation) and final_scope is not scope
    assert scope.finished and not scope.failed and owner.fenced and not owner.failed
    assert final_scope.finished and not final_scope.failed
    assert request.resources.finalization.context.closure.initial == value.resources.initial
    assert request.resources.finalization.context.binding == value.resources.binding.request
    assert len(reports(value.resources)) == len(native_settlements(value)) == 1
    assert value.legacy.calls == []
    native = value.resources.agent
    query = StandingAgentSettlementQuery(
        schema_version="1.0",
        registration=request.registration,
        settlement_key=request.idempotency_key,
    )
    retained = await native.standing_settlement_lookup(query)
    assert retained is not None and retained.settlement == receipt.settlement
    assert await native.standing_unresolved() is None
    writes = value.journal_transport.paths(SETTLE)
    assert len(writes) == 1 and writes[0].content == request.canonical_bytes()
    assert writes[0].headers[CAPABILITY] == value.secret.hex()
    assert all(
        CAPABILITY not in sent.headers
        for sent in value.journal_transport.requests
        if sent.url.path != SETTLE
    )
    assert value.secret.hex().encode() not in request.canonical_bytes()
    assert all(
        body.closes == 1 and body.closed and not body.failed and not body.cancelled
        for body in value.journal_transport.bodies
    )
    return request, receipt


async def test_actual_normal_chain_registers_before_bind_model_admit_and_settles_known_outcome(
    settlement_chain,
):
    value = await settlement_chain()
    assert (await launch(value)).disposition == "created"
    await joined(value.agent.record.worker)
    truth = value.agent.record.task.model_dump(mode="json")
    request, _ = await positive(value)
    paths = [sent.url.path for sent in value.journal_transport.requests]
    assert paths.index(REGISTER) < paths.index(ADMIT)
    assert value.registrations[0].registration.request == value.registration_calls[0][0]
    outcome = request.work.outcome
    native = value.agent.env.store.entry(outcome.event_id)
    full = native.model_dump(mode="json")
    frozen_request = {
        key: full[key]
        for key in full
        if key not in {"event_id", "sequence", "origin", "created_at"}
    }
    assert outcome.request_sha256 == hashlib.sha256(canonical(frozen_request)).hexdigest()
    assert outcome.entry_sha256 == hashlib.sha256(canonical(full)).hexdigest()
    assert outcome.objective_revision == truth["objective_revision"]
    assert outcome.state == truth["state"] == "succeeded"
    assert (value.agent.workspace / "result.txt").read_text() == TEXT
    assert value.agent.record.task.model_dump(mode="json") == truth


async def test_original_model_close_blocks_settlement_even_after_terminal_projection(
    settlement_chain,
):
    value = await settlement_chain()
    value.model_close.clear()
    assert (await launch(value)).disposition == "created"
    provider = value.agent.made[0]
    await reached(provider.closing)
    rec = value.agent.record
    truth = (await value.agent.runtime.wait(value.agent.task_id, timeout=5)).model_dump(mode="json")
    assert truth["state"] == "succeeded" and not rec.worker.done()
    assert not rec.model_lease.settled and value.settlement_calls == []
    assert native_settlements(value) == []
    value.model_close.set()
    await joined(rec.worker)
    await positive(value)
    assert rec.task.model_dump(mode="json") == truth and provider.closed


@pytest.mark.parametrize(
    "committed", [False, True], ids=["before-append", "native-row-unconfirmed"]
)
async def test_unconfirmed_outcome_preserves_pending_truth_without_importing_native_history(
    settlement_chain, monkeypatch, committed
):
    value = await settlement_chain()
    journal = value.agent.journal
    append, read = journal.append, journal.read
    attempts, committed_entries = [], []

    async def fail_append(request):
        if request.kind != "agent.outcome":
            return await append(request)
        attempts.append(request.model_dump(mode="json"))
        if committed:
            committed_entries.append(await append(request))
        raise JournalError("Synthetic unconfirmed original outcome")

    async def fail_read(**kwargs):
        page = await read(**kwargs)
        if kwargs.get("kinds") == ["agent.outcome"]:
            raise JournalError("Synthetic unavailable outcome readback")
        return page

    monkeypatch.setattr(journal, "append", fail_append)
    monkeypatch.setattr(journal, "read", fail_read)
    assert (await launch(value)).disposition == "created"
    await joined(value.agent.record.worker)
    rec = value.agent.record
    truth = (rec.task.model_dump(mode="json"), rec.completion_pending, rec.done.is_set())
    assert truth[0]["state"] == "paused" and truth[1] and len(attempts) == 1
    request, _ = await positive(value, "launched-outcome-unconfirmed")
    assert request.work.outcome is None
    assert (rec.task.model_dump(mode="json"), rec.completion_pending, rec.done.is_set()) == truth
    native = value.agent.env.store.read(kinds=["agent.outcome"])
    assert len(native) == len(committed_entries) == int(committed)
    assert len(attempts) == 1 and rec.model_lease.settled


async def test_shutdown_before_any_outcome_attempt_can_retire_clean_original_worker(
    settlement_chain,
):
    value = await settlement_chain()
    value.generation.clear()
    assert (await launch(value)).disposition == "created"
    await reached(value.agent.made[0].generating)
    rec = value.agent.record
    assert value.agent.env.store.read(kinds=["agent.outcome"]) == []
    await joined(value.agent.runtime.close())
    value.agent.runtime_closed = True
    truth = rec.task.model_dump(mode="json")
    request, _ = await positive(value, "launched-outcome-unconfirmed")
    assert request.work.outcome is None and truth["state"] == "paused"
    assert rec.worker.done() and rec.model_lease.settled
    assert value.agent.env.store.read(kinds=["agent.outcome"]) == []
    assert rec.task.model_dump(mode="json") == truth


@pytest.mark.parametrize("phase", ["acquire", "read", "close"])
async def test_registration_caller_cancel_retains_original_ack_without_launch_or_false_unbound_proof(
    settlement_chain, phase
):
    value = await settlement_chain()
    gate = gate_for(value, REGISTER, phase)
    public = launch(value)
    await reached(gate.entered)
    exchange = exchange_for(value, gate)
    preflight, scope = original_scope(value)
    registration, child, child_scope, retained_at_entry = value.registration_calls[0]
    assert child is not public and child is not preflight and retained_at_entry
    assert child in scope.handles
    assert child_scope is scope and exchange.actual_owner is scope and exchange in scope.handles
    assert value.agent.made == [] and entries(value.agent) == []
    assert value.transport.paths(BIND) == [] and value.agent.transport.paths(ADMIT) == []
    public.cancel()
    with pytest.raises(asyncio.CancelledError):
        await public
    assert not child.done() and not exchange.task.done() and not scope.finished
    assert value.registration_reads == [] and value.settlement_calls == []
    gate.release.set()
    await reached(value.registered)
    owner = await retired_original(value)
    assert value.registrations[0].registration.request == registration
    assert exchange.task.done() and exchange.close_task.done() and not scope.failed
    assert value.agent.made == [] and entries(value.agent) == []
    assert value.transport.paths(BIND) == [] and reports(value.resources) == []
    assert value.settlement_calls == [] and native_settlements(value) == []
    assert owner.fenced and not owner.failed and scope.finished
    assert value.agent.runtime.workspace_operation_state()["cleanup_failed"] == 0
    assert await value.resources.agent.standing_unresolved() == value.agent.claim


def lose_completed_ack(value, monkeypatch, path):
    delegate = value.journal_transport.delegate
    send = delegate.handle_async_request
    completed = []

    async def lose(request):
        response = await send(request)
        if request.url.path == path and not completed:
            raw = await response.aread()
            await response.aclose()
            assert response.is_closed and response.status_code == 200
            completed.append(raw)
            raise httpx.ReadError("Synthetic lost completed native ACK", request=request)
        return response

    monkeypatch.setattr(delegate, "handle_async_request", lose)
    return completed


async def test_lost_registration_ack_uses_exact_readonly_reconciliation_without_second_write(
    settlement_chain, monkeypatch
):
    value = await settlement_chain()
    completed = lose_completed_ack(value, monkeypatch, REGISTER)
    assert (await launch(value)).disposition == "created"
    await joined(value.agent.record.worker)
    request, _ = await positive(value)
    original = value.registration_calls[0][0].canonical_bytes()
    assert completed and value.registration_reads == [original]
    assert len(value.journal_transport.paths(REGISTER)) == 1
    assert request.registration == value.registrations[0].registration


async def test_readonly_foreign_created_history_does_not_adopt_its_actual_running_worker(
    settlement_chain, tmp_path
):
    value = await settlement_chain()
    value.generation.clear()
    accepted = await launch(value)
    await reached(value.agent.made[0].generating)
    boundary = Transport(value.agent.env.app)
    journal = JournalClient("http://standing.test", "a" * 32, transport=boundary)
    resources = StandingResourceClient(
        "http://127.0.0.1:8764",
        "a" * 32,
        transport=httpx.ASGITransport(app=value.resources.http._transport.app),
    )
    provider = MeasuredFiles()
    runtime = AgentRuntime(
        journal,
        provider,
        tmp_path,
        ToolPolicy(tmp_path, allow_write=True),
        standing_resource_client=resources,
        standing_protocol=2,
    )
    try:
        retained = await runtime.submit_standing(value.agent.original, capability=SECRET)
        assert retained.disposition == "retained" and retained.proof == accepted.proof
        assert runtime._tasks == {} and provider.calls == []
        assert boundary.paths(REGISTER) == boundary.paths(SETTLE) == []
        assert value.settlement_calls == [] and not value.agent.record.worker.done()
        assert await value.resources.agent.standing_unresolved() == value.agent.claim
        value.generation.set()
        await joined(value.agent.record.worker)
        await positive(value)
        assert len(value.agent.made) == 1
    finally:
        value.generation.set()
        await joined(runtime.close())
        await joined(journal.close())
        await joined(resources.close())
        assert journal.http.is_closed and resources.http.is_closed


async def test_late_created_admission_is_created_unlaunched_and_never_generates(settlement_chain):
    value = await settlement_chain()
    gate = gate_for(value, ADMIT, "close")
    value.model_close.clear()
    public = launch(value)
    await reached(gate.entered)
    public.cancel()
    with pytest.raises(asyncio.CancelledError):
        await public
    gate.release.set()
    await reached(value.created_ack_observed)
    await reached(value.agent.made[0].closing)
    assert value.settlement_calls == [] and value.agent.runtime._tasks == {}
    value.model_close.set()
    request, _ = await positive(value, "created-unlaunched")
    assert request.resources.finalization.context.closure.initial == value.resources.initial
    assert value.agent.made[0].calls == [] and value.agent.runtime._tasks == {}
    assert not (value.agent.workspace / "result.txt").exists()


async def test_bound_never_admitted_has_literal_null_and_retains_original_session_close(
    settlement_chain,
):
    value = await settlement_chain()
    gate = Gate("/v1/sessions/" + value.agent.original.task.task_session.session_id, "close")
    value.session_gate = gate
    value.gates.append(gate)
    public = launch(value)
    await reached(gate.entered)
    exchange = exchange_for(value, gate)
    public.cancel()
    with pytest.raises(asyncio.CancelledError):
        await public
    assert value.resources.binding is not None and not exchange.close_task.done()
    assert value.settlement_calls == [] and value.agent.made == [] and entries(value.agent) == []
    gate.release.set()
    request, _ = await positive(value, "bound-never-admitted")
    assert request.resources.finalization.context.closure.initial is None
    assert b'"initial":null' in request.canonical_bytes()
    assert value.agent.runtime._tasks == {} and exchange.close_task.done()


async def test_failed_actual_model_close_never_releases_native_claim_even_after_later_success(
    settlement_chain, monkeypatch
):
    value = await settlement_chain()
    factory = value.agent.runtime.model_settings.host_factory

    def broken(effort):
        provider = factory(effort)
        provider.close_error = ProviderCleanupError("Synthetic original model close failure")
        return provider

    monkeypatch.setattr(value.agent.runtime.model_settings, "host_factory", broken)
    assert (await launch(value)).disposition == "created"
    await joined(value.agent.record.worker)
    owner = await retired_original(value)
    provider, rec = value.agent.made[0], value.agent.record
    truth = rec.task.model_dump(mode="json")
    assert rec.model_lease.failed and not rec.model_lease.settled and owner.failed
    await provider.close()
    assert provider.closed and rec.model_lease.failed
    assert value.settlement_calls == [] and native_settlements(value) == []
    assert (
        reports(value.resources) == []
        and value.agent.runtime.workspace_operation_state()["cleanup_failed"]
    )
    assert await value.resources.agent.standing_unresolved() == value.agent.claim
    assert rec.task.model_dump(mode="json") == truth


async def test_original_external_control_sole_close_precedes_outcome_settlement(
    settlement_chain, monkeypatch
):
    value = await settlement_chain()
    value.generation.clear()
    assert (await launch(value)).disposition == "created"
    await reached(value.agent.made[0].generating)
    gate = Gate("/v1/transcript/entries", "close")
    value.gates.append(gate)
    delegate = value.journal_transport.delegate
    send = delegate.handle_async_request

    async def target(request):
        response = await send(request)
        if request.url.path == gate.path:
            body = json.loads(request.content)
            if body.get("kind") == "agent.command" and body["payload"].get("op") == "interrupt":
                value.journal_transport.gate = gate
        return response

    monkeypatch.setattr(delegate, "handle_async_request", target)
    public = asyncio.create_task(
        value.agent.runtime.interrupt(
            value.agent.task_id,
            InterruptRequest(expected_objective_revision=1, idempotency_key="settlement-interrupt"),
        )
    )
    value.public.append(public)
    await reached(gate.entered)
    exchange = exchange_for(value, gate)
    await joined(value.agent.record.worker)
    assert value.agent.record.model_lease.settled and not exchange.close_task.done()
    assert value.settlement_calls == [] and native_settlements(value) == []
    gate.release.set()
    await joined(public)
    request, _ = await positive(value)
    assert request.work.outcome.state == "cancelled"
    assert exchange.close_task.done() and exchange.close_task.exception() is None


@pytest.mark.parametrize("phase", ["acquire", "read", "close"])
async def test_shutdown_cancel_keeps_original_settlement_exchange_and_dependencies(
    settlement_chain, phase
):
    value = await settlement_chain()
    gate = gate_for(value, SETTLE, phase)
    assert (await launch(value)).disposition == "created"
    await reached(gate.entered)
    exchange = exchange_for(value, gate)
    request, finalizer, scope = value.settlement_calls[0]
    assert exchange.actual_owner is scope and exchange in scope.handles
    assert not finalizer.done() and not value.agent.journal.http.is_closed
    assert len(native_settlements(value)) == 1
    close = asyncio.create_task(value.agent.runtime.close())
    value.public.append(close)
    await asyncio.sleep(0)
    close.cancel()
    await joined(asyncio.gather(close, return_exceptions=True), 8)
    assert not exchange.task.done() and not value.agent.journal.http.is_closed
    assert not value.client.http.is_closed and value.settlement_reads == []
    assert value.agent.runtime.workspace_operation_state()["cleanup_pending"] > 0
    gate.release.set()
    await positive(value)
    await joined(value.agent.runtime.close())
    value.agent.runtime_closed = True
    assert exchange.task.done() and exchange.close_task.done()
    assert value.agent.journal.http.is_closed and value.client.http.is_closed
    assert value.settlement_calls[0][0].canonical_bytes() == request.canonical_bytes()


async def test_completed_settlement_ack_loss_reconciles_original_key_without_restore_or_launch(
    settlement_chain, monkeypatch
):
    value = await settlement_chain()
    completed = lose_completed_ack(value, monkeypatch, SETTLE)
    assert (await launch(value)).disposition == "created"
    await joined(value.agent.record.worker)
    request, receipt = await positive(value)
    assert completed and len(value.settlement_reads) == 1
    query = StandingAgentSettlementQuery.from_json(value.settlement_reads[0])
    assert (
        query.registration == request.registration
        and query.settlement_key == request.idempotency_key
    )
    assert receipt.disposition == "retained"
    before = physical_calls(value.resources)
    retained = await submit(value.agent)
    assert retained.disposition == "retained" and len(value.agent.made) == 1
    assert physical_calls(value.resources) == before and len(native_settlements(value)) == 1


@pytest.mark.parametrize("phase", ["fail-close", "cooperative-close"], ids=["failed", "cancelled"])
async def test_actual_failed_settlement_close_cannot_be_replaced_by_retained_positive_history(
    settlement_chain, phase
):
    value = await settlement_chain()
    gate = gate_for(value, SETTLE, phase)
    assert (await launch(value)).disposition == "created"
    await reached(gate.entered)
    exchange = exchange_for(value, gate)
    assert len(native_settlements(value)) == 1
    if phase == "cooperative-close":
        gate.close_task.cancel()
    else:
        gate.release.set()
    owner = await retired_original(value)
    assert gate.close_task.done() and (gate.close_task.cancelled() or gate.close_task.exception())
    assert value.agent.journal._submit_cleanup_failed and owner.failed
    assert value.journal_transport.paths(SETTLEMENT_LOOKUP) == [] and value.settlements == []
    assert value.agent.runtime.workspace_operation_state()["cleanup_failed"] > 0
    assert exchange.task.done() and len(value.journal_transport.paths(SETTLE)) == 1
    assert value.agent.record.model_lease.settled


async def next_claim(value):
    env, original = value.agent.env, value.agent.original
    old = env.app.state.standing_owner.snapshot(original.responsibility_id).record
    raw = old.receipt.request.model_dump(mode="json")
    raw.update(
        expected_revision=0,
        idempotency_key="settlement-next-responsibility",
        responsibility_id="23900000-0000-4000-8000-000000000102",
    )
    grant = StandingChange.model_validate(raw)
    response = await env.client.post(
        "/v1/standing/changes",
        content=grant.canonical_bytes(),
        headers=headers(**{"Content-Type": "application/json"}),
    )
    checked(response, 200, StandingChangeReceipt).validate_request(grant)
    record = env.app.state.standing_owner.snapshot(grant.responsibility_id).record
    raw = original.model_dump(mode="json")
    raw.update(
        idempotency_key="settlement-next-original",
        responsibility_id=grant.responsibility_id,
        authorization_revision=record.receipt.revision,
        authorization_digest=record.receipt.authorization_digest,
        record_digest=record.record_digest,
    )
    raw["task"]["task_id"] = "23900000-0000-4000-8000-000000000104"
    raw["capability_sha256"] = capability_digest(
        original.expected_backend, raw["idempotency_key"], original.executor_id, SECRET
    )
    original = StandingExecutionIntent.model_validate(raw)
    return original, (await value.resources.agent.standing_prepare(original)).validate_request(
        original
    ).claim


@pytest.mark.parametrize("replay", ["settlement", "readonly-submit"])
async def test_settled_a_replay_does_not_clear_new_active_b(settlement_chain, replay):
    value = await settlement_chain()
    assert (await launch(value)).disposition == "created"
    await joined(value.agent.record.worker)
    request, receipt = await positive(value)
    _, other = await next_claim(value)
    before = physical_calls(value.resources)
    if replay == "settlement":
        result = await value.resources.agent.standing_settle(request, secret32=value.secret)
        assert result.disposition == "retained" and result.settlement == receipt.settlement
    else:
        result = await submit(value.agent)
        assert result.disposition == "retained" and result.proof == value.resources.initial
    assert await value.resources.agent.standing_unresolved() == other
    assert physical_calls(value.resources) == before and len(value.agent.made) == 1
    assert len(native_settlements(value)) == 1


async def test_actual_reserved_lease_restores_once_before_agent_settlement(settlement_chain):
    value = await settlement_chain()
    value.generation.clear()
    assert (await launch(value)).disposition == "created"
    await reached(value.agent.made[0].generating)
    process = await ready(value.resources)
    result = await handoff(value.resources)
    assert result["ok"] and process.returncode is not None
    reservation = recorded_reservation(value.resources)
    value.generation.set()
    await joined(value.agent.record.worker)
    request, _ = await positive(value)
    report = request.resources.finalization
    assert report.context.observation.reservation == reservation
    assert (
        report.resource_disposition == "matching-lease-finalized"
        and report.restoration == "restored"
    )
    before = physical_calls(value.resources)
    await joined(value.agent.runtime.close())
    value.agent.runtime_closed = True
    assert physical_calls(value.resources) == before


async def test_replaced_journal_cannot_substitute_for_entered_original_settlement_or_closer(
    settlement_chain,
):
    value = await settlement_chain()
    gate = gate_for(value, SETTLE, "close")
    value.journal_transport.close_release.clear()
    assert (await launch(value)).disposition == "created"
    await reached(gate.entered)
    original = value.agent.journal
    exchange = exchange_for(value, gate)
    other_transport = Transport(value.agent.env.app)
    other = JournalClient("http://standing.test", "a" * 32, transport=other_transport)
    value.agent.runtime.journal = other
    close = asyncio.create_task(value.agent.runtime.close())
    value.public.append(close)
    try:
        await joined(asyncio.gather(close, return_exceptions=True), 8)
        assert not original.http.is_closed and not exchange.close_task.done()
        assert value.journal_transport.close_calls == 0 and other_transport.paths(SETTLE) == []
        gate.release.set()
        await positive(value)
        await reached(value.journal_transport.close_entered)
        closer = value.journal_transport.close_owner
        assert closer is not exchange.close_task and not closer.done()
        assert value.journal_transport.close_calls == 1 and not original.http.is_closed
        assert value.agent.runtime.workspace_operation_state()["cleanup_pending"] > 0
        value.journal_transport.close_release.set()
        await joined(closer)
        await joined(value.agent.runtime.close())
        value.agent.runtime_closed = True
        assert original.http.is_closed and other.http.is_closed
        assert value.journal_transport.close_calls == other_transport.close_calls == 1
        assert other_transport.paths(REGISTER) == other_transport.paths(SETTLE) == []
    finally:
        gate.release.set()
        value.journal_transport.close_release.set()
        await joined(other.close())


async def test_caller_mutation_during_registration_cannot_change_frozen_owner_or_outcome(
    settlement_chain,
):
    value = await settlement_chain()
    gate = gate_for(value, REGISTER, "close")
    original = value.agent.original.task
    objective = original.objective
    public = launch(value)
    try:
        await reached(gate.entered)
        frozen = value.registration_calls[0][0].canonical_bytes()
        original.__dict__["objective"] = "Synthetic changed caller input after original freeze"
        gate.release.set()
        assert (await public).disposition == "created"
        await joined(value.agent.record.worker)
        request, _ = await positive(value)
        assert request.registration.request.canonical_bytes() == frozen
        assert request.registration.request.claim.request.task.objective == objective
        native = value.agent.env.store.entry(request.work.outcome.event_id)
        assert native.payload["task"]["objective"] == objective
        assert original.objective != objective
    finally:
        original.__dict__["objective"] = objective
        gate.release.set()
