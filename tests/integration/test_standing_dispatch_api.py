"""Independent dispatch HTTP oracles from the frozen f47aa07 boundary.

Actual private SQLite and ASGI requests use the committed native controllers.
Synthetic lease packets do not qualify a real producer, scheduler or Agent task.
No new HTTP implementation or HTTP author's probes informed these assertions.
"""

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from packages.contracts.agent_control import AgentModeChange
from packages.contracts.models import SessionRequest
from packages.contracts.standing_dispatch import (
    StandingDispatchActivation,
    StandingDispatchActivationLookup,
    StandingDispatchActivationReceipt,
    StandingDispatchState,
    StandingDispatchStop,
    StandingDispatchStopProof,
    StandingProducerReceipt,
    StandingProducerRenewal,
)
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeLookup,
    StandingChangeReceipt,
    StandingResponsibilitySnapshot,
    StandingUnavailable,
    canonical_bytes,
)
from services.common.auth import Auth
from services.session.agent_control import AgentControl
from services.session.api import create_app
from services.session.standing_dispatch import StandingDispatchOwner
from services.session.standing_work import StandingOwner
from services.session.standing_work_api import install_standing_owner_routes
from services.session.store import Store

STATE = "/v1/standing/dispatch"
CONTEXT = STATE + "/stop-context"
ACTIVATE = STATE + "/activate"
LOOKUP = ACTIVATE + "/lookup"
STOP = STATE + "/stop"
RENEW = "/v1/standing/producer/renew"
DEFINITIONS = "/v1/standing/changes"
DETAIL = "/v1/standing/responsibilities/00000000-0000-0000-0000-000000000001"
PRODUCER = "00000000-0000-0000-0000-000000000101"
UNKNOWN = "00000000-0000-0000-0000-999999999999"
MARKER = "PUBLIC-SYNTHETIC-DO-NOT-ECHO"
TOKENS = dict(
    zip(
        ("owner", "agent", "converse", "decisions", "resources"),
        (c * 32 for c in "oacdr"),
        strict=True,
    )
)
AUTH = Auth(TOKENS, allowed_origins=("http://synthetic-dispatch.test",))
POSTS = (ACTIVATE, LOOKUP, STOP, RENEW)
ROUTES = (("GET", STATE), ("GET", CONTEXT), *(("POST", p) for p in POSTS))


class MonotonicClock:
    def __init__(self, value=1000):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


def headers(role="owner", **extra):
    return {**({"Authorization": "Bearer " + TOKENS[role]} if role else {}), **extra}


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def rows(store):
    names = [
        r[0]
        for r in store.db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
    ]
    return tuple(
        (
            name,
            tuple(
                sorted(
                    (
                        tuple(row)
                        for row in store.db.execute(
                            'SELECT * FROM "' + name.replace('"', '""') + '"'
                        )
                    ),
                    key=repr,
                )
            ),
        )
        for name in names
    )


def response(response, status, kind=None):
    assert response.status_code == status, (response.status_code, response.text)
    assert response.headers.get("cache-control") == "no-store"
    assert len(response.content) <= (kind.wire_bytes if kind is not None else 32768)
    if status != 200:
        assert MARKER not in response.text
        return None
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    return kind.from_json(response.content) if kind is not None else response.json()


async def post(env, path, packet, *, role=None, raw=None, **extra):
    role = ("agent" if path == RENEW else "owner") if role is None else role
    return await env.client.post(
        path,
        content=canonical_bytes(packet) if raw is None else raw,
        headers=headers(role, **{"Content-Type": "application/json", **extra}),
    )


async def close_env(env):
    if env.closed:
        return
    try:
        await env.client.aclose()
        await env.lifespan.__aexit__(None, None, None)
    finally:
        env.store.close()
        env.closed = True


@pytest.fixture
async def factory(tmp_path):
    tmp_path.chmod(0o700)
    opened = []

    async def make(*, path=None, clock=None):
        path = tmp_path / f"dispatch-{len(opened)}.sqlite" if path is None else path
        store = Store(path)
        sessions = store.sessions()
        session = sessions[0] if sessions else store.create_session(SessionRequest(channel="agent"))
        clock = MonotonicClock() if clock is None else clock
        try:
            app = create_app(
                store,
                AUTH,
                standing_clock=lambda: datetime(2024, 1, 1, 0, 0, 0, 250000, tzinfo=UTC),
                standing_monotonic_ms=clock,
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
        except BaseException:
            store.close()
            raise
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://dispatch.test"
        )
        env = SimpleNamespace(
            path=path,
            store=store,
            session=session,
            clock=clock,
            app=app,
            control=app.state.agent_control,
            owner=app.state.standing_owner,
            dispatch=app.state.standing_dispatch_owner,
            operations=app.state.standing_owner_operations,
            lifespan=lifespan,
            client=client,
            closed=False,
        )
        opened.append(env)
        return env

    try:
        yield make
    finally:
        for env in reversed(opened):
            if not env.closed:
                await close_env(env)


