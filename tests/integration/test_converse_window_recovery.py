"""Native Converse recovery; synthetic text/inference and no physical audio."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import (
    AppendRequest,
    SessionRequest,
    SpeechWindow,
    Task,
    WindowDecision,
)
from packages.providers.text import (
    CompatibleTextProvider,
    MetaResponsesProvider,
    ProviderError,
    ScriptedProvider,
)
from services.common.auth import Auth
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.decisions.window_routing import WindowAgentDispatcher
from services.session.api import create_app
from services.session.store import LocalJournal, Store


class StreamFixture:
    info = ScriptedProvider.info

    def __init__(self):
        self.calls = 0
        self.deltas = 0
        self.closed = asyncio.Event()
        self.between = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.error = False

    async def stream(self, messages):
        self.calls += 1
        try:
            self.deltas += 1
            yield "Public synthetic first sentence. "
            self.between.set()
            if self.block:
                await self.release.wait()
            if self.error:
                raise ProviderError("Synthetic inference failure")
            self.deltas += 1
            yield "Public synthetic second sentence."
        finally:
            self.closed.set()


class Faults:
    def __init__(self):
        self.target = None
        self.when = "before"
        self.remaining = 0
        self.requests = []
        self.entered = asyncio.Event()
        self.release = None

    async def run(self, target, body, operation):
        if target != self.target or target is None:
            return await operation()
        self.requests.append(body)
        if not self.remaining:
            return await operation()
        self.remaining -= 1
        result = await operation() if self.when == "after" else None
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        close = getattr(result, "aclose", None)
        if callable(close):
            await close()
        raise httpx.ReadTimeout("Synthetic native journal outage")


def append_target(body):
    if body["kind"] == "conversation.converse":
        return "reply"
    if body["kind"] == "converse.window_state":
        return body["payload"]["phase"]
    if body["kind"] == "converse.safety":
        return body["payload"].get("reason")
    return None


@pytest.fixture(params=["local", "http"])
async def env(tmp_path, request):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    faults = Faults()
    if request.param == "local":

        class FaultJournal(LocalJournal):
            async def append(self, value):
                operation = super().append
                return await faults.run(
                    append_target(value.model_dump()),
                    value.model_dump(mode="json"),
                    lambda: operation(value),
                )

            async def resolve(self, event_id):
                operation = super().resolve
                return await faults.run("resolve", None, lambda: operation(event_id))

            async def read(self, **kwargs):
                operation = super().read
                return await faults.run("read", None, lambda: operation(**kwargs))

        journal = FaultJournal(store, "converse")
    else:
        auth = Auth({"converse": "synthetic-converse-credential-000000000"})

        class FaultTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.actual = httpx.ASGITransport(app=create_app(store, auth))

            async def handle_async_request(self, value):
                body = json.loads(value.content) if value.method == "POST" else None
                target = append_target(body) if body is not None else None
                if value.method == "GET" and value.url.params.get("resolve") == "true":
                    target = "resolve"
                if value.method == "GET" and value.url.path == "/v1/transcript":
                    target = "read"
                return await faults.run(
                    target, body, lambda: self.actual.handle_async_request(value)
                )

        journal = JournalClient(
            "http://fixture", auth.tokens["converse"], transport=FaultTransport()
        )
    provider = StreamFixture()
    runtime = ConverseRuntime(journal, provider)
    controller = WindowConverseController(runtime)
    result = SimpleNamespace(
        store=store,
        session=session,
        faults=faults,
        journal=journal,
        provider=provider,
        runtime=runtime,
        controller=controller,
    )
    try:
        yield result
    finally:
        if faults.release is not None:
            faults.release.set()
        provider.release.set()
        await controller.close()
        if isinstance(journal, JournalClient):
            await journal.close()
        store.close()


async def decision(
    env,
    *,
    name="request",
    text="Public synthetic request.",
    conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
    route="IGNORE_FOR_IMMEDIATE_ROUTING",
    index=0,
    scope="public_synthetic",
    user_speaking=False,
):
    s = env.session
    source = await env.controller.ingest(
        SpeechWindow(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            utterance_id=name,
            window_index=index,
            text=text,
            start_ms=0,
            end_ms=1000 + index,
            user_speaking=user_speaking,
            capture_ended=not user_speaking,
            content_scope=scope,
        )
    )
    value = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=source.payload["revision"],
        conversation=conversation,
        agent_route=route,
    )
    event = env.store.append(
        AppendRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            kind="routing.window_decided",
            payload=value.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key="decision:" + source.event_id,
        ),
        "decisions",
    )
    return source, event


async def settle(env, count=5):
    for _ in range(count):
        await env.controller.tick()
        tasks = [p.task for p in env.controller.pending.values()]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 4)


class AgentFixture:
    def __init__(self):
        self.tasks = []

    async def status(self):
        return self.tasks

    async def submit(self, request, *, actor):
        assert actor == "decisions"
        task = Task(
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            objective=request.objective,
            idempotency_key=request.idempotency_key,
        )
        self.tasks.append(task)
        return task


async def test_pre_generation_read_recovers_without_new_window_and_agent_dispatch_stays_independent(
    env,
):
    env.faults.target, env.faults.remaining = "resolve", 1
    source, event = await decision(env, route="QUEUE_UNTIL_TASK_DONE")
    agent = AgentFixture()
    dispatcher = WindowAgentDispatcher(LocalJournal(env.store, "decisions"), agent)
    await dispatcher.process(event)
    await settle(env)
    await WindowAgentDispatcher(LocalJournal(env.store, "decisions"), agent).process(event)
    assert len(agent.tasks) == 1
    assert env.provider.calls == 1 and env.provider.deltas == 2
    replies = env.store.read(kinds=["conversation.converse"])
    assert len(replies) == 2
    assert {e.payload["window_source_id"] for e in replies} == {source.event_id}
    assert all(e.payload["delivery"] == "generated" for e in replies)


@pytest.mark.parametrize("target", ["preparing", "started", "reply"])
@pytest.mark.parametrize("when", ["before", "after"])
async def test_uncertain_native_appends_recover_entire_same_inference_with_exact_request_identity(
    env, target, when
):
    env.faults.target, env.faults.when, env.faults.remaining = target, when, 1
    await decision(env)
    await settle(env)
    assert env.provider.calls == 1 and env.provider.deltas == 2
    assert env.provider.closed.is_set()
    replies = env.store.read(kinds=["conversation.converse"])
    assert [e.payload["text"] for e in replies] == [
        "Public synthetic first sentence.",
        "Public synthetic second sentence.",
    ]
    assert len({e.payload["chunk_id"] for e in replies}) == 2
    # The first unconfirmed append is retried/reconciled verbatim. A start
    # receipt may instead reconcile through its native indexed lookup.
    if target != "started" or when == "before":
        assert env.faults.requests[0] == env.faults.requests[1]
    assert len({e.payload["generation_epoch"] for e in replies}) == 1
    assert (
        len(
            [
                e
                for e in env.store.read(kinds=["converse.window_state"])
                if e.payload["phase"] == "started"
            ]
        )
        == 1
    )
    for entry in replies:
        await env.runtime.validate_speech(entry.event_id, entry.payload["generation_epoch"])


@pytest.mark.parametrize(
    "target,when",
    [
        ("preparing", "before"),
        ("preparing", "after"),
        ("started", "before"),
        ("reply", "before"),
        ("reply", "after"),
    ],
)
async def test_exhausted_native_outage_fails_closed_and_healing_cannot_resume_buffered_text(
    env, target, when
):
    env.faults.target, env.faults.when, env.faults.remaining = target, when, 100
    await decision(env)
    await settle(env)
    calls = env.provider.calls
    replies = env.store.read(kinds=["conversation.converse"])
    assert calls == (0 if target == "preparing" else 1)
    assert not env.runtime._active
    errors = [
        e for e in env.store.read(kinds=["converse.window_state"]) if e.payload["phase"] == "error"
    ]
    assert errors and errors[-1].payload["error_source"] == "journal"
    assert errors[-1].payload["delivery"] == "delivery_unknown"
    env.faults.remaining = 0
    await settle(env)
    assert env.provider.calls == calls
    assert env.store.read(kinds=["conversation.converse"]) == replies
    for entry in replies:
        with pytest.raises(ValueError):
            await env.runtime.validate_speech(entry.event_id, entry.payload["generation_epoch"])


async def test_native_start_receipt_recovers_despite_permanently_lost_start_acknowledgment(env):
    env.faults.target, env.faults.when, env.faults.remaining = "started", "after", 100
    await decision(env)
    await settle(env)
    assert env.provider.calls == 1 and env.provider.deltas == 2
    assert len(env.faults.requests) == 1
    assert len(env.store.read(kinds=["conversation.converse"])) == 2
    assert not [
        e for e in env.store.read(kinds=["converse.window_state"]) if e.payload["phase"] == "error"
    ]


async def test_recovery_retains_cursor_when_preparation_read_is_still_unavailable(env):
    env.faults.target, env.faults.remaining = "resolve", 100
    source, event = await decision(env)
    await env.controller.tick()
    assert env.controller.after == source.sequence < event.sequence
    assert not env.provider.calls
    env.faults.remaining = 0
    await settle(env)
    assert env.provider.calls == 1 and len(env.store.read(kinds=["conversation.converse"])) == 2


async def test_fresh_same_text_window_is_not_lost_behind_an_abandoned_preparation(env):
    env.faults.target, env.faults.remaining = "preparing", 100
    await decision(env)
    await env.controller.tick()
    await asyncio.gather(*(p.task for p in env.controller.pending.values()), return_exceptions=True)
    env.faults.remaining = 0
    source, _ = await decision(env, index=1)
    await settle(env)
    replies = env.store.read(kinds=["conversation.converse"])
    assert env.provider.calls == 1 and len(replies) == 2
    assert {e.payload["window_source_id"] for e in replies} == {source.event_id}


@pytest.mark.parametrize(
    "change", ["interrupt", "owner_off_on", "reset", "new_intent", "correction", "scope"]
)
@pytest.mark.parametrize("when", ["before", "after"])
async def test_changed_authority_or_intent_fences_uncertain_reply_before_retry(env, change, when):
    env.faults.target, env.faults.when, env.faults.remaining = "reply", when, 1
    env.faults.release = asyncio.Event()
    source, _ = await decision(env)
    await env.controller.tick()
    await asyncio.wait_for(env.faults.entered.wait(), 1)
    if change == "interrupt":
        await env.runtime.interrupt(env.session.session_id)
    elif change == "owner_off_on":
        await env.runtime.set_enabled(False, owner=True)
        await env.runtime.set_enabled(True, owner=True)
    elif change == "reset":
        env.store.reset(env.session.session_id)
    elif change == "new_intent":
        await decision(
            env,
            name="new-intent",
            text="Public synthetic unfinished correction",
            conversation="WAIT_FOR_NEXT_WINDOW",
        )
    elif change == "correction":
        await decision(env, index=1, text="Public synthetic replacement instruction")
    elif change == "scope":
        await decision(env, index=1, scope="local_only")
    env.faults.release.set()
    old = env.controller.pending.get(env.session.session_id)
    if old is not None:
        await asyncio.wait_for(asyncio.gather(old.task, return_exceptions=True), 2)
    # Check the old operation directly; processing the new valid intent would
    # legitimately begin another inference and is a separate action.
    if old is not None:
        for _ in range(3):
            await env.controller.flush(old)
    assert env.provider.calls == 1 and env.provider.deltas == 1
    assert env.provider.closed.is_set()
    assert len(env.faults.requests) == 1
    for entry in env.store.read(kinds=["conversation.converse"]):
        assert entry.payload["window_source_id"] == source.event_id
        assert entry.payload["delivery"] == "generated"
        with pytest.raises(ValueError):
            await env.runtime.validate_speech(entry.event_id, entry.payload["generation_epoch"])


async def test_ignored_backchannel_preserves_blocked_explanation_and_both_chunks(env):
    env.provider.block = True
    await decision(env)
    await env.controller.tick()
    await asyncio.wait_for(env.provider.between.wait(), 1)
    generation = (await env.runtime._epoch(env.session.session_id)).generation
    _, ignored = await decision(env, name="backchannel", text="Mm-hmm.", conversation="IGNORE")
    await env.controller.process(ignored)
    env.provider.release.set()
    await settle(env)
    replies = env.store.read(kinds=["conversation.converse"])
    assert len(replies) == 2 and env.provider.calls == 1
    assert (await env.runtime._epoch(env.session.session_id)).generation == generation
    for entry in replies:
        await env.runtime.validate_speech(entry.event_id, generation)


async def test_admitted_healthy_explanation_can_continue_past_initial_source_age_limit(
    env, monkeypatch
):
    env.provider.block = True
    await decision(env)
    await env.controller.tick()
    await asyncio.wait_for(env.provider.between.wait(), 1)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=90)

    monkeypatch.setattr("services.converse.windows.datetime", Later)
    env.provider.release.set()
    await settle(env)
    assert env.provider.calls == 1 and env.provider.deltas == 2
    assert len(env.store.read(kinds=["conversation.converse"])) == 2


async def test_generation_interrupt_during_final_pre_provider_read_prevents_stale_inference(env):
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.journal.latest_window
    gated = False

    async def suspended(*args, **kwargs):
        nonlocal gated
        value = await original(*args, **kwargs)
        preparing = any(
            e.payload["phase"] == "preparing"
            for e in env.store.read(kinds=["converse.window_state"])
        )
        if preparing and not gated:
            gated = True
            entered.set()
            await release.wait()
        return value

    env.journal.latest_window = suspended
    await decision(env)
    await env.controller.tick()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await env.runtime.interrupt(env.session.session_id)
        release.set()
        await settle(env)
        assert not env.provider.calls and not env.store.read(kinds=["conversation.converse"])
    finally:
        release.set()


async def test_provider_error_is_not_retried_as_native_outage(env):
    env.provider.error = True
    await decision(env)
    await settle(env)
    assert env.provider.calls == 1
    assert env.controller.last_error == "ProviderError"
    errors = [
        e for e in env.store.read(kinds=["converse.window_state"]) if e.payload["phase"] == "error"
    ]
    assert errors[-1].payload["error_source"] == "generation"


async def test_default_omitted_scope_is_normalized_without_widening_disclosure(env):
    s = env.session
    # The authenticated append endpoint accepts omitted SpeechWindow defaults
    # and persists the original payload, rather than the normalized model.
    source = await env.journal.append(
        AppendRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            kind="conversation.window",
            payload={
                "session_id": s.session_id,
                "thread_id": s.thread_id,
                "epoch": s.epoch,
                "utterance_id": "omitted-scope",
                "window_index": 0,
                "revision": 1,
                "text": "Public synthetic request.",
                "start_ms": 0,
                "end_ms": 1000,
                "user_speaking": False,
                "capture_ended": True,
            },
            idempotency_key="omitted-scope",
        )
    )
    assert "content_scope" not in source.payload
    value = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=1,
        conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
        agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
    )
    env.store.append(
        AppendRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            kind="routing.window_decided",
            payload=value.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key="decision:" + source.event_id,
        ),
        "decisions",
    )
    await settle(env)
    replies = env.store.read(kinds=["conversation.converse"])
    assert len(replies) == 2 and {e.payload["content_scope"] for e in replies} == {"local_only"}


async def test_plain_conforming_async_iterator_without_aclose_remains_supported(env):
    class Iterator:
        def __init__(self):
            self.values = iter(
                ["Public synthetic first sentence. ", "Public synthetic second sentence."]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.values)
            except StopIteration:
                raise StopAsyncIteration from None

    class Provider:
        info = ScriptedProvider.info
        calls = 0

        def stream(self, messages):
            self.calls += 1
            return Iterator()

    provider = Provider()
    env.runtime.provider = provider
    await decision(env)
    await settle(env)
    assert provider.calls == 1 and len(env.store.read(kinds=["conversation.converse"])) == 2
    assert env.controller.last_error is None


class SlowCloseIterator:
    """A provider owns cleanup that outlives the controller's bounded wait."""

    def __init__(self, release):
        self.release = release
        self.holding = asyncio.Event()
        self.closing = asyncio.Event()
        self.closed = False
        self.cleanup_cancelled = False
        self.first = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.first:
            self.first = False
            return "Public synthetic first sentence. "
        self.holding.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closing.set()
        try:
            await self.release.wait()
            self.closed = True
        except asyncio.CancelledError:
            self.cleanup_cancelled = True
            raise


