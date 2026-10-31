"""Owned typed-turn transport cleanup; synthetic HTTP only, no inference."""

import asyncio
import json

import httpx
import pytest

from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.text import (
    CompatibleTextProvider,
    MetaResponsesProvider,
    ProviderError,
    ScriptedProvider,
)
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime, PlaybackEpoch, TextOperation
from services.converse.windows import WindowConverseController
from services.session.store import LocalJournal, Store


class HeldHTTPStream(httpx.AsyncByteStream):
    def __init__(self, adapter="compatible", ending="eof"):
        self.adapter, self.ending = adapter, ending
        self.between, self.closing, self.release = (asyncio.Event() for _ in range(3))
        self.closed = self.close_cancelled = False
        self.close_calls = 0

    async def __aiter__(self):
        event = (
            {"choices": [{"delta": {"content": "Public synthetic sentence. "}}]}
            if self.adapter == "compatible"
            else {"type": "response.output_text.delta", "delta": "Public synthetic sentence. "}
        )
        yield ("data: " + json.dumps(event) + "\n\n").encode()
        self.between.set()
        if self.ending == "blocked":
            await asyncio.Event().wait()
        elif self.ending == "error":
            raise ProviderError("Synthetic provider failure")
        if self.adapter == "meta":
            yield b'data: {"type":"response.completed"}\n\n'

    async def aclose(self):
        self.close_calls += 1
        self.closing.set()
        try:
            await self.release.wait()
            if self.ending == "close_error":
                raise OSError("Synthetic transport close failure")
            if self.ending == "close_cancel":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            self.closed = True
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise


def provider_for(stream, calls):
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, stream=stream)

    transport = httpx.MockTransport(respond)
    if stream.adapter == "compatible":
        return CompatibleTextProvider("http://localhost/v1", "synthetic", transport=transport)
    return MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-key",
        allow_cloud=True,
        transport=transport,
    )


def request_for(session, key="first"):
    return ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text="Public synthetic input " + key,
        content_scope="public_synthetic",
        idempotency_key=key,
    )


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    yield store, session
    store.close()


@pytest.mark.parametrize("adapter", ["compatible", "meta"])
@pytest.mark.parametrize("ending", ["blocked", "eof", "error", "close_error", "close_cancel"])
@pytest.mark.parametrize("stop", ["interrupt", "off", "disconnect"])
async def test_typed_stop_is_bounded_and_retains_real_transport_cleanup(
    state, adapter, ending, stop
):
    store, session = state
    stream, calls = HeldHTTPStream(adapter, ending), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.025
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(stream.between.wait(), 1)
        if ending != "blocked":
            await asyncio.wait_for(stream.closing.wait(), 1)
        if stop == "interrupt":
            await runtime.interrupt(session.session_id)
        elif stop == "off":
            await runtime.set_enabled(False, owner=True)
        else:
            pending.cancel()
        await asyncio.wait_for(stream.closing.wait(), 1)
        done, _ = await asyncio.wait({pending}, timeout=0.2)
        assert done == {pending}
        assert not stream.closed and not stream.close_cancelled
        operation = runtime._text[session.session_id]
        assert not operation.cleanup.done()
        assert runtime.text_status()["cleanup_pending"] == 1
        await runtime.set_enabled(True, owner=True)
        before = len(store.read(kinds=["conversation.user"]))
        with pytest.raises(RuntimeError, match="cleanup"):
            await runtime.turn(request_for(session, "replacement"))
        assert len(calls) == 1 and len(store.read(kinds=["conversation.user"])) == before
        for _ in range(3):
            await runtime.interrupt(session.session_id)
            await runtime.set_enabled(False, owner=True)
        assert runtime._text[session.session_id] is operation and not stream.close_cancelled
        stream.release.set()
        await asyncio.wait_for(operation.cleanup, 1)
        if ending in {"close_error", "close_cancel"}:
            assert operation.cleanup_failed and runtime._text[session.session_id] is operation
            assert runtime.text_status()["cleanup_failed"] == 1
        else:
            assert stream.closed and session.session_id not in runtime._text
        assert stream.close_calls == 1
    finally:
        stream.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        if hasattr(runtime, "close"):
            await runtime.close()
        await provider.close()


