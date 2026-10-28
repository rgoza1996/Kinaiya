"""Independent targets and preserved Whisper/TTS transports; no model calls."""

import json

import httpx
import pytest

from packages.providers.speech import SpeechClient, SpeechProviderError
from services.resources.whistle import ManagedWhistleConfig


async def test_default_whisper_stays_multipart_and_tts_stays_same_profile():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("transcriptions"):
            assert request.headers["content-type"].startswith("multipart/form-data")
            return httpx.Response(200, json={"text": "synthetic whisper"})
        assert json.loads(request.content) == {"input": "Public text", "profile": "Supertonic 3"}
        return httpx.Response(200, text='{"pcm":"synthetic"}\n')

    client = SpeechClient(
        "http://127.0.0.1:9990",
        "synthetic-token",
        profile="Supertonic 3",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.transcribe(b"public fixture") == {"text": "synthetic whisper"}
        assert [frame async for frame in client.synthesize("Public text")] == [{"pcm": "synthetic"}]
        assert [request.url.port for request in calls] == [9990, 9990]
        assert client.stt is None
    finally:
        await client.close()


async def test_whistle_raw_target_is_independent_of_tts_and_preserves_credentials_scope():
    calls = []

    def stt(request):
        calls.append(request)
        assert request.url.port == 8764 and request.headers["content-type"] == "audio/wav"
        assert request.content == b"public fixture"
        assert request.headers["authorization"] == "Bearer synthetic-converse-token"
        return httpx.Response(
            200,
            json={
                "text": "synthetic whistle",
                "profile": "experimental_buffered_cpu",
                "provider": "whistle",
            },
        )

    def tts(request):
        calls.append(request)
        assert request.url.port == 9990 and request.url.path == "/v1/audio/speech"
        assert json.loads(request.content)["profile"] == "VibeVoice Realtime 0.5B"
        return httpx.Response(200, text='{"pcm":"synthetic"}\n')

    client = SpeechClient(
        "http://127.0.0.1:9990",
        "synthetic-converse-token",
        profile="VibeVoice Realtime 0.5B",
        transport=httpx.MockTransport(tts),
        stt_url="http://127.0.0.1:8764",
        stt_provider="whistle",
        stt_transport=httpx.MockTransport(stt),
    )
    try:
        assert (await client.transcribe(b"public fixture"))["text"] == "synthetic whistle"
        assert [frame async for frame in client.synthesize("Public text")] == [{"pcm": "synthetic"}]
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1:8764/path",
        "http://user:secret@127.0.0.1:8764",
        "http://127.0.0.1:8764?token=value",
    ],
)
def test_whistle_stt_origin_cannot_send_service_token_remotely(url):
    with pytest.raises(ValueError):
        SpeechClient(
            "http://127.0.0.1:9990", "synthetic-token", stt_provider="whistle", stt_url=url
        )


@pytest.mark.parametrize("values", [{"stt_provider": []}, {"stt_provider": "whistle"}])
def test_invalid_selection_is_rejected_before_constructing_any_http_client(monkeypatch, values):
    def unexpected_client(**kwargs):
        pytest.fail("Invalid selection constructed an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValueError):
        SpeechClient("http://127.0.0.1:9990", "synthetic-token", **values)


@pytest.mark.parametrize(
    "body",
    [
        b'{"text":123}',
        b'{"text":"a","text":"b"}',
        json.dumps({"text": "x" * 16001}).encode(),
        b"x" * (128 * 1024 + 1),
    ],
)
async def test_bounded_whistle_response_validation(body):
    client = SpeechClient(
        "http://127.0.0.1:9990",
        "synthetic-token",
        stt_provider="whistle",
        stt_url="http://127.0.0.1:8764",
        stt_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )
    try:
        with pytest.raises(ValueError):
            await client.transcribe(b"fixture")
    finally:
        await client.close()


async def test_managed_refusal_does_not_forward_untrusted_error_body():
    client = SpeechClient(
        "http://127.0.0.1:9990",
        "synthetic-token",
        stt_provider="whistle",
        stt_url="http://127.0.0.1:8764",
        stt_transport=httpx.MockTransport(
            lambda request: httpx.Response(409, json={"detail": "untrusted native error text"})
        ),
    )
    try:
        with pytest.raises(SpeechProviderError) as exc:
            await client.transcribe(b"fixture")
        assert exc.value.status_code == 409 and "untrusted" not in str(exc.value)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "values",
    [
        {"request_timeout_s": 0},
        {"request_timeout_s": float("inf")},
        {"cpu_affinity_count": 0},
        {"memory_bytes": 1},
        {"network": True},
    ],
)
def test_invalid_owner_limits_or_extra_capabilities(values):
    with pytest.raises(ValueError):
        ManagedWhistleConfig(
            python="/owner/python", weights="/owner/weights", library="/owner/library", **values
        )
