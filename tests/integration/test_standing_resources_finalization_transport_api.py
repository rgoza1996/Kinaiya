"""Independent Resources RPC gate through actual ASGI/Session/physical owners.

This reuses the frozen disposable CPU Whistle/native SQLite fixture. The new
RPC implementation was not read. Original response bodies and barrier scopes
are observed, never replaced by fabricated cleanup or physical testimony.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from test_resource_handoff import action
from test_standing_physical_capture_fences import physical_calls
from test_standing_physical_finalization import (
    Body,
    acquired,
    bound,
    closure,
    gate_entered,
    handoff,
    recorded_reservation,
    reports,
)
from test_standing_physical_finalization import factory as factory
from test_standing_physical_finalization import physical as physical
from test_standing_task_admission import SECRET

from packages.contracts.operation_ownership import current_owner
from packages.contracts.standing_resource_finalization import (
    StandingResourceFinalizationReceipt,
    finalization_bytes,
)
from packages.contracts.standing_work import StandingUnavailable
from services.resources.standing_client import StandingResourceClient

FINALIZE = "/v1/resources/standing/finalization"
LOOKUP = FINALIZE + "/lookup"
ROUTES = (FINALIZE, LOOKUP)
CAP = "X-Kinaiya-Standing-Capability"
MARKER = "SYNTHETIC-PRIVATE-RESOURCE-RPC-INPUT"


def method(client, name):
    invoke = getattr(client, name, None)
    assert callable(invoke), "Frozen Resources finalization transport capability is absent"
    return invoke


def response(reply, status):
    assert reply.status_code == status, (reply.status_code, reply.text)
    assert reply.headers.get("cache-control") == "no-store"
    assert MARKER not in reply.text and SECRET.hex() not in reply.text


@asynccontextmanager
async def rpc(value, *, lose_reply=False):
    # Reuse the fixture's existing composed app, not a second coordinator or
    # registration. Its supplied transport is genuinely ASGITransport.
    app = value.http._transport.app
    bodies, requests, replies, lost = [], [], [], []
    closes = []

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.delegate = httpx.ASGITransport(app=app)

        async def handle_async_request(self, request):
            requests.append(request)
            reply = await self.delegate.handle_async_request(request)
            reply.stream = Body(reply.stream)
            bodies.append(reply.stream)
            replies.append(reply)
            assert reply.headers.get("cache-control") == "no-store"
            if (
                lose_reply
                and not lost
                and request.url.path == FINALIZE
                and reply.status_code == 200
            ):
                # The original actual server response fully completes and its
                # real stream closes once before this synthetic ACK is lost.
                raw = await reply.aread()
                packet = StandingResourceFinalizationReceipt.from_json(raw)
                packet.validate_request(value.request)
                assert bodies[-1].closed and bodies[-1].closes == 1 and reply.is_closed
                lost.append(packet)
                raise httpx.ReadError("Synthetic completed Resources reply loss", request=request)
            return reply

        async def aclose(self):
            closes.append(True)
            await self.delegate.aclose()

    client = StandingResourceClient("http://127.0.0.1:8764", "a" * 32, transport=Transport())
    try:
        yield client, requests, lost
    finally:
        try:
            await client.close()
        except StandingUnavailable:
            pass
        await asyncio.wait_for(asyncio.shield(client._close_task), 30)
        assert client.http.is_closed and not client._submit_exchanges and closes == [True]
        assert all(body.closes == 1 and body.closed and not body.failed for body in bodies)
        assert all(reply.is_closed for reply in replies)


async def exact_native_report(value, receipt, disposition, restoration, reservation):
    assert type(receipt) is StandingResourceFinalizationReceipt
    receipt = receipt.validate_request(value.request)
    report = receipt.finalization
    assert (
        report.context.request == value.request and report.context.binding == value.binding.request
    )
    assert report.context.binding.claim == value.claim
    assert report.context.closure.initial == value.initial
    assert report.context.observation.reservation == reservation
    assert report.resource_disposition == disposition and report.restoration == restoration
    native = await value.agent.standing_resource_finalization_lookup(value.request)
    assert native is not None and native.finalization == report
    assert all(body == report.model_dump(mode="json") for body in reports(value))
    assert (
        value.session.store.db.execute(
            "SELECT count(*) FROM standing_resource_finalizations"
        ).fetchone()[0]
        == 1
    )
    return report


async def test_real_rpc_lookup_null_then_full_no_reservation_uses_one_original_ingress_scope(
    physical, monkeypatch
):
    value = await physical()
    await bound(value)
    barrier = value.coordinator.standing_barrier
    scopes, contexts = [], []
    actual_admit = barrier.admit
    actual_context = value.gateway.standing_resource_finalize_context

    def admit(*args, **kwargs):
        scope = actual_admit(*args, **kwargs)
        scopes.append(scope)
        return scope

    async def context(request):
        contexts.append(current_owner())
        return await actual_context(request)

    monkeypatch.setattr(barrier, "admit", admit)
    monkeypatch.setattr(value.gateway, "standing_resource_finalize_context", context)
    async with rpc(value) as (client, requests, _):
        # Existing methods remain compatible with the same exact native bind.
        known = await client.lookup_standing_binding(value.claim)
        assert known.request == value.binding.request
        assert await method(client, "lookup_standing_finalization")(value.request) is None
        await closure(value)
        sealed = await client.seal_standing_claim(value.request)
        assert sealed.binding == value.binding.request and reports(value) == []
        scopes.clear()
        contexts.clear()
        before = len(value.original_calls)
        receipt = await method(client, "finalize_standing_claim")(value.request)
        originals = value.original_calls[before:]
        assert len(scopes) == len(originals) == 1
        name, call, work = originals[0]
        assert name == "finalize" and call.scope is scopes[0] and call.scope.hub is barrier
        assert contexts and all(owner is call.scope for owner in contexts)
        await asyncio.wait_for(call.scope.join(), 30)
        assert work.done() and not work.cancelled() and work.exception() is None
        assert call.scope.finished and not call.scope.failed
        report = await exact_native_report(value, receipt, "no-reservation", "not-required", None)
        known = await method(client, "lookup_standing_finalization")(value.request)
        assert known.finalization == report
        assert [request.url.path for request in requests] == [
            "/v1/resources/standing/binding/lookup",
            LOOKUP,
            "/v1/resources/standing/seal",
            FINALIZE,
            LOOKUP,
        ]
        assert all(CAP not in request.headers for request in requests)


async def test_actual_acquired_cpu_lease_lost_rpc_ack_lookup_and_replay_do_not_restore_twice(
    physical,
):
    value = await physical()
    reservation = await acquired(value)
    await closure(value)
    async with rpc(value, lose_reply=True) as (client, requests, lost):
        with pytest.raises(StandingUnavailable):
            await method(client, "finalize_standing_claim")(value.request)
        assert len(lost) == 1
        restored = value.coordinator.manager.owned_identity("whistle")
        assert restored is not None and value.coordinator.manager.is_current(restored)
        before = physical_calls(value)
        known = await method(client, "lookup_standing_finalization")(value.request)
        report = await exact_native_report(
            value, known, "matching-lease-finalized", "restored", reservation
        )
        assert report == lost[0].finalization
        replay = await method(client, "finalize_standing_claim")(value.request)
        assert replay.finalization == report and physical_calls(value) == before
        assert value.coordinator.manager.is_current(restored) and len(reports(value)) == 1
        assert [request.url.path for request in requests] == [FINALIZE, LOOKUP, FINALIZE]


async def test_real_large_registered_graph_crosses_old262144_response_clamp(physical):
    value = await physical(large=True)
    await bound(value)
    refused = await handoff(value)
    assert refused["ok"] is False and value.effect_checks == 0 and value.starts == []
    reservation = recorded_reservation(value)
    await closure(value)
    async with rpc(value) as (client, requests, _):
        receipt = await method(client, "finalize_standing_claim")(value.request)
        report = await exact_native_report(
            value, receipt, "registered-no-lease", "not-required", reservation
        )
        assert 262144 < len(finalization_bytes(receipt)) <= 819200
        assert len(finalization_bytes(report)) <= 802816
        assert len(requests) == 1 and len(requests[0].content) <= 147456
        assert requests[0].content == finalization_bytes(value.request)


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("role", ("owner", "resources", "missing"))
async def test_agent_only_routes_authenticate_before_body_and_always_disable_cache(
    physical, path, role
):
    value = await physical()
    read = []

    async def body():
        read.append(True)
        yield MARKER.encode()

    token = {"owner": "o", "resources": "r"}.get(role)
    headers = {"Authorization": "Bearer " + token * 32} if token is not None else {}
    reply = await value.http.post(path, content=body(), headers=headers)
    response(reply, 401 if role == "missing" else 403)
    assert read == [] and reports(value) == [] and value.original_calls == []


@pytest.mark.parametrize("fault", ("query", "private-capability", "media", "oversize", "duplicate"))
async def test_new_api_ingress_retains_strict_bounds_and_no_store_errors(physical, fault):
    value = await physical()
    await bound(value)
    path = FINALIZE
    body = finalization_bytes(value.request)
    headers = {"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"}
    status = 422
    if fault == "query":
        path += "?untrusted=" + MARKER
    elif fault == "private-capability":
        headers[CAP] = SECRET.hex()
    elif fault == "media":
        headers["Content-Type"] = "text/plain"
        status = 415
    elif fault == "oversize":
        body = b" " * 147457
    else:
        body = b'{"schema_version":"1.0",' + body[1:]
    before = len(value.original_calls)
    reply = await value.http.post(path, content=body, headers=headers)
    response(reply, status)
    assert value.original_calls[before:] == [] and reports(value) == []


async def test_mixed_old_and_new_ingress_share_exact64_scopes_without_blocking_owner_off(physical):
    value = await physical()
    await bound(value)
    entered, release = asyncio.Event(), asyncio.Event()
    value.releases.append(release)
    scopes = {}
    barrier = value.coordinator.standing_barrier

    async def body(index):
        scope = current_owner()
        assert scope is not None and scope.hub is barrier
        scopes[index] = scope
        if len(scopes) == 64:
            entered.set()
        yield b"{"
        await release.wait()
        raw = finalization_bytes(value.request) if index == 63 else finalization_bytes(value.claim)
        yield raw[1:]

    pending = [
        asyncio.create_task(
            value.http.post(
                LOOKUP if index == 63 else "/v1/resources/standing/binding/lookup",
                content=body(index),
                headers={"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"},
            )
        )
        for index in range(64)
    ]
    value.tasks.extend(pending)
    try:
        await gate_entered(entered, 8)
        originals = tuple(scopes.values())
        assert len(set(originals)) == len(barrier.operations) == 64
        assert all(scope in barrier.operations and not scope.finished for scope in originals)
        before = len(value.native.requests)
        for path in ROUTES:
            reply = await value.http.post(
                path,
                content=finalization_bytes(value.request),
                headers={"Authorization": "Bearer " + "a" * 32, "Content-Type": "application/json"},
            )
            response(reply, 503)
        assert len(value.native.requests) == before and reports(value) == []
        off = await action(
            value.coordinator, "converse", key="rpc:saturated-owner-off", enabled=False
        )
        assert off["ok"] is True and not value.coordinator.intent.read().enabled
    finally:
        release.set()
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.wait_for(asyncio.gather(*(scope.join() for scope in originals)), 30)
    assert all(scope.finished and not scope.handles for scope in originals)
