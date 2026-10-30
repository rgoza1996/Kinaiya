"""First-party local STT/TTS bridge; no voice dependencies in Agent installs."""

import json
import math
from contextlib import aclosing
from urllib.parse import urlsplit

import httpx

RETAINED_TTS = ("Ming 3 Tiny", "VibeVoice Realtime 0.5B", "Kokoro 82M", "Supertonic 3")


class SpeechProviderError(RuntimeError):
    def __init__(self, status_code):
        self.status_code = (
            status_code if status_code in {400, 409, 413, 415, 499, 503, 504} else 502
        )
        super().__init__("Speech provider request was refused or unavailable")


class SpeechClient:
    def __init__(
        self,
        base_url,
        token,
        *,
        profile="Kokoro 82M",
        transport=None,
        stt_url=None,
        stt_provider="whisper",
        stt_timeout_s=300,
        stt_transport=None,
    ):
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
        if (
            not isinstance(stt_provider, str)
            or stt_provider not in {"whisper", "whistle"}
            or (stt_provider == "whisper" and stt_url is not None)
        ):
            raise ValueError("Select an explicit managed Whistle STT target")
        managed_url = None
        if stt_provider == "whistle":
            from services.resources.client import local_base

            if (
                not isinstance(stt_url, str)
                or not isinstance(stt_timeout_s, (int, float))
                or isinstance(stt_timeout_s, bool)
                or not math.isfinite(stt_timeout_s)
                or not 30 <= stt_timeout_s <= 900
            ):
                raise ValueError("Whistle requires a local target and finite request deadline")
            managed_url = local_base(stt_url)
        self.profile = profile
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=120,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self.stt = (
            httpx.AsyncClient(
                base_url=managed_url,
                timeout=stt_timeout_s + 5,
                headers={"Authorization": f"Bearer {token}"},
                trust_env=False,
                follow_redirects=False,
                transport=stt_transport,
            )
            if managed_url is not None
            else None
        )

    async def transcribe(self, data, filename="recording.wav"):
        if len(data) > 10_000_000:
            raise ValueError("Audio exceeds 10 MB temporary processing limit")
        if self.stt is not None:
            async with self.stt.stream(
                "POST",
                "/v1/audio/transcriptions",
                content=data,
                headers={"Content-Type": "audio/wav"},
            ) as response:
                if not response.is_success:
                    raise SpeechProviderError(response.status_code)
                raw = bytearray()
                async for part in response.aiter_bytes():
                    if len(raw) + len(part) > 128 * 1024:
                        raise ValueError("Whistle response exceeds its bound")
                    raw.extend(part)
                from packages.providers.whistle_ipc import decode_json

                result = decode_json(bytes(raw), 128 * 1024)
                if (
                    not isinstance(result, dict)
                    or not isinstance(result.get("text"), str)
                    or len(result["text"]) > 16000
                ):
                    raise ValueError("Invalid bounded Whistle response")
                return result
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
        try:
            await self.http.aclose()
        finally:
            if self.stt is not None:
                await self.stt.aclose()
