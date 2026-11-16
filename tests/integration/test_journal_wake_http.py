"""Actual authenticated ASGI/client boundaries and original metadata exchange ownership."""

import asyncio

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.journal_wake import JournalWakeCursor, JournalWakeWait
from packages.contracts.models import SessionRequest
from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import LocalJournal, Store

BASE = "/v1/transcript/wake"


@pytest.fixture
def env(tmp_path):
    from types import SimpleNamespace

    store = Store(tmp_path / "wake-http.sqlite")
    auth = Auth(
        {
            role: role + "-public-wake-token-000000000"
            for role in ("owner", "converse", "decisions", "agent", "resources")
        }
    )
    app = create_app(store, auth)
    yield SimpleNamespace(store=store, auth=auth, transport=httpx.ASGITransport(app=app))
    if not store.journal_wake.closed:
        store.close()


def request_body(env, **change):
    return (
        JournalWakeWait(
            schema_version="1.0",
            protocol="journal_wake_v1",
            cursor=env.store.journal_wake.snapshot(),
            wait_ms=0,
        ).model_dump(mode="json")
        | change
    )


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "agent", "resources", "none"])
async def test_authentication_and_local_remote_role_parity(env, role):
    headers = {} if role == "none" else {"Authorization": "Bearer " + env.auth.tokens[role]}
    async with httpx.AsyncClient(
        transport=env.transport, base_url="http://session.local", headers=headers
    ) as client:
        response = await client.get(BASE)
        waited = await client.post(BASE + "/wait", json=request_body(env))
        if role in {"owner", "converse", "decisions"}:
            assert response.status_code == waited.status_code == 200
            assert (
                response.headers["cache-control"] == waited.headers["cache-control"] == "no-store"
            )
            cursor = JournalWakeCursor.model_validate(response.json())
            local = LocalJournal(env.store, role)
            assert await local.journal_wake_cursor() == cursor
            assert (await local.wait_for_journal_change(cursor, wait_ms=0)).changed is False
            assert waited.json()["changed"] is False
        else:
            assert response.status_code == waited.status_code == (401 if role == "none" else 403)
            with pytest.raises(PermissionError):
                await LocalJournal(env.store, role).journal_wake_cursor()
    assert not env.store.journal_wake.waiters


@pytest.mark.parametrize(
    "change",
    [
        {"wait_ms": True},
        {"wait_ms": 1000.0},
        {"wait_ms": 1001},
        {"wait_ms": -1},
        {"schema_version": "2.0"},
        {"publication_committed": True},
    ],
)
async def test_malformed_wait_is_refused_before_registration(env, change):
    async with httpx.AsyncClient(
        transport=env.transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + env.auth.tokens["decisions"]},
    ) as client:
        response = await client.post(BASE + "/wait", json=request_body(env, **change))
        assert response.status_code == 422
    assert not env.store.journal_wake.waiters


@pytest.mark.parametrize(
    "kind,status", [("duplicate", 422), ("oversize", 413), ("encoding", 415), ("query", 400)]
)
async def test_wire_envelope_and_byte_bounds(env, kind, status):
    async with httpx.AsyncClient(
        transport=env.transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + env.auth.tokens["decisions"]},
    ) as client:
        if kind == "duplicate":
            response = await client.post(
                BASE + "/wait",
                content=b'{"wait_ms":0,"wait_ms":1}',
                headers={"Content-Type": "application/json"},
            )
        elif kind == "oversize":
            response = await client.post(
                BASE + "/wait", content=b" " * 4097, headers={"Content-Type": "application/json"}
            )
        elif kind == "encoding":
            response = await client.post(
                BASE + "/wait", json=request_body(env), headers={"Content-Encoding": "gzip"}
            )
        else:
            response = await client.post(BASE + "/wait?grant=true", json=request_body(env))
        assert response.status_code == status
    assert not env.store.journal_wake.waiters


async def test_real_client_cancel_does_not_erase_its_original_http_wait(env, monkeypatch):
    client = JournalClient(
        "http://session.local", env.auth.tokens["decisions"], transport=env.transport
    )
    original_wait, entered = env.store.journal_wake.wait, asyncio.Event()

    async def observe(request):
        entered.set()
        return await original_wait(request)

    monkeypatch.setattr(env.store.journal_wake, "wait", observe)
    try:
        cursor = await client.journal_wake_cursor()
        task = asyncio.create_task(client.wait_for_journal_change(cursor))
        await asyncio.wait_for(entered.wait(), 3)
        assert len(env.store.journal_wake.waiters) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(env.store.journal_wake.waiters) == 1 and len(client._submit_exchanges) == 1
        env.store.create_session(SessionRequest(channel="converse"))
        owned = [exchange.task for exchange in client._submit_exchanges]
        await asyncio.wait_for(asyncio.gather(*owned), 3)
        assert not env.store.journal_wake.waiters and not client._submit_exchanges
        current = await client.journal_wake_cursor()
        assert current.revision > cursor.revision
        assert (await client.wait_for_journal_change(current, wait_ms=0)).changed is False
    finally:
        await client.close()


async def test_credential_rotation_refuses_delayed_metadata_result(env, monkeypatch):
    original_wait, entered = env.store.journal_wake.wait, asyncio.Event()

    async def observe(request):
        entered.set()
        return await original_wait(request)

    monkeypatch.setattr(env.store.journal_wake, "wait", observe)
    async with httpx.AsyncClient(
        transport=env.transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + env.auth.tokens["decisions"]},
    ) as client:
        task = asyncio.create_task(
            client.post(BASE + "/wait", json=request_body(env, wait_ms=1000))
        )
        await asyncio.wait_for(entered.wait(), 3)
        env.auth.tokens["decisions"] = "decisions-rotated-public-token-0000000"
        env.store.create_session(SessionRequest(channel="converse"))
        response = await asyncio.wait_for(task, 3)
        assert response.status_code == 401 and "cursor" not in response.json()
    assert not env.store.journal_wake.waiters
