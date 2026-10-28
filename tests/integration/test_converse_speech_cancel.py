"""Synthetic blocked speech producers; no model, sound device or paid inference."""

import asyncio
import json

import httpx
import pytest

from packages.contracts.models import ConverseRequest, SessionRequest
from packages.providers.speech import SpeechClient
from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import SpeechRequest, create_app
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store

AUTH = Auth({"owner": "o" * 32, "resources": "r" * 32, "agent": "a" * 32})
OWNER = {"Authorization": "Bearer " + AUTH.tokens["owner"]}
FRAME = {"audio_base64": "AAA=", "sample_rate": 24000, "index": 0}


class BlockedSpeech:
    def __init__(self, first_frame=False):
        self.first_frame = first_frame
        self.entered = {}
        self.closed = {}
        self.calls = []

    async def synthesize(self, text):
        self.calls.append(text)
        entered = self.entered.setdefault(text, asyncio.Event())
        closed = self.closed.setdefault(text, asyncio.Event())
        try:
            if self.first_frame:
                yield {**FRAME, "event_id": "untrusted", "generation_epoch": 999}
            entered.set()
            await asyncio.Event().wait()
            yield FRAME
        finally:
            closed.set()


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"),
        ScriptedProvider(["Public synthetic greeting."]),
        intent=ConverseIntent(tmp_path / "converse-intent.json"),
    )
    yield store, runtime
    store.close()


async def reply(store, runtime, key="greeting", session=None):
    session = session or store.create_session(SessionRequest(channel="converse"))
    result = await runtime.turn(
        ConverseRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            text="Public synthetic input " + key,
            idempotency_key=key,
        )
    )
    entry = next(e["entry"] for e in result["events"] if e["type"] == "text")
    return session, entry


def speech_body(entry):
    return {
        "event_id": entry["event_id"],
        "generation_epoch": entry["payload"]["generation_epoch"],
    }


async def wait_started(speech, text):
    # The provider signals the precise no-frame boundary, rather than relying
    # on a sleep to guess whether the HTTP request has started.
    while text not in speech.entered:
        await asyncio.sleep(0)
    await speech.entered[text].wait()


@pytest.mark.parametrize("first_frame", [False, True])
@pytest.mark.parametrize(
    "transition",
    [
        "interrupt",
        "owner_off",
        "resource_pause",
        "new_turn",
        "window_generation",
        "resource_setter",
    ],
)
async def test_native_controls_close_a_blocked_upstream(state, first_frame, transition):
    store, runtime = state
    session, entry = await reply(store, runtime)
    text = entry["payload"]["text"]
    speech = BlockedSpeech(first_frame)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
        base_url="http://test",
    ) as client:
        pending = asyncio.create_task(
            client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
        )
        try:
            await asyncio.wait_for(wait_started(speech, text), 1)
            if transition == "interrupt":
                result = await client.post(
                    "/v1/converse/interrupt",
                    json={"session_id": session.session_id},
                    headers=OWNER,
                )
                assert result.status_code == 200
                assert result.json()["agent_cancelled"] is False
            elif transition in {"owner_off", "resource_pause"}:
                actor = "owner" if transition == "owner_off" else "resources"
                result = await client.post(
                    "/v1/converse/enabled",
                    json={"enabled": False},
                    headers={"Authorization": "Bearer " + AUTH.tokens[actor]},
                )
                assert result.status_code == 200
            elif transition == "new_turn":
                runtime.provider = ScriptedProvider(["A newer public synthetic reply."])
                await reply(store, runtime, "newer", session)
            elif transition == "window_generation":
                # Prepared window replies use the same generation object's begin().
                (await runtime._epoch(session.session_id)).begin()
            else:
                runtime.enabled = False
            await asyncio.wait_for(speech.closed[text].wait(), 1)
            response = await asyncio.wait_for(pending, 1)
            assert response.status_code == 200
            frames = [json.loads(line) for line in response.text.splitlines()]
            assert len(frames) <= int(first_frame)
            assert all(frame["event_id"] == entry["event_id"] for frame in frames)
            assert all(frame["generation_epoch"] != 999 for frame in frames)
            assert not runtime._speech
            assert store.read(kinds=["agent.command"]) == []
            if transition == "owner_off":
                await runtime.set_enabled(True)
                assert not runtime.enabled and not runtime.intent.read().enabled
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_cancellation_does_not_wait_for_interrupt_journal_io(state):
    store, runtime = state
    session, entry = await reply(store, runtime)
    speech = BlockedSpeech()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.journal.session

    async def held(session_id):
        entered.set()
        await release.wait()
        return await original(session_id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
        base_url="http://test",
    ) as client:
        pending = asyncio.create_task(
            client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
        )
        interrupt = None
        try:
            text = entry["payload"]["text"]
            await asyncio.wait_for(wait_started(speech, text), 1)
            runtime.journal.session = held
            interrupt = asyncio.create_task(runtime.interrupt(session.session_id))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(speech.closed[text].wait(), 1)
            assert not interrupt.done()
            release.set()
            await asyncio.wait_for(interrupt, 1)
            assert (await asyncio.wait_for(pending, 1)).status_code == 200
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(
                pending, *([interrupt] if interrupt else []), return_exceptions=True
            )


