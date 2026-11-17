"""#623: explicit logical utterances keep their original mic and STT lifetime."""

import asyncio

import pytest

from packages.contracts.live_windows import LiveWindowPublicationRef
from packages.contracts.stt_settlement import digest
from services.converse.live_stream_api import BASE
from tests.integration.test_incremental_whistle_converse import (
    converse_incremental_env as converse_incremental_env,
)
from tests.integration.test_live_stream_api import begin_body, live_client, pcm_for


async def finish_segment(env, live, body, started):
    pcm = {**pcm_for(env, started["native"]["result"]), **body}
    observed = await live.client.post(BASE + "/pcm", json=pcm)
    assert observed.status_code == 200
    finish = {key: value for key, value in pcm.items() if key != "pcm16_base64"}
    finish.update(sequence=2, sample_offset=160, user_speaking=False, capture_ended=False)
    result = await live.client.post(BASE + "/finish", json=finish)
    assert result.status_code == 200
    capture = live.app.state.live_streams.captures[body["capture_id"]]
    await asyncio.wait_for(asyncio.shield(capture.states[-1].lifetime), 3)
    return finish, result.json()


def boundary(body, finish, *, segment=None):
    index = body["segment_index"] + 1 if segment is None else segment
    return {
        **body,
        "boundary_protocol": "native_live_utterance_v1",
        "previous_utterance_id": body["utterance_id"],
        "previous_finish_sha256": digest(finish),
        "segment_index": index,
        "utterance_id": body["capture_id"] + "_" + str(index),
    }


async def started(env, live):
    owner = (await live.client.get(BASE + "/ownership")).json()["owner_instance"]
    body = begin_body(env, owner)
    result = await live.client.post(BASE + "/begin", json=body)
    assert result.status_code == 200
    return body, result.json()


async def test_explicit_utterances_preserve_one_mic_lease_and_old_source_permission(
    converse_incremental_env,
):
    env = converse_incremental_env
    async with live_client(env) as live:
        capability = await live.client.get(BASE + "/next-utterance")
        assert capability.status_code == 200 and capability.json()["available"]
        assert capability.json()["protocol"] == "native_live_utterance_v1"
        assert live.app.state.live_streams.captures == {} and env.engine.begin_calls == 0
        body, first = await started(env, live)
        assert capability.json()["owner_instance"] == body["owner_instance"]
        capture = live.app.state.live_streams.captures[body["capture_id"]]
        lease = capture.native.lease
        finish, final = await finish_segment(env, live, body, first)
        request = boundary(body, finish)
        # Ordinary v2 begin cannot silently change an utterance.
        implicit = {key: value for key, value in request.items() if key in body}
        assert (await live.client.post(BASE + "/begin", json=implicit)).status_code == 409
        result = await live.client.post(BASE + "/next-utterance", json=request)
        assert result.status_code == 200
        proof = result.json()
        assert proof["request_sha256"] == digest(request)
        assert proof["segment_sample_offset"] == 160
        assert proof["capture"] == first["capture"]
        assert proof["native"]["result"]["utterance_id"] == request["utterance_id"]
        assert proof["native"]["result"]["stream_id"] != first["native"]["result"]["stream_id"]
        assert capture.native.lease is lease and len(env.runtime.stt_model_settings.leases) == 1
        assert not capture.capture_task.done() and not capture.capture_fenced
        readback = await live.client.post(
            BASE + "/lookup",
            json={
                **{
                    key: body[key]
                    for key in ("schema_version", "protocol", "owner_instance", "capture_id")
                },
                "phase": "begin",
                "request_sha256": digest(request),
            },
        )
        assert readback.status_code == 200 and readback.json()["native"]["outcome"] == "confirmed"
        assert env.engine.begin_calls == 2
        old = await live.client.post(
            BASE + "/lookup",
            json={
                **{
                    key: body[key]
                    for key in ("schema_version", "protocol", "owner_instance", "capture_id")
                },
                "phase": "finish",
                "request_sha256": digest(finish),
            },
        )
        assert old.status_code == 200 and old.json()["publication"] == final["publication"]
        assert old.json()["capture"]["capture_open"]
        next_body = {key: request[key] for key in body}
        second_finish, second = await finish_segment(env, live, next_body, proof)
        source = second["publication"]["source"]["payload"]
        assert source["utterance_id"] == request["utterance_id"]
        assert source["window_index"] == 1 and source["start_ms"] == 10
        history = env.store.read(kinds=["conversation.window"])
        assert history[2].payload["utterance_id"] == request["utterance_id"]
        assert history[2].payload["window_index"] == 0
        assert history[2].payload["start_ms"] == 10
        assert env.store.live_captures.source_state(
            LiveWindowPublicationRef.model_validate(final["publication"]["original"])
        ).capture_permits_source
        # A further explicit boundary has its own unique native source domain.
        third = boundary(next_body, second_finish)
        assert (await live.client.post(BASE + "/next-utterance", json=third)).status_code == 200
        assert capture.native.lease is lease and env.engine.begin_calls == 3


