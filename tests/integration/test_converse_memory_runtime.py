"""Real Session/Core and Converse paths; synthetic counting/generation, no models."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.memory import MemoryBinding
from packages.contracts.memory_context import MemoryUseConfigure
from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.prompt_budget import PromptTokenCount
from services.common.auth import Auth
from services.converse.api import create_app as converse_app
from services.converse.memory_adapter import SessionMemoryAdapter
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.api import create_app as session_app
from services.session.store import Store

TOKENS = {role: char * 32 for role, char in (("owner", "o"), ("converse", "c"), ("agent", "a"))}


class Provider:
    info = ProviderInfo(provider="synthetic", model="local-counted-fixture", local=True)

    def __init__(self):
        self.counts = []
        self.prompts = []
        self.fits = True
        self.before_count = None

    async def measure_prompt(self, messages):
        self.counts.append(messages)
        if self.before_count:
            await self.before_count()
        return PromptTokenCount(32 if self.fits else 60, 64, 16, "a" * 64, "b" * 64)

    async def stream(self, messages):
        self.prompts.append(messages)
        yield f"Synthetic conversational reply {len(self.prompts)}."


@pytest.fixture
async def host(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    binding = MemoryBinding(
        session_id=session.session_id, thread_id=session.thread_id, epoch=session.epoch
    )
    auth = Auth(TOKENS)
    app = session_app(store, auth, converse_memory=True)
    memory = app.state.converse_memory
    memory.configure({**binding.model_dump(), "expected_revision": 0, "enabled": True})
    source = store.append(
        AppendRequest(
            **binding.model_dump(),
            kind="conversation.user",
            idempotency_key="curated-source",
            payload={
                "text": "Synthetic owner-curated source.",
                "final": True,
                "revision": 1,
                "content_scope": "local_only",
            },
        ),
        "owner",
    )
    parent = memory.capture(
        {
            **binding.model_dump(),
            "expected_revision": 1,
            "idempotency_key": "parent",
            "kind": "assertion",
            "source_event_id": source.event_id,
            "source_revision": 1,
            "body": "Synthetic parent memory.",
            "fact": {"key": "synthetic.preference", "value": "green"},
        }
    ).node
    child = memory.capture(
        {
            **binding.model_dump(),
            "expected_revision": 2,
            "idempotency_key": "child",
            "kind": "inference",
            "source_event_id": source.event_id,
            "source_revision": 1,
            "body": "Synthetic dependent memory.",
            "fact": None,
            "prerequisite_ids": [parent.node_id],
        }
    ).node
    transport = httpx.ASGITransport(app=app)
    journal = JournalClient("http://127.0.0.1", TOKENS["converse"], transport=transport)
    adapter = SessionMemoryAdapter("http://127.0.0.1", TOKENS["converse"], transport=transport)
    provider = Provider()
    runtime = ConverseRuntime(journal, provider, memory_adapters={"korina-dag-v1": adapter})
    windows = WindowConverseController(runtime)
    value = SimpleNamespace(
        store=store,
        session=session,
        binding=binding,
        memory=memory,
        source=source,
        parent=parent,
        child=child,
        journal=journal,
        adapter=adapter,
        provider=provider,
        runtime=runtime,
        windows=windows,
        auth=auth,
    )
    try:
        yield value
    finally:
        await windows.close()
        await runtime.close()
        await journal.close()
        store.close()


async def use(host, enabled=True):
    state = await host.runtime.memory_state(host.binding)
    return await host.runtime.memory_use(
        MemoryUseConfigure(
            **host.binding.model_dump(),
            expected_runtime_id=state.runtime_id,
            expected_generation=state.use_generation,
            enabled=enabled,
            implementation_id="korina-dag-v1" if enabled else None,
            selector_ids=[host.child.node_id] if enabled else [],
        )
    )


async def typed(host, key):
    return await host.runtime.turn(
        ConverseRequest(
            **host.binding.model_dump(),
            text="Synthetic question " + key,
            idempotency_key=key,
            content_scope="public_synthetic",
        )
    )


def replies(host):
    return host.store.read(session_id=host.session.session_id, kinds=["conversation.converse"])


async def test_runtime_starts_off_and_off_does_not_read_dag_or_count(host, monkeypatch):
    def forbidden(*_, **__):
        pytest.fail("Disabled runtime use accessed the DAG")

    monkeypatch.setattr(host.memory, "recall", forbidden)
    monkeypatch.setattr(host.memory, "validate_context", forbidden)
    assert not (await host.runtime.memory_state(host.binding)).enabled
    result = await typed(host, "off")
    assert result["text"] == "Synthetic conversational reply 1."
    assert host.provider.counts == []
    assert "memory_context" not in replies(host)[0].payload


async def test_typed_exact_prompt_transitive_history_and_off_filter_with_retained_transcript(host):
    await use(host)
    await typed(host, "first")
    first = replies(host)[0]
    assert first.payload["content_scope"] == "local_only"
    assert first.payload["memory_context"]["selector_ids"] == [host.child.node_id]
    prompt = host.provider.prompts[0]
    assert prompt == host.provider.counts[0]
    assert "Synthetic parent memory." in prompt[1]["content"]
    assert "Synthetic dependent memory." in prompt[1]["content"]
    await typed(host, "second")
    assert first.payload["text"] in [row["content"] for row in host.provider.prompts[-1]]
    assert replies(host)[-1].payload["memory_context"] == first.payload["memory_context"]
    last = replies(host)[-1]
    assert await host.runtime.validate_speech(last.event_id, last.payload["generation_epoch"])
    await use(host, False)
    with pytest.raises(ValueError):
        await host.runtime.validate_speech(last.event_id, last.payload["generation_epoch"])
    await typed(host, "third")
    third_prompt = host.provider.prompts[-1]
    assert all("memory." not in row["content"] for row in third_prompt)
    assert not any(row["role"] == "assistant" for row in third_prompt)
    assert "memory_context" not in replies(host)[-1].payload
    assert len(replies(host)) == 3 and len(host.provider.counts) == 2


@pytest.mark.parametrize(
    "mode", ["too_large", "forget", "correction", "disable", "replace_provider"]
)
async def test_whole_closure_or_stale_measurement_is_omitted_before_generation(host, mode):
    await use(host)
    if mode == "too_large":
        host.provider.fits = False
    else:

        async def during_count():
            if mode == "forget":
                host.memory.forget(
                    {
                        **host.binding.model_dump(),
                        "expected_revision": 3,
                        "idempotency_key": "forget",
                        "node_id": host.parent.node_id,
                    }
                )
            elif mode == "correction":
                host.store.append(
                    AppendRequest(
                        **host.binding.model_dump(),
                        kind="conversation.revision",
                        idempotency_key="correct",
                        payload={
                            "target_event_id": host.source.event_id,
                            "revision": 2,
                            "text": "Corrected synthetic source.",
                        },
                        causes=[host.source.event_id],
                    ),
                    "owner",
                )
            elif mode == "disable":
                host.memory.configure(
                    {**host.binding.model_dump(), "expected_revision": 3, "enabled": False}
                )
            else:
                host.runtime.provider = Provider()

        host.provider.before_count = during_count
    await typed(host, mode)
    if mode == "replace_provider":
        assert host.provider.prompts == [] and replies(host) == []
        return
    assert len(host.provider.prompts) == 1
    prompt = host.provider.prompts[0]
    assert not any(
        "Synthetic parent memory." in row["content"]
        or "Synthetic dependent memory." in row["content"]
        for row in prompt
    )
    assert "memory_context" not in replies(host)[-1].payload


async def window_reply(host, key, *, speaking=False):
    source = await host.windows.ingest(
        SpeechWindow(
            **host.binding.model_dump(),
            utterance_id=key,
            window_index=0,
            revision=1,
            text="Synthetic speech-window question.",
            start_ms=0,
            end_ms=1000,
            user_speaking=speaking,
            content_scope="public_synthetic",
        )
    )
    decision = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=1,
        conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
        agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
    )
    event = host.store.append(
        AppendRequest(
            **host.binding.model_dump(),
            kind="routing.window_decided",
            causes=[source.event_id],
            payload=decision.model_dump(mode="json"),
            idempotency_key="decision:" + key,
        ),
        "decisions",
    )
    await host.windows.process(event)
    prepared = host.windows.pending[host.session.session_id]
    await asyncio.wait_for(prepared.task, 2)
    return prepared


async def test_window_prompt_consumes_current_tagged_typed_history_and_publishes_pin(host):
    await use(host)
    await typed(host, "typed")
    first = replies(host)[0]
    await window_reply(host, "window")
    assert len(replies(host)) == 2
    assert replies(host)[-1].payload["memory_context"] == first.payload["memory_context"]
    assert host.provider.prompts[-1] == host.provider.counts[-1]
    assert first.payload["text"] in [row["content"] for row in host.provider.prompts[-1]]


async def test_memory_off_prevents_prepared_window_from_publishing_at_later_opening(host):
    await use(host)
    prepared = await window_reply(host, "held", speaking=True)
    assert prepared.chunks and not replies(host)
    await use(host, False)
    await host.windows.flush(prepared)
    assert prepared.abandoned and not replies(host)


async def test_owner_controls_authenticate_before_body_receive(host):
    app = converse_app(host.runtime, host.auth)
    receives = 0
    output = []

    async def receive():
        nonlocal receives
        receives += 1
        raise AssertionError("Unauthorized body receive")

    async def send(value):
        output.append(value)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/memory/use",
            "raw_path": b"/v1/memory/use",
            "query_string": b"",
            "headers": [(b"authorization", ("Bearer " + TOKENS["converse"]).encode())],
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 1),
            "root_path": "",
        },
        receive,
        send,
    )
    assert receives == 0 and output[0]["status"] == 403


@pytest.mark.parametrize(
    "body",
    [
        b'{"enabled":true,"enabled":false}',
        b'{"epoch":NaN}',
        b'{"session_id":"\\ud800"}',
        b"x" * 16385,
    ],
)
async def test_owner_control_rejects_malformed_or_oversized_json_without_echo(host, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=converse_app(host.runtime, host.auth)),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/v1/memory/use",
            content=body,
            headers={
                "Authorization": "Bearer " + TOKENS["owner"],
                "Content-Type": "application/json",
            },
        )
    assert response.status_code in {413, 422}
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) < 200
