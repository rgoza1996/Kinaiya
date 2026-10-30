"""Separate-work acknowledgments stay natural; synthetic providers only."""

import asyncio

import pytest

from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.prompt_budget import PromptTokenCount, request_digest
from packages.providers.text import ScriptedProvider
from services.converse.runtime import ConverseRuntime
from services.converse.state_guard import UNVERIFIED_STATE_REPLY
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store

ACK_PHRASES = ("Give me some time on it.", "Let me think about that.")


class MeasuredAckProvider(ScriptedProvider):
    async def measure_prompt(self, messages):
        # Synthetic deterministic test tokenizer, not a model-token estimate.
        count = 11 + sum(7 + len(m["role"]) + len(m["content"].encode()) for m in messages)
        return PromptTokenCount(count, 32768, 128, request_digest(messages), "b" * 64)


def _typed_request(session, text, key):
    return ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text=text,
        content_scope="public_synthetic",
        idempotency_key=key,
    )


def _prompt_text(messages):
    return " ".join(
        str(part.get("content", part) if isinstance(part, dict) else part)
        for part in (messages if isinstance(messages, list) else [messages])
    )


def _journal_kinds(store):
    try:
        return [entry.kind for entry in store.read()]
    except TypeError:
        return [entry.kind for entry in store.read(limit=100)]


@pytest.mark.parametrize("phrase", ACK_PHRASES)
async def test_acknowledgment_phrase_passes_through_typed_and_published_reply(tmp_path, phrase):
    """A receipt-only acknowledgment must survive typed reply publication unsubstituted."""
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        provider = ScriptedProvider([phrase])
        runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
        try:
            result = await asyncio.wait_for(
                runtime.turn(_typed_request(session, "please analyze this repo", "ack-1")),
                2,
            )
            # Typed reply carries the provider's natural acknowledgment verbatim.
            assert result["text"] == phrase
            assert UNVERIFIED_STATE_REPLY not in result["text"]
            text_events = [event for event in result["events"] if event.get("type") == "text"]
            assert text_events
            assert text_events[0]["entry"]["payload"]["text"] == phrase
            # Live/shared publication records the same acknowledgment verbatim.
            replies = store.read(kinds=["conversation.converse"])
            assert replies
            assert replies[-1].payload["text"] == phrase
            # A receipt-only acknowledgment is not an ungrounded state claim.
            safety = store.read(kinds=["converse.safety"])
            assert [
                entry
                for entry in safety
                if entry.payload.get("reason") == "ungrounded_runtime_state_claim"
            ] == []
        finally:
            await runtime.close()
    finally:
        store.close()


async def test_builtin_persona_reaches_provider_prompt(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        provider = ScriptedProvider(["Give me some time on it."])
        runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
        try:
            await asyncio.wait_for(
                runtime.turn(_typed_request(session, "please analyze this repo", "ack-2")),
                2,
            )
            assert len(provider.calls) == 1
            prompt = _prompt_text(provider.calls[0])
            assert "briefly acknowledge receipt" in prompt
            assert "receipt only" in prompt
            assert "asynchronous" in prompt
        finally:
            await runtime.close()
    finally:
        store.close()


async def test_builtin_persona_reaches_budgeted_provider_prompt(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        provider = MeasuredAckProvider(["Let me think about that."])
        runtime = ConverseRuntime(LocalJournal(store, "converse"), provider, context_budgeted=True)
        try:
            result = await asyncio.wait_for(
                runtime.turn(_typed_request(session, "please analyze this repo", "ack-3")),
                2,
            )
            assert result["text"] == "Let me think about that."
            assert len(provider.calls) == 1
            prompt = _prompt_text(provider.calls[0])
            assert "briefly acknowledge receipt" in prompt
            assert "receipt only" in prompt
        finally:
            await runtime.close()
    finally:
        store.close()


async def test_acknowledgment_creates_no_agent_work(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        provider = ScriptedProvider(["Give me some time on it."])
        runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
        try:
            kinds_before = set(_journal_kinds(store))
            await asyncio.wait_for(
                runtime.turn(_typed_request(session, "please analyze this repo", "ack-4")),
                2,
            )
            kinds_after = set(_journal_kinds(store))
            assert len(provider.calls) == 1
            for kind in kinds_after - kinds_before:
                lowered = kind.lower()
                assert "agent" not in lowered
                assert "task" not in lowered
                assert "claim" not in lowered
                assert "tool" not in lowered
        finally:
            await runtime.close()
    finally:
        store.close()


async def test_custom_constructor_personality_remains_unchanged(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        provider = ScriptedProvider(["Custom ack."])
        runtime = ConverseRuntime(
            LocalJournal(store, "converse"), provider, personality="Custom voice."
        )
        try:
            assert runtime.personality == "Custom voice."
            assert "briefly acknowledge receipt" not in runtime.personality
            result = await asyncio.wait_for(
                runtime.turn(_typed_request(session, "hello", "ack-5")),
                2,
            )
            assert result["text"] == "Custom ack."
            prompt = _prompt_text(provider.calls[0])
            assert "Custom voice." in prompt
            assert "briefly acknowledge receipt" not in prompt
        finally:
            await runtime.close()
    finally:
        store.close()


@pytest.mark.parametrize("phrase", ACK_PHRASES)
@pytest.mark.parametrize("budgeted", [False, True])
async def test_live_window_uses_shared_acknowledgment_without_dispatch_proof(
    tmp_path, phrase, budgeted
):
    store = Store(tmp_path / "journal.sqlite")
    provider = MeasuredAckProvider([phrase])
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider, context_budgeted=budgeted)
    controller = WindowConverseController(runtime)
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        source = await controller.ingest(
            SpeechWindow(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                utterance_id="synthetic-work",
                window_index=0,
                text="Analyze this public synthetic repository.",
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
            source_revision=1,
            conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
            agent_route="QUEUE_UNTIL_TASK_DONE",
        )
        entry = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="routing.window_decided",
                payload=decision.model_dump(mode="json"),
                causes=[source.event_id],
                idempotency_key="synthetic-decision",
            ),
            "decisions",
        )
        await controller.process(entry)
        tasks = [pending.task for pending in controller.pending.values() if pending.task]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
        replies = store.read(kinds=["conversation.converse"])
        assert [reply.payload["text"] for reply in replies] == [phrase]
        assert replies[0].payload["window_source_id"] == source.event_id
        assert "briefly acknowledge receipt" in _prompt_text(provider.calls[0])
        assert (
            store.read(kinds=["routing.window_dispatch", "task.created", "task.state_changed"])
            == []
        )
    finally:
        await controller.close()
        await runtime.close()
        store.close()
