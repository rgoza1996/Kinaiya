"""One held original native child; cached diagnostics are not COMMIT proof."""

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
from test_standing_native_timeout_observation import cached
from test_standing_physical_finalization import factory as factory
from test_standing_physical_finalization import physical as physical
from test_standing_physical_timeout_observation import KEYS, nodes, task_evidence

from packages.contracts.standing_resource_finalization import StandingResourceFinalizationReceipt
from services.session.standing_resource_finalization import StandingResourceFinalizationOwner

PRIVATE = "PUBLIC-SYNTHETIC-NATIVE-CHILD-PRIVATE-9862e0ba"
MARKERS = (
    "call_wrapper_entered",
    "call_result_observed",
    "call_error_observed",
    "owner_report_wrapper_entered",
    "owner_report_result_observed",
    "owner_report_error_observed",
)


def bounded_task(summary, original):
    assert summary["id"] == id(original) and summary["kind"] == "task"
    assert summary["done"] is summary["cancelled"] is False
    task_evidence([summary], original)
    chain = summary["await_chain"]
    assert type(chain) is list and 1 <= len(chain) <= 24
    assert all(
        type(frame) is dict
        and type(frame["name"]) is str
        and 0 < len(frame["name"]) <= 64
        and (
            (type(frame["line"]) is int and frame["line"] >= 0)
            or (index == len(chain) - 1 and frame["name"] == "awaitable" and frame["line"] is None)
        )
        for index, frame in enumerate(chain)
    )
    return chain