@pytest.mark.parametrize("adapter", ["compatible", "meta"])
@pytest.mark.parametrize("ending", ["close_error", "close_cancel"])
async def test_healthy_eof_cleanup_failure_stays_quarantined_without_stop(state, adapter, ending):
    store, session = state
    stream, calls = HeldHTTPStream(adapter, ending), []
    stream.release.set()
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    try:
        result = await asyncio.wait_for(runtime.turn(request_for(session)), 1)
        assert result["events"][-1] == {"type": "error", "error": "ProviderCleanupError"}
        operation = runtime._text[session.session_id]
        assert operation.cleanup.done() and operation.cleanup_failed
        assert runtime.text_status()["cleanup_failed"] == 1
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await runtime.turn(request_for(session, "later"))
        assert len(calls) == 1 and len(store.read(kinds=["conversation.user"])) == 1
    finally:
        await runtime.close()
        await provider.close()


@pytest.mark.parametrize("paused", ["accepted", "text"])
async def test_closing_paused_events_retains_transport_owner(state, paused):
    store, session = state
    stream, calls = HeldHTTPStream(ending="blocked"), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.025
    events = runtime.events(request_for(session))
    try:
        assert (await anext(events))["type"] == "accepted"
        await asyncio.wait_for(stream.between.wait(), 1)
        if paused == "text":
            assert (await anext(events))["type"] == "text"
        await asyncio.wait_for(events.aclose(), 0.2)
        await asyncio.wait_for(stream.closing.wait(), 1)
        operation = runtime._text[session.session_id]
        assert not operation.cleanup.done() and not stream.close_cancelled
        stream.release.set()
        await asyncio.wait_for(operation.cleanup, 1)
        assert stream.closed and not runtime._text
    finally:
        stream.release.set()
        await events.aclose()
        await runtime.close()
        await provider.close()


async def test_repeated_request_cancellation_cannot_cancel_actual_http_close(state):
    store, session = state
    stream, calls = HeldHTTPStream(ending="blocked"), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.06
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(stream.between.wait(), 1)
        pending.cancel()
        await asyncio.wait_for(stream.closing.wait(), 1)
        operation = runtime._text[session.session_id]
        for _ in range(4):
            pending.cancel()
            operation.task.cancel()
            await asyncio.sleep(0)
        done, _ = await asyncio.wait({pending}, timeout=0.2)
        assert done == {pending} and pending.cancelled()
        assert not stream.close_cancelled and not operation.cleanup.done()
        stream.release.set()
        await asyncio.wait_for(operation.cleanup, 1)
        assert stream.closed and not runtime._text
    finally:
        stream.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()
        await provider.close()


