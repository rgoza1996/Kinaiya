"""Compatible replacement and provider identity fences with the actual Core."""

import asyncio

import pytest

from packages.contracts.memory import MemoryBinding
from packages.contracts.memory_context import MemoryContextPin, MemoryUseConfigure
from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.prompt_budget import PromptTokenCount
from services.converse.memory_adapter import RecallSnapshot
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.resources.providers import ResourceBoundProvider
from services.resources.state import ResourceState
from services.session.store import LocalJournal, Store


class Adapter:
    def __init__(self, label):
        self.label, self.reads, self.closed = label, 0, 0
        self.on_recall = self.on_validate = None

    async def recall(self, binding, selectors, generation):
        self.reads += 1
        if self.on_recall:
            self.on_recall()
        return RecallSnapshot(
            binding,
            MemoryContextPin(
                schema_version="1.0",
                implementation_id="compatible-v1",
                use_generation=generation,
                namespace_revision=0,
                closure_sha256="a" * 64,
                selector_ids=list(selectors),
            ),
            {"role": "system", "content": "Quoted untrusted synthetic note: " + self.label},
        )

    async def validate(self, snapshot):
        if self.on_validate:
            self.on_validate()
        return True

    async def close(self):
        self.closed += 1


class Provider:
    def __init__(self, name="synthetic-model"):
        self.info = ProviderInfo(provider="managed-gguf", model=name, local=True, streaming=True)
        self.counts, self.prompts = [], []
        self._binding = {
            "provider": "managed-gguf",
            "model": name,
            "profile": "synthetic-profile",
            "manifest_sha256": "b" * 64,
            "resources_url": "http://127.0.0.1:9911",
            "context_tokens": 64,
            "max_new_tokens": 16,
        }

    @property
    def managed_binding(self):
        return dict(self._binding)

    async def measure_prompt(self, messages):
        self.counts.append(messages)
        return PromptTokenCount(32, 64, 16, "a" * 64, "b" * 64)

    async def stream(self, messages):
        self.prompts.append(messages)
        yield "Synthetic reply."


@pytest.fixture
async def runtime_fixture(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    binding = MemoryBinding(session_id=session.session_id, thread_id=session.thread_id, epoch=1)
    # This trusted synthetic validator models another compatible implementation;
    # Core's actual reserved-tag shape/channel/role gate remains in the path.
    store.register_converse_context_validator(
        "compatible-v1",
        lambda sid, tid, epoch, pin: (
            (sid, tid, epoch) == (binding.session_id, binding.thread_id, 1)
            and pin.implementation_id == "compatible-v1"
            and pin.closure_sha256 == "a" * 64
        ),
    )
    first, provider = Adapter("first"), Provider()
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"), provider, memory_adapters={"compatible-v1": first}
    )
    try:
        yield store, binding, runtime, first, provider
    finally:
        await runtime.close()
        store.close()


async def enable(runtime, binding):
    return await runtime.memory_use(
        MemoryUseConfigure(
            **binding.model_dump(),
            expected_generation=runtime.memory.state(binding).use_generation,
            enabled=True,
            implementation_id="compatible-v1",
            selector_ids=["synthetic-note"],
        )
    )


async def turn(runtime, binding, key):
    return await runtime.turn(
        ConverseRequest(
            **binding.model_dump(),
            text="Synthetic question.",
            idempotency_key=key,
            content_scope="public_synthetic",
        )
    )


async def test_compatible_adapter_can_be_replaced_without_dag_or_dialogue_rewrite(runtime_fixture):
    store, binding, runtime, first, provider = runtime_fixture
    await enable(runtime, binding)
    await turn(runtime, binding, "first")
    old = store.read(kinds=["conversation.converse"])[0]
    second = Adapter("second")
    runtime.replace_memory_adapter("compatible-v1", second)
    assert not runtime.memory.state(binding).enabled
    await enable(runtime, binding)
    await turn(runtime, binding, "second")
    assert first.reads == second.reads == 1
    assert "first" not in provider.prompts[-1][1]["content"]
    assert "second" in provider.prompts[-1][1]["content"]
    assert not any(row["role"] == "assistant" for row in provider.prompts[-1])
    assert (
        old.payload["memory_context"]["use_generation"]
        != store.read(kinds=["conversation.converse"])[-1].payload["memory_context"][
            "use_generation"
        ]
    )
    await runtime.close()
    await runtime.close()
    assert first.closed == second.closed == 1
    assert not runtime.memory.state(binding).enabled


