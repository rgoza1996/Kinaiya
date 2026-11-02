"""Independent actual-ASGI execution API oracles, frozen before adapter read.

Synthetic Agent credentials/inference-free proposals are not a real producer,
worker, provider call, settlement, inbox or editor qualification.
"""

import asyncio
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from test_standing_dispatch_api import AUTH, headers, rows

from packages.contracts.agent_control import AgentModeChange
from packages.contracts.models import SessionRequest
from packages.contracts.standing_admission import StandingInitialProof, StandingTaskAdmission
from packages.contracts.standing_dispatch import (
    StandingDispatchActivation,
    StandingDispatchActivationReceipt,
    StandingDispatchState,
    StandingProducerReceipt,
    StandingProducerRenewal,
)
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionClaimAdmission,
    StandingExecutionIntent,
    capability_digest,
)
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeReceipt,
    StandingResponsibilitySnapshot,
)
from services.session.api import create_app
from services.session.store import Store

PREPARE = "/v1/standing/executions/prepare"
CLAIM_LOOKUP = "/v1/standing/executions/lookup"
UNRESOLVED = "/v1/standing/executions/unresolved"
ADMIT = "/v1/standing/tasks/admit"
TASK_LOOKUP = "/v1/standing/tasks/lookup"
POSTS = (PREPARE, CLAIM_LOOKUP, ADMIT, TASK_LOOKUP)
ROUTES = (("GET", UNRESOLVED), *(("POST", path) for path in POSTS))
SECRET = bytes(range(32))  # Public synthetic test capability.
CAP_HEADER = "X-Kinaiya-Standing-Capability"
RESPONSIBILITY = "22800000-0000-4000-8000-000000000002"
MARKER = "SYNTHETIC-PRIVATE-EXECUTION-HTTP-DO-NOT-ECHO"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def checked(reply, status, kind=None):
    assert reply.status_code == status, (reply.status_code, reply.text)
    assert reply.headers.get("cache-control") == "no-store"
    assert MARKER not in reply.text
    assert SECRET.hex() not in reply.text
    if status == 200:
        assert reply.headers["content-type"].split(";", 1)[0] == "application/json"
        if reply.content == b"null":
            assert kind is None
            return None
        assert kind is not None
        assert len(reply.content) <= kind.wire_bytes
        return kind.from_json(reply.content)
    return None


async def send(env, path, original, *, cap=None, raw=None, extra=None):
    custom = {"Content-Type": "application/json", **(extra or {})}
    if cap is not None:
        custom[CAP_HEADER] = cap
    return await env.client.post(
        path,
        content=original.canonical_bytes() if raw is None else raw,
        headers=headers("agent", **custom),
    )


async def close_env(env):
    if not env.closed:
        await env.client.aclose()
        await env.lifespan.__aexit__(None, None, None)
        env.store.close()
        env.closed = True


@pytest.fixture
async def factory(tmp_path):
    opened = []

    async def make(path=None):
        path = tmp_path / f"execution-{len(opened)}.sqlite" if path is None else path
        store = Store(path)
        sessions = store.sessions()
        session = sessions[0] if sessions else store.create_session(SessionRequest(channel="agent"))
        wall = SimpleNamespace(value=datetime(2026, 11, 1, 11, 59, 50, tzinfo=UTC))
        mono = SimpleNamespace(value=1000)
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


@pytest.fixture
async def env(factory):
    return await factory()


