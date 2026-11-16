"""Actual SQLite model lease + synthetic IPC, without a model or microphone."""

import asyncio
import base64

import httpx
import pytest

from services.converse.stt_model_settings import STTModelConflict, STTModelSettings
from tests.integration.test_incremental_whistle_converse import (
    converse_incremental_env as converse_incremental_env,
)

BASE = "/v1/converse/audio/stream"


def scope_for(env):
    return {
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
        "utterance_id": "public-owned-lease",
        "content_scope": "local_only",
    }


def pcm_for(env, start):
    return {
        **scope_for(env),
        "stream_id": start["stream_id"],
        "worker_incarnation": start["worker_incarnation"],
        "sequence": 1,
        "sample_offset": 0,
        "user_speaking": True,
        "pcm16_base64": base64.b64encode(b"\x01\x00" * 160).decode(),
    }


def stream_state(env):
    return next(iter(env.runtime._incremental_stream_owners)).states[-1]


def install_settings(env):
    settings = STTModelSettings(env.runtime, None, env.speech)
    env.runtime.stt_model_settings = settings
    env.runtime._stt_options_configured = True
    return settings


async def test_stream_lease_pins_every_publication_and_retires_after_original_final_ack(
    converse_incremental_env,
):
    env = converse_incremental_env
    settings = install_settings(env)
    try:
        began = await env.client.post(BASE + "/begin", json=scope_for(env))
        assert began.status_code == 200
        state = stream_state(env)
        lease = state.native.lease
        assert len(settings.leases) == 1 and len(settings._admissions()) == 1
        assert lease.admission_binding is None and settings.snapshot()["busy"] is True
        before = settings.snapshot()
        with pytest.raises(STTModelConflict):
            settings.change(
                {
                    "schema_version": "1.0",
                    "expected_revision": before["revision"],
                    "expected_state": before["state_token"],
                    "expected_backend": before["backend_id"],
                    "expected_instance": before["instance_id"],
                    "idempotency_key": "public-while-stream-owned",
                    "selection": before["selection"],
                }
            )
        assert settings.snapshot()["revision"] == before["revision"]
        pcm = pcm_for(env, began.json())
        accepted = await env.client.post(BASE + "/pcm", json=pcm)
        assert accepted.status_code == 200
        assert accepted.json()["entry"]["payload"]["stt_model"] == lease.pin.model_dump(mode="json")
        final = await env.client.post(
            BASE + "/finish",
            json={
                **scope_for(env),
                "stream_id": began.json()["stream_id"],
                "worker_incarnation": began.json()["worker_incarnation"],
                "sequence": 2,
                "sample_offset": 160,
                "user_speaking": True,
                "capture_ended": False,
            },
        )
        assert final.status_code == 200 and final.json()["capture_ended"] is False
        assert final.json()["entry"]["payload"]["user_speaking"] is True
        await asyncio.wait_for(asyncio.shield(state.lifetime), 5)
        assert lease.settled and not lease.cleanup_failed and not lease.cleanup_unknown
        assert not settings.leases and not settings._admissions() and not settings._native_unsettled
        assert settings.snapshot()["busy"] is False
        assert env.engine.begin_calls == 1 and env.engine.step == 1 and env.engine.finish_calls == 1
    finally:
        await settings.close()


async def test_one_pending_original_excludes_concurrent_different_pcm(
    converse_incremental_env, monkeypatch
):
    env = converse_incremental_env
    began = await env.client.post(BASE + "/begin", json=scope_for(env))
    assert began.status_code == 200
    entered, release = asyncio.Event(), asyncio.Event()
    original_pcm = env.broker.endpoint.stream_pcm

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_pcm(*args, **kwargs)

    monkeypatch.setattr(env.broker.endpoint, "stream_pcm", held)
    body = pcm_for(env, began.json())
    original = asyncio.create_task(env.client.post(BASE + "/pcm", json=body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        same, changed = await asyncio.gather(
            env.client.post(BASE + "/pcm", json=body),
            env.client.post(BASE + "/pcm", json={**body, "user_speaking": False}),
        )
        assert same.status_code == changed.status_code == 409
        assert env.engine.step == 0 and env.store.read(kinds=["conversation.window"]) == []
    finally:
        release.set()
    accepted = await asyncio.wait_for(original, 5)
    assert accepted.status_code == 200 and env.engine.step == 1
    assert len(env.store.read(kinds=["conversation.window"])) == 1


async def test_owner_off_uses_captured_endpoint_and_needs_positive_native_cleanup(
    converse_incremental_env, monkeypatch
):
    env = converse_incremental_env
    settings = install_settings(env)
    try:
        began = await env.client.post(BASE + "/begin", json=scope_for(env))
        assert began.status_code == 200
        state, calls = stream_state(env), []

        async def unconfirmed(identity):
            calls.append(identity)
            return {
                "ok": True,
                "release_confirmed": False,
                "stop_evidence": {"reaped": False, "group_empty": False},
            }

        monkeypatch.setattr(env.broker.manager, "stop_if_current", unconfirmed)
        env.runtime.enabled = False
        await asyncio.wait_for(asyncio.shield(state.cleanup), 5)
        assert len(calls) == 1
        assert state.fenced and not state.process_released and not state.release.is_set()
        assert not state.lifetime.done() and settings.snapshot()["busy"] is True
        assert len(settings._admissions()) == 1
        refused = await env.client.post(BASE + "/pcm", json=pcm_for(env, began.json()))
        assert refused.status_code == 409 and env.engine.step == 0
        assert env.broker.operation is not None and env.broker.operation.cleanup_failed
    finally:
        await settings.close()


async def test_changed_parent_client_cannot_redirect_original_abort(converse_incremental_env):
    env = converse_incremental_env
    began = await env.client.post(BASE + "/begin", json=scope_for(env))
    assert began.status_code == 200
    state, original = stream_state(env), env.speech.stt
    requests = []

    async def replacement(request):
        requests.append(request.url.path)
        return httpx.Response(500)

    changed = httpx.AsyncClient(
        base_url="http://127.0.0.1:8764", transport=httpx.MockTransport(replacement)
    )
    env.speech.stt = changed
    try:
        env.runtime.enabled = False
        await asyncio.wait_for(asyncio.shield(state.cleanup), 5)
        assert state.process_released and env.broker.manager.current is False
        await asyncio.wait_for(asyncio.shield(state.lifetime), 5)
        assert requests == [] and env.engine.begin_calls == 1
    finally:
        env.speech.stt = original
        await changed.aclose()
