"""Public synthetic IPC plus authenticated HTTP, with no model or physical device."""

import asyncio
import hashlib

import httpx
import pytest
from fastapi import HTTPException

from packages.contracts.stt_settlement import digest, encode
from packages.contracts.whistle_stream_owner import ORIGINAL_HEADER, PROTOCOL, pcm_request_digest
from tests.integration.test_incremental_whistle_converse import (
    AUTH,
    _LoseSuccessfulResponse,
)
from tests.integration.test_incremental_whistle_converse import (
    converse_incremental_env as converse_incremental_env,
)

BASE = "/v1/audio/transcriptions/stream"


def client_for(env, transport=None):
    return httpx.AsyncClient(
        transport=transport or httpx.ASGITransport(app=env.resources_app),
        base_url="http://resources.local",
        headers={"Authorization": "Bearer " + AUTH.tokens["converse"]},
    )


def scope_for(env, utterance="public-owned-stream"):
    return {
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
        "utterance_id": utterance,
        "content_scope": "local_only",
    }


async def reserve(client, phase, request_sha256):
    caps = await client.get(BASE + "/ownership")
    assert caps.status_code == 200 and caps.headers["cache-control"] == "no-store"
    capability = caps.json()
    response = await client.post(
        BASE + "/ownership/reserve",
        json={
            "schema_version": "1.0",
            "protocol": PROTOCOL,
            "broker_instance": capability["broker_instance"],
            "phase": phase,
            "request_sha256": request_sha256,
        },
    )
    assert response.status_code == 200
    return response.json()


async def lookup(client, original):
    response = await client.post(BASE + "/ownership/lookup", json=original)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    return response.json()