async def original(env):
    state = checked(
        await env.client.get("/v1/standing/dispatch", headers=headers()), 200, StandingDispatchState
    )
    grant = StandingChange.model_validate(
        {
            "schema_version": "1.0",
            "idempotency_key": "execution-http:grant",
            "expected_backend": state.backend_id,
            "expected_instance": state.instance_id,
            "expected_revision": 0,
            "responsibility_id": RESPONSIBILITY,
            "authorization": {
                "label": "Synthetic execution API",
                "objective": "Write a bounded synthetic report.",
                "content_scope": "local_only",
                "enabled": True,
                "task_session": {
                    "session_id": env.session.session_id,
                    "thread_id": env.session.thread_id,
                    "epoch": env.session.epoch,
                },
                "workspace_pin": {
                    "schema_version": "1.0",
                    "workspace_id": "a" * 64,
                    "root_identity": "b" * 64,
                    "settings_revision": 0,
                    "source": "host",
                    "shell_backend": "bubblewrap",
                    "mode": "direct",
                },
                "schedule": {
                    "kind": "daily_local",
                    "timezone": "UTC",
                    "local_time": "12:00",
                    "fold_policy": "earlier",
                    "gap_policy": "skip",
                    "grace_seconds": 60,
                    "missed_run_policy": "latest_within_grace",
                    "start_local_date": "2026-11-01",
                    "valid_until_utc": "2026-11-05T00:00:00Z",
                },
                "delivery": {
                    "kind": "in_app",
                    "attention": "routine",
                    "preview": "status_only",
                    "quiet_hours": None,
                },
                "overlap_policy": "one_unresolved_per_authorization",
                "maximum_runs": 1,
                "inference_authorization": "configured_agent_profile",
            },
        }
    )
    result = await env.client.post(
        "/v1/standing/changes",
        content=grant.canonical_bytes(),
        headers=headers(**{"Content-Type": "application/json"}),
    )
    checked(result, 200, StandingChangeReceipt).validate_request(grant)
    record = checked(
        await env.client.get("/v1/standing/responsibilities/" + RESPONSIBILITY, headers=headers()),
        200,
        StandingResponsibilitySnapshot,
    ).record
    renewal = StandingProducerRenewal(
        schema_version="1.0",
        expected_backend=state.backend_id,
        expected_instance=state.instance_id,
        producer_id=str(uuid.uuid4()),
        sequence=1,
    )
    result = await env.client.post(
        "/v1/standing/producer/renew",
        content=renewal.canonical_bytes(),
        headers=headers("agent", **{"Content-Type": "application/json"}),
    )
    producer = checked(result, 200, StandingProducerReceipt).validate_request(renewal)
    activation = StandingDispatchActivation(
        schema_version="1.0",
        expected_epoch=state.epoch,
        expected_fence_token=state.fence_token,
        mode_change=AgentModeChange(
            schema_version="1.0",
            idempotency_key="execution-http:activate",
            expected_backend=state.backend_id,
            expected_instance=state.instance_id,
            expected_revision=state.control_revision,
            mode="autonomous",
            active_work_policy="finish_already_accepted",
        ),
    )
    result = await env.client.post(
        "/v1/standing/dispatch/activate",
        content=activation.canonical_bytes(),
        headers=headers(**{"Content-Type": "application/json"}),
    )
    active = checked(result, 200, StandingDispatchActivationReceipt).validate_request(activation)
    env.wall.value = datetime(2026, 11, 1, 12, 0, 10, tzinfo=UTC)
    key = "execution-http:original"
    executor = "22800000-0000-4000-8000-000000000003"
    return StandingExecutionIntent.model_validate(
        {
            "schema_version": "1.0",
            "idempotency_key": key,
            "expected_backend": state.backend_id,
            "expected_instance": state.instance_id,
            "responsibility_id": RESPONSIBILITY,
            "authorization_revision": record.receipt.revision,
            "authorization_digest": record.receipt.authorization_digest,
            "record_digest": record.record_digest,
            "occurrence": {
                "schema_version": "1.0",
                "local_date": "2026-11-01",
                "due_utc": "2026-11-01T12:00:00Z",
                "utc_offset_seconds": 0,
            },
            "dispatch_epoch": active.epoch,
            "activation_digest": active.activation_digest,
            "control_revision": active.mode_receipt.revision,
            "producer": producer,
            "executor_id": executor,
            "capability_sha256": capability_digest(state.backend_id, key, executor, SECRET),
            "task": {
                "schema_version": "1.0",
                "task_id": "22800000-0000-4000-8000-000000000004",
                "task_session": record.receipt.request.authorization.task_session,
                "objective": grant.authorization.objective,
                "content_scope": "local_only",
                "workspace_pin": grant.authorization.workspace_pin,
                "harness_version": "seed-v1",
                "tool_subset": "native-files-v1",
                "model_pin": {
                    "schema_version": "1.0",
                    "profile_id": "synthetic-profile",
                    "provider": "synthetic-no-inference",
                    "model": "synthetic-model",
                    "local": True,
                    "reasoning": None,
                    "settings_revision": 0,
                    "catalog_binding": None,
                    "catalog_revision": None,
                },
            },
        }
    )


