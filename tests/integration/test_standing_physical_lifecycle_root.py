"""Review-derived lifecycle root controls from sealed69be, not blinded oracles.

The independent55-case module remains byte exact. Its genuine SQLite/ASGI/CPU
fixture is reused as a self-contained CI dependency. This file targets only the
separately frozen initial-root ordering defect, before consulting its repair.
"""

import asyncio
import json
import threading

import pytest
from test_resource_handoff import action
from test_standing_physical_finalization import (
    REPORT,
    SqlFault,
    bound,
    gate_entered,
    handoff,
)
from test_standing_physical_finalization import factory as factory
from test_standing_physical_finalization import physical as physical

from packages.resources.processes import REAP_GRACE_S, SPAWN_SETTLE_S


class PendingWitnessCommit(SqlFault):
    """Hold the actual pending-init SQL COMMIT, not an await-only substitute."""

    def __init__(self, mode):
        super().__init__(mode, report_only=False)

    def matches(self, sql, parameters):
        return "".join(sql.lower().split()).startswith(
            "updatestanding_resource_witnessessetphase='restoring',restoration='pending'"
        )


async def pending_native_spawn(value, monkeypatch):
    """Manager itself creates/retains the original spawn and late reaper."""
    entered, release = asyncio.Event(), asyncio.Event()
    value.releases.append(release)
    actual_spawn = asyncio.create_subprocess_exec
    observed = {}

    async def spawn(*args, **kwargs):
        process = await actual_spawn(*args, **kwargs)
        observed.update(process=process, original_task=asyncio.current_task())
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    caller = asyncio.create_task(value.coordinator.manager.start("whistle"))
    value.tasks.append(caller)
    await gate_entered(entered)
    caller.cancel()
    # Keep the production five-second spawn-settlement bound unchanged. Once
    # it expires, Manager creates its actual original pending cleanup owner.
    await asyncio.wait_for(
        asyncio.gather(caller, return_exceptions=True), SPAWN_SETTLE_S + REAP_GRACE_S + 1
    )
    managed = value.coordinator.manager._workers["whistle"]
    original_spawn, original_reaper = managed.pending_spawn, managed.pending_cleanup
    assert original_spawn is observed["original_task"]
    assert isinstance(original_reaper, asyncio.Task)
    assert not original_spawn.done() and not original_reaper.done()
    assert managed.pending_fds and managed.pending_obj is not None
    assert managed.granted is False and managed.grant_w is None
    assert not (value.path / "ready").exists(), "Configured CPU fixture was never granted"
    return observed["process"], original_spawn, original_reaper, release


async def join_original_children(process, spawn, reaper, release):
    release.set()
    await asyncio.wait_for(asyncio.gather(spawn, reaper), REAP_GRACE_S + 1)
    assert spawn.result() is process and not spawn.cancelled()
    assert reaper.exception() is None and not reaper.cancelled()
    await asyncio.wait_for(process.wait(), REAP_GRACE_S + 1)
    assert process.returncode is not None


def off_ack_count(value):
    return sum(
        path == "/v1/converse/enabled" and body == {"enabled": False}
        for path, body, _ in value.control_transport.requests
    )


