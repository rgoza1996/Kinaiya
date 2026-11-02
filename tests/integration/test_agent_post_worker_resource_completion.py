"""Observable post-worker completion oracles; synthetic inference, real owners.

The old ordinary and genuine standing fixtures are imported unchanged. No
completion-helper name, private future registry, or task factory is assumed.
"""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_agent_mode_permissions import bind_workspace
from test_agent_resource_restore_retirement import PriorCleanup
from test_agent_resource_restore_retirement import ordinary as ordinary
from test_resource_restore_model_retirement import gated_standing
from test_standing_agent_runtime import TEXT
from test_standing_agent_runtime import factory as factory
from test_standing_agent_runtime import stack as stack
from test_standing_agent_runtime import submit as standing_submit

from core.policy import ToolPolicy
from packages.contracts.models import TaskState
from packages.providers.text import ScriptedProvider
from services.agent.runtime import AgentRuntime
from services.agent.task_model_lease import TaskModelLease
from services.resources.client import ResourceClient
from services.session.store import LocalJournal


async def reached(event):
    await asyncio.wait_for(event.wait(), 5)


async def joined(task):
    return await asyncio.wait_for(asyncio.shield(task), 8)


async def eventually(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.005)


async def resistant(event):
    while not event.is_set():
        try:
            await event.wait()
        except asyncio.CancelledError:
            continue


def pending(runtime):
    state = runtime.workspace_operation_state()
    return sum(state[key] for key in ("workers", "ecosystem_operations", "cleanup_pending"))


class Observation:
    def __init__(self, value):
        self.value, self.calls, self.owner = value, [], None
        self.entered, self.ended = asyncio.Event(), asyncio.Event()

    def enter(self, task_id):
        value, owner = self.value, asyncio.current_task()
        cleanup = value.cleanup
        self.calls.append(
            {
                "task_id": task_id,
                "worker_done": value.worker.done(),
                "worker_success": value.worker.done()
                and not value.worker.cancelled()
                and value.worker.exception() is None,
                "different_owner": owner is not value.worker,
                "lease_settled": None if value.lease is None else value.lease.settled,
                "lease_failed": None if value.lease is None else value.lease.failed,
                "cleanup_done": None if cleanup is None else cleanup.done(),
                "cleanup_success": None
                if cleanup is None
                else cleanup.done() and not cleanup.cancelled() and cleanup.exception() is None,
            }
        )
        self.owner = owner
        owner.add_done_callback(lambda _: self.ended.set())
        self.entered.set()


class Callback:
    def __init__(self, observation, *, hold=False, error=None):
        self.observation, self.error = observation, error
        self.gate, self.close_entered = asyncio.Event(), asyncio.Event()
        self.close_calls, self.closed = 0, False
        if not hold:
            self.gate.set()

    async def task_finished(self, task_id):
        self.observation.enter(task_id)
        await resistant(self.gate)
        if self.error is not None:
            raise self.error
        return {"ok": True}

    async def close(self):
        self.close_calls += 1
        self.close_entered.set()
        self.closed = True


class Body(httpx.AsyncByteStream):
    def __init__(self, transport):
        self.transport, self.close_calls, self.close_owner = transport, 0, None

    async def __aiter__(self):
        self.transport.read_entered.set()
        await resistant(self.transport.read_gate)
        yield b'{"ok":true}'

    async def aclose(self):
        self.close_calls += 1
        self.close_owner = asyncio.current_task()
        self.transport.response_close_entered.set()
        await resistant(self.transport.response_close_gate)
        self.transport.response_closed.set()


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, observation, phase):
        self.observation, self.requests, self.bodies = observation, [], []
        self.acquire_gate, self.read_gate, self.response_close_gate = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.read_entered, self.response_close_entered, self.response_closed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.close_entered, self.close_gate, self.closed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.close_calls, self.close_owner = 0, None
        for name in ("acquire", "read", "response_close"):
            if name != phase:
                getattr(self, name + "_gate").set()

    async def handle_async_request(self, request):
        payload = json.loads(await request.aread())
        assert request.method == "POST" and request.url.path == "/v1/resources/actions"
        assert request.headers["Authorization"] == "Bearer " + "a" * 32
        self.requests.append(payload)
        self.observation.enter(payload["task_id"])
        await resistant(self.acquire_gate)
        body = Body(self)
        self.bodies.append(body)
        return httpx.Response(200, stream=body, request=request)

    async def aclose(self):
        self.close_calls += 1
        self.close_owner = asyncio.current_task()
        self.close_entered.set()
        await resistant(self.close_gate)
        self.closed.set()