async def test_registration_precedes_suspended_authoritative_validation(state):
    store, runtime = state
    session, entry = await reply(store, runtime)
    speech = BlockedSpeech()
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.validate_speech
    calls = 0

    async def held(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return await original(*args)

    runtime.validate_speech = held
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
        base_url="http://test",
    ) as client:
        pending = asyncio.create_task(
            client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.interrupt(session.session_id)
            response = await asyncio.wait_for(pending, 1)
            assert response.status_code == 200 and not response.content
            assert not speech.calls and not runtime._speech
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("transition", ["session_reset", "external_owner_off", "journal_outage"])
async def test_external_revocation_is_observed_while_no_audio_arrives(state, transition):
    store, runtime = state
    session, entry = await reply(store, runtime)
    speech = BlockedSpeech()
    text = entry["payload"]["text"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
        base_url="http://test",
    ) as client:
        pending = asyncio.create_task(
            client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
        )
        try:
            await asyncio.wait_for(wait_started(speech, text), 1)
            if transition == "session_reset":
                store.reset(session.session_id)
            elif transition == "external_owner_off":
                ConverseIntent(runtime.intent.path).set(False)
            else:

                async def unavailable(session_id):
                    raise httpx.ConnectError("synthetic journal outage")

                runtime.journal.session = unavailable
            await asyncio.wait_for(speech.closed[text].wait(), 2)
            assert (await asyncio.wait_for(pending, 1)).status_code == 200
            assert not runtime._speech
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_response_backpressure_does_not_keep_the_provider_open(state):
    store, runtime = state
    session, entry = await reply(store, runtime)
    speech = BlockedSpeech(first_frame=True)
    app = create_app(runtime, AUTH, speech)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/v1/converse/speech")
    response = await endpoint(SpeechRequest(**speech_body(entry)))
    stream = response.body_iterator
    try:
        frame = json.loads(await anext(stream))
        assert frame["event_id"] == entry["event_id"]
        text = entry["payload"]["text"]
        await asyncio.wait_for(wait_started(speech, text), 1)
        await runtime.interrupt(session.session_id)
        # No next response frame is requested before the upstream is closed.
        await asyncio.wait_for(speech.closed[text].wait(), 1)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert not runtime._speech
    finally:
        await stream.aclose()


@pytest.mark.parametrize("transition", ["interrupt", "owner_off_on", "resource_off_on"])
async def test_revoke_after_endpoint_validation_before_stream_registration(state, transition):
    store, runtime = state
    session, entry = await reply(store, runtime)
    speech = BlockedSpeech()
    app = create_app(runtime, AUTH, speech)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/v1/converse/speech")
    response = await endpoint(SpeechRequest(**speech_body(entry)))
    if transition == "interrupt":
        await runtime.interrupt(session.session_id)
    elif transition == "owner_off_on":
        await runtime.set_enabled(False, owner=True)
        await runtime.set_enabled(True, owner=True)
    else:
        runtime.enabled = False
        runtime.enabled = True
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)
    assert not speech.calls and not runtime._speech


async def test_cancel_during_per_frame_validation_closes_upstream_without_journal_reply(state):
    store, runtime = state
    session, entry = await reply(store, runtime)
    speech = BlockedSpeech(first_frame=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.validate_speech
    calls = 0

    async def held(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            entered.set()
            await release.wait()
        return await original(*args)

    runtime.validate_speech = held
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
        base_url="http://test",
    ) as client:
        pending = asyncio.create_task(
            client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.interrupt(session.session_id)
            text = entry["payload"]["text"]
            await asyncio.wait_for(speech.closed[text].wait(), 1)
            response = await asyncio.wait_for(pending, 1)
            assert response.status_code == 200 and not response.content
            assert not runtime._speech
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_late_chunk_after_provider_catches_cancellation_is_discarded(state):
    store, runtime = state
    session, entry = await reply(store, runtime)
    entered, closed = asyncio.Event(), asyncio.Event()

    class LateSpeech:
        async def synthesize(self, text):
            try:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    yield FRAME
            finally:
                closed.set()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH, LateSpeech())),
        base_url="http://test",
    ) as client:
        pending = asyncio.create_task(
            client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.interrupt(session.session_id)
            await asyncio.wait_for(closed.wait(), 1)
            response = await asyncio.wait_for(pending, 1)
            assert response.status_code == 200 and not response.content
            assert not runtime._speech
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_stale_expected_generation_and_other_session_cannot_cancel_active_speech(state):
    store, runtime = state
    first, old = await reply(store, runtime, "first")
    runtime.provider = ScriptedProvider(["Another public synthetic greeting."])
    second, current = await reply(store, runtime, "second")
    speech = BlockedSpeech()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
        base_url="http://test",
    ) as client:
        pending = [
            asyncio.create_task(
                client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
            )
            for entry in (old, current)
        ]
        try:
            for entry in (old, current):
                await asyncio.wait_for(wait_started(speech, entry["payload"]["text"]), 1)
            result = await client.post(
                "/v1/converse/interrupt",
                json={"session_id": second.session_id, "expected_generation": 999},
                headers=OWNER,
            )
            assert result.status_code == 409
            assert not speech.closed[current["payload"]["text"]].is_set()
            await runtime.interrupt(first.session_id)
            await asyncio.wait_for(speech.closed[old["payload"]["text"]].wait(), 1)
            assert not pending[1].done()
            assert not speech.closed[current["payload"]["text"]].is_set()
            await runtime.interrupt(second.session_id)
            assert all(r.status_code == 200 for r in await asyncio.gather(*pending))
            assert not runtime._speech
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


