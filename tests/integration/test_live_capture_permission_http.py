"""Real authenticated native permission APIs and retained original HTTP ownership."""

import asyncio

import httpx
import pytest

from packages.contracts.client import JournalClient
from services.common.auth import Auth
from services.session.api import create_app
from services.session.live_windows import publish
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import fence, registration
from tests.integration.test_live_window_publication import env as env
from tests.integration.test_live_window_publication import original

BASE = "/v2/transcript/live-captures"


@pytest.fixture
def wire(env):
    auth = Auth(
        {
            role: role + "-public-capture-token-000000000"
            for role in ("owner", "converse", "decisions", "agent", "resources")
        }
    )
    return auth, httpx.ASGITransport(app=create_app(env.store, auth))


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", "none"])
async def test_role_parity_and_native_permission_only(env, wire, role):
    auth, transport = wire
    request = registration(env)
    headers = {} if role == "none" else {"Authorization": "Bearer " + auth.tokens[role]}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://session.local", headers=headers
    ) as client:
        backend = await client.get(BASE + "/backend")
        lookup = await client.post(BASE + "/lookup", json=request.model_dump(mode="json"))
        registered = await client.post(BASE + "/register", json=request.model_dump(mode="json"))
        source = await client.post(
            BASE + "/source", json=original(env).reference().model_dump(mode="json")
        )
        stopped = await client.post(BASE + "/fence", json=fence(request).model_dump(mode="json"))
    denied = 401 if role == "none" else 403
    assert (
        backend.status_code
        == lookup.status_code
        == source.status_code
        == (200 if role in {"converse", "decisions"} else denied)
    )
    assert registered.status_code == stopped.status_code == (200 if role == "converse" else denied)
    local = LocalJournal(env.store, role)
    if role in {"converse", "decisions"}:
        assert await local.live_capture_backend() == request.backend
        assert (await local.lookup_live_capture(request)).original == request
        assert not (
            await local.live_capture_for_source(original(env).reference())
        ).capture_permits_source
        assert backend.headers["cache-control"] == lookup.headers["cache-control"] == "no-store"
    else:
        with pytest.raises(PermissionError):
            await local.live_capture_backend()
    if role != "converse":
        with pytest.raises(PermissionError):
            await local.register_live_capture(request)
        with pytest.raises(PermissionError):
            await local.fence_live_capture(fence(request))
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


async def test_real_client_original_registration_source_permission_and_fence(env, wire):
    auth, transport = wire
    client = JournalClient("http://session.local", auth.tokens["converse"], transport=transport)
    request = registration(env)
    try:
        assert await client.live_capture_backend() == request.backend
        assert not (await client.lookup_live_capture(request)).capture_open
        state = await client.register_live_capture(request)
        assert state.capture_open and (await client.lookup_live_capture(request)) == state
        source = publish(env.store, original(env))
        permission = await client.live_capture_for_source(source.original)
        assert permission.capture_permits_source and permission.capture.receipt == state.receipt
        stopped = await client.fence_live_capture(fence(request))
        assert stopped.receipt == state.receipt and not stopped.capture_open
        assert not (await client.live_capture_for_source(source.original)).capture_permits_source
        assert not client._submit_exchanges
    finally:
        await client.close()


async def test_cancelled_registration_transport_cannot_reactivate_a_pre_ack_fence(env, wire):
    auth, asgi = wire
    entered, release = asyncio.Event(), asyncio.Event()

    class Delayed(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/register"):
                entered.set()
                await release.wait()
            return await asgi.handle_async_request(request)

    client = JournalClient("http://session.local", auth.tokens["converse"], transport=Delayed())
    request = registration(env)
    task = asyncio.create_task(client.register_live_capture(request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(client._submit_exchanges) == 1
        stopped = await client.fence_live_capture(fence(request))
        assert stopped.receipt is None and stopped.fence is not None
        owners = [exchange.task for exchange in client._submit_exchanges]
        release.set()
        await asyncio.wait_for(asyncio.gather(*owners), 3)
        current = await client.lookup_live_capture(request)
        assert current == stopped and not current.capture_open and not client._submit_exchanges
    finally:
        release.set()
        await client.close()


async def test_committed_registration_lost_ack_lookup_does_not_replay(env, wire):
    auth, asgi = wire
    effects, closes = [], []

    class Lost(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            raise httpx.ReadError("Public synthetic lost acknowledgment")
            yield b""

        async def aclose(self):
            closes.append(True)
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await asgi.handle_async_request(request)
            if request.url.path.endswith("/register"):
                effects.append(request.content)
                response.stream = Lost(response.stream)
            return response

    client = JournalClient("http://session.local", auth.tokens["converse"], transport=Transport())
    request = registration(env)
    try:
        with pytest.raises(httpx.ReadError):
            await client.register_live_capture(request)
        state = await client.lookup_live_capture(request)
        assert state.capture_open and state.receipt.original == request
        assert len(effects) == len(closes) == 1
        assert not client._submit_exchanges
        assert env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "change", [{"epoch": True}, {"epoch": 1.0}, {"may_infer": True}, {"schema_version": "1.0"}]
)
async def test_untrusted_registration_fields_are_refused_before_effect(env, wire, change):
    auth, transport = wire
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + auth.tokens["converse"]},
    ) as client:
        response = await client.post(
            BASE + "/register", json=registration(env).model_dump(mode="json") | change
        )
    assert response.status_code == 422
    assert env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 0


@pytest.mark.parametrize(
    "case,status", [("duplicate", 422), ("oversize", 413), ("encoding", 415), ("query", 400)]
)
async def test_closed_wire_envelope_and_byte_bounds(env, wire, case, status):
    auth, transport = wire
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + auth.tokens["converse"]},
    ) as client:
        if case == "duplicate":
            response = await client.post(
                BASE + "/register",
                content=b'{"epoch":1,"epoch":2}',
                headers={"Content-Type": "application/json"},
            )
        elif case == "oversize":
            response = await client.post(
                BASE + "/register",
                content=b" " * 4097,
                headers={"Content-Type": "application/json"},
            )
        elif case == "encoding":
            response = await client.post(
                BASE + "/register",
                json=registration(env).model_dump(mode="json"),
                headers={"Content-Encoding": "gzip"},
            )
        else:
            response = await client.post(
                BASE + "/register?grant=true", json=registration(env).model_dump(mode="json")
            )
    assert response.status_code == status
    assert env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 0


async def test_body_await_cannot_outlive_original_service_credential(env, wire):
    auth, transport = wire
    request = registration(env)
    entered, release = asyncio.Event(), asyncio.Event()

    async def body():
        raw = request.model_dump_json().encode()
        yield raw[:10]
        entered.set()
        await release.wait()
        yield raw[10:]

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + auth.tokens["converse"]},
    ) as client:
        task = asyncio.create_task(
            client.post(
                BASE + "/register", content=body(), headers={"Content-Type": "application/json"}
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            auth.tokens["converse"] = "converse-rotated-public-token-000000000"
            release.set()
            assert (await asyncio.wait_for(task, 3)).status_code == 401
            assert env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 0
        finally:
            release.set()


async def test_closed_original_controller_is_explicitly_unavailable(env, wire):
    auth, transport = wire
    env.store.live_captures.close()
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + auth.tokens["decisions"]},
    ) as client:
        response = await client.get(BASE + "/backend")
    assert response.status_code == 503 and "backend_id" not in response.json()
