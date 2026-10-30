"""Actual typed/live prompts over native local and HTTP Session; synthetic tokenizer only."""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.prompt_budget import PromptTokenCount, request_digest
from services.common.auth import Auth
from services.converse.context_budget import (
    ContextBudgetBuilder,
    ContextBudgetOverflow,
    ContextBudgetStale,
    ContextBudgetUnavailable,
    ContextHistoryUnavailable,
)
from services.converse.delivery_context import PREFIX
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.api import create_app
from services.session.store import LocalJournal, Store


def synthetic_tokens(messages):
    # Deliberately includes role/template/UTF-8 overhead. This is a deterministic
    # fixture tokenizer, never a production count estimator or native model proof.
    return 11 + sum(7 + len(m["role"]) + len(m["content"].encode()) for m in messages)


class CountedProvider:
    info = ProviderInfo(provider="synthetic", model="whole-prompt-fixture", local=True)

    def __init__(self):
        self.context = 32768
        self.counts, self.prompts = [], []
        self.binding = "b" * 64
        self.before_count = None
        self.last = None

    async def measure_prompt(self, messages):
        self.counts.append(copy.deepcopy(messages))
        if self.before_count:
            await self.before_count(messages)
        count = PromptTokenCount(
            synthetic_tokens(messages), self.context, 16, request_digest(messages), self.binding
        )
        self.last = (copy.deepcopy(messages), count)
        return count

    async def stream(self, messages):
        assert self.last[0] == messages and self.last[1].fits
        self.prompts.append(copy.deepcopy(messages))
        yield "Synthetic first sentence. "
        yield "Synthetic second sentence."


@pytest.fixture(params=["local", "http"])
async def env(tmp_path, request):
    store = Store(tmp_path / "synthetic.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    auth = Auth({role: role[0] * 32 for role in ("owner", "converse")})
    journal = (
        LocalJournal(store, "converse")
        if request.param == "local"
        else JournalClient(
            "http://fixture",
            auth.tokens["converse"],
            transport=httpx.ASGITransport(app=create_app(store, auth)),
        )
    )
    provider = CountedProvider()
    runtime = ConverseRuntime(
        journal,
        provider,
        personality="Public persona.",
        personality_scope="public_synthetic",
        context_budgeted=True,
    )
    controller = WindowConverseController(runtime)
    result = SimpleNamespace(
        store=store,
        session=session,
        journal=journal,
        provider=provider,
        runtime=runtime,
        controller=controller,
        key=0,
    )
    yield result
    await controller.close()
    await runtime.close()
    if isinstance(journal, JournalClient):
        await journal.close()
    store.close()


def write(env, kind="conversation.user", text="Public prior.", **payload):
    env.key += 1
    return env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind=kind,
            payload={
                "text": text,
                "revision": 1,
                "final": True,
                "content_scope": "public_synthetic",
                **payload,
            },
            idempotency_key=f"public-{env.key}",
        ),
        "owner" if kind == "conversation.user" else "converse",
    )


def builder(env, source, **kwargs):
    return ContextBudgetBuilder(
        env.journal,
        source,
        env.provider,
        personality=env.runtime.personality,
        runtime_id=env.runtime._runtime_id,
        current=lambda: True,
        **kwargs,
    )


async def typed(env, text="Public current.", key="current"):
    return await env.runtime.turn(
        ConverseRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            text=text,
            content_scope="public_synthetic",
            idempotency_key=key,
        )
    )


async def live(env):
    s = env.session
    source = await env.controller.ingest(
        SpeechWindow(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            utterance_id="public-current-utterance",
            window_index=0,
            text="Public current.",
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
    )
    decision = env.store.append(
        AppendRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            kind="routing.window_decided",
            causes=[source.event_id],
            payload=WindowDecision(
                source_event_id=source.event_id,
                source_sequence=source.sequence,
                source_revision=1,
                conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
                agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
            ).model_dump(mode="json"),
            idempotency_key="public-decision",
        ),
        "decisions",
    )
    await env.controller.process(decision)
    await asyncio.wait_for(asyncio.gather(*(p.task for p in env.controller.pending.values())), 5)
    await env.controller.tick()


@pytest.mark.parametrize("mode", ["typed", "live"])
async def test_actual_prompt_retains_over_eight_messages_and_noise_cannot_evict(env, mode):
    texts = []
    for index in range(25):
        text = f"Public item {index}."
        texts.append(text)
        write(env, text=text)
    for index in range(260):
        write(env, "converse.window_state", phase="synthetic", index=index)
    await (typed(env) if mode == "typed" else live(env))
    prompt = env.provider.prompts[-1]
    assert [m["content"] for m in prompt[1:-1]] == texts
    assert prompt[-1]["content"] == "Public current."
    assert len(prompt) == 27
    assert env.provider.counts[-1] == prompt


async def test_multi_sentence_chunks_remain_separate_but_all_fit_and_qualified(env):
    for index in range(15):
        write(
            env,
            "conversation.converse",
            text=f"Public sentence {index}.",
            runtime_id="fixture-runtime",
            generation_epoch=1,
            chunk_id=f"chunk-{index}",
        )
    await typed(env)
    prompt = env.provider.prompts[-1]
    assistants = [m for m in prompt if m["role"] == "assistant"]
    assert len(assistants) == 15
    block = next(m["content"] for m in prompt if m["content"].startswith(PREFIX))
    rows = json.loads(block[len(PREFIX) :])["history"]
    assert [r["history_index"] for r in rows] == list(range(15))
    assert all(r["latest_status"] == "delivery_unknown" for r in rows)