async def prepared(env, packet):
    return checked(
        await send(env, PREPARE, packet), 200, StandingExecutionClaimAdmission
    ).validate_request(packet)


@pytest.mark.asyncio
async def test_existing_public_fixture_arms_full_original_before_execution_adapter(env):
    packet = await original(env)
    assert packet.task.objective == "Write a bounded synthetic report."
    assert packet.producer.request.expected_instance == packet.expected_instance
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    assert StandingExecutionIntent.from_json(packet.canonical_bytes()) == packet


class BodyWitness(httpx.AsyncByteStream):
    def __init__(self):
        self.reads = 0

    async def __aiter__(self):
        self.reads += 1
        yield MARKER.encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("role", [None, "owner", "converse", "decisions", "resources"])
async def test_all_five_agent_routes_authenticate_before_body_query_or_private_header(
    env, method, path, role
):
    stream = BodyWitness()
    before = rows(env.store)
    result = await env.client.request(
        method,
        path + "?bad=1",
        content=stream,
        headers=headers(role, **{CAP_HEADER: MARKER, "Content-Type": "not/json"}),
    )
    checked(result, 401 if role is None else 403)
    assert stream.reads == 0
    assert rows(env.store) == before


@pytest.mark.asyncio
async def test_actual_prepare_admit_readback_replay_and_private_capability_never_persist(
    env,
):
    packet = await original(env)
    checked(await send(env, CLAIM_LOOKUP, packet), 200)
    checked(await send(env, TASK_LOOKUP, packet), 200)
    checked(await env.client.get(UNRESOLVED, headers=headers("agent")), 200)
    record = await prepared(env, packet)
    assert record.disposition == "created"
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    checked(await send(env, ADMIT, packet), 409)
    checked(await send(env, ADMIT, packet, cap="00" * 32), 409)
    result = checked(
        await send(env, ADMIT, packet, cap=SECRET.hex()), 200, StandingTaskAdmission
    ).validate_request(packet)
    assert result.disposition == "created"
    assert result.proof.claim == record.claim
    assert result.proof.journal_entry().origin == "standing-supervisor"
    assert env.store.db.execute("SELECT writer FROM entries").fetchone()[0] == "standing-supervisor"
    assert env.store.db.execute("SELECT count(*) FROM standing_task_admissions").fetchone()[0] == 1
    assert env.store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 0
    before = rows(env.store)
    replay = checked(await send(env, ADMIT, packet), 200, StandingTaskAdmission).validate_request(
        packet
    )
    assert replay.disposition == "retained" and replay.proof == result.proof
    assert (
        checked(await send(env, PREPARE, packet), 200, StandingExecutionClaimAdmission).disposition
        == "retained"
    )
    assert (
        checked(await send(env, TASK_LOOKUP, packet), 200, StandingInitialProof).validate_request(
            packet
        )
        == result.proof
    )
    assert (
        checked(
            await send(env, CLAIM_LOOKUP, packet), 200, StandingExecutionClaim
        ).validate_request(packet)
        == record.claim
    )
    assert (
        checked(
            await env.client.get(UNRESOLVED, headers=headers("agent")), 200, StandingExecutionClaim
        )
        == record.claim
    )
    assert rows(env.store) == before
    assert SECRET.hex() not in repr(before)
    reply = await env.client.delete("/v1/sessions/" + env.session.session_id, headers=headers())
    assert reply.status_code == 409
    assert rows(env.store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", POSTS)
async def test_execution_post_raw_bounds_duplicate_json_queries_and_media_are_exact(env, path):
    packet = await original(env)
    before = rows(env.store)
    for raw in (b" " * 131073, b'{"schema_version":"1.0",' + packet.canonical_bytes()[1:]):
        checked(await send(env, path, packet, raw=raw), 422)
        assert rows(env.store) == before
    for extra in ({"Content-Type": "text/plain"}, {"Content-Encoding": "gzip"}):
        checked(await send(env, path, packet, extra=extra), 415)
        assert rows(env.store) == before
    checked(await send(env, path + "?bad=1", packet), 422)
    assert rows(env.store) == before


@pytest.mark.asyncio
async def test_inclusive_raw_limit_and_absolute_native_deadline_are_preserved(env, monkeypatch):
    packet = await original(env)
    claims = getattr(env.app.state, "standing_execution_owner", None)
    assert claims is not None, "Native execution HTTP capability is absent"
    previous = claims.prepare
    observed = []

    def capture(value, **options):
        assert options["deadline"] is not None
        assert callable(options["cancelled"]) and options["cancelled"]() is False
        observed.append(options["deadline"])
        return previous(value, **options)

    monkeypatch.setattr(claims, "prepare", capture)
    body = packet.canonical_bytes()
    raw = b" " * (131072 - len(body)) + body
    first = checked(await send(env, PREPARE, packet, raw=raw), 200, StandingExecutionClaimAdmission)
    assert first.disposition == "created" and len(observed) == 1
    retained = checked(
        await send(env, PREPARE, packet, raw=raw), 200, StandingExecutionClaimAdmission
    )
    assert retained.disposition == "retained" and len(observed) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["A" * 64, "a" * 63, "a" * 65, " " + "a" * 64, "0x" + "a" * 64, MARKER]
)
async def test_private_header_is_strict_single_ascii_hex_without_echo_or_body_effect(env, value):
    packet = await original(env)
    await prepared(env, packet)
    before = rows(env.store)
    checked(await send(env, ADMIT, packet, cap=value), 422)
    assert rows(env.store) == before