class ObservableResourceClient(ResourceClient):
    @property
    def closed(self):
        return self.http.is_closed


@pytest.fixture
async def cases(ordinary, stack, tmp_path, record_property):
    opened = []

    async def make(*, standing=False, with_lease=True, hold=False, error=None, phase=None):
        if standing:
            old = await stack()
            close_gate, closing = gated_standing(old)
            runtime = old.runtime
            value = SimpleNamespace(
                old=old,
                runtime=runtime,
                lease=None,
                worker=None,
                cleanup=None,
                provider=None,
                record=None,
                task_id=old.task_id,
                close_gate=close_gate,
                closing=closing,
                restorations=[],
                callers=[],
                extra_clients=[],
                owner=old.workspace_owner,
            )
        else:
            old = await ordinary(with_lease=with_lease)
            runtime = old.runtime
            value = SimpleNamespace(
                old=old,
                runtime=runtime,
                lease=old.lease,
                worker=old.worker,
                cleanup=None,
                provider=old.provider,
                record=old.rec,
                task_id=old.task.task_id,
                close_gate=old.provider.close_gate,
                closing=old.provider.closing,
                restorations=[],
                callers=[],
                extra_clients=[],
                owner=None,
            )
        value.observation = Observation(value)
        value.transport = Transport(value.observation, phase) if phase is not None else None
        value.client = (
            ObservableResourceClient("http://127.0.0.1:8763", "a" * 32, transport=value.transport)
            if value.transport is not None
            else Callback(value.observation, hold=hold, error=error)
        )
        runtime.resource_client = value.client
        opened.append(value)
        if standing:
            await standing_submit(old)
            value.record, value.worker = old.record, old.record.worker
            value.lease, value.provider = old.record.model_lease, old.made[0]
        else:
            old.spy = value.client
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            if not hasattr(value.old, "original"):
                value.provider.generate_gate.set()
                for service in value.old.earlier:
                    service.release.set()
            value.close_gate.set()
            if isinstance(value.client, Callback):
                value.client.gate.set()
            if value.transport is not None:
                for gate in (
                    value.transport.acquire_gate,
                    value.transport.read_gate,
                    value.transport.response_close_gate,
                    value.transport.close_gate,
                ):
                    gate.set()
            await joined(asyncio.gather(value.worker, return_exceptions=True))
            if value.cleanup is not None:
                await joined(asyncio.gather(value.cleanup, return_exceptions=True))
            if value.observation.owner is not None:
                await joined(asyncio.gather(value.observation.owner, return_exceptions=True))
            for restore in reversed(value.restorations):
                restore()
            value.runtime.skill_service = value.runtime.plugin_service = None
            await joined(value.runtime.close())
            if value.callers:
                await joined(asyncio.gather(*value.callers, return_exceptions=True))
            if value.transport is not None and value.transport.close_owner is not None:
                await joined(value.transport.close_owner)
                assert value.transport.closed.is_set()
                assert all(body.close_calls == 1 for body in value.transport.bodies)
            for client in value.extra_clients:
                await client.close()
            if value.owner is not None:
                # Failed-capability cases intentionally retain the native lock.
                # Test teardown retires descriptors only after actual handles join.
                value.owner.close()
                if not value.owner._db_closed:
                    value.owner.db.close()
                    value.owner._db_closed = True
                value.owner._release_descriptors()
            record_property(
                "actual_cleanup",
                json.dumps(
                    {
                        "worker_done": value.worker.done(),
                        "lease_cleanup_done": value.cleanup is None or value.cleanup.done(),
                        "callback_owner_done": value.observation.owner is None
                        or value.observation.owner.done(),
                        "client_closed": value.client.closed,
                    }
                ),
            )
            assert value.worker.done() and value.client.closed


async def terminal_at_close(value):
    if not hasattr(value.old, "original"):
        value.provider.generate_gate.set()
    if value.lease is not None:
        await reached(value.closing)
        value.cleanup = value.lease.cleanup
        assert isinstance(value.cleanup, asyncio.Task) and not value.cleanup.done()
    final = await value.runtime.wait(value.task_id, timeout=5)
    assert final.state in {TaskState.SUCCEEDED, TaskState.FAILED}
    assert value.record.done.is_set()
    return final.model_dump(mode="json")


