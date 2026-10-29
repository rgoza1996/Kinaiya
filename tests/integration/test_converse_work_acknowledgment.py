"""Native prompt/authority regressions; recording providers do not prove dialogue quality."""

import asyncio
from types import SimpleNamespace

import pytest

from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))

    class RecordingProvider:
        info = ProviderInfo(provider="synthetic", model="local-fixture", local=True)
        text = "Tell me a little more."

        def __init__(self):
            self.calls = []

        async def stream(self, messages):
            # Observe native persistence at inference time, rather than after it.
            self.calls.append((messages, store.read(limit=100)))
            yield self.text

    provider = RecordingProvider()
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    controller = WindowConverseController(runtime)
    result = SimpleNamespace(
        store=store, session=session, provider=provider, runtime=runtime, controller=controller
    )
    yield result
    await controller.close()
    await runtime.close()
    store.close()


async def respond(env, path, text):
    s = env.session
    if path == "typed":
        await asyncio.wait_for(
            env.runtime.turn(
                ConverseRequest(
                    session_id=s.session_id,
                    thread_id=s.thread_id,
                    epoch=s.epoch,
                    text=text,
                    content_scope="public_synthetic",
                    idempotency_key="public-input",
                )
            ),
            2,
        )
    else:
        source = await env.controller.ingest(
            SpeechWindow(
                session_id=s.session_id,
                thread_id=s.thread_id,
                epoch=s.epoch,
                utterance_id="public-utterance",
                window_index=0,
                revision=1,
                text=text,
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                content_scope="public_synthetic",
            )
        )
        # Synthetic timing control only; no Agent status or model routing claim.
        decision = WindowDecision(
            source_event_id=source.event_id,
            source_sequence=source.sequence,
            source_revision=1,
            conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
            agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
        )
        event = env.store.append(
            AppendRequest(
                session_id=s.session_id,
                thread_id=s.thread_id,
                epoch=s.epoch,
                kind="routing.window_decided",
                payload=decision.model_dump(mode="json"),
                causes=[source.event_id],
                idempotency_key="synthetic-timing-control",
            ),
            "decisions",
        )
        await env.controller.process(event)
        jobs = [p.task for p in env.controller.pending.values()]
        if jobs:
            await asyncio.wait_for(asyncio.gather(*jobs), 2)
        await env.controller.tick()
    return env.store.read(kinds=["conversation.converse"])


@pytest.mark.parametrize("path", ["typed", "window"])
@pytest.mark.parametrize(
    "text",
    [
        "Please write a note about tomorrow's meeting.",
        "Can you inspect the report and summarize the risks?",
        "Hi, how is your day?",
        "Could you help with that thing?",
    ],
)
async def test_native_reply_needs_no_agent_and_offers_generic_role_guidance(env, path, text):
    replies = await respond(env, path, text)
    assert len(env.provider.calls) == len(replies) == 1
    messages, persisted_at_inference = env.provider.calls[0]
    assert messages[0] == {"role": "system", "content": env.runtime.personality}
    assert messages[-1] == {"role": "user", "content": text}
    source = next(e for e in persisted_at_inference if e.payload.get("text") == text)
    assert source.kind == ("conversation.user" if path == "typed" else "conversation.window")
    assert source.event_id in replies[0].causes
    assert replies[0].payload["content_scope"] == "public_synthetic"
    assert replies[0].payload["text"] == env.provider.text
    guidance = messages[0]["content"].lower()
    assert "acknowledge a work request" in guidance
    assert "unknown until an authenticated notice" in guidance
    assert "optional agent" in guidance
    assert "legitimate safety and capability refusals" in guidance
    assert not env.store.read(kinds=["agent.command", "agent.outcome", "approval.requested"])


@pytest.mark.parametrize("path", ["typed", "window"])
async def test_native_effect_claim_guard_remains_independent_of_prompt_guidance(env, path):
    env.provider.text = "I created the file."
    replies = await respond(env, path, "Please write the note.")
    assert (
        replies[0].payload["text"] == "The Agent has not provided a verified result for that claim."
    )
    # The typed path emits a safety event; windows currently replace the claim
    # inline. Preserve both existing behaviors rather than inventing telemetry.
    assert replies[0].payload["text"] != env.provider.text
    if path == "typed":
        assert env.store.read(kinds=["converse.safety"])
    assert not env.store.read(kinds=["agent.outcome"])


@pytest.mark.parametrize("path", ["typed", "window"])
@pytest.mark.parametrize(
    "refusal",
    [
        "I can't help steal someone's passwords.",
        "I don't have enough information to answer that.",
    ],
)
async def test_legitimate_refusal_is_preserved_without_forcing_acceptance(env, path, refusal):
    env.provider.text = refusal
    replies = await respond(env, path, "Can you help with this?")
    assert replies[0].payload["text"] == refusal
    assert not env.store.read(kinds=["agent.command", "agent.outcome", "converse.safety"])


@pytest.mark.parametrize("path", ["typed", "window"])
async def test_custom_personality_is_preserved_with_local_derived_scope(env, path):
    persona = "A private custom character description."
    env.runtime.personality = persona
    env.runtime.personality_scope = "local_only"
    replies = await respond(env, path, "Let's talk about the weather.")
    assert env.provider.calls[0][0][0] == {"role": "system", "content": persona}
    assert replies[0].payload["content_scope"] == "local_only"
    custom = ConverseRuntime(
        LocalJournal(env.store, "converse"), env.provider, personality=persona, name="Custom"
    )
    assert custom.personality == persona and custom.personality_scope == "local_only"
    await custom.close()
