"""Source-aware capture-fence regressions, frozen before a capture repair.

These are additional chronology controls, not the original independent55
oracles. They use the same real Session/Resources SQLite/ASGI, native synthetic
Whistle process/protocol and original HTTP/COMMIT owners. No model, inference,
Agent retirement, settlement or claim release is qualified.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from test_resource_handoff import action
from test_standing_physical_finalization import (
    RESERVE,
    Gate,
    begin,
    bound,
    closure,
    completed,
    contains,
    gate_entered,
    handoff,
    ready,
    recorded_reservation,
    reports,
    witnesses,
)
from test_standing_physical_finalization import factory as factory
from test_standing_physical_finalization import physical as physical

from packages.contracts.standing_resource_finalization import StandingResourceFinalizeContext
from packages.contracts.standing_resources import StandingResourceReservationReceipt
from packages.contracts.standing_work import StandingUnavailable


class ObservedFence:
    """Observe the unchanged real fence after a complete native context ACK."""

    def __init__(self, value, monkeypatch):
        self.value = value
        self.entered = asyncio.Event()
        self.contexts = []
        self.requests = []
        self.original_context = value.gateway.standing_resource_finalize_context
        self.original_fence = value.coordinator.standing_barrier.fence

        async def context(request):
            result = await self.original_context(request)
            assert type(result) is StandingResourceFinalizeContext
            self.contexts.append(result.validate_request(request))
            return result

        def fence(task_id, request):
            self.original_fence(task_id, request)
            assert task_id == value.packet.task.task_id
            assert request.canonical_bytes() == value.request.canonical_bytes()
            assert self.contexts and self.contexts[-1].request == request
            assert self.contexts[-1].binding == value.binding.request
            assert self.contexts[-1].closure.initial == value.initial
            barrier = value.coordinator.standing_barrier
            assert barrier.sealed(task_id)
            assert barrier.seal_requests[task_id] == request.canonical_bytes().decode()
            self.requests.append(request.canonical_bytes())
            self.entered.set()

        monkeypatch.setattr(value.gateway, "standing_resource_finalize_context", context)
        monkeypatch.setattr(value.coordinator.standing_barrier, "fence", fence)


def original_capture(value):
    matches = [
        capture
        for capture in value.coordinator.standing_handoff.attempts.values()
        if capture.payload["task_id"] == value.packet.task.task_id
    ]
    assert len(matches) == 1
    capture = matches[0]
    assert type(capture.task) is asyncio.Task and capture.task in capture.ownership.handles
    assert capture.gateway is value.gateway and capture.request is not None
    return capture


def original_exchange(value, capture, gate):
    matches = [
        exchange
        for exchange in value.gateway._submit_exchanges
        if exchange.task is gate.exchange_task and exchange.actual_owner is capture.ownership
    ]
    assert len(matches) == 1
    bodies = [body for body in value.native.bodies if body.gate is gate]
    assert len(bodies) == 1
    return matches[0], bodies[0]


async def public_outcome(task):
    try:
        return await asyncio.wait_for(asyncio.shield(task), 30), None
    except StandingUnavailable as exc:
        return None, type(exc).__name__


async def join_capture(capture):
    await asyncio.wait_for(capture.ownership.join(), 30)
    assert capture.task.done() and not capture.task.cancelled()
    error = capture.task.exception()
    assert error is None or isinstance(error, StandingUnavailable)
    return None if error is None else type(error).__name__


async def join_finalizer_original(value, task, *, healthy):
    result, facade_error = await public_outcome(task)
    originals = [entry for entry in value.original_calls if entry[0] == "finalize"]
    assert len(originals) == 1
    _, call, work = originals[0]
    await asyncio.wait_for(call.scope.join(), 30)
    assert work.done() and not work.cancelled()
    error = work.exception()
    assert error is None or isinstance(error, StandingUnavailable)
    if healthy:
        assert not call.scope.failed
    return result, facade_error


def assert_healthy_exchange(exchange, body, request):
    assert exchange.task.done() and not exchange.task.cancelled()
    assert exchange.task.exception() is None
    receipt = exchange.task.result()
    assert type(receipt) is StandingResourceReservationReceipt
    assert receipt.validate_request(request).request == request
    close = exchange.close_task
    assert type(close) is asyncio.Task and close.done() and not close.cancelled()
    assert close.exception() is None
    assert body.closes == 1 and body.closed and not body.failed and not body.cancelled


def physical_calls(value):
    return (
        tuple(value.starts),
        tuple(value.stops),
        tuple(
            json.dumps(body, sort_keys=True)
            for path, body, _ in value.control_transport.requests
            if path == "/v1/converse/enabled"
        ),
    )


async def exact_report(value, first, restoration, reservation):
    first_result, first_error = await join_finalizer_original(value, first, healthy=True)
    # A first logical unavailable result may be reconciled only after those
    # exact originals have settled. Never mint another request/key or reserve.
    before = physical_calls(value)
    receipt = await completed(value, "registered-no-lease", restoration, reservation)
    if first_result is not None or restoration == "not-required":
        assert physical_calls(value) == before
    if first_result is not None:
        assert first_result.validate_request(value.request).finalization == receipt.finalization
    settled = physical_calls(value)
    assert all(body == receipt.finalization.model_dump(mode="json") for body in reports(value))
    assert len([path for path, _, _ in value.native.requests if path == RESERVE]) == 1
    assert len(value.reservation_receipts) == 1
    assert any(
        contains(row, receipt.finalization.model_dump(mode="json")) for _, row in witnesses(value)
    )
    replay = await completed(value, "registered-no-lease", restoration, reservation)
    assert replay.finalization == receipt.finalization and physical_calls(value) == settled
    assert (
        value.session.store.db.execute(
            "SELECT count(*) FROM standing_resource_finalizations"
        ).fetchone()[0]
        == 1
    )
    return first_error


@pytest.mark.parametrize("phase", ["acquire", "read", "close"])
async def test_late_original_reservation_ack_after_actual_fence_is_never_discarded(
    physical, monkeypatch, record_property, phase
):
    value = await physical()
    await bound(value)
    fence = ObservedFence(value, monkeypatch)
    gate = value.native.gate = Gate(RESERVE, phase)
    capture_facade = handoff(value)
    await gate_entered(gate.entered)
    capture = original_capture(value)
    exchange, body = original_exchange(value, capture, gate)
    assert not capture.effects_started and value.starts == []
    await closure(value)
    finalizer = begin(value)
    try:
        await gate_entered(fence.entered, 8)
        assert not exchange.task.done() and not capture.task.done()
        assert reports(value) == [] and value.coordinator.state.record.lease_id is None
    finally:
        gate.release.set()
    capture_result, capture_error = await public_outcome(capture_facade)
    original_error = await join_capture(capture)
    assert_healthy_exchange(exchange, body, capture.request)
    assert not capture.ownership.failed and not value.gateway._submit_cleanup_failed
    reservation = recorded_reservation(value)
    record_property(
        "fence_originals",
        json.dumps(
            {
                "phase": phase,
                "native_context_before_fence": True,
                "fence_before_release": True,
                "capture_facade_error": capture_error,
                "capture_original_error": original_error,
                "capture_result_ok": None if capture_result is None else capture_result.get("ok"),
                "original_exchange_and_close_healthy": True,
            }
        ),
    )
    await exact_report(value, finalizer, "not-required", reservation)
    assert value.starts == [] and not capture.effects_started


async def test_committed_physical_pending_thread_return_after_actual_fence_is_retained(
    physical, monkeypatch, record_property
):
    value = await physical()
    await bound(value)
    fence = ObservedFence(value, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    value.faults.append(SimpleNamespace(release=release))
    observation = {}
    actual_database = value.coordinator.standing_handoff._database

    async def database(invoke, capture=None):
        def committed_return():
            result = invoke()
            if capture is not None and not entered.is_set():
                # invoke has returned through the real transaction/COMMIT and
                # released its SQLite lock. Independently read the committed
                # full physical pending witness, not an unrelated SQL write.
                rows = witnesses(value)
                if capture.request is not None and any(
                    contains(row, capture.request.model_dump(mode="json"))
                    and contains(row, "pending")
                    and contains(row, capture.payload["idempotency_key"])
                    for _, row in rows
                ):
                    observation["capture"] = capture
                    observation["thread"] = threading.get_ident()
                    observation["committed_rows"] = rows
                    entered.set()
                    assert release.wait(25), "Original pending COMMIT return was not released"
            return result

        return await actual_database(committed_return, capture)

    monkeypatch.setattr(value.coordinator.standing_handoff, "_database", database)
    capture_facade = handoff(value)
    await gate_entered(entered, 8)
    capture = original_capture(value)
    assert observation["capture"] is capture
    assert observation["thread"] != threading.get_ident()
    native = tuple(value.coordinator.standing_handoff.native)
    assert len(native) == 1 and native[0] in capture.ownership.handles and not native[0].done()
    assert not capture.effects_started and reports(value) == []
    assert recorded_reservation(value) == capture.request
    await closure(value)
    finalizer = begin(value)
    try:
        await gate_entered(fence.entered, 8)
        assert not native[0].done() and not capture.task.done()
        assert reports(value) == []
    finally:
        release.set()
    _, capture_error = await public_outcome(capture_facade)
    original_error = await join_capture(capture)
    assert native[0].done() and not native[0].cancelled() and native[0].exception() is None
    assert not capture.ownership.failed and not value.gateway._submit_cleanup_failed
    record_property(
        "fence_originals",
        json.dumps(
            {
                "real_pending_commit_visible_before_fence": True,
                "native_thread_return_held_after_commit": True,
                "fence_before_thread_release": True,
                "original_native_thread_healthy": True,
                "capture_facade_error": capture_error,
                "capture_original_error": original_error,
            }
        ),
    )
    await exact_report(value, finalizer, "not-required", recorded_reservation(value))


async def test_trusted_postpreemption_fence_restores_before_registered_report(
    physical, monkeypatch, record_property
):
    value = await physical()
    await bound(value)
    process = await ready(value)
    original_identity = value.coordinator.manager.owned_identity("whistle")
    fence = ObservedFence(value, monkeypatch)
    await value.coordinator.lock.acquire()
    try:
        capture_facade = handoff(value)
        await gate_entered(value.effects_entered, 8)
        capture = original_capture(value)
        assert capture.effects_started and value.coordinator._whistle_preemptions == 1
        assert value.coordinator.manager.is_current(original_identity)
        assert value.coordinator.state.record.lease_id is None
        await closure(value)
        finalizer = begin(value)
        await gate_entered(fence.entered, 8)
        assert not capture.task.done() and reports(value) == []
    finally:
        value.coordinator.lock.release()
    _, capture_error = await public_outcome(capture_facade)
    original_error = await join_capture(capture)
    assert not capture.ownership.failed and not value.gateway._submit_cleanup_failed
    record_property(
        "fence_originals",
        json.dumps(
            {
                "real_effect_latch_before_fence": True,
                "fence_before_lifecycle_lock_release": True,
                "no_original_allocation": True,
                "capture_facade_error": capture_error,
                "capture_original_error": original_error,
                "original_process_exited": process.returncode is not None,
            }
        ),
    )
    await exact_report(value, finalizer, "restored", recorded_reservation(value))
    identity = value.coordinator.manager.owned_identity("whistle")
    assert identity is not None and value.coordinator.manager.is_current(identity)
    assert value.endpoint.ready is not None and value.endpoint.ready[0] == identity.pid
    assert value.coordinator.state.record.phase == "ready"


@pytest.mark.parametrize("ending", ["failed", "cancelled"])
async def test_actual_reservation_sole_close_failure_after_fence_stays_negative_through_off(
    physical, monkeypatch, record_property, ending
):
    value = await physical()
    await bound(value)
    fence = ObservedFence(value, monkeypatch)
    gate = value.native.gate = Gate(
        RESERVE, "fail-close" if ending == "failed" else "cooperative-close"
    )
    capture_facade = handoff(value)
    await gate_entered(gate.entered)
    capture = original_capture(value)
    exchange, body = original_exchange(value, capture, gate)
    close = gate.close_task
    assert type(close) is asyncio.Task and exchange.close_task is close and not close.done()
    await closure(value)
    finalizer = begin(value)
    try:
        await gate_entered(fence.entered, 8)
        assert reports(value) == [] and not close.done()
        if ending == "cancelled":
            close.cancel()
        else:
            gate.release.set()
    finally:
        gate.release.set()
    _, capture_error = await public_outcome(capture_facade)
    original_error = await join_capture(capture)
    assert exchange.close_task is close and close.done()
    assert close.cancelled() == (ending == "cancelled")
    assert close.cancelled() or close.exception() is not None
    assert body.closes == 1 and body.closed and (body.failed or body.cancelled)
    assert capture.ownership.failed and value.gateway._submit_cleanup_failed
    record_property(
        "fence_originals",
        json.dumps(
            {
                "fence_before_original_close_fault": True,
                "original_close_ending": ending,
                "capture_facade_error": capture_error,
                "capture_original_error": original_error,
                "scope_failed": capture.ownership.failed,
                "client_cleanup_failed": value.gateway._submit_cleanup_failed,
            }
        ),
    )
    with pytest.raises(StandingUnavailable):
        await asyncio.wait_for(asyncio.shield(finalizer), 30)
    await join_finalizer_original(value, finalizer, healthy=False)
    assert not value.gateway._submit_exchanges
    off = await action(
        value.coordinator, "converse", key="capture-fence:independent-off", enabled=False
    )
    assert off["ok"] and value.coordinator.state.record.phase == "off"
    assert value.coordinator.manager.owned_identity("whistle") is None
    assert value.gateway._submit_cleanup_failed and capture.ownership.failed
    with pytest.raises(StandingUnavailable):
        await asyncio.wait_for(begin(value), 30)
    assert reports(value) == []
    assert await value.agent.standing_resource_finalization_lookup(value.request) is None
    assert len([path for path, _, _ in value.native.requests if path == RESERVE]) == 1