@pytest.mark.parametrize(
    "case", ["new", "reuse-failed-scope", "already-joined-control", "init-error", "init-cancel"]
)
async def test_original_lifecycle_root_covers_pending_init_and_original_manager_children(
    physical, monkeypatch, record_property, case
):
    value = await physical()
    await bound(value)
    refused = await handoff(value)
    assert refused["ok"] is False and value.effect_checks == 0
    producer = value.coordinator.standing_finalization
    key = value.claim.claim_id
    # The active claim comes only from genuine Session binding/admission and
    # the original registered handoff, never an assigned private key/flag.
    assert producer.active_key == key
    while producer.pending.get(key):
        await asyncio.wait_for(asyncio.gather(*tuple(producer.pending[key])), REAP_GRACE_S + 1)
    assert not producer.failed and producer.lifecycles == {}

    retained = []
    native_retain = value.coordinator.manager.retain_operations

    def retain(scope):
        if getattr(scope, "physical_lifecycle", False):
            retained.append((scope, frozenset(scope.handles)))
        return native_retain(scope)

    monkeypatch.setattr(value.coordinator.manager, "retain_operations", retain)
    process = original_spawn = original_reaper = release = None
    fault = operation = scope = first_scope = None
    outcome = None
    observations = {"case": case, "production_spawn_settle_seconds": SPAWN_SETTLE_S}
    try:
        process, original_spawn, original_reaper, release = await pending_native_spawn(
            value, monkeypatch
        )
        if case == "reuse-failed-scope":
            first = await action(
                value.coordinator,
                "converse",
                key="lifecycle-root:first-unsettled-off",
                enabled=False,
            )
            assert first["ok"] is False
            first_scope = producer.lifecycles[key].scope
            await asyncio.sleep(0)
            assert first_scope.failed and not first_scope.finished
            assert {original_spawn, original_reaper} <= first_scope.handles
            observations["first_real_off_failed_with_original_pending_children"] = True
        elif case == "already-joined-control":
            await join_original_children(process, original_spawn, original_reaper, release)

        fault = PendingWitnessCommit("before" if case == "init-error" else "held")
        value.faults.append(fault)
        for connection in value.connections:
            connection.fault = fault
        before_ack = off_ack_count(value)
        operation = asyncio.create_task(
            action(value.coordinator, "converse", key="lifecycle-root:observed-off", enabled=False)
        )
        value.tasks.append(operation)
        await gate_entered(fault.entered)
        assert fault.thread != threading.get_ident()
        scope = producer.lifecycles[key].scope
        if first_scope is not None:
            assert scope is first_scope, "Original unresolved failed lifecycle must stay retained"
        initial_other_handles = frozenset(scope.handles) - {original_spawn, original_reaper}
        observations.update(
            initial_non_manager_handles=len(initial_other_handles),
            original_spawn_in_scope=original_spawn in scope.handles,
            original_reaper_in_scope=original_reaper in scope.handles,
            original_manager_enrollment_roots=[
                len(handles) for owner, handles in retained if owner is scope
            ],
            pending_sql_native_thread=True,
        )
        if case == "init-cancel":
            operation.cancel()
        if case != "already-joined-control":
            assert {original_spawn, original_reaper} <= scope.handles
            await join_original_children(process, original_spawn, original_reaper, release)
        # Follow actual callbacks, not a fabricated finished flag. In the race
        # cases this is observed while the original native SQL COMMIT is held.
        await asyncio.sleep(0)
        observations["scope_finished_while_init_sql_held"] = scope.finished
        observations["remaining_handles_while_init_sql_held"] = len(scope.handles)
        assert value.coordinator.manager._workers["whistle"].pending_spawn is None
        assert value.coordinator.manager._workers["whistle"].pending_cleanup is None
        fault.release.set()
        outcome = (await asyncio.gather(operation, return_exceptions=True))[0]
        observations["original_off_outcome"] = (
            {"ok": outcome.get("ok"), "error": outcome.get("error")}
            if isinstance(outcome, dict)
            else {"exception": type(outcome).__name__}
        )
        observations["original_off_actual_acknowledgments"] = off_ack_count(value) - before_ack

        if case in {"new", "reuse-failed-scope"}:
            assert isinstance(outcome, dict) and outcome["ok"] is True, outcome
            assert initial_other_handles, "Original lifecycle root was absent during pending SQL"
            assert observations["scope_finished_while_init_sql_held"] is False
            if first_scope is None:
                assert retained and retained[0][1], "Root must precede original Manager enrollment"
            else:
                assert scope.failed, "Independent Off cannot erase the original cleanup failure"
        elif case == "already-joined-control":
            assert isinstance(outcome, dict) and outcome["ok"] is True, outcome
        else:
            # An init fault/cancel is not positive testimony. Once the exact
            # original children/SQL settle, a fresh independent Off still works.
            retried = await action(
                value.coordinator, "converse", key="lifecycle-root:independent-off", enabled=False
            )
            assert retried["ok"] is True, retried
            assert not any(path == REPORT for path, _, _ in value.native.requests)
        await asyncio.wait_for(scope.join(), REAP_GRACE_S + 1)
        assert scope.finished and not scope.handles
        assert value.coordinator.state.record.phase == "off"
        assert value.coordinator.intent.read().enabled is False
        assert value.runtime.enabled is False
        assert value.coordinator.manager.owned_identity("whistle") is None
    finally:
        if fault is not None:
            fault.release.set()
        if release is not None:
            release.set()
        if original_spawn is not None and original_reaper is not None:
            await asyncio.wait_for(
                asyncio.gather(original_spawn, original_reaper, return_exceptions=True),
                REAP_GRACE_S + 1,
            )
        if operation is not None:
            await asyncio.wait_for(
                asyncio.gather(operation, return_exceptions=True), REAP_GRACE_S + 1
            )
        if process is not None:
            await asyncio.wait_for(process.wait(), REAP_GRACE_S + 1)
            assert process.returncode is not None
            # Manager's original ungranted-bootstrap reaper is already joined;
            # retire this fixture's captured pipe transport after actual reap.
            process._transport.close()
        if scope is not None:
            observations.update(
                original_scope_final_finished=scope.finished,
                original_scope_final_handles=len(scope.handles),
                original_scope_failed=scope.failed,
            )
        managed = value.coordinator.manager._workers["whistle"]
        observations.update(
            bootstrap_reaped=process is not None and process.returncode is not None,
            original_spawn_done=original_spawn is not None and original_spawn.done(),
            original_reaper_done=original_reaper is not None and original_reaper.done(),
            pending_spawn_cleared=managed.pending_spawn is None,
            pending_reaper_cleared=managed.pending_cleanup is None,
            native_pending_fds_closed=managed.pending_fds == [],
            native_pending_config_closed=managed.pending_obj is None,
        )
        record_property("original_lifecycle_root", json.dumps(observations, sort_keys=True))