@pytest.mark.asyncio
async def test_duplicate_private_header_and_capability_query_are_refused(env):
    packet = await original(env)
    await prepared(env, packet)
    before = rows(env.store)
    duplicate = list(headers("agent", **{"Content-Type": "application/json"}).items()) + [
        (CAP_HEADER, SECRET.hex()),
        (CAP_HEADER, SECRET.hex()),
    ]
    checked(await env.client.post(ADMIT, content=packet.canonical_bytes(), headers=duplicate), 422)
    checked(await send(env, ADMIT + "?capability=" + SECRET.hex(), packet), 422)
    assert rows(env.store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["null_prepare", "wrong_claim", "wrong_task_reply"])
async def test_unbound_or_wrong_native_replies_fail_unavailable_instead_of_success(
    env, monkeypatch, action
):
    packet = await original(env)
    claim = await prepared(env, packet)
    if action == "null_prepare":
        monkeypatch.setattr(
            env.app.state.standing_execution_owner, "prepare", lambda *a, **kw: None
        )
        path = PREPARE
    elif action == "wrong_claim":
        raw = packet.model_dump(mode="json")
        raw["idempotency_key"] = "execution-http:other-original"
        alternate = StandingExecutionIntent.model_validate(raw)
        from test_standing_execution_owner import make_claim

        other = make_claim(alternate)
        monkeypatch.setattr(
            env.app.state.standing_execution_owner, "lookup", lambda *a, **kw: other
        )
        path = CLAIM_LOOKUP
    else:
        monkeypatch.setattr(env.app.state.standing_task_owner, "admit", lambda *a, **kw: claim)
        path = ADMIT
    before = rows(env.store)
    checked(await send(env, path, packet, cap=SECRET.hex()), 503)
    assert rows(env.store) == before


@pytest.mark.asyncio
async def test_same_database_restart_retains_receipts_and_replacement_backend_refuses_them(factory):
    first = await factory()
    packet = await original(first)
    await prepared(first, packet)
    admitted = checked(
        await send(first, ADMIT, packet, cap=SECRET.hex()), 200, StandingTaskAdmission
    )
    backend = packet.expected_backend
    instance = packet.expected_instance
    path = first.path
    await close_env(first)
    second = await factory(path)
    assert second.app.state.agent_control.snapshot().backend_id == backend
    assert second.app.state.agent_control.instance != instance
    replay = checked(await send(second, ADMIT, packet), 200, StandingTaskAdmission)
    assert replay.disposition == "retained" and replay.proof == admitted.proof
    assert (
        checked(await send(second, TASK_LOOKUP, packet), 200, StandingInitialProof)
        == admitted.proof
    )
    third = await factory()
    assert third.app.state.agent_control.snapshot().backend_id != backend
    before = rows(third.store)
    for route in (CLAIM_LOOKUP, TASK_LOOKUP):
        checked(await send(third, route, packet), 409)
    assert rows(third.store) == before