class SlowCloseProvider:
    info = ScriptedProvider.info

    def __init__(self, slow_calls):
        self.slow_calls = slow_calls
        self.calls = 0
        self.iterators = []
        self.release = asyncio.Event()

    def stream(self, messages):
        self.calls += 1
        if self.calls <= self.slow_calls:
            iterator = SlowCloseIterator(self.release)
            self.iterators.append(iterator)
            return iterator
        return ScriptedProvider(["Public synthetic replacement reply."]).stream(messages)


async def test_cleanup_survives_repeated_ticks_off_close_and_defers_new_decision_cursor(env):
    provider = SlowCloseProvider(1)
    env.runtime.provider = provider
    env.controller._provider_cleanup_timeout_s = 0.03
    await decision(env)
    await env.controller.tick()
    old = env.controller.pending[env.session.session_id]
    try:
        while not provider.iterators:
            await asyncio.sleep(0)
        iterator = provider.iterators[0]
        await asyncio.wait_for(iterator.holding.wait(), 1)
        env.controller._abandon(old)
        await asyncio.wait_for(iterator.closing.wait(), 1)
        for _ in range(3):
            await env.controller.tick()
        await env.runtime.set_enabled(False, owner=True)
        await env.runtime.set_enabled(True, owner=True)
        await env.controller.close()
        assert old.task.done() and not old.cleanup.done()
        assert env.controller.pending[env.session.session_id] is old
        assert not iterator.closed and not iterator.cleanup_cancelled
        source, event = await decision(
            env, name="new-intent", text="Public synthetic next request."
        )
        await env.controller.tick()
        assert env.controller.after < event.sequence
        assert provider.calls == 1 and len(env.controller.pending) == 1
        provider.release.set()
        await asyncio.wait_for(old.cleanup, 1)
        await settle(env)
        assert iterator.closed and not iterator.cleanup_cancelled
        assert provider.calls == 2
        assert any(
            e.payload["window_source_id"] == source.event_id
            for e in env.store.read(kinds=["conversation.converse"])
        )
    finally:
        provider.release.set()
        if old.cleanup is not None:
            await asyncio.gather(old.cleanup, return_exceptions=True)