@pytest.fixture
async def env(factory):
    return await factory()


def renewal(env, *, sequence=1, producer=PRODUCER):
    state = env.dispatch.snapshot()
    return StandingProducerRenewal(
        schema_version="1.0",
        expected_backend=state.backend_id,
        expected_instance=state.instance_id,
        producer_id=producer,
        sequence=sequence,
    )


def activation(env, key="synthetic-activation"):
    state = env.dispatch.snapshot()
    return StandingDispatchActivation(
        schema_version="1.0",
        mode_change=AgentModeChange(
            schema_version="1.0",
            idempotency_key=key,
            expected_backend=state.backend_id,
            expected_instance=state.instance_id,
            expected_revision=state.control_revision,
            mode="autonomous",
            active_work_policy="finish_already_accepted",
        ),
        expected_epoch=state.epoch,
        expected_fence_token=state.fence_token,
    )


def packet(env, path):
    if path == RENEW:
        return renewal(env)
    if path == STOP:
        return env.dispatch.stop_context()
    return activation(env)


async def live(env, key="synthetic-activation"):
    original_renewal = renewal(env)
    receipt = response(await post(env, RENEW, original_renewal), 200, StandingProducerReceipt)
    receipt.validate_request(original_renewal)
    original = activation(env, key)
    saved = response(await post(env, ACTIVATE, original), 200, StandingDispatchActivationReceipt)
    saved.validate_request(original)
    return original_renewal, receipt, original, saved


def definition(env, *, large=False):
    control = env.control.snapshot()
    return StandingChange.model_validate(
        {
            "schema_version": "1.0",
            "idempotency_key": "synthetic-definition",
            "expected_backend": control.backend_id,
            "expected_instance": control.instance_id,
            "expected_revision": 0,
            "responsibility_id": DETAIL.rsplit("/", 1)[1],
            "authorization": {
                "label": "Synthetic definition",
                "objective": "\0" * 16000 if large else "Synthetic metadata only",
                "content_scope": "public_synthetic",
                "enabled": True,
                "task_session": {
                    "session_id": env.session.session_id,
                    "thread_id": env.session.thread_id,
                    "epoch": env.session.epoch,
                },
                "workspace_pin": {
                    "schema_version": "1.0",
                    "workspace_id": "b" * 64,
                    "root_identity": "c" * 64,
                    "settings_revision": 0,
                    "source": "host",
                    "shell_backend": "bubblewrap",
                    "mode": "direct",
                },
                "schedule": {
                    "kind": "daily_local",
                    "timezone": "UTC",
                    "local_time": "09:00",
                    "fold_policy": "earlier",
                    "gap_policy": "skip",
                    "grace_seconds": 120,
                    "missed_run_policy": "latest_within_grace",
                    "start_local_date": "2024-01-01",
                    "valid_until_utc": "2024-01-31T00:00:00Z",
                },
                "delivery": {
                    "kind": "in_app",
                    "attention": "routine",
                    "preview": "status_only",
                    "quiet_hours": None,
                },
                "overlap_policy": "one_unresolved_per_authorization",
                "maximum_runs": 3,
                "inference_authorization": "configured_agent_profile",
            },
        }
    )


async def entered(event):
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(0.005)


