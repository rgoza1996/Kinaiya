"""Review-derived regressions for the sealed24f physical producer findings.

These are not blinded contract oracles. They retain real Session/Resources
SQLite, ASGI exchanges, disposable CPU workers and original response closers.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
import test_standing_resource_barrier as barrier_fixture
from test_standing_resource_barrier import (
    SECRET,
    NativeBody,
    action,
    bind,
    capture,
    seal_request,
)
from test_standing_resource_barrier import (
    barriers as barriers,
)
from test_standing_resource_barrier import (
    factory as factory,
)
from test_standing_resources_registration import join_client

from packages.contracts.standing_resource_finalization import finalization_bytes
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.converse.intent import IntentRecord

REPORT = "/v1/standing/resources/finalization/report"
WIRE_MAX = (1 << 53) - 1
REFUSALS = (StandingConflict, StandingUnavailable)


def report_count(value):
    return value.session.store.db.execute(
        "SELECT count(*) FROM standing_resource_finalizations"
    ).fetchone()[0]


async def acquired(value):
    binding = await bind(value)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    assert (await action(value.coordinator, "profile", profile="local_work_priority"))["ok"]
    assert (await action(value.coordinator, "converse", enabled=True))["ok"]
    assert (await capture(value))["ok"]
    lease = value.coordinator.state.record.lease_id
    assert lease is not None
    assert value.coordinator.manager.owned_identity("whistle") is None
    return binding, lease


async def closed_request(value, binding):
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    return seal_request(value, binding)


async def joined_operations(value):
    scopes = tuple(value.coordinator.standing_barrier.operations)
    if scopes:
        await asyncio.wait_for(asyncio.gather(*(scope.join() for scope in scopes)), 8)
    await asyncio.wait_for(join_client(value.gateway), 5)


async def test_genuine_restored_worker_reports_without_a_second_restore(barriers, monkeypatch):
    value = await barriers()
    binding, lease = await acquired(value)
    manager = value.coordinator.manager
    original_start, starts = manager.start, []

    async def start(worker_id):
        starts.append(worker_id)
        return await original_start(worker_id)

    monkeypatch.setattr(manager, "start", start)
    assert (await action(value.coordinator, "task_finished", task_id=value.packet.task.task_id))[
        "ok"
    ]
    restored = manager.owned_identity("whistle")
    assert restored is not None and manager.is_current(restored)
    request = await closed_request(value, binding)
    before = list(starts)
    receipt = await value.coordinator.finalize_standing_claim(request)
    assert receipt.finalization.resource_disposition == "matching-lease-finalized"
    assert receipt.finalization.reserved_lease_id == lease
    assert receipt.finalization.restoration == "restored"
    assert manager.is_current(restored) and starts == before
    assert report_count(value) == 1


@pytest.mark.parametrize("last_await", ["control", "publication"])
async def test_exited_original_restored_worker_cannot_report(
    barriers, monkeypatch, record_property, last_await
):
    value = await barriers()
    binding, _ = await acquired(value)
    coordinator, manager = value.coordinator, value.coordinator.manager
    lost = []

    async def stop_original():
        assert not lost
        identity = manager.owned_identity("whistle")
        assert identity is not None and manager.is_current(identity)
        result = await manager.stop_if_current(identity)
        assert result["ok"] and result["stop_evidence"]["reaped"]
        assert result["stop_evidence"]["group_empty"]
        assert not manager.is_current(identity)
        lost.append(identity)

    if last_await == "control":

        async def response_hook(response):
            if (
                not lost
                and response.request.url.path == "/v1/converse/enabled"
                and json.loads(response.request.content)["enabled"] is True
            ):
                await stop_original()

        coordinator.converse.http.event_hooks["response"].append(response_hook)
    else:
        original_publish = coordinator._publish

        async def publish():
            result = await original_publish()
            if not lost and coordinator.state.record.phase == "ready":
                await stop_original()
            return result

        monkeypatch.setattr(coordinator, "_publish", publish)

    result = await action(coordinator, "task_finished", task_id=value.packet.task.task_id)
    assert type(result) is dict and len(lost) == 1
    assert not manager.is_current(lost[0])
    request = await closed_request(value, binding)
    refused = False
    try:
        await coordinator.finalize_standing_claim(request)
    except REFUSALS:
        refused = True
    record_property(
        "actual_worker_loss",
        json.dumps(
            {
                "point": last_await,
                "original_pid": lost[0].pid,
                "original_not_current": not manager.is_current(lost[0]),
                "saved_phase": coordinator.state.record.phase,
                "native_reports": report_count(value),
                "finalizer_refused": refused,
            },
            sort_keys=True,
        ),
    )
    assert refused, "Exited original worker was certified from cached restoration state"
    assert report_count(value) == 0
    assert not any(path == REPORT for path, _, _ in value.boundary.requests)


def overflow_revision(value, which):
    coordinator = value.coordinator
    if which == "policy":
        # Seed the native monotonic counter, then use its actual validated,
        # fsynced save. The resulting record/file both contain the boundary.
        coordinator.state.record.revision = WIRE_MAX
        coordinator.state.save()
        assert coordinator.state.record.revision == WIRE_MAX + 1
        assert json.loads(coordinator.state.path.read_text())["revision"] == WIRE_MAX + 1
    else:
        # This is legal in the old durable owner grammar, but not report wire
        # Revision. Use the original writer/reader instead of a fake read hook.
        intent = coordinator.intent
        with intent._locked() as directory:
            intent._write(directory, IntentRecord(intent.read().enabled, WIRE_MAX + 1))
        assert coordinator.intent.read().revision == WIRE_MAX + 1


@pytest.mark.parametrize("which", ["policy", "intent"])
@pytest.mark.parametrize("stage", ["handoff", "finalizer"])
async def test_wire_revision_overflow_refuses_before_covered_effects(
    barriers, monkeypatch, record_property, which, stage
):
    value = await barriers()
    coordinator, manager = value.coordinator, value.coordinator.manager
    if stage == "finalizer":
        binding, _ = await acquired(value)
        request = await closed_request(value, binding)
    else:
        await bind(value)
        await value.agent.standing_admit(value.packet, capability=SECRET)
        assert (await action(coordinator, "profile", profile="local_work_priority"))["ok"]
        assert (await action(coordinator, "converse", enabled=True))["ok"]
        request = None
    effects = []
    for name in ("start", "stop", "stop_if_current"):
        original = getattr(manager, name)

        async def tracked(*args, _name=name, _original=original, **kwargs):
            effects.append(_name)
            return await _original(*args, **kwargs)

        monkeypatch.setattr(manager, name, tracked)

    async def control_request(request):
        if request.url.path == "/v1/converse/enabled":
            effects.append("converse-enabled")

    coordinator.converse.http.event_hooks["request"].append(control_request)
    original_action = coordinator._registered_action

    async def registered_action(payload, *, capture=None):
        try:
            return await original_action(payload, capture=capture)
        finally:
            if capture is not None and capture.effects_started:
                effects.append("capture-effects-started")

    monkeypatch.setattr(coordinator, "_registered_action", registered_action)
    overflow_revision(value, which)
    refused = False
    try:
        if stage == "handoff":
            result = await capture(value)
            refused = type(result) is dict and result.get("ok") is False
        else:
            await coordinator.finalize_standing_claim(request)
    except REFUSALS:
        refused = True
    observed_effects = list(effects)
    # This is independent owner authority, including with a revision that
    # prevents testimony. It must still perform genuine cleanup successfully.
    off = await action(coordinator, "converse", enabled=False)
    assert off["ok"] and coordinator.state.record.phase == "off"
    assert manager.owned_identity("whistle") is None
    record_property(
        "actual_revision_boundary",
        json.dumps(
            {
                "revision": which,
                "stage": stage,
                "refused": refused,
                "effects_before_off": observed_effects,
                "independent_off_ok": off["ok"],
                "native_reports": report_count(value),
            },
            sort_keys=True,
        ),
    )
    assert refused
    assert observed_effects == [], "Wire-invalid testimony started physical effects"
    assert report_count(value) == 0
    assert not any(path == REPORT for path, _, _ in value.boundary.requests)


class HeldReportBody(NativeBody):
    """The transport's original body/sole-close, with a declared close fault."""

    async def aclose(self):
        if self.owner.phase != "review-held-report-close":
            return await super().aclose()
        self.close_calls += 1
        self.actual_close = asyncio.current_task()
        self.owner.entered.set()
        while not self.owner.release.is_set():
            try:
                await self.owner.release.wait()
            except asyncio.CancelledError:
                self.owner.close_cancel_observed.set()
        await self.inner.aclose()
        self.closed = True  # Actual inner close completed; the Task may fail next.
        if self.owner.fail_report_close:
            raise OSError("Synthetic original report close failed after transport release")


