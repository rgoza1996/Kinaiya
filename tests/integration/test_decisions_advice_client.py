"""Actual Decisions and Session routes with a synthetic measured model wire."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager

import httpx
import pytest
from test_agent_advice_api import application
from test_agent_advice_export import TOKENS
from test_agent_advice_operations import call, measured
from test_agent_advice_operations import env as env

from packages.agent_adapters.decisions_advice import (
    AdviceConflict,
    AdviceDispatchRefused,
    AdviceUnavailable,
    DecisionsAdviceClient,
)
from packages.contracts.agent_advice_operations import AgentAdviceLookup
from packages.contracts.client import JournalClient
from packages.contracts.operation_ownership import ActualOperation, actual_owner

URL = "http://127.0.0.1:8763"


class TracedTransport(httpx.AsyncBaseTransport):
    def __init__(self, app, intercept=None):
        self.asgi = httpx.ASGITransport(app=app)
        self.intercept = intercept
        self.calls = Counter()
        self.closes = 0

    async def handle_async_request(self, request):
        assert request.url.host == "127.0.0.1" and request.url.port == 8763
        assert request.headers["authorization"] == "Bearer " + TOKENS["agent"]
        assert request.headers["accept-encoding"] == "identity"
        route = request.url.path.rsplit("/", 1)[-1]
        self.calls[route] += 1
        if self.intercept is not None:
            return await self.intercept(self.asgi, request, route)
        return await self.asgi.handle_async_request(request)

    async def aclose(self):
        self.closes += 1
        await self.asgi.aclose()


@asynccontextmanager
async def connected(env, intercept=None, **limits):
    app, _ = application(env)
    transport = TracedTransport(app, intercept)
    client = DecisionsAdviceClient(URL, TOKENS["agent"], transport=transport, **limits)
    async with app.router.lifespan_context(app):
        yield client, transport


def responsible_parent():
    owner = ActualOperation(lambda _: None)
    enrollment = object()
    owner.hold(enrollment)
    return owner, enrollment


async def test_local_task_guard_runs_at_actual_dispatch_and_keeps_the_key_spent(env):
    original = call(env)
    lookup = AgentAdviceLookup.of(original)
    parent, enrollment = responsible_parent()
    checks = []

    def revoked():
        checks.append(asyncio.current_task())
        return False

    async with connected(env) as (client, transport):
        with actual_owner(parent), pytest.raises(AdviceDispatchRefused):
            await client.submit(original, current=revoked)
        parent.release(enrollment)
        assert checks and checks == [checks[0]] and checks[0] is not asyncio.current_task()
        assert client.cleanup_confirmed(lookup)
        assert await client.submit(original, current=lambda: True) is None
        assert not env.owner._originals and not transport.calls
        assert parent.finished and not parent.failed
        assert await client.close() is True


async def test_lost_ack_observes_one_qualified_prediction_and_confirmed_remote_stop(
    env, monkeypatch
):
    wire, _, _ = measured(env, monkeypatch, hold="model-reply")
    wire.hold_cancel_ack = True
    journal = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    env.owner.journal = journal

    async def drop_ack(asgi, request, route):
        response = await asgi.handle_async_request(request)
        if route == "requests":
            assert response.status_code == 202
            await response.aclose()
            raise httpx.ReadError("Synthetic lost advice acknowledgment")
        return response

    original = call(env)
    lookup = AgentAdviceLookup.of(original)
    parent, enrollment = responsible_parent()
    try:
        async with connected(env, drop_ack, capacity=1) as (client, transport):
            runtime = await client.runtime()
            assert (
                runtime.new_request_available and runtime.instance_id == original.expected_instance
            )
            with actual_owner(parent), pytest.raises(AdviceUnavailable):
                await client.submit(original)
            parent.release(enrollment)
            await asyncio.wait_for(wire.entered.wait(), 3)
            assert parent.handles and not parent.finished
            assert (await client.submit(original)).phase == "running"
            assert transport.calls["requests"] == wire.predictions == 1
            assert (await client.cancel(lookup)).cancellation_requested
            await asyncio.wait_for(wire.cancel_entered.wait(), 3)
            assert await client.close(timeout=0.05) is False
            assert not parent.finished and transport.closes == 0
            assert transport.calls["cancel"] == 1
            wire.cancel_release.set()
            assert (await env.owner.wait(lookup)).phase == "cancelled"
            terminal = await client.lookup(lookup)
            assert terminal.phase == "cancelled" and terminal.cleanup == "confirmed"
            assert terminal.model_pin is not None and terminal.generation_admission_observed
            await asyncio.wait_for(parent.join(), 1)
            assert parent.finished and not parent.failed
            assert await client.close() is True
            assert transport.closes == 1 and wire.predictions == 1
    finally:
        wire.cancel_release.set()
        wire.release.set()
        await journal.close()


@pytest.mark.parametrize("interrupt", ["deadline", "cancelled-observer"])
async def test_absent_observation_cannot_retire_a_late_original(env, interrupt):
    sending, release = asyncio.Event(), asyncio.Event()

    async def delay_ingress(asgi, request, route):
        if route == "requests":
            sending.set()
            await release.wait()
        return await asgi.handle_async_request(request)

    env.settings["hold"] = True
    original = call(env)
    lookup = AgentAdviceLookup.of(original)
    parent, enrollment = responsible_parent()
    try:
        async with connected(env, delay_ingress, capacity=1) as (client, transport):
            with actual_owner(parent):
                facade = asyncio.create_task(
                    client.submit(original, timeout=0.05 if interrupt == "deadline" else 3)
                )
            await asyncio.wait_for(sending.wait(), 1)
            if interrupt == "cancelled-observer":
                facade.cancel()
            expected = AdviceUnavailable if interrupt == "deadline" else asyncio.CancelledError
            with pytest.raises(expected):
                await facade
            parent.release(enrollment)
            assert await client.lookup(lookup) is None
            assert await client.cancel(lookup) is None
            assert parent.handles and not parent.finished
            with pytest.raises(AdviceUnavailable):
                await client.submit(call(env, "another-original"))
            assert transport.calls["requests"] == 1
            release.set()
            await asyncio.wait_for(env.created.wait(), 3)
            assert (await client.lookup(lookup)).phase == "running"
            assert (await client.cancel(lookup)).cancellation_requested
            assert (await env.owner.wait(lookup)).cleanup == "confirmed"
            assert (await client.lookup(lookup)).phase == "cancelled"
            assert await client.close() is True and parent.finished
            assert len(env.adapters) == 1 and transport.calls["requests"] == 1
    finally:
        release.set()


class HeldResponse(httpx.AsyncByteStream):
    def __init__(self, stream, entered, release, *, fail=False):
        self.stream = stream
        self.entered, self.release = entered, release
        self.fail = fail
        self.closes = 0

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        self.closes += 1
        self.entered.set()
        await self.release.wait()
        await self.stream.aclose()
        if self.fail:
            raise httpx.CloseError("Synthetic private close exception detail")


@pytest.mark.parametrize("close_failure", [False, True])
async def test_remote_terminal_does_not_erase_actual_client_response_close(env, close_failure):
    entered, release = asyncio.Event(), asyncio.Event()
    bodies = []

    async def hold_ack(asgi, request, route):
        response = await asgi.handle_async_request(request)
        if route == "requests":
            body = HeldResponse(response.stream, entered, release, fail=close_failure)
            bodies.append(body)
            response.stream = body
        return response

    original = call(env)
    lookup = AgentAdviceLookup.of(original)
    parent, enrollment = responsible_parent()
    async with connected(env, hold_ack, capacity=1) as (client, transport):
        try:
            with actual_owner(parent):
                facade = asyncio.create_task(client.submit(original))
            await asyncio.wait_for(entered.wait(), 1)
            parent.release(enrollment)
            assert (await env.owner.wait(lookup)).phase == "completed"
            observed = await client.lookup(lookup)
            assert (
                observed.phase == "completed"
                and observed.model_output == "  Raw synthetic advice Ω\n"
            )
            assert not parent.finished and bodies[0].closes == 1
            with pytest.raises(AdviceUnavailable):
                await client.submit(call(env, "before-local-close"))
            assert await client.close(timeout=0.01) is False
            release.set()
            if close_failure:
                with pytest.raises(AdviceUnavailable) as caught:
                    await facade
                assert "private close exception" not in str(caught.value)
                assert parent.failed and not parent.finished
                assert await client.close(timeout=0.01) is False
                assert transport.closes == 0
            else:
                await facade
                await asyncio.wait_for(parent.join(), 1)
                assert parent.finished and not parent.failed
                assert await asyncio.gather(client.close(), client.close()) == [True, True]
                assert transport.closes == 1
            assert bodies[0].closes == 1 and transport.calls["requests"] == 1
        finally:
            release.set()


@pytest.mark.parametrize(
    "field",
    [
        "task",
        "objective_revision",
        "policy_sha256",
        "content_scope",
        "cleanup",
        "request",
        "length",
    ],
)
async def test_conflicting_remote_terminal_cannot_release_agent_or_resources(env, field):
    damage = True

    async def tamper(asgi, request, route):
        response = await asgi.handle_async_request(request)
        if route != "lookup" or not damage:
            return response
        await response.aread()
        value = response.json()
        observation = value["observation"]
        if field == "task":
            observation["task"]["task_id"] = "12345678-1234-1234-1234-123456789abc"
        elif field == "objective_revision":
            observation[field] = 2
        elif field == "policy_sha256":
            observation[field] = "b" * 64
            observation["model_pin"][field] = "b" * 64
        elif field == "content_scope":
            observation[field] = "public_synthetic"
        elif field == "cleanup":
            observation["phase"] = "closing"
        elif field == "request":
            value["request"]["request_sha256"] = "c" * 64
            observation["request_sha256"] = "c" * 64
        else:
            return httpx.Response(
                200, json=value, headers={"Cache-Control": "no-store", "Content-Length": "1"}
            )
        return httpx.Response(200, json=value, headers={"Cache-Control": "no-store"})

    original = call(env, content_scope="local_only")
    lookup = AgentAdviceLookup.of(original)
    parent, enrollment = responsible_parent()
    async with connected(env, tamper, capacity=1) as (client, transport):
        with actual_owner(parent):
            await client.submit(original)
        parent.release(enrollment)
        assert (await env.owner.wait(lookup)).phase == "completed"
        with pytest.raises(AdviceUnavailable):
            await client.lookup(lookup)
        assert parent.handles and not parent.finished
        with pytest.raises(AdviceConflict):
            await client.submit(call(env, content_scope="public_synthetic"))
        assert transport.calls["requests"] == 1
        damage = False
        assert (await client.lookup(lookup)).cleanup == "confirmed"
        assert parent.finished and not parent.failed
        assert await client.close() is True


async def test_remote_controller_change_and_unqualified_cleanup_do_not_reissue(env, monkeypatch):
    wire, _, _ = measured(env, monkeypatch, hold="model-reply")
    wire.cancel_ack = False
    replaced = False

    async def controller(asgi, request, route):
        if replaced:
            return httpx.Response(409, json={"detail": "Synthetic retired controller"})
        return await asgi.handle_async_request(request)

    original = call(env)
    lookup = AgentAdviceLookup.of(original)
    parent, enrollment = responsible_parent()
    try:
        async with connected(env, controller, capacity=1) as (client, transport):
            with actual_owner(parent):
                await client.submit(original)
            parent.release(enrollment)
            await asyncio.wait_for(wire.entered.wait(), 3)
            replaced = True
            with pytest.raises(AdviceConflict):
                await client.submit(original)
            assert not parent.finished and transport.calls["requests"] == 1
            replaced = False
            assert (await client.cancel(lookup)).cancellation_requested
            assert (await env.owner.wait(lookup)).phase == "cleanup_unconfirmed"
            assert (await client.lookup(lookup)).cleanup == "unconfirmed"
            assert parent.failed and not parent.finished
            assert await client.close() is False and transport.closes == 0
            assert wire.predictions == transport.calls["requests"] == transport.calls["cancel"] == 1
    finally:
        wire.release.set()


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8763",
        "https://127.0.0.1:8763",
        URL + "/other",
        URL + "?x=1",
        "http://127.0.0.1:0",
    ],
)
def test_only_literal_loopback_origin_can_receive_the_agent_token(url):
    with pytest.raises(ValueError, match="literal loopback"):
        DecisionsAdviceClient(url, TOKENS["agent"])


async def test_wrong_credential_and_redirect_cannot_create_remote_work(env):
    app, _ = application(env)
    wrong = DecisionsAdviceClient(URL, TOKENS["owner"], transport=httpx.ASGITransport(app=app))
    redirects = []

    async def redirect(request):
        redirects.append(request.url)
        return httpx.Response(307, headers={"Location": "http://redirect.invalid/advice"})

    redirected = DecisionsAdviceClient(
        URL, TOKENS["agent"], transport=httpx.MockTransport(redirect)
    )
    async with app.router.lifespan_context(app):
        for client in (wrong, redirected):
            with pytest.raises(AdviceUnavailable):
                await client.runtime()
            assert await client.close() is True
    assert len(redirects) == 1 and redirects[0].host == "127.0.0.1"
    assert not env.adapters and not env.owner._originals
