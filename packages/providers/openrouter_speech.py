"""OpenRouter Kokoro TTS adapter (issue #146).

Explicit opt-in cloud TTS only. The exact model ``hexgrad/kokoro-82m`` is
synthesized through the exact ``https://openrouter.ai/api/v1`` origin (a
loopback ``/api/v1`` origin is accepted for deterministic local tests),
returning raw 24 kHz mono signed-16-bit little-endian PCM framed as native
audio events. There are no retries and no fallback.
"""

import asyncio
import base64
import inspect
import math
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from packages.contracts.models import ProviderInfo
from packages.providers.disclosure import scope_allowed
from packages.providers.openrouter_budget import CharacterBudget
from packages.providers.speech import SpeechProviderError
from packages.providers.text import (
    ProviderCleanupError,
    ProviderError,
    _OwnedHTTPStream,
    _reject_compressed_response,
)

MODEL = "hexgrad/kokoro-82m"
VOICE = "af_heart"
PROFILE = "Kokoro 82M (OpenRouter)"
SAMPLE_RATE = 24000
MAX_INPUT_BYTES = 4096
MAX_AUDIO_BYTES = 120 * SAMPLE_RATE * 2  # 120 s of 24 kHz mono s16le PCM
MAX_FRAME_SAMPLES = 2400  # 100 ms, at most 1200 frames per request
_SETTINGS_KEYS = frozenset(
    {
        "provider",
        "allow_cloud",
        "secret_ref",
        "model",
        "voice",
        "base_url",
        "timeout_s",
        "budget_path",
        "budget_limit_usd",
    }
)
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _validate_origin(base_url, timeout_s):
    """Allow only the exact OpenRouter origin or a loopback /api/v1 test origin."""
    if not isinstance(base_url, str):
        raise ValueError("Select the explicitly authorized OpenRouter TTS origin")
    try:
        origin = urlsplit(base_url)
        port = origin.port
    except ValueError as exc:
        raise ValueError("Select the explicitly authorized OpenRouter TTS origin") from exc
    if (
        origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or not origin.hostname
        or origin.path.rstrip("/") != "/api/v1"
    ):
        raise ValueError("Select the explicitly authorized OpenRouter TTS origin")
    if origin.scheme == "https" and origin.hostname == "openrouter.ai" and port in (None, 443):
        pass
    elif origin.scheme == "http" and origin.hostname in _LOOPBACK:
        pass
    else:
        raise ValueError("Select the explicitly authorized OpenRouter TTS origin")
    if (
        type(timeout_s) not in (int, float)
        or not math.isfinite(timeout_s)
        or not 1 <= timeout_s <= 120
    ):
        raise ValueError("OpenRouter TTS requires a finite deadline of at most 120 s")


def _openrouter_credential():
    """Read the OpenRouter key from its private file reference only."""
    try:
        override = os.environ.get("KINAIYA_OPENROUTER_KEY_FILE")
        if override:
            path = Path(override)
        else:
            path = (
                Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
                / "kinaiya"
                / "openrouter.key"
            )
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > 65536
            ):
                raise ValueError("Unsafe credential reference")
            data = os.read(fd, 65537)
            if len(data) > 65536:
                raise ValueError("Unsafe credential reference")
            key = data.decode("utf-8").strip()
            if not key or len(key) > 16384 or any(mark in key for mark in ("\0", "\r", "\n")):
                raise ValueError("Invalid credential")
            return key
        finally:
            os.close(fd)
    except Exception as exc:
        raise ValueError("OpenRouter credential is unavailable") from exc


def openrouter_tts_settings(choice):
    """Validate the explicit cloud TTS selection without reading credentials."""
    if (
        type(choice) is not dict
        or set(choice) - _SETTINGS_KEYS
        or choice.get("provider") != "openrouter"
        or choice.get("allow_cloud") is not True
        or choice.get("secret_ref") != "openrouter"
    ):
        raise ValueError(
            "OpenRouter TTS requires explicit cloud authorization and its own secret reference"
        )
    result = dict(choice)
    result.pop("provider")
    result.setdefault("model", MODEL)
    result.setdefault("voice", VOICE)
    result.setdefault("base_url", "https://openrouter.ai/api/v1")
    result.setdefault("timeout_s", 120)
    result.setdefault("budget_limit_usd", 1.0)
    if result.get("model") != MODEL or result.get("voice") != VOICE:
        raise ValueError("Select the explicitly authorized OpenRouter TTS model and voice")
    if (
        type(result.get("budget_path")) is not str
        or not Path(result["budget_path"]).is_absolute()
        or ".." in Path(result["budget_path"]).parts
    ):
        raise ValueError("OpenRouter TTS requires an explicit budget path")
    _validate_origin(result.get("base_url"), result.get("timeout_s"))
    limit = result.get("budget_limit_usd")
    if type(limit) not in (int, float) or not math.isfinite(limit) or not 0 < limit <= 25:
        raise ValueError("OpenRouter TTS requires a finite positive budget limit")
    return result


