"""Protected storage and replay across managed Pi generations."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from packages.agent_adapters.pi_sdk import PiEvent, PiEventReplayExpired, PiReply
from packages.contracts.external_agent import (
    ExternalAgentClaim,
    ExternalAgentClaimReceipt,
    ExternalAgentReplay,
    ExternalAgentSource,
    ExternalAgentSubmit,
    ExternalAgentTarget,
)
from services.decisions import pi_observations
from services.decisions.pi_observations import (
    PiObservations,
    PiObservationUnavailable,
    PiQueueConflict,
)

TARGET = ExternalAgentTarget(attachment_id="a" * 32, backend_session_id="session")
TASK = "b" * 32


def replay(after=0):
    return ExternalAgentReplay(
        contract_version=1,
        request_id="replay",
        operation="events",
        target=TARGET,
        after=after,
        limit=8,
    )


def original_claim():
    return ExternalAgentClaimReceipt(
        contract_version=1,
        request_id="claim",
        kind="claim",
        target=TARGET,
        decision_event_id="decision",
        idempotency_key="dispatch",
        command_digest="c" * 64,
        admission_id=TASK,
        disposition="original",
    )


@pytest.fixture
def original_store(tmp_path):
    (tmp_path / "external-agent").mkdir(mode=0o700)
    original = PiObservations(tmp_path, TARGET)
    original.start()
    original.prepare(original_claim(), ExternalAgentSource(event_ids=("source",), revision=1))
    original.record_reply(TASK, PiReply("accepted", None))
    return original


def test_replacement_seals_unknown_tail_and_preserves_exact_reply(original_store, tmp_path):
    original_store.record_event(PiEvent(1, TASK, "running"))
    original_store.record_event(PiEvent(2, TASK, "assistant_message", text="Exact partial output"))

    replacement = PiObservations(tmp_path, TARGET)
    replacement.start()
    page = replacement.events(replay())
    assert [event.cursor for event in page.events] == [1, 2, 3]
    assert page.events[1].text == "Exact partial output"
    assert page.events[2].kind == "task_state" and page.events[2].state == "uncertain"
    assert replacement.original_reply(original_claim()) == PiReply("accepted", None)
    with pytest.raises(PiObservationUnavailable):
        original_store.record_event(PiEvent(3, TASK, "settled"))
    assert replacement.events(replay(3)).events == ()


@pytest.mark.parametrize("stop_first", [False, True])
def test_interrupt_acknowledgment_retains_the_observed_stop(original_store, tmp_path, stop_first):
    original_store.record_event(PiEvent(1, TASK, "running"))
    control = original_claim().model_copy(
        update={"admission_id": "e" * 32, "command_digest": "d" * 64}
    )
    assert original_store.prepare_interrupt(control, TASK) is None
    assert original_store.interrupt_reply(control, TASK) is None
    assert original_store.task(TASK, live=True).state == "running"

    terminal = PiEvent(2, TASK, "interrupted")
    if stop_first:
        original_store.record_event(terminal)
    ack = PiReply("requested", None)
    original_store.record_interrupt_reply(control.admission_id, ack)
    if not stop_first:
        assert original_store.task(TASK, live=True).state == "interrupt_requested"
        original_store.record_event(terminal)

    replacement = PiObservations(tmp_path, TARGET)
    replacement.start()
    assert replacement.interrupt_reply(control, TASK) == ack
    assert replacement.task(TASK, live=False).state == "interrupted"
    events = [event.state for event in replacement.events(replay()).events]
    expected = (
        ["running", "interrupted"]
        if stop_first
        else ["running", "interrupt_requested", "interrupted"]
    )
    assert events == expected
    with pytest.raises(PiObservationUnavailable):
        replacement.interrupt_reply(control, "f" * 32)


def test_replay_eviction_expires_only_the_lost_prefix(original_store, monkeypatch):
    monkeypatch.setattr(pi_observations, "_MAX_EVENTS", 2)
    original_store.record_event(PiEvent(1, TASK, "running"))
    original_store.record_event(PiEvent(2, TASK, "assistant_message", text="Retained output"))
    original_store.record_event(PiEvent(3, TASK, "settled"))
    original_store.stop()

    with pytest.raises(PiEventReplayExpired):
        original_store.events(replay())
    page = original_store.events(replay(1))
    assert [event.cursor for event in page.events] == [2, 3]
    assert page.next_after == 3 and not page.has_more
    retired, tasks = original_store.status(live=False)
    assert retired and tasks[0].state == "settled"


def test_storage_refuses_aliased_or_wrong_backend_file(tmp_path):
    registry = tmp_path / "external-agent"
    registry.mkdir(mode=0o700)
    original = PiObservations(tmp_path, TARGET)
    sentinel = tmp_path / "other.sqlite"
    sentinel.write_bytes(b"other file")
    sentinel.chmod(0o600)
    alias = registry / original.filename
    alias.symlink_to(sentinel)
    with pytest.raises(PiObservationUnavailable):
        original.start()
    assert sentinel.read_bytes() == b"other file"
    alias.unlink()
    original.start()
    other = TARGET.model_copy(update={"backend_session_id": "foreign-backend"})
    with pytest.raises(PiObservationUnavailable):
        PiObservations(tmp_path, other).status(live=False)


def test_refused_receipts_obey_the_retention_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_observations, "_MAX_RECEIPTS", 1)
    (tmp_path / "external-agent").mkdir(mode=0o700)
    observations = PiObservations(tmp_path, TARGET)
    observations.start()
    claim = original_claim()
    source = ExternalAgentSource(event_ids=("source",), revision=1)
    observations.prepare(claim, source)
    observations.record_reply(TASK, PiReply("refused", "busy"))
    second = claim.model_copy(update={"admission_id": "d" * 32})
    observations.prepare(second, source)
    assert observations.original_reply(claim) is None
    assert observations.status(live=True)[1][0].task_id == second.admission_id


def test_concurrent_replay_observes_committed_prefix(original_store, tmp_path):
    readers = 5
    writes = 160
    start = Barrier(readers + 1, timeout=5)

    def produce():
        start.wait()
        for cursor in range(1, writes + 1):
            original_store.record_event(
                PiEvent(cursor, TASK, "assistant_message", text=str(cursor))
            )

    def observe():
        reader = PiObservations(tmp_path, TARGET)
        start.wait()
        for _ in range(writes):
            page = reader.events(replay())
            assert [event.cursor for event in page.events] == list(range(1, page.next_after + 1))
            assert [event.text for event in page.events] == [
                str(event.cursor) for event in page.events
            ]

    with ThreadPoolExecutor(max_workers=readers + 1) as executor:
        jobs = [executor.submit(produce), *(executor.submit(observe) for _ in range(readers))]
        for job in jobs:
            job.result(timeout=15)
    assert original_store.events(replay(writes - 1)).events[0].text == str(writes)


def test_pending_capacity_and_exact_queued_retry(original_store, monkeypatch):
    monkeypatch.setattr(pi_observations, "_MAX_QUEUED", 1)

    def candidate(key):
        command = ExternalAgentSubmit(
            contract_version=1,
            request_id="submit",
            operation="submit",
            target=TARGET,
            idempotency_key=key,
            source=ExternalAgentSource(event_ids=(key,), revision=1),
            instruction="Queued synthetic action",
            content_scope="public_synthetic",
            execution_policy="backend_owned",
            queue_if_busy=True,
        )
        request = ExternalAgentClaim(
            contract_version=1,
            request_id="queue",
            operation="claim",
            decision_event_id=key,
            command=command,
        )
        digest = request.command_digest()
        eligibility = original_claim().model_copy(
            update={
                "decision_event_id": key,
                "idempotency_key": key,
                "command_digest": digest,
                "admission_id": digest[:32],
                "disposition": "eligible",
            }
        )
        return request, eligibility

    first, proof = candidate("first")
    task = original_store.enqueue(first, proof)
    assert task.state == "queued"
    replayed = first.model_copy(update={"request_id": "retry"})
    assert original_store.queued_task(replayed) == task
    changed = first.model_copy(
        update={"command": first.command.model_copy(update={"instruction": "Changed action"})}
    )
    with pytest.raises(PiQueueConflict):
        original_store.queued_task(changed)
    assert original_store.next_queued() == first
    assert original_store.enqueue(*candidate("second")) is None
    original_store.retire_queued(first, "not_applied")
    assert original_store.status(live=True)[1][-1].state == "not_applied"
    assert original_store.enqueue(*candidate("second")).state == "queued"
