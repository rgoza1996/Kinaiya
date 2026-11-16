"""Captured first-party Whistle endpoint; it never follows a changed selection."""

import asyncio
from urllib.parse import urlsplit

import httpx

from packages.contracts.stt_settlement import encode
from packages.contracts.whistle_stream_owner import (
    ORIGINAL_HEADER,
    PROTOCOL,
    WhistleStreamCapability,
    WhistleStreamOriginal,
    WhistleStreamProof,
    WhistleStreamReservation,
)
from packages.providers.speech import SpeechProviderError
from packages.providers.whistle_ipc import decode_json

BASE = "/v1/audio/transcriptions/stream"


def _identity(client):
    return (
        str(client.base_url),
        dict(client.headers),
        client._transport,
        tuple(client._mounts.items()),
        client.auth,
        {name: tuple(items) for name, items in client.event_hooks.items()},
        client.timeout.as_dict(),
        client.follow_redirects,
        client.trust_env,
    )


class CapturedWhistleStream:
    def __init__(self, speech):
        from packages.providers.speech import SpeechClient

        if not isinstance(speech, SpeechClient) or not isinstance(speech.stt, httpx.AsyncClient):
            raise ValueError("The selected STT adapter does not support owned Whistle streams")
        self.speech, self.client = speech, speech.stt
        origin = urlsplit(str(self.client.base_url))
        if (
            origin.scheme != "http"
            or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
            or self.client.follow_redirects
            or self.client.trust_env
        ):
            raise ValueError("Owned Whistle requires its unchanged private loopback client")
        self.identity = _identity(self.client)
        self.instance = None
        self.exchanges, self.closes = set(), set()
        self.cleanup_failed = False

    def bound(self):
        try:
            return not self.client.is_closed and _identity(self.client) == self.identity
        except Exception:
            return False

    def selected(self):
        return self.speech.stt is self.client and self.bound()

    async def _exchange(self, method, path, *, body=None, raw=None, headers=None, metadata=True):
        if not self.bound():
            raise RuntimeError("The original Whistle endpoint identity is unavailable")
        req_headers = dict(headers or {})
        if body is not None:
            raw = encode(body).encode("utf-8")
            if len(raw) > 4096:
                raise ValueError("Whistle control metadata exceeds its bound")
            req_headers["Content-Type"] = "application/json"

        async def exchange():
            response = None
            try:
                async with asyncio.timeout(5 if metadata else self.client.timeout.read):
                    response = await self.client.send(
                        self.client.build_request(method, path, content=raw, headers=req_headers),
                        stream=True,
                        follow_redirects=False,
                    )
                    if not response.is_success:
                        raise SpeechProviderError(response.status_code)
                    if (
                        response.headers.get("content-encoding", "identity").strip().lower()
                        != "identity"
                    ):
                        raise ValueError("Whistle originals require an identity response")
                    payload = bytearray()
                    # Leave EOF/close to this one retained close owner.
                    async for part in response.stream:
                        if type(part) is not bytes or len(payload) + len(part) > 132 * 1024:
                            raise ValueError("Whistle original response exceeds its bound")
                        payload.extend(part)
                    if not self.bound():
                        raise RuntimeError("The original Whistle endpoint identity changed")
                    return decode_json(bytes(payload), 132 * 1024)
            finally:
                if response is not None:
                    closing = asyncio.create_task(response.aclose())
                    self.closes.add(closing)
                    self.speech._native_closes.add(closing)

                    def closed(task):
                        self.closes.discard(task)
                        self.speech._native_closes.discard(task)
                        if task.cancelled() or task.exception() is not None:
                            self.cleanup_failed = self.speech._cleanup_failed = True

                    closing.add_done_callback(closed)
                    await asyncio.shield(closing)

        task = asyncio.create_task(exchange())
        self.exchanges.add(task)
        self.speech._native_exchanges.add(task)

        def finished(done):
            self.exchanges.discard(done)
            self.speech._native_exchanges.discard(done)
            if done.cancelled():
                self.cleanup_failed = self.speech._cleanup_failed = True
            else:
                done.exception()

        task.add_done_callback(finished)
        if metadata:
            async with asyncio.timeout(5):
                return await asyncio.shield(task)
        return await asyncio.shield(task)

    async def drain(self):
        while pending := self.exchanges | self.closes:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.cleanup_failed:
            raise RuntimeError("Original Whistle transport cleanup is unconfirmed")

    async def reserve(self, phase, request_sha256):
        if not self.selected():
            raise RuntimeError("STT selection changed before stream reservation")
        capability = WhistleStreamCapability.model_validate(
            await self._exchange("GET", BASE + "/ownership")
        )
        if self.instance is not None and self.instance != capability.broker_instance:
            raise RuntimeError("The original Whistle broker instance changed")
        self.instance = capability.broker_instance
        request = WhistleStreamReservation(
            schema_version="1.0",
            protocol=PROTOCOL,
            broker_instance=self.instance,
            phase=phase,
            request_sha256=request_sha256,
        )
        original = WhistleStreamOriginal.model_validate(
            await self._exchange("POST", BASE + "/ownership/reserve", body=request.model_dump())
        )
        if original.model_dump(exclude={"operation_id"}) != request.model_dump():
            raise ValueError("Whistle reservation changed its complete original identity")
        return original

    async def dispatch(self, original, *, body=None, pcm16=None, headers=None):
        if not self.selected() or original.broker_instance != self.instance:
            raise RuntimeError("STT selection or broker changed before dispatch")
        return await self._exchange(
            "POST",
            BASE + "/" + original.phase,
            body=body,
            raw=pcm16,
            headers={**(headers or {}), ORIGINAL_HEADER: encode(original.model_dump())},
            metadata=False,
        )

    async def inspect(self, original, *, operation="lookup", result_sha256=None):
        # Recovery/abort addresses the captured endpoint even after a new
        # selection. It cannot use that new endpoint to clear the old owner.
        body = original.model_dump()
        if result_sha256 is not None:
            body["result_sha256"] = result_sha256
        proof = WhistleStreamProof.model_validate(
            await self._exchange("POST", BASE + "/ownership/" + operation, body=body)
        )
        if proof.original != original:
            raise ValueError("Whistle proof does not bind the exact original operation")
        return proof
