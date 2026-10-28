"""Durable native generation recovery, with synthetic local/authenticated HTTP data."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import (
    MAX_PLAYBACK_GENERATION,
    AppendRequest,
    PlaybackGeneration,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.runtime import ConverseRuntime, PlaybackEpoch, PlaybackStart, PlaybackStop
from services.converse.windows import WindowConverseController
from services.session.api import create_app
from services.session.store import JournalError, LocalJournal, Store


def native_entry(store, session, kind="converse.safety", *, role="converse", **payload):
    if kind in {"conversation.user", "conversation.converse"}:
        payload = {"text": "PUBLIC_SYNTHETIC_BODY_NOT_RETURNED", "final": True, **payload}
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind=kind,
            payload=payload,
            idempotency_key=f"metadata:{store.db.total_changes}",
        ),
        role,
    )


@pytest.fixture(params=["local", "http"])
async def env(tmp_path, request):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    auth = Auth({"owner": "o" * 32, "converse": "c" * 32, "agent": "a" * 32})
    journal = (
        LocalJournal(store, "converse")
        if request.param == "local"
        else JournalClient(
            "http://synthetic-asgi",
            auth.tokens["converse"],
            transport=httpx.ASGITransport(app=create_app(store, auth)),
        )
    )
    state = SimpleNamespace(store=store, session=session, journal=journal, auth=auth)
    try:
        yield state
    finally:
        if isinstance(journal, JournalClient):
            await journal.close()
        store.close()


async def maximum(env, session=None):
    session = env.session if session is None else session
    return await env.journal.playback_generation(
        session.session_id, session.thread_id, session.epoch
    )


@pytest.mark.parametrize(
    "kind,field",
    [
        ("conversation.user", "generation_epoch"),
        ("conversation.converse", "generation_epoch"),
        ("converse.safety", "generation_epoch"),
        ("converse.window_state", "generation_epoch"),
        ("delivery.updated", "generation_epoch"),
        ("delivery.updated", "next_generation_epoch"),
    ],
)
async def test_every_native_generation_field_is_recovered_without_new_runtime_tags(
    env, kind, field
):
    assert (await maximum(env)).maximum_generation == 0
    event = native_entry(env.store, env.session, kind, **{field: 17})
    # Older rows need neither runtime_id nor a newly introduced control tag.
    assert "runtime_id" not in event.payload and "control" not in event.payload
    recovered = await maximum(env)
    assert recovered == PlaybackGeneration(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        maximum_generation=17,
    )
    assert set(recovered.model_dump()) == {
        "schema_version",
        "session_id",
        "thread_id",
        "epoch",
        "maximum_generation",
    }
    assert "PUBLIC_SYNTHETIC_BODY" not in recovered.model_dump_json()


async def test_maximum_covers_both_delivery_fields_and_ignores_unallocated_nulls(env):
    native_entry(env.store, env.session, generation_epoch=None)
    native_entry(
        env.store, env.session, "converse.window_state", phase="error", generation_epoch=None
    )
    native_entry(
        env.store, env.session, "delivery.updated", generation_epoch=31, next_generation_epoch=12
    )
    native_entry(
        env.store, env.session, "delivery.updated", generation_epoch=2, next_generation_epoch=40
    )
    native_entry(env.store, env.session, generation_epoch=7)
    assert (await maximum(env)).maximum_generation == 40


@pytest.mark.parametrize("field", ["generation_epoch", "next_generation_epoch"])
@pytest.mark.parametrize(
    "counter", [True, False, 1.0, "90", -1, MAX_PLAYBACK_GENERATION + 1, 2**80, [], {}]
)
async def test_malformed_trusted_counter_fails_closed_instead_of_lowering_maximum(
    env, field, counter
):
    native_entry(env.store, env.session, generation_epoch=7)
    native_entry(env.store, env.session, "delivery.updated", **{field: counter})
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await maximum(env)
    provider = ScriptedProvider([])
    runtime = ConverseRuntime(env.journal, provider)
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await runtime._epoch(env.session.session_id)
    assert not runtime.playback and not runtime._playback_start and not provider.calls


async def test_unrelated_writer_kind_session_and_epoch_cannot_influence_or_poison(env):
    old = env.session
    native_entry(env.store, old, generation_epoch=MAX_PLAYBACK_GENERATION + 1)
    env.session = env.store.reset(old.session_id)
    assert (await maximum(env)).maximum_generation == 0
    foreign = env.store.create_session(SessionRequest(channel="converse"))
    native_entry(env.store, foreign, generation_epoch=MAX_PLAYBACK_GENERATION + 1)
    native_entry(env.store, env.session, "conversation.user", role="owner", generation_epoch=True)
    poison = native_entry(
        env.store, env.session, "delivery.updated", role="owner", generation_epoch="poison"
    )
    target = native_entry(
        env.store, env.session, "conversation.user", generation_epoch=5, revision=1
    )
    unrelated = env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.revision",
            causes=[target.event_id],
            payload={
                "target_event_id": target.event_id,
                "text": "Public revision",
                "revision": 2,
                "generation_epoch": MAX_PLAYBACK_GENERATION + 1,
            },
            idempotency_key="unrelated-kind",
        ),
        "converse",
    )
    # Even malformed JSON outside native writer/kind scope must not be inspected.
    with env.store.db:
        env.store.db.execute(
            "UPDATE entries SET value='not-json' WHERE event_id IN (?,?)",
            (poison.event_id, unrelated.event_id),
        )
    assert (await maximum(env)).maximum_generation == 5
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await maximum(env, old)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "foreign"),
        ("thread_id", "foreign"),
        ("epoch", True),
        ("origin", "authenticated-user"),
        ("kind", "conversation.window"),
        ("sequence", 999),
    ],
)
async def test_native_metadata_identity_corruption_is_not_trusted(env, field, value):
    entry = native_entry(env.store, env.session, generation_epoch=50)
    body = entry.model_dump(mode="json")
    body[field] = value
    with env.store.db:
        env.store.db.execute(
            "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(body), entry.event_id)
        )
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await maximum(env)


async def test_native_read_failure_does_not_fall_back_to_zero(env):
    native_entry(env.store, env.session, generation_epoch=9)
    env.store.db.set_authorizer(lambda action, table, *_: 1 if table == "entries" else 0)
    try:
        with pytest.raises((JournalError, httpx.HTTPStatusError)):
            await maximum(env)
        runtime = ConverseRuntime(env.journal, ScriptedProvider([]))
        with pytest.raises((JournalError, httpx.HTTPStatusError)):
            await runtime._epoch(env.session.session_id)
        assert not runtime.playback and not runtime._playback_start
    finally:
        env.store.db.set_authorizer(None)


async def test_reset_during_native_query_revalidates_before_return(env, monkeypatch):
    native_entry(env.store, env.session, generation_epoch=9)
    original, calls = env.store.session, 0

    def reset_before_second_read(session_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            env.store.reset(session_id)
        return original(session_id)

    monkeypatch.setattr(env.store, "session", reset_before_second_read)
    with pytest.raises((JournalError, httpx.HTTPStatusError)):
        await maximum(env)
    assert original(env.session.session_id).epoch == env.session.epoch + 1


async def test_reset_after_query_response_prevents_old_scope_restart_barrier(env):
    native_entry(env.store, env.session, generation_epoch=9)
    original = env.journal.playback_generation

    async def reset_after_query(*args):
        recovered = await original(*args)
        env.store.reset(env.session.session_id)
        return recovered

    env.journal.playback_generation = reset_after_query
    runtime = ConverseRuntime(env.journal, ScriptedProvider([]))
    before = len(env.store.read())
    with pytest.raises(ValueError, match="Session changed"):
        await runtime._epoch(env.session.session_id)
    assert not runtime.playback and not runtime._playback_start
    assert len(env.store.read()) == before


async def test_auth_and_exact_scope_are_required_for_scalar_read(env):
    app = create_app(env.store, env.auth)
    query = {
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
    }
    path = "/v1/transcript/playback/generation"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
    ) as client:
        assert (await client.get(path, params=query)).status_code == 401
        assert (
            await client.get(
                path, params=query, headers={"Authorization": "Bearer " + env.auth.tokens["agent"]}
            )
        ).status_code == 403
        for role in ("owner", "converse"):
            headers = {"Authorization": "Bearer " + env.auth.tokens[role]}
            response = await client.get(path, params=query, headers=headers)
            assert response.status_code == 200 and len(response.content) < 512
            assert (
                await client.get(path, params={**query, "thread_id": "wrong"}, headers=headers)
            ).status_code == 409
            assert (
                await client.get(path, params={**query, "epoch": "true"}, headers=headers)
            ).status_code == 422
    with pytest.raises(PermissionError):
        await LocalJournal(env.store, "agent").playback_generation(**query)
    other = env.store.create_session(SessionRequest(channel="agent"))
    with pytest.raises(JournalError):
        env.store.playback_generation(other.session_id, other.thread_id, other.epoch)


@pytest.mark.parametrize(
    "change",
    [
        {"maximum_generation": True},
        {"maximum_generation": "5"},
        {"maximum_generation": 1.0},
        {"maximum_generation": -1},
        {"maximum_generation": MAX_PLAYBACK_GENERATION + 1},
        {"epoch": True},
        {"session_id": "other"},
        {"thread_id": "other"},
        {"epoch": 2},
        {"text": "not metadata"},
    ],
)
async def test_client_rejects_malformed_or_foreign_native_response(change):
    body = {
        "schema_version": "1.0",
        "session_id": "session",
        "thread_id": "thread",
        "epoch": 1,
        "maximum_generation": 5,
        **change,
    }
    journal = JournalClient(
        "http://synthetic",
        "public-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    try:
        with pytest.raises(ValueError):
            await journal.playback_generation("session", "thread", 1)
    finally:
        await journal.close()


async def test_json_safe_boundary_and_overflow_never_allocate_an_unrepresentable_generation(env):
    entry = native_entry(env.store, env.session, generation_epoch=MAX_PLAYBACK_GENERATION)
    assert (await maximum(env)).maximum_generation == MAX_PLAYBACK_GENERATION
    runtime = ConverseRuntime(env.journal, ScriptedProvider([]))
    with pytest.raises(ValueError, match="JSON-safe"):
        await runtime._epoch(env.session.session_id)
    assert not runtime.playback and not runtime._playback_start
    assert not env.store.read(kinds=["delivery.updated"])
    cancelled = []
    epoch = PlaybackEpoch(
        MAX_PLAYBACK_GENERATION,
        chunks={entry.event_id: entry},
        on_begin=lambda: cancelled.append(True),
    )
    runtime.playback[env.session.session_id] = epoch
    task = asyncio.create_task(asyncio.Event().wait())
    runtime._active[env.session.session_id] = task
    with pytest.raises(ValueError, match="JSON-safe"):
        runtime.fence_playback(env.session.session_id)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and cancelled and not epoch.chunks
    assert epoch.generation == MAX_PLAYBACK_GENERATION and epoch.stop is None


async def test_maximum_minus_one_can_publish_last_representable_restart_stop(env):
    native_entry(env.store, env.session, generation_epoch=MAX_PLAYBACK_GENERATION - 1)
    runtime = ConverseRuntime(env.journal, ScriptedProvider([]))
    epoch = await runtime._epoch(env.session.session_id)
    assert epoch.generation == MAX_PLAYBACK_GENERATION
    assert (
        env.store.read(kinds=["delivery.updated"])[0].payload["next_generation_epoch"]
        == MAX_PLAYBACK_GENERATION
    )
    with pytest.raises(ValueError, match="JSON-safe"):
        epoch.begin()


@pytest.mark.parametrize("counter", [MAX_PLAYBACK_GENERATION, -1, True])
@pytest.mark.parametrize("location", ["cached", "pending_start"])
async def test_native_reset_retires_obsolete_counter_without_allocating_in_old_scope(
    env, counter, location
):
    entry = native_entry(env.store, env.session, generation_epoch=counter)
    cancelled = []
    old = PlaybackEpoch(
        counter,
        scope=(env.session.epoch, env.session.thread_id),
        chunks={entry.event_id: entry},
        on_begin=lambda: cancelled.append(True),
        stop=PlaybackStop(1, 0),
    )
    runtime = ConverseRuntime(env.journal, ScriptedProvider([]))
    if location == "cached":
        runtime.playback[env.session.session_id] = old
    else:
        runtime._playback_start[env.session.session_id] = PlaybackStart(old)
    current = env.store.reset(env.session.session_id)
    recovered = await runtime._epoch(env.session.session_id)
    assert recovered is not old and recovered.scope == (current.epoch, current.thread_id)
    assert recovered.generation == 0 and old.generation is counter
    assert old.stop.retired and not old.chunks and cancelled == [True]
    assert not runtime._playback_start and not runtime.provider.calls


async def decide(store, session, controller, name, behavior):
    source = await controller.ingest(
        SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id=name,
            window_index=0,
            text="Public synthetic " + name,
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
    )
    decision = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=source.payload["revision"],
        conversation=behavior,
        agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="routing.window_decided",
            payload=decision.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key="decision:" + source.event_id,
        ),
        "decisions",
    )


async def test_restart_after_105_non_generation_windows_emits_newer_stop_without_provider_replay(
    env,
):
    provider = ScriptedProvider(["Public synthetic reply already queued."])
    first_runtime = ConverseRuntime(env.journal, provider)
    first_controller = WindowConverseController(first_runtime)
    event = await decide(
        env.store, env.session, first_controller, "first", "RESPOND_WITHOUT_INTERRUPTING_USER"
    )
    await first_controller.process(event)
    old = first_controller.pending[env.session.session_id]
    await asyncio.wait_for(old.task, 2)
    assert first_controller._released(old) and not first_runtime._active
    generation = first_runtime.playback[env.session.session_id].generation
    for index in range(105):
        await first_controller.ingest(
            SpeechWindow(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                utterance_id="retained-" + str(index),
                window_index=0,
                text="Public synthetic capture window.",
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                capture_ended=True,
                content_scope="public_synthetic",
            )
        )
    assert not any(
        "generation_epoch" in e.payload
        for e in await env.journal.recent(session_id=env.session.session_id, limit=100)
    )
    # Simulated process loss: no clean old shutdown hook that would itself stop PCM.
    runtime = ConverseRuntime(env.journal, ScriptedProvider([]))
    controller = WindowConverseController(runtime)
    try:
        event = await decide(env.store, env.session, controller, "wait", "WAIT_FOR_NEXT_WINDOW")
        await controller.process(event)
        stops = env.store.read(kinds=["delivery.updated"])
        assert len(stops) == 1
        assert stops[0].payload["generation_epoch"] == generation
        assert stops[0].payload["next_generation_epoch"] > generation
        assert not runtime.provider.calls and len(provider.calls) == 1
        assert (
            runtime.playback[env.session.session_id].generation
            == stops[0].payload["next_generation_epoch"]
        )
    finally:
        await controller.close()


def test_durable_generation_survives_store_reopen_and_uses_existing_scope_index(tmp_path):
    path = tmp_path / "journal.sqlite"
    store = Store(path)
    session = store.create_session(SessionRequest(channel="converse"))
    native_entry(store, session, generation_epoch=71)
    store.close()
    store = Store(path)
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        assert (
            store.playback_generation(
                session.session_id, session.thread_id, session.epoch
            ).maximum_generation
            == 71
        )
        queries = [sql for sql in statements if "AS generation_type" in sql]
        assert len(queries) == 1
        plan = " ".join(row[3] for row in store.db.execute("EXPLAIN QUERY PLAN " + queries[0]))
        assert "SEARCH entries USING INDEX" in plan and "(session_id=?" in plan
    finally:
        store.close()
