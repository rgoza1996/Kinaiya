"""Explicit buffered Meta STT, composed with independently local TTS.

Audio is transient. Decoding denies nested protocols and normalizes actual
samples, rather than labelling a WebM upload as WAV. No model runtime is loaded.
"""

import asyncio
import importlib
import inspect
import io
import json
import wave
from fractions import Fraction
from urllib.parse import urlsplit

import httpx

from packages.contracts.models import ProviderInfo
from packages.providers.disclosure import scope_allowed
from packages.providers.speech import SpeechProviderError
from packages.providers.text import (
    ProviderCleanupError,
    _meta_credential,
    _OwnedHTTPStream,
    _reject_compressed_response,
)
from packages.providers.whistle_ipc import decode_json

MODEL = "muse-voice-transcribe-1.0"
MAX_AUDIO_BYTES = 10_000_000
MAX_SAMPLES = 60 * 16000


def _validate_origin(base_url, timeout_s):
    if not isinstance(base_url, str):
        raise ValueError("Select an explicitly authorized Meta STT origin")
    origin = urlsplit(base_url)
    if (
        origin.scheme not in {"http", "https"}
        or (origin.scheme != "https" and origin.hostname not in {"127.0.0.1", "localhost", "::1"})
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path.rstrip("/") != "/v1"
        or type(timeout_s) not in (int, float)
        or not 1 <= timeout_s <= 300
    ):
        raise ValueError("Select an explicitly authorized Meta STT origin and bounded deadline")


def meta_stt_settings(choice):
    """Validate the explicit cloud selection without reading credentials."""
    if (
        type(choice) is not dict
        or set(choice) - {"provider", "base_url", "model", "allow_cloud", "secret_ref", "timeout_s"}
        or choice.get("provider") != "meta"
        or choice.get("allow_cloud") is not True
        or choice.get("secret_ref") != "muse"
        or choice.get("model", MODEL) != MODEL
    ):
        raise ValueError(
            "Meta STT requires explicit cloud authorization and its own secret reference"
        )
    result = dict(choice)
    result.pop("provider")
    _validate_origin(result.get("base_url", "https://api.meta.ai/v1"), result.get("timeout_s", 120))
    return result


def normalize_wav(data):
    """Bound both compressed input and decoded duration before cloud export."""
    if type(data) is not bytes or not data or len(data) > MAX_AUDIO_BYTES:
        raise SpeechProviderError(413)
    try:
        av = importlib.import_module("av")
    except ImportError as exc:
        raise SpeechProviderError(503) from exc
    pcm, samples, duration = io.BytesIO(), 0, Fraction(0)
    try:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

        def append(frames):
            nonlocal samples
            for frame in frames:
                count = frame.samples
                if type(count) is not int or count < 0 or samples + count > MAX_SAMPLES:
                    raise SpeechProviderError(413)
                raw = frame.to_ndarray().tobytes()
                if len(raw) != count * 2:
                    raise SpeechProviderError(415)
                pcm.write(raw)
                samples += count

        with av.open(
            io.BytesIO(data),
            mode="r",
            metadata_errors="ignore",
            options={"protocol_whitelist": ""},
        ) as container:
            for frame in container.decode(audio=0):
                count, rate = frame.samples, frame.sample_rate
                if type(count) is not int or count < 0 or type(rate) is not int or rate <= 0:
                    raise SpeechProviderError(415)
                duration += Fraction(count, rate)
                if duration > 60:
                    raise SpeechProviderError(413)
                frame.pts = None
                append(resampler.resample(frame))
            append(resampler.resample(None))
        if not samples:
            raise SpeechProviderError(415)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm.getvalue())
        return output.getvalue()
    except SpeechProviderError:
        raise
    except Exception as exc:
        raise SpeechProviderError(415) from exc


async def _owned_decode(data):
    # A cancelled to_thread await does not stop its decoder. Retain the real
    # worker until it exits so the API's bounded owner registry stays truthful.
    task = asyncio.create_task(asyncio.to_thread(normalize_wav, data))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        task.exception()  # retrieve a decoder failure without exporting audio
        raise asyncio.CancelledError
    return task.result()


