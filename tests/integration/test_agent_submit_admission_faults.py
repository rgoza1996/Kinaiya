"""Additional #199 fault boundaries; the original 20/40 baselines stay frozen."""

import asyncio
import hashlib
import importlib
import sqlite3
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from test_agent_submit_admission import (
    ADMIT,
    AUTH,
    LOOKUP,
    TASK_B,
    api,
    encoded,
    entry_rows,
    headers,
    legacy_database,
    lookup,
    proposal,
)
from test_agent_submit_uncertainty import (
    ACTIVE,
    checkpoint,
    outcome,
    request,
    settled,
    start,
    submits,
)
from test_agent_submit_uncertainty import (
    env as env,
)

from core.policy import ToolPolicy
from packages.contracts.models import AppendRequest, TaskState
from services.agent.app import create_app as agent_app
from services.agent.runtime import AgentRuntime
from services.session.store import LocalJournal, Store


def seed_confirmed(store, body):
    # Until the new API lands, use the genuine old native append to seed its
    # accepted row. This is never a model-provided or fabricated ACK.
    admit = getattr(store, "admit_submit", None)
    if admit is not None:
        return admit(body, "agent").entry
    return store.append(body, "agent")


def matching_proposal(env, body, *, task_id=TASK_B):
    return proposal(
        env.session,
        task_id=task_id,
        label="retained",
        key=body.idempotency_key,
        objective=body.objective,
        source_event_ids=list(body.source_event_ids),
        source_revision=body.source_revision,
        content_scope=body.content_scope,
    )


def local_source(env):
    return env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.user",
            payload={
                "text": request(env).objective,
                "final": True,
                "revision": 1,
                "content_scope": "local_only",
            },
            idempotency_key="local-disclosure-source",
        ),
        "owner",
    )


async def test_native_retained_winner_is_resolved_before_current_provider_selection(
    env, monkeypatch
):
    body = request(env)
    original = matching_proposal(env, body)
    winner = seed_confirmed(env.store, original)
    before = entry_rows(env.store)
    settings_before = env.runtime.model_settings.db.total_changes
    traps = []
    for owner, name, asynchronous in (
        (env.runtime, "session_harness", False),
        (env.runtime.model_settings, "selection", False),
        (env.runtime.model_settings, "resolve", False),
        (env.runtime, "_complete", True),
        (env.runtime, "_run", True),
        (env.journal, "agent_export_admission", True),
        (env.provider, "prepare_complete", True),
        (env.provider, "complete", True),
    ):
        trap = (AsyncMock if asynchronous else Mock)(
            side_effect=AssertionError("A retained receipt selected or used a current provider")
        )
        monkeypatch.setattr(owner, name, trap)
        traps.append(trap)
    retained = await env.runtime.submit(body)
    assert retained.task_id == winner.payload["task_id"]
    assert retained.state == TaskState.PAUSED
    assert retained.harness_version == winner.payload["harness_version"]
    assert retained.model_pin.model_dump(mode="json") == winner.payload["model_pin"]
    assert entry_rows(env.store) == before
    assert env.runtime.model_settings.db.total_changes == settings_before
    assert env.journal.attempts == []
    assert env.worker_calls == []
    assert all(trap.call_count == 0 for trap in traps)


@pytest.mark.parametrize("retired_by", ["close", "cancel", "malformed_ack"])
async def test_final_status_cannot_cache_active_after_submit_retirement(env, retired_by):
    if retired_by == "malformed_ack":
        env.journal.bad_ack = "objective"
        await outcome(env.runtime.submit(request(env)))
    else:
        env.journal.hold_after = True
        env.journal.after.defer_cancellation = retired_by == "cancel"
        operation = start(env, request(env))
        await asyncio.wait_for(env.journal.after.entered.wait(), timeout=2)
        if retired_by == "close":
            await asyncio.wait_for(env.runtime.close(), timeout=2)
        else:
            operation.cancel()
            await checkpoint()
        env.journal.after.release.set()
        await settled(operation)
        await asyncio.wait_for(env.journal.finished.wait(), timeout=2)
    await checkpoint()
    assert len(submits(env)) == 1
    statuses = await env.runtime.status()
    assert all(task.state not in ACTIVE for task in statuses)
    for task in statuses:
        assert (await env.runtime.status(task.task_id)).state not in ACTIVE


async def test_agent_http_unconfirmed_is_fixed_503_and_never_201(env):
    env.journal.failure = "before_commit"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent_app(env.runtime, AUTH)),
        base_url="http://agent.test",
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/v1/agent/tasks",
                json=request(env).model_dump(mode="json"),
                headers=headers("owner"),
            )
            assert response.status_code == 503
            assert response.json() == {"detail": "Agent submission persistence is unconfirmed"}
        statuses = await client.get("/v1/agent/tasks", headers=headers("owner"))
        assert statuses.status_code == 200
        assert all(
            task["state"] not in {state.value for state in ACTIVE} for task in statuses.json()
        )
    assert submits(env) == []
    assert env.worker_calls == []


async def test_agent_http_native_occupied_epoch_is_fixed_409(env):
    old = request(env, sources=False)
    seed_confirmed(env.store, matching_proposal(env, old))
    current = env.store.reset(env.session.session_id)
    body = old.model_copy(update={"epoch": current.epoch})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent_app(env.runtime, AUTH)),
        base_url="http://agent.test",
    ) as client:
        response = await client.post(
            "/v1/agent/tasks", json=body.model_dump(mode="json"), headers=headers("owner")
        )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Agent submission key conflicts with retained scope or content"
    }
    assert len(submits(env)) == 1
    assert env.worker_calls == []