async def test_more_than_one_native_page_is_used_when_context_allows(env):
    texts = [f"Public {i}." for i in range(205)]
    for text in texts:
        write(env, text=text)
    source = write(env, text="Public current.")
    b = builder(env, source)
    projection = await b.build()
    assert len(b.pages) == 3 and b.exhausted
    assert [m["content"] for m in projection.messages[1:-1]] == texts


async def test_exact_boundary_and_one_token_overflow_drop_only_oldest_whole_message(env):
    for text in ["Oldest public.", "Recent public.", "Newest public."]:
        write(env, text=text)
    source = write(env, text="Current 你好🙂.")
    full = await builder(env, source).build()
    cost = synthetic_tokens(full.messages)
    env.provider.context = cost + 16
    assert (await builder(env, source).build()).messages == full.messages
    env.provider.context -= 1
    selected = await builder(env, source).build()
    assert selected.messages == [full.messages[0], *full.messages[2:]]
    assert selected.measurement.fits
    assert all(m["content"] != "Oldest public." for m in selected.messages)


async def test_newest_oversized_history_is_not_skipped_for_older_fill(env):
    write(env, text="Small older.")
    write(env, text="Newest " + ("x" * 3000))
    source = write(env, text="Current.")
    env.provider.context = 200
    selected = await builder(env, source).build()
    assert selected.messages == [
        {"role": "system", "content": "Public persona."},
        {"role": "user", "content": "Current."},
    ]


async def test_protected_current_input_refuses_without_any_generation(env):
    env.provider.context = 128
    result = await typed(env, text="你好🙂 " * 200)
    assert not result["text"] and not env.provider.prompts
    assert any(e.get("error") == "ContextBudgetOverflow" for e in result["events"])


async def test_missing_measurement_never_falls_back_to_fixed_history(env):
    env.provider.measure_prompt = None
    result = await typed(env)
    assert not env.provider.prompts
    assert any(e.get("error") == "ContextBudgetUnavailable" for e in result["events"])


async def test_binding_replacement_between_candidates_refuses(env):
    write(env)
    source = write(env, text="Current.")

    async def change(messages):
        if len(env.provider.counts) == 2:
            env.provider.binding = "c" * 64

    env.provider.before_count = change
    with pytest.raises(ContextBudgetStale):
        await builder(env, source).build()
    assert not env.provider.prompts


async def test_reset_during_count_refuses(env):
    source = write(env)

    async def reset(messages):
        env.store.reset(env.session.session_id)

    env.provider.before_count = reset
    with pytest.raises(ContextBudgetStale):
        await builder(env, source).build()


async def test_cancellation_during_count_retains_no_background_builder(env):
    source = write(env)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def wait(messages):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    env.provider.before_count = wait
    task = asyncio.create_task(builder(env, source).build())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and not env.provider.prompts


async def test_final_count_refreshes_selected_request_after_probe_fallback(env):
    write(env)
    source = write(env, text="Current.")
    b = builder(env, source)
    ordinary = await b.build()
    env.provider.context = 200
    # Changing capacity is itself stale, so use a same-binding oversized closure.
    env.provider.context = 32768
    with pytest.raises(ContextBudgetOverflow):
        await b.build(recall={"role": "system", "content": "r" * 40000})
    assert env.provider.last[0] != ordinary.messages
    final = await b.finalize(ordinary)
    assert env.provider.last[0] == final.messages == ordinary.messages


async def test_explicit_work_limit_refuses_instead_of_returning_partial_history(env):
    write(env)
    source = write(env)
    b = builder(env, source)
    b.max_measurements = 1
    with pytest.raises(ContextBudgetUnavailable):
        await b.build()
    b = builder(env, source)
    b.max_native_bytes = 1
    with pytest.raises(ContextHistoryUnavailable):
        await b.build()


async def test_complete_revision_chain_and_qualifications_before_selection(env):
    base = write(env, text="Old public draft.")
    for revision in range(2, 108):
        env.key += 1
        env.store.append(
            AppendRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                kind="conversation.revision",
                causes=[base.event_id],
                payload={
                    "target_event_id": base.event_id,
                    "revision": revision,
                    "text": f"Public revision {revision}.",
                    "content_scope": "public_synthetic",
                },
                idempotency_key=f"revision-{revision}",
            ),
            "owner",
        )
    source = write(env, text="Current.")
    b = builder(env, source)
    prompt = (await b.build()).messages
    assert prompt[1]["content"] == "Public revision 107."
    assert len(b.qualifiers) == 106 and "Old public draft." not in json.dumps(prompt)


async def test_full_delivery_block_counts_instead_of_truncating_to_byte_cap(env):
    for index in range(40):
        write(
            env,
            "conversation.converse",
            text=f"Public {index}.",
            runtime_id="runtime",
            generation_epoch=1,
            chunk_id=f"chunk-{index}",
        )
    source = write(env, text="Current.")
    all_context = await builder(env, source).build()
    block = all_context.messages[1]["content"]
    assert len(block.encode()) > 8192
    assert len(json.loads(block[len(PREFIX) :])["history"]) == 40
    env.provider.context = 2500
    reduced = await builder(env, source).build()
    rows = json.loads(reduced.messages[1]["content"][len(PREFIX) :])["history"]
    assistants = [m for m in reduced.messages if m["role"] == "assistant"]
    assert len(rows) == len(assistants) < 40
    assert [r["history_index"] for r in rows] == list(range(len(rows)))
    assert reduced.measurement.fits