@pytest.mark.asyncio
async def test_protected_cors_keeps_no_store_and_does_not_expose_private_header(env):
    result = await env.client.options(
        ADMIT,
        headers={
            "Origin": "http://synthetic-dispatch.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization,Content-Type," + CAP_HEADER,
        },
    )
    assert result.status_code == 400
    assert result.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_capacity_exhaustion_preserves_passive_claim_and_task_history(factory):
    from test_standing_execution_owner import seed_generations

    first = await factory()
    packet = await original(first)
    claim = await prepared(first, packet)
    admission = checked(
        await send(first, ADMIT, packet, cap=SECRET.hex()), 200, StandingTaskAdmission
    )
    first.app.state.agent_control.close()
    seed_generations(first, total=10000)  # Real schema seed, not10000 registration calls.
    path = first.path
    await close_env(first)
    second = await factory(path)
    control = second.app.state.agent_control
    assert control.instance is None
    assert not second.app.state.standing_owner._registered
    assert second.app.state.standing_execution_initialized
    assert second.app.state.standing_task_initialized
    before = rows(second.store)
    assert (
        checked(await send(second, CLAIM_LOOKUP, packet), 200, StandingExecutionClaim)
        == claim.claim
    )
    assert (
        checked(
            await second.client.get(UNRESOLVED, headers=headers("agent")),
            200,
            StandingExecutionClaim,
        )
        == claim.claim
    )
    assert (
        checked(await send(second, TASK_LOOKUP, packet), 200, StandingInitialProof)
        == admission.proof
    )
    retained = checked(await send(second, ADMIT, packet), 200, StandingTaskAdmission)
    assert retained.disposition == "retained" and retained.proof == admission.proof
    assert rows(second.store) == before
    assert control.instance is None


@pytest.mark.asyncio
async def test_old_and_new_routes_share_64_actual_native_owners_and_shutdown_joins_them(
    env, monkeypatch
):
    dispatch = env.app.state.standing_dispatch_owner
    claims = getattr(env.app.state, "standing_execution_owner", None)
    assert claims is not None, "Native execution HTTP capability is absent"
    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_default_executor", None)
    executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="standing-http-oracle")
    loop.set_default_executor(executor)
    release = threading.Event()
    entered = threading.Event()
    lock = threading.Lock()
    count = 0
    old = dispatch.snapshot
    new = claims.unresolved

    def block(call):
        def invoke():
            nonlocal count
            with lock:
                count += 1
                if count == 64:
                    entered.set()
            assert release.wait(10), "Bounded controlled native hold expired"
            return call()

        return invoke

    monkeypatch.setattr(dispatch, "snapshot", block(old))
    monkeypatch.setattr(claims, "unresolved", block(new))
    requests = []
    closing = None
    try:
        for index in range(64):
            route, role = (
                ("/v1/standing/dispatch", "owner") if index < 32 else (UNRESOLVED, "agent")
            )
            requests.append(asyncio.create_task(env.client.get(route, headers=headers(role))))
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        assert count == 64 and env.operations.active_operations == 64
        checked(await env.client.get(UNRESOLVED, headers=headers("agent")), 503)
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        assert env.operations.active_operations == 64
        closing = asyncio.create_task(close_env(env))
        async with asyncio.timeout(3):
            while not env.operations._closed:
                await asyncio.sleep(0)
        assert not closing.done()
        for name in (
            "standing_owner",
            "standing_dispatch_owner",
            "standing_execution_owner",
            "standing_task_owner",
        ):
            assert not getattr(env.app.state, name)._closed
        assert env.app.state.agent_control.instance is not None
        release.set()
        await asyncio.wait_for(asyncio.shield(closing), 5)
        assert env.operations.active_operations == 0
        for name in (
            "standing_owner",
            "standing_dispatch_owner",
            "standing_execution_owner",
            "standing_task_owner",
        ):
            assert getattr(env.app.state, name)._closed
        assert env.app.state.agent_control.instance is None
    finally:
        release.set()
        await asyncio.gather(*requests, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        if previous is None:
            loop._default_executor = None
        else:
            loop.set_default_executor(previous)
        executor.shutdown(wait=True)
