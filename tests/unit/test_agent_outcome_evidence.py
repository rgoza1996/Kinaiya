"""Public synthetic bounded prefix commitments; no tools or provider calls."""

import asyncio

import pytest

from packages.contracts import agent_outcome as contract
from packages.contracts.agent_outcome import (
    OutcomeEvidenceIndex,
    OutcomeEvidenceUnavailable,
    bounded_outcome_request,
    read_outcome_task,
    restore_outcome_task,
)
from packages.contracts.models import AppendRequest, Evidence, JournalEntry, Task, TaskState


def fixture(count=20, *, state=TaskState.FAILED):
    task = Task(
        task_id="public-task",
        session_id="public-session",
        thread_id="public-thread",
        epoch=1,
        objective="Inspect public synthetic receipts",
        idempotency_key="public-key",
        content_scope="public_synthetic",
        state=state,
        objective_revision=2,
    )
    submit = JournalEntry(
        session_id=task.session_id,
        thread_id=task.thread_id,
        epoch=1,
        kind="agent.command",
        origin="agent-supervisor",
        sequence=1,
        task_refs=[task.task_id],
        idempotency_key="public-submit",
        payload={
            "task_id": task.task_id,
            "op": "submit",
            "idempotency_key": task.idempotency_key,
            "content_scope": task.content_scope,
        },
    )
    entries, index = [submit], OutcomeEvidenceIndex(task, 1)
    for number in range(count):
        evidence = Evidence(
            kind="command",
            evidence_id=f"public-evidence-{number}",
            exit_code=0,
            summary="Synthetic observed command",
            output="public synthetic output " * 125,
            objective_revision=1 + number % 2,
        )
        request = AppendRequest(
            session_id=task.session_id,
            thread_id=task.thread_id,
            epoch=1,
            kind="agent.tool",
            task_refs=[task.task_id],
            idempotency_key=f"public-tool-{number}",
            payload={
                "task_id": task.task_id,
                "content_scope": "public_synthetic",
                "evidence": evidence.model_dump(mode="json"),
            },
        )
        entry = JournalEntry(**request.model_dump(), origin="agent-supervisor", sequence=number + 2)
        index.observe_ack(request, entry)
        entries.append(entry)
        task.evidence.append(evidence)
    request = AppendRequest(
        session_id=task.session_id,
        thread_id=task.thread_id,
        epoch=1,
        kind="agent.outcome",
        task_refs=[task.task_id],
        idempotency_key="public-outcome",
        payload={
            "task_id": task.task_id,
            "objective_revision": task.objective_revision,
            "state": state.value,
            "content_scope": task.content_scope,
            "task": task.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in task.evidence],
        },
    )
    return task, index, request, entries


def outcome(request, entries):
    return JournalEntry(
        **request.model_dump(), origin="agent-supervisor", sequence=len(entries) + 1
    )


class Journal:
    def __init__(self, entries):
        self.entries, self.reads = entries, []

    async def read(self, **query):
        self.reads.append(query)
        return [
            e for e in self.entries if e.sequence > query["after"] and e.kind in query["kinds"]
        ][: query["limit"]]


@pytest.mark.parametrize("state", [TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED])
async def test_sparse_terminal_restores_every_original_receipt_without_output_duplication(state):
    task, index, request, entries = fixture(state=state)
    assert len(request.model_dump_json().encode()) > contract.MAX_OUTCOME_BYTES
    sparse = bounded_outcome_request(request, task, index)
    assert len(sparse.model_dump_json().encode()) < contract.MAX_OUTCOME_BYTES
    assert sparse.payload["task"]["evidence"] == sparse.payload["evidence"] == []
    assert "public synthetic output" not in sparse.model_dump_json()
    assert request.payload["task"]["evidence"]  # Original body/Task were not mutated.
    restored = await read_outcome_task(Journal(entries), outcome(sparse, entries))
    assert restored == task
    assert [e.objective_revision for e in restored.evidence] == [1, 2] * 10


def test_small_inline_shape_and_empty_evidence_remain_unchanged():
    for count in (0, 1):
        task, index, request, _ = fixture(count)
        assert bounded_outcome_request(request, task, index) is request


def test_standing_caller_defers_oversized_sparse_format_without_widening_origin():
    task, index, request, _ = fixture()
    with pytest.raises(OutcomeEvidenceUnavailable):
        bounded_outcome_request(request, task, index, allow_sparse=False)
    task, index, request, _ = fixture(1)
    assert bounded_outcome_request(request, task, index, allow_sparse=False) is request


