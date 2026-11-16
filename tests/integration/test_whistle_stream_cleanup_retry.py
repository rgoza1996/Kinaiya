"""Exact retried native cleanup must preserve independent transport failures."""

import asyncio

from packages.contracts.stt_settlement import digest
from tests.integration.test_incremental_stream_ownership import (
    install_settings,
    scope_for,
    stream_state,
)
from tests.integration.test_incremental_whistle_converse import (
    converse_incremental_env as converse_incremental_env,
)
from tests.integration.test_whistle_stream_originals import BASE, acknowledge, begin, client_for


async def test_positive_retry_reports_original_process_release_after_unconfirmed_stop(
    converse_incremental_env, monkeypatch
):
    env = converse_incremental_env
    original_stop = env.broker.manager.stop_if_current
    calls = []

    async def unconfirmed_once(identity):
        calls.append(identity)
        if len(calls) == 1:
            return {
                "ok": True,
                "release_confirmed": False,
                "stop_evidence": {"reaped": False, "group_empty": False},
            }
        return await original_stop(identity)

    monkeypatch.setattr(env.broker.manager, "stop_if_current", unconfirmed_once)
    async with client_for(env) as client:
        original, _, proof = await begin(client, env)
        await acknowledge(client, original, proof)
        owned = env.broker.operation
        first = await client.post(BASE + "/ownership/abort", json=original)
        assert first.status_code == 200 and first.json()["process_released"] is False
        assert env.broker.operation is owned and owned.cleanup_failed
        second = await client.post(BASE + "/ownership/abort", json=original)
        assert second.status_code == 200
        assert len(calls) == 2 and calls[0] == calls[1] == owned.identity
        assert env.broker.operation is None and owned.cleanup_confirmed
        assert second.json()["process_released"] is True
        assert env.engine.begin_calls == 1 and env.engine.step == env.engine.finish_calls == 0


async def test_independent_ipc_close_failure_is_not_cleared_by_positive_native_retry(
    converse_incremental_env, monkeypatch
):
    env = converse_incremental_env
    stop, calls = env.broker.manager.stop_if_current, []

    async def first_unknown(identity):
        calls.append(identity)
        if len(calls) == 1:
            return {
                "ok": True,
                "release_confirmed": False,
                "stop_evidence": {"reaped": False, "group_empty": False},
            }
        return await stop(identity)

    monkeypatch.setattr(env.broker.manager, "stop_if_current", first_unknown)
    async with client_for(env) as client:
        original, _, proof = await begin(client, env)
        await acknowledge(client, original, proof)
        owned = env.broker.operation
        assert (await client.post(BASE + "/ownership/abort", json=original)).json()[
            "process_released"
        ] is False
        monkeypatch.setattr(env.broker.endpoint.stream_closes, "failed", True)
        second = await client.post(BASE + "/ownership/abort", json=original)
        assert second.status_code == 200 and len(calls) == 2
        assert owned.cleanup_confirmed and not owned.cleanup_failed
        assert env.broker.endpoint.stream_closes.failed is True
        assert second.json()["process_released"] is False
        assert env.engine.begin_calls == 1


async def test_explicit_converse_cancel_retry_retires_exact_lease_while_lookup_never_retries(
    converse_incremental_env, monkeypatch
):
    env = converse_incremental_env
    settings = install_settings(env)
    stop, calls = env.broker.manager.stop_if_current, []

    async def first_unknown(identity):
        calls.append(identity)
        if len(calls) == 1:
            return {
                "ok": True,
                "release_confirmed": False,
                "stop_evidence": {"reaped": False, "group_empty": False},
            }
        return await stop(identity)

    monkeypatch.setattr(env.broker.manager, "stop_if_current", first_unknown)
    prefix = "/v1/converse/audio/stream"
    try:
        started = await env.client.post(prefix + "/begin", json=scope_for(env))
        assert started.status_code == 200
        state = stream_state(env)
        cancel = {"stream_id": state.stream_id, "worker_incarnation": state.worker_incarnation}
        first = await env.client.post(prefix + "/cancel", json=cancel)
        assert first.status_code == 200 and first.json()["cleanup_confirmed"] is False
        assert len(calls) == 1 and settings.snapshot()["busy"] is True
        metadata = {
            "schema_version": "1.0",
            "protocol": "converse_stream_owner_v1",
            "owner_instance": (await env.client.get(prefix + "/ownership")).json()[
                "owner_instance"
            ],
            "request_sha256": digest(scope_for(env)),
        }
        observed = await env.client.post(prefix + "/begin/lookup", json=metadata)
        assert observed.status_code == 200 and observed.json()["process_released"] is False
        assert len(calls) == 1 and not state.lifetime.done()
        second = await env.client.post(prefix + "/cancel", json=cancel)
        assert second.status_code == 200 and second.json()["cleanup_confirmed"] is True
        await asyncio.wait_for(asyncio.shield(state.lifetime), 5)
        assert len(calls) == 2 and calls[0] == calls[1]
        assert not settings.leases and not settings._admissions()
        assert env.engine.begin_calls == 1 and env.engine.step == env.engine.finish_calls == 0
    finally:
        await settings.close()