async def test_32_unconfirmed_provider_cleanups_hold_slots_and_33rd_durable_decision(env):
    provider = SlowCloseProvider(32)
    env.runtime.provider = provider
    env.controller._provider_cleanup_timeout_s = 0.03
    original_session = env.session
    try:
        for index in range(32):
            env.session = env.store.create_session(SessionRequest(channel="converse"))
            await decision(env, name=f"request-{index}")
            await env.controller.tick()
            while len(provider.iterators) <= index:
                await asyncio.sleep(0)
            await asyncio.wait_for(provider.iterators[index].holding.wait(), 1)
        await env.controller.close()
        owners = dict(env.controller.pending)
        assert len(owners) == 32
        await asyncio.wait_for(
            asyncio.gather(*(p.task for p in owners.values()), return_exceptions=True), 0.5
        )
        assert all(p.task.done() and not p.cleanup.done() for p in owners.values())
        env.session = original_session
        source, event = await decision(env, name="request-33")
        for _ in range(3):
            await env.controller.tick()
        assert env.controller.after == source.sequence < event.sequence
        assert env.controller.pending == owners and provider.calls == 32
        assert all(not iterator.cleanup_cancelled for iterator in provider.iterators)
        provider.release.set()
        await asyncio.gather(*(p.cleanup for p in owners.values()))
        await settle(env)
        assert provider.calls == 33 and env.controller.after >= event.sequence
        assert all(iterator.closed for iterator in provider.iterators)
        assert len(env.controller.pending) == 1
        assert any(
            e.payload["window_source_id"] == source.event_id
            for e in env.store.read(kinds=["conversation.converse"])
        )
    finally:
        env.session = original_session
        provider.release.set()
        await env.controller.close()