@pytest.mark.parametrize("resist_cancel", [False, True])
async def test_conforming_iterator_without_aclose_waits_for_actual_producer_exit(
    state, resist_cancel
):
    store, session = state
    entered, release = asyncio.Event(), asyncio.Event()

    class Iterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not resist_cancel:
                    raise
                await release.wait()
            raise StopAsyncIteration

    class Provider:
        info = ScriptedProvider.info

        def stream(self, messages):
            return Iterator()

    runtime = ConverseRuntime(LocalJournal(store, "converse"), Provider())
    runtime._text_cleanup_timeout = 0.025
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        operation = runtime._text[session.session_id]
        await runtime.interrupt(session.session_id)
        result = await asyncio.wait_for(pending, 0.2)
        assert result["events"][-1]["type"] == "interrupted"
        assert not store.read(kinds=["conversation.converse"])
        assert (session.session_id in runtime._text) is resist_cancel
        release.set()
        await asyncio.wait_for(operation.cleanup, 1)
        assert not runtime._text
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("invalid", ["thread", "epoch", "text", "mode", "replay"])
async def test_invalid_or_replayed_request_does_not_cancel_legitimate_work(state, invalid):
    store, session = state
    stream, calls = HeldHTTPStream(ending="blocked"), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.025
    original = request_for(session)
    pending = asyncio.create_task(runtime.turn(original))
    try:
        await asyncio.wait_for(stream.between.wait(), 1)
        operation = runtime._text[session.session_id]
        generation = runtime.playback[session.session_id].generation
        changes = {
            "thread": {"thread_id": "wrong-thread"},
            "epoch": {"epoch": session.epoch + 1},
            "text": {"text": "Different public synthetic input"},
            "mode": {"input_mode": "voice"},
            "replay": {},
        }[invalid]
        candidate = original.model_copy(update=changes)
        if invalid == "replay":
            result = await runtime.turn(candidate)
            assert result["events"][-1]["replay"]
        else:
            with pytest.raises(ValueError):
                await runtime.turn(candidate)
        assert runtime._text[session.session_id] is operation and not operation.cancelled
        assert runtime.playback[session.session_id].generation == generation
        assert not stream.closing.is_set() and len(calls) == 1
    finally:
        stream.release.set()
        await runtime.interrupt(session.session_id)
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()
        await provider.close()


@pytest.mark.parametrize("transition", ["reset", "owner_file", "resource_setter"])
async def test_external_scope_change_stops_before_first_delta_and_discards_late_text(
    state, transition, tmp_path
):
    store, session = state
    entered, late = asyncio.Event(), asyncio.Event()

    class Provider:
        info = ScriptedProvider.info

        async def stream(self, messages):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                late.set()
                yield "Public synthetic late sentence."

    runtime = ConverseRuntime(
        LocalJournal(store, "converse"),
        Provider(),
        intent=ConverseIntent(tmp_path / "owner-intent.json"),
    )
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if transition == "reset":
            store.reset(session.session_id)
        elif transition == "owner_file":
            ConverseIntent(runtime.intent.path).set(False)
        else:
            runtime.enabled = False
        result = await asyncio.wait_for(pending, 1)
        assert late.is_set() and result["events"][-1]["type"] == "interrupted"
        assert not store.read(kinds=["conversation.converse", "converse.safety"])
        assert not runtime._text
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()


async def test_disconnect_with_failed_cleanup_does_no_diagnostic_journal_io(state):
    store, session = state
    stream, calls = HeldHTTPStream(ending="close_error"), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.025
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    writes = []
    try:
        await asyncio.wait_for(stream.closing.wait(), 1)

        async def forbidden_append(request):
            writes.append(request)
            await asyncio.Event().wait()

        runtime.journal.append = forbidden_append
        pending.cancel()
        stream.release.set()
        done, _ = await asyncio.wait({pending}, timeout=0.2)
        assert done == {pending} and pending.cancelled()
        assert not writes and runtime.text_status()["cleanup_failed"] == 1
    finally:
        stream.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()
        await provider.close()


