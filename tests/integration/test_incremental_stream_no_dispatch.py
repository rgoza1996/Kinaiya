"""Positive unused-reservation proof cannot be confused with missing state."""

import asyncio

import httpx
import pytest

from packages.contracts.stt_settlement import digest
from packages.providers.speech import SpeechClient
from services.converse.api import create_app
from services.converse.stt_model_settings import STTModelSettings
from tests.integration.test_incremental_stream_ownership import pcm_for, scope_for
from tests.integration.test_incremental_stream_transport_cleanup import speech_for
from tests.integration.test_incremental_whistle_converse import AUTH, _LoseSuccessfulResponse
from tests.integration.test_incremental_whistle_converse import (
    converse_incremental_env as converse_incremental_env,
)

BASE = "/v1/converse/audio/stream"


class _BeforeDispatch(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def handle_async_request(self, request):
        if request.url.path.endswith("/stream/pcm"):
            self.entered.set()
            await self.release.wait()
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


@pytest.mark.parametrize("action", ["expire", "cancel"])
async def test_no_effect_pcm_is_separate_from_active_stream_and_actual_transport_retirement(
    converse_incremental_env, action
):
    env = converse_incremental_env
    transport = _BeforeDispatch(env.resources_app)
    speech = speech_for(env, transport)
    settings = STTModelSettings(env.runtime, None, speech)
    env.runtime.stt_model_settings = settings
    env.runtime._stt_options_configured = True
    pending = None
    try:
        app = create_app(env.runtime, AUTH, speech=speech, window_controller=env.window_controller)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://converse.local",
            headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
        ) as client:
            began = await client.post(BASE + "/begin", json=scope_for(env))
            assert began.status_code == 200
            start, body = began.json(), pcm_for(env, began.json())
            pending = asyncio.create_task(client.post(BASE + "/pcm", json=body))
            await asyncio.wait_for(transport.entered.wait(), 5)
            state = next(
                state
                for owner in env.runtime._incremental_stream_owners
                for state in owner.states
                if state.stream_id == start["stream_id"]
            )
            reserved = env.broker.stream_owner.records[state.pending.native_original.operation_id]
            assert reserved.state == "reserved" and env.engine.step == 0
            cancel = {
                "stream_id": start["stream_id"],
                "worker_incarnation": start["worker_incarnation"],
            }
            metadata = {
                "schema_version": "1.0",
                "protocol": "converse_stream_owner_v1",
                "owner_instance": (await client.get(BASE + "/ownership")).json()["owner_instance"],
                "request_sha256": digest(body),
            }
            if action == "expire":
                reserved.created -= 31  # Advance only the unused reservation fixture clock.
            else:
                cancelled = await client.post(BASE + "/cancel", json=cancel)
                assert (
                    cancelled.status_code == 200 and cancelled.json()["cleanup_confirmed"] is True
                )
                assert reserved.state == "not_dispatched" and env.broker.manager.current is False
                # An internal release request is not actual lifetime/transport
                # retirement: the original HTTP body is still held here.
                assert not state.lifetime.done() and settings.snapshot()["busy"] is True
                assert state.client.exchanges
            transport.release.set()
            failed = await asyncio.wait_for(pending, 5)
            assert failed.status_code == 500 and reserved.state == "not_dispatched"
            observed = await client.post(BASE + "/pcm/lookup", json=metadata)
            assert observed.status_code == 200 and observed.json()["outcome"] == "not_dispatched"
            assert observed.json()["result"] is None
            if action == "expire":
                assert env.broker.manager.current is True and env.engine.active
                assert settings.snapshot()["busy"] is True and not state.lifetime.done()
                reconciled = await client.post(BASE + "/pcm/reconcile", json=metadata)
                assert (
                    reconciled.status_code == 200
                    and reconciled.json()["outcome"] == "not_dispatched"
                )
                assert not state.lifetime.done() and settings.snapshot()["busy"] is True
                cancelled = await client.post(BASE + "/cancel", json=cancel)
                assert (
                    cancelled.status_code == 200 and cancelled.json()["cleanup_confirmed"] is True
                )
            await asyncio.wait_for(asyncio.shield(state.lifetime), 5)
            assert not settings.leases and not settings._admissions()
            assert env.engine.begin_calls == 1 and env.engine.step == env.engine.finish_calls == 0
            assert env.store.read(kinds=["conversation.window"]) == []
    finally:
        transport.release.set()
        if pending is not None and not pending.done():
            await asyncio.gather(pending, return_exceptions=True)
        await settings.close()
        await speech.close()


