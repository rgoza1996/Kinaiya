"""Native one-owner consumption, provenance, revocation and uncertain outcomes."""

from types import SimpleNamespace

import pytest

from packages.contracts.live_decisions_attempts import LiveDecisionsSource
from packages.contracts.live_effects import (
    PROTOCOL,
    LiveEffectAdmit,
    LiveEffectEvidence,
    LiveEffectFinish,
    LiveEffectQuery,
    LiveEffectStart,
)
from packages.contracts.models import AppendRequest
from packages.contracts.stt_settlement import digest
from services.session import live_effects
from services.session.live_effects import LiveEffectConflict
from services.session.live_windows import publish
from services.session.store import Store
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import fence
from tests.integration.test_live_decisions_attempts import (
    admission,
    finish,
    output,
    publication,
)
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_window_publication import original


def outcome(live, conversation="WAIT_WITH_BACKCHANNEL", route="IGNORE_FOR_IMMEDIATE_ROUTING"):
    identity = digest(live.source.model_dump(mode="json"))[:32]
    attempt = live.owner.admit(admission(live, attempt_id=identity, owner_id=identity))
    completed = live.owner.finish(finish(live, attempt, output(live, conversation, route)))
    proof = publication(live, completed)
    live.owner.publish_result(proof)
    return proof


@pytest.fixture
def effects(live):
    proof = outcome(live)
    query = LiveEffectQuery(
        schema_version="2.0", protocol=PROTOCOL, publication=proof, effect="converse_backchannel"
    )
    request = LiveEffectAdmit(
        **query.model_dump(mode="json"), owner_instance="c" * 32, operation_id="d" * 32
    )
    return SimpleNamespace(
        **vars(live), query=query, request=request, effects=live.store.live_effects
    )


def completion(receipt, **change):
    return LiveEffectFinish.model_validate(
        dict(
            schema_version="2.0",
            protocol=PROTOCOL,
            original=receipt.model_dump(mode="json"),
            status="declined",
            evidence=None,
            reason_code="public_converse_choice",
        )
        | change,
        strict=True,
    )


def start_record(e, record, *, owner=None, operation=None):
    original = record.receipt.original
    return e.store.live_effects.start(
        LiveEffectStart(
            schema_version="2.0",
            protocol=PROTOCOL,
            original=record.receipt,
            owner_instance=owner or original.owner_instance,
            operation_id=operation or original.operation_id,
        ),
        "decisions" if original.effect == "agent_route" else "converse",
    )


def test_lookup_is_passive_and_one_original_owner_survives_duplicate_and_replacement(effects):
    e = effects
    wake = e.store.journal_wake.snapshot()
    assert not e.effects._present()
    state = e.effects.lookup(e.query)
    assert state.record is None and state.source_current
    assert not e.effects._present() and e.store.journal_wake.snapshot() == wake
    claimed = e.effects.admit(e.request, "converse")
    admitted_wake = e.store.journal_wake.snapshot()
    assert claimed.receipt.original == e.request and claimed.completion is None
    assert e.effects.admit(e.request, "converse") == claimed
    assert e.effects.lookup(e.query).record == claimed
    for field in ("owner_instance", "operation_id"):
        changed = e.request.model_copy(update={field: "e" * 32})
        with pytest.raises(LiveEffectConflict):
            e.effects.admit(changed, "converse")
    assert e.store.db.execute("SELECT count(*) FROM live_effects").fetchone()[0] == 1
    assert e.store.journal_wake.snapshot() == admitted_wake


@pytest.mark.parametrize("role", ["owner", "agent", "resources", "decisions", ""])
def test_wrong_roles_cannot_allocate_native_consumption(effects, role):
    with pytest.raises(LiveEffectConflict):
        effects.effects.admit(effects.request, role)
    assert not effects.effects._present()


@pytest.mark.parametrize("effect", ["converse_reply", "agent_route"])
def test_advisory_axes_cannot_grant_an_unselected_effect(effects, effect):
    request = effects.request.model_copy(update={"effect": effect})
    with pytest.raises(LiveEffectConflict):
        effects.effects.admit(request, "decisions" if effect == "agent_route" else "converse")
    assert not effects.effects._present()


def test_source_revocation_refuses_new_claim_but_preserves_original_history(effects):
    e = effects
    claimed = e.effects.admit(e.request, "converse")
    e.store.live_captures.fence(fence(e.source.capture.original))
    state = e.effects.lookup(e.query)
    assert not state.source_current and state.record == claimed
    assert e.effects.admit(e.request, "converse") == claimed
    declined = completion(claimed.receipt)
    result = e.effects.finish(declined, "converse")
    assert result.completion == declined
    assert e.effects.finish(declined, "converse") == result
    with pytest.raises(LiveEffectConflict):
        e.effects.finish(completion(claimed.receipt, status="refused"), "converse")
    with pytest.raises(LiveEffectConflict):
        e.effects.admit(e.request.model_copy(update={"operation_id": "e" * 32}), "converse")