async def test_all_six_routes_use_native_proof_without_announcing_a_real_producer(env):
    assert env.app.state.standing_dispatch_initialized is True
    initial = response(await env.client.get(STATE, headers=headers()), 200, StandingDispatchState)
    assert initial.epoch == 0 and initial.producer is None and initial.dispatch_ready is False
    context = response(await env.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    assert context.issued_epoch == 0 and context.expected_fence_token == initial.fence_token
    original_renewal, lease, original, saved = await live(env)
    assert lease.request == original_renewal
    assert lease.issued_monotonic_ms == 1000 and lease.expires_monotonic_ms == 16000
    assert saved.request == original and saved.epoch == 1
    observed = response(await post(env, LOOKUP, original), 200, StandingDispatchActivationLookup)
    observed.validate_request(original)
    assert (
        observed.status == "recorded"
        and observed.receipt == saved
        and observed.mutation_fenced is True
    )
    assert observed.state.dispatch_ready is True
    stop = response(await env.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    proof = response(await post(env, STOP, stop), 200, StandingDispatchStopProof)
    proof.validate_request(stop)
    assert proof.target_fenced is True and proof.state.dispatch_ready is False
    assert proof.state.mode == "autonomous"
    ordinary = response(await env.client.get("/v1/agent-control/state", headers=headers()), 200)
    assert (
        ordinary["available_modes"] == ["off", "on_demand"]
        and ordinary["standing_work_available"] is False
    )
    assert env.store.read(kinds=["agent.command"]) == []


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_roles_and_origins_refuse_before_ingress_or_any_native_method(
    env, monkeypatch, method, path
):
    before = rows(env.store)
    native_calls = []

    def forbidden(*args, **kwargs):
        native_calls.append(True)
        raise AssertionError("Unauthorized request entered a native dispatch method")

    for name in ("snapshot", "stop_context", "activate", "lookup", "stop", "renew"):
        monkeypatch.setattr(env.dispatch, name, forbidden)

    async def unread():
        pytest.fail("Unauthorized dispatch request consumed its body")
        yield b"unreachable"

    allowed = "agent" if path == RENEW else "owner"
    for role in TOKENS:
        if role != allowed:
            response(
                await env.client.request(method, path, content=unread(), headers=headers(role)), 403
            )
    response(await env.client.request(method, path, content=unread(), headers={}), 401)
    response(
        await env.client.request(
            method, path, content=unread(), headers={"Authorization": "Bearer " + "x" * 32}
        ),
        401,
    )
    response(
        await env.client.request(
            method, path, content=unread(), headers=headers(allowed, Origin="http://foreign.test")
        ),
        403,
    )
    assert native_calls == [] and rows(env.store) == before


@pytest.mark.parametrize("path", POSTS)
async def test_each_post_refuses_non_identity_json_without_native_writes(env, path):
    original = packet(env, path)
    before = rows(env.store)
    for extra in (
        {"Content-Type": "text/plain"},
        {"Content-Encoding": "gzip"},
        {"Content-Encoding": "br"},
    ):
        response(await post(env, path, original, **extra), 415)
    assert rows(env.store) == before


@pytest.mark.parametrize("path", POSTS)
async def test_each_post_has_strict_raw_4096_json_and_identity_fields(env, path):
    original = packet(env, path)
    raw = canonical_bytes(original)
    bad = [
        raw + b" " * (4097 - len(raw)),
        raw[:-1] + b',"schema_version":"1.0"}',
        raw[:-1] + b',"untrusted":"' + MARKER.encode() + b'"}',
        b"\xff",
        b'{"schema_version":"\\ud800"}',
    ]
    field = "sequence" if path == RENEW else "issued_epoch" if path == STOP else "expected_epoch"
    for value in (True, 1.0, 9007199254740992):
        changed = original.model_dump(mode="json")
        changed[field] = value
        bad.append(encoded(changed))
    before = rows(env.store)
    for body in bad:
        response(await post(env, path, original, raw=body), 422)
    assert rows(env.store) == before


@pytest.mark.parametrize("path", POSTS)
async def test_4096_is_inclusive_and_4097_does_not_reach_native(env, path):
    if path == ACTIVATE:
        original_renewal = renewal(env)
        response(await post(env, RENEW, original_renewal), 200, StandingProducerReceipt)
    original = packet(env, path)
    raw = canonical_bytes(original)
    body = raw + b" " * (4096 - len(raw))
    kind = {
        ACTIVATE: StandingDispatchActivationReceipt,
        LOOKUP: StandingDispatchActivationLookup,
        STOP: StandingDispatchStopProof,
        RENEW: StandingProducerReceipt,
    }[path]
    response(await post(env, path, original, raw=body), 200, kind)
    before = rows(env.store)
    response(await post(env, path, original, raw=body + b" "), 422)
    assert rows(env.store) == before


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_all_new_routes_reject_extra_and_duplicate_query_fields(env, method, path):
    original = packet(env, path) if method == "POST" else None
    before = rows(env.store)
    for query in ("?extra=" + MARKER, "?limit=1", "?x=1&x=2"):
        if method == "POST":
            result = await post(
                env, path + query, original, role="agent" if path == RENEW else "owner"
            )
        else:
            result = await env.client.get(path + query, headers=headers())
        response(result, 422)
    assert rows(env.store) == before


@pytest.mark.parametrize(
    ("origin", "status"), [("http://synthetic-dispatch.test", 200), ("http://foreign.test", 400)]
)
async def test_no_store_also_covers_cors_before_new_route_dependencies(env, origin, status):
    before = rows(env.store)
    for method, path in ROUTES:
        result = await env.client.options(
            path,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
        assert result.status_code == status
        assert result.headers.get("cache-control") == "no-store"
    assert rows(env.store) == before


async def test_no_store_covers_unknown_dispatch_paths_and_method_errors(env):
    for method, path, status in [
        ("POST", STATE, 405),
        ("GET", RENEW, 405),
        ("GET", STATE + "/missing", 404),
    ]:
        response(await env.client.request(method, path, headers=headers()), status)


async def test_old_definition_raw_and_proof_caps_are_not_reduced_to_dispatch_limits(env):
    original = definition(env, large=True)
    assert len(canonical_bytes(original)) > 96000
    response(await post(env, DEFINITIONS, original), 200, StandingChangeReceipt).validate_request(
        original
    )
    observed = await post(env, DEFINITIONS + "/lookup", original)
    response(observed, 200, StandingChangeLookup).validate_request(original)
    assert len(observed.content) > 131072


async def test_activation_needs_native_current_lease_and_lookup_echoes_exact_original(env):
    original = activation(env)
    before = rows(env.store)
    response(await post(env, ACTIVATE, original), 409)
    assert rows(env.store) == before
    unobserved = response(await post(env, LOOKUP, original), 200, StandingDispatchActivationLookup)
    assert (
        unobserved.status == "unobserved"
        and unobserved.receipt is None
        and unobserved.mutation_fenced is False
    )
    response(await post(env, RENEW, renewal(env)), 200, StandingProducerReceipt)
    saved = response(await post(env, ACTIVATE, original), 200, StandingDispatchActivationReceipt)
    saved.validate_request(original)
    for path in (ACTIVATE, LOOKUP):
        changed = original.model_dump(mode="json")
        changed["expected_fence_token"] = UNKNOWN
        response(await post(env, path, StandingDispatchActivation.model_validate(changed)), 409)
    observed = response(await post(env, LOOKUP, original), 200, StandingDispatchActivationLookup)
    assert observed.request == original and observed.receipt == saved
    assert (
        env.store.db.execute("SELECT count(*) FROM standing_dispatch_activations").fetchone()[0]
        == 1
    )


async def test_stop_context_is_one_passive_native_observation_not_a_stop(env):
    _, _, _, saved = await live(env)
    before = rows(env.store)
    calls = env.clock.calls
    first = response(await env.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    second = response(await env.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    assert first == second
    assert first.issued_epoch == saved.epoch and first.activation_digest == saved.activation_digest
    assert first.expected_instance == saved.mode_receipt.service_instance
    assert env.clock.calls == calls and rows(env.store) == before
    state = response(await env.client.get(STATE, headers=headers()), 200, StandingDispatchState)
    assert state.dispatch_ready is True and state.stopped_through_epoch == 0


async def test_fresh_epoch_zero_stop_rotates_fence_before_old_activation_can_commit(env):
    response(await post(env, RENEW, renewal(env)), 200, StandingProducerReceipt)
    prepared = activation(env)
    stop = response(await env.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    assert stop.issued_epoch == 0 and stop.activation_digest is None
    proof = response(await post(env, STOP, stop), 200, StandingDispatchStopProof)
    proof.validate_request(stop)
    assert proof.state.epoch == proof.state.stopped_through_epoch == 0
    assert proof.state.fence_token != prepared.expected_fence_token
    response(await post(env, ACTIVATE, prepared), 409)
    assert (
        env.store.db.execute("SELECT count(*) FROM standing_dispatch_activations").fetchone()[0]
        == 0
    )


async def test_historical_activation_and_stale_stop_never_rearm_or_stop_later_epoch(env):
    _, _, original, saved = await live(env)
    old_stop = response(await env.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    stopped = response(await post(env, STOP, old_stop), 200, StandingDispatchStopProof)
    replay = response(await post(env, ACTIVATE, original), 200, StandingDispatchActivationReceipt)
    assert replay == saved
    state = response(await env.client.get(STATE, headers=headers()), 200, StandingDispatchState)
    assert state.dispatch_ready is False and state.stopped_through_epoch == saved.epoch
    assert state.fence_token == stopped.state.fence_token
    fresh = activation(env, "synthetic-next-activation")
    next_receipt = response(
        await post(env, ACTIVATE, fresh), 200, StandingDispatchActivationReceipt
    )
    stale = response(await post(env, STOP, old_stop), 200, StandingDispatchStopProof)
    stale.validate_request(old_stop)
    assert stale.target_fenced is True and stale.state.epoch == next_receipt.epoch == 2
    assert stale.state.dispatch_ready is True and stale.state.stopped_through_epoch == 1
    assert stale.state.fence_token == stopped.state.fence_token


async def test_lease_exact_retry_keeps_expiry_and_new_sequence_can_renew(env):
    original_renewal, lease, _, _ = await live(env)
    env.clock.value = 15999
    assert (
        response(
            await env.client.get(STATE, headers=headers()), 200, StandingDispatchState
        ).dispatch_ready
        is True
    )
    env.clock.value = 16000
    expired = response(await env.client.get(STATE, headers=headers()), 200, StandingDispatchState)
    assert expired.producer is None and expired.dispatch_ready is False
    old = response(await post(env, RENEW, original_renewal), 200, StandingProducerReceipt)
    assert old == lease and old.expires_monotonic_ms == 16000
    assert (
        response(
            await env.client.get(STATE, headers=headers()), 200, StandingDispatchState
        ).dispatch_ready
        is False
    )
    renewed = response(
        await post(env, RENEW, renewal(env, sequence=2)), 200, StandingProducerReceipt
    )
    assert renewed.issued_monotonic_ms == 16000 and renewed.expires_monotonic_ms == 31000
    assert renewed.lease_token != lease.lease_token
    assert (
        response(
            await env.client.get(STATE, headers=headers()), 200, StandingDispatchState
        ).dispatch_ready
        is True
    )
    assert (
        env.store.db.execute("SELECT count(*) FROM standing_dispatch_producers").fetchone()[0] == 1
    )


async def test_monotonic_rollback_fails_readiness_closed_but_context_and_stop_remain_possible(env):
    await live(env)
    env.clock.value = 999
    state = response(await env.client.get(STATE, headers=headers()), 200, StandingDispatchState)
    assert state.dispatch_ready is False and state.producer is None
    response(await post(env, RENEW, renewal(env, sequence=2)), 503)
    calls = env.clock.calls
    stop = response(await env.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    assert env.clock.calls == calls
    proof = response(await post(env, STOP, stop), 200, StandingDispatchStopProof)
    assert proof.target_fenced is True and proof.state.dispatch_ready is False


async def test_unknown_backend_generation_or_future_lookup_never_becomes_absence_proof(env):
    original = activation(env)
    for field, value in (
        ("expected_revision", 1),
        ("expected_instance", UNKNOWN),
        ("expected_backend", "d" * 32),
    ):
        changed = original.model_dump(mode="json")
        changed["mode_change"][field] = value
        response(await post(env, LOOKUP, StandingDispatchActivation.model_validate(changed)), 409)
    changed = original.model_dump(mode="json")
    changed["expected_epoch"] = 1
    response(await post(env, LOOKUP, StandingDispatchActivation.model_validate(changed)), 409)
    before = rows(env.store)
    for path in (ACTIVATE, STOP, RENEW):
        raw = packet(env, path).model_dump(mode="json")
        target = raw["mode_change"] if path == ACTIVATE else raw
        target["expected_instance"] = UNKNOWN
        kind = {
            ACTIVATE: StandingDispatchActivation,
            STOP: StandingDispatchStop,
            RENEW: StandingProducerRenewal,
        }[path]
        response(await post(env, path, kind.model_validate(raw)), 409)
    assert rows(env.store) == before


async def test_same_sqlite_restart_keeps_intent_but_requires_current_native_lease(factory):
    first = await factory()
    old_renewal, _, original, saved = await live(first)
    old_context = response(
        await first.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop
    )
    path = first.path
    await close_env(first)
    current = await factory(path=path, clock=MonotonicClock(0))
    state = response(await current.client.get(STATE, headers=headers()), 200, StandingDispatchState)
    assert state.backend_id == original.mode_change.expected_backend
    assert state.instance_id != original.mode_change.expected_instance
    assert state.mode == "autonomous" and state.epoch == saved.epoch
    assert state.dispatch_ready is False and state.producer is None
    lookup = response(await post(current, LOOKUP, original), 200, StandingDispatchActivationLookup)
    assert lookup.status == "recorded" and lookup.receipt == saved
    assert lookup.state.dispatch_ready is False
    assert (
        response(await post(current, ACTIVATE, original), 200, StandingDispatchActivationReceipt)
        == saved
    )
    stale = old_renewal.model_dump(mode="json")
    stale["sequence"] = 2
    response(await post(current, RENEW, StandingProducerRenewal.model_validate(stale)), 409)
    response(
        await post(
            current, RENEW, renewal(current, producer="00000000-0000-0000-0000-000000000102")
        ),
        200,
        StandingProducerReceipt,
    )
    assert (
        response(
            await current.client.get(STATE, headers=headers()), 200, StandingDispatchState
        ).dispatch_ready
        is True
    )
    proof = response(await post(current, STOP, old_context), 200, StandingDispatchStopProof)
    assert proof.target_fenced is True and proof.state.dispatch_ready is False


async def test_replacement_backend_refuses_original_activation_and_stop(factory):
    first = await factory()
    _, _, original, _ = await live(first)
    stop = response(await first.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    replacement = await factory()
    for path, request in ((ACTIVATE, original), (LOOKUP, original), (STOP, stop)):
        response(await post(replacement, path, request), 409)
    state = response(
        await replacement.client.get(STATE, headers=headers()), 200, StandingDispatchState
    )
    assert state.backend_id != original.mode_change.expected_backend
    assert state.epoch == 0 and state.producer is None


@pytest.mark.parametrize("action", ("activate", "stop", "renew"))
async def test_mutating_routes_pass_absolute_deadline_and_cancellation_into_native(
    env, monkeypatch, action
):
    if action == "activate":
        response(await post(env, RENEW, renewal(env)), 200, StandingProducerReceipt)
    path = {"activate": ACTIVATE, "stop": STOP, "renew": RENEW}[action]
    original = packet(env, path)
    actual = getattr(env.dispatch, action)
    observed = []

    def instrumented(request, *, deadline=None, cancelled=None):
        assert type(deadline) in (float, int) and 0 < deadline - time.monotonic() <= 25
        assert callable(cancelled) and cancelled() is False
        observed.append(True)
        return actual(request, deadline=deadline, cancelled=cancelled)

    monkeypatch.setattr(env.dispatch, action, instrumented)
    kind = {
        "activate": StandingDispatchActivationReceipt,
        "stop": StandingDispatchStopProof,
        "renew": StandingProducerReceipt,
    }[action]
    response(await post(env, path, original), 200, kind)
    assert observed == [True]


async def test_corrupt_native_dispatch_proof_is_unavailable_and_never_repaired(env):
    with env.store.lock, env.store.db:
        env.store.db.execute("UPDATE standing_dispatch_gate SET epoch=1")
    before = rows(env.store)
    response(await env.client.get(STATE, headers=headers()), 503)
    response(await env.client.get(CONTEXT, headers=headers()), 503)
    assert rows(env.store) == before
    response(await env.client.get(DETAIL, headers=headers()), 200, StandingResponsibilitySnapshot)


async def test_dispatch_registration_failure_does_not_disable_definition_or_mode_api(
    factory, monkeypatch
):
    def unavailable(self):
        raise StandingUnavailable

    monkeypatch.setattr(StandingDispatchOwner, "register", unavailable)
    current = await factory()
    assert current.app.state.standing_dispatch_initialized is False
    response(await current.client.get(STATE, headers=headers()), 503)
    response(await current.client.get(CONTEXT, headers=headers()), 503)
    response(
        await current.client.get(DETAIL, headers=headers()), 200, StandingResponsibilitySnapshot
    )
    ordinary = response(await current.client.get("/v1/agent-control/state", headers=headers()), 200)
    assert ordinary["mode"] == "on_demand" and ordinary["standing_work_available"] is False


async def test_compatibly_omitted_dispatch_capability_is_unavailable_not_implicitly_discovered(
    tmp_path,
):
    tmp_path.chmod(0o700)
    store = Store(tmp_path / "omitted.sqlite")
    control = AgentControl(store, new_store=store.agent_control.new_store)
    owner = StandingOwner(store, control)
    dispatch = StandingDispatchOwner(store, control, monotonic_ms=MonotonicClock())
    operations = None
    try:
        control.register()
        owner.register()
        dispatch.register()
        native = SimpleNamespace(dispatch=dispatch)
        app = FastAPI()
        operations = install_standing_owner_routes(app, owner, AUTH)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://omitted.test"
        ) as client:
            for method, path in ROUTES:
                if method == "GET":
                    result = await client.get(path, headers=headers())
                else:
                    result = await client.post(
                        path,
                        content=canonical_bytes(packet(native, path)),
                        headers=headers(
                            "agent" if path == RENEW else "owner",
                            **{"Content-Type": "application/json"},
                        ),
                    )
                response(result, 503)
            response(
                await client.get(DETAIL, headers=headers()), 200, StandingResponsibilitySnapshot
            )
    finally:
        if operations is not None:
            await operations.close()
        dispatch.close()
        owner.close()
        control.close()
        store.close()


async def test_actual_generation_cap_still_exposes_null_controller_context_and_emergency_stop(
    factory,
):
    first = await factory()
    _, _, _, saved = await live(first)
    path = first.path
    await close_env(first)
    store = Store(path)
    try:
        # Every generation here is genuinely issued and retired by the native
        # public capability. No smaller cap or fabricated SQL ACK rows.
        while (
            store.db.execute("SELECT count(*) FROM agent_control_generations").fetchone()[0] < 10000
        ):
            control = AgentControl(store, new_store=False)
            control.register()
            control.close()
        assert (
            store.db.execute("SELECT count(*) FROM agent_control_generations").fetchone()[0]
            == 10000
        )
    finally:
        store.close()
    limited = await factory(path=path)
    assert limited.app.state.standing_dispatch_initialized is True
    state = response(await limited.client.get(STATE, headers=headers()), 200, StandingDispatchState)
    assert state.instance_id is None and state.control_writable is False and state.producer is None
    assert state.epoch == saved.epoch and state.dispatch_ready is False
    calls = limited.clock.calls
    stop = response(await limited.client.get(CONTEXT, headers=headers()), 200, StandingDispatchStop)
    assert stop.expected_instance == saved.mode_receipt.service_instance
    assert stop.issued_epoch == saved.epoch and limited.clock.calls == calls
    proof = response(await post(limited, STOP, stop), 200, StandingDispatchStopProof)
    assert proof.target_fenced is True and proof.state.instance_id is None
    assert proof.state.stopped_through_epoch == saved.epoch and proof.state.mode == "autonomous"


async def test_dispatch_ingress_five_second_deadline_is_503_not_input_failure(env, monkeypatch):
    started, stream_settled = asyncio.Event(), asyncio.Event()
    native_calls = []
    actual = env.dispatch.renew

    def instrumented(*args, **kwargs):
        native_calls.append(True)
        return actual(*args, **kwargs)

    monkeypatch.setattr(env.dispatch, "renew", instrumented)

    async def body():
        try:
            yield b"{"
            started.set()
            await asyncio.Event().wait()
        finally:
            stream_settled.set()

    beginning = time.monotonic()
    task = asyncio.create_task(
        env.client.post(
            RENEW, content=body(), headers=headers("agent", **{"Content-Type": "application/json"})
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        response(await asyncio.wait_for(task, 10), 503)
        assert time.monotonic() - beginning >= 5
        await asyncio.wait_for(stream_settled.wait(), 5)
        async with asyncio.timeout(5):
            while env.operations.active_operations:
                await asyncio.sleep(0.005)
        assert native_calls == [] and env.operations.active_operations == 0
        assert (
            env.store.db.execute("SELECT count(*) FROM standing_dispatch_producers").fetchone()[0]
            == 0
        )
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await env.operations.close()


async def test_dispatch_total_deadline_retains_real_native_owner_until_actual_join(
    env, monkeypatch
):
    started, release = threading.Event(), threading.Event()
    observed_cancel = []
    actual = env.dispatch.renew

    def blocked(request, *, deadline=None, cancelled=None):
        started.set()
        assert release.wait(45), "Owned native renewal was not released"
        observed_cancel.append(cancelled is not None and cancelled())
        return actual(request, deadline=deadline, cancelled=cancelled)

    monkeypatch.setattr(env.dispatch, "renew", blocked)
    original = renewal(env)
    beginning = time.monotonic()
    task = asyncio.create_task(post(env, RENEW, original))
    close_task = None
    try:
        await entered(started)
        response(await asyncio.wait_for(task, 30), 503)
        assert time.monotonic() - beginning >= 25
        assert env.operations.active_operations == 1
        close_task = asyncio.create_task(env.operations.close())
        await asyncio.sleep(0)
        assert not close_task.done() and env.operations.active_operations == 1
        release.set()
        await asyncio.wait_for(close_task, 5)
        assert observed_cancel == [True] and env.operations.active_operations == 0
        assert (
            env.store.db.execute("SELECT count(*) FROM standing_dispatch_producers").fetchone()[0]
            == 0
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if close_task is not None:
            await close_task
        await env.operations.close()


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("cancel_shutdown", (False, True))
async def test_single_shared_64_registry_and_lifespan_join_both_native_capabilities(
    env, monkeypatch, cancel_shutdown
):
    loop = asyncio.get_running_loop()
    previous_executor = getattr(loop, "_default_executor", None)
    executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="dispatch-http64-owned")
    all_entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    actual_definition, actual_renew = env.owner.change, env.dispatch.renew
    entries = {"definition": 0, "dispatch": 0}
    observed_cancel, closed = [], []

    def gate(kind, actual, request, *, deadline=None, cancelled=None):
        with guard:
            entries[kind] += 1
            if sum(entries.values()) == 64:
                all_entered.set()
        assert release.wait(45), "Owned mixed64 native gate was not released"
        with guard:
            observed_cancel.append(cancelled is not None and cancelled())
        return actual(request, deadline=deadline, cancelled=cancelled)

    def blocked_definition(request, **kwargs):
        return gate("definition", actual_definition, request, **kwargs)

    def blocked_renew(request, **kwargs):
        return gate("dispatch", actual_renew, request, **kwargs)

    def close_hook(label, actual):
        def close():
            closed.append(label)
            return actual()

        return close

    monkeypatch.setattr(env.owner, "change", blocked_definition)
    monkeypatch.setattr(env.dispatch, "renew", blocked_renew)
    for label, capability in (
        ("definition", env.owner),
        ("dispatch", env.dispatch),
        ("control", env.control),
    ):
        monkeypatch.setattr(capability, "close", close_hook(label, capability.close))
    tasks, close_task = [], None
    try:
        loop.set_default_executor(executor)
        original_definition, original_renewal = definition(env), renewal(env)
        for _ in range(32):
            tasks.append(asyncio.create_task(post(env, DEFINITIONS, original_definition)))
            tasks.append(asyncio.create_task(post(env, RENEW, original_renewal)))
        # Readiness means ALL64 actually entered their native callable, not
        # merely that a default executor queued64 facades behind fewer workers.
        await entered(all_entered)
        assert entries == {"definition": 32, "dispatch": 32}
        assert env.operations.active_operations == 64
        response(await asyncio.wait_for(env.client.get(STATE, headers=headers()), 5), 503)
        response(await asyncio.wait_for(env.client.get(DETAIL, headers=headers()), 5), 503)
        assert env.operations.active_operations == 64
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert env.operations.active_operations == 64
        close_task = asyncio.create_task(close_env(env))
        await asyncio.sleep(0)
        assert not close_task.done() and closed == []
        if cancel_shutdown:
            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done() and closed == []
        release.set()
        outcome = (await asyncio.gather(close_task, return_exceptions=True))[0]
        if cancel_shutdown:
            assert isinstance(outcome, asyncio.CancelledError)
        else:
            assert outcome is None
        assert env.operations.active_operations == 0
        assert len(observed_cancel) == 64 and all(observed_cancel)
        assert (
            len(closed) == 3
            and set(closed[:2]) == {"definition", "dispatch"}
            and closed[2] == "control"
        )
        with sqlite3.connect(env.path) as observer:
            assert (
                observer.execute("SELECT count(*) FROM standing_owner_changes").fetchone()[0] == 0
            )
            assert (
                observer.execute("SELECT count(*) FROM standing_dispatch_producers").fetchone()[0]
                == 0
            )
            assert (
                observer.execute("SELECT count(*) FROM standing_dispatch_activations").fetchone()[0]
                == 0
            )
        assert env.closed is True
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        try:
            await env.operations.close()
            if not env.closed:
                await close_env(env)
        finally:
            if previous_executor is not None:
                loop.set_default_executor(previous_executor)
            executor.shutdown(wait=True)
