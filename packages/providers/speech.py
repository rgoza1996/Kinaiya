"""First-party local STT/TTS bridge; no voice dependencies in Agent installs."""

import asyncio
import hashlib
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
        host = f"[{origin.hostname}]" if origin.hostname == "::1" else origin.hostname
        self._native_stt_origin = f"http://{host}:{origin.port or 80}"
        self._native_closes = set()
        self._native_exchanges = set()
        self._cleanup_failed = False
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=120,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._native_http = self.http
        self._native_origin_identity = self._native_stt_origin
        self._native_http_identity = (
            str(self.http.base_url),
            dict(self.http.headers),
            self.http._transport,
            tuple(self.http._mounts.items()),
            self.http.auth,
            {name: tuple(items) for name, items in self.http.event_hooks.items()},
            self.http.timeout.as_dict(),
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

    def _native_bound(self):
        client = self._native_http
        try:
            return (
                self.http is client
                and not client.is_closed
                and self._native_stt_origin == self._native_origin_identity
                and client.follow_redirects is False
                and client.trust_env is False
                and (
                    str(client.base_url),
                    dict(client.headers),
                    client._transport,
                    tuple(client._mounts.items()),
                    client.auth,
                    {name: tuple(items) for name, items in client.event_hooks.items()},
                    client.timeout.as_dict(),
                )
                == self._native_http_identity
            )
        except Exception:
            return False

    async def _native_exchange(
        self, path, *, body=None, data=None, headers=None, timeout=5, current=None
    ):
        if len(self._native_exchanges) >= 40 or not self._native_bound():
            raise SpeechProviderError(503)
        task = asyncio.create_task(
            self._native_exchange_owned(
                path, body=body, data=data, headers=headers, timeout=timeout, current=current
            )
        )
        self._native_exchanges.add(task)

        def settled(completed):
            self._native_exchanges.discard(completed)
            if completed.cancelled():
                self._cleanup_failed = True
            else:
                completed.exception()

        task.add_done_callback(settled)
        result = await asyncio.shield(task)
        if not self._native_bound():
            raise SpeechProviderError(409)
        return result

    async def _native_exchange_owned(
        self, path, *, body=None, data=None, headers=None, timeout=5, current=None
    ):
        """Bounded native metadata/audio response with retained actual close."""
        response = None
        try:
            async with asyncio.timeout(timeout):
                if current is not None and not await current():
                    raise SpeechProviderError(409)
                if not self._native_bound():
                    raise SpeechProviderError(409)
                client = self._native_http
                request = client.build_request(
                    "GET" if body is None and data is None else "POST",
                    path,
                    json=body,
                    content=data,
                    headers=headers,
                )
                response = await client.send(request, stream=True)
                if not self._native_bound():
                    raise SpeechProviderError(409)
                raw = bytearray()
                async for part in response.aiter_bytes():
                    if not self._native_bound():
                        raise SpeechProviderError(409)
                    if len(raw) + len(part) > 128 * 1024:
                        raise ValueError("Native STT response exceeds its bound")
                    raw.extend(part)
                if response.status_code == 404 and path.endswith("/capability"):
                    return None
                if not response.is_success:
                    raise SpeechProviderError(response.status_code)
                from packages.providers.whistle_ipc import decode_json

                return decode_json(bytes(raw), 128 * 1024)
        finally:
            data = None
            if response is not None:
                closing = asyncio.create_task(response.aclose())
                self._native_closes.add(closing)

                def settled(task):
                    self._native_closes.discard(task)
                    if task.cancelled() or task.exception() is not None:
                        self._cleanup_failed = True

                closing.add_done_callback(settled)
                await asyncio.shield(closing)

    async def prepare_native_stt(self, data, *, current):
        """Reserve metadata only. No audio or inference is sent by this handshake."""
        if self.stt is not None:
            return None  # Managed Whistle has no supported settlement protocol yet.
        from packages.contracts.stt_settlement import PROTOCOL, STTWorkerOperation

        capability = await self._native_exchange("/v1/audio/stt-operations/capability")
        if not await current():
            raise SpeechProviderError(409)
        if capability is None:
            return None  # Explicit old worker, retaining legacy unknown semantics.
        if (
            type(capability) is not dict
            or set(capability)
            != {
                "schema_version",
                "protocol",
                "worker_instance",
                "active_limit",
                "receipt_limit",
                "reservation_seconds",
            }
            or capability["schema_version"] != "1.0"
            or capability["protocol"] != PROTOCOL
            or any(
                type(capability[name]) is not int or capability[name] != expected
                for name, expected in (
                    ("active_limit", 32),
                    ("receipt_limit", 1024),
                    ("reservation_seconds", 30),
                )
            )
        ):
            raise ValueError("Unsupported native STT settlement capability")
        request = {
            "schema_version": "1.0",
            "protocol": PROTOCOL,
            "worker_instance": capability["worker_instance"],
            "audio_sha256": hashlib.sha256(data).hexdigest(),
        }
        from packages.contracts.stt_settlement import STTWorkerReservation

        original = STTWorkerReservation.model_validate(request)
        reserved = await self._native_exchange(
            "/v1/audio/stt-operations/reserve", body=original.model_dump()
        )
        if not await current():
            raise SpeechProviderError(409)
        operation = STTWorkerOperation.model_validate(reserved)
        if (
            operation.model_dump(exclude={"operation_id", "request_sha256"})
            != original.model_dump()
        ):
            raise ValueError("Native STT reservation changed its original request")
        return {
            "protocol": PROTOCOL,
            "origin": self._native_stt_origin,
            "operation": operation.model_dump(),
        }

    @staticmethod
    def _native_proof(value, operation):
        if (
            type(value) is not dict
            or set(value) != set(operation.model_dump()) | {"state", "settled", "outcome"}
            or any(value.get(key) != item for key, item in operation.model_dump().items())
            or type(value["settled"]) is not bool
            or value["state"] not in {"reserved", "running", "settled", "unknown"}
            or value["outcome"] not in {None, "completed", "failed", "not_dispatched"}
            or value["settled"] != (value["state"] == "settled")
            or (value["settled"] != (value["outcome"] is not None))
        ):
            raise ValueError("Native STT proof does not bind its original operation")
        return value

    async def dispatch_native_stt(self, data, binding, *, current):
        from packages.contracts.stt_settlement import STTWorkerBinding

        binding = STTWorkerBinding.model_validate(binding)
        if self.stt is not None or binding.origin != self._native_stt_origin or not await current():
            raise SpeechProviderError(409)
        operation = binding.operation
        if hashlib.sha256(data).hexdigest() != operation.audio_sha256:
            raise ValueError("Native STT audio changed after reservation")
        result = await self._native_exchange(
            "/v1/audio/stt-operations/dispatch",
            data=data,
            headers={
                "Content-Type": "application/octet-stream",
                **{
                    "x-kinaiya-" + key.replace("_", "-"): getattr(operation, key)
                    for key in ("worker_instance", "operation_id", "audio_sha256", "request_sha256")
                },
            },
            timeout=120,
            current=current,
        )
        if type(result) is not dict or set(result) != {"transcript", "operation"}:
            raise ValueError("Invalid native STT dispatch response")
        proof = self._native_proof(result["operation"], operation)
        if (
            not proof["settled"]
            or proof["outcome"] != "completed"
            or type(result["transcript"]) is not dict
        ):
            raise ValueError("Native STT completion was not proved")
        return result["transcript"]

    async def lookup_native_stt(self, binding):
        from packages.contracts.stt_settlement import STTWorkerBinding

        binding = STTWorkerBinding.model_validate(binding)
        if self.stt is not None or binding.origin != self._native_stt_origin:
            raise ValueError("The original native STT endpoint is not configured")
        return self._native_proof(
            await self._native_exchange(
                "/v1/audio/stt-operations/lookup", body=binding.operation.model_dump()
            ),
            binding.operation,
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
            if self._native_exchanges:
                await asyncio.shield(
                    asyncio.gather(*tuple(self._native_exchanges), return_exceptions=True)
                )
            if self._native_closes:
                await asyncio.shield(
                    asyncio.gather(*tuple(self._native_closes), return_exceptions=True)
                )
            await self._native_http.aclose()
            if self.http is not self._native_http:
                await self.http.aclose()
        finally:
            if self.stt is not None:
                await self.stt.aclose()