class BlockedAudio(httpx.AsyncByteStream):
    def __init__(self):
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        await asyncio.Event().wait()
        yield b""

    async def aclose(self):
        self.closed.set()


@pytest.mark.parametrize("disconnect", [False, True])
async def test_cancellation_closes_real_httpx_response_while_waiting_for_audio(state, disconnect):
    store, runtime = state
    session, entry = await reply(store, runtime)
    audio = BlockedAudio()
    speech = SpeechClient(
        "http://127.0.0.1:8765",
        "synthetic-voice-service-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=audio)),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
            base_url="http://test",
        ) as client:
            pending = asyncio.create_task(
                client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
            )
            try:
                await asyncio.wait_for(audio.entered.wait(), 1)
                if disconnect:
                    pending.cancel()
                else:
                    await runtime.interrupt(session.session_id)
                await asyncio.wait_for(audio.closed.wait(), 1)
                await asyncio.gather(pending, return_exceptions=True)
                assert not runtime._speech
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        await speech.close()


async def test_cancellation_reaches_http_request_before_voice_response_headers(state):
    store, runtime = state
    session, entry = await reply(store, runtime)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def request_without_headers(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    speech = SpeechClient(
        "http://127.0.0.1:8765",
        "synthetic-voice-service-token",
        transport=httpx.MockTransport(request_without_headers),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
            base_url="http://test",
        ) as client:
            pending = asyncio.create_task(
                client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await runtime.interrupt(session.session_id)
                await asyncio.wait_for(cancelled.wait(), 1)
                assert (await asyncio.wait_for(pending, 1)).status_code == 200
                assert not runtime._speech
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        await speech.close()


async def test_asgi_disconnect_waits_for_upstream_iterator_cleanup(state):
    store, runtime = state
    _, entry = await reply(store, runtime)
    entered, cleanup, release, closed, disconnect = (asyncio.Event() for _ in range(5))

    class ClosingSpeech:
        async def synthesize(self, text):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield FRAME
            finally:
                cleanup.set()
                await release.wait()
                closed.set()

    app = create_app(runtime, AUTH, ClosingSpeech())
    body = json.dumps(speech_body(entry)).encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    messages = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": "/v1/converse/speech",
        "raw_path": b"/v1/converse/speech",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", ("Bearer " + AUTH.tokens["owner"]).encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8762),
    }
    pending = asyncio.create_task(app(scope, receive, send))
    try:
        try:
            await asyncio.wait_for(entered.wait(), 1)
        except TimeoutError:
            assert not pending.done(), (messages, await pending)
            raise
        disconnect.set()
        await asyncio.wait_for(cleanup.wait(), 1)
        # Cleanup holds an explicit barrier. The request must retain ownership
        # until that cleanup finishes rather than orphaning a background task.
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        await asyncio.wait_for(pending, 1)
        assert closed.is_set() and not runtime._speech
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_upstream_http_error_closes_stream_and_unregisters_operation(state):
    store, runtime = state
    _, entry = await reply(store, runtime)
    audio = BlockedAudio()
    speech = SpeechClient(
        "http://127.0.0.1:8765",
        "synthetic-voice-service-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(503, stream=audio)),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, AUTH, speech)),
            base_url="http://test",
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
            assert audio.closed.is_set() and not runtime._speech
    finally:
        await speech.close()


async def test_old_runtime_and_non_owner_requests_do_not_start_a_speech_operation(state):
    store, runtime = state
    _, entry = await reply(store, runtime)
    restored = ConverseRuntime(runtime.journal, runtime.provider, intent=runtime.intent)
    speech = BlockedSpeech()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(restored, AUTH, speech)),
        base_url="http://test",
    ) as client:
        assert (
            await client.post("/v1/converse/speech", json=speech_body(entry), headers=OWNER)
        ).status_code == 409
        assert (
            await client.post(
                "/v1/converse/speech",
                json=speech_body(entry),
                headers={"Authorization": "Bearer " + AUTH.tokens["agent"]},
            )
        ).status_code == 403
        assert not restored._speech and not speech.calls
