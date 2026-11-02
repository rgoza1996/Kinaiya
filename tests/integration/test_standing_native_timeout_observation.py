"""Real held native-report observation, not a COMMIT or retirement certificate."""

import asyncio
import builtins
import io
import json
import os
import socket
import threading
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import test_standing_physical_finalization as physical_module
from test_standing_physical_finalization import factory as factory
from test_standing_physical_finalization import physical as physical
from test_standing_physical_timeout_observation import KEYS, nodes, task_evidence

from packages.contracts.standing_resource_finalization import StandingResourceFinalizationReceipt

PRIVATE = "PUBLIC-SYNTHETIC-NATIVE-OBSERVER-PRIVATE-21ef904b"


def cached(value, public, operations, shared, control, calls):
    """Test-local state only; never included in diagnostic notes/properties."""
    owner = value.coordinator
    scopes = sorted(owner.standing_barrier.operations, key=id)
    exchanges = sorted(value.gateway._submit_exchanges, key=id)
    return (
        id(public),
        public.done(),
        public.cancelled(),
        public.cancelling(),
        id(operations),
        id(shared),
        id(operations._operations),
        id(shared._operations),
        operations._closed,
        shared._closed,
        tuple(
            (id(task), id(event), task.done(), task.cancelled(), task.cancelling(), event.is_set())
            for task, event in sorted(operations._operations.items(), key=lambda pair: id(pair[0]))
        ),
        tuple(
            (
                name,
                id(call),
                id(work),
                work.done(),
                work.cancelled(),
                work.cancelling(),
                call.active,
            )
            for name, call, work in value.original_calls
        ),
        tuple(
            (id(scope), tuple(sorted(map(id, scope.handles))), scope.failed, scope.finished)
            for scope in scopes
        ),
        tuple(
            (
                id(exchange),
                id(exchange.task),
                id(exchange.response),
                id(exchange.close_task),
                exchange.task.done(),
                exchange.task.cancelled(),
                exchange.task.cancelling(),
                exchange.abandoned,
                exchange.close_started,
            )
            for exchange in exchanges
        ),
        tuple(
            (id(body), body.closes, body.closed, body.failed, body.cancelled)
            for body in value.native.bodies + value.control_transport.bodies
        ),
        tuple(
            (
                id(worker),
                id(worker.process),
                worker.state,
                worker.ready,
                None if worker.process is None else worker.process.returncode,
                id(worker.pending_spawn),
                id(worker.pending_cleanup),
                tuple(map(id, worker.drains)),
            )
            for worker in owner.manager._workers.values()
        ),
        tuple(sorted(map(id, owner.standing_handoff.attempts.values()))),
        tuple(sorted(map(id, owner.standing_handoff.operations))),
        tuple(sorted(map(id, owner.standing_handoff.native))),
        len(value.native.requests),
        len(value.control_transport.requests),
        value.native.close_calls,
        value.control_transport.close_calls,
        control.entered.is_set(),
        control.release.is_set(),
        control.tail.is_set(),
        len(calls),
        tuple(value.starts),
        tuple(value.stops),
        tuple(value.memories),
        value.effect_checks,
        owner.lock.locked(),
        owner.admission_lock.locked(),
        owner.manager._lock.locked(),
        owner._closing,
        owner._closed,
        owner.standing_barrier.failed,
        owner.standing_barrier.uncertain,
        value.gateway._submit_cleanup_failed,
        owner.state.record.model_dump_json(),
    )


