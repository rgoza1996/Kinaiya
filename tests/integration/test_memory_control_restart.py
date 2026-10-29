"""Native owner intent must not cross a runtime restart through a reused counter."""

from contextlib import asynccontextmanager

import httpx
import pytest

from packages.contracts.memory import MemoryBinding
from packages.contracts.models import SessionRequest
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store

TOKEN = "synthetic-owner-restart-credential-000000"


@asynccontextmanager
async def instance(path):
    store = Store(path)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider([]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, Auth({"owner": TOKEN}))),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        try:
            yield store, runtime, client
        finally:
            await runtime.close()
            store.close()


def command(binding, state, enabled):
    return {
        **binding,
        "expected_runtime_id": state["runtime_id"],
        "expected_generation": state["use_generation"],
        "enabled": enabled,
        **({"implementation_id": "synthetic-v1", "selector_ids": ["note"]} if enabled else {}),
    }


@pytest.mark.parametrize("stale_enabled", [True, False])
async def test_old_enable_and_off_cannot_change_fresh_runtime(tmp_path, stale_enabled):
    path = tmp_path / "journal.sqlite"
    async with instance(path) as (store, _, client):
        session = store.create_session(SessionRequest(channel="converse"))
        binding = {key: getattr(session, key) for key in ("session_id", "thread_id", "epoch")}
        old = (await client.post("/v1/memory/state", json=binding)).json()
        if not stale_enabled:
            accepted = await client.post("/v1/memory/use", json=command(binding, old, True))
            assert accepted.status_code == 200
            old = accepted.json()
        delayed = command(binding, old, stale_enabled)
    async with instance(path) as (_, runtime, client):
        fresh = (await client.post("/v1/memory/state", json=binding)).json()
        assert not fresh["enabled"] and fresh["runtime_id"] != old["runtime_id"]
        if not stale_enabled:
            accepted = await client.post("/v1/memory/use", json=command(binding, fresh, True))
            assert accepted.status_code == 200
            fresh = accepted.json()
        assert fresh["use_generation"] == old["use_generation"]

        # Even the Session read must follow the identity gate, so stale off
        # cannot cancel/fence a new selection before the request is refused.
        async def forbidden(*_):
            pytest.fail("Old-runtime control awaited the journal")

        runtime.journal.session = forbidden
        response = await client.post("/v1/memory/use", json=delayed)
        assert response.status_code == 409
        assert runtime.memory.state(MemoryBinding(**binding)).model_dump(mode="json") == fresh
        assert runtime.provider.calls == []


async def test_control_requires_runtime_identity(tmp_path):
    async with instance(tmp_path / "journal.sqlite") as (store, runtime, client):
        session = store.create_session(SessionRequest(channel="converse"))
        binding = {key: getattr(session, key) for key in ("session_id", "thread_id", "epoch")}
        response = await client.post(
            "/v1/memory/use", json={**binding, "expected_generation": 0, "enabled": False}
        )
        assert response.status_code == 422 and runtime.provider.calls == []