def _check_audio_headers(response):
    """Accept unambiguous uncompressed mono PCM headers only."""
    try:
        _reject_compressed_response(response)
    except ProviderError as exc:
        raise SpeechProviderError(415) from exc
    values = response.headers.get_list("content-type")
    if len(values) != 1 or len(values[0]) > 256:
        raise SpeechProviderError(415)
    parts = [part.strip() for part in values[0].split(";")]
    if parts[0].lower() != "audio/pcm":
        raise SpeechProviderError(415)
    seen = set()
    for param in parts[1:]:
        name, sep, value = param.partition("=")
        name, value = name.strip().lower(), value.strip()
        expected = {"rate": "24000", "channels": "1"}.get(name)
        if (
            not sep
            or name in seen
            or expected is None
            or value not in {expected, '"' + expected + '"'}
        ):
            raise SpeechProviderError(415)
        seen.add(name)


def _declared_length(response):
    lengths = response.headers.get_list("content-length")
    transfer = response.headers.get("transfer-encoding")
    if len(lengths) > 1 or (lengths and transfer is not None):
        raise SpeechProviderError(502)
    if transfer is not None and transfer.lower() != "chunked":
        raise SpeechProviderError(502)
    if not lengths:
        return None
    raw = lengths[0].strip()
    if re.fullmatch(r"[0-9]{1,8}", raw) is None:
        raise SpeechProviderError(502)
    length = int(raw)
    if length > MAX_AUDIO_BYTES:
        raise SpeechProviderError(413)
    return length


def _generation_id(response):
    value = response.headers.get("x-generation-id")
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) else None
    )


