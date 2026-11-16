"""Actual native setup availability stays authoritative with safe diagnostics."""

import pytest
from native_startup_observers import observe_standing_read
from test_standing_execution_api import factory as factory
from test_standing_execution_api import headers, original

from packages.contracts.standing_dispatch import StandingDispatchState


async def test_real_initial_dispatch_read_is_unchanged_and_observer_is_retired(factory):
    env = await factory()
    before = env.operations.run
    with observe_standing_read(env.operations) as failures:
        reply = await env.client.get("/v1/standing/dispatch", headers=headers())
    assert reply.status_code == 200
    StandingDispatchState.from_json(reply.content)
    assert failures == [] and env.operations.run == before
    assert "run" not in vars(env.operations)


async def test_original_setup_refuses_real_closed_authority_once_with_safe_context(
    factory, monkeypatch
):
    env = await factory()
    before = env.operations.run
    await env.operations.close()
    get, calls, replies = env.client.get, [], []

    async def count(*args, **kwargs):
        calls.append(args[0])
        reply = await get(*args, **kwargs)
        replies.append(reply)
        return reply

    monkeypatch.setattr(env.client, "get", count)
    with pytest.raises(AssertionError) as raised:
        await original(env)
    assert calls == ["/v1/standing/dispatch"]
    assert len(replies) == 1 and replies[0].status_code == 503
    assert replies[0].json() == {"detail": "Standing work authority is unavailable"}
    detail = str(raised.value)
    assert "'owner_closed': True" in detail, "First setup failure lost native diagnostic context"
    assert "'active_operations': 0" in detail
    assert env.operations.run == before and "run" not in vars(env.operations)
    assert env.operations._closed and env.operations.active_operations == 0