@pytest.mark.parametrize(
    "change",
    [
        {"owner_instance": "a" * 32},
        {"session_id": "another-session"},
        {"thread_id": "another-thread"},
        {"epoch": 2},
        {"content_scope": "public_synthetic"},
        {"previous_utterance_id": "another-utterance"},
        {"previous_finish_sha256": "a" * 64},
    ],
)
async def test_boundary_cannot_replace_its_native_scope_or_original_finish(
    converse_incremental_env,
    change,
):
    env = converse_incremental_env
    async with live_client(env) as live:
        body, first = await started(env, live)
        finish, _ = await finish_segment(env, live, body, first)
        capture = live.app.state.live_streams.captures[body["capture_id"]]
        result = await live.client.post(
            BASE + "/next-utterance", json={**boundary(body, finish), **change}
        )
        assert result.status_code == 409
        assert capture.scope.utterance_id == body["utterance_id"] and capture.segment_index == 0
        assert env.engine.begin_calls == 1
        assert len(env.runtime.stt_model_settings.leases) == 1


async def test_boundary_refuses_original_decoder_cleanup_until_it_actually_finishes(
    converse_incremental_env,
    monkeypatch,
):
    env = converse_incremental_env
    async with live_client(env) as live:
        body, first = await started(env, live)
        capture = live.app.state.live_streams.captures[body["capture_id"]]
        state = capture.states[-1]
        entered, release = asyncio.Event(), asyncio.Event()
        original_drain = state.client.drain

        async def held():
            entered.set()
            await release.wait()
            await original_drain()

        monkeypatch.setattr(state.client, "drain", held)
        pcm = {**body, **pcm_for(env, first["native"]["result"])}
        assert (await live.client.post(BASE + "/pcm", json=pcm)).status_code == 200
        finish = {key: value for key, value in pcm.items() if key != "pcm16_base64"}
        finish.update(sequence=2, sample_offset=160, user_speaking=False, capture_ended=False)
        assert (await live.client.post(BASE + "/finish", json=finish)).status_code == 200
        try:
            await asyncio.wait_for(entered.wait(), 3)
            request = boundary(body, finish)
            result = await live.client.post(BASE + "/next-utterance", json=request)
            assert result.status_code == 409
            assert (
                env.engine.begin_calls == 1 and capture.scope.utterance_id == body["utterance_id"]
            )
        finally:
            release.set()
            await asyncio.wait_for(asyncio.shield(state.lifetime), 3)
        assert (await live.client.post(BASE + "/next-utterance", json=request)).status_code == 200