class OpenRouterTTSClient:
    """Explicit OpenRouter Kokoro speech client yielding native PCM frames."""

    def __init__(
        self,
        *,
        base_url="https://openrouter.ai/api/v1",
        model=MODEL,
        voice=VOICE,
        allow_cloud=False,
        secret_ref=None,
        timeout_s=120,
        budget_path=None,
        budget_limit_usd=1.0,
        require_existing_budget=False,
        api_key="",
        transport=None,
    ):
        _validate_origin(base_url, timeout_s)
        if allow_cloud is not True or model != MODEL or voice != VOICE:
            raise ValueError("Select the explicitly authorized OpenRouter TTS model and voice")
        if (
            budget_path is None
            or not isinstance(budget_path, (str, os.PathLike))
            or not str(budget_path)
        ):
            raise ValueError("OpenRouter TTS requires an explicit budget path")
        if (
            type(budget_limit_usd) not in (int, float)
            or not math.isfinite(budget_limit_usd)
            or not budget_limit_usd > 0
        ):
            raise ValueError("OpenRouter TTS requires a finite positive budget limit")
        if isinstance(api_key, str) and api_key:
            # Nonempty api_key only for synthetic embedded tests.
            if len(api_key) > 16384 or any(mark in api_key for mark in ("\0", "\r", "\n")):
                raise ValueError("OpenRouter credential is unavailable")
        elif secret_ref == "openrouter" and not api_key:
            api_key = _openrouter_credential()
        else:
            raise ValueError("OpenRouter credential is unavailable")
        self._model = model
        self._voice = voice
        self.timeout_s = timeout_s
        self._transport = transport
        self._budget = CharacterBudget(
            budget_path, limit_usd=budget_limit_usd, require_existing=require_existing_budget
        )
        self._cleanup_failed = False
        self._closed = False
        self.info = ProviderInfo(provider="openrouter", model=MODEL, local=False, streaming=True)
        self.profile = PROFILE
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept-Encoding": "identity",
            },
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def synthesize_scoped(
        self, text, *, content_scope="local_only", current=None, before_export=None
    ):
        info = self.info.model_dump()
        client, origin, headers = self.http, str(self.http.base_url), dict(self.http.headers)
        _validate_origin(origin, self.timeout_s)
        config = (self._model, self._voice, self.timeout_s)
        transport, budget = client._transport, self._budget
        mounts, auth = tuple(client._mounts.items()), client.auth
        hooks = {name: tuple(items) for name, items in client.event_hooks.items()}
        budget_config = (budget.path, budget.limit, budget.require_existing)

        def bound():
            return not (
                self._cleanup_failed
                or self._closed
                or info
                != {
                    "schema_version": "1.0",
                    "provider": "openrouter",
                    "model": MODEL,
                    "local": False,
                    "streaming": True,
                }
                or config[:2] != (MODEL, VOICE)
                or not scope_allowed(self.info, content_scope)
                or self.info.model_dump() != info
                or self.http is not client
                or client.is_closed
                or str(client.base_url) != origin
                or dict(client.headers) != headers
                or client._transport is not transport
                or tuple(client._mounts.items()) != mounts
                or client.follow_redirects is not False
                or client.auth is not auth
                or {name: tuple(items) for name, items in client.event_hooks.items()} != hooks
                or (self._model, self._voice, self.timeout_s) != config
                or self._budget is not budget
                or (budget.path, budget.limit, budget.require_existing) != budget_config
                or current is None
            )

        async def check():
            if not bound():
                raise SpeechProviderError(409)
            try:
                value = current()
                if inspect.isawaitable(value):
                    value = await value
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise SpeechProviderError(409) from exc
            if value is not True or not bound():
                raise SpeechProviderError(409)

        await check()
        if type(text) is not str or not text:
            raise SpeechProviderError(400)
        try:
            text_bytes = len(text.encode("utf-8"))
        except UnicodeError as exc:
            raise SpeechProviderError(400) from exc
        if text_bytes > MAX_INPUT_BYTES:
            raise SpeechProviderError(413)
        await check()
        reservation = budget.admit(text)
        if not isinstance(reservation, str) or not reservation:
            raise SpeechProviderError(502)
        # Ledger fsync can race an external Sleep or policy write. Keep the full
        # reservation, but revalidate native admission before any HTTP export.
        await check()
        body = {"model": config[0], "input": text, "voice": config[1], "response_format": "pcm"}
        if before_export is not None:
            if not callable(before_export):
                raise SpeechProviderError(409)
            before_export()
        try:
            async with asyncio.timeout(self.timeout_s):
                async with client.stream("POST", "audio/speech", json=body) as response:
                    # Install ownership before checks, iteration or any await.
                    # HTTPX EOF and context exit then share the same real close.
                    response.stream = _OwnedHTTPStream(response.stream)
                    await check()
                    if response.status_code != 200:
                        raise SpeechProviderError(response.status_code)
                    _check_audio_headers(response)
                    declared, generation = _declared_length(response), _generation_id(response)
                    total, pending, chunk_index = 0, b"", 0
                    async for part in response.aiter_raw():
                        if not part:
                            continue
                        total += len(part)
                        if total > MAX_AUDIO_BYTES or (declared is not None and total > declared):
                            raise SpeechProviderError(413)
                        data = pending + part
                        full = len(data) // (2 * MAX_FRAME_SAMPLES) * (2 * MAX_FRAME_SAMPLES)
                        for offset in range(0, full, 2 * MAX_FRAME_SAMPLES):
                            frame = data[offset : offset + 2 * MAX_FRAME_SAMPLES]
                            if chunk_index == 0 and frame.startswith(
                                (b"RIFF", b"ID3", b"OggS", b"fLaC", b'{"')
                            ):
                                raise SpeechProviderError(415)
                            await check()
                            yield {
                                "type": "audio",
                                "audio": base64.b64encode(frame).decode("ascii"),
                                "sample_rate": SAMPLE_RATE,
                                "samples": len(frame) // 2,
                                "chunk_index": chunk_index,
                            }
                            chunk_index += 1
                        pending = data[full:]
                    if (
                        not total
                        or len(pending) % 2
                        or (declared is not None and total != declared)
                    ):
                        raise SpeechProviderError(502)
                    if pending:
                        if chunk_index == 0 and pending.startswith(
                            (b"RIFF", b"ID3", b"OggS", b"fLaC", b'{"')
                        ):
                            raise SpeechProviderError(415)
                        await check()
                        yield {
                            "type": "audio",
                            "audio": base64.b64encode(pending).decode("ascii"),
                            "sample_rate": SAMPLE_RATE,
                            "samples": len(pending) // 2,
                            "chunk_index": chunk_index,
                        }
                await check()
                budget.finish(reservation, generation)
        except ProviderCleanupError as exc:
            self._cleanup_failed = True
            raise SpeechProviderError(503) from exc
        except (SpeechProviderError, asyncio.CancelledError, GeneratorExit):
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise SpeechProviderError(504) from exc
        except Exception as exc:
            raise SpeechProviderError(502) from exc

    async def synthesize(self, text):
        raise SpeechProviderError(409)
        yield  # pragma: no cover - fails closed without native context

    async def close(self):
        self._closed = True
        await self.http.aclose()