async def test_snapshot_retains_actual_native_report_task_event_and_shared_registry(
    physical, monkeypatch
):
    snapshot = getattr(physical_module, "_physical_timeout_snapshot", None)
    assert callable(snapshot)
    value = await physical()
    await physical_module.bound(value)
    await physical_module.closure(value)
    starts_before, stops_before = tuple(value.starts), tuple(value.stops)
    operations = value.session.app.state.standing_resource_finalization_operations
    shared = value.session.app.state.standing_owner_operations
    registry = operations._operations
    assert type(registry) is dict and registry is shared._operations
    assert not operations._closed and not shared._closed

    original_report = operations.owner.report
    control = SimpleNamespace(
        entered=threading.Event(),
        release=threading.Event(),
        tail=threading.Event(),
        private=PRIVATE,
    )
    # Enroll before starting work: even the expected cold descriptor assertion
    # must unwind the very same actual native thread and original client owners.
    value.faults.append(control)
    value.diagnostic_private = PRIVATE
    calls = []

    def held_report(*args, **kwargs):
        calls.append(None)
        control.entered.set()
        try:
            assert control.release.wait(10), "Original native report gate was not released"
            return original_report(*args, **kwargs)
        finally:
            control.tail.set()

    monkeypatch.setattr(operations.owner, "report", held_report)
    public = physical_module.begin(value)
    try:
        await physical_module.gate_entered(control.entered)
        assert calls == [None] and not control.release.is_set() and not control.tail.is_set()
        # This fixture has one request in flight. Shared registries can contain
        # other standing families in production; no request arguments are read.
        assert len(registry) == 1
        native_task, cancellation = next(iter(registry.items()))
        assert type(native_task) is asyncio.Task and type(cancellation) is threading.Event
        assert not native_task.done() and not native_task.cancelled()
        assert native_task.cancelling() == 0 and not cancellation.is_set()
        original_calls = [entry for entry in value.original_calls if entry[0] == "finalize"]
        assert len(original_calls) == 1
        _, call, work = original_calls[0]
        scope = call.scope
        assert work in scope.handles and not work.done() and not scope.failed
        exchanges = [
            exchange
            for exchange in value.gateway._submit_exchanges
            if exchange.actual_owner is scope and not exchange.task.done()
        ]
        assert len(exchanges) == 1
        exchange = exchanges[0]
        assert exchange.response is None and exchange.close_task is None
        assert len(physical_module.reports(value)) == 1

        databases = list(dict.fromkeys([value.session.store.db, *value.connections]))
        sql_before = [tuple(database.iterdump()) for database in databases]
        before = cached(value, public, operations, shared, control, calls)
        tasks_before = {
            (id(task), task.done(), task.cancelled(), task.cancelling())
            for task in asyncio.all_tasks()
        }
        loop = asyncio.get_running_loop()
        ready_before = tuple(map(id, loop._ready))
        scheduled_before = tuple(map(id, loop._scheduled))
        attempted = []

        def forbidden(*args, **kwargs):
            attempted.append("forbidden-side-effect")
            raise AssertionError("Native snapshot attempted a side effect")

        for database in databases:
            database.set_trace_callback(lambda statement: attempted.append("sql"))
        try:
            with ExitStack() as guards:
                for target, name in (
                    (builtins, "open"),
                    (io, "open"),
                    (os, "open"),
                    (os, "read"),
                    (os, "stat"),
                    (os, "lstat"),
                    (os, "readlink"),
                    (socket, "socket"),
                    (asyncio, "create_task"),
                    (asyncio, "all_tasks"),
                    (loop, "create_task"),
                    (threading.Event, "set"),
                    (threading.Event, "clear"),
                    (threading.Event, "wait"),
                    (asyncio.Event, "set"),
                    (asyncio.Event, "clear"),
                    (asyncio.Event, "wait"),
                    (public, "cancel"),
                    (work, "cancel"),
                    (native_task, "cancel"),
                    (exchange.task, "cancel"),
                    (value.gateway.http, "send"),
                    (value.control.http, "send"),
                    (value.coordinator, "status"),
                    (value.coordinator.manager, "status"),
                    (value.coordinator.manager, "owned_identity"),
                    (scope, "hold"),
                    (scope, "release"),
                    (scope, "fail"),
                ):
                    guards.enter_context(patch.object(target, name, forbidden))
                observed = snapshot(value)
        finally:
            for database in databases:
                database.set_trace_callback(None)

        assert attempted == []
        assert cached(value, public, operations, shared, control, calls) == before
        assert {
            (id(task), task.done(), task.cancelled(), task.cancelling())
            for task in asyncio.all_tasks()
        } == tasks_before
        assert tuple(map(id, loop._ready)) == ready_before
        assert tuple(map(id, loop._scheduled)) == scheduled_before
        assert [tuple(database.iterdump()) for database in databases] == sql_before
        assert registry is operations._operations is shared._operations
        assert registry[native_task] is cancellation and not cancellation.is_set()
        assert not control.release.is_set() and not control.tail.is_set()
        assert not public.done() and not work.done() and not native_task.done()

        assert type(observed) is dict and set(observed) == KEYS
        assert type(observed["truncated"]) is bool
        encoded = json.dumps(observed, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        assert len(encoded.encode("utf-8")) <= 32768 and json.loads(encoded) == observed
        for node in nodes(observed):
            assert type(node) in {dict, list, str, int, bool, type(None)}
            if type(node) in {dict, list}:
                assert len(node) <= 32
        components = [
            item
            for item in observed["operations"]
            if type(item) is dict and item.get("kind") == "session-operations"
        ]
        assert len(components) == 1, (
            "Existing native Session actual-operation observation is absent"
        )
        component = components[0]
        assert component["id"] == id(operations) and component["available"] is True
        assert component["shared"] is (registry is shared._operations)
        assert component["closed"] is operations._closed
        assert type(component["pending_count"]) is int and component["pending_count"] == len(
            registry
        )
        assert type(component["tasks"]) is list and len(component["tasks"]) == len(registry)
        native = next(item for item in component["tasks"] if item["id"] == id(native_task))
        assert native["kind"] == "session-operation"
        assert native["cancellation_requested"] is cancellation.is_set() is False
        task = native["task"]
        assert task["id"] == id(native_task) and task["kind"] == "task"
        assert task["done"] is task["cancelled"] is False
        task_evidence([task], native_task)
        chain = task["await_chain"]
        assert 1 <= len(chain) <= 24
        assert all(type(frame["name"]) is str and 0 < len(frame["name"]) <= 64 for frame in chain)
        names = {frame["name"] for frame in chain}
        assert "StandingResourceFinalizationOperations._execute" in names
        assert "StandingOwnerOperations._thread_result" in names
        for private in (
            PRIVATE,
            physical_module.SECRET.hex(),
            "Bearer ",
            "a" * 32,
            "r" * 32,
            value.claim.claim_id,
            value.packet.task.task_id,
            value.request.expected_resource_backend,
            value.request.expected_resource_instance,
            str(value.path),
            str(physical_module.ROOT),
        ):
            assert private not in encoded
    finally:
        # Releasing an enrolled fixture gate is not a new/native cleanup owner.
        control.release.set()

    receipt = await asyncio.wait_for(public, 8)
    assert type(receipt) is StandingResourceFinalizationReceipt
    assert receipt.validate_request(value.request) == receipt
    assert receipt.finalization.context.request == value.request
    assert receipt.finalization.context.binding == value.binding.request
    assert receipt.finalization.context.closure.initial == value.initial
    assert receipt.finalization.context.observation.reservation is None
    assert receipt.finalization.resource_disposition == "no-reservation"
    assert receipt.finalization.restoration == "not-required"
    await asyncio.wait_for(scope.join(), 8)
    await asyncio.wait_for(asyncio.shield(native_task), 8)
    assert calls == [None] and control.tail.is_set()
    assert native_task.done() and not native_task.cancelled() and native_task.exception() is None
    assert not cancellation.is_set() and native_task not in registry
    assert work.done() and not work.cancelled() and work.exception() is None
    assert not scope.failed and not value.gateway._submit_cleanup_failed
    assert (
        exchange.task.done() and not exchange.task.cancelled() and exchange.task.exception() is None
    )
    close = exchange.close_task
    assert type(close) is asyncio.Task and close.done() and not close.cancelled()
    assert close.exception() is None
    body = value.native.bodies[-1]
    assert exchange.response.stream._stream is body
    assert isinstance(body, physical_module.Body) and body in value.native.bodies
    assert body.closes == 1 and body.closed and not body.cancelled and not body.failed
    native_receipt = await value.agent.standing_resource_finalization_lookup(value.request)
    assert native_receipt is not None and native_receipt.finalization == receipt.finalization
    assert tuple(value.starts) == starts_before == () and tuple(value.stops) == stops_before
    assert value.effect_checks == 0 and value.provider.calls == []
    # Existing physical/factory teardown positively joins actual clients,
    # ledgers, Manager and stream owners; no snapshot or empty registry does so.