@pytest.mark.parametrize("role", [None, "agent", "converse", "decisions", "resources"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_new_boundary_requires_owner_before_any_native_capture(
    converse_incremental_env, role, method
):
    from tests.integration.test_incremental_whistle_converse import AUTH

    env = converse_incremental_env
    headers = {"Authorization": "" if role is None else "Bearer " + AUTH.tokens[role]}
    result = await env.client.request(
        method,
        BASE + "/next-utterance",
        headers=headers,
        **({"json": {}} if method == "POST" else {}),
    )
    assert result.status_code == (401 if role is None else 403)
    assert env.engine.begin_calls == 0 and env.app.state.live_streams.captures == {}


@pytest.mark.parametrize(
    "change",
    [
        {"segment_index": 0},
        {"segment_index": 1.0},
        {"segment_index": True},
        {"segment_index": 1000001},
        {"previous_finish_sha256": "X" * 64},
        {"boundary_protocol": "native_live_utterance_v2"},
        {"utterance_id": "reused"},
        {"extra": True},
    ],
)
async def test_malformed_boundary_cannot_create_an_original(converse_incremental_env, change):
    env = converse_incremental_env
    body = begin_body(env, "a" * 32)
    request = boundary(body, {})
    result = await env.client.post(BASE + "/next-utterance", json={**request, **change})
    assert result.status_code == 400
    assert env.engine.begin_calls == 0 and env.app.state.live_streams.captures == {}


async def test_initial_utterance_cannot_be_reused_by_a_later_boundary(converse_incremental_env):
    env = converse_incremental_env
    async with live_client(env) as live:
        owner = (await live.client.get(BASE + "/ownership")).json()["owner_instance"]
        body = begin_body(env, owner)
        body["utterance_id"] = body["capture_id"] + "_2"
        first = await live.client.post(BASE + "/begin", json=body)
        assert first.status_code == 200
        finish, _ = await finish_segment(env, live, body, first.json())
        request = boundary(body, finish)
        next_result = await live.client.post(BASE + "/next-utterance", json=request)
        assert next_result.status_code == 200
        next_body = {key: request[key] for key in body}
        finish, _ = await finish_segment(env, live, next_body, next_result.json())
        refused = await live.client.post(BASE + "/next-utterance", json=boundary(next_body, finish))
        assert refused.status_code == 409
        assert env.engine.begin_calls == 2


async def test_concurrent_boundary_delivery_has_one_original_begin(
    converse_incremental_env, monkeypatch
):
    env = converse_incremental_env
    async with live_client(env) as live:
        body, first = await started(env, live)
        finish, _ = await finish_segment(env, live, body, first)
        capture = live.app.state.live_streams.captures[body["capture_id"]]
        lease = capture.native.lease
        entered, release = asyncio.Event(), asyncio.Event()
        verify = capture._verify_current

        async def held(state):
            if state.live_segment == 1:
                entered.set()
                await release.wait()
            await verify(state)

        monkeypatch.setattr(capture, "_verify_current", held)
        request = boundary(body, finish)
        original = asyncio.create_task(live.client.post(BASE + "/next-utterance", json=request))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert (
                await live.client.post(BASE + "/next-utterance", json=request)
            ).status_code == 409
            lookup = {
                **{
                    key: body[key]
                    for key in ("schema_version", "protocol", "owner_instance", "capture_id")
                },
                "phase": "begin",
                "request_sha256": digest(request),
            }
            pending = await live.client.post(BASE + "/lookup", json=lookup)
            assert (
                pending.status_code == 200 and pending.json()["native"]["outcome"] == "in_progress"
            )
            assert env.engine.begin_calls == 1 and capture.native.lease is lease
        finally:
            release.set()
            result = await asyncio.wait_for(asyncio.shield(original), 3)
        assert result.status_code == 200 and env.engine.begin_calls == 2


async def test_lost_boundary_acknowledgment_uses_exact_metadata_without_decoder_replay(
    converse_incremental_env,
    monkeypatch,
):
    import httpx

    env = converse_incremental_env
    async with live_client(env) as live:
        body, first = await started(env, live)
        finish, _ = await finish_segment(env, live, body, first)
        request = boundary(body, finish)
        transport = live.client._transport
        send = transport.handle_async_request
        dropped = False

        async def lose_ack(incoming):
            nonlocal dropped
            result = await send(incoming)
            if (
                incoming.method == "POST"
                and incoming.url.path.endswith("/next-utterance")
                and not dropped
            ):
                dropped = True
                raise httpx.ReadError("Public synthetic boundary link cut", request=incoming)
            return result

        monkeypatch.setattr(transport, "handle_async_request", lose_ack)
        with pytest.raises(httpx.ReadError):
            await live.client.post(BASE + "/next-utterance", json=request)
        lookup = {
            **{
                key: body[key]
                for key in ("schema_version", "protocol", "owner_instance", "capture_id")
            },
            "phase": "begin",
            "request_sha256": digest(request),
        }
        proof = await live.client.post(BASE + "/lookup", json=lookup)
        assert proof.status_code == 200 and proof.json()["native"]["outcome"] == "confirmed"
        assert proof.json()["request_sha256"] == digest(request)
        assert proof.json()["capture"]["capture_open"]
        assert env.engine.begin_calls == 2 and len(env.runtime.stt_model_settings.leases) == 1


async def test_explicit_ended_capture_cannot_restart_through_a_logical_boundary(
    converse_incremental_env,
):
    env = converse_incremental_env
    async with live_client(env) as live:
        body, first = await started(env, live)
        pcm = {**pcm_for(env, first["native"]["result"]), **body}
        assert (await live.client.post(BASE + "/pcm", json=pcm)).status_code == 200
        finish = {key: value for key, value in pcm.items() if key != "pcm16_base64"}
        finish.update(sequence=2, sample_offset=160, user_speaking=False, capture_ended=True)
        assert (await live.client.post(BASE + "/finish", json=finish)).status_code == 200
        capture = live.app.state.live_streams.captures[body["capture_id"]]
        await asyncio.wait_for(asyncio.shield(capture.states[-1].lifetime), 3)
        assert (
            await live.client.post(BASE + "/next-utterance", json=boundary(body, finish))
        ).status_code == 409
        assert capture.scope.utterance_id == body["utterance_id"] and env.engine.begin_calls == 1