def test_foreign_transaction_is_not_rolled_back_or_committed(effects):
    e = effects
    e.store.db.execute("CREATE TABLE public_consumer_owner(value TEXT)")
    e.store.db.commit()
    e.store.db.execute("BEGIN")
    e.store.db.execute("INSERT INTO public_consumer_owner VALUES('public still-owned write')")
    try:
        with pytest.raises(LiveEffectConflict):
            e.effects.admit(e.request, "converse")
        assert e.store.db.in_transaction
        assert (
            e.store.db.execute("SELECT value FROM public_consumer_owner").fetchone()[0]
            == "public still-owned write"
        )
    finally:
        e.store.db.rollback()


def test_uncertain_effect_keeps_its_capacity_and_never_funds_replacement(effects, monkeypatch):
    e = effects
    monkeypatch.setattr(live_effects, "MAX_ACTIVE", 1)
    record = e.effects.admit(e.request, "converse")
    start_record(e, record)
    e.effects.finish(
        completion(record.receipt, status="uncertain", reason_code="public_ack_unknown"), "converse"
    )
    pair = publish(e.store, original(e, 1, text="Public next fresh thought"))
    next_live = SimpleNamespace(
        **(vars(e) | {"source": LiveDecisionsSource.of(pair, e.source.capture), "pair": pair})
    )
    proof = outcome(next_live)
    request = e.request.model_copy(update={"publication": proof, "operation_id": "e" * 32})
    with pytest.raises(LiveEffectConflict):
        e.effects.admit(request, "converse")


def test_a_full_reply_cannot_be_reported_as_the_native_backchannel_evidence(effects):
    e = effects
    record = e.effects.admit(e.request, "converse")
    start_record(e, record)
    source = e.source.original
    entry = e.store.append(
        AppendRequest(
            session_id=source.session_id,
            thread_id=source.thread_id,
            epoch=source.epoch,
            kind="conversation.converse",
            causes=[source.source_event_id],
            payload={
                "text": "Public synthetic full reply",
                "final": True,
                "window_behavior": "RESPOND_INTERRUPT_USER",
                "window_source_id": source.source_event_id,
                "window_utterance_id": e.source.publication.utterance_id,
            },
            idempotency_key="live-effect:" + record.receipt.receipt_id + ":0",
        ),
        "converse",
    )
    evidence = LiveEffectEvidence(event_id=entry.event_id, sequence=entry.sequence)
    with pytest.raises(LiveEffectConflict):
        e.effects.finish(
            completion(record.receipt, status="recorded", evidence=evidence.model_dump()),
            "converse",
        )


def reply_evidence(e, receipt, *, key=None):
    source = e.source.original
    return e.store.append(
        AppendRequest(
            session_id=source.session_id,
            thread_id=source.thread_id,
            epoch=source.epoch,
            kind="conversation.converse",
            causes=[source.source_event_id],
            payload={
                "text": "Public synthetic acknowledgment",
                "final": True,
                "window_behavior": "WAIT_WITH_BACKCHANNEL",
                "window_source_id": source.source_event_id,
                "window_utterance_id": e.source.publication.utterance_id,
            },
            idempotency_key=key or "live-effect:" + receipt.receipt_id + ":0",
        ),
        "converse",
    )


def test_positive_late_native_evidence_resolves_uncertain_without_another_admission(effects):
    e = effects
    record = e.effects.admit(e.request, "converse")
    start_record(e, record)
    uncertain = completion(record.receipt, status="uncertain", reason_code="public_ack_unknown")
    e.effects.finish(uncertain, "converse")
    entry = reply_evidence(e, record.receipt)
    known = completion(
        record.receipt,
        status="recorded",
        evidence={"event_id": entry.event_id, "sequence": entry.sequence},
    )
    result = e.effects.finish(known, "converse")
    assert result.completion == known and result.receipt == record.receipt
    assert e.effects.finish(known, "converse") == result
    assert e.store.db.execute("SELECT count(*) FROM live_effects").fetchone()[0] == 1
    assert len(e.store.read(kinds=["conversation.converse"])) == 1
    with pytest.raises(LiveEffectConflict):
        e.effects.finish(uncertain, "converse")
    with pytest.raises(LiveEffectConflict):
        e.effects.admit(e.request.model_copy(update={"operation_id": "e" * 32}), "converse")