@pytest.mark.parametrize("close_failure", [False, True], ids=["healthy", "failed"])
async def test_report_replay_waits_for_original_sole_close(
    barriers, monkeypatch, record_property, close_failure
):
    monkeypatch.setattr(barrier_fixture, "NativeBody", HeldReportBody)
    value = await barriers()
    binding = await bind(value)
    request = await closed_request(value, binding)
    boundary = value.boundary
    boundary.path, boundary.phase = REPORT, "review-held-report-close"
    boundary.close_cancel_observed = asyncio.Event()
    boundary.fail_report_close = close_failure
    reports = []
    original_report = value.gateway.standing_resource_report

    async def report(body):
        reports.append(SimpleNamespace(task=asyncio.current_task(), wire=finalization_bytes(body)))
        if len(reports) == 1:
            assert report_count(value) == 0
        assert reports[-1].wire == reports[0].wire
        with value.coordinator.standing_handoff.lock:
            local = value.coordinator.standing_handoff.db.execute(
                "SELECT report FROM standing_resource_witnesses WHERE claim_id=?",
                (value.claim.claim_id,),
            ).fetchone()[0]
        assert local.encode() == reports[-1].wire
        return await original_report(body)

    monkeypatch.setattr(value.gateway, "standing_resource_report", report)
    caller = asyncio.create_task(value.coordinator.finalize_standing_claim(request))
    value.tasks.append(caller)
    unexpected_positive = False
    try:
        await asyncio.wait_for(boundary.entered.wait(), 25)
        assert report_count(value) == 1 and len(reports) == 1
        body = next(stream for stream in boundary.streams if hasattr(stream, "actual_close"))
        original_close = body.actual_close
        exchange = next(
            exchange
            for exchange in value.gateway._submit_exchanges
            if exchange.close_task is original_close
        )
        original_scope = exchange.actual_owner
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        # Do not shorten or replace the existing25s RPC deadline. It retires
        # its original work while the genuine close resists cancellation.
        done, _ = await asyncio.wait({reports[0].task}, timeout=30)
        assert done and reports[0].task.done()
        await asyncio.gather(reports[0].task, return_exceptions=True)
        await asyncio.wait_for(boundary.close_cancel_observed.wait(), 2)
        assert exchange in value.gateway._submit_exchanges
        assert exchange.close_task is original_close and not original_close.done()
        assert original_scope in value.coordinator.standing_barrier.operations
        retry = asyncio.create_task(value.coordinator.finalize_standing_claim(request))
        value.tasks.append(retry)
        done, _ = await asyncio.wait({retry}, timeout=27)
        assert done, "The original bounded finalizer facade did not retire"
        try:
            unexpected_positive = await retry is not None
        except REFUSALS:
            pass
        assert not original_close.done() and body.close_calls == 1
    finally:
        boundary.release.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await asyncio.wait_for(join_client(value.gateway), 8)
        await joined_operations(value)
    assert original_close.done() and body.close_calls == 1 and body.closed
    if close_failure:
        assert original_close.exception() is not None
        assert value.gateway._submit_cleanup_failed is True
        with pytest.raises(REFUSALS):
            await value.coordinator.finalize_standing_claim(request)
    else:
        assert original_close.exception() is None
        retained = await value.coordinator.finalize_standing_claim(request)
        assert retained.disposition == "retained"
        assert finalization_bytes(retained.finalization) == reports[0].wire
    record_property(
        "actual_report_close",
        json.dumps(
            {
                "failed": close_failure,
                "sole_close_calls": body.close_calls,
                "original_close_done": original_close.done(),
                "native_reports": report_count(value),
                "report_sends": len(reports),
                "positive_while_original_close_pending": unexpected_positive,
                "sticky_close_failure": value.gateway._submit_cleanup_failed,
            },
            sort_keys=True,
        ),
    )
    assert report_count(value) == 1
    assert all(report.wire == reports[0].wire for report in reports)
    assert not unexpected_positive, (
        "Finalizer replay bypassed its original pending sole-close owner"
    )