async def test_finished_window_reacquires_publication_owner_at_later_opening(runtime_fixture):
    store, binding, runtime, adapter, _ = runtime_fixture
    controller = WindowConverseController(runtime)
    await enable(runtime, binding)
    window = SpeechWindow(
        **binding.model_dump(),
        utterance_id="held",
        window_index=0,
        revision=1,
        text="Synthetic speech.",
        start_ms=0,
        end_ms=1000,
        user_speaking=True,
    )
    source = await controller.ingest(window)
    decision = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=1,
        conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
        agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
    )
    entry = store.append(
        AppendRequest(
            **binding.model_dump(),
            kind="routing.window_decided",
            causes=[source.event_id],
            payload=decision.model_dump(mode="json"),
            idempotency_key="decision",
        ),
        "decisions",
    )
    await controller.process(entry)
    prepared = controller.pending[binding.session_id]
    await asyncio.wait_for(prepared.task, 2)
    assert prepared.chunks and not prepared.published
    original = runtime.journal.append
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_append(request):
        if request.kind == "conversation.converse":
            entered.set()
            await release.wait()
        return await original(request)

    runtime.journal.append = held_append
    await controller.ingest(
        window.model_copy(
            update={"window_index": 1, "user_speaking": False, "start_ms": 1000, "end_ms": 2000}
        )
    )
    flush = asyncio.create_task(controller.flush(prepared))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert runtime.memory.state(binding).pending
        off = await runtime.memory_use(
            MemoryUseConfigure(
                **binding.model_dump(),
                expected_generation=runtime.memory.state(binding).use_generation,
                enabled=False,
            )
        )
        assert off.pending and not off.enabled
        publication = next(iter(prepared.memory_lease.publications.values()))[1]
        assert not publication.done() and not publication.cancelling()
        release.set()
        await asyncio.wait_for(flush, 2)
        await publication
        await asyncio.sleep(0)
        assert not runtime.memory.state(binding).pending
        assert store.read(kinds=["conversation.converse"])
        assert not runtime.playback[binding.session_id].chunks
        before = adapter.reads
        await turn(runtime, binding, "ordinary-after-off")
        assert adapter.reads == before and runtime.enabled
    finally:
        release.set()
        await asyncio.gather(flush, return_exceptions=True)
        runtime.journal.append = original
        await controller.close()


@pytest.mark.parametrize("change", ["inner_provider", "context_limit", "locality_before_count"])
async def test_provider_stamp_is_checked_before_private_count_and_after_validation(
    runtime_fixture, tmp_path, change
):
    store, binding, runtime, adapter, provider = runtime_fixture
    state = ResourceState(tmp_path / "resources.json", profile="configured_hybrid")
    state.save(phase="ready")
    outer = ResourceBoundProvider(provider, state.path, "converse")
    runtime.provider = outer
    other = Provider("replacement-model")
    if change == "inner_provider":
        adapter.on_validate = lambda: setattr(outer, "provider", other)
    elif change == "context_limit":
        adapter.on_validate = lambda: provider._binding.update(context_tokens=128)
    else:
        adapter.on_recall = lambda: setattr(
            provider,
            "info",
            ProviderInfo(provider="synthetic-cloud", model="changed-model", local=False),
        )
    try:
        await enable(runtime, binding)
        result = await turn(runtime, binding, change)
        assert not result["text"]
        assert provider.prompts == other.prompts == []
        assert not store.read(kinds=["conversation.converse"])
        if change == "locality_before_count":
            assert provider.counts == []
        else:
            assert len(provider.counts) == 1 and other.counts == []
    finally:
        state.close()
