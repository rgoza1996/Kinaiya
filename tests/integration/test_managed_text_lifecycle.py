"""Registered common lifecycle over the existing owned synthetic Qwen engine."""

import asyncio
import json
from contextlib import aclosing
from pathlib import Path

import pytest
from test_managed_qwen3 import consume, request, wait_for
from test_managed_qwen3 import env as env
from test_managed_qwen3 import native_env as native_env


@pytest.mark.parametrize("operation", ["off", "handoff"])
async def test_registered_owner_barrier_joins_real_worker_and_held_source(
    native_env, monkeypatch, operation
):
    host = native_env
    host.coordinator.register_text_lifecycle(
        abort=host.broker.abort_active,
        drain=host.broker.drain,
        release=host.endpoint.release_after_stop,
    )
    if operation == "handoff":
        assert (
            await host.coordinator.action(
                {
                    "operation": "profile",
                    "profile": "local_work_priority",
                    "idempotency_key": "registered-profile",
                }
            )
        )["ok"]
    release, closing = asyncio.Event(), asyncio.Event()
    original = host.endpoint.stream

    async def held(messages, identity):
        try:
            async with aclosing(original(messages, identity)) as source:
                async for frame in source:
                    yield frame
        finally:
            closing.set()
            await release.wait()

    monkeypatch.setattr(host.endpoint, "stream", held)
    old = host.manager.owned_identity("qwen3")
    body = await host.broker.stream(request("after-delta")[0])
    assert json.loads(await anext(body))["kind"] == "delta"
    action = (
        {"operation": "converse", "enabled": False}
        if operation == "off"
        else {
            "operation": "handoff",
            "required_bytes": 128 * 1024**2,
        }
    )
    task = asyncio.create_task(
        host.coordinator.action({**action, "idempotency_key": "registered-" + operation})
    )
    try:
        async with asyncio.timeout(5):
            await closing.wait()
            await wait_for(lambda: not Path(f"/proc/{old.pid}").exists())
        assert not task.done()
        if operation == "handoff":
            assert host.coordinator.state.record.phase == "unloading"
        else:
            # Off's existing durable intent is the admission fence while its
            # release is pending; it does not publish successful off early.
            assert not host.coordinator.intent.read().enabled
            assert host.coordinator.state.record.phase != "off"
        assert host.broker.operation is body.operation
        assert not body.operation.cleanup_confirmed
    finally:
        release.set()
    async with asyncio.timeout(5):
        assert (await task)["ok"]
    assert await consume(body) == [{"kind": "error", "code": "paused"}]
    assert host.broker.operation is None and host.endpoint.ready is None
    assert not Path(f"/proc/{old.pid}").exists()
    restored = (
        {"operation": "converse", "enabled": True}
        if operation == "off"
        else {"operation": "restore"}
    )
    assert (await host.coordinator.action({**restored, "idempotency_key": "registered-restore"}))[
        "ok"
    ]
    current = host.manager.owned_identity("qwen3")
    assert current is not None and current is not old
    monkeypatch.setattr(host.endpoint, "stream", original)
    assert (await consume(await host.broker.stream(request()[0])))[-1]["kind"] == "done"