async def test_released_slots_are_reclaimed_while_oldest_cleanup_stays_owned(env):
    provider = SlowCloseProvider(1)
    env.runtime.provider = provider
    env.controller._provider_cleanup_timeout_s = 0.03
    original_session = env.session
    try:
        for index in range(32):
            env.session = env.store.create_session(SessionRequest(channel="converse"))
            await decision(env, name=f"request-{index}")
            await env.controller.tick()
            if index == 0:
                while not provider.iterators:
                    await asyncio.sleep(0)
                await asyncio.wait_for(provider.iterators[0].holding.wait(), 1)
                oldest = env.controller.pending[env.session.session_id]
            else:
                await asyncio.wait_for(env.controller.pending[env.session.session_id].task, 1)
                if index == 1:
                    completed = env.controller.pending[env.session.session_id]
                    completed_reply = env.store.read(
                        session_id=env.session.session_id, kinds=["conversation.converse"]
                    )[0]
        env.controller._abandon(oldest)
        await asyncio.wait_for(provider.iterators[0].closing.wait(), 1)
        await asyncio.wait_for(asyncio.gather(oldest.task, return_exceptions=True), 1)
        assert len(env.controller.pending) == 32 and not oldest.cleanup.done()
        env.session = original_session
        source, event = await decision(env, name="request-33")
        await settle(env)
        assert provider.calls == 33 and env.controller.after >= event.sequence
        assert env.controller.pending[oldest.source.session_id] is oldest
        assert completed.source.session_id not in env.controller.pending
        await env.runtime.validate_speech(
            completed_reply.event_id, completed_reply.payload["generation_epoch"]
        )
        assert not oldest.cleanup.done() and not provider.iterators[0].cleanup_cancelled
        assert len(env.controller.pending) <= 32
        assert any(
            e.payload["window_source_id"] == source.event_id
            for e in env.store.read(kinds=["conversation.converse"])
        )
    finally:
        env.session = original_session
        provider.release.set()
        await env.controller.close()


