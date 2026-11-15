"""Replay the original real-engine history frames without inference or network."""

import base64
import gzip
import hashlib
import json
from pathlib import Path

from packages.providers.realtime_audio import NativeAudioInput, RealtimeAudioProvider


async def test_original_native_history_capture_matches_current_provider_exactly():
    root = Path(__file__).resolve().parents[1] / "fixtures/native_history"
    metadata = json.loads((root / "omni-native-roles.json").read_text())
    with gzip.open(root / "omni-native-roles.jsonl.gz", "rb") as source:
        raw = source.read(2 * 1024 * 1024 + 1)
    assert len(raw) <= 2 * 1024 * 1024
    assert len(raw) == metadata["raw_bytes"]
    assert hashlib.sha256(raw).hexdigest() == metadata["raw_sha256"]
    rows = [json.loads(line) for line in raw.decode().splitlines()]
    sent = [row["raw"] for row in rows if row["direction"] == "send"]
    received = [row["raw"] for row in rows if row["direction"] == "recv"]
    selected = next(
        json.loads(row)["session"] for row in sent if json.loads(row)["type"] == "session.update"
    )
    pcm = b"".join(
        base64.b64decode(json.loads(row)["audio"])
        for row in sent
        if json.loads(row)["type"] == "input_audio_buffer.append"
    )
    assert hashlib.sha256(pcm).hexdigest() == metadata["input_pcm_sha256"]
    calls = []

    class Replay:
        send_index = receive_index = close_calls = 0

        async def send(self, value):
            assert value == sent[self.send_index]
            self.send_index += 1

        async def recv(self):
            value = received[self.receive_index]
            self.receive_index += 1
            return value

        async def close(self):
            self.close_calls += 1

    replay = Replay()

    async def connect(uri, **options):
        calls.append((uri, options))
        return replay

    async def current():
        return True

    provider = RealtimeAudioProvider(
        "http://127.0.0.1:19451", "history-qualification", connect=connect
    )
    try:
        reply = await provider.respond(
            NativeAudioInput(pcm, 16000),
            [
                {"role": "system", "content": selected["instructions"]},
                *selected["history"],
                {"role": "user", "content": "SIDECAR-MUST-NOT-BE-SENT-626"},
            ],
            current=current,
        )
        assert reply.history_mode == metadata["history_mode"] == "native_roles_v1"
        assert reply.rate == metadata["sample_rate"]
        assert hashlib.sha256(reply.text.encode()).hexdigest() == metadata["output_text_sha256"]
        assert hashlib.sha256(reply.pcm16).hexdigest() == metadata["output_pcm_sha256"]
        assert replay.send_index == len(sent) and replay.receive_index == len(received)
        assert replay.close_calls == 1 and len(calls) == 1
    finally:
        await provider.close()