@pytest.mark.parametrize("damage", ["missing", "changed", "reordered", "future-revision", "scope"])
def test_sparse_freeze_requires_exact_native_acknowledged_evidence(damage):
    task, index, request, _ = fixture()
    if damage == "missing":
        task.evidence.pop()
    elif damage == "changed":
        task.evidence[0].output = "Different synthetic output"
    elif damage == "reordered":
        task.evidence.reverse()
    elif damage == "future-revision":
        task.objective_revision = 1
    else:
        task.content_scope = "public_synthetic"
        index.scope = "private_cloud_authorized"
    with pytest.raises(OutcomeEvidenceUnavailable):
        bounded_outcome_request(request, task, index)


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "authenticated-user"),
        ("sequence", 1),
        ("epoch", 2),
        ("session_id", "foreign"),
        ("thread_id", "foreign"),
        ("task_refs", ["foreign"]),
        ("kind", "agent.outcome"),
        ("idempotency_key", "foreign"),
    ],
)
def test_tool_ack_must_be_exact_native_request(field, value):
    task, _, _, entries = fixture(1)
    index = OutcomeEvidenceIndex(task, 1)
    entry = entries[1]
    request = AppendRequest.model_validate(
        entry.model_dump(include=set(AppendRequest.model_fields))
    )
    with pytest.raises(OutcomeEvidenceUnavailable):
        index.observe_ack(request, entry.model_copy(update={field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_count", True),
        ("evidence_count", 2001),
        ("evidence_bytes", -1),
        ("through_sequence", 9999),
        ("after_sequence", 0),
        ("receipts_sha256", "0" * 64),
        ("evidence_sha256", "F" * 64),
        ("content_scope", "public"),
        ("objective_revision", 1),
        ("task_id", "foreign"),
        ("extra", "untrusted"),
    ],
)
async def test_malformed_or_changed_commitment_never_restores(field, value):
    task, index, request, entries = fixture()
    sparse = bounded_outcome_request(request, task, index)
    sparse.payload["evidence_ref"][field] = value
    with pytest.raises(OutcomeEvidenceUnavailable):
        await read_outcome_task(Journal(entries), outcome(sparse, entries))


@pytest.mark.parametrize(
    "damage", ["missing", "changed", "foreign", "reorder", "duplicate", "trailing"]
)
async def test_incomplete_or_malformed_page_is_not_partial_proof(damage):
    task, index, request, entries = fixture()
    sparse = bounded_outcome_request(request, task, index)
    native = outcome(sparse, entries)
    if damage == "missing":
        entries.pop(5)
    elif damage == "changed":
        entries[5].payload["evidence"]["output"] = "Changed observed bytes"
    elif damage == "foreign":
        entries[5] = entries[5].model_copy(update={"origin": "authenticated-user"})
    elif damage == "reorder":
        entries[4], entries[5] = entries[5], entries[4]
    elif damage == "duplicate":
        entries.insert(5, entries[5])
    else:

        class Trailing(Journal):
            async def read(self, **query):
                page = await super().read(**query)
                return [*page, {"sequence": 999}] if query["kinds"] == ["agent.tool"] else page

        with pytest.raises(OutcomeEvidenceUnavailable):
            await read_outcome_task(Trailing(entries), native)
        return
    with pytest.raises(OutcomeEvidenceUnavailable):
        await read_outcome_task(Journal(entries), native)


async def test_pagination_and_explicit_read_deadline(monkeypatch):
    task, index, request, entries = fixture()
    sparse = bounded_outcome_request(request, task, index)
    monkeypatch.setattr(contract, "EVIDENCE_PAGE_LIMIT", 3)
    journal = Journal(entries)
    assert await read_outcome_task(journal, outcome(sparse, entries)) == task
    assert len(journal.reads) == 8
    monkeypatch.setattr(contract, "EVIDENCE_READ_TIMEOUT_S", 0.01)

    class Held(Journal):
        async def read(self, **query):
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await read_outcome_task(Held(entries), outcome(sparse, entries))


@pytest.mark.parametrize("external_cancel", [False, True])
async def test_cancellation_resistant_read_cannot_return_usable_proof(monkeypatch, external_cancel):
    task, index, request, entries = fixture()
    sparse = bounded_outcome_request(request, task, index)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Resistant(Journal):
        async def read(self, **query):
            if not entered.is_set():
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
            return await super().read(**query)

    monkeypatch.setattr(contract, "EVIDENCE_READ_TIMEOUT_S", 10 if external_cancel else 0.01)
    operation = asyncio.create_task(read_outcome_task(Resistant(entries), outcome(sparse, entries)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if external_cancel:
            operation.cancel()
        await asyncio.wait_for(cancelled.wait(), 1)
        release.set()
        with pytest.raises(asyncio.CancelledError if external_cancel else TimeoutError):
            await asyncio.wait_for(operation, 1)
    finally:
        release.set()
        operation.cancel()
        await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), 1)


def test_metadata_overflow_is_refused_without_cropping_or_post():
    task, index, request, entries = fixture()
    request.payload["task"]["objective"] = "public metadata " * 10000
    with pytest.raises(OutcomeEvidenceUnavailable):
        bounded_outcome_request(request, task, index)
    assert len(request.payload["task"]["objective"]) > contract.MAX_OUTCOME_BYTES


def test_restore_refuses_nonempty_inline_competitor_or_foreign_outcome():
    task, index, request, entries = fixture()
    sparse = bounded_outcome_request(request, task, index)
    native = outcome(sparse, entries)
    native.payload["task"]["evidence"] = [task.evidence[0].model_dump(mode="json")]
    with pytest.raises(OutcomeEvidenceUnavailable):
        restore_outcome_task(native, index, task.evidence)


def test_prefix_contract_refuses_nonterminal_task_even_with_valid_receipts():
    task, index, request, entries = fixture()
    sparse = bounded_outcome_request(request, task, index)
    sparse.payload["state"] = sparse.payload["task"]["state"] = "paused"
    with pytest.raises(OutcomeEvidenceUnavailable):
        restore_outcome_task(outcome(sparse, entries), index, task.evidence)