async def test_full_prepared_cache_waits_for_user_opening_without_dropping_reply_or_audio(env):
    original_session = env.session
    held_sessions = []
    try:
        for index in range(32):
            env.session = env.store.create_session(SessionRequest(channel="converse"))
            held_sessions.append(env.session)
            await decision(env, name=f"held-{index}", user_speaking=True)
            await env.controller.tick()
            await asyncio.wait_for(env.controller.pending[env.session.session_id].task, 1)
        assert len(env.controller.pending) == 32
        assert all(
            p.generation is None and p.published == 0 for p in env.controller.pending.values()
        )
        assert not env.store.read(kinds=["conversation.converse"])
        env.session = original_session
        _, event = await decision(env, name="request-33")
        await env.controller.tick()
        assert env.controller.after < event.sequence and env.provider.calls == 32
        assert env.controller.last_error == "window_capacity_busy"
        assert len(env.controller.pending) == 32
        env.session = held_sessions[0]
        await decision(env, name="held-0", index=1, user_speaking=False)
        await settle(env)
        assert env.provider.calls == 33 and env.controller.after >= event.sequence
        replies = env.store.read(session_id=env.session.session_id, kinds=["conversation.converse"])
        assert len(replies) == 2 and env.session.session_id not in env.controller.pending
        for entry in replies:
            await env.runtime.validate_speech(entry.event_id, entry.payload["generation_epoch"])
    finally:
        env.session = original_session
        await env.controller.close()