class MetaSTTClient:
    def __init__(
        self,
        *,
        base_url="https://api.meta.ai/v1",
        api_key="",
        secret_ref=None,
        model=MODEL,
        allow_cloud=False,
        timeout_s=120,
        transport=None,
    ):
        _validate_origin(base_url, timeout_s)
        if allow_cloud is not True or model != MODEL:
            raise ValueError("Select the explicitly authorized Meta STT model")
        if secret_ref == "muse":
            api_key = _meta_credential()
        elif secret_ref is not None:
            raise ValueError("Unknown Meta STT secret reference")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("Meta STT credential is unavailable")
        self.info = ProviderInfo(provider="meta", model=MODEL, local=False, streaming=False)
        self.timeout_s = timeout_s
        self._cleanup_failed = False
        # Voice is Standard-only: never copy the text Contributor header or a
        # Kinaiya service credential into this independently owned transport.
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def transcribe(
        self, data, filename="recording.wav", *, content_scope="local_only", current=None
    ):
        info = self.info.model_dump()
        client, origin, headers = self.http, str(self.http.base_url), dict(self.http.headers)

        def bound():
            return not (
                self._cleanup_failed
                or not scope_allowed(self.info, content_scope)
                or self.info.model_dump() != info
                or self.http is not client
                or client.is_closed
                or str(client.base_url) != origin
                or dict(client.headers) != headers
                or current is None
            )

        async def check():
            if not bound():
                raise SpeechProviderError(409)
            value = current()
            if inspect.isawaitable(value):
                value = await value
            if value is not True or not bound():
                raise SpeechProviderError(409)

        await check()
        try:
            async with asyncio.timeout(self.timeout_s):
                wav = await _owned_decode(data)
                await check()
                async with client.stream(
                    "POST",
                    "asr/transcribe",
                    files={
                        "request": (
                            None,
                            json.dumps(
                                {"model": MODEL, "mode": "PUSH_TO_TALK", "audioEncoding": "WAV"}
                            ),
                            "application/json",
                        ),
                        "audio": ("recording.wav", wav, "audio/wav"),
                    },
                ) as response:
                    response.stream = _OwnedHTTPStream(response.stream)
                    if response.status_code != 200:
                        raise SpeechProviderError(response.status_code)
                    _reject_compressed_response(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 128 * 1024:
                            raise SpeechProviderError(502)
                        raw.extend(chunk)
                await check()
                result = decode_json(bytes(raw), 128 * 1024)
                if (
                    type(result) is not dict
                    or set(result) != {"sessionId", "transcript", "audioDurationMs", "turns"}
                    or type(result["sessionId"]) is not str
                    or not 1 <= len(result["sessionId"].encode("utf-8")) <= 256
                    or type(result["transcript"]) is not str
                    or len(result["transcript"]) > 16000
                    or len(result["transcript"].encode("utf-8")) > 64000
                    or type(result["audioDurationMs"]) is not int
                    or not 0 <= result["audioDurationMs"] <= 60000
                    or result["turns"] != []
                ):
                    raise SpeechProviderError(502)
                return {
                    "text": result["transcript"],
                    "provider": "meta",
                    "model": MODEL,
                    "audio_duration_ms": result["audioDurationMs"],
                }
        except ProviderCleanupError as exc:
            self._cleanup_failed = True
            raise SpeechProviderError(503) from exc
        except (SpeechProviderError, asyncio.CancelledError):
            raise
        except TimeoutError as exc:
            raise SpeechProviderError(504) from exc
        except Exception as exc:
            raise SpeechProviderError(502) from exc

    async def close(self):
        await self.http.aclose()


class CloudSTTLocalTTS:
    """Composition only; no local worker or GPU ownership is invented for STT."""

    def __init__(self, local, stt, *, resource_state=None):
        self.local, self.stt = local, stt
        self.profile = local.profile
        self.resource_state = resource_state

    @property
    def stt_info(self):
        return self.stt.info

    async def transcribe(self, data, filename="recording.wav", **kwargs):
        from services.resources.state import read_record

        state_path = self.resource_state

        def policy():
            if state_path is None:
                return None
            try:
                state = read_record(state_path)
            except Exception as exc:
                raise SpeechProviderError(503) from exc
            if (
                state.profile == "local_only"
                or not state.desired_converse
                or state.phase != "ready"
            ):
                raise SpeechProviderError(409)
            return state.revision

        revision = policy()
        native_current = kwargs.get("current")

        async def current():
            if self.resource_state != state_path or policy() != revision or native_current is None:
                return False
            value = native_current()
            valid = await value if inspect.isawaitable(value) else value
            return valid is True and self.resource_state == state_path and policy() == revision

        return await self.stt.transcribe(data, filename, **{**kwargs, "current": current})

    def synthesize(self, text):
        return self.local.synthesize(text)

    async def close(self):
        try:
            await self.stt.close()
        finally:
            await self.local.close()
