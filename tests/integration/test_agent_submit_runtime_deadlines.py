"""#199: elapsed deadlines still apply when a completed operation wins scheduling.

The injected clock advances at a real native boundary, without wall-clock sleeps
or changing the event loop's clock. SQLite admission and receipt validation stay
real; only the worker is the existing no-inference observer.
"""

import importlib
import time
from types import SimpleNamespace

import pytest
from test_agent_submit_uncertainty import assert_no_worker, checkpoint, request, submits
from test_agent_submit_uncertainty import env as env

from packages.contracts.agent_submit import AgentSubmitUnavailable, AgentSubmitUnconfirmed
from packages.contracts.models import TaskState


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=100.0)
    monkeypatch.setattr(
        importlib.import_module("services.agent.runtime"),
        "time",
        SimpleNamespace(monotonic=lambda: value.now, time=time.time),
    )
    return value


async def test_direct_created_after_elapsed_admission_deadline_never_launches(
    env, clock, monkeypatch
):
    original = env.journal.admit_submit

    async def expired_ack(body):
        receipt = await original(body)
        clock.now += 15.01
        return receipt

    monkeypatch.setattr(env.journal, "admit_submit", expired_ack)
    body = request(env)
    with pytest.raises(AgentSubmitUnconfirmed):
        await env.runtime.submit(body)
    await checkpoint()
    assert_no_worker(env)
    assert len(submits(env)) == 1
    assert len(env.journal.attempts) == 1
    reconciled = await env.runtime.submit(body)
    assert reconciled.task_id == submits(env)[0].payload["task_id"]
    assert reconciled.state == TaskState.PAUSED
    await checkpoint()
    assert_no_worker(env)
    assert len(env.journal.attempts) == 1


async def test_lookup_after_elapsed_lookup_deadline_cannot_start_admission(env, clock, monkeypatch):
    original = env.journal.lookup_submit

    async def expired_lookup(query):
        result = await original(query)
        clock.now += 5.01
        return result

    monkeypatch.setattr(env.journal, "lookup_submit", expired_lookup)
    with pytest.raises(AgentSubmitUnavailable):
        await env.runtime.submit(request(env))
    await checkpoint()
    assert_no_worker(env)
    assert env.journal.attempts == []
    assert submits(env) == []


async def test_fresh_authority_after_elapsed_deadline_never_launches(env, clock, monkeypatch):
    original = env.runtime._fresh_submit_authority

    async def expired_authority(pending):
        await original(pending)
        clock.now += 5.01

    monkeypatch.setattr(env.runtime, "_fresh_submit_authority", expired_authority)
    body = request(env)
    with pytest.raises(AgentSubmitUnavailable):
        await env.runtime.submit(body)
    await checkpoint()
    assert_no_worker(env)
    assert len(submits(env)) == 1
    assert len(env.journal.attempts) == 1
    retained = await env.runtime.submit(body)
    assert retained.state == TaskState.PAUSED
    await checkpoint()
    assert_no_worker(env)
    assert len(env.journal.attempts) == 1
