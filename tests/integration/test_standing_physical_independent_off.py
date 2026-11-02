"""Review-derived unfinished-lifecycle Off regressions, not blinded oracles."""

import asyncio
import json

import pytest
import test_standing_resource_barrier as existing
from test_standing_resource_barrier import (
    SECRET,
    NativeBody,
    NativeBoundary,
    action,
    bind,
    capture,
    seal_request,
)
from test_standing_resource_barrier import barriers as barriers
from test_standing_resource_barrier import factory as factory
from test_standing_resources_registration import join_client

from packages.contracts.operation_ownership import current_owner
from packages.contracts.standing_resource_finalization import (
    StandingResourceFinalizationReceipt,
    finalization_bytes,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.converse.intent import IntentRecord

WIRE_MAX = (1 << 53) - 1
REFUSALS = (StandingConflict, StandingUnavailable)


class PublicationBody(NativeBody):
    def __init__(self, inner, owner, *, held=False):
        super().__init__(inner, owner)
        self.held = held

    async def aclose(self):
        if not self.held:
            return await super().aclose()
        self.close_calls += 1
        self.original_close = asyncio.current_task()
        self.original_scope = current_owner()
        self.owner.publication_entered.set()
        while not self.owner.publication_release.is_set():
            try:
                await self.owner.publication_release.wait()
            except asyncio.CancelledError:
                self.owner.publication_cancel_observed = True
        await self.inner.aclose()
        self.closed = True
        if self.owner.fail_publication_close:
            raise OSError("Synthetic original publication close failed")


class SharedJournalBoundary(NativeBoundary):
    """Preserve native POSTs and add the launcher's genuine journal GET path."""

    def __init__(self, app):
        super().__init__(app)
        self.arm_ready_close = False
        self.publication_entered, self.publication_release = asyncio.Event(), asyncio.Event()
        self.publication_cancel_observed = False
        self.fail_publication_close = False

    async def handle_async_request(self, request):
        if request.method != "GET":
            return await super().handle_async_request(request)
        self.requests.append((request.url.path, None, dict(request.headers)))
        response = await self.delegate.handle_async_request(request)
        held = (
            self.arm_ready_close
            and request.url.path == "/v1/sessions"
            and self.coordinator.state.record.phase == "ready"
        )
        if held:
            self.arm_ready_close = False
        body = PublicationBody(response.stream, self, held=held)
        response.stream = body
        self.streams.append(body)
        return response


def actual_shared_journal(monkeypatch):
    original = existing.ResourceCoordinator

    def create(manager, state, converse, **kwargs):
        # Set actual composition at construction, as the launcher does. Never
        # replace a client/collaborator after native binding captures it.
        kwargs["journal"] = kwargs["handoff_journal"]
        return original(manager, state, converse, **kwargs)

    monkeypatch.setattr(existing, "ResourceCoordinator", create)
    monkeypatch.setattr(existing, "NativeBoundary", SharedJournalBoundary)


def seed_actual_revision(coordinator, revision):
    if revision == "policy":
        coordinator.state.record.revision = WIRE_MAX
        coordinator.state.save()
        assert coordinator.state.record.revision == WIRE_MAX + 1
        assert json.loads(coordinator.state.path.read_text())["revision"] == WIRE_MAX + 1
    elif revision == "intent":
        intent = coordinator.intent
        with intent._locked() as directory:
            intent._write(directory, IntentRecord(intent.read().enabled, WIRE_MAX + 1))
        assert intent.read().revision == WIRE_MAX + 1


def native_reports(value):
    return value.session.store.db.execute(
        "SELECT count(*) FROM standing_resource_finalizations"
    ).fetchone()[0]


async def join_originals(value, finalizer):
    producer = value.coordinator.standing_finalization
    observed = set()

    async def settle():
        await asyncio.gather(finalizer, return_exceptions=True)
        while True:
            tasks = set(producer.observers) | set(value.coordinator.standing_handoff.native)
            observed.update(tasks)
            if tasks:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in tasks), return_exceptions=True
                )
            scopes = tuple(value.coordinator.standing_barrier.operations)
            if scopes:
                await asyncio.gather(*(scope.join() for scope in scopes))
            await join_client(value.gateway)
            await asyncio.sleep(0)
            if not producer.observers and not value.coordinator.standing_barrier.operations:
                break

    await asyncio.wait_for(settle(), 30)
    assert all(task.done() for task in observed)
    return {
        "observed_task_failures": [
            "cancelled" if task.cancelled() else type(task.exception()).__name__
            for task in observed
            if task.cancelled() or task.exception() is not None
        ],
        "producer_failed": producer.failed,
        "barrier_uncertain": value.coordinator.standing_barrier.uncertain,
        "gateway_cleanup_failed": value.gateway._submit_cleanup_failed,
        "actual_registry_empty": not value.gateway._submit_exchanges,
        "finalizer_outcome": "cancelled"
        if finalizer.cancelled()
        else type(finalizer.exception()).__name__
        if finalizer.exception() is not None
        else type(finalizer.result()).__name__,
    }