async def test_hung_native_read_is_bounded_and_retains_pre_generation_decision(env):
    env.controller._journal_timeout_s = 0.02
    env.faults.target, env.faults.remaining = "resolve", 100
    env.faults.release = asyncio.Event()
    source, event = await decision(env)
    await asyncio.wait_for(env.controller.tick(), 1)
    assert len(env.faults.requests) == 3
    assert env.controller.after == source.sequence < event.sequence
    assert not env.provider.calls
    env.faults.remaining = 0
    env.faults.release.set()
    await settle(env)
    assert env.provider.calls == 1 and len(env.store.read(kinds=["conversation.converse"])) == 2


async def seed_previous_generation(env):
    s = env.session
    await env.journal.append(
        AppendRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            kind="converse.safety",
            payload={"reason": "public_synthetic_history", "generation_epoch": 5},
            idempotency_key="public-synthetic-generation-history",
        )
    )


@pytest.mark.parametrize("failure", ["before_timeout", "after_timeout", "after_lost_ack"])
async def test_playback_restart_barrier_reuses_exact_generation_after_uncertain_append(
    env, failure
):
    await seed_previous_generation(env)
    env.faults.target, env.faults.remaining = "playback_restart_barrier", 1
    env.faults.when = "before" if failure == "before_timeout" else "after"
    if failure.endswith("timeout"):
        env.controller._journal_timeout_s = 0.02
        env.faults.release = asyncio.Event()
    await decision(env)
    await settle(env)
    assert env.provider.calls == 1 and env.provider.deltas == 2
    assert len(env.faults.requests) == 2
    assert env.faults.requests[0] == env.faults.requests[1]
    barriers = [
        e
        for e in env.store.read(kinds=["converse.safety"])
        if e.payload["reason"] == "playback_restart_barrier"
    ]
    assert len(barriers) == 1 and barriers[0].payload["generation_epoch"] == 6
    replies = env.store.read(kinds=["conversation.converse"])
    assert len(replies) == 2 and {e.payload["generation_epoch"] for e in replies} == {7}


