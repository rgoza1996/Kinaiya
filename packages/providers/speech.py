"""Interchangeable STT/TTS HTTP adapters; no voice dependencies in Agent installs."""

import json

import httpx

RETAINED_TTS = ("Ming 3 Tiny", "VibeVoice Realtime 0.5B", "Kokoro 82M", "Supertonic 3")


class SpeechClient:
    def __init__(self, base_url, token, *, profile="Kokoro 82M"):
        if profile not in RETAINED_TTS:
            raise ValueError("Unknown retained TTS profile")
        self.profile = profile
        self.http = httpx.AsyncClient(
            base_url=base_url, timeout=120, headers={"Authorization": f"Bearer {token}"}
        )

    async def transcribe(self, data, filename="recording.wav"):
        if len(data) > 10_000_000:
            raise ValueError("Audio exceeds 10 MB temporary processing limit")
        response = await self.http.post(
            "/v1/audio/transcriptions", files={"file": (filename, data)}
        )
        response.raise_for_status()
        return response.json()

    async def synthesize(self, text):
        async with self.http.stream(
            "POST", "/v1/audio/speech", json={"input": text, "profile": self.profile}
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    yield json.loads(line)

    async def close(self):
        await self.http.aclose()
