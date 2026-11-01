"""Independent #199 checks before a native submit reservation exists.

Only synthetic native read scheduling and the runtime-local monotonic clock
are controlled. SQLite, source authority and the integration lock stay real.
"""

import asyncio

import pytest
from test_agent_submit_native_revision import table_rows
from test_agent_submit_recovery_authority import receipt_traps
from test_agent_submit_runtime_deadlines import clock as clock
from test_agent_submit_uncertainty import (
    assert_no_worker,
    checkpoint,
    outcome,
    request,
    settled,
    start,
)
from test_agent_submit_uncertainty import env as env

from packages.contracts.agent_submit import AgentSubmitUnavailable

NATIVE_METHODS = ("session", "resolve", "entry", "read", "lookup_submit", "admit_submit")


class ResistantPreflightRead:
    """Return a cached first native response after repeated cancellation."""

    def __init__(self, journal, method):
        self.original = {name: getattr(journal, name) for name in NATIVE_METHODS}
        self.method = method
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.operation = None
        self.cancellations = 0
        self.retired = False
        self.calls_after_retirement = []

    def wrapper(self, name):
        async def invoke(*args, **kwargs):
            if self.retired:
                self.calls_after_retirement.append(name)
            value = await self.original[name](*args, **kwargs)
            if name == self.method and not self.entered.is_set():
                self.operation = asyncio.current_task()
                self.entered.set()
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        self.cancellations += 1
            return value

        return invoke


async def assert_lock_released(runtime):
    await asyncio.wait_for(runtime.integration_lock.acquire(), timeout=2)
    runtime.integration_lock.release()


@pytest.mark.parametrize("method,retire_by", [("session", "caller_cancel"), ("resolve", "close")])
async def test_retired_first_preflight_read_cannot_continue(env, monkeypatch, method, retire_by):
    gate = ResistantPreflightRead(env.journal, method)
    for name in NATIVE_METHODS:
        monkeypatch.setattr(env.journal, name, gate.wrapper(name))
    traps = receipt_traps(env.runtime, monkeypatch)
    before = table_rows(env.store.db)
    operation = start(env, request(env))
    try:
        await asyncio.wait_for(gate.entered.wait(), timeout=2)
        assert await env.runtime.status() == []
        assert env.runtime._pending_submits == env.runtime._submit_idem == {}
        assert gate.operation is not operation
        assert gate.operation in env.runtime._submit_operations
        gate.retired = True
        if retire_by == "close":
            await asyncio.wait_for(env.runtime.close(), timeout=2)
            assert env.runtime._closed
            result = await settled(operation)
            assert isinstance(result, AgentSubmitUnavailable)
        else:
            assert operation.cancel()
            result = await settled(operation)
            assert isinstance(result, asyncio.CancelledError)
        await checkpoint()
        assert gate.cancellations >= 1, "The first native read did not receive cancellation"
        assert not gate.operation.done()
        assert gate.operation in env.runtime._submit_operations
        await assert_lock_released(env.runtime)
    finally:
        gate.release.set()
        if gate.operation is not None:
            await asyncio.wait_for(
                asyncio.gather(gate.operation, return_exceptions=True), timeout=2
            )
        await checkpoint()
    assert gate.operation not in env.runtime._submit_operations
    assert gate.calls_after_retirement == []
    assert await env.runtime.status() == []
    assert env.runtime._pending_submits == env.runtime._submit_idem == {}
    assert env.journal.attempts == []
    assert table_rows(env.store.db) == before
    assert all(trap.call_count == 0 for trap in traps)
    assert_no_worker(env)


async def test_preflight_uses_one_total_budget_and_releases_integration_lock(
    env, clock, monkeypatch
):
    original = {name: getattr(env.journal, name) for name in NATIVE_METHODS}
    calls = []

    def tracked(name):
        async def invoke(*args, **kwargs):
            calls.append(name)
            value = await original[name](*args, **kwargs)
            if len(calls) == 1:
                clock.now += 3
            elif len(calls) == 2:
                clock.now += 2.01
            return value

        return invoke

    for name in NATIVE_METHODS:
        monkeypatch.setattr(env.journal, name, tracked(name))
    traps = receipt_traps(env.runtime, monkeypatch)
    before = table_rows(env.store.db)
    started = clock.now
    result = await outcome(env.runtime.submit(request(env)))
    assert clock.now - started > 5
    assert isinstance(result, AgentSubmitUnavailable)
    await assert_lock_released(env.runtime)
    await checkpoint()
    assert len(calls) == 2
    assert "admit_submit" not in calls
    assert await env.runtime.status() == []
    assert env.runtime._pending_submits == env.runtime._submit_idem == {}
    assert env.runtime._submit_operations == set()
    assert env.journal.attempts == []
    assert table_rows(env.store.db) == before
    assert all(trap.call_count == 0 for trap in traps)
    assert_no_worker(env)
