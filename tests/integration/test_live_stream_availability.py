"""#681/#623: native protocol availability is not generic STT configuration."""

from types import SimpleNamespace

import httpx
import pytest

from packages.providers.whistle_stream_owner import CapturedWhistleStream
from services.converse.live_stream_api import BASE
from services.converse.stt_model_settings import STTModelSettings
from tests.integration.test_incremental_whistle_converse import (
    converse_incremental_env as converse_incremental_env,
)
from tests.integration.test_live_stream_api import live_client


@pytest.mark.parametrize("kind", ["supported", "plain", "closed", "replaced"])
async def test_availability_uses_selected_original_protocol_without_stt_effects(
    converse_incremental_env,
    kind,
):
    env = converse_incremental_env
    calls = []

    async def transcribe(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Public synthetic availability entered transcription")

    provider = SimpleNamespace(transcribe=transcribe) if kind == "plain" else env.speech
    settings = STTModelSettings(env.runtime, None, provider)
    env.runtime.stt_model_settings = settings
    env.runtime._stt_options_configured = True
    env.runtime._stt_options_unavailable = False
    assert settings._host_available()
    original_client, replacement = env.speech.stt, None
    if kind == "plain":
        with pytest.raises(ValueError):
            CapturedWhistleStream(provider)
    elif kind == "closed":
        await original_client.aclose()
    elif kind == "replaced":
        replacement = httpx.AsyncClient(base_url="http://127.0.0.1:9", trust_env=False)
        env.speech.stt = replacement
    selection = settings.selection()
    try:
        async with live_client(env) as live:
            for suffix in ("/ownership", "/next-utterance"):
                response = await live.client.get(BASE + suffix)
                assert response.status_code == 200
                assert calls == [] and not settings.leases
                assert not live.app.state.live_streams.captures and env.engine.begin_calls == 0
                assert settings.selection() == selection
                if kind == "plain":
                    assert settings._host_available()  # buffered/legacy selection remains intact
                assert response.json()["available"] is (kind == "supported")
    finally:
        if replacement is not None:
            await replacement.aclose()
            env.speech.stt = original_client
        await settings.close()