async def test_batch_only_adapter_refuses_stream_without_model_or_provider_fallback(
    converse_incremental_env,
):
    env = converse_incremental_env
    speech = SpeechClient("http://127.0.0.1:8765", AUTH.tokens["converse"])
    try:
        app = create_app(env.runtime, AUTH, speech=speech, window_controller=env.window_controller)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://converse.local",
            headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
        ) as client:
            refused = await client.post(BASE + "/begin", json=scope_for(env))
            assert refused.status_code == 503
            owner = next(owner for owner in env.runtime._incremental_stream_owners if owner.states)
            state = owner.states[-1]
            await asyncio.wait_for(asyncio.shield(state.cleanup), 5)
            assert state.never_dispatched and state.lifetime.done() and owner.busy_count == 0
            assert env.engine.begin_calls == env.engine.step == env.engine.finish_calls == 0
            assert (
                env.broker.operation is None and env.store.read(kinds=["conversation.window"]) == []
            )
    finally:
        await speech.close()


async def test_missing_scope_refuses_before_any_native_metadata_or_inference(
    converse_incremental_env,
):
    env = converse_incremental_env
    refused = await env.client.post(
        BASE + "/begin", json={**scope_for(env), "session_id": "public-missing-session"}
    )
    assert refused.status_code == 409
    owner = next(owner for owner in env.runtime._incremental_stream_owners if owner.states)
    state = owner.states[-1]
    await asyncio.wait_for(asyncio.shield(state.cleanup), 5)
    await asyncio.wait_for(asyncio.shield(state.lifetime), 5)
    assert state.never_dispatched and env.broker.stream_owner.counter == 0
    assert env.engine.begin_calls == env.engine.step == env.engine.finish_calls == 0


async def test_owner_pcm_reserves_final_sequence_without_consuming_or_replacing_input(
    converse_incremental_env,
):
    env = converse_incremental_env
    began = await env.client.post(BASE + "/begin", json=scope_for(env))
    assert began.status_code == 200
    state = next(
        state
        for owner in env.runtime._incremental_stream_owners
        for state in owner.states
        if state.stream_id == began.json()["stream_id"]
    )
    refused = await env.client.post(
        BASE + "/pcm", json={**pcm_for(env, began.json()), "sequence": 100000}
    )
    assert refused.status_code == 413
    assert state.pending is None and state.sequence == state.sample_offset == 0
    assert env.engine.step == 0 and env.broker.stream_owner.counter == 1
    cancelled = await env.client.post(BASE + "/cancel", json={"stream_id": state.stream_id})
    assert cancelled.status_code == 200 and cancelled.json()["cleanup_confirmed"] is True
    await asyncio.wait_for(asyncio.shield(state.lifetime), 5)


async def test_completed_local_task_before_dispatch_is_positive_no_effect_despite_lost_metadata_ack(
    converse_incremental_env,
):
    env = converse_incremental_env
    transport = _LoseSuccessfulResponse(
        env.resources_app, "/v1/audio/transcriptions/stream/ownership/acknowledge"
    )
    speech = speech_for(env, transport)
    settings = STTModelSettings(env.runtime, None, speech)
    env.runtime.stt_model_settings = settings
    env.runtime._stt_options_configured = True
    try:
        app = create_app(env.runtime, AUTH, speech=speech, window_controller=env.window_controller)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://converse.local",
            headers={"Authorization": "Bearer " + AUTH.tokens["owner"]},
        ) as client:
            began = await client.post(BASE + "/begin", json=scope_for(env))
            assert began.status_code == 200
            body = pcm_for(env, began.json())
            failed = await client.post(BASE + "/pcm", json=body)
            assert failed.status_code == 500 and transport.lost == 1
            state = next(
                state
                for owner in env.runtime._incremental_stream_owners
                for state in owner.states
                if state.stream_id == began.json()["stream_id"]
            )
            assert state.pending.task.done() and state.pending.never_dispatched
            assert not state.pending.native_dispatched and state.pending.native_original is None
            assert env.broker.stream_owner.counter == 1 and env.engine.step == 0
            metadata = {
                "schema_version": "1.0",
                "protocol": "converse_stream_owner_v1",
                "owner_instance": (await client.get(BASE + "/ownership")).json()["owner_instance"],
                "request_sha256": digest(body),
            }
            proof = await client.post(BASE + "/pcm/lookup", json=metadata)
            assert proof.status_code == 200 and proof.json()["outcome"] == "not_dispatched"
            assert settings.snapshot()["busy"] is True and not state.release.is_set()
            cancelled = await client.post(
                BASE + "/cancel",
                json={"stream_id": state.stream_id, "worker_incarnation": state.worker_incarnation},
            )
            assert cancelled.status_code == 200 and cancelled.json()["cleanup_confirmed"] is True
            await asyncio.wait_for(asyncio.shield(state.lifetime), 5)
            assert not settings.leases and not settings._admissions()
            assert env.engine.begin_calls == 1 and env.engine.step == env.engine.finish_calls == 0
            assert env.store.read(kinds=["conversation.window"]) == []
    finally:
        await settings.close()
        await speech.close()