@pytest.mark.parametrize("when", ["before", "after"])
async def test_reset_during_uncertain_restart_barrier_discards_old_admission(env, when):
    await seed_previous_generation(env)
    env.faults.target, env.faults.when, env.faults.remaining = "playback_restart_barrier", when, 1
    env.faults.release = asyncio.Event()
    await decision(env)
    tick = asyncio.create_task(env.controller.tick())
    try:
        await asyncio.wait_for(env.faults.entered.wait(), 1)
        assert env.session.session_id not in env.runtime.playback
        env.session = env.store.reset(env.session.session_id)
        env.faults.release.set()
        await asyncio.wait_for(tick, 2)
        assert not env.provider.calls and not env.store.read(kinds=["conversation.converse"])
        source, _ = await decision(env)
        await settle(env)
        replies = env.store.read(kinds=["conversation.converse"])
        assert env.provider.calls == 1 and len(replies) == 2
        assert {e.epoch for e in replies} == {env.session.epoch}
        assert {e.payload["window_source_id"] for e in replies} == {source.event_id}
        assert {e.payload["generation_epoch"] for e in replies} == {1}
    finally:
        env.faults.release.set()
        await asyncio.gather(tick, return_exceptions=True)


async def test_owner_off_cancels_provider_before_first_delta_during_native_read_outage(env):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        info = ScriptedProvider.info

        async def stream(self, messages):
            entered.set()
            try:
                await asyncio.Event().wait()
                yield "Unreachable synthetic reply."
            finally:
                closed.set()

    env.runtime.provider = Provider()
    await decision(env)
    await env.controller.tick()
    await asyncio.wait_for(entered.wait(), 1)
    env.faults.target, env.faults.remaining = "read", 100
    await env.controller.tick()
    await env.runtime.set_enabled(False, owner=True)
    await asyncio.wait_for(closed.wait(), 1)
    await env.controller.tick()
    assert not env.store.read(kinds=["conversation.converse"])
    assert all(p.task.done() for p in env.controller.pending.values())


async def test_recovered_generation_timeouts_preserve_later_owner_cancellation(env):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        info = ScriptedProvider.info
        calls = 0

        async def stream(self, messages):
            self.calls += 1
            entered.set()
            try:
                await asyncio.Event().wait()
                yield "Unreachable synthetic reply."
            finally:
                closed.set()

    provider = Provider()
    env.runtime.provider = provider
    env.controller._journal_timeout_s = 0.02
    env.faults.target, env.faults.remaining = "preparing", 2
    env.faults.release = asyncio.Event()
    try:
        await decision(env)
        await env.controller.tick()
        await asyncio.wait_for(entered.wait(), 1)
        assert len(env.faults.requests) == 3 and provider.calls == 1
        task = env.controller.pending[env.session.session_id].task
        assert task.cancelling() == 0
        await env.runtime.set_enabled(False, owner=True)
        await asyncio.wait_for(closed.wait(), 1)
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() and not env.store.read(kinds=["conversation.converse"])
    finally:
        env.faults.release.set()


