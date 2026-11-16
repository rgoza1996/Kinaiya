"""Real Session HTTP/client publication, original ownership and role parity."""

import asyncio

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_windows import PROTOCOL, LiveWindowScope
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import LocalJournal
from tests.integration.test_live_window_publication import env as env
from tests.integration.test_live_window_publication import original

BASE = "/v2/transcript/live-windows"


@pytest.fixture
def wire(env):
    auth = Auth(
        {
            role: role + "-public-live-token-000000000"
            for role in ("owner", "converse", "decisions", "agent", "resources")
        }
    )
    return auth, httpx.ASGITransport(app=create_app(env.store, auth))


def scope(env):
    return LiveWindowScope(
        schema_version="2.0",
        protocol=PROTOCOL,
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        utterance_id="public-live-utterance",
    )


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", "none"])
async def test_role_parity_and_legacy_window_shape(env, wire, role):
    auth, transport = wire
    headers = {} if role == "none" else {"Authorization": "Bearer " + auth.tokens[role]}
    request = original(env)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://session.local", headers=headers
    ) as client:
        published = await client.post(BASE, json=request.model_dump(mode="json"))
        read = await client.post(BASE + "/latest", json=scope(env).model_dump(mode="json"))
        looked = await client.post(
            BASE + "/lookup", json=request.reference().model_dump(mode="json")
        )
    status = 401 if role == "none" else 403
    assert published.status_code == looked.status_code == (200 if role == "converse" else status)
    assert read.status_code == (200 if role in {"converse", "decisions"} else status)
    local = LocalJournal(env.store, role)
    if role == "converse":
        receipt = await local.lookup_live_window(request.reference())
        assert receipt.receipt.source.payload == request.request.payload
        assert published.headers["cache-control"] == "no-store"
        assert await local.publish_live_window(request) == receipt.receipt
        assert await local.latest_live_window(scope(env)) is not None
    else:
        with pytest.raises(PermissionError):
            await local.publish_live_window(request)
        with pytest.raises(PermissionError):
            await local.lookup_live_window(request.reference())
    if role not in {"converse", "decisions"}:
        with pytest.raises(PermissionError):
            await local.latest_live_window(scope(env))


async def test_real_client_roundtrip_and_empty_original_lookup(env, wire):
    auth, transport = wire
    client = JournalClient("http://session.local", auth.tokens["converse"], transport=transport)
    request = original(env)
    try:
        assert (await client.lookup_live_window(request.reference())).receipt is None
        assert await client.latest_live_window(scope(env)) is None
        receipt = await client.publish_live_window(request)
        assert (await client.lookup_live_window(request.reference())).receipt == receipt
        current = await client.latest_live_window(scope(env))
        assert current.eligible == current.latest == receipt
        assert not client._submit_exchanges
    finally:
        await client.close()


@pytest.mark.parametrize(
    "change",
    [
        {"epoch": True},
        {"owner_instance": "unknown"},
        {"schema_version": "1.0"},
        {"authority": True},
    ],
)
async def test_malformed_native_request_has_no_write(env, wire, change):
    auth, transport = wire
    data = original(env).model_dump(mode="json")
    if "epoch" in change:
        data["request"] = data["request"] | change
    else:
        data |= change
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + auth.tokens["converse"]},
    ) as client:
        response = await client.post(BASE, json=data)
    assert response.status_code == 422
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


@pytest.mark.parametrize(
    "case,status", [("duplicate", 422), ("oversize", 413), ("encoding", 415), ("query", 400)]
)
async def test_wire_envelope_is_bounded_and_closed(env, wire, case, status):
    auth, transport = wire
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + auth.tokens["converse"]},
    ) as client:
        if case == "duplicate":
            response = await client.post(
                BASE, content=b'{"epoch":1,"epoch":2}', headers={"Content-Type": "application/json"}
            )
        elif case == "oversize":
            response = await client.post(
                BASE, content=b" " * (68 * 1024 + 1), headers={"Content-Type": "application/json"}
            )
        elif case == "encoding":
            response = await client.post(
                BASE,
                json=original(env).model_dump(mode="json"),
                headers={"Content-Encoding": "gzip"},
            )
        else:
            response = await client.post(
                BASE + "?grant=true", json=original(env).model_dump(mode="json")
            )
    assert response.status_code == status
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0


async def test_client_cancellation_retains_original_response_read_and_sole_close(env):
    from services.session.live_windows import publish

    receipt = publish(env.store, original(env))
    entered, release = asyncio.Event(), asyncio.Event()

    class Held(httpx.AsyncByteStream):
        closes = 0

        async def __aiter__(self):
            entered.set()
            await release.wait()
            yield receipt.model_dump_json().encode()

        async def aclose(self):
            self.closes += 1

    stream = Held()
    client = JournalClient(
        "http://session.local",
        "public-live-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
    )
    task = asyncio.create_task(client.publish_live_window(original(env)))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(client._submit_exchanges) == 1 and stream.closes == 0
        owners = [exchange.task for exchange in client._submit_exchanges]
        release.set()
        await asyncio.wait_for(asyncio.gather(*owners), 3)
        assert not client._submit_exchanges and stream.closes == 1
    finally:
        release.set()
        await client.close()


async def test_client_retains_frozen_original_when_callers_mutate_nested_v1_request(env):
    from services.session.live_windows import publish

    request = original(env)
    receipt = publish(env.store, request)
    entered, release = asyncio.Event(), asyncio.Event()

    class Held(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await release.wait()
            yield receipt.model_dump_json().encode()

        async def aclose(self):
            pass

    client = JournalClient(
        "http://session.local",
        "public-live-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Held())),
    )
    task = asyncio.create_task(client.publish_live_window(request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        request.request.payload["text"] = "Public mutated caller value"
        release.set()
        assert await asyncio.wait_for(task, 3) == receipt
    finally:
        release.set()
        await client.close()


@pytest.mark.parametrize("case", ["malformed", "legacy-append", "legacy-decision"])
async def test_legacy_http_boundary_reports_native_refusal_without_server_error(env, wire, case):
    from packages.contracts.models import WindowDecision
    from services.session.live_windows import publish

    auth, _ = wire
    transport = httpx.ASGITransport(app=create_app(env.store, auth), raise_app_exceptions=False)
    request = original(env)
    if case == "malformed":
        data = request.request.model_dump(mode="json")
        data["payload"]["window_index"] = True
        role = "converse"
    else:
        source = publish(env.store, request).source
        if case == "legacy-append":
            data = original(env, 1).request.model_dump(mode="json")
            role = "converse"
        else:
            data = request.request.model_dump(mode="json") | {
                "kind": "routing.window_decided",
                "payload": WindowDecision(
                    source_event_id=source.event_id,
                    source_sequence=source.sequence,
                    source_revision=1,
                    conversation="IGNORE",
                    agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
                ).model_dump(mode="json"),
                "causes": [source.event_id],
                "idempotency_key": "public-legacy-wire-attempt",
            }
            role = "decisions"
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + auth.tokens[role]},
    ) as client:
        response = await client.post("/v1/transcript/entries", json=data)
    assert response.status_code == 409
    assert response.json()["detail"]