@pytest.mark.parametrize("mode", ["ordinary-lease", "ordinary-no-lease", "standing"])
async def test_notification_observes_original_successful_worker_and_actual_model_close(cases, mode):
    value = await cases(standing=mode == "standing", with_lease=mode != "ordinary-no-lease")
    truth = await terminal_at_close(value)
    if value.lease is not None:
        assert not value.worker.done() and value.observation.calls == []
    value.close_gate.set()
    await reached(value.observation.entered)
    await joined(value.worker)
    await reached(value.observation.ended)
    assert value.observation.calls == [
        {
            "task_id": value.task_id,
            "worker_done": True,
            "worker_success": True,
            "different_owner": True,
            "lease_settled": None if value.lease is None else True,
            "lease_failed": None if value.lease is None else False,
            "cleanup_done": None if value.lease is None else True,
            "cleanup_success": None if value.lease is None else True,
        }
    ]
    assert (await value.runtime.status(value.task_id)).model_dump(mode="json") == truth
    if mode == "standing":
        assert (value.old.workspace / "result.txt").read_text() == TEXT
        evidence = [item for item in value.record.task.evidence if item.kind == "file"]
        assert evidence[-1].sha256 == hashlib.sha256(TEXT.encode()).hexdigest()
        assert await value.old.journal.standing_unresolved() == value.old.claim


async def test_held_notification_has_ended_worker_but_retains_native_workspace(cases, tmp_path):
    value = await cases(hold=True)
    value.owner = bind_workspace(value.runtime, tmp_path / "held-notification-workspace")
    truth = await terminal_at_close(value)
    value.close_gate.set()
    await reached(value.observation.entered)
    observed = value.worker.done()
    assert observed, "the callback must be outside the original worker"
    assert value.observation.owner is not value.worker
    assert pending(value.runtime) > 0
    value.owner.close()
    assert value.owner._lock_fd is not None and not value.owner.release_if_settled()
    assert (await value.runtime.status(value.task_id)).model_dump(mode="json") == truth
    value.client.gate.set()
    await reached(value.observation.ended)
    await eventually(lambda: pending(value.runtime) == 0)
    await eventually(lambda: value.owner._lock_fd is None)


@pytest.mark.parametrize("phase,cancel_waiter", [("acquire", False), ("response_close", True)])
async def test_shutdown_retains_real_resource_http_until_original_callback_and_single_close_end(
    cases, phase, cancel_waiter
):
    value = await cases(phase=phase)
    truth = await terminal_at_close(value)
    value.close_gate.set()
    event = (
        value.observation.entered if phase == "acquire" else value.transport.response_close_entered
    )
    await reached(event)
    assert value.worker.done() and value.observation.owner is not value.worker
    assert pending(value.runtime) > 0
    first = asyncio.create_task(value.runtime.close())
    value.callers.append(first)
    await eventually(lambda: value.runtime.workspace_operation_state()["closed"])
    if cancel_waiter:
        first.cancel()
    await eventually(lambda: first.done() or value.transport.close_entered.is_set())
    assert not value.transport.close_entered.is_set() and not value.client.http.is_closed
    second = asyncio.create_task(value.runtime.close())
    value.callers.append(second)
    await eventually(lambda: second.done() or value.transport.close_entered.is_set())
    assert value.transport.close_calls == 0 and not value.observation.owner.done()
    getattr(value.transport, phase + "_gate").set()
    await reached(value.observation.ended)
    await reached(value.transport.close_entered)
    original_closer = value.transport.close_owner
    assert value.client.http.is_closed and not original_closer.done()
    entered = asyncio.Event()

    async def wait_close():
        entered.set()
        await value.runtime.close()

    third = asyncio.create_task(wait_close())
    value.callers.append(third)
    await reached(entered)
    third.cancel()
    await joined(asyncio.gather(third, return_exceptions=True))
    assert not original_closer.done() and value.transport.close_calls == 1
    value.transport.close_gate.set()
    await joined(original_closer)
    await joined(asyncio.gather(*value.callers, return_exceptions=True))
    assert value.transport.closed.is_set() and value.transport.close_calls == 1
    assert value.transport.requests == [
        {
            "operation": "task_finished",
            "task_id": value.task_id,
            "idempotency_key": "finished:" + value.task_id,
        }
    ]
    assert all(body.close_calls == 1 for body in value.transport.bodies)
    assert (await value.runtime.status(value.task_id)).model_dump(mode="json") == truth