async def test_32_retained_cleanups_refuse_33rd_before_user_append_and_reclaim_confirmed_slot(
    state,
):
    store, first = state
    streams = []
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        stream = HeldHTTPStream(ending="blocked")
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic", transport=httpx.MockTransport(respond)
    )
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.01
    sessions = [first] + [
        store.create_session(SessionRequest(channel="converse")) for _ in range(32)
    ]
    store.append(
        AppendRequest(
            session_id=sessions[32].session_id,
            thread_id=sessions[32].thread_id,
            epoch=sessions[32].epoch,
            kind="conversation.user",
            payload={"text": "Earlier public input.", "final": True, "generation_epoch": 7},
            idempotency_key="earlier-public-input",
        ),
        "converse",
    )
    tasks = [asyncio.create_task(runtime.turn(request_for(session))) for session in sessions[:32]]
    try:
        while len(streams) < 32:
            await asyncio.sleep(0)
        await asyncio.wait_for(asyncio.gather(*(stream.between.wait() for stream in streams)), 2)
        runtime.enabled = False
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert len(runtime._text) == 32 and runtime.text_status()["cleanup_pending"] == 32
        runtime.enabled = True
        before = len(store.read(limit=200))
        with pytest.raises(RuntimeError, match="capacity"):
            await runtime.turn(request_for(sessions[32]))
        assert len(calls) == 32 and len(store.read(limit=200)) == before
        assert len(store.read(kinds=["conversation.user"])) == 33
        oldest = runtime._text[first.session_id]
        released = runtime._text[sessions[1].session_id]
        streams[1].release.set()
        await asyncio.wait_for(released.cleanup, 1)
        replacement = asyncio.create_task(runtime.turn(request_for(sessions[32])))
        tasks.append(replacement)
        while len(streams) < 33:
            await asyncio.sleep(0)
        assert runtime._text[first.session_id] is oldest and len(runtime._text) == 32
    finally:
        for stream in streams:
            stream.release.set()
        runtime.enabled = False
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.close()
        await provider.close()


async def window_decision(
    store, session, controller, *, behavior="RESPOND_WITHOUT_INTERRUPTING_USER"
):
    source = await controller.ingest(
        SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="public-window",
            window_index=0,
            text="Public synthetic speech window.",
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
    )
    value = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=1,
        conversation=behavior,
        agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="routing.window_decided",
            payload=value.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key="decision:" + source.event_id,
        ),
        "decisions",
    )


@pytest.mark.parametrize("adapter", ["compatible", "meta"])
@pytest.mark.parametrize("direction", ["typed_to_window", "window_to_typed"])
@pytest.mark.parametrize("ending", ["eof", "close_error", "close_cancel"])
async def test_symmetric_cross_window_admission_retains_actual_http_close(
    state, adapter, direction, ending
):
    store, session = state
    stream, calls = HeldHTTPStream(adapter, ending), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.025
    controller = WindowConverseController(runtime)
    controller._provider_cleanup_timeout_s = 0.025
    pending = None
    try:
        if direction == "typed_to_window":
            pending = asyncio.create_task(runtime.turn(request_for(session)))
            await asyncio.wait_for(stream.closing.wait(), 1)
            await runtime.interrupt(session.session_id)
            await asyncio.wait_for(pending, 0.2)
            operation = runtime._text[session.session_id]
            decision = await window_decision(store, session, controller)
            await controller.tick()
            assert controller.after < decision.sequence and not controller.pending
            assert runtime._text[session.session_id] is operation
            stream.release.set()
            await asyncio.wait_for(operation.cleanup, 1)
            runtime.provider = ScriptedProvider(["A fresh public window reply."])
            await controller.tick()
            if ending == "eof":
                await asyncio.wait_for(controller.pending[session.session_id].task, 1)
                assert controller.after >= decision.sequence and len(runtime.provider.calls) == 1
            else:
                assert operation.cleanup_failed and runtime._text[session.session_id] is operation
                assert controller.after < decision.sequence and not runtime.provider.calls
        else:
            await window_decision(store, session, controller)
            await controller.tick()
            await asyncio.wait_for(stream.closing.wait(), 1)
            operation = controller.pending[session.session_id]
            before = len(store.read(kinds=["conversation.user"]))
            with pytest.raises(RuntimeError, match="cleanup"):
                await runtime.turn(request_for(session))
            assert len(store.read(kinds=["conversation.user"])) == before
            assert controller.pending[session.session_id] is operation and not operation.task.done()
            stream.release.set()
            await asyncio.wait_for(asyncio.gather(operation.task, return_exceptions=True), 1)
            runtime.provider = ScriptedProvider(["A fresh public typed reply."])
            if ending == "eof":
                result = await runtime.turn(request_for(session))
                assert result["text"] == "A fresh public typed reply."
            else:
                assert operation.cleanup_failed
                with pytest.raises(RuntimeError, match="cleanup"):
                    await runtime.turn(request_for(session))
                assert not runtime.provider.calls
        assert len(calls) == 1 and stream.closed is (ending == "eof")
        assert stream.close_cancelled is (ending == "close_cancel")
    finally:
        stream.release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await controller.close()
        await runtime.close()
        await provider.close()


