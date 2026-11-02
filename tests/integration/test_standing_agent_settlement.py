"""Independent native-v2 acceptance from the frozen public settlement protocol.

No native settlement candidate or new Agent client implementation was read.
The existing physical fixture supplies real SQLite/ASGI Resource reports and a
disposable CPU/protocol worker when acquisition is requested. Its synthetic
engine performs no inference. Hand-authored trusted Agent testimony here tests
native verification, not genuine Agent-owner retirement or permission to work.
H64/H+1, worst valid graphs/storage and ordinary-Session availability remain
separate required gates; this bounded suite does not qualify their capacity.

Root source-aware fixture calibration: the shared old execution factory was
explicitly pinned to v1 after these oracles froze. This local factory preserves
their intended genuine default-v2 composition without changing any assertion.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from test_agent_export_admission import export_request
from test_agent_submit_admission import proposal as ordinary_proposal
from test_standing_dispatch_api import AUTH, headers, rows
from test_standing_execution_api import BodyWitness, close_env, original
from test_standing_execution_owner import build_native as build_native
from test_standing_physical_finalization import (
    Gate,
    Native,
    begin,
    bound,
    close_resources,
    closure,
    handoff,
    ready,
    recorded_reservation,
    reports,
)
from test_standing_physical_finalization import physical as physical
from test_standing_resource_finalization_api import make_report

from packages.contracts.models import (
    AppendRequest,
    JournalEntry,
    ProviderInfo,
    SessionRequest,
    Task,
)
from packages.contracts.standing_agent_settlement import (
    StandingAgentOutcomeReference,
    StandingAgentRetirementRegistrationReceipt,
    StandingAgentRetirementRegistrationRequest,
    StandingAgentRetirementWork,
    StandingAgentSettlementQuery,
    StandingAgentSettlementReceipt,
    StandingAgentSettlementRequest,
    agent_retirement_capability_digest,
    agent_retirement_registration_digest,
    agent_settlement_digest,
)
from packages.contracts.standing_dispatch import StandingDispatchState
from packages.contracts.standing_execution import (
    StandingExecutionClaimAdmission,
    StandingExecutionIntent,
    capability_digest,
    standing_initial_request,
)
from packages.contracts.standing_resource_finalization import (
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeContext,
)
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeReceipt,
    StandingConflict,
    StandingResponsibilitySnapshot,
)
from services.session.api import create_app
from services.session.standing_admission import StandingTaskOwner
from services.session.standing_resource_finalization import StandingResourceFinalizationOwner
from services.session.standing_resources import StandingResourceOwner
from services.session.store import Store

REGISTER = "/v1/standing/tasks/retirement/register"
REGISTRATION = "/v1/standing/tasks/retirement/registration/lookup"
SETTLE = "/v1/standing/tasks/settle"
LOOKUP = "/v1/standing/tasks/settlement/lookup"
ROUTES = (REGISTER, REGISTRATION, SETTLE, LOOKUP)
HEADER = "X-Kinaiya-Standing-Settlement-Capability"
CLAIM_HEADER = "X-Kinaiya-Standing-Capability"
CLAIM_SECRET = bytes(range(32))
SECRET = bytes(range(32, 64))  # Public, independently purpose-separated fixture.
OWNER = "23900000-0000-4000-8000-000000000001"
OTHER = "23900000-0000-4000-8000-000000000002"
MARKER = "SYNTHETIC-PRIVATE-NATIVE-SETTLEMENT-INPUT"
WORK = (
    "launched-outcome-known",
    "launched-outcome-unconfirmed",
    "created-unlaunched",
    "bound-never-admitted",
)


@pytest.fixture
async def factory(tmp_path):
    opened = []

    async def make(path=None):
        path = tmp_path / f"native-settlement-{len(opened)}.sqlite" if path is None else path
        store = Store(path)
        sessions = store.sessions()
        session = sessions[0] if sessions else store.create_session(SessionRequest(channel="agent"))
        wall = SimpleNamespace(value=datetime(2026, 11, 1, 11, 59, 50, tzinfo=UTC))
        mono = SimpleNamespace(value=1000)
        # Intentionally omit the selector: this qualifies the production default.
        app = create_app(
            store, AUTH, standing_clock=lambda: wall.value, standing_monotonic_ms=lambda: mono.value
        )
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
        return env

    try:
        yield make
    finally:
        for env in reversed(opened):
            await close_env(env)


def wire(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def rehash(body, field, domain):
    body[field] = hashlib.sha256(
        domain.encode() + b"\0" + wire({k: v for k, v in body.items() if k != field})
    ).hexdigest()
    return body


def response(reply, status, kind=None, request=None):
    assert reply.status_code == status, (reply.status_code, reply.text)
    assert reply.headers.get("cache-control") == "no-store"
    assert MARKER not in reply.text and SECRET.hex() not in reply.text
    assert CLAIM_SECRET.hex() not in reply.text
    assert reply.is_closed
    if status != 200:
        assert len(reply.content) <= 32768
        return None
    assert reply.headers["content-type"].split(";", 1)[0] == "application/json"
    if kind is None:
        assert reply.content == b"null"
        return None
    assert len(reply.content) <= kind.wire_bytes
    value = kind.from_json(reply.content)
    return value.validate_request(request) if request is not None else value


async def post(env, path, body, *, secret=None, role="agent", extra=None, client=None):
    custom = {"Content-Type": "application/json", **(extra or {})}
    if secret is not None:
        custom[HEADER] = secret.hex()
    raw = body.canonical_bytes() if hasattr(body, "canonical_bytes") else wire(body)
    return await (client or env.client).post(path, content=raw, headers=headers(role, **custom))


def registration(claim, *, owner=OWNER, key="native-retirement:original", secret=SECRET):
    commitment = agent_retirement_capability_digest(claim, owner, key, secret)
    return StandingAgentRetirementRegistrationRequest(
        schema_version="1.0",
        claim=claim,
        idempotency_key=key,
        owner_id=owner,
        capability_sha256=commitment,
        registration_digest=agent_retirement_registration_digest(claim, key, owner, commitment),
    )


async def register(value, *, request=None):
    request = request or registration(value.claim)
    assert await value.agent.standing_task_lookup(value.packet) is None
    assert value.starts == [] and not value.effects_entered.is_set()
    receipt = response(
        await post(value.session, REGISTER, request),
        200,
        StandingAgentRetirementRegistrationReceipt,
        request,
    )
    assert receipt.disposition == "created"
    assert receipt.registration.request == request
    assert receipt.registration.claim_origin == "fresh-v2-prepared"
    assert receipt.registration.backend_id == value.claim.request.expected_backend
    assert receipt.registration.registered_instance == value.claim.request.expected_instance
    return SimpleNamespace(request=request, receipt=receipt)


def settlement(
    registration_record, resources, *, kind, reference=None, key="native-settle:original"
):
    work = StandingAgentRetirementWork(schema_version="1.0", kind=kind, outcome=reference)
    return StandingAgentSettlementRequest(
        schema_version="1.0",
        idempotency_key=key,
        registration=registration_record,
        resources=resources,
        work=work,
        settlement_digest=agent_settlement_digest(registration_record, resources, work, key),
    )


def query(request):
    return StandingAgentSettlementQuery(
        schema_version="1.0",
        registration=request.registration,
        settlement_key=request.idempotency_key,
    )


async def outcome(value, *, state="succeeded", revision=1, fault=None):
    initial = standing_initial_request(value.claim)
    task = Task(
        schema_version="1.0",
        task_id=value.packet.task.task_id,
        session_id=value.packet.task.task_session.session_id,
        thread_id=value.packet.task.task_session.thread_id,
        epoch=value.packet.task.task_session.epoch,
        objective=value.packet.task.objective
        + (" Owner steering revision two." if revision == 2 else ""),
        objective_revision=revision,
        state=state,
        content_scope=value.packet.task.content_scope,
        summary="Synthetic native outcome only",
        evidence=[],
        remaining_required_work=[],
        source_event_ids=[],
        idempotency_key=initial.payload["idempotency_key"],
        harness_version=value.packet.task.harness_version,
        model_pin=value.packet.task.model_pin.model_dump(mode="json"),
        auxiliary_models={},
        workspace_pin=value.packet.task.workspace_pin,
        error="Synthetic known failure" if state == "failed" else None,
    )
    snapshot = task.model_dump(mode="json")
    if fault == "model":
        snapshot["model_pin"]["model"] += ":foreign"
    elif fault == "workspace":
        snapshot["workspace_pin"]["root_identity"] = "f" * 64
    elif fault == "harness":
        snapshot["harness_version"] = "v-" + "f" * 24
    elif fault == "standing-key":
        snapshot["idempotency_key"] += ":foreign"
    payload = {
        "task_id": task.task_id,
        "session_id": task.session_id,
        "thread_id": task.thread_id,
        "epoch": task.epoch,
        "objective": task.objective,
        "objective_revision": True if fault == "mirrored-revision" else task.objective_revision,
        "idempotency_key": task.idempotency_key,
        "source_event_ids": [],
        "content_scope": task.content_scope,
        "evidence": [],
        "task": snapshot,
        "state": "failed" if fault == "state" else state,
        "plugin_versions": {"synthetic-literal": "v1"},
        "accepted_extra": {"finite_float": 1.25, "large_integer": 10**20},
    }
    request = AppendRequest(
        session_id=task.session_id,
        thread_id=task.thread_id,
        epoch=task.epoch,
        kind="agent.outcome",
        payload=payload,
        causes=[],
        task_refs=[task.task_id],
        idempotency_key=f"native-outcome:{task.task_id}:{fault or state}:{revision}",
    )
    if fault == "origin":
        reply = await value.session.client.post(
            "/v1/transcript/entries", json=request.model_dump(mode="json"), headers=headers("owner")
        )
        assert reply.status_code == 200
        entry = JournalEntry.model_validate(reply.json())
        assert entry.origin == "authenticated-user"
    else:
        entry = await value.agent.append(request)
        assert entry.origin == "agent-supervisor"
    assert entry.sequence > value.initial.sequence
    assert value.session.store.entry(entry.event_id) == entry
    reference = StandingAgentOutcomeReference(
        schema_version="1.0",
        task_id=task.task_id,
        event_id=entry.event_id,
        sequence=entry.sequence,
        objective_revision=revision,
        state=state,
        request_sha256=hashlib.sha256(wire(request.model_dump(mode="json"))).hexdigest(),
        entry_sha256=hashlib.sha256(wire(entry.model_dump(mode="json"))).hexdigest(),
    )
    return SimpleNamespace(request=request, entry=entry, reference=reference)


async def actual_report(value):
    await closure(value)
    result = await asyncio.wait_for(asyncio.shield(begin(value)), 25)
    assert type(result) is StandingResourceFinalizationReceipt
    result = result.validate_request(value.request)
    assert result.finalization.context.binding == value.binding.request
    assert result.finalization.context.closure.initial == value.initial
    stored = await value.agent.standing_resource_finalization_lookup(value.request)
    assert stored is not None and stored.finalization == result.finalization
    assert len(reports(value)) == 1
    return result


@pytest.fixture
async def prepared_settlement(physical, record_property):
    originals = []

    async def make(
        *,
        kind="created-unlaunched",
        acquired=False,
        state="succeeded",
        revision=1,
        fault=None,
        session=None,
        claim=None,
    ):
        value = await physical(session=session, claim=claim)
        reg = await register(value)
        await bound(value, admitted=kind != "bound-never-admitted")
        worker, captured = None, None
        if kind == "launched-outcome-known":
            worker = asyncio.create_task(
                outcome(value, state=state, revision=revision, fault=fault)
            )
            value.tasks.append(worker)
            originals.append((kind, worker))
            captured = await worker
            assert worker.done() and not worker.cancelled() and worker.exception() is None
        elif kind == "launched-outcome-unconfirmed":
            started = asyncio.Event()

            async def before_outcome():
                started.set()
                await asyncio.Event().wait()

            worker = asyncio.create_task(before_outcome())
            value.tasks.append(worker)
            originals.append((kind, worker))
            await asyncio.wait_for(started.wait(), 5)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            assert worker.done() and worker.cancelled()
        reservation = None
        if acquired:
            process = await ready(value)
            result = await asyncio.wait_for(asyncio.shield(handoff(value)), 25)
            assert result["ok"] and result["policy"]["phase"] == "local_work"
            reservation = recorded_reservation(value)
            assert process.returncode is not None
            evidence = value.coordinator.manager._workers["whistle"].stop_evidence
            assert evidence["reaped"] and evidence["group_empty"]
        report = await actual_report(value)
        if acquired:
            assert report.finalization.resource_disposition == "matching-lease-finalized"
            assert report.finalization.reserved_lease_id == reservation.lease_id
            assert report.finalization.restoration == "restored"
            assert value.coordinator.manager.owned_identity("whistle") is not None
        request = settlement(
            reg.receipt.registration,
            report,
            kind=kind,
            reference=None if captured is None else captured.reference,
        )
        return SimpleNamespace(
            value=value, reg=reg, report=report, outcome=captured, request=request, worker=worker
        )

    try:
        yield make
    finally:
        record_property(
            "native_settlement_synthetic_worker_handles",
            json.dumps(
                [
                    {"kind": kind, "done": worker.done(), "cancelled": worker.cancelled()}
                    for kind, worker in originals
                ],
                sort_keys=True,
            ),
        )


async def commit(data):
    value, request = data.value, data.request
    before_effects = (list(value.starts), list(value.stops))
    receipt = response(
        await post(value.session, SETTLE, request, secret=SECRET),
        200,
        StandingAgentSettlementReceipt,
        request,
    )
    assert receipt.disposition == "created"
    assert receipt.settlement.request == request
    assert receipt.settlement.backend_id == value.claim.request.expected_backend
    assert receipt.settlement.settled_instance == value.claim.request.expected_instance
    assert receipt.settlement.request.resources == data.report
    assert (value.starts, value.stops) == before_effects
    assert await value.agent.standing_unresolved() is None
    looked = response(
        await post(value.session, LOOKUP, query(request)), 200, StandingAgentSettlementReceipt
    )
    assert looked.validate_query(query(request)).settlement == receipt.settlement
    return receipt


@pytest.fixture
async def legacy_app(build_native):
    opened = []

    async def make(*, prepared=False, admitted=False):
        native = build_native()
        claims = native.ledger()
        tasks = StandingTaskOwner(
            native.store, native.control, native.owner, native.dispatch, claims
        )
        tasks.register()
        resources = StandingResourceOwner(native.store, native.control, tasks)
        resources.register()
        finalizations = StandingResourceFinalizationOwner(
            native.store, native.control, tasks, resources
        )
        resources.register_finalization_owner(finalizations)
        finalizations.register()
        native.ledgers.extend((tasks, resources, finalizations))
        packet = native.request()
        claim = claims.prepare(packet).claim if prepared else None
        initial = tasks.admit(packet, capability=CLAIM_SECRET).proof if admitted else None
        old_record = native.owner.snapshot(packet.responsibility_id).record
        app = create_app(
            native.store, AUTH, standing_clock=native.wall, standing_monotonic_ms=native.mono
        )
        lifespan = app.router.lifespan_context(app)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://standing.test"
        )
        env = SimpleNamespace(
            app=app,
            store=native.store,
            session=native.session,
            client=client,
            wall=native.wall,
            mono=native.mono,
            native=native,
            packet=packet,
            claim=claim,
            initial=initial,
            old_record=old_record,
            lifespan=lifespan,
            entered=False,
        )
        opened.append(env)
        await lifespan.__aenter__()
        env.entered = True
        return env

    try:
        yield make
    finally:
        for env in reversed(opened):
            await env.client.aclose()
            if env.entered:
                await env.lifespan.__aexit__(None, None, None)
            assert env.client.is_closed and not env.store.db.in_transaction


async def next_packet(env, previous, *, suffix="b", task_id="23900000-0000-4000-8000-000000000020"):
    day = 2 if suffix == "b" else 3
    env.wall.value = datetime(2026, 11, day, 11, 59, 50, tzinfo=UTC)
    state = StandingDispatchState.from_json(
        (await env.client.get("/v1/standing/dispatch", headers=headers())).content
    )
    saved = StandingResponsibilitySnapshot.from_json(
        (
            await env.client.get(
                "/v1/standing/responsibilities/" + previous.responsibility_id, headers=headers()
            )
        ).content
    ).record
    authorization = saved.receipt.request.authorization.model_dump(mode="json")
    authorization["schedule"]["start_local_date"] = f"2026-11-0{day}"
    change = StandingChange.model_validate(
        {
            "schema_version": "1.0",
            "idempotency_key": "settlement:grant:" + suffix,
            "expected_backend": state.backend_id,
            "expected_instance": state.instance_id,
            "expected_revision": 0,
            "responsibility_id": "23900000-0000-4000-8000-0000000000"
            + ("21" if suffix == "b" else "22"),
            "authorization": authorization,
        }
    )
    result = response(
        await post(env, "/v1/standing/changes", change, role="owner"),
        200,
        StandingChangeReceipt,
        change,
    )
    record = StandingResponsibilitySnapshot.from_json(
        (
            await env.client.get(
                "/v1/standing/responsibilities/" + result.request.responsibility_id,
                headers=headers(),
            )
        ).content
    ).record
    env.wall.value = datetime(2026, 11, day, 12, 0, 10, tzinfo=UTC)
    raw = previous.model_dump(mode="json")
    key = "settlement:execution:" + suffix
    raw.update(
        idempotency_key=key,
        responsibility_id=record.receipt.request.responsibility_id,
        authorization_revision=record.receipt.revision,
        authorization_digest=record.receipt.authorization_digest,
        record_digest=record.record_digest,
        occurrence={
            "schema_version": "1.0",
            "local_date": f"2026-11-0{day}",
            "due_utc": f"2026-11-0{day}T12:00:00Z",
            "utc_offset_seconds": 0,
        },
        capability_sha256=capability_digest(
            previous.expected_backend, key, previous.executor_id, CLAIM_SECRET
        ),
    )
    raw["task"]["task_id"] = task_id
    return StandingExecutionIntent.model_validate(raw)


@pytest.mark.asyncio
async def test_default_app_exposes_all_four_v2_routes_on_one_registered_session(factory):
    env = await factory()
    paths = set(env.app.openapi()["paths"])
    assert set(ROUTES) <= paths
    packet = await original(env)
    claim = response(
        await post(env, "/v1/standing/executions/prepare", packet),
        200,
        StandingExecutionClaimAdmission,
        packet,
    ).claim
    request = registration(claim)
    response(await post(env, REGISTRATION, request), 200)
    receipt = response(
        await post(env, REGISTER, request), 200, StandingAgentRetirementRegistrationReceipt, request
    )
    assert (
        receipt.disposition == "created"
        and receipt.registration.claim_origin == "fresh-v2-prepared"
    )


@pytest.mark.asyncio
async def test_complete_empty_v1_upgrades_without_losing_old_owner_history(legacy_app):
    env = await legacy_app()
    old = StandingResponsibilitySnapshot.from_json(
        (
            await env.client.get(
                "/v1/standing/responsibilities/" + env.packet.responsibility_id, headers=headers()
            )
        ).content
    ).record
    assert old == env.old_record
    env.wall.value = datetime(2026, 11, 1, 11, 59, 50, tzinfo=UTC)
    packet = await original(env)
    claim = response(
        await post(env, "/v1/standing/executions/prepare", packet),
        200,
        StandingExecutionClaimAdmission,
        packet,
    ).claim
    receipt = response(
        await post(env, REGISTER, registration(claim)),
        200,
        StandingAgentRetirementRegistrationReceipt,
    )
    assert receipt.registration.claim_origin == "fresh-v2-prepared"
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", (False, True))
async def test_v1_historical_claim_is_uncovered_and_cannot_be_adopted(legacy_app, admitted):
    env = await legacy_app(prepared=True, admitted=admitted)
    request = registration(env.claim)
    before = rows(env.store)
    response(await post(env, REGISTRATION, request), 200)
    response(await post(env, REGISTER, request), 409)
    assert rows(env.store) == before
    claim = response(
        await post(env, "/v1/standing/executions/lookup", env.packet), 200, type(env.claim)
    )
    assert claim == env.claim
    unresolved = response(
        await env.client.get("/v1/standing/executions/unresolved", headers=headers("agent")),
        200,
        type(env.claim),
    )
    assert unresolved == env.claim
    reply = await post(env, "/v1/standing/tasks/lookup", env.packet)
    assert reply.status_code == 200
    assert (reply.json() is None) == (env.initial is None)
    if env.initial is not None:
        assert reply.json() == env.initial.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize("different", (False, True))
async def test_original_registration_race_never_transfers_winner_capability(physical, different):
    value = await physical()
    first = registration(value.claim)
    second = (
        registration(
            value.claim, owner=OTHER, key="native-retirement:other", secret=bytes(reversed(SECRET))
        )
        if different
        else first
    )
    originals = [
        asyncio.create_task(post(value.session, REGISTER, request)) for request in (first, second)
    ]
    value.tasks.extend(originals)
    replies = await asyncio.gather(*originals)
    assert all(
        task.done() and not task.cancelled() and task.exception() is None for task in originals
    )
    assert sorted(r.status_code for r in replies) == ([200, 409] if different else [200, 200])
    accepted = [
        (request, response(reply, 200, StandingAgentRetirementRegistrationReceipt, request))
        for request, reply in zip((first, second), replies, strict=True)
        if reply.status_code == 200
    ]
    assert sum(receipt.disposition == "created" for _, receipt in accepted) == 1
    if different:
        loser = second if accepted[0][0] == first else first
        response(await post(value.session, REGISTRATION, loser), 409)
    else:
        assert {receipt.disposition for _, receipt in accepted} == {"created", "retained"}
        assert accepted[0][1].registration == accepted[1][1].registration
    assert await value.agent.standing_unresolved() == value.claim
    assert value.initial is None and value.binding is None and value.provider.calls == []
    assert SECRET.hex() not in repr(rows(value.session.store))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("bind", "admit"))
async def test_v2_registration_is_required_before_bind_or_initial_admission(physical, operation):
    value = await physical()
    before = rows(value.session.store)
    if operation == "bind":
        with pytest.raises(StandingConflict):
            await value.coordinator.bind_standing_claim(value.claim)
    else:
        response(
            await post(
                value.session,
                "/v1/standing/tasks/admit",
                value.packet,
                extra={CLAIM_HEADER: CLAIM_SECRET.hex()},
            ),
            409,
        )
    assert rows(value.session.store) == before
    assert await value.agent.standing_task_lookup(value.packet) is None
    assert not value.effects_entered.is_set() and value.provider.calls == []


@pytest.mark.asyncio
async def test_lost_registration_ack_reads_exact_original_record_after_real_http_close(physical):
    value = await physical()
    request = registration(value.claim)
    transport = Native(value.session.app)
    transport.gate = Gate(REGISTER, "lost")
    async with httpx.AsyncClient(transport=transport, base_url="http://standing.test") as client:
        task = asyncio.create_task(post(value.session, REGISTER, request, client=client))
        with pytest.raises(httpx.ReadError):
            await task
        assert task.done() and not task.cancelled()
        (body,) = transport.bodies
        assert body.closes == 1 and body.closed and not body.failed and not body.cancelled
    assert transport.close_calls == 1
    before = rows(value.session.store)
    receipt = response(
        await post(value.session, REGISTRATION, request),
        200,
        StandingAgentRetirementRegistrationReceipt,
        request,
    )
    assert receipt.disposition == "retained"
    replay = response(
        await post(value.session, REGISTER, request),
        200,
        StandingAgentRetirementRegistrationReceipt,
        request,
    )
    assert replay.registration == receipt.registration and replay.disposition == "retained"
    assert rows(value.session.store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", WORK)
async def test_four_work_forms_need_genuine_native_resources_report(prepared_settlement, kind):
    data = await prepared_settlement(kind=kind, acquired=kind == "launched-outcome-known")
    assert (data.report.finalization.context.closure.initial is None) == (
        kind == "bound-never-admitted"
    )
    if kind == "bound-never-admitted":
        assert b'"initial":null' in data.request.canonical_bytes()
    response(await post(data.value.session, LOOKUP, query(data.request)), 200)
    result = await commit(data)
    before = rows(data.value.session.store)
    replay = response(
        await post(data.value.session, SETTLE, data.request, secret=SECRET),
        200,
        StandingAgentSettlementReceipt,
        data.request,
    )
    assert replay.disposition == "retained" and replay.settlement == result.settlement
    assert rows(data.value.session.store) == before
    if kind == "launched-outcome-unconfirmed":
        assert data.request.work.outcome is None
        assert not data.value.session.store.db.execute(
            "SELECT 1 FROM entries WHERE kind='agent.outcome'"
        ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ("succeeded", "failed", "cancelled"))
async def test_native_full_known_outcome_accepts_consistent_later_objective_revision(
    prepared_settlement, state
):
    data = await prepared_settlement(kind="launched-outcome-known", state=state, revision=2)
    assert data.outcome.request.payload["task"]["objective"] != data.value.packet.task.objective
    result = await commit(data)
    assert result.settlement.request.work.outcome == data.outcome.reference
    assert result.settlement.request.work.outcome.state == state


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", (CLAIM_SECRET, b"x" * 32))
async def test_wrong_purpose_or_wrong_exact_secret_cannot_release_active_claim(
    prepared_settlement, wrong
):
    data = await prepared_settlement()
    before = rows(data.value.session.store)
    response(await post(data.value.session, SETTLE, data.request, secret=wrong), 409)
    assert rows(data.value.session.store) == before
    assert await data.value.agent.standing_unresolved() == data.value.claim
    response(await post(data.value.session, LOOKUP, query(data.request)), 200)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    (
        "registration",
        "report",
        "request-hash",
        "entry-hash",
        "origin",
        "model",
        "workspace",
        "harness",
        "standing-key",
        "mirrored-revision",
        "state",
    ),
)
async def test_valid_wire_cannot_replace_actual_registration_report_or_full_native_outcome(
    prepared_settlement, fault
):
    data = await prepared_settlement(kind="launched-outcome-known", fault=fault)
    raw = data.request.model_dump(mode="json")
    if fault == "registration":
        raw["registration"]["registered_instance"] = OTHER
        rehash(
            raw["registration"],
            "record_digest",
            "kinaiya.standing.agent-retirement-registration-record.v1",
        )
    elif fault == "report":
        raw["resources"]["finalization"]["policy_revision"] += 1
        rehash(
            raw["resources"]["finalization"],
            "finalization_digest",
            "kinaiya.standing.resource-finalization.v1",
        )
    elif fault in {"request-hash", "entry-hash"}:
        raw["work"]["outcome"]["request_sha256" if fault == "request-hash" else "entry_sha256"] = (
            "0" * 64
        )
    rehash(raw, "settlement_digest", "kinaiya.standing.agent-settlement.v1")
    malicious = StandingAgentSettlementRequest.model_validate(raw)
    before = rows(data.value.session.store)
    response(await post(data.value.session, SETTLE, malicious, secret=SECRET), 409)
    assert rows(data.value.session.store) == before
    assert await data.value.agent.standing_unresolved() == data.value.claim
    response(await post(data.value.session, LOOKUP, query(data.request)), 200)


@pytest.mark.asyncio
async def test_context_and_pure_report_shape_are_not_native_physical_testimony(physical):
    value = await physical()
    reg = await register(value)
    await bound(value)
    await closure(value)
    context = response(
        await value.session.client.post(
            "/v1/standing/resources/finalization/context",
            content=value.request.canonical_bytes(),
            headers=headers("resources", **{"Content-Type": "application/json"}),
        ),
        200,
        StandingResourceFinalizeContext,
        value.request,
    )
    forged = StandingResourceFinalizationReceipt.model_validate(
        {
            "schema_version": "1.0",
            "disposition": "created",
            "finalization": make_report(context.model_dump(mode="json")),
        }
    )
    request = settlement(reg.receipt.registration, forged, kind="created-unlaunched")
    before = rows(value.session.store)
    response(await post(value.session, SETTLE, request, secret=SECRET), 409)
    assert rows(value.session.store) == before
    assert await value.agent.standing_unresolved() == value.claim
    assert await value.agent.standing_resource_finalization_lookup(value.request) is None
    assert reports(value) == []


@pytest.mark.asyncio
async def test_lost_settlement_ack_reconciles_immutable_record_without_repeat_restore(
    prepared_settlement,
):
    data = await prepared_settlement(acquired=True)
    value = data.value
    transport = Native(value.session.app)
    transport.gate = Gate(SETTLE, "lost")
    before_effects = (list(value.starts), list(value.stops))
    async with httpx.AsyncClient(transport=transport, base_url="http://standing.test") as client:
        task = asyncio.create_task(
            post(value.session, SETTLE, data.request, secret=SECRET, client=client)
        )
        with pytest.raises(httpx.ReadError):
            await task
        (body,) = transport.bodies
        assert task.done() and not task.cancelled()
        assert body.closes == 1 and body.closed and not body.failed and not body.cancelled
    before = rows(value.session.store)
    receipt = response(
        await post(value.session, LOOKUP, query(data.request)), 200, StandingAgentSettlementReceipt
    )
    assert receipt.validate_query(query(data.request)).settlement.request == data.request
    replay = response(
        await post(value.session, SETTLE, data.request, secret=SECRET),
        200,
        StandingAgentSettlementReceipt,
        data.request,
    )
    assert receipt.settlement == replay.settlement and replay.disposition == "retained"
    assert rows(value.session.store) == before and transport.close_calls == 1
    assert (value.starts, value.stops) == before_effects and len(reports(value)) == 1


@pytest.mark.asyncio
async def test_two_initial_histories_active_b_and_a_replay_never_clear_or_relaunch_b(
    prepared_settlement, physical
):
    first = await prepared_settlement()
    a = await commit(first)
    env = first.value.session
    packet_b = await next_packet(env, first.value.packet)
    claim_b = response(
        await post(env, "/v1/standing/executions/prepare", packet_b),
        200,
        StandingExecutionClaimAdmission,
        packet_b,
    ).claim
    assert await first.value.agent.standing_unresolved() == claim_b
    before = rows(env.store)
    retained_a = response(
        await post(env, SETTLE, first.request, secret=SECRET),
        200,
        StandingAgentSettlementReceipt,
        first.request,
    )
    assert retained_a.settlement == a.settlement and retained_a.disposition == "retained"
    assert await first.value.agent.standing_unresolved() == claim_b and rows(env.store) == before
    second = await prepared_settlement(session=env, claim=claim_b)
    b = await commit(second)
    assert b.settlement.request.registration != a.settlement.request.registration
    assert first.value.initial.event_id != second.value.initial.event_id
    assert (
        env.store.db.execute(
            "SELECT count(*) FROM entries WHERE kind='agent.standing-command'"
        ).fetchone()[0]
        == 2
    )
    for data, proof in ((first, first.value.initial), (second, second.value.initial)):
        assert await first.value.agent.standing_claim_lookup(data.value.packet) == data.value.claim
        assert await first.value.agent.standing_task_lookup(data.value.packet) == proof
        assert (
            await first.value.agent.standing_closure_lookup(data.value.packet)
            == data.report.finalization.context.closure
        )
        assert (
            response(
                await post(env, LOOKUP, query(data.request)), 200, StandingAgentSettlementReceipt
            ).settlement.request
            == data.request
        )
    ordinary = ordinary_proposal(
        env.session, task_id="ordinary-after-two-standing", key="ordinary-after-two-standing"
    )
    admitted = await first.value.agent.admit_submit(ordinary)
    assert admitted.entry.task_refs == ["ordinary-after-two-standing"]
    export = export_request(
        env.session,
        task_id="ordinary-after-two-standing",
        content_scope="local_only",
        provider=ProviderInfo(provider="synthetic-local", model="no-inference", local=True),
    )
    admission = await first.value.agent.agent_export_admission(export)
    assert admission.validate_request(export).effective_scope == "local_only"
    assert admission.journal_sequence >= second.value.initial.sequence
    assert await first.value.agent.standing_unresolved() is None
    candidate = await next_packet(env, packet_b, suffix="c")
    for old in (first.value.packet.task.task_id, second.value.packet.task.task_id):
        raw = candidate.model_dump(mode="json")
        raw["task"]["task_id"] = old
        before = rows(env.store)
        response(
            await post(
                env, "/v1/standing/executions/prepare", StandingExecutionIntent.model_validate(raw)
            ),
            409,
        )
        assert rows(env.store) == before


@pytest.mark.asyncio
async def test_settlement_and_registration_mismatch_lookup_are_conflict_not_new_absence(
    prepared_settlement,
):
    data = await prepared_settlement(kind="bound-never-admitted")
    await commit(data)
    before = rows(data.value.session.store)
    other_registration = registration(data.value.claim, owner=OTHER, key="foreign-owner")
    response(await post(data.value.session, REGISTRATION, other_registration), 409)
    raw = query(data.request).model_dump(mode="json")
    raw["settlement_key"] += ":other"
    response(
        await post(data.value.session, LOOKUP, StandingAgentSettlementQuery.model_validate(raw)),
        409,
    )
    raw = data.request.model_dump(mode="json")
    raw["idempotency_key"] += ":other"
    rehash(raw, "settlement_digest", "kinaiya.standing.agent-settlement.v1")
    response(
        await post(
            data.value.session,
            SETTLE,
            StandingAgentSettlementRequest.model_validate(raw),
            secret=SECRET,
        ),
        409,
    )
    assert rows(data.value.session.store) == before
    # This original claim never had an initial. Its historical Task ID remains
    # reserved just as an admitted historical Task ID does.
    candidate = await next_packet(
        data.value.session, data.value.packet, task_id=data.value.packet.task.task_id
    )
    before = rows(data.value.session.store)
    response(await post(data.value.session, "/v1/standing/executions/prepare", candidate), 409)
    assert rows(data.value.session.store) == before


@pytest.mark.asyncio
async def test_real_database_reopen_keeps_exact_historical_records_without_new_restoration(
    prepared_settlement, factory
):
    data = await prepared_settlement()
    receipt = await commit(data)
    value = data.value
    await close_resources(value)
    assert value.cleanup_error is None
    assert value.coordinator._close_owner.done()
    assert value.gateway.http.is_closed and not value.gateway._submit_exchanges
    # Explicit shutdown may add safety stops. Historical reads after that
    # positively retired boundary must add no new physical operation.
    effects = (list(value.starts), list(value.stops))
    await value.agent.close()
    assert value.agent.http.is_closed and not value.agent._submit_exchanges
    await close_env(value.session)
    reopened = await factory(value.session.path)
    state = response(
        await reopened.client.get("/v1/standing/dispatch", headers=headers()),
        200,
        StandingDispatchState,
    )
    assert state.instance_id != data.reg.receipt.registration.registered_instance
    before = rows(reopened.store)
    registration_receipt = response(
        await post(reopened, REGISTRATION, data.reg.request),
        200,
        StandingAgentRetirementRegistrationReceipt,
        data.reg.request,
    )
    assert registration_receipt.registration == data.reg.receipt.registration
    retained = response(
        await post(reopened, LOOKUP, query(data.request)), 200, StandingAgentSettlementReceipt
    )
    assert retained.validate_query(query(data.request)).settlement == receipt.settlement
    response(
        await reopened.client.get("/v1/standing/executions/unresolved", headers=headers("agent")),
        200,
    )
    assert rows(reopened.store) == before
    assert (value.starts, value.stops) == effects
    assert len(reports(value)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ROUTES)
async def test_new_routes_authenticate_before_body_or_private_capability(factory, path):
    env = await factory()
    for role in (None, "owner", "resources", "converse", "decisions"):
        stream = BodyWitness()
        before = rows(env.store)
        result = await env.client.post(
            path + "?private=" + MARKER,
            content=stream,
            headers=headers(role, **{HEADER: MARKER, "Content-Type": "not/json"}),
        )
        response(result, 401 if role is None else 403)
        assert stream.reads == 0 and rows(env.store) == before


@pytest.mark.asyncio
async def test_settlement_capability_header_is_exact_single_and_required(prepared_settlement):
    data = await prepared_settlement()
    env = data.value.session
    base = [
        ("Authorization", headers("agent")["Authorization"]),
        ("Content-Type", "application/json"),
    ]
    for extra in (
        [],
        [(HEADER, "x" * 64)],
        [(HEADER, SECRET.hex().upper())],
        [(HEADER, SECRET.hex()), (HEADER, SECRET.hex())],
        [(HEADER, SECRET.hex()), (CLAIM_HEADER, CLAIM_SECRET.hex())],
    ):
        before = rows(env.store)
        result = await env.client.post(
            SETTLE, content=data.request.canonical_bytes(), headers=[*base, *extra]
        )
        response(result, 422)
        assert rows(env.store) == before
    assert await data.value.agent.standing_unresolved() == data.value.claim
