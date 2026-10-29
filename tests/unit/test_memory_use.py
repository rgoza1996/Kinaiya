"""Synthetic recall ownership, exact prompt preparation and local controls."""

import asyncio
from types import SimpleNamespace

import pytest

from packages.contracts.memory import MemoryBinding
from packages.contracts.memory_context import MemoryContextPin, MemoryUseConfigure
from packages.contracts.models import AppendRequest, ProviderInfo
from packages.providers.prompt_budget import PromptTokenCount
from services.converse.context_projection import ContextProjection
from services.converse.memory import MAX_GENERATION, MemoryUse

BINDING = MemoryBinding(session_id="synthetic-session", thread_id="synthetic-thread", epoch=1)


class Adapter:
    def __init__(self):
        self.reads = 0
        self.checks = 0
        self.valid = True

    async def recall(self, binding, selectors, generation):
        self.reads += 1
        return SimpleNamespace(
            binding=binding,
            pin=MemoryContextPin(
                schema_version="1.0",
                implementation_id="fixture-v1",
                use_generation=generation,
                namespace_revision=2,
                closure_sha256="a" * 64,
                selector_ids=list(selectors),
            ),
            message={"role": "system", "content": 'Untrusted notes: [{"body":"synthetic note"}]'},
        )

    async def validate(self, snapshot):
        self.checks += 1
        return self.valid


class Provider:
    def __init__(self, *, local=True, fits=True):
        self.info = ProviderInfo(provider="synthetic", model="fixture", local=local)
        self.counted = []
        self.fits = fits

    async def measure_prompt(self, messages):
        self.counted.append(messages)
        return PromptTokenCount(32 if self.fits else 60, 64, 16, "b" * 64, "c" * 64)


def compile_context(pin):
    return ContextProjection(
        [
            {"role": "system", "content": "Synthetic personality."},
            *([{"role": "assistant", "content": "Synthetic tagged history."}] if pin else []),
            {"role": "user", "content": "Synthetic current input."},
        ],
        ["public_synthetic"],
        consumed_memory=pin is not None,
    )


def configure(use, *, enabled=True, binding=BINDING, expected=None, selectors=("node",)):
    return use.configure(
        MemoryUseConfigure(
            **binding.model_dump(),
            expected_generation=(
                use.state(binding).use_generation if expected is None else expected
            ),
            enabled=enabled,
            implementation_id="fixture-v1" if enabled else None,
            selector_ids=list(selectors) if enabled else [],
        )
    )


def registry(adapter=None):
    return MemoryUse("runtime", {"fixture-v1": adapter or Adapter()}, on_revoke=lambda _: None)


async def prepare(use, provider=None, cancel=lambda: None, attach=lambda _: None):
    return await use.prepare(
        BINDING, provider or Provider(), compile_context, cancel, lambda: True, attach
    )


async def test_off_and_cloud_and_empty_selection_do_not_read_or_count():
    adapter, provider = Adapter(), Provider()
    use = registry(adapter)
    ordinary, lease = await prepare(use, provider)
    assert lease is None and ordinary == compile_context(None)
    configure(use)
    assert (await prepare(use, Provider(local=False)))[1] is None
    configure(use, selectors=())
    assert (await prepare(use, provider))[1] is None
    assert adapter.reads == adapter.checks == 0 and provider.counted == []


async def test_exact_final_prompt_with_persona_history_and_notes_is_measured_then_validated():
    adapter, provider = Adapter(), Provider()
    use = registry(adapter)
    configure(use)
    projection, lease = await prepare(use, provider)
    assert projection.messages == provider.counted[0]
    assert [row["role"] for row in projection.messages] == ["system", "system", "assistant", "user"]
    assert projection.consumed_memory and projection.consumed_scopes == ["local_only"]
    assert adapter.reads == adapter.checks == 1 and use.state(BINDING).pending
    lease.finish()
    assert not use.state(BINDING).pending