async def test_window_wait_bypasses_typed_cleanup_admission(state):
    store, session = state
    stream, calls = HeldHTTPStream(ending="blocked"), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.025
    controller = WindowConverseController(runtime)
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(stream.between.wait(), 1)
        decision = await window_decision(
            store, session, controller, behavior="WAIT_FOR_NEXT_WINDOW"
        )
        await asyncio.wait_for(controller.tick(), 0.2)
        assert controller.after >= decision.sequence and not controller.pending
        await asyncio.wait_for(pending, 0.2)
        assert runtime._text[session.session_id].cancelled and len(calls) == 1
    finally:
        stream.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await controller.close()
        await runtime.close()
        await provider.close()


async def test_asgi_disconnect_while_send_is_paused_closes_events_with_bounded_wait(state):
    store, session = state
    stream, calls = HeldHTTPStream(ending="blocked"), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.025
    auth = Auth({"owner": "o" * 32})
    app = create_app(runtime, auth)
    disconnect, sending = asyncio.Event(), asyncio.Event()
    body = request_for(session).model_dump_json().encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            sending.set()
            await asyncio.Event().wait()

    path = "/v1/converse/turns/stream"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", b"Bearer " + b"o" * 32),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8762),
    }
    pending = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(sending.wait(), 1)
        await asyncio.wait_for(stream.between.wait(), 1)
        disconnect.set()
        await asyncio.wait_for(pending, 0.2)
        operation = runtime._text[session.session_id]
        assert operation.cancelled and not operation.cleanup.done() and not stream.close_cancelled
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            status = (await client.get("/health")).json()["text_operations"]
        assert status == {
            "active": 0,
            "cleanup_pending": 1,
            "cleanup_failed": 0,
            "admitting": 0,
            "capacity": 32,
        }
        stream.release.set()
        await asyncio.wait_for(operation.cleanup, 1)
        assert stream.closed and not runtime._text
    finally:
        stream.release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()
        await provider.close()


async def test_admission_lock_wait_is_bounded_without_cancelling_existing_admission(state):
    store, session = state
    entered, release = asyncio.Event(), asyncio.Event()
    journal = LocalJournal(store, "converse")
    original = journal.append

    async def held(request):
        if request.kind == "conversation.user":
            entered.set()
            await release.wait()
        return await original(request)

    journal.append = held
    provider = ScriptedProvider(["Public synthetic first reply."])
    runtime = ConverseRuntime(journal, provider)
    runtime._text_cleanup_timeout = 0.025
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        operation = runtime._text[session.session_id]
        replacement = asyncio.create_task(runtime.turn(request_for(session, "next")))
        done, _ = await asyncio.wait({replacement}, timeout=0.2)
        assert done == {replacement}
        with pytest.raises(RuntimeError, match="admission/cleanup"):
            await replacement
        assert runtime._text[session.session_id] is operation and not operation.cancelled
        assert not provider.calls and not store.read(kinds=["conversation.user"])
        release.set()
        assert (await asyncio.wait_for(pending, 1))["text"] == "Public synthetic first reply."
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()


async def test_replay_releases_admission_lock_before_paused_output(state):
    store, session = state
    provider = ScriptedProvider(
        ["First public sentence. Second public sentence.", "New public reply."]
    )
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    original = request_for(session)
    await runtime.turn(original)
    replay = runtime.events(original)
    try:
        event = await anext(replay)
        assert event["type"] == "text" and event["replay"]
        assert (await runtime.turn(request_for(session, "new")))["text"] == "New public reply."
        assert len(provider.calls) == 2
    finally:
        await replay.aclose()
        await runtime.close()