async def test_repeated_generation_native_timeouts_exhaust_with_no_inference_replay(env):
    env.controller._journal_timeout_s = 0.02
    env.faults.target, env.faults.remaining = "preparing", 100
    env.faults.release = asyncio.Event()
    await decision(env)
    await asyncio.wait_for(settle(env), 1)
    assert len(env.faults.requests) == 3 and not env.provider.calls
    errors = [
        e for e in env.store.read(kinds=["converse.window_state"]) if e.payload["phase"] == "error"
    ]
    assert errors[-1].payload["error_source"] == "journal"
    env.faults.release.set()
    env.faults.remaining = 0
    await settle(env)
    assert not env.provider.calls and not env.store.read(kinds=["conversation.converse"])


@pytest.mark.parametrize(
    "ending", ["blocked", "eof", "error", "close_error", "close_cancel", "close_error_diagnostic"]
)
@pytest.mark.parametrize("adapter", ["compatible", "meta"])
async def test_owner_controls_cannot_cancel_http_cleanup_inside_provider_next(env, ending, adapter):
    class TransportStream(httpx.AsyncByteStream):
        def __init__(self):
            self.between = asyncio.Event()
            self.closing = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False
            self.cleanup_cancelled = False

        async def __aiter__(self):
            event = (
                {"choices": [{"delta": {"content": "Public synthetic first sentence. "}}]}
                if adapter == "compatible"
                else {
                    "type": "response.output_text.delta",
                    "delta": "Public synthetic first sentence. ",
                }
            )
            yield ("data: " + json.dumps(event) + "\n\n").encode()
            self.between.set()
            if ending == "blocked":
                await asyncio.Event().wait()
            elif ending == "error":
                raise ProviderError("Synthetic provider stream failure")
            elif adapter == "meta":
                yield b'data: {"type":"response.completed"}\n\n'

        async def aclose(self):
            self.closing.set()
            try:
                await self.release.wait()
                if ending in {"close_error", "close_error_diagnostic"}:
                    raise OSError("Synthetic HTTP response close failure")
                if ending == "close_cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                self.closed = True
            except asyncio.CancelledError:
                self.cleanup_cancelled = True
                raise

    stream = TransportStream()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    provider = (
        CompatibleTextProvider(
            "https://synthetic.invalid/v1",
            "public-synthetic",
            allow_cloud=True,
            transport=transport,
        )
        if adapter == "compatible"
        else MetaResponsesProvider(
            base_url="https://synthetic.invalid/v1",
            api_key="synthetic-fixture-key",
            allow_cloud=True,
            transport=transport,
        )
    )
    env.runtime.provider = provider
    env.controller._provider_cleanup_timeout_s = 0.03
    if ending == "close_error_diagnostic":
        env.controller._journal_timeout_s = 0.02
        env.faults.target, env.faults.remaining = "error", 100
        env.faults.release = asyncio.Event()
    try:
        await decision(env)
        await env.controller.tick()
        await asyncio.wait_for(stream.between.wait(), 1)
        old = env.controller.pending[env.session.session_id]
        if ending == "blocked":
            env.controller._abandon(old)
        await asyncio.wait_for(stream.closing.wait(), 1)
        await env.runtime.set_enabled(False, owner=True)
        await env.runtime.set_enabled(True, owner=True)
        await asyncio.wait_for(env.controller.close(), 0.5)
        assert not old.task.done()
        assert env.controller.pending[env.session.session_id] is old
        assert not stream.closed and not stream.cleanup_cancelled
        for _ in range(3):
            await env.controller.tick()
        assert not stream.cleanup_cancelled
        stream.release.set()
        done, _ = await asyncio.wait({old.task}, timeout=0.1)
        assert done == {old.task}
        assert old.task.cancelled()
        await env.controller.close()
        if ending in {"close_error", "close_cancel", "close_error_diagnostic"}:
            assert not stream.closed
            assert old.cleanup_failed and env.controller.pending[env.session.session_id] is old
            assert env.controller.last_error == "ProviderCleanupError"
            _, event = await decision(env, name="new-intent", text="Public synthetic next request.")
            await env.controller.tick()
            assert env.controller.after < event.sequence
        else:
            assert stream.closed and not stream.cleanup_cancelled
            assert not env.controller.pending
        if ending == "close_error_diagnostic":
            assert not env.faults.requests
    finally:
        if env.faults.release is not None:
            env.faults.release.set()
        stream.release.set()
        await env.controller.close()
        await provider.close()
