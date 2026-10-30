"""Buffered cloud speech uses synthetic HTTP/audio only; local TTS stays local."""

import asyncio
import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest

from packages.providers import meta_speech
from packages.providers.meta_speech import CloudSTTLocalTTS, MetaSTTClient, normalize_wav
from packages.providers.speech import SpeechClient, SpeechProviderError

RESULT = {
    "sessionId": "public-session",
    "transcript": "Public synthetic speech.",
    "audioDurationMs": 1000,
    "turns": [],
}


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000)
    return output.getvalue()


class Body(httpx.AsyncByteStream):
    def __init__(self, raw=None, hold=False, fail=False):
        self.raw = json.dumps(RESULT).encode() if raw is None else raw
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.closed, self.fail = 0, fail
        if not hold:
            self.release.set()

    async def __aiter__(self):
        yield self.raw

    async def aclose(self):
        self.closing.set()
        self.closed += 1
        await self.release.wait()
        if self.fail:
            raise OSError("synthetic private transport detail")


def client(monkeypatch, body=None, status=200):
    calls = []
    body = body or Body()

    def respond(request):
        calls.append(request)
        return httpx.Response(status, stream=body)

    monkeypatch.setattr(meta_speech, "normalize_wav", lambda data: wav())
    return (
        MetaSTTClient(
            api_key="synthetic-cloud-key",
            allow_cloud=True,
            transport=httpx.MockTransport(respond),
        ),
        calls,
        body,
    )


async def test_actual_wav_multipart_no_contributor_or_service_header(monkeypatch):
    stt, calls, body = client(monkeypatch)
    local_calls = []

    def tts(request):
        local_calls.append(request)
        return httpx.Response(200, text='{"pcm":"synthetic"}\n')

    local = SpeechClient(
        "http://127.0.0.1:8765", "synthetic-service-token", transport=httpx.MockTransport(tts)
    )
    speech = CloudSTTLocalTTS(local, stt)
    try:
        result = await speech.transcribe(
            b"actual encoded browser fixture",
            "private.webm",
            content_scope="public_synthetic",
            current=lambda: True,
        )
        assert result["text"] == RESULT["transcript"] and body.closed == 1
        assert calls[0].url.path == "/v1/asr/transcribe"
        assert calls[0].headers["authorization"] == "Bearer synthetic-cloud-key"
        assert "x-tbh-contributor-mode" not in calls[0].headers
        assert b"synthetic-service-token" not in calls[0].content
        assert b"private.webm" not in calls[0].content
        assert wav() in calls[0].content and b"PUSH_TO_TALK" in calls[0].content
        assert [frame async for frame in speech.synthesize("Public reply.")] == [
            {"pcm": "synthetic"}
        ]
        assert local_calls[0].headers["authorization"] == "Bearer synthetic-service-token"
        assert local_calls[0].url.host == "127.0.0.1"
    finally:
        await speech.close()


@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized", "unknown"])
async def test_denied_disclosure_never_decodes_or_uploads(monkeypatch, scope):
    stt, calls, _ = client(monkeypatch)
    monkeypatch.setattr(meta_speech, "normalize_wav", lambda data: pytest.fail("denied decoding"))
    try:
        with pytest.raises(SpeechProviderError):
            await stt.transcribe(b"private", content_scope=scope, current=lambda: True)
        assert not calls
    finally:
        await stt.close()


async def test_off_during_decode_blocks_upload(monkeypatch):
    stt, calls, _ = client(monkeypatch)
    active = True

    def decode(data):
        nonlocal active
        active = False
        return wav()

    monkeypatch.setattr(meta_speech, "normalize_wav", decode)
    try:
        with pytest.raises(SpeechProviderError):
            await stt.transcribe(
                b"public", content_scope="public_synthetic", current=lambda: active
            )
        assert not calls
    finally:
        await stt.close()


async def test_late_result_after_off_is_closed_and_refused(monkeypatch):
    stt, calls, body = client(monkeypatch, Body(hold=True))
    active = True
    task = asyncio.create_task(
        stt.transcribe(b"public", content_scope="public_synthetic", current=lambda: active)
    )
    try:
        await asyncio.wait_for(body.closing.wait(), 1)
        active = False
        body.release.set()
        with pytest.raises(SpeechProviderError):
            await task
        assert len(calls) == 1 and body.closed == 1
    finally:
        body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await stt.close()


