"""Issue #633: closed standing-prefix grammar and full native initial binding."""

import copy
import hashlib
import json

import pytest
from test_agent_outcome_evidence import Journal, fixture, outcome
from test_standing_admission_contract import entry

from packages.contracts.agent_outcome import (
    OutcomeEvidenceIndex,
    OutcomeEvidenceUnavailable,
    bounded_outcome_request,
    read_outcome_task,
)
from packages.contracts.models import JournalEntry, Task, TaskState


def standing_fixture(state=TaskState.FAILED):
    old_task, _, request, old_entries = fixture(state=state)
    initial = JournalEntry.model_validate(entry())
    payload = initial.payload
    task = Task(
        **{
            key: payload[key]
            for key in (
                "task_id",
                "session_id",
                "thread_id",
                "epoch",
                "objective",
                "idempotency_key",
                "content_scope",
                "workspace_pin",
                "model_pin",
                "harness_version",
                "auxiliary_models",
            )
        },
        state=state,
        objective_revision=2,
        evidence=old_task.evidence,
    )
    index = OutcomeEvidenceIndex(task, initial.sequence, initial=initial)
    entries = [initial]
    for number, old in enumerate(old_entries[1:]):
        row = old.model_copy(
            deep=True,
            update={
                "session_id": task.session_id,
                "thread_id": task.thread_id,
                "epoch": task.epoch,
                "task_refs": [task.task_id],
                "sequence": initial.sequence + number + 1,
            },
        )
        row.payload["task_id"] = task.task_id
        index.observe(row)
        entries.append(row)
    request = request.model_copy(
        deep=True,
        update={
            "session_id": task.session_id,
            "thread_id": task.thread_id,
            "epoch": task.epoch,
            "task_refs": [task.task_id],
        },
    )
    request.payload.update(task_id=task.task_id, task=task.model_dump(mode="json"))
    return task, index, request, entries


def terminal(request, entries):
    result = outcome(request, entries)
    result.sequence = entries[-1].sequence + 1
    return result


@pytest.mark.parametrize("state", [TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED])
async def test_standing_v2_binds_full_original_entry_and_ordered_mixed_revision_receipts(state):
    task, index, request, entries = standing_fixture(state)
    original = copy.deepcopy(entries[0].model_dump(mode="json"))
    sparse = bounded_outcome_request(request, task, index)
    reference = sparse.payload["evidence_ref"]
    assert reference["contract"] == "agent.evidence-prefix.v2"
    assert reference["admission"] == {
        "kind": "agent.standing-command",
        "origin": "standing-supervisor",
        "event_id": original["event_id"],
        "sha256": hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
    }
    assert [e.objective_revision for e in task.evidence] == [1, 2] * 10
    journal = Journal(entries)
    assert await read_outcome_task(journal, terminal(sparse, entries)) == task
    assert journal.reads[0]["kinds"] == ["agent.standing-command"]
    # The index retains an immutable commitment, never a caller-owned graph.
    entries[0].payload["standing"]["authorization_digest"] = "f" * 64
    assert bounded_outcome_request(request, task, index) == sparse


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "origin",
        "family",
        "event_id",
        "sequence",
        "created_at",
        "task",
        "epoch",
        "idempotency",
        "objective",
        "scope",
        "authorization",
        "model",
        "extra",
    ],
)
async def test_changed_or_missing_authenticated_standing_initial_is_unavailable(damage):
    task, index, request, entries = standing_fixture()
    sparse = bounded_outcome_request(request, task, index)
    final = terminal(sparse, entries)
    initial = entries[0]
    if damage == "missing":
        entries.pop(0)
    elif damage in {"origin", "family", "event_id", "sequence", "created_at", "epoch"}:
        field = "kind" if damage == "family" else damage
        value = {
            "origin": "agent-supervisor",
            "family": "agent.command",
            "event_id": "different-native-event",
            "sequence": initial.sequence - 1,
            "created_at": initial.created_at.replace(microsecond=999000),
            "epoch": 4,
        }[damage]
        entries[0] = initial.model_copy(deep=True, update={field: value})
    elif damage == "task":
        initial.task_refs = ["foreign-task"]
    elif damage == "idempotency":
        initial.payload["idempotency_key"] = "standing:foreign"
    elif damage == "objective":
        initial.payload["objective"] += " Changed"
    elif damage == "scope":
        initial.payload["content_scope"] = "local_only"
    elif damage == "authorization":
        initial.payload["standing"]["authorization_digest"] = "f" * 64
    elif damage == "model":
        initial.payload["model_pin"]["model"] = "foreign-model"
    else:
        initial.payload["unrecognized"] = "changed native metadata"
    with pytest.raises(OutcomeEvidenceUnavailable):
        await read_outcome_task(Journal(entries), final)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "agent.command"),
        ("origin", "agent-supervisor"),
        ("event_id", "foreign"),
        ("sha256", "F" * 64),
        ("sha256", "0" * 64),
        ("sha256", True),
        ("extra", "unknown"),
    ],
)
async def test_standing_reference_is_closed_and_cannot_change_admission_family(field, value):
    task, index, request, entries = standing_fixture()
    sparse = bounded_outcome_request(request, task, index)
    sparse.payload["evidence_ref"]["admission"][field] = value
    with pytest.raises(OutcomeEvidenceUnavailable):
        await read_outcome_task(Journal(entries), terminal(sparse, entries))


async def test_standing_reference_cannot_downgrade_to_ordinary_v1():
    task, index, request, entries = standing_fixture()
    sparse = bounded_outcome_request(request, task, index)
    reference = sparse.payload["evidence_ref"]
    reference["contract"] = "agent.evidence-prefix.v1"
    del reference["admission"]
    with pytest.raises(OutcomeEvidenceUnavailable):
        await read_outcome_task(Journal(entries), terminal(sparse, entries))
