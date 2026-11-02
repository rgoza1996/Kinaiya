"""Independent restoration oracles frozen before the #239 ordering repair.

AgentRuntime, TaskModelLease and native journals are real. Standing cases reuse
the unchanged native claim/model fixture and its measured synthetic inference.
Ordinary cases explicitly own a synthetic adapter in an actual task lease; no
production cleanup method is patched. No devices, models or providers are used.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from test_standing_agent_runtime import TEXT
from test_standing_agent_runtime import factory as factory
from test_standing_agent_runtime import stack as stack
from test_standing_agent_runtime import submit as standing_submit

from core.policy import ToolPolicy
from packages.contracts.models import InterruptRequest, SessionRequest, TaskRequest, TaskState
from packages.providers.text import ProviderCleanupError, ProviderError, ScriptedProvider
from services.agent.runtime import AgentRuntime
from services.agent.task_model_lease import TaskModelLease
from services.session.store import LocalJournal, Store


async def joined(operation):
    return await asyncio.wait_for(asyncio.shield(operation), 5)


async def reached(event):
    await asyncio.wait_for(event.wait(), 5)


class ResourceSpy:
    def __init__(self, observed, *, error=None):
        self.observed = observed
        self.error = error
        self.calls = []
        self.called = asyncio.Event()
        self.closed = False

    async def task_finished(self, task_id):
        self.calls.append({"task_id": task_id, **self.observed()})
        self.called.set()
        if self.error is not None:
            raise self.error
        return {"ok": True}

    async def close(self):
        self.closed = True


def standing_spy(value, *, error=None):
    def observed():
        lease = value.record.model_lease
        return {
            "lease_settled": lease.settled,
            "adapter_closed": value.made[0].closed,
            "shared_base_closed": value.base.closed,
            "cleanup_uncertain": value.record.standing_cleanup_uncertain,
        }

    spy = ResourceSpy(observed, error=error)
    value.runtime.resource_client = spy
    return spy


def gated_standing(value, *, prepare_error=None, close_error=None):
    gate, entered = asyncio.Event(), asyncio.Event()
    original_factory = value.runtime.model_settings.host_factory

    def fresh(effort):
        provider = original_factory(effort)
        provider.close_gate = gate
        provider.closing = entered
        provider.prepare_error = prepare_error
        provider.close_error = close_error
        return provider

    value.runtime.model_settings.host_factory = fresh
    return gate, entered


def record_observation(record_property, spy, **extra):
    record_property("retirement_observations", json.dumps({"callbacks": spy.calls, **extra}))


@pytest.mark.parametrize("callback_error", [False, True], ids=["ack", "callback-failure"])
async def test_restore_waits_for_actual_owned_model_close_and_keeps_work_truth(
    stack, record_property, callback_error
):
    value = await stack()
    spy = standing_spy(
        value,
        error=RuntimeError("Synthetic restore transport failed") if callback_error else None,
    )
    gate, entered = gated_standing(value)
    await standing_submit(value)
    final = await value.runtime.wait(value.task_id, timeout=5)
    await reached(entered)
    pending_callbacks = list(spy.calls)
    pending = {
        "native_done": value.record.done.is_set(),
        "worker_done": value.record.worker.done(),
        "lease_settled": value.record.model_lease.settled,
        "adapter_closed": value.made[0].closed,
    }
    gate.set()
    await joined(value.record.worker)
    record_observation(record_property, spy, pending=pending, pending_callbacks=pending_callbacks)
    assert final.state == TaskState.SUCCEEDED
    assert pending == {
        "native_done": True,
        "worker_done": False,
        "lease_settled": False,
        "adapter_closed": False,
    }
    assert pending_callbacks == [], "terminal/done is not actual model retirement"
    assert spy.calls == [
        {
            "task_id": value.task_id,
            "lease_settled": True,
            "adapter_closed": True,
            "shared_base_closed": False,
            "cleanup_uncertain": False,
        }
    ]
    evidence = [item for item in final.evidence if item.kind == "file"]
    assert evidence and evidence[-1].sha256 == hashlib.sha256(TEXT.encode()).hexdigest()
    assert (value.workspace / "result.txt").read_text() == TEXT
    assert (await value.runtime.status(value.task_id)).model_dump() == final.model_dump()
    assert await value.journal.standing_unresolved() == value.claim
    assert value.made[0].close_calls == 1 and value.base.closed is False


@pytest.mark.parametrize("where", ["prepare", "close"])
async def test_standing_cleanup_uncertainty_never_restores_even_after_normal_close(
    stack, record_property, where
):
    value = await stack()
    spy = standing_spy(value)
    failure = ProviderCleanupError("Synthetic owned cleanup is unconfirmed")
    gate, entered = gated_standing(
        value,
        prepare_error=failure if where == "prepare" else None,
        close_error=failure if where == "close" else None,
    )
    await standing_submit(value)
    final = await value.runtime.wait(value.task_id, timeout=5)
    await reached(entered)
    gate.set()
    await joined(value.record.worker)
    lease = value.record.model_lease
    record_observation(
        record_property,
        spy,
        where=where,
        lease_settled=lease.settled,
        lease_failed=lease.failed,
        sticky=value.record.standing_cleanup_uncertain,
    )
    assert final.state == (TaskState.FAILED if where == "prepare" else TaskState.SUCCEEDED)
    assert value.record.done.is_set() and value.record.standing_cleanup_uncertain is True
    assert lease.settled is (where == "prepare")
    assert value.made[0].closed is (where == "prepare")
    assert spy.calls == []
    assert await value.journal.standing_unresolved() == value.claim
    assert value.base.closed is False


async def test_cancelled_close_waiter_never_restores_after_shielded_cleanup_later_settles(
    stack, record_property
):
    value = await stack()
    spy = standing_spy(value)
    gate, entered = gated_standing(value)
    await standing_submit(value)
    final = await value.runtime.wait(value.task_id, timeout=5)
    await reached(entered)
    worker, lease = value.record.worker, value.record.model_lease
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await joined(worker)
    retained_pending = lease.cleanup is not None and not lease.cleanup.done()
    before_late_settlement = list(spy.calls)
    gate.set()
    await joined(lease.cleanup)
    record_observation(
        record_property,
        spy,
        retained_pending=retained_pending,
        before_late_settlement=before_late_settlement,
    )
    assert final.state == TaskState.SUCCEEDED and value.record.done.is_set()
    assert retained_pending and lease.settled and value.made[0].closed
    assert before_late_settlement == [] and spy.calls == []
    assert value.base.closed is False
    assert await value.journal.standing_unresolved() == value.claim


async def test_no_resource_client_still_closes_the_exact_owned_lease(stack):
    value = await stack()
    gate, entered = gated_standing(value)
    assert value.runtime.resource_client is None
    await standing_submit(value)
    await reached(entered)
    lease = value.record.model_lease
    assert type(lease) is TaskModelLease and lease.adapters == [value.made[0]]
    assert not lease.settled and not value.record.worker.done()
    gate.set()
    await joined(value.record.worker)
    assert lease.settled and value.made[0].closed and value.base.closed is False


async def test_closing_changed_during_pending_close_still_suppresses_restore(stack):
    value = await stack()
    spy = standing_spy(value)
    gate, entered = gated_standing(value)
    await standing_submit(value)
    await reached(entered)
    value.record.closing = True
    gate.set()
    await joined(value.record.worker)
    assert value.record.model_lease.settled and value.made[0].closed
    assert value.record.task.state == TaskState.SUCCEEDED and value.record.done.is_set()
    assert spy.calls == []


async def test_unconfirmed_native_completion_is_nonterminal_without_closing_and_never_restores(
    stack, monkeypatch
):
    value = await stack()
    spy = standing_spy(value)
    gate, entered = gated_standing(value)
    append = value.journal.append
    refused = []

    async def unavailable_outcome(request):
        if request.kind == "agent.outcome":
            refused.append(request)
            raise RuntimeError("Synthetic final outcome unavailable before commit")
        return await append(request)

    monkeypatch.setattr(value.journal, "append", unavailable_outcome)
    await standing_submit(value)
    await reached(entered)
    gate.set()
    await joined(value.record.worker)
    assert len(refused) == 1
    assert value.record.task.state == TaskState.PAUSED and value.record.done.is_set()
    assert value.record.completion_pending and value.record.closing is False
    assert value.record.model_lease.settled and value.made[0].closed
    assert spy.calls == []


class OrdinaryAdapter(ScriptedProvider):
    def __init__(self, error):
        super().__init__([error])
        self.generating, self.generate_gate = asyncio.Event(), asyncio.Event()
        self.closing, self.close_gate = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.close_calls = 0

    async def complete(self, messages):
        self.generating.set()
        await self.generate_gate.wait()
        return await super().complete(messages)

    async def close(self):
        self.close_calls += 1
        self.closing.set()
        await self.close_gate.wait()
        self.closed = True


@dataclass
class PriorCleanup:
    name: str
    trace: list
    fail: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def release_task(self, task_id):
        self.trace.append(self.name + "-entered")
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise ProviderCleanupError("Synthetic earlier provider cleanup is unconfirmed")
        self.trace.append(self.name + "-settled")

    def close(self):
        pass


@pytest.fixture
async def ordinary(tmp_path):
    opened = []

    async def make(*, cleanup_error=False, with_lease=True, resource_error=None):
        path = tmp_path / str(len(opened))
        path.mkdir()
        store = Store(path / "session.sqlite")
        session = store.create_session(SessionRequest(channel="agent"))
        error = (
            ProviderCleanupError("Synthetic earlier request cleanup is unconfirmed")
            if cleanup_error
            else ProviderError("Synthetic inference failed without cleanup uncertainty")
        )
        provider = OrdinaryAdapter(error)
        runtime = AgentRuntime(LocalJournal(store, "agent"), provider, path, ToolPolicy(path))
        value = SimpleNamespace(
            runtime=runtime,
            store=store,
            provider=provider,
            rec=None,
            worker=None,
            lease=None,
            spy=None,
            earlier=[],
        )
        opened.append(value)
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Public synthetic task for restoration ordering",
                idempotency_key="restore-order",
            )
        )
        await reached(provider.generating)
        rec = runtime._tasks[task.task_id]
        released = []
        lease = TaskModelLease(released.append) if with_lease else None
        if lease is not None:
            # Install the exact actual lease while this task's inference is
            # explicitly held. Its adapter is the task's captured provider;
            # the production run/finally and lease close remain untouched.
            lease.own(rec.provider)
            rec.model_lease = lease
        spy = ResourceSpy(
            lambda: {
                "lease_settled": None if lease is None else lease.settled,
                "adapter_closed": provider.closed,
                "cleanup_uncertain": rec.standing_cleanup_uncertain,
            },
            error=resource_error,
        )
        runtime.resource_client = spy
        value.task, value.rec, value.worker = task, rec, rec.worker
        value.lease, value.released, value.spy = lease, released, spy
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            value.provider.generate_gate.set()
            value.provider.close_gate.set()
            for service in value.earlier:
                service.release.set()
            workers = {
                rec.worker for rec in value.runtime._tasks.values() if rec.worker is not None
            }
            if value.worker is not None:
                workers.add(value.worker)
            if workers:
                await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), timeout=5)
            if value.lease is not None and value.lease.cleanup is not None:
                await asyncio.wait_for(
                    asyncio.gather(value.lease.cleanup, return_exceptions=True), timeout=5
                )
            value.runtime.skill_service = value.runtime.plugin_service = None
            try:
                await asyncio.wait_for(value.runtime.close(), 5)
            finally:
                value.store.close()
            assert all(worker.done() for worker in workers)
            assert value.lease is None or value.lease.cleanup.done()
            assert value.spy is None or value.spy.closed


@pytest.mark.parametrize("with_lease", [True, False], ids=["owned-lease", "no-lease"])
async def test_ordinary_request_cleanup_error_blocks_restore_after_later_close(
    ordinary, record_property, with_lease
):
    value = await ordinary(cleanup_error=True, with_lease=with_lease)
    value.provider.generate_gate.set()
    if with_lease:
        await reached(value.provider.closing)
        value.provider.close_gate.set()
    await joined(value.worker)
    record_observation(record_property, value.spy, with_lease=with_lease)
    assert value.rec.task.state == TaskState.FAILED and value.rec.done.is_set()
    if with_lease:
        assert value.lease.settled and value.provider.closed and value.released == [value.lease]
    else:
        assert value.provider.close_calls == 0
    assert value.spy.calls == []


@pytest.mark.parametrize("prior_failure", [False, True], ids=["settled", "uncertain"])
async def test_prior_skill_plugin_cleanup_settles_before_model_and_restore(
    ordinary, record_property, prior_failure
):
    value = await ordinary()
    trace = []
    skill = PriorCleanup("skill", trace, fail=prior_failure)
    plugin = PriorCleanup("plugin", trace)
    value.earlier.extend([skill, plugin])
    # Inference has already captured its inputs. Only the real final cleanup
    # now sees these controlled release owners; no dispatch method is replaced.
    value.runtime.skill_service, value.runtime.plugin_service = skill, plugin
    value.provider.generate_gate.set()
    await reached(skill.entered)
    assert value.lease.cleanup is None and value.spy.calls == []
    skill.release.set()
    if not prior_failure:
        await reached(plugin.entered)
        assert value.lease.cleanup is None and value.spy.calls == []
        plugin.release.set()
    await reached(value.provider.closing)
    pending_callbacks = list(value.spy.calls)
    value.provider.close_gate.set()
    outcome = await joined(asyncio.gather(value.worker, return_exceptions=True))
    record_observation(record_property, value.spy, trace=trace, pending_callbacks=pending_callbacks)
    assert value.lease.settled and value.provider.closed
    assert value.rec.task.state == TaskState.FAILED and value.rec.done.is_set()
    if prior_failure:
        assert isinstance(outcome[0], ProviderCleanupError)
        assert not plugin.entered.is_set() and trace == ["skill-entered"]
        assert value.spy.calls == []
    else:
        assert outcome == [None]
        assert trace == ["skill-entered", "skill-settled", "plugin-entered", "plugin-settled"]
        assert pending_callbacks == []
        assert len(value.spy.calls) == 1 and value.spy.calls[0]["lease_settled"] is True


async def test_runtime_shutdown_keeps_nonterminal_closing_suppression_and_joins_lease(ordinary):
    value = await ordinary()
    closer = asyncio.create_task(value.runtime.close())
    try:
        await reached(value.provider.closing)
        pending = not closer.done() and not value.lease.cleanup.done()
        value.provider.close_gate.set()
        await joined(closer)
        assert pending and value.rec.closing
        assert value.rec.task.state == TaskState.PAUSED and value.rec.done.is_set()
        assert value.lease.settled and value.provider.closed
        assert value.spy.calls == [] and value.spy.closed
    finally:
        value.provider.close_gate.set()
        await asyncio.wait_for(asyncio.gather(closer, return_exceptions=True), 5)


async def test_inference_cancellation_with_actual_positive_cleanup_keeps_terminal_callback(
    ordinary,
):
    value = await ordinary()
    response = await value.runtime.interrupt(
        value.task.task_id,
        InterruptRequest(expected_objective_revision=1, idempotency_key="owner-cancel"),
    )
    assert response.state == TaskState.CANCEL_REQUESTED
    await reached(value.provider.closing)
    value.provider.close_gate.set()
    await joined(value.worker)
    assert value.rec.task.state == TaskState.CANCELLED and value.rec.done.is_set()
    assert value.lease.settled and value.provider.closed
    assert len(value.spy.calls) == 1
    assert value.spy.calls[0]["lease_settled"] is True


@pytest.mark.parametrize("callback_error", [False, True], ids=["ack", "callback-failure"])
async def test_no_lease_compatible_callback_preserves_native_terminal_result(
    ordinary, callback_error
):
    value = await ordinary(
        with_lease=False,
        resource_error=RuntimeError("Synthetic restore transport failed")
        if callback_error
        else None,
    )
    value.provider.generate_gate.set()
    await joined(value.worker)
    assert value.rec.task.state == TaskState.FAILED and value.rec.done.is_set()
    assert len(value.spy.calls) == 1 and value.spy.calls[0]["task_id"] == value.task.task_id
    assert value.spy.calls[0]["lease_settled"] is None
    assert value.provider.close_calls == 0 and value.provider.closed is False
