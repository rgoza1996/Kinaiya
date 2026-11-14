"""Capacity diagnostics use only public synthetic PCM and the native fixture.

Exhaustion describes new original admission, not provider readiness or pending
cleanup. Existing originals remain retained and follow the same replay rules.
"""

import pytest
from test_native_audio_converse import admit, body, environment, wav

from packages.providers.speech import SpeechProviderError
from packages.providers.text import ProviderRefusal
from services.converse import native_audio


def expected_capacity(clips, reserved, *, max_clips=32, max_bytes=16 * 1024 * 1024):
    minimum = 48 + 2 * 1024 * 1024
    return {
        "retained_clips": clips,
        "max_clips": max_clips,
        "reserved_bytes": reserved,
        "max_reserved_bytes": max_bytes,
        "remaining_clips": max_clips - clips,
        "remaining_bytes": max_bytes - reserved,
        "min_clip_reserved_bytes": minimum,
        "exhausted": clips >= max_clips or max_bytes - reserved < minimum,
    }


async def test_empty_native_capacity_is_bounded_metadata_only(tmp_path):
    async with environment(tmp_path) as env:
        status = (await env.http.get("/health")).json()["native_audio"]
        assert status["available"] is True
        assert status["capacity"] == expected_capacity(0, 0)
        assert all(
            type(value) is int and 0 <= value <= 16 * 1024 * 1024
            for key, value in status["capacity"].items()
            if key != "exhausted"
        )
        assert type(status["capacity"]["exhausted"]) is bool
        assert env.native.transcriptions == []
        assert env.owner.db.execute("SELECT count(*) FROM native_audio_intents").fetchone()[0] == 0


async def test_settled_capacity_exhaustion_is_unavailable_without_pending_or_eviction(tmp_path):
    async with environment(tmp_path) as env:
        requests = [body(env, pcm=b"\x00\x00") for _ in range(8)]
        original = None
        for request in requests[:7]:
            result = await admit(env, request)
            original = original or result
        status = (await env.http.get("/health")).json()["native_audio"]
        assert status["capacity"] == expected_capacity(7, 7 * (48 + 2 * 1024 * 1024))
        assert status["capacity"]["exhausted"] is True
        assert status["available"] is False
        assert status["configured"] is True
        assert status["active"] == 0 and status["pending"] is False
        assert status["cleanup_failed"] is False
        refused = await env.http.post("/v1/converse/native-audio/clips", json=requests[7])
        assert refused.status_code == 503
        assert refused.json() == {"detail": "Native audio is unavailable"}
        assert refused.headers["cache-control"] == "no-store"
        assert env.owner.db.execute("SELECT count(*) FROM native_audio_intents").fetchone()[0] == 7
        replay = await admit(env, requests[0])
        assert replay["replay"] is True and replay["entry"] == original["entry"]
        assert len(env.native.transcriptions) == 7
        assert env.owner.status()["capacity"] == status["capacity"]


async def test_completed_refusals_keep_the_original_capacity_reservation(tmp_path):
    async with environment(tmp_path) as env:
        env.native.asr_error = ProviderRefusal("Public synthetic completed refusal")
        requests = [body(env, pcm=b"\x00\x00") for _ in range(8)]
        for request in requests[:7]:
            with pytest.raises(SpeechProviderError) as refused:
                await admit(env, request)
            assert refused.value.status_code == 503
        status = env.owner.status()
        assert status["capacity"] == expected_capacity(7, 7 * (48 + 2 * 1024 * 1024))
        assert status["available"] is False
        assert status["active"] == 0 and status["pending"] is False
        assert status["cleanup_failed"] is False
        with pytest.raises(SpeechProviderError) as refused:
            await admit(env, requests[7])
        assert refused.value.status_code == 503
        assert len(env.native.transcriptions) == 7
        assert env.owner.db.execute("SELECT count(*) FROM native_audio_intents").fetchone()[0] == 7


@pytest.mark.parametrize("headroom", [-1, 0, 1])
async def test_availability_obeys_the_exact_minimum_clip_byte_boundary(
    tmp_path, monkeypatch, headroom
):
    minimum = len(wav(b"\x00\x00")) + 2 + 2 * 1024 * 1024
    maximum = minimum + headroom
    monkeypatch.setattr(native_audio, "MAX_RETAINED_BYTES", maximum)
    async with environment(tmp_path) as env:
        status = env.owner.status()
        assert status["capacity"] == expected_capacity(0, 0, max_bytes=maximum)
        assert status["available"] is (headroom >= 0)
        if headroom < 0:
            with pytest.raises(SpeechProviderError) as refused:
                await admit(env, body(env, pcm=b"\x00\x00"))
            assert refused.value.status_code == 503
            assert env.native.transcriptions == []
        else:
            await admit(env, body(env, pcm=b"\x00\x00"))
            status = env.owner.status()
            assert status["capacity"] == expected_capacity(1, minimum, max_bytes=maximum)
            assert status["available"] is False


async def test_clip_count_exhaustion_is_visible_even_with_byte_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(native_audio, "MAX_CLIPS", 1)
    async with environment(tmp_path) as env:
        request = body(env, pcm=b"\x00\x00")
        await admit(env, request)
        status = env.owner.status()
        assert status["capacity"] == expected_capacity(1, 48 + 2 * 1024 * 1024, max_clips=1)
        assert status["capacity"]["remaining_bytes"] > status["capacity"]["min_clip_reserved_bytes"]
        assert status["available"] is False
        with pytest.raises(SpeechProviderError) as refused:
            await admit(env, body(env, pcm=b"\x00\x00"))
        assert refused.value.status_code == 503
        assert len(env.native.transcriptions) == 1
        assert (await admit(env, request))["replay"] is True
