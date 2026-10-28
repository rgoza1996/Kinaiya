"""First-party local STT/TTS bridge; no voice dependencies in Agent installs."""

import json
from contextlib import aclosing
from urllib.parse import urlsplit

import httpx

RETAINED_TTS = ("Ming 3 Tiny", "VibeVoice Realtime 0.5B", "Kokoro 82M", "Supertonic 3")


class SpeechClient:
    def __init__(self, base_url, token, *, profile="Kokoro 82M", transport=None):
        # This token is a Kinaiya service credential, not a cloud API key.
        # Remote adapters need their own scoped credentials and disclosure policy.
        if not isinstance(base_url, str):
            raise ValueError("Local voice requires a loopback HTTP origin")
        origin = urlsplit(base_url)
        if (
            origin.scheme != "http"
            or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
        ):
            raise ValueError("Local voice requires a loopback HTTP origin")
        if profile not in RETAINED_TTS:
            raise ValueError("Unknown retained TTS profile")
        self.profile = profile
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=120,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
            transport=transport,
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
            async with aclosing(response.aiter_lines()) as lines:
                async for line in lines:
                    if line:
                        yield json.loads(line)

    async def close(self):
        await self.http.aclose()