async def test_reset_during_idempotency_read_rejects_replay_without_new_provider_call(state):
    store, session = state
    provider = ScriptedProvider(["Original public reply."])
    journal = LocalJournal(store, "converse")
    runtime = ConverseRuntime(journal, provider)
    original = request_for(session)
    await runtime.turn(original)
    recent = runtime._recent

    async def reset_during_recent(session_id):
        entries = await recent(session_id)
        store.reset(session_id)
        return entries

    runtime._recent = reset_during_recent
    with pytest.raises(ValueError, match="Session changed"):
        await runtime.turn(original)
    assert len(provider.calls) == 1
    await runtime.close()


async def test_typed_reservation_blocks_window_during_native_scope_revalidation(state):
    store, session = state
    entered, release = asyncio.Event(), asyncio.Event()
    journal = LocalJournal(store, "converse")
    provider = ScriptedProvider(["Public typed reply."])
    runtime = ConverseRuntime(journal, provider)
    controller = WindowConverseController(runtime)
    await runtime._epoch(session.session_id)
    original = journal.session

    async def held(session_id):
        if session_id in runtime._text_admitting and not runtime._epoch_locks[session_id].locked():
            entered.set()
            await release.wait()
        return await original(session_id)

    journal.session = held
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not runtime.typed_provider_ready(session.session_id)
        decision = await window_decision(store, session, controller)
        # Only typed revalidation is held. The window's real native reads
        # remain available, so readiness alone must prevent its admission.
        journal.session = original
        await controller.tick()
        assert controller.after < decision.sequence and not controller.pending
        assert not provider.calls and not store.read(kinds=["conversation.user"])
        release.set()
        assert (await asyncio.wait_for(pending, 1))["text"] == "Public typed reply."
    finally:
        journal.session = original
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await controller.close()
        await runtime.close()


async def test_admission_and_new_close_share_one_request_wait_budget(state, monkeypatch):
    store, session = state
    releases = [asyncio.Event(), asyncio.Event()]
    closing = [asyncio.Event(), asyncio.Event()]

    class Iterator:
        def __init__(self, index):
            self.index, self.sent = index, False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return "Public synthetic reply."

        async def aclose(self):
            closing[self.index].set()
            await releases[self.index].wait()

    class Provider:
        info = ScriptedProvider.info
        calls = 0

        def stream(self, messages):
            iterator = Iterator(self.calls)
            self.calls += 1
            return iterator

    runtime = ConverseRuntime(LocalJournal(store, "converse"), Provider())
    # Assert the allowance passed to terminal cleanup, rather than assuming
    # unrelated journal I/O finishes inside a sub-100 ms wall-clock window.
    runtime._text_cleanup_timeout = 10
    cleanup_allowances, admission_allowances = [], []
    original_cleanup = runtime._wait_text_cleanup

    async def observe_cleanup(cleanups, timeout):
        cleanup_allowances.append(timeout)
        # Exercise the real no-wait path; the actual iterator close stays owned.
        await original_cleanup(cleanups, 0)

    monkeypatch.setattr(runtime, "_wait_text_cleanup", observe_cleanup)
    pending = None
    try:
        await runtime.turn(request_for(session))
        first = runtime._text[session.session_id]
        await asyncio.wait_for(closing[0].wait(), 5)
        assert closing[0].is_set() and not first.cleanup.done()
        admitting = asyncio.Event()
        original_admission = runtime._wait_text_admission

        async def observe_admission(*args):
            turn = args[1]
            before = turn.wait_remaining
            admitting.set()
            await original_admission(*args)
            assert 0 <= turn.wait_remaining < before
            admission_allowances.append(turn.wait_remaining)

        runtime._wait_text_admission = observe_admission
        pending = asyncio.create_task(runtime.turn(request_for(session, "next")))
        await asyncio.wait_for(admitting.wait(), 5)
        releases[0].set()
        assert (await asyncio.wait_for(pending, 5))["text"] == "Public synthetic reply."
        assert len(cleanup_allowances) == 2 and len(admission_allowances) == 1
        assert cleanup_allowances[1] == admission_allowances[0]
        assert 0 < cleanup_allowances[1] < runtime._text_cleanup_timeout
        replacement = runtime._text[session.session_id]
        await asyncio.wait_for(closing[1].wait(), 5)
        assert replacement is not first and closing[1].is_set() and not replacement.cleanup.done()
        assert first.cleanup.done() and runtime.provider.calls == 2
        releases[1].set()
        await asyncio.wait_for(replacement.cleanup, 1)
        assert not runtime._text
    finally:
        for release in releases:
            release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        monkeypatch.setattr(runtime, "_wait_text_cleanup", original_cleanup)
        await runtime.close()


