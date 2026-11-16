"""Real synthetic IPC behind a retained original HTTP response close (#654)."""

import asyncio
import base64

import httpx
import pytest

from packages.contracts.stt_settlement import digest
from packages.contracts.whistle_stream_owner import pcm_request_digest
from packages.providers.speech import SpeechClient
from packages.providers.whistle_stream_owner import CapturedWhistleStream
from services.converse.api import create_app
from tests.integration.test_incremental_whistle_converse import AUTH
from tests.integration.test_incremental_whistle_converse import (
    converse_incremental_env as converse_incremental_env,
)
from tests.integration.test_whistle_stream_originals import scope_for

BASE = "/v1/converse/audio/stream"


class _CloseGate(httpx.AsyncByteStream):
    def __init__(self, raw, *, fail=False):
        self.raw, self.fail = raw, fail
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def __aiter__(self):
        yield self.raw

    async def aclose(self):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise OSError("PUBLIC-ORIGINAL-CLOSE-FAILED")


class _ClosingTransport(httpx.AsyncBaseTransport):
    def __init__(self, app, *, fail=False):
        self.inner = httpx.ASGITransport(app=app)
        self.fail = fail
        self.gate_ready = asyncio.Event()
        self.gate = None

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if request.url.path.endswith("/stream/pcm") and response.status_code == 200:
            raw = await response.aread()
            await response.aclose()
            self.gate = _CloseGate(raw, fail=self.fail)
            self.gate_ready.set()
            return httpx.Response(response.status_code, headers=response.headers, stream=self.gate)
        return response

    async def aclose(self):
        await self.inner.aclose()


def speech_for(env, transport):
    return SpeechClient(
        "http://127.0.0.1:8765",
        AUTH.tokens["converse"],
        stt_provider="whistle",
        stt_url="http://127.0.0.1:8764",
        stt_transport=transport,
    )


async def test_converse_does_not_publish_or_retire_while_original_response_close_is_held(
    converse_incremental_env,
):
    env = converse_incremental_env
    transport = _ClosingTransport(env.resources_app)
    speech = speech_for(env, transport)
    pending = None
    try:
        app = create_app(env.runtime, AUTH, speech=speech, window_controller=env.window_controller)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://converse.local",
            headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
        ) as client:
            began = await client.post(BASE + "/begin", json=scope_for(env))
            assert began.status_code == 200
            start = began.json()
            body = {
                **scope_for(env),
                "stream_id": start["stream_id"],
                "worker_incarnation": start["worker_incarnation"],
                "sequence": 1,
                "sample_offset": 0,
                "user_speaking": True,
                "pcm16_base64": base64.b64encode(b"\x01\x00" * 160).decode(),
            }
            pending = asyncio.create_task(client.post(BASE + "/pcm", json=body))
            await asyncio.wait_for(transport.gate_ready.wait(), 5)
            await asyncio.wait_for(transport.gate.entered.wait(), 5)
            assert env.engine.step == 1 and not pending.done()
            assert env.store.read(kinds=["conversation.window"]) == []
            assert len(speech._native_closes) == 1 and env.runtime.model_operations()["stt"] > 0
            duplicate = await client.post(BASE + "/pcm", json={**body, "user_speaking": False})
            assert duplicate.status_code == 409 and env.engine.step == 1
            transport.gate.release.set()
            accepted = await asyncio.wait_for(pending, 5)
            assert accepted.status_code == 200 and transport.gate.calls == 1
            assert len(env.store.read(kinds=["conversation.window"])) == 1
            final = await client.post(
                BASE + "/finish",
                json={
                    **scope_for(env),
                    "stream_id": start["stream_id"],
                    "worker_incarnation": start["worker_incarnation"],
                    "sequence": 2,
                    "sample_offset": 160,
                    "capture_ended": False,
                    "user_speaking": True,
                },
            )
            assert final.status_code == 200
            state = next(
                state
                for owner in env.runtime._incremental_stream_owners
                for state in owner.states
                if state.stream_id == start["stream_id"]
            )
            await asyncio.wait_for(asyncio.shield(state.lifetime), 5)
            assert not speech._native_closes and not speech._native_exchanges
            assert env.engine.step == env.engine.finish_calls == 1
    finally:
        if transport.gate is not None:
            transport.gate.release.set()
        if pending is not None and not pending.done():
            await asyncio.gather(pending, return_exceptions=True)
        await speech.close()


async def test_valid_broker_result_cannot_erase_failed_original_http_cleanup(
    converse_incremental_env,
):
    env = converse_incremental_env
    transport = _ClosingTransport(env.resources_app, fail=True)
    speech = speech_for(env, transport)
    try:
        client = CapturedWhistleStream(speech)
        body = scope_for(env)
        begin = await client.reserve("begin", digest(body))
        started = await client.dispatch(begin, body=body)
        await client.inspect(begin, operation="acknowledge", result_sha256=digest(started))
        pcm = b"\x01\x00" * 160
        import hashlib

        headers = {
            "Content-Type": "application/octet-stream",
            "x-kinaiya-stream-id": started["stream_id"],
            "x-kinaiya-worker-incarnation": started["worker_incarnation"],
            "x-kinaiya-session-id": body["session_id"],
            "x-kinaiya-thread-id": body["thread_id"],
            "x-kinaiya-epoch": str(body["epoch"]),
            "x-kinaiya-utterance-id": body["utterance_id"],
            "x-kinaiya-content-scope": body["content_scope"],
            "x-kinaiya-sequence": "1",
            "x-kinaiya-sample-offset": "0",
            "x-kinaiya-pcm-sha256": hashlib.sha256(pcm).hexdigest(),
        }
        original = await client.reserve("pcm", pcm_request_digest(headers, pcm))
        pending = asyncio.create_task(client.dispatch(original, pcm16=pcm, headers=headers))
        await asyncio.wait_for(transport.gate_ready.wait(), 5)
        await asyncio.wait_for(transport.gate.entered.wait(), 5)
        close = next(iter(speech._native_closes))
        assert not close.done() and env.engine.step == 1
        transport.gate.release.set()
        with pytest.raises(OSError, match="PUBLIC-ORIGINAL-CLOSE-FAILED"):
            await pending
        assert close.done() and client.cleanup_failed and speech._cleanup_failed
        proof = await client.inspect(original)
        assert proof.state == "completed" and proof.result["received_samples"] == 160
        assert proof.process_released is False and env.engine.step == 1
        with pytest.raises(RuntimeError, match="cleanup is unconfirmed"):
            await client.drain()
        assert client.cleanup_failed and speech._cleanup_failed
        await client.inspect(begin, operation="abort")
    finally:
        if transport.gate is not None:
            transport.gate.release.set()
        await speech.close()
