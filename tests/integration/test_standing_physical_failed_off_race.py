"""Source-aware failed-Off/capture chronology regression, not a blinded oracle.

This additional control reuses the frozen real Session/Resources/Whistle fixture
and capture-fence observers. It holds the original native pending-COMMIT return,
then observes a genuine failed independent Off before releasing the fenced,
otherwise healthy original capture. No inference, Agent retirement, settlement
or claim release is qualified. No original test file or deadline is changed.
"""

import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

from test_resource_handoff import action
from test_standing_physical_capture_fences import (
    ObservedFence,
    assert_healthy_exchange,
    join_capture,
    join_finalizer_original,
    original_capture,
    original_exchange,
    public_outcome,
)
from test_standing_physical_finalization import (
    RESERVE,
    Gate,
    begin,
    bound,
    closure,
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

from packages.contracts.operation_ownership import current_owner
from packages.contracts.standing_work import StandingUnavailable


def committed_witness(value, capture, *, unreported=True):
    """Observe the actual unique full witness through an independent connection.

    Source-aware assumption: the discovered physical witness retains claim_id,
    binding, reservation, action_key, phase and restoration columns. No row is
    inserted, rewritten, adopted or supplied to production by this observer.
    """
    rows = witnesses(value)
    assert len(rows) == 1, "Cannot identify the unique genuine physical witness"
    table, _ = rows[0]
    quoted = table.replace('"', '""')
    with sqlite3.connect(value.path / "control/resource-actions.sqlite") as db:
        db.row_factory = sqlite3.Row
        records = list(
            db.execute(
                f'SELECT * FROM "{quoted}" WHERE claim_id=? LIMIT 2', (value.claim.claim_id,)
            )
        )
    assert len(records) == 1
    row = dict(records[0])
    assert row["claim_id"] == value.claim.claim_id
    assert json.loads(row["binding"]) == value.binding.request.model_dump(mode="json")
    assert json.loads(row["reservation"]) == capture.request.model_dump(mode="json")
    assert row["action_key"] == capture.payload["idempotency_key"]
    assert row["lease_id"] == capture.request.lease_id
    if unreported:
        assert row["report"] is None
    return row


async def joined_finalize_calls(value):
    """Join each original barrier root, including its dynamic native children."""
    originals = [entry for entry in value.original_calls if entry[0] == "finalize"]
    assert originals
    for _, call, work in originals:
        await asyncio.wait_for(call.scope.join(), 30)
        assert work.done() and not work.cancelled()
        error = work.exception()
        assert error is None or isinstance(error, StandingUnavailable)
    return originals


async def test_failed_independent_off_stays_negative_after_healthy_fenced_capture_settles(
    physical, monkeypatch, record_property
):
    value = await physical()
    await bound(value)
    process = await ready(value)
    identity = value.coordinator.manager.owned_identity("whistle")
    assert identity is not None and value.coordinator.manager.is_current(identity)
    fence = ObservedFence(value, monkeypatch)
    physical_owner = value.coordinator.standing_finalization
    entered, release = threading.Event(), threading.Event()
    value.faults.append(SimpleNamespace(release=release))
    observed = {"off_window": False}
    actual_database = value.coordinator.standing_handoff._database
    actual_off = value.coordinator._off_unwitnessed_locked
    actual_enabled = value.control._enabled
    actual_lifecycle_settled = physical_owner._lifecycle_settled
    off_snapshots = []
    actual_save = value.coordinator.state.save

    async def database(invoke, capture=None):
        def committed_return():
            result = invoke()
            if capture is not None and not entered.is_set() and capture.request is not None:
                rows = witnesses(value)
                if any(
                    contains(row, capture.request.model_dump(mode="json"))
                    and contains(row, "pending")
                    and contains(row, capture.payload["idempotency_key"])
                    for _, row in rows
                ):
                    # Original invoke has actually committed and released its
                    # SQLite lock. Only that same native thread's return waits.
                    observed["capture"] = capture
                    observed["pending"] = committed_witness(value, capture)
                    observed["thread"] = threading.get_ident()
                    entered.set()
                    assert release.wait(25), "Original pending COMMIT return was not released"
            return result

        return await actual_database(committed_return, capture)

    async def off():
        if observed["off_window"]:
            observed["off_scope"] = current_owner()
            observed["off_work"] = asyncio.current_task()
            assert observed["off_work"] in observed["off_scope"].handles
        return await actual_off()

    async def enabled(enabled):
        if observed["off_window"] and enabled is False:
            observed["control_scope"] = current_owner()
            observed["control_task"] = asyncio.current_task()
            assert observed["control_task"] in observed["control_scope"].handles
        return await actual_enabled(enabled)

    async def lifecycle_settled(life):
        if life.key == value.claim.claim_id and "life" not in observed:
            observed["life"] = life
            observed["off_observer"] = asyncio.current_task()
        return await actual_lifecycle_settled(life)

    def save(**changes):
        record = actual_save(**changes)
        if observed["off_window"] and changes.get("phase") == "off":
            off_snapshots.append((record, json.loads(value.coordinator.state.path.read_bytes())))
        return record

    monkeypatch.setattr(value.coordinator.standing_handoff, "_database", database)
    monkeypatch.setattr(value.coordinator, "_off_unwitnessed_locked", off)
    monkeypatch.setattr(value.control, "_enabled", enabled)
    monkeypatch.setattr(physical_owner, "_lifecycle_settled", lifecycle_settled)
    monkeypatch.setattr(value.coordinator.state, "save", save)
    reservation_close = value.native.gate = Gate(RESERVE, "close")
    capture_facade = handoff(value)
    try:
        await gate_entered(reservation_close.entered, 8)
        capture = original_capture(value)
        exchange, body = original_exchange(value, capture, reservation_close)
        assert exchange.close_task is reservation_close.close_task
        assert not exchange.task.done() and not exchange.close_task.done()
        reservation_close.release.set()
        await gate_entered(entered, 8)
        assert original_capture(value) is capture
        capture_observer = capture.witness_observer
        assert observed["capture"] is capture and observed["thread"] != threading.get_ident()
        assert observed["pending"]["phase"] == "pending"
        assert not capture.effects_started and not capture.ownership.failed
        assert recorded_reservation(value) == capture.request and reports(value) == []
        native = tuple(value.coordinator.standing_handoff.native)
        assert len(native) == 1 and native[0] in capture.ownership.handles
        assert not native[0].done() and not capture_observer.done()
        assert_healthy_exchange(exchange, body, capture.request)
        assert current_owner() is None

        await closure(value)
        finalizer = begin(value)
        await gate_entered(fence.entered, 8)
        assert not native[0].done() and not capture_observer.done()
        assert committed_witness(value, capture)["phase"] == "pending"
        assert reports(value) == []

        value.control_transport.bad_off_ack = True
        observed["off_window"] = True
        control_start = len(value.control_transport.bodies)
        off_result = await action(
            value.coordinator, "converse", key="failed-off-race:original", enabled=False
        )
        observed["off_window"] = False
        value.control_transport.bad_off_ack = False
        life, off_observer = observed["life"], observed["off_observer"]
        assert life.observer is off_observer
        await asyncio.wait_for(asyncio.shield(off_observer), 30)
        await asyncio.wait_for(life.scope.join(), 30)
        assert life.scope is observed["off_scope"] is observed["control_scope"]
        assert life.scope is not capture.ownership and life.scope.failed
        assert off_observer.done() and not off_observer.cancelled()
        assert off_observer.exception() is None
        for task in (observed["off_work"], observed["control_task"]):
            assert (
                task.done() and not task.cancelled() and isinstance(task.exception(), RuntimeError)
            )
        off_bodies = value.control_transport.bodies[control_start:]
        assert len(off_bodies) == 1
        assert off_bodies[0].closes == 1 and off_bodies[0].closed
        assert not off_bodies[0].failed and not off_bodies[0].cancelled
        assert off_result["ok"] is False and len(off_snapshots) == 1
        committed_off, persisted_off = off_snapshots[0]
        assert committed_off.phase == "off" and committed_off.lease_id is None
        assert persisted_off == committed_off.model_dump(mode="json")
        assert value.coordinator.state.record.phase == "failed"
        assert value.coordinator.state.record.lease_id is None
        assert process.returncode is not None
        evidence = value.coordinator.manager._workers["whistle"].stop_evidence
        assert evidence["reaped"] and evidence["group_empty"]
        assert value.coordinator.manager.owned_identity("whistle") is None
        assert not capture.ownership.failed and not value.gateway._submit_cleanup_failed
        failed_before_release = committed_witness(value, capture)
        assert failed_before_release["phase"] == "failed"
        assert failed_before_release["restoration"] == "uncertain"
        assert not native[0].done() and not capture.task.done()
        assert fence.entered.is_set() and not capture_observer.done()
        assert committed_witness(value, capture) == failed_before_release
        assert reports(value) == []
    finally:
        observed["off_window"] = False
        value.control_transport.bad_off_ack = False
        reservation_close.release.set()
        release.set()

    _, facade_error = await public_outcome(capture_facade)
    capture_error = await join_capture(capture)
    await asyncio.wait_for(asyncio.shield(capture_observer), 30)
    assert capture_observer.done() and not capture_observer.cancelled()
    assert capture_observer.exception() is None
    assert native[0].done() and not native[0].cancelled() and native[0].exception() is None
    assert_healthy_exchange(exchange, body, capture.request)
    assert not value.gateway._submit_cleanup_failed and not capture.effects_started

    # Record all public outcomes before asserting negativity so the original
    # cold failure also retains actual root joins, retry and later-Off facts.
    first_result, first_error = await join_finalizer_original(value, finalizer, healthy=False)
    after_capture = committed_witness(value, capture, unreported=False)
    retry_result, retry_error = await public_outcome(begin(value))
    await joined_finalize_calls(value)
    later_off = await action(
        value.coordinator, "converse", key="failed-off-race:later-healthy", enabled=False
    )
    assert later_off["ok"] is True and value.coordinator.state.record.phase == "off"
    later_result, later_error = await public_outcome(begin(value))
    originals = await joined_finalize_calls(value)
    native_lookup = await value.agent.standing_resource_finalization_lookup(value.request)
    row_after_later_off = committed_witness(value, capture, unreported=False)
    record_property(
        "failed_off_capture_originals",
        json.dumps(
            {
                "genuine_pending_commit_before_independent_off": True,
                "original_pending_native_thread_joined_healthy": True,
                "independent_off_work_and_control_failed": True,
                "original_off_response_sole_close_healthy": True,
                "independent_off_observer_joined_healthy": True,
                "independent_off_scope_failed": life.scope.failed,
                "original_worker_reaped": True,
                "failed_witness_before_capture_release": failed_before_release,
                "native_context_and_fence_before_capture_release": True,
                "capture_facade_error": facade_error,
                "capture_original_error": capture_error,
                "capture_scope_failed": capture.ownership.failed,
                "capture_witness_complete": capture.witness_complete,
                "original_reservation_exchange_and_sole_close_joined_healthy": True,
                "witness_after_capture": after_capture,
                "witness_after_later_off": row_after_later_off,
                "finalizer_originals_joined": len(originals),
                "first_error": first_error,
                "retry_error": retry_error,
                "later_error": later_error,
                "native_report_count": len(reports(value)),
                "native_history_present": native_lookup is not None,
            },
            sort_keys=True,
        ),
    )
    assert life.scope.failed
    assert len(originals) == 3
    assert first_result is retry_result is later_result is None
    assert first_error == retry_error == later_error == "StandingUnavailable"
    assert reports(value) == [] and native_lookup is None
    for row in (after_capture, row_after_later_off):
        assert row["phase"] == "failed" and row["restoration"] == "uncertain"
        assert row["report"] is None
    assert len([path for path, _, _ in value.native.requests if path == RESERVE]) == 1
    assert len(value.reservation_receipts) == 1 and value.effect_checks == 0