async def test_agent_http_ambiguous_legacy_is_fixed_503(env):
    body = request(env, sources=False)
    one = matching_proposal(env, body)
    two = matching_proposal(env, body, task_id="00000000-0000-4000-8000-000000000003")
    path = env.runtime.workspace.parent / "ambiguous.sqlite"
    legacy_database(path, env.session, [one, two])
    store = Store(path)
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        env.provider,
        env.runtime.workspace,
        ToolPolicy(env.runtime.workspace),
    )
    runtime._run = AsyncMock()
    try:
        before = entry_rows(store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent_app(runtime, AUTH)),
            base_url="http://agent.test",
        ) as client:
            response = await client.post(
                "/v1/agent/tasks", json=body.model_dump(mode="json"), headers=headers("owner")
            )
        assert response.status_code == 503
        assert response.json() == {"detail": "Agent submission admission is unavailable"}
        assert entry_rows(store) == before
        runtime._run.assert_not_called()
    finally:
        await asyncio.wait_for(runtime.close(), timeout=3)
        store.close()


class FailAfterEntryInsert:
    """Raise after SQLite has really inserted an entry inside its transaction."""

    def __init__(self, connection):
        self.connection = connection
        self.fired = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, sql, *args):
        result = self.connection.execute(sql, *args)
        compact = " ".join(sql.lower().split())
        if compact.startswith("insert into entries("):
            self.fired = True
            raise sqlite3.OperationalError("Synthetic failure after journal entry insertion")
        return result


def test_sqlite_failure_between_entry_and_slot_rolls_back_both(env):
    admit = getattr(env.store, "admit_submit", None)
    assert callable(admit), "Native submit API is absent (capability baseline)"
    before = entry_rows(env.store)
    connection = env.store.db
    proxy = FailAfterEntryInsert(connection)
    env.store.db = proxy
    body = proposal(env.session)
    try:
        with pytest.raises((OSError, ValueError, sqlite3.Error)):
            admit(body, "agent")
    finally:
        env.store.db = connection
    assert proxy.fired, "The fault must occur after an actual SQLite entry insert"
    assert entry_rows(env.store) == before
    contract = importlib.import_module("packages.contracts.agent_submit")
    query = contract.AgentSubmitLookupRequest(**lookup(env.session))
    assert env.store.lookup_submit(query, "agent").status == "unobserved"
    receipt = env.store.admit_submit(body, "agent")
    assert receipt.disposition == "created"
    assert len(submits(env)) == 1


@pytest.mark.parametrize("through", ["runtime", "native"])
async def test_requested_public_digest_preserves_effective_local_source_floor(env, through):
    source = local_source(env)
    body = request(env).model_copy(
        update={"content_scope": "public_synthetic", "source_event_ids": [source.event_id]}
    )
    if through == "runtime":
        await env.runtime.submit(body)
        native = submits(env)[0]
    else:
        proposed = matching_proposal(env, body)
        proposed = proposed.model_copy(
            update={"payload": {**proposed.payload, "content_scope": "local_only"}}, deep=True
        )
        async with api(env.store) as client:
            response = await client.post(
                ADMIT, json=proposed.model_dump(mode="json"), headers=headers("agent")
            )
        assert response.status_code == 200
        native = submits(env)[0]
    logical = body.model_dump(exclude={"schema_version", "idempotency_key"})
    expected = hashlib.sha256(encoded(logical)).hexdigest()
    narrowed = hashlib.sha256(encoded({**logical, "content_scope": "local_only"})).hexdigest()
    assert native.payload["requested_content_scope"] == "public_synthetic"
    assert native.payload["content_scope"] == "local_only"
    assert native.payload["payload_digest"] == expected != narrowed


@pytest.mark.parametrize("writer", ["owner", "decisions"])
async def test_supervisor_json_cannot_override_non_agent_sql_writer(env, writer):
    path = env.runtime.workspace.parent / "forged-writer.sqlite"
    legacy_database(path, env.session, [proposal(env.session)], writer=writer)
    store = Store(path)
    try:
        before = entry_rows(store)
        assert store.read()[0].origin == "agent-supervisor"
        async with api(store) as client:
            response = await client.post(LOOKUP, json=lookup(env.session), headers=headers("agent"))
        assert response.status_code == 503
        assert entry_rows(store) == before
    finally:
        store.close()


@pytest.mark.parametrize("route", [ADMIT, LOOKUP], ids=["admit", "lookup"])
@pytest.mark.parametrize("encoding", ["invalid_utf8", "lone_surrogate"])
async def test_invalid_unicode_in_known_string_is_rejected_before_mutation(env, route, encoding):
    if route == ADMIT:
        value = proposal(env.session, objective="known-string-marker").model_dump(mode="json")
    else:
        value = lookup(env.session, key="known-string-marker")
    raw = encoded(value)
    replacement = b"bad\xffutf8" if encoding == "invalid_utf8" else b"\\ud800"
    raw = raw.replace(b"known-string-marker", replacement)
    assert b"known-string-marker" not in raw
    async with api(env.store) as client:
        response = await client.post(
            route, content=raw, headers={**headers("agent"), "Content-Type": "application/json"}
        )
    assert response.status_code == 422
    assert submits(env) == []
