"""Role-policy leads typed and retained-window prompts; personality stays unchanged.

Capture-provider checks over fresh public/synthetic fixtures only. No model
inference runs here; scripted prompts do not establish dialogue quality.
"""

from types import SimpleNamespace

from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
)
from services.converse.context import ROLE_POLICY, dialogue_system_message
from services.converse.runtime import ConverseRuntime
from services.converse.windows import PreparedReply, WindowConverseController
from services.session.store import LocalJournal, Store

LOCAL = ProviderInfo(provider="synthetic", model="local-fixture", local=True)
CONTRIBUTOR = ProviderInfo(provider="meta", model="synthetic-contributor-fixture", local=False)

ROLE_SNIPPET = "You provide dialogue, not tool execution."


class RecordingProvider:
    def __init__(self, info):
        self.info = info
        self.prompts = []

    async def stream(self, messages):
        self.prompts.append(messages)
        yield "Synthetic role-context reply."


def make_runtime(store, session, provider, **kwargs):
    journal = LocalJournal(store, "converse")
    runtime = ConverseRuntime(journal, provider, **kwargs)
    return runtime


def typed_request(session, *, text="Public synthetic work request.", key="role-1"):
    return ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text=text,
        content_scope="public_synthetic",
        idempotency_key=key,
    )


def test_helper_returns_single_leading_system_message_with_personality_unchanged():
    personality = "Custom synthetic style: brief and warm."
    message = dialogue_system_message(personality)
    assert message["role"] == "system"
    assert set(message.keys()) == {"role", "content"}
    assert ROLE_SNIPPET in message["content"]
    assert ROLE_POLICY in message["content"]
    assert personality in message["content"]
    # Fixed block precedes the labelled personality section.
    assert message["content"].index(ROLE_SNIPPET) < message["content"].index(personality)
    assert "Personality/style" in message["content"]


async def test_typed_default_personality_leads_once_and_keeps_history_order(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        provider = RecordingProvider(CONTRIBUTOR)
        runtime = make_runtime(store, session, provider)
        try:
            owner = LocalJournal(store, "owner")
            # Fresh public prior turn stays in scope for the contributor fixture.
            first = await owner.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.user",
                    payload={
                        "text": "Public synthetic prior question.",
                        "final": True,
                        "revision": 1,
                        "content_scope": "public_synthetic",
                    },
                    idempotency_key="role-prior-user",
                )
            )
            await LocalJournal(store, "converse").append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.converse",
                    payload={
                        "text": "Public synthetic prior answer.",
                        "final": True,
                        "content_scope": "public_synthetic",
                    },
                    causes=[first.event_id],
                    idempotency_key="role-prior-reply",
                )
            )
            result = await runtime.turn(typed_request(session))
            assert result["text"] == "Synthetic role-context reply."
            prompt = provider.prompts[-1]
            assert prompt[0] == dialogue_system_message(runtime.personality)
            assert prompt[0]["content"].count(ROLE_SNIPPET) == 1
            assert runtime.personality in prompt[0]["content"]
            # Existing history ordering is respected after the leading message.
            assert {"role": "user", "content": "Public synthetic prior question."} in prompt
            assert {"role": "assistant", "content": "Public synthetic prior answer."} in prompt
            assert prompt[-1] == {"role": "user", "content": "Public synthetic work request."}
            assert (
                prompt.index({"role": "user", "content": "Public synthetic prior question."})
                < prompt.index({"role": "assistant", "content": "Public synthetic prior answer."})
                < len(prompt) - 1
            )
        finally:
            await runtime.close()
    finally:
        store.close()


async def test_typed_custom_personality_included_unchanged_with_scope_preserved(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        custom = "Custom synthetic style: terse plain sentences."
        provider = RecordingProvider(LOCAL)
        runtime = make_runtime(store, session, provider, personality=custom)
        try:
            assert runtime.personality == custom
            assert runtime.personality_scope == "local_only"
            result = await runtime.turn(typed_request(session, key="role-custom"))
            assert result["text"] == "Synthetic role-context reply."
            prompt = provider.prompts[-1]
            assert prompt[0] == dialogue_system_message(custom)
            assert custom in prompt[0]["content"]
            assert ROLE_SNIPPET in prompt[0]["content"]
            assert prompt[-1] == {"role": "user", "content": "Public synthetic work request."}
        finally:
            await runtime.close()
    finally:
        store.close()


async def test_window_compilation_uses_same_leading_message_and_order(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        provider = RecordingProvider(LOCAL)
        runtime = make_runtime(store, session, provider)
        try:
            controller = WindowConverseController(runtime)
            owner = LocalJournal(store, "owner")
            payload = SpeechWindow(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                utterance_id="role-window-1",
                window_index=0,
                revision=1,
                text="Public synthetic window request.",
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                capture_ended=True,
                content_scope="public_synthetic",
            ).model_dump(mode="json")
            source = await owner.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.window",
                    payload=payload,
                    idempotency_key="role-window-source",
                )
            )
            epoch = await runtime._epoch(session.session_id)
            prepared = PreparedReply(
                source=source,
                decision=SimpleNamespace(payload={"agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING"}),
                behavior="RESPOND",
                base_generation=epoch.generation,
                admission=runtime.admission(),
            )
            prepared.provider = provider
            messages = await controller._prepare(prepared)
            assert messages is not None
            assert messages[0] == dialogue_system_message(runtime.personality)
            assert messages[0]["content"].count(ROLE_SNIPPET) == 1
            assert runtime.personality in messages[0]["content"]
            assert messages[-1] == {"role": "user", "content": "Public synthetic window request."}
            # Same helper object shape as the typed path.
            assert messages[0]["role"] == "system"
        finally:
            await runtime.close()
    finally:
        store.close()
