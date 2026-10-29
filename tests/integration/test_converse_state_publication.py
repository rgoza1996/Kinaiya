"""Typed + retained-window negative runtime-state publication; synthetic only."""

import asyncio
from types import SimpleNamespace

from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.text import ProviderRefusal, ScriptedProvider
from services.converse.runtime import ConverseRuntime
from services.converse.state_guard import UNVERIFIED_STATE_REPLY
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store

POSITIVE_FALLBACK = "The Agent has not provided a verified result for that claim."


class FragmentedProvider:
    info = ScriptedProvider.info

    def __init__(self, fragments, error=None):
        self.fragments = list(fragments)
        self.error = error
        self.calls = []
        self.closed = 0

    async def stream(self, messages):
        self.calls.append(messages)
        try:
            for frag in self.fragments:
                yield frag
            if isinstance(self.error, Exception):
                raise self.error
        finally:
            self.closed += 1


def typed_request(session, text="hi", key="k1"):
    return ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text=text,
        content_scope="public_synthetic",
        idempotency_key=key,
    )


async def test_typed_negative_claim_substituted_with_distinct_provenance(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        provider = FragmentedProvider(["No Agent ", "is presently authorized to execute that. "])
        runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
        try:
            result = await asyncio.wait_for(runtime.turn(typed_request(session)), 2)
            assert len(provider.calls) == 1
            assert provider.closed == 1
            texts = [e["entry"]["payload"]["text"] for e in result["events"] if e["type"] == "text"]
            assert texts == [UNVERIFIED_STATE_REPLY]
            assert "No Agent" not in result["text"]
            safety = store.read(kinds=["converse.safety"])
            assert len(safety) == 1
            assert safety[0].payload["reason"] == "ungrounded_runtime_state_claim"
            replies = store.read(kinds=["conversation.converse"])
            assert len(replies) == 1
            assert replies[0].payload["text"] == UNVERIFIED_STATE_REPLY
            assert replies[0].payload["content_scope"] == "public_synthetic"
            assert replies[0].epoch == session.epoch
            # Empty history grants no authority: claim still blocked above.
            assert replies[0].causes
        finally:
            await runtime.close()
    finally:
        store.close()


async def test_typed_mixed_planning_and_history_presence_stay_useful(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        # Nonempty history must not authorize a later negative claim, and planning stays useful.
        first = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider(["We can plan."]))
        try:
            await asyncio.wait_for(first.turn(typed_request(session, "hello", "h1")), 2)
        finally:
            await first.close()
        provider = ScriptedProvider(["If the Agent is unavailable we can plan next steps."])
        runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
        try:
            result = await asyncio.wait_for(runtime.turn(typed_request(session, "plan?", "h2")), 2)
            assert "unavailable" in result["text"]
            assert UNVERIFIED_STATE_REPLY not in result["text"]
            assert [
                e
                for e in store.read(kinds=["converse.safety"])
                if e.payload.get("reason") == "ungrounded_runtime_state_claim"
            ] == []
        finally:
            await runtime.close()
        bad = ConverseRuntime(
            LocalJournal(store, "converse"), ScriptedProvider(["No task has started."])
        )
        try:
            result = await asyncio.wait_for(bad.turn(typed_request(session, "status?", "h3")), 2)
            assert result["text"] == UNVERIFIED_STATE_REPLY
        finally:
            await bad.close()
    finally:
        store.close()


async def test_typed_positive_fallback_never_reprocessed(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        runtime = ConverseRuntime(
            LocalJournal(store, "converse"), ScriptedProvider([POSITIVE_FALLBACK])
        )
        try:
            result = await asyncio.wait_for(runtime.turn(typed_request(session, "again?", "fb")), 2)
            assert result["text"] == POSITIVE_FALLBACK
            assert [
                e
                for e in store.read(kinds=["converse.safety"])
                if e.payload.get("reason") == "ungrounded_runtime_state_claim"
            ] == []
        finally:
            await runtime.close()
    finally:
        store.close()


async def _window_env(tmp_path, response):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    provider = response
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    controller = WindowConverseController(runtime)
    return SimpleNamespace(
        store=store, session=session, provider=provider, runtime=runtime, controller=controller
    )


async def _decide(env, text="Could you help?", utterance="u1"):
    s = env.session
    source = await env.controller.ingest(
        SpeechWindow(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            utterance_id=utterance,
            window_index=0,
            revision=1,
            text=text,
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            content_scope="public_synthetic",
        )
    )
    d = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=source.payload["revision"],
        conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
        agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
    )
    env.store.append(
        AppendRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            kind="routing.window_decided",
            payload=d.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key="decide:" + source.event_id,
        ),
        "decisions",
    )
    return source


