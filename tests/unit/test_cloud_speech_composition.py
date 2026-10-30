"""Independent cloud speech selection without credentials or external inference."""

import sys
from types import SimpleNamespace

import pytest

from packages.contracts.models import ProviderInfo
from packages.providers import meta_speech
from packages.providers import speech as local_speech
from packages.providers.meta_speech import CloudSTTLocalTTS
from services.converse.setup import host_setup, setup_state
from services.launcher import converse_speech

MODEL = "hexgrad/kokoro-82m"


@pytest.fixture
def cloud(monkeypatch):
    calls = []

    def settings(choice):
        assert choice == {"provider": "openrouter", "allow_cloud": True, "secret_ref": "openrouter"}
        return {"allow_cloud": True, "secret_ref": "openrouter"}

    class TTS:
        profile = "Kokoro 82M (OpenRouter)"
        info = ProviderInfo(provider="openrouter", model=MODEL, local=False, streaming=True)

        def __init__(self, **kwargs):
            calls.append(("tts", kwargs))

        async def close(self):
            calls.append("tts-closed")

    class STT:
        info = ProviderInfo(provider="meta", model=meta_speech.MODEL, local=False)

        def __init__(self, **kwargs):
            calls.append(("stt", kwargs))

        async def close(self):
            calls.append("stt-closed")

    monkeypatch.setitem(
        sys.modules,
        "packages.providers.openrouter_speech",
        SimpleNamespace(MODEL=MODEL, openrouter_tts_settings=settings, OpenRouterTTSClient=TTS),
    )
    monkeypatch.setattr(meta_speech, "MetaSTTClient", STT)
    return calls


def tts_choice():
    return {"provider": "openrouter", "allow_cloud": True, "secret_ref": "openrouter"}


async def test_both_cloud_services_never_construct_local_voice_or_receive_service_token(
    monkeypatch, cloud
):
    monkeypatch.setattr(
        local_speech, "SpeechClient", lambda *a, **k: pytest.fail("unneeded local speech client")
    )
    voice = {
        "stt": {"provider": "meta", "allow_cloud": True, "secret_ref": "muse"},
        "tts": tts_choice(),
    }
    speech = converse_speech({"voice": voice}, "synthetic-private-service-token")
    assert isinstance(speech, CloudSTTLocalTTS) and speech.local is None
    assert speech.stt_info.provider == "meta" and speech.tts_info.provider == "openrouter"
    assert speech.profile == "Kokoro 82M (OpenRouter)"
    assert cloud == [
        ("stt", {"allow_cloud": True, "secret_ref": "muse"}),
        ("tts", {"allow_cloud": True, "secret_ref": "openrouter"}),
    ]
    await speech.close()
    assert cloud[-2:] == ["stt-closed", "tts-closed"]


async def test_local_stt_and_cloud_tts_keep_one_loopback_client_and_close_it_once(
    monkeypatch, cloud
):
    local = SimpleNamespace(profile="Kokoro 82M")
    closes = []

    async def close():
        closes.append("local")

    async def transcribe(data, filename):
        return {"text": "Synthetic local transcription."}

    local.close, local.transcribe = close, transcribe

    def make_local(url, token, **kwargs):
        assert url == "http://127.0.0.1:8765" and token == "synthetic-service-token"
        assert kwargs == {"profile": "Kokoro 82M"}
        return local

    monkeypatch.setattr(local_speech, "SpeechClient", make_local)
    speech = converse_speech(
        {"voice": {"url": "http://127.0.0.1:8765", "tts": tts_choice()}},
        "synthetic-service-token",
    )
    assert speech.local is speech.stt is local and speech.tts is not local
    assert speech.stt_info is None
    assert await speech.transcribe(b"fixture", "fixture.wav") == {
        "text": "Synthetic local transcription."
    }
    await speech.close()
    assert closes == ["local"] and cloud[-1] == "tts-closed"


def test_host_setup_declares_cloud_tts_without_revealing_host_configuration(cloud):
    host = host_setup({"voice": {"tts": tts_choice()}, "providers": {}})
    assert host.tts.selected_id == "openrouter:" + MODEL
    assert host.tts.label == "Kokoro 82M (OpenRouter cloud)"
    assert host.tts.configured and not host.tts.mutable
    assert host.stt.selected_id is None
    assert cloud == []  # Setup validation never constructs a client or reads a key.

    runtime = SimpleNamespace(
        provider=SimpleNamespace(
            info=ProviderInfo(provider="synthetic", model="local-fixture", local=True)
        ),
        text_status=lambda: {
            "active": 0,
            "cleanup_pending": 0,
            "cleanup_failed": 0,
            "admitting": 0,
            "capacity": 32,
        },
        speech_status=lambda: {"active": 0, "cancellation_pending": 0, "cleanup_failed": 0},
    )
    serialized = setup_state(runtime, host).model_dump_json()
    assert "secret_ref" not in serialized and "budget_path" not in serialized