def test_another_operation_cannot_supply_the_recorded_effect_evidence(effects):
    e = effects
    record = e.effects.admit(e.request, "converse")
    start_record(e, record)
    entry = reply_evidence(e, record.receipt, key="public-another-native-operation")
    known = completion(
        record.receipt,
        status="recorded",
        evidence={"event_id": entry.event_id, "sequence": entry.sequence},
    )
    with pytest.raises(LiveEffectConflict):
        e.effects.finish(known, "converse")
    assert e.effects.lookup(e.query).record.completion is None


def test_reserved_work_can_change_executor_once_but_started_work_cannot(effects):
    e = effects
    record = e.effects.admit(e.request, "converse")
    assert record.started is None
    started = start_record(e, record, owner="e" * 32, operation="f" * 32)
    assert started.receipt == record.receipt and started.started.original.owner_instance == "e" * 32
    assert start_record(e, record, owner="e" * 32, operation="f" * 32) == started
    with pytest.raises(LiveEffectConflict):
        start_record(e, record)
    assert e.effects.lookup(e.query).record == started


def test_backchannel_cannot_start_after_capture_permission_is_revoked(effects):
    e = effects
    record = e.effects.admit(e.request, "converse")
    e.store.live_captures.fence(fence(e.source.capture.original))
    with pytest.raises(ValueError):
        start_record(e, record, owner="e" * 32)
    assert e.effects.lookup(e.query).record.started is None


def test_queued_agent_route_survives_mic_end_but_requires_unchanged_user_authority(live):
    proof = outcome(live, "IGNORE", "QUEUE_UNTIL_TASK_DONE")
    request = LiveEffectAdmit(
        schema_version="2.0",
        protocol=PROTOCOL,
        publication=proof,
        effect="agent_route",
        owner_instance="c" * 32,
        operation_id="d" * 32,
    )
    record = live.store.live_effects.admit(request, "decisions")
    live.store.live_captures.fence(fence(live.source.capture.original))
    started = start_record(live, record, owner="e" * 32, operation="f" * 32)
    assert started.started is not None and started.receipt == record.receipt
    assert not live.store.live_effects.lookup(
        LiveEffectQuery(**request.model_dump(exclude={"owner_instance", "operation_id"}))
    ).source_current


@pytest.mark.parametrize(
    "route", ["QUEUE_UNTIL_TASK_DONE", "STEER_CURRENT_TASK", "INTERRUPT_CURRENT_TASK"]
)
def test_published_agent_route_can_be_reserved_after_microphone_end(live, route):
    proof = outcome(live, "IGNORE", route)
    live.store.live_captures.fence(fence(live.source.capture.original))
    request = LiveEffectAdmit(
        schema_version="2.0",
        protocol=PROTOCOL,
        publication=proof,
        effect="agent_route",
        owner_instance="c" * 32,
        operation_id="d" * 32,
    )
    record = live.store.live_effects.admit(request, "decisions")
    assert record.receipt.original == request
    assert start_record(live, record, owner="e" * 32).started is not None


def test_changed_queued_instruction_cannot_start_under_its_old_reservation(live):
    proof = outcome(live, "IGNORE", "QUEUE_UNTIL_TASK_DONE")
    request = LiveEffectAdmit(
        schema_version="2.0",
        protocol=PROTOCOL,
        publication=proof,
        effect="agent_route",
        owner_instance="c" * 32,
        operation_id="d" * 32,
    )
    record = live.store.live_effects.admit(request, "decisions")
    publish(live.store, original(live, 1, text="Public corrected instruction"))
    with pytest.raises(LiveEffectConflict):
        start_record(live, record, owner="e" * 32)


@pytest.mark.parametrize("later_route", ["INTERRUPT_CURRENT_TASK", "IGNORE_FOR_IMMEDIATE_ROUTING"])
def test_later_native_interrupt_fences_a_reserved_queue_from_another_utterance(live, later_route):
    proof = outcome(live, "IGNORE", "QUEUE_UNTIL_TASK_DONE")
    request = LiveEffectAdmit(
        schema_version="2.0",
        protocol=PROTOCOL,
        publication=proof,
        effect="agent_route",
        owner_instance="c" * 32,
        operation_id="d" * 32,
    )
    record = live.store.live_effects.admit(request, "decisions")
    pair = publish(
        live.store,
        original(live, 1, utterance_id="public-later-interrupt", text="Public stop instruction"),
    )
    later = SimpleNamespace(
        **(
            vars(live)
            | {
                "pair": pair,
                "source": LiveDecisionsSource.of(pair, live.source.capture),
            }
        )
    )
    outcome(later, "IGNORE", later_route)
    if later_route == "INTERRUPT_CURRENT_TASK":
        with pytest.raises(LiveEffectConflict):
            start_record(live, record, owner="e" * 32)
        query = LiveEffectQuery(**request.model_dump(exclude={"owner_instance", "operation_id"}))
        assert live.store.live_effects.lookup(query).record.started is None
    else:
        assert start_record(live, record, owner="e" * 32).started is not None