async def test_repeated_cancel_retains_real_close(monkeypatch):
    stt, _, body = client(monkeypatch, Body(hold=True))
    task = asyncio.create_task(
        stt.transcribe(b"public", content_scope="public_synthetic", current=lambda: True)
    )
    try:
        await asyncio.wait_for(body.closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and body.closed == 1
        body.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        body.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await stt.close()


async def test_failed_close_taints_adapter_and_blocks_next_upload(monkeypatch):
    stt, calls, _ = client(monkeypatch, Body(fail=True))
    try:
        for _ in range(2):
            with pytest.raises(SpeechProviderError):
                await stt.transcribe(
                    b"public", content_scope="public_synthetic", current=lambda: True
                )
        assert len(calls) == 1
    finally:
        await stt.close()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"transcript":"x","transcript":"y"}',
        b"\xff",
        b" " * (128 * 1024 + 1),
        json.dumps({**RESULT, "audioDurationMs": True}).encode(),
        json.dumps({**RESULT, "turns": [{"text": "unexpected"}]}).encode(),
        json.dumps({**RESULT, "transcript": "a" * 16001}).encode(),
    ],
)
async def test_strict_bounded_response(monkeypatch, raw):
    stt, calls, body = client(monkeypatch, Body(raw))
    try:
        with pytest.raises(SpeechProviderError):
            await stt.transcribe(b"public", content_scope="public_synthetic", current=lambda: True)
        assert len(calls) == 1 and body.closed == 1
    finally:
        await stt.close()


@pytest.mark.parametrize("seconds", [1, 60, 61])
def test_real_normalizer_shape_and_duration_before_large_conversion(monkeypatch, seconds):
    frame = SimpleNamespace(samples=seconds * 16000, sample_rate=16000, pts=1)
    frame.to_ndarray = lambda: SimpleNamespace(tobytes=lambda: b"\0\0" * frame.samples)
    opened = []

    class Container:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            opened.append("closed")

        def decode(self, **kwargs):
            return iter([frame])

    def open_audio(source, **kwargs):
        assert source.read() == b"encoded webm"
        assert kwargs["options"] == {"protocol_whitelist": ""}
        opened.append("open")
        return Container()

    def resampler(**kwargs):
        assert kwargs == {"format": "s16", "layout": "mono", "rate": 16000}
        return SimpleNamespace(resample=lambda value: [] if value is None else [value])

    monkeypatch.setattr(
        meta_speech.importlib,
        "import_module",
        lambda name: SimpleNamespace(open=open_audio, AudioResampler=resampler),
    )
    if seconds > 60:
        frame.to_ndarray = lambda: pytest.fail("oversize decoded frame materialized")
        with pytest.raises(SpeechProviderError):
            normalize_wav(b"encoded webm")
    else:
        output = normalize_wav(b"encoded webm")
        with wave.open(io.BytesIO(output), "rb") as stream:
            assert (
                stream.getnchannels(),
                stream.getsampwidth(),
                stream.getframerate(),
                stream.getnframes(),
            ) == (1, 2, 16000, seconds * 16000)
    assert opened == ["open", "closed"]


@pytest.mark.parametrize(
    "change",
    [
        {"allow_cloud": False},
        {"secret_ref": "other"},
        {"model": "other"},
        {"api_key": "must-not-be-in-host-profile"},
        {"base_url": "http://remote.invalid/v1"},
        {"base_url": "https://user:secret@remote.invalid/v1"},
        {"base_url": "https://remote.invalid/v1?secret=x"},
        {"timeout_s": True},
        {"timeout_s": float("nan")},
        {"timeout_s": 301},
    ],
)
def test_host_cloud_profile_requires_explicit_bounded_selection(change):
    choice = {"provider": "meta", "secret_ref": "muse", "allow_cloud": True, **change}
    with pytest.raises(ValueError):
        meta_speech.meta_stt_settings(choice)


async def test_launcher_composes_real_local_tts_without_service_token_export(monkeypatch):
    from services.launcher import converse_speech

    captured = []

    class STT:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        async def close(self):
            captured.append("closed")

    monkeypatch.setattr(meta_speech, "MetaSTTClient", STT)
    choice = {"provider": "meta", "allow_cloud": True, "secret_ref": "muse"}
    speech = converse_speech(
        {"voice": {"url": "http://127.0.0.1:8765", "stt": choice}}, "synthetic-service-token"
    )
    try:
        assert isinstance(speech, CloudSTTLocalTTS)
        assert captured == [{"allow_cloud": True, "secret_ref": "muse"}]
        assert speech.local.http.headers["authorization"] == "Bearer synthetic-service-token"
        assert speech.local.stt is None
    finally:
        await speech.close()


@pytest.mark.parametrize("profile", ["local_only", "configured_hybrid"])
async def test_protected_resource_policy_independently_gates_cloud_speech(
    monkeypatch, tmp_path, profile
):
    from services.resources.state import ResourceState

    state = ResourceState(tmp_path / "resources.json", profile=profile)
    state.save(phase="ready")
    stt, calls, _ = client(monkeypatch)
    speech = CloudSTTLocalTTS(SimpleNamespace(profile="Kokoro 82M"), stt, resource_state=state.path)
    try:
        if profile == "local_only":
            with pytest.raises(SpeechProviderError):
                await speech.transcribe(
                    b"public", content_scope="public_synthetic", current=lambda: True
                )
            assert not calls
        else:
            assert (
                await speech.transcribe(
                    b"public", content_scope="public_synthetic", current=lambda: True
                )
            )["text"] == RESULT["transcript"]
            assert len(calls) == 1
    finally:
        await stt.close()
        state.close()