@pytest.mark.parametrize("changed", ["record", "worker", "lease", "cleanup", "client"])
async def test_changed_captured_owner_cannot_redirect_notification(cases, changed):
    value = await cases()
    truth = await terminal_at_close(value)
    if changed == "record":
        value.runtime._tasks[value.task_id] = replace(value.record)
        value.restorations.append(
            lambda: value.runtime._tasks.update({value.task_id: value.record})
        )
    elif changed == "worker":
        replacement = asyncio.create_task(asyncio.sleep(0))
        await joined(replacement)
        value.record.worker = replacement
        value.restorations.append(lambda: setattr(value.record, "worker", value.worker))
    elif changed == "lease":
        replacement = TaskModelLease(lambda _: None)
        await replacement.close()
        value.record.model_lease = replacement
        value.restorations.append(lambda: setattr(value.record, "model_lease", value.lease))
    elif changed == "cleanup":
        value.lease.cleanup = SimpleNamespace(done=lambda: True, cancelled=lambda: False)
        value.restorations.append(lambda: setattr(value.lease, "cleanup", value.cleanup))
    else:
        replacement = Callback(Observation(value))
        value.runtime.resource_client = replacement
        value.extra_clients.append(replacement)
        value.restorations.append(lambda: setattr(value.runtime, "resource_client", value.client))
    value.close_gate.set()
    await joined(asyncio.gather(value.worker, return_exceptions=True))
    await eventually(lambda: pending(value.runtime) == 0)
    assert value.observation.calls == []
    if changed == "client":
        assert replacement.observation.calls == []
    assert value.record.task.model_dump(mode="json") == truth


@pytest.mark.parametrize(
    "changed", ["closing", "recovered", "nonterminal", "prior-uncertainty", "standing-uncertainty"]
)
async def test_no_new_authority_from_invalid_or_uncertain_record_after_real_close(cases, changed):
    value = await cases()
    truth = await terminal_at_close(value)
    if changed == "nonterminal":
        value.record.task.state = TaskState.PAUSED
    elif changed == "prior-uncertainty":
        value.record._cleanup_uncertain = True
    elif changed == "standing-uncertainty":
        value.record.standing_cleanup_uncertain = True
    else:
        setattr(value.record, changed, True)
    value.close_gate.set()
    await joined(asyncio.gather(value.worker, return_exceptions=True))
    await eventually(lambda: pending(value.runtime) == 0)
    assert value.lease.settled and value.provider.closed
    assert value.observation.calls == []
    if changed != "nonterminal":
        assert value.record.task.model_dump(mode="json") == truth


@pytest.mark.parametrize("failure", ["release", "lease-close", "cancelled-close-waiter"])
async def test_failed_original_cleanup_cannot_arm_notification_after_late_adapter_close(
    cases, failure
):
    value = await cases()
    if failure == "release":
        service = PriorCleanup("skill", [], fail=True)
        value.old.earlier.append(service)
        value.runtime.skill_service = service
        value.provider.generate_gate.set()
        await reached(service.entered)
        service.release.set()
    truth = await terminal_at_close(value)
    if failure == "lease-close":
        value.cleanup.cancel()
    elif failure == "cancelled-close-waiter":
        value.worker.cancel()
    value.close_gate.set()
    await joined(asyncio.gather(value.worker, value.cleanup, return_exceptions=True))
    if failure == "lease-close":
        await value.provider.close()
        assert value.provider.closed and value.cleanup.cancelled() and not value.lease.settled
    await eventually(lambda: pending(value.runtime) == 0)
    assert value.observation.calls == []
    assert value.record.task.model_dump(mode="json") == truth


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_failed_notification_is_accounted_sticky_without_changing_work_truth_or_retry(
    cases, error
):
    value = await cases(error=error("Synthetic original notification failure"))
    truth = await terminal_at_close(value)
    value.close_gate.set()
    await reached(value.observation.entered)
    await reached(value.observation.ended)
    assert value.worker.done() and not value.worker.cancelled() and value.worker.exception() is None
    assert value.runtime.workspace_operation_state()["cleanup_failed"] > 0
    await value.runtime.close()
    await value.runtime.close()
    assert value.runtime.workspace_operation_state()["cleanup_failed"] > 0
    assert len(value.observation.calls) == 1
    assert value.record.task.model_dump(mode="json") == truth


async def test_real_recovery_is_read_only_and_never_replays_resource_completion(cases):
    value = await cases()
    truth = await terminal_at_close(value)
    value.close_gate.set()
    await reached(value.observation.entered)
    await reached(value.observation.ended)
    await value.runtime.close()
    recovered = AgentRuntime(
        LocalJournal(value.old.store, "agent"),
        ScriptedProvider([]),
        value.runtime.workspace,
        ToolPolicy(value.runtime.workspace),
    )
    spy = Callback(Observation(value))
    recovered.resource_client = spy
    try:
        result = await recovered.recover()
        assert len(result) == 1 and result[0].model_dump(mode="json") == truth
        assert recovered._tasks[value.task_id].recovered
        assert recovered._tasks[value.task_id].worker is None
        await recovered.close()
        assert spy.observation.calls == [] and spy.close_calls == 1
    finally:
        if not spy.closed:
            await recovered.close()