def observe_physical_calls(monkeypatch, manager):
    calls = []
    for name in ("start", "stop", "stop_if_current"):
        original = getattr(manager, name)

        async def observed(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return await _original(*args, **kwargs)

        monkeypatch.setattr(manager, name, observed)
    return calls


async def reconcile_healthy_owner_off(value, request, finalizer, calls, after_off, record_property):
    first = None
    try:
        first = finalizer.result()
    except StandingUnavailable as error:
        assert type(error) is StandingUnavailable
        first_outcome = "StandingUnavailable"
    else:
        assert type(first) is StandingResourceFinalizationReceipt
        first.validate_request(request)
        first_outcome = type(first).__name__
    assert calls == after_off
    # Only the exact frozen request may reconcile the already completed Off.
    # A bounded first facade is not a promise of immediate positive testimony.
    receipt = await value.coordinator.finalize_standing_claim(request)
    assert type(receipt) is StandingResourceFinalizationReceipt
    assert receipt.validate_request(request) == receipt
    if first is not None:
        assert finalization_bytes(first.finalization) == finalization_bytes(receipt.finalization)
    assert calls == after_off
    with value.coordinator.standing_handoff.lock:
        local = value.coordinator.standing_handoff.db.execute(
            "SELECT report FROM standing_resource_witnesses WHERE claim_id=?",
            (value.claim.claim_id,),
        ).fetchone()[0]
    assert local.encode() == finalization_bytes(receipt.finalization)
    native = await value.gateway.standing_resource_finalization_lookup(request)
    assert type(native) is StandingResourceFinalizationReceipt
    assert native.validate_request(request) == native
    assert finalization_bytes(native.finalization) == local.encode()
    assert native_reports(value) == 1 and calls == after_off
    record_property(
        "healthy_exact_reconciliation",
        json.dumps(
            {
                "first_facade_exact_class": first_outcome,
                "first_unavailability_cause": "Unresolved; reconciliation is an uncertainty control",
                "request_key": request.idempotency_key,
                "physical_calls_after_first_off": after_off,
                "physical_calls_after_reconciliation": calls,
                "native_reports": native_reports(value),
                "immutable_report_digest": receipt.finalization.finalization_digest,
            },
            sort_keys=True,
        ),
    )
    return receipt


@pytest.mark.parametrize("revision", ["valid", "policy", "intent"])
@pytest.mark.parametrize("failed_close", [False, True], ids=["healthy-close", "failed-close"])
async def test_independent_off_keeps_originals_without_inheriting_wire_policy(
    barriers, monkeypatch, record_property, revision, failed_close
):
    actual_shared_journal(monkeypatch)
    value = await barriers()
    coordinator, manager, boundary = value.coordinator, value.coordinator.manager, value.boundary
    physical_calls = (
        observe_physical_calls(monkeypatch, manager)
        if revision == "valid" and not failed_close
        else None
    )
    assert coordinator.journal is coordinator.handoff_journal is value.gateway
    binding = await bind(value)
    await value.agent.standing_admit(value.packet, capability=SECRET)
    assert (await action(coordinator, "profile", profile="local_work_priority"))["ok"]
    assert (await action(coordinator, "converse", key="off-review-on", enabled=True))["ok"]
    assert (await capture(value, key="off-review-handoff"))["ok"]
    lease = coordinator.state.record.lease_id
    assert lease is not None
    await value.agent.standing_close_admission(value.packet, capability=SECRET)
    request = seal_request(value, binding, key="off-review-finalize")
    returned = asyncio.Event()
    original_restore = coordinator._restore_locked

    async def restore():
        try:
            return await original_restore()
        finally:
            returned.set()

    monkeypatch.setattr(coordinator, "_restore_locked", restore)
    boundary.arm_ready_close = True
    boundary.fail_publication_close = failed_close
    finalizer = asyncio.create_task(coordinator.finalize_standing_claim(request))
    value.tasks.append(finalizer)
    off_ok = stopped_before_release = False
    after_off = None
    before_reports = None
    body = None
    try:
        await asyncio.wait_for(boundary.publication_entered.wait(), 25)
        body = next(stream for stream in boundary.streams if getattr(stream, "held", False))
        identity = manager.owned_identity("whistle")
        assert identity is not None and manager.is_current(identity)
        original_scope, original_close = body.original_scope, body.original_close
        assert original_scope is not None and not original_close.done()
        exchange = next(
            exchange
            for exchange in value.gateway._submit_exchanges
            if exchange.close_task is original_close
        )
        assert exchange.actual_owner is original_scope
        seed_actual_revision(coordinator, revision)
        await asyncio.wait_for(returned.wait(), 8)
        if revision != "valid":
            with pytest.raises(REFUSALS):
                await asyncio.wait_for(asyncio.shield(finalizer), 8)
        try:
            result = await action(coordinator, "converse", key="off-review-off", enabled=False)
            off_ok = type(result) is dict and result.get("ok") is True
        except REFUSALS:
            pass
        if physical_calls is not None:
            after_off = list(physical_calls)
        stopped_before_release = not manager.is_current(identity)
        before_reports = native_reports(value)
        record_property(
            "off_before_original_close",
            json.dumps(
                {
                    "revision": revision,
                    "failed_close": failed_close,
                    "off_ack": off_ok,
                    "exact_original_worker_stopped": stopped_before_release,
                    "original_scope_finished": original_scope.finished,
                    "original_close_pending": not original_close.done(),
                    "native_reports": before_reports,
                },
                sort_keys=True,
            ),
        )
        assert not original_close.done() and body.close_calls == 1
        assert exchange in value.gateway._submit_exchanges
        assert not original_scope.finished
        assert before_reports == 0
        if off_ok:
            assert stopped_before_release
            proof = manager._workers["whistle"].stop_evidence
            assert proof["pid"] == identity.pid and proof["reaped"] and proof["group_empty"]
    finally:
        boundary.publication_release.set()
        settlement = await join_originals(value, finalizer)
        if body is not None:
            assert body.original_close.done() and body.close_calls == 1 and body.closed
            settlement["original_close_failed"] = body.original_close.exception() is not None
            settlement["original_scope_failed"] = body.original_scope.failed
        settlement["first_off_ack"] = off_ok
        settlement["worker_stopped_before_release"] = stopped_before_release
        settlement["reports_before_release"] = before_reports
        record_property("off_original_settlement", json.dumps(settlement, sort_keys=True))
    if failed_close:
        assert body.original_close.exception() is not None
        assert value.gateway._submit_cleanup_failed is True
        with pytest.raises(REFUSALS):
            await coordinator.finalize_standing_claim(request)
    else:
        assert body.original_close.exception() is None
    if revision != "valid":
        with pytest.raises(REFUSALS):
            await coordinator.finalize_standing_claim(request)
        assert native_reports(value) == 0
    elif not failed_close:
        receipt = await reconcile_healthy_owner_off(
            value, request, finalizer, physical_calls, after_off, record_property
        )
        assert receipt.finalization.reserved_lease_id == lease
        assert receipt.finalization.restoration == "owner-off"
        assert native_reports(value) == 1
    assert off_ok, "Independent owner Off inherited an earlier finalizer's wire requirement"
    assert stopped_before_release