@pytest.mark.parametrize("mode", ["too_large", "unsupported", "invalidated", "malformed"])
async def test_omission_returns_entire_ordinary_projection_before_any_generation(mode):
    adapter, provider = Adapter(), Provider(fits=mode != "too_large")
    if mode == "unsupported":
        provider.measure_prompt = None
    if mode == "invalidated":
        adapter.valid = False
    if mode == "malformed":

        async def malformed(*_):
            return object()

        adapter.recall = malformed
    use = registry(adapter)
    configure(use)
    projection, lease = await prepare(use, provider)
    assert lease is None and projection == compile_context(None)
    assert not use.state(BINDING).pending
    assert use.state(BINDING).notice == "context_omitted"


async def test_off_during_held_count_revokes_before_cleanup_and_drops_snapshot():
    entered, release = asyncio.Event(), asyncio.Event()
    provider = Provider()
    original = provider.measure_prompt

    async def held(messages):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()
        return await original(messages)

    provider.measure_prompt = held
    use = registry()
    configure(use)
    cancelled = []
    task = asyncio.create_task(prepare(use, provider, lambda: cancelled.append(True)))
    await entered.wait()
    state = configure(use, enabled=False)
    assert state.pending and not state.enabled and cancelled == [True]
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert not use.state(BINDING).pending


async def test_late_publication_is_retained_after_parent_cancellation_and_off():
    use = registry()
    configure(use)
    _, lease = await prepare(use)
    entered, release = asyncio.Event(), asyncio.Event()

    class Journal:
        async def append(self, request):
            entered.set()
            await release.wait()
            return request

    request = AppendRequest(
        **BINDING.model_dump(),
        kind="conversation.converse",
        payload={"text": "Synthetic."},
        idempotency_key="synthetic-publication",
    )
    caller = asyncio.create_task(lease.publish(Journal(), request))
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    configure(use, enabled=False)
    lease.finish()
    assert use.state(BINDING).pending
    publication = lease.publications[request.idempotency_key][1]
    assert not publication.done() and not publication.cancelling()
    release.set()
    assert await publication == request
    await asyncio.sleep(0)
    assert not use.state(BINDING).pending


async def test_unknown_publication_keeps_pending_and_refuses_new_memory_but_ordinary_works():
    use = registry()
    configure(use)
    _, lease = await prepare(use)

    class Journal:
        async def append(self, _):
            raise OSError("Synthetic unknown acknowledgment")

    request = AppendRequest(
        **BINDING.model_dump(),
        kind="conversation.converse",
        payload={},
        idempotency_key="unknown",
    )
    with pytest.raises(OSError):
        await lease.publish(Journal(), request)
    lease.finish()
    await asyncio.sleep(0)
    assert use.state(BINDING).pending
    configure(use)
    projection, newer = await prepare(use)
    assert newer is None and projection == compile_context(None)


def test_cas_bounded_scope_eviction_never_reuses_generation_and_off_at_exhaustion():
    use = registry()
    first = configure(use)
    with pytest.raises(ValueError):
        configure(use, expected=0)
    configure(use, enabled=False)
    use.capacity = 1
    second_binding = BINDING.model_copy(update={"session_id": "second"})
    second = configure(use, binding=second_binding)
    assert second.use_generation > first.use_generation
    assert use.state(BINDING).use_generation == 0
    use.generation = MAX_GENERATION
    old_choice = use.choices[
        (second_binding.session_id, second_binding.thread_id, second_binding.epoch)
    ]
    off = configure(use, enabled=False, binding=second_binding)
    assert not off.enabled and not use.current(old_choice)
    with pytest.raises(ValueError):
        configure(use, binding=second_binding)


def test_adapter_replacement_revokes_existing_choice_and_starts_disabled():
    use = registry()
    configure(use)
    old = next(iter(use.choices.values()))
    use.replace("fixture-v1", Adapter())
    assert not use.current(old) and not use.state(BINDING).enabled