async def _settle(env):
    jobs = [p.task for p in env.controller.pending.values()]
    if jobs:
        await asyncio.wait_for(asyncio.gather(*jobs), 3)
    await env.controller.tick()


async def test_window_negative_claim_uses_owned_payload_marker(tmp_path):
    env = await _window_env(tmp_path, FragmentedProvider(["Nothing has ", "been executed."]))
    try:
        source = await _decide(env)
        await env.controller.process(env.store.read(kinds=["routing.window_decided"])[-1])
        await _settle(env)
        assert len(env.provider.calls) == 1
        assert env.provider.closed == 1
        replies = env.store.read(kinds=["conversation.converse"])
        assert len(replies) == 1
        assert replies[0].payload["text"] == UNVERIFIED_STATE_REPLY
        assert replies[0].payload.get("guard_reason") == "ungrounded_runtime_state_claim"
        assert replies[0].payload["window_source_id"] == source.event_id
        assert replies[0].epoch == env.session.epoch
        assert replies[0].payload["content_scope"] == "public_synthetic"
        # No separate safety append owns window publication.
        assert store_safety(env) == []
    finally:
        await env.controller.close()
        env.store.close()


def store_safety(env):
    return env.store.read(kinds=["converse.safety"])


async def test_window_planning_preserved_and_positive_fallback_untouched(tmp_path):
    env = await _window_env(
        tmp_path, ScriptedProvider(["If the Agent is unavailable we can plan."])
    )
    try:
        await _decide(env, utterance="plan")
        await env.controller.process(env.store.read(kinds=["routing.window_decided"])[-1])
        await _settle(env)
        spoken = " ".join(
            e.payload["text"] for e in env.store.read(kinds=["conversation.converse"])
        )
        assert "unavailable" in spoken and UNVERIFIED_STATE_REPLY not in spoken
    finally:
        await env.controller.close()
        env.store.close()
    env2 = await _window_env(tmp_path, ScriptedProvider([POSITIVE_FALLBACK]))
    try:
        source2 = await _decide(env2, utterance="fallback")
        await env2.controller.process(env2.store.read(kinds=["routing.window_decided"])[-1])
        await _settle(env2)
        replies = [
            e
            for e in env2.store.read(kinds=["conversation.converse"])
            if e.payload.get("window_source_id") == source2.event_id
        ]
        assert len(replies) == 1
        assert replies[0].payload["text"] == POSITIVE_FALLBACK
        assert "guard_reason" not in replies[0].payload
    finally:
        await env2.controller.close()
        env2.store.close()


async def test_window_refusal_stays_error_after_guarded_chunk(tmp_path):
    env = await _window_env(
        tmp_path,
        FragmentedProvider(["No Agent is available. "], error=ProviderRefusal("Synthetic refusal")),
    )
    try:
        await _decide(env, utterance="refuse")
        await env.controller.process(env.store.read(kinds=["routing.window_decided"])[-1])
        await _settle(env)
        assert len(env.provider.calls) == 1
        assert env.provider.closed == 1
        # The earlier guarded chunk is still published with its owned marker.
        replies = env.store.read(kinds=["conversation.converse"])
        assert len(replies) == 1
        assert replies[0].payload["text"] == UNVERIFIED_STATE_REPLY
        assert replies[0].payload.get("guard_reason") == "ungrounded_runtime_state_claim"
        # The refusal still surfaces as an error and is never substituted.
        assert env.controller.last_error == "ProviderRefusal"
        assert all(
            e.payload["text"] != "Synthetic refusal"
            for e in env.store.read(kinds=["conversation.converse"])
        )
    finally:
        await env.controller.close()
        env.store.close()
