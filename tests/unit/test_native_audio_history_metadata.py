"""Negotiated representation is retained with the original native reply."""

import base64
import json

import pytest
from test_native_audio_converse import (
    OUTPUT_PCM,
    NativeModel,
    environment,
    released,
    request_speech,
)

from packages.providers.realtime_audio import NativeAudioReply


@pytest.mark.parametrize("mode", ["system_json", "native_roles_v1"])
async def test_native_history_representation_stays_with_actual_audio_and_source(tmp_path, mode):
    class HistoryModel(NativeModel):
        async def respond(self, audio, messages, *, current):
            reply = await super().respond(audio, messages, current=current)
            return NativeAudioReply(reply.text, reply.pcm16, reply.rate, mode)

    async with environment(tmp_path, native=HistoryModel()) as env:
        reply = await released(env)
        assert reply.payload["native_audio_history"] == mode
        response = await request_speech(env, reply)
        assert response.status_code == 200
        frames = [json.loads(line) for line in response.text.splitlines() if line]
        assert b"".join(base64.b64decode(frame["audio"]) for frame in frames) == OUTPUT_PCM
        assert all(frame["event_id"] == reply.event_id for frame in frames)
        assert env.store.read(kinds=["delivery.updated"]) == []
