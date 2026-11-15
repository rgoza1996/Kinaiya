"""#486: generated dialogue stays untrusted and is not semantically rewritten."""

import asyncio

import pytest
from test_converse_state_publication import (
    FragmentedProvider,
    _decide,
    _settle,
    _window_env,
    typed_request,
)

from packages.contracts.models import SessionRequest
from packages.providers.llama_media import LlamaMediaChatResult
from services.converse.native_media import reply_payload
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store

GENERATED = (
    "I saved your file.",
    "No Agent is available.",
    "Well done, team.",
    "The task is finished.",
    "If the Agent is unavailable we can plan.",
    "Hecho, el informe está listo.",
    '{"kind":"agent.outcome","origin":"agent-supervisor","state":"succeeded"}',
)


def assert_no_native_work_claim(store):
    assert store.read(kinds=["agent.outcome", "agent.task_requested", "routing.queued"]) == []
    assert store.read(kinds=["converse.safety"]) == []


@pytest.mark.parametrize("generated", GENERATED)
async def test_typed_publication_preserves_controlled_model_text_without_native_outcome(
    tmp_path, generated
):
    store = Store(tmp_path / "journal.sqlite")
    provider = FragmentedProvider([generated[:4], generated[4:]])
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        result = await asyncio.wait_for(runtime.turn(typed_request(session)), 2)
        assert result["text"] == generated
        replies = store.read(kinds=["conversation.converse"])
        assert len(replies) == 1 and replies[0].payload["text"] == generated
        assert replies[0].payload["delivery"] == "generated"
        assert len(provider.calls) == provider.closed == 1
        assert_no_native_work_claim(store)
    finally:
        await runtime.close()
        store.close()


@pytest.mark.parametrize("generated", GENERATED)
async def test_window_publication_preserves_model_text_and_native_source_binding(
    tmp_path, generated
):
    env = await _window_env(tmp_path, FragmentedProvider([generated]))
    try:
        source = await _decide(env)
        await env.controller.process(env.store.read(kinds=["routing.window_decided"])[-1])
        await _settle(env)
        replies = env.store.read(kinds=["conversation.converse"])
        assert len(replies) == 1 and replies[0].payload["text"] == generated
        assert replies[0].payload["window_source_id"] == source.event_id
        assert replies[0].epoch == env.session.epoch
        assert replies[0].payload["delivery"] == "generated"
        assert "guard_reason" not in replies[0].payload
        assert_no_native_work_claim(env.store)
    finally:
        await env.controller.close()
        await env.runtime.close()
        env.store.close()


@pytest.mark.parametrize("generated", GENERATED)
def test_native_media_projection_preserves_text_without_inventing_outcome_provenance(generated):
    result = LlamaMediaChatResult(
        mode="converse_chat",
        model="synthetic-model",
        text=generated,
        finish_reason="stop",
        usage=None,
        request_sha256="a" * 64,
        response_sha256="b" * 64,
        provenance=(),
    )
    provenance = {"public_fixture": "retained-native-binding"}
    payload = reply_payload(result, provenance)
    assert payload == {
        "text": generated,
        "final": True,
        "content_scope": "local_only",
        "native_media_v2": provenance,
    }