async def test_cached_child_and_callback_markers_preserve_original_report_and_cleanup(
    physical, monkeypatch
):
    snapshot = getattr(physical_module, "_physical_timeout_snapshot", None)
    assert callable(snapshot)
    actual_report = StandingResourceFinalizationOwner.report
    control = SimpleNamespace(
        entered=threading.Event(), release=threading.Event(), tail=threading.Event()
    )
    calls, callbacks, selected = [], [], []

    def held_report(owner, finalization, *, deadline=None, cancelled=None):
        assert selected and owner is selected[0]
        calls.append(None)
        callbacks.append(cancelled)
        control.entered.set()
        try:
            assert control.release.wait(10), "Original native report gate was not released"
            return actual_report(owner, finalization, deadline=deadline, cancelled=cancelled)
        finally:
            control.tail.set()

    # Install before construction: the fixture's instance recorder delegates
    # into this genuine bound method, reaching both entry boundaries first.
    monkeypatch.setattr(StandingResourceFinalizationOwner, "report", held_report)
    value = await physical()
    value.faults.append(control)
    value.diagnostic_private = PRIVATE
    operations = value.session.app.state.standing_resource_finalization_operations
    shared = value.session.app.state.standing_owner_operations
    selected.append(operations.owner)
    await physical_module.bound(value)
    await physical_module.closure(value)
    starts_before, stops_before = tuple(value.starts), tuple(value.stops)
    registry = operations._operations
    assert type(registry) is dict and registry is shared._operations
    public = physical_module.begin(value)
    scope = work = native_parent = child = exchange = cancellation = None
    try:
        await physical_module.gate_entered(control.entered)
        assert calls == [None] and not control.release.is_set() and not control.tail.is_set()
        assert len(registry) == 1
        native_parent, cancellation = next(iter(registry.items()))
        assert type(native_parent) is asyncio.Task and type(cancellation) is threading.Event
        assert not native_parent.done() and not native_parent.cancelled()
        assert native_parent.cancelling() == 0 and not cancellation.is_set()
        assert callbacks[0].__self__ is cancellation
        assert callbacks[0].__func__ is threading.Event.is_set
        # This single-flight fixture has exactly one held to_thread child. The
        # inventory is outside observation; no frame locals/closures are read.
        children = [
            task
            for task in asyncio.all_tasks()
            if not task.done()
            and getattr(task.get_coro(), "cr_code", None) is asyncio.to_thread.__code__
        ]
        assert len(children) == 1
        (child,) = children
        assert child is not native_parent and not child.cancelled() and child.cancelling() == 0
        original_calls = [entry for entry in value.original_calls if entry[0] == "finalize"]
        assert len(original_calls) == 1
        _, call, work = original_calls[0]
        scope = call.scope
        assert work in scope.handles and not work.done() and not scope.failed
        exchanges = [
            item
            for item in value.gateway._submit_exchanges
            if item.actual_owner is scope and not item.task.done()
        ]
        assert len(exchanges) == 1
        (exchange,) = exchanges
        assert exchange.response is None and exchange.close_task is None
        assert len(physical_module.reports(value)) == 1
        databases = list(dict.fromkeys([value.session.store.db, *value.connections]))
        loop = asyncio.get_running_loop()

        def observe():
            before = cached(value, public, operations, shared, control, calls)
            sql_before = [tuple(database.iterdump()) for database in databases]
            tasks_before = {
                (id(task), task.done(), task.cancelled(), task.cancelling())
                for task in asyncio.all_tasks()
            }
            ready_before = tuple(map(id, loop._ready))
            scheduled_before = tuple(map(id, loop._scheduled))
            observations = getattr(value, "native_thread_observations", None)
            entries_before = (
                tuple((id(event), id(record)) for event, record in observations.items())
                if type(observations) is dict
                else None
            )
            attempted = []

            def forbidden(*args, **kwargs):
                attempted.append("forbidden-side-effect")
                raise AssertionError("Native child snapshot attempted a side effect")

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
                        (asyncio, "to_thread"),
                        (asyncio, "sleep"),
                        (loop, "create_task"),
                        (loop, "call_at"),
                        (loop, "call_later"),
                        (loop, "call_soon"),
                        (loop, "call_soon_threadsafe"),
                        (loop, "run_in_executor"),
                        (threading, "Thread"),
                        (threading, "Timer"),
                        (threading.Event, "__init__"),
                        (threading.Event, "set"),
                        (threading.Event, "clear"),
                        (threading.Event, "wait"),
                        (asyncio.Event, "__init__"),
                        (asyncio.Event, "set"),
                        (asyncio.Event, "clear"),
                        (asyncio.Event, "wait"),
                        (public, "cancel"),
                        (work, "cancel"),
                        (native_parent, "cancel"),
                        (child, "cancel"),
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
            assert getattr(value, "native_thread_observations", None) is observations
            assert entries_before == (
                tuple((id(event), id(record)) for event, record in observations.items())
                if type(observations) is dict
                else None
            )
            assert registry is operations._operations is shared._operations
            assert registry[native_parent] is cancellation and not cancellation.is_set()
            assert not control.release.is_set() and not control.tail.is_set()
            assert not public.done() and not work.done() and not native_parent.done()
            assert not child.done() and not child.cancelled() and child.cancelling() == 0
            assert type(observed) is dict and set(observed) == KEYS
            assert type(observed["truncated"]) is bool
            encoded = json.dumps(
                observed, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            )
            assert len(encoded.encode("utf-8")) <= 32768 and json.loads(encoded) == observed
            for node in nodes(observed):
                assert type(node) in {dict, list, str, int, bool, type(None)}
                if type(node) in {dict, list}:
                    assert len(node) <= 32
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
            components = [
                item
                for item in observed["operations"]
                if type(item) is dict and item.get("kind") == "session-operations"
            ]
            assert len(components) == 1
            component = components[0]
            assert component["id"] == id(operations) and component["available"] is True
            assert component["shared"] is True and component["closed"] is operations._closed
            assert component["pending_count"] == len(registry) == 1
            native = next(item for item in component["tasks"] if item["id"] == id(native_parent))
            assert native["cancellation_requested"] is cancellation.is_set() is False
            bounded_task(native["task"], native_parent)
            return native

        native = observe()
        assert native.get("native_child_observed") is True, (
            "Original native child observation is absent"
        )
        summary = native["native_child"]
        chain = bounded_task(summary, child)
        assert any(frame["name"] == "to_thread" for frame in chain)
        assert tuple(native[name] for name in MARKERS) == (True, False, False, True, False, False)

        observations = value.native_thread_observations
        assert type(observations) is dict and len(observations) <= 64
        # A missing diagnostic association is unavailable, not a claim that
        # native work is queued. The actual registry/child/Event stay intact.
        retained = observations.pop(cancellation)
        try:
            missing = observe()
            assert missing["native_child_observed"] is False
            assert missing["native_child"]["kind"] == "unavailable"
            assert missing["native_child"]["id"] is None
            assert tuple(missing[name] for name in MARKERS) == (None,) * 6
        finally:
            observations[cancellation] = retained
        assert observations[cancellation] is retained
        restored = observe()
        assert restored["native_child"] == summary
        assert tuple(restored[name] for name in MARKERS) == tuple(native[name] for name in MARKERS)
    finally:
        # Cold descriptor absence must still release and positively join these
        # same original owners; an empty registry is not the cleanup evidence.
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
        assert scope is not None and native_parent is not None and child is not None
        await asyncio.wait_for(scope.join(), 8)
        await asyncio.wait_for(asyncio.shield(native_parent), 8)
        await asyncio.wait_for(asyncio.shield(child), 8)
        assert calls == [None] and control.tail.is_set()
        assert native_parent.done() and not native_parent.cancelled()
        assert native_parent.exception() is None and not cancellation.is_set()
        assert native_parent not in registry
        assert child.done() and not child.cancelled() and child.exception() is None
        assert work.done() and not work.cancelled() and work.exception() is None
        assert not scope.failed and not value.gateway._submit_cleanup_failed
        assert exchange.task.done() and not exchange.task.cancelled()
        assert exchange.task.exception() is None
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
        await physical_module.close_resources(value)
        assert value.cleanup_error is None
        assert value.gateway.http.is_closed and value.control.http.is_closed
        assert value.native.close_calls == value.control_transport.close_calls == 1
        assert value.coordinator._close_owner.done()
        assert value.coordinator._close_owner.exception() is None
        assert value.coordinator.standing_handoff.db is None
        assert not value.gateway._submit_exchanges and not value.gateway._submit_cleanup_failed
        assert not value.endpoint.stream_closes.pending and value.provider.calls == []