async def test_reset_while_reply_append_is_suspended_rejects_late_old_epoch_write(state):
    store, session = state
    journal = LocalJournal(store, "converse")
    entered, release = asyncio.Event(), asyncio.Event()
    original = journal.append

    async def held(request):
        if request.kind == "conversation.converse":
            entered.set()
            await release.wait()
        return await original(request)

    journal.append = held
    runtime = ConverseRuntime(journal, ScriptedProvider(["Old public synthetic reply."]))
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        store.reset(session.session_id)
        release.set()
        with pytest.raises(ValueError, match="Stale epoch"):
            await asyncio.wait_for(pending, 1)
        assert not store.read(kinds=["conversation.converse"])
        assert not runtime.playback[session.session_id].chunks and not runtime._text
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("prior_reply", [False, True])
async def test_wait_resuming_held_typed_user_append_never_starts_provider(state, prior_reply):
    store, session = state
    entered, release = asyncio.Event(), asyncio.Event()
    journal = LocalJournal(store, "converse")
    original = journal.append

    async def held(request):
        if request.kind == "conversation.user":
            entered.set()
            await release.wait()
        return await original(request)

    provider = ScriptedProvider(["Public prior reply.", "Public old admission must stay silent."])
    runtime = ConverseRuntime(journal, provider)
    controller = WindowConverseController(runtime)
    if prior_reply:
        await runtime.turn(request_for(session, "prior"))
    before_calls = len(provider.calls)
    before_text = len(store.read(kinds=["conversation.converse"]))
    journal.append = held
    pending = asyncio.create_task(runtime.turn(request_for(session)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        before = runtime.playback[session.session_id].generation
        if prior_reply:
            assert runtime.playback[session.session_id].stop.desired_generation < before
        decision = await window_decision(
            store, session, controller, behavior="WAIT_FOR_NEXT_WINDOW"
        )
        await controller.tick()
        assert controller.after >= decision.sequence
        assert runtime.playback[session.session_id].generation > before
        fenced = runtime.playback[session.session_id].generation
        assert runtime.playback[session.session_id].stop.desired_generation == fenced
        for _ in range(3):
            await controller.process(decision)
        assert runtime.playback[session.session_id].generation == fenced
        release.set()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(pending, 1)
        assert len(provider.calls) == before_calls
        assert len(store.read(kinds=["conversation.converse"])) == before_text
        assert not runtime._text and not runtime._text_admitting
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await controller.close()
        await runtime.close()


async def test_refused_typed_replacement_still_stops_emitted_window_pcm(state):
    store, session = state
    stream, calls = HeldHTTPStream(ending="eof"), []
    provider = provider_for(stream, calls)
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    runtime._text_cleanup_timeout = 0.025
    controller = WindowConverseController(runtime)
    controller._provider_cleanup_timeout_s = 0.025

    class Speech:
        async def synthesize(self, text):
            yield {"audio_base64": "AAA=", "sample_rate": 24000, "index": 0}

    auth = Auth({"owner": "o" * 32})
    app = create_app(runtime, auth, Speech(), controller)
    try:
        await window_decision(store, session, controller)
        await controller.tick()
        await asyncio.wait_for(stream.closing.wait(), 1)
        old = controller.pending[session.session_id]
        reply = store.read(kinds=["conversation.converse"])[0]
        generation = reply.payload["generation_epoch"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/converse/speech",
                headers={"Authorization": "Bearer " + "o" * 32},
                json={"event_id": reply.event_id, "generation_epoch": generation},
            )
        assert response.status_code == 200 and json.loads(response.text)["audio_base64"] == "AAA="
        with pytest.raises(RuntimeError, match="cleanup"):
            await runtime.turn(request_for(session))
        stops = [
            entry
            for entry in store.read(kinds=["delivery.updated"])
            if entry.payload.get("status") == "interrupted"
            and entry.payload.get("generation_epoch") == generation
        ]
        assert stops and stops[-1].payload["next_generation_epoch"] > generation
        assert not old.task.done() and not stream.closed and len(calls) == 1
        assert not store.read(kinds=["conversation.user"])
        with pytest.raises(ValueError, match="Obsolete"):
            await runtime.validate_speech(reply.event_id, generation)
    finally:
        stream.release.set()
        await controller.close()
        await runtime.close()
        await provider.close()


async def test_text_scope_cannot_alias_equal_generation_after_native_reset(state):
    store, session = state
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider([]))
    old = TextOperation(
        session.session_id, session.thread_id, session.epoch, 8, runtime.admission()
    )
    current = store.reset(session.session_id)
    runtime.playback[session.session_id] = PlaybackEpoch(
        generation=8, scope=(current.epoch, current.thread_id)
    )
    assert not old.cancelled and runtime.playback[session.session_id].accepts(old.generation)
    assert not runtime._text_current(old)
    await runtime.close()


@pytest.mark.parametrize("when", ["before", "after"])
async def test_uncertain_stop_refuses_new_input_and_retains_exact_native_request(state, when):
    store, session = state
    provider = ScriptedProvider(["First public reply.", "Fresh public reply."])
    journal = LocalJournal(store, "converse")
    runtime = ConverseRuntime(journal, provider)
    runtime._text_cleanup_timeout = 0.025
    await runtime.turn(request_for(session))
    original = journal.append
    attempts = []

    async def uncertain(request):
        if (
            request.kind == "delivery.updated"
            and request.payload.get("control") == "generation_stop"
        ):
            attempts.append(request)
            if when == "after":
                await original(request)
            await asyncio.Event().wait()
        return await original(request)

    journal.append = uncertain
    pending = asyncio.create_task(runtime.turn(request_for(session, "next")))
    try:
        done, _ = await asyncio.wait({pending}, timeout=0.2)
        assert done == {pending}
        with pytest.raises(RuntimeError, match="cleanup"):
            await pending
        # No new user/provider work is admitted on an uncertain stop receipt.
        assert len(provider.calls) == 1 and len(store.read(kinds=["conversation.user"])) == 1
        playback = runtime.playback[session.session_id]
        stop = playback.stop
        assert len(attempts) == 1 and stop.request is attempts[0]
        assert stop.desired_generation < playback.generation
        assert not stop.covers(stop.desired_generation)
        assert not runtime._text_admitting and not runtime._text
        journal.append = original
        await runtime.reconcile_stop(session.session_id)
        assert stop.covers(stop.desired_generation) and stop.request is None
        events = [
            event
            for event in store.read(kinds=["delivery.updated"])
            if event.idempotency_key == attempts[0].idempotency_key
        ]
        assert len(events) == 1 and events[0].payload == attempts[0].payload
    finally:
        journal.append = original
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()