async def acknowledge(client, original, proof):
    response = await client.post(
        BASE + "/ownership/acknowledge",
        json={**original, "result_sha256": proof["result_sha256"]},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "acknowledged" and response.json()["result"] is None


async def begin(client, env, utterance="public-owned-stream"):
    body = scope_for(env, utterance)
    original = await reserve(client, "begin", digest(body))
    response = await client.post(
        BASE + "/begin", json=body, headers={ORIGINAL_HEADER: encode(original)}
    )
    assert response.status_code == 200
    proof = await lookup(client, original)
    assert proof["state"] == "completed" and proof["result"] == response.json()
    return original, response.json(), proof


async def test_lost_broker_begin_ack_keeps_exact_result_and_refuses_redispatch(
    converse_incremental_env,
):
    env = converse_incremental_env
    transport = _LoseSuccessfulResponse(env.resources_app, BASE + "/begin")
    async with client_for(env, transport) as client:
        body = scope_for(env)
        original = await reserve(client, "begin", digest(body))
        assert env.engine.begin_calls == 0 and env.broker.operation is None
        with pytest.raises(httpx.ReadError, match="PUBLIC-LOST-INCREMENTAL-ACK"):
            await client.post(
                BASE + "/begin", json=body, headers={ORIGINAL_HEADER: encode(original)}
            )
        assert env.engine.begin_calls == 1 and env.engine.active
        proof = await lookup(client, original)
        assert proof["state"] == "completed" and proof["result"]["sequence"] == 0
        assert proof["decoder_finished"] is False and proof["process_released"] is False
        refused = await client.post(
            BASE + "/begin", json=body, headers={ORIGINAL_HEADER: encode(original)}
        )
        assert refused.status_code == 409 and env.engine.begin_calls == 1
        changed = await lookup(client, {**original, "broker_instance": "f" * 32})
        assert changed["state"] == "unknown" and changed["result"] is None
        wrong = await client.post(
            BASE + "/ownership/acknowledge", json={**original, "result_sha256": "0" * 64}
        )
        assert (
            wrong.status_code == 409
            and (await lookup(client, original))["result"] == proof["result"]
        )
        await acknowledge(client, original, proof)
        env.broker.closed = True
        assert (await lookup(client, original))["state"] == "acknowledged"
        assert env.engine.begin_calls == 1


async def test_original_outlives_cancelled_http_wait_and_keeps_late_stream_id(
    converse_incremental_env, monkeypatch
):
    env = converse_incremental_env
    entered, release = asyncio.Event(), asyncio.Event()
    original_begin = env.broker.endpoint.begin_stream

    async def held(identity):
        entered.set()
        await release.wait()
        return await original_begin(identity)

    monkeypatch.setattr(env.broker.endpoint, "begin_stream", held)
    async with client_for(env) as client:
        body = scope_for(env)
        original = await reserve(client, "begin", digest(body))
        pending = asyncio.create_task(
            client.post(BASE + "/begin", json=body, headers={ORIGINAL_HEADER: encode(original)})
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            retained = env.broker.stream_owner.records[original["operation_id"]].task
            refused = await client.post(
                BASE + "/begin", json=body, headers={ORIGINAL_HEADER: encode(original)}
            )
            assert refused.status_code == 409
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert retained is not None and not retained.done()
            assert (await lookup(client, original))["state"] == "running"
        finally:
            release.set()
        result = await asyncio.wait_for(asyncio.shield(retained), 5)
        proof = await lookup(client, original)
        assert proof["state"] == "completed" and proof["result"] == result
        assert env.engine.begin_calls == 1 and env.engine.active
        assert env.broker.manager.current is True


async def test_expired_or_changed_reservation_cannot_create_native_work(converse_incremental_env):
    env = converse_incremental_env
    async with client_for(env) as client:
        body = scope_for(env)
        original = await reserve(client, "begin", digest(body))
        changed = await client.post(
            BASE + "/begin",
            json={**body, "utterance_id": "changed"},
            headers={ORIGINAL_HEADER: encode(original)},
        )
        assert changed.status_code == 409 and env.engine.begin_calls == 0
        record = env.broker.stream_owner.records[original["operation_id"]]
        record.created -= 31
        assert (await lookup(client, original))["state"] == "not_dispatched"
        refused = await client.post(
            BASE + "/begin", json=body, headers={ORIGINAL_HEADER: encode(original)}
        )
        assert refused.status_code == 409 and env.broker.operation is None
        assert env.engine.begin_calls == 0
        for role in ("agent", "decisions", "owner"):
            response = await client.get(
                BASE + "/ownership", headers={"Authorization": "Bearer " + AUTH.tokens[role]}
            )
            assert response.status_code == (200 if role == "owner" else 403)


async def test_finish_receipt_and_old_abort_do_not_stop_replacement_stream(
    converse_incremental_env,
):
    env = converse_incremental_env
    async with client_for(env) as client:
        old, start, proof = await begin(client, env)
        await acknowledge(client, old, proof)
        pcm = b"\x01\x00" * 160
        headers = {
            "x-kinaiya-stream-id": start["stream_id"],
            "x-kinaiya-worker-incarnation": start["worker_incarnation"],
            "x-kinaiya-session-id": env.session.session_id,
            "x-kinaiya-thread-id": env.session.thread_id,
            "x-kinaiya-epoch": str(env.session.epoch),
            "x-kinaiya-utterance-id": "public-owned-stream",
            "x-kinaiya-content-scope": "local_only",
            "x-kinaiya-sequence": "1",
            "x-kinaiya-sample-offset": "0",
            "x-kinaiya-pcm-sha256": hashlib.sha256(pcm).hexdigest(),
        }
        appended = await reserve(client, "pcm", pcm_request_digest(headers, pcm))
        response = await client.post(
            BASE + "/pcm",
            content=pcm,
            headers={
                **headers,
                "Content-Type": "application/octet-stream",
                ORIGINAL_HEADER: encode(appended),
            },
        )
        assert response.status_code == 200 and env.engine.step == 1
        append_proof = await lookup(client, appended)
        assert (
            append_proof["result"] == response.json() and append_proof["process_released"] is False
        )
        await acknowledge(client, appended, append_proof)
        body = {
            **scope_for(env),
            "stream_id": start["stream_id"],
            "worker_incarnation": start["worker_incarnation"],
            "sequence": 2,
            "sample_offset": 160,
        }
        final = await reserve(client, "finish", digest(body))
        response = await client.post(
            BASE + "/finish", json=body, headers={ORIGINAL_HEADER: encode(final)}
        )
        assert response.status_code == 200 and env.engine.finish_calls == 1
        proof = await lookup(client, final)
        assert proof["decoder_finished"] is True and proof["process_released"] is False
        assert proof["result"]["committed_text"] == "please check the build status"
        await acknowledge(client, final, proof)
        assert (await lookup(client, final))["decoder_finished"] is True
        _, new_start, _ = await begin(client, env, "public-replacement-stream")
        assert new_start["stream_id"] != start["stream_id"]
        aborted = await client.post(BASE + "/ownership/abort", json=old)
        assert aborted.status_code == 200 and aborted.json()["process_released"] is False
        assert env.broker.operation.stream["stream_id"] == new_start["stream_id"]
        assert env.broker.manager.current and env.engine.active and env.engine.begin_calls == 2


async def test_native_policy_refusal_before_entry_has_irreversible_no_dispatch_proof(
    converse_incremental_env, monkeypatch
):
    env = converse_incremental_env
    async with client_for(env) as client:
        body = scope_for(env)
        original = await reserve(client, "begin", digest(body))

        def refused():
            raise HTTPException(409, "PUBLIC-OWNER-POLICY-REFUSAL")

        monkeypatch.setattr(env.broker, "policy", refused)
        response = await client.post(
            BASE + "/begin", json=body, headers={ORIGINAL_HEADER: encode(original)}
        )
        assert response.status_code == 409
        record = env.broker.stream_owner.records[original["operation_id"]]
        assert record.native is None and record.task is None
        assert env.broker.operation is None and env.engine.begin_calls == 0
        proof = await lookup(client, original)
        assert proof["state"] == "not_dispatched"
        assert proof["result"] is None and proof["process_released"] is False
        again = await client.post(
            BASE + "/begin", json=body, headers={ORIGINAL_HEADER: encode(original)}
        )
        assert again.status_code == 409 and env.engine.begin_calls == 0
