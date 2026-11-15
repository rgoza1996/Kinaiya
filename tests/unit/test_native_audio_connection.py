"""#595: explicit connection choice never relaxes ASR identity or ownership."""

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from test_native_audio_converse import admit, body, environment, native_config
from test_realtime_audio_provider import (
    MODEL,
    PCM,
    Connector,
    Current,
    Socket,
    TranscriptionTransitionSocket,
    audio,
    messages,
    provider,
    wait,
    wire_pcm,
)

from packages.providers.speech import SpeechProviderError
from packages.providers.text import ProviderRefusal
from services import launcher


class ExplicitTranscriptionSocket(TranscriptionTransitionSocket):
    async def send(self, raw):
        event = json.loads(raw)
        if event["type"] == "session.update":
            self.mode = event["session"]["type"]
        await super().send(raw)


@pytest.mark.parametrize("choice", ["intent_url", "session_update"])
async def test_connection_choice_requires_matching_ack_before_original_pcm(choice):
    socket = ExplicitTranscriptionSocket(hold_ack=True)
    connector = Connector(socket)
    target = provider(connector, transcription_connection=choice)
    pending = asyncio.create_task(target.transcribe(audio(), current=Current()))
    try:
        await wait(socket.receive_held)
        assert wire_pcm(socket) == b"" and not pending.done()
        assert [e["type"] for e in socket.sent] == ["session.update"]
        assert socket.sent[0]["session"]["type"] == "transcription"
        expected = {"model": [MODEL]}
        if choice == "intent_url":
            expected["intent"] = ["transcription"]
        assert parse_qs(urlsplit(connector.calls[0][0]).query) == expected
        socket.receive_release.set()
        assert await asyncio.wait_for(pending, 2) == socket.transcript
        assert wire_pcm(socket) == PCM
        assert len(connector.calls) == socket.close_calls == 1
        assert not any(e["type"] == "response.create" for e in socket.sent)
    finally:
        socket.receive_release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await target.close()


@pytest.mark.parametrize("choice", ["intent_url", "session_update"])
@pytest.mark.parametrize("changed_ack", ["type", "session-id", "rate"])
async def test_bad_ack_is_refused_without_pcm_or_another_connection(choice, changed_ack):
    socket = ExplicitTranscriptionSocket(changed_ack=changed_ack)
    connector = Connector(socket)
    target = provider(connector, transcription_connection=choice)
    try:
        with pytest.raises(ProviderRefusal):
            await target.transcribe(audio(), current=Current())
        assert wire_pcm(socket) == b"" and len(connector.calls) == 1
        assert socket.close_calls == 1 and socket.close_exited.is_set()
    finally:
        await target.close()


@pytest.mark.parametrize("choice", ["intent_url", "session_update"])
async def test_uncommitted_stream_delta_does_not_trigger_mode_fallback(choice):
    class StreamingSocket(ExplicitTranscriptionSocket):
        def push(self, event):
            if isinstance(event, dict) and event["type"] == "input_audio_buffer.committed":
                event = {
                    "type": "conversation.item.input_audio_transcription.delta",
                    "item_id": "uncommitted-public-fixture",
                    "content_index": 0,
                    "delta": "Public partial",
                }
            super().push(event)

    socket = StreamingSocket()
    connector = Connector(socket)
    target = provider(connector, transcription_connection=choice)
    try:
        with pytest.raises(ProviderRefusal):
            await target.transcribe(audio(), current=Current())
        assert len(connector.calls) == 1 and socket.close_calls == 1
        assert not any(e["type"] == "response.create" for e in socket.sent)
    finally:
        await target.close()


@pytest.mark.parametrize("choice", [None, True, 1, [], {}, "", "auto", "SESSION_UPDATE"])
def test_invalid_connection_choice_is_refused_before_transport(choice):
    connector = Connector()
    with pytest.raises(ValueError, match="Native audio configuration is invalid"):
        provider(connector, transcription_connection=choice)
    assert not connector.calls


async def test_session_update_choice_does_not_change_native_dialogue_connection():
    socket = Socket()
    connector = Connector(socket)
    target = provider(connector, transcription_connection="session_update")
    try:
        reply = await target.respond(audio(), messages(), current=Current())
        assert reply.text and reply.pcm16
        assert parse_qs(urlsplit(connector.calls[0][0]).query) == {"model": [MODEL]}
        assert socket.sent[0]["session"]["type"] == "realtime"
    finally:
        await target.close()


async def test_native_owner_rejects_connection_selection_change_during_asr(tmp_path):
    socket = ExplicitTranscriptionSocket(hold_ack=True)
    connector = Connector(socket)
    target = provider(connector, transcription_connection="session_update")
    # Reuse the real Store/runtime fixture's release-and-join teardown.
    target.asr_release = target.reply_release = socket.receive_release
    async with environment(tmp_path, native=target) as env:
        request = body(env)
        pending = asyncio.create_task(admit(env, request))
        try:
            await wait(socket.receive_held)
            target._transcription_connection = "intent_url"
            socket.receive_release.set()
            with pytest.raises(SpeechProviderError):
                await asyncio.wait_for(pending, 2)
            assert env.store.read(kinds=["conversation.window"]) == []
            assert wire_pcm(socket) == b""
            with pytest.raises(SpeechProviderError):
                await admit(env, request)
            assert len(connector.calls) == 1 and socket.close_calls == 1
        finally:
            socket.receive_release.set()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("choice", ["intent_url", "session_update"])
def test_actual_launcher_passes_explicit_connection_choice(tmp_path, monkeypatch, choice):
    from packages.providers import realtime_audio
    from services.converse import native_audio

    seen = []
    config = native_config()
    config["native_audio"]["transcription_connection"] = choice
    monkeypatch.setenv("KINAIYA_PUBLIC_NATIVE_FIXTURE", "public-synthetic-local-bearer")
    monkeypatch.setattr(realtime_audio, "RealtimeAudioProvider", lambda *a, **kw: seen.append(kw))
    monkeypatch.setattr(native_audio, "NativeAudioOwner", lambda *a, **kw: object())
    launcher.converse_native_audio(config, None, tmp_path, tmp_path)
    assert len(seen) == 1 and seen[0]["transcription_connection"] == choice


@pytest.mark.parametrize("choice", [None, True, 1, [], {}, "auto"])
def test_launcher_refuses_bad_choice_before_constructing_native_owner(
    tmp_path, monkeypatch, choice
):
    from services.converse import native_audio

    config = native_config()
    config["native_audio"]["transcription_connection"] = choice
    monkeypatch.setenv("KINAIYA_PUBLIC_NATIVE_FIXTURE", "public-synthetic-local-bearer")

    def forbidden(*a, **kw):
        pytest.fail("Invalid ASR connection choice must not construct an owner")

    monkeypatch.setattr(native_audio, "NativeAudioOwner", forbidden)
    with pytest.raises(ValueError, match="Native audio configuration is invalid"):
        launcher.converse_native_audio(config, None, tmp_path, tmp_path)
