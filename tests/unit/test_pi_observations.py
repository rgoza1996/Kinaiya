"""Protected storage and replay across managed Pi generations."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from packages.agent_adapters.pi_sdk import PiEvent, PiEventReplayExpired, PiReply
from packages.contracts.external_agent import (
    ExternalAgentClaimReceipt,
    ExternalAgentReplay,
    ExternalAgentSource,
    ExternalAgentTarget,
)
from services.decisions import pi_observations
from services.decisions.pi_observations import PiObservations, PiObservationUnavailable

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