def test_delayed_older_interrupt_outcome_cannot_fence_a_newer_queued_instruction(live):
    attempt = live.owner.admit(admission(live))
    completed = live.owner.finish(
        finish(live, attempt, output(live, "IGNORE", "INTERRUPT_CURRENT_TASK"))
    )
    delayed = publication(live, completed)
    pair = publish(
        live.store,
        original(live, 1, utterance_id="public-newer-queue", text="Public newer instruction"),
    )
    newer = SimpleNamespace(
        **(
            vars(live)
            | {
                "pair": pair,
                "source": LiveDecisionsSource.of(pair, live.source.capture),
            }
        )
    )
    proof = outcome(newer, "IGNORE", "QUEUE_UNTIL_TASK_DONE")
    request = LiveEffectAdmit(
        schema_version="2.0",
        protocol=PROTOCOL,
        publication=proof,
        effect="agent_route",
        owner_instance="c" * 32,
        operation_id="d" * 32,
    )
    record = live.store.live_effects.admit(request, "decisions")
    live.owner.publish_result(delayed)
    assert start_record(newer, record, owner="e" * 32).started is not None


@pytest.mark.parametrize("state", ["reserved", "started", "uncertain"])
def test_native_restart_preserves_queued_reservation_and_never_regrants_started_work(live, state):
    proof = outcome(live, "IGNORE", "QUEUE_UNTIL_TASK_DONE")
    request = LiveEffectAdmit(
        schema_version="2.0",
        protocol=PROTOCOL,
        publication=proof,
        effect="agent_route",
        owner_instance="c" * 32,
        operation_id="d" * 32,
    )
    record = live.store.live_effects.admit(request, "decisions")
    if state != "reserved":
        record = start_record(live, record)
    if state == "uncertain":
        record = live.store.live_effects.finish(
            completion(record.receipt, status="uncertain"), "decisions"
        )
    path = live.store.db.execute("PRAGMA database_list").fetchone()[2]
    live.store.close()
    recovered = Store(path)
    try:
        assert recovered.live_captures.activate()
        query = LiveEffectQuery(**request.model_dump(exclude={"owner_instance", "operation_id"}))
        observed = recovered.live_effects.lookup(query)
        assert observed.record == record and not observed.source_current
        owner = SimpleNamespace(store=recovered)
        if state == "reserved":
            assert start_record(owner, record, owner="e" * 32).started is not None
        else:
            with pytest.raises(LiveEffectConflict):
                start_record(owner, record, owner="e" * 32)
            assert recovered.live_effects.lookup(query).record == record
    finally:
        recovered.close()


def test_unknown_started_handoff_cannot_make_a_later_queue_assume_agent_is_idle(live):
    proof = outcome(live, "IGNORE", "QUEUE_UNTIL_TASK_DONE")
    first = LiveEffectAdmit(
        schema_version="2.0",
        protocol=PROTOCOL,
        publication=proof,
        effect="agent_route",
        owner_instance="c" * 32,
        operation_id="d" * 32,
    )
    start_record(live, live.store.live_effects.admit(first, "decisions"))
    pair = publish(live.store, original(live, 1, utterance_id="public-second-queue"))
    later = SimpleNamespace(
        **(
            vars(live)
            | {
                "pair": pair,
                "source": LiveDecisionsSource.of(pair, live.source.capture),
            }
        )
    )
    second = first.model_copy(
        update={
            "publication": outcome(later, "IGNORE", "QUEUE_UNTIL_TASK_DONE"),
            "operation_id": "e" * 32,
        }
    )
    reservation = live.store.live_effects.admit(second, "decisions")
    with pytest.raises(LiveEffectConflict):
        start_record(later, reservation, owner="f" * 32)


def test_no_recorded_or_uncertain_effect_can_be_claimed_before_native_start(effects):
    e = effects
    record = e.effects.admit(e.request, "converse")
    entry = reply_evidence(e, record.receipt)
    for status, evidence in [
        ("uncertain", None),
        ("recorded", {"event_id": entry.event_id, "sequence": entry.sequence}),
    ]:
        with pytest.raises(LiveEffectConflict):
            e.effects.finish(
                completion(record.receipt, status=status, evidence=evidence), "converse"
            )
