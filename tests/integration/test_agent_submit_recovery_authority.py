"""Recovered RAM keys cannot bypass unique native submit provenance (#199)."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from test_agent_submit_admission import (
    AUTH,
    LOOKUP,
    TASK_B,
    api,
    entry_rows,
    headers,
    legacy_database,
    lookup,
    proposal,
)
from test_agent_submit_uncertainty import env as env
from test_agent_submit_uncertainty import request

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.models import TaskState
from packages.providers.text import ScriptedProvider
from services.agent.app import create_app
from services.agent.model_settings import AgentModelSettings
from services.agent.runtime import AgentRuntime
from services.session.store import LocalJournal, Store


class IdentityTrap:
    @property
    def info(self):
        raise AssertionError("Receipt recovery read a replacement provider identity")


def receipt_traps(runtime, monkeypatch):
    traps = []
    for owner, name, asynchronous in (
        (runtime, "session_harness", False),
        (runtime.model_settings, "selection", False),
        (runtime.model_settings, "resolve", False),
        (runtime, "_run", True),
        (runtime, "_complete", True),
    ):
        trap = (AsyncMock if asynchronous else Mock)(
            side_effect=AssertionError("Recovered receipt attempted new admission or execution")
        )
        monkeypatch.setattr(owner, name, trap)
        traps.append(trap)
    runtime.provider = IdentityTrap()
    return traps


@pytest.mark.parametrize("invalid", ["duplicates", "cross_epoch_duplicates", "owner", "decisions"])
async def test_recover_cannot_make_ambiguous_or_forged_keys_a_successful_ram_receipt(
    env, monkeypatch, invalid
):
    old_session = env.session
    current = (
        old_session.model_copy(update={"epoch": 2})
        if invalid == "cross_epoch_duplicates"
        else old_session
    )
    body = request(env, sources=False).model_copy(update={"epoch": current.epoch})
    first = proposal(old_session, key=body.idempotency_key, objective=body.objective)
    writer = "agent"
    bodies = [first]
    if "duplicates" in invalid:
        bodies.append(
            proposal(
                current,
                task_id=TASK_B,
                label="b",
                key=body.idempotency_key,
                objective=body.objective,
            )
        )
    else:
        writer = invalid
    path = env.runtime.workspace.parent / "recovered-invalid.sqlite"
    legacy_database(path, current, bodies, writer=writer)
    store = Store(path)
    provider = ScriptedProvider([])
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        env.runtime.workspace,
        ToolPolicy(env.runtime.workspace),
    )
    runtime.model_settings = AgentModelSettings(provider, {}, path=path.with_suffix(".models"))
    try:
        before = entry_rows(store)
        try:
            await runtime.recover()
        except (ValueError, OSError, PolicyDenied):
            # Refusal is legitimate. A partially populated cache must still
            # never become an authoritative unique accepted receipt.
            pass
        app = create_app(runtime, AUTH)
        traps = receipt_traps(runtime, monkeypatch)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
        ) as client:
            reply = await client.post(
                "/v1/agent/tasks", json=body.model_dump(mode="json"), headers=headers("owner")
            )
        assert reply.status_code == 503
        assert reply.json() == {"detail": "Agent submission admission is unavailable"}
        assert all(trap.call_count == 0 for trap in traps)
        assert provider.calls == []
        assert entry_rows(store) == before
        async with api(store) as client:
            native = await client.post(
                LOOKUP, json=lookup(current, key=body.idempotency_key), headers=headers("agent")
            )
            assert native.status_code == 503
            read = await client.get("/v1/transcript", headers=headers("agent"))
            assert read.status_code == 200 and len(read.json()) == len(bodies)
    finally:
        await asyncio.wait_for(runtime.close(), timeout=3)
        store.close()


async def test_unique_canonical_legacy_recovery_still_returns_original_receipt(env, monkeypatch):
    body = request(env, sources=False)
    accepted = proposal(env.session, key=body.idempotency_key, objective=body.objective)
    path = env.runtime.workspace.parent / "recovered-valid.sqlite"
    legacy_database(path, env.session, [accepted])
    store = Store(path)
    provider = ScriptedProvider([])
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        env.runtime.workspace,
        ToolPolicy(env.runtime.workspace),
    )
    runtime.model_settings = AgentModelSettings(provider, {}, path=path.with_suffix(".models"))
    try:
        await runtime.recover()
        before = entry_rows(store)
        traps = receipt_traps(runtime, monkeypatch)
        receipt = await runtime.submit(body)
        assert receipt.task_id == accepted.payload["task_id"]
        assert receipt.state == TaskState.PAUSED
        assert receipt.model_pin.model_dump(mode="json") == accepted.payload["model_pin"]
        assert receipt.harness_version == accepted.payload["harness_version"]
        assert entry_rows(store) == before
        assert all(trap.call_count == 0 for trap in traps)
    finally:
        await asyncio.wait_for(runtime.close(), timeout=3)
        store.close()


async def test_existing_confirmed_live_receipt_keeps_its_worker(env):
    body = request(env)
    original = await env.runtime.submit(body)
    await asyncio.wait_for(env.worker_started.wait(), timeout=2)
    worker = env.runtime._get(original.task_id).worker
    before = entry_rows(env.store)
    replay = await env.runtime.submit(body)
    assert replay == original
    assert env.runtime._get(original.task_id).worker is worker
    assert worker is not None and not worker.done()
    assert env.worker_calls == [original.task_id]
    assert entry_rows(env.store) == before
