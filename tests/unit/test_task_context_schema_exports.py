"""Additive #486/#559 contracts preserve every legacy schema byte."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.contracts.converse_prompts import ConversePromptConfig
from packages.contracts.task_context import (
    EVIDENCE_KINDS,
    TaskContextObservation,
    TaskContextRequest,
    TaskContextResponse,
    TaskContextSource,
    TaskContextUnavailable,
)

MODELS = (
    ConversePromptConfig,
    TaskContextSource,
    TaskContextRequest,
    TaskContextObservation,
    TaskContextResponse,
)


def test_exact_five_additive_exports_match_models_and_have_their_own_byte_pin():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    paths = sorted(root / f"{model.__name__}.json" for model in MODELS)
    raw = b"".join(path.name.encode() + b"\0" + path.read_bytes() + b"\0" for path in paths)
    assert len(paths) == 5
    assert (
        hashlib.sha256(raw).hexdigest()
        == "51ca908c96e72ea442b68d59db0d4f465972cb8e82d26067dd5c391053a67da5"
    )
    for model in MODELS:
        assert (
            json.loads((root / f"{model.__name__}.json").read_text()) == model.model_json_schema()
        )


def wire():
    source = {
        "source_event_id": "public-source",
        "source_sequence": 1,
        "source_revision": 1,
        "session_id": "public-session",
        "thread_id": "public-thread",
        "epoch": 1,
    }
    return {
        "contract": "converse_task_context.v1",
        "source": source,
        "instance_id": "public-instance",
        "observed_at": "2026-11-14T00:00:00+00:00",
        "read_only": True,
        "truncated": False,
        "tasks": [
            {
                "task_id": "public-task",
                "session_id": "public-session",
                "thread_id": "public-thread",
                "epoch": 1,
                "objective_revision": 1,
                "state": "running",
                "submission_confirmed": True,
                "steer_pending": False,
                "completion_publication_pending": False,
                "content_scope": "public_synthetic",
                "evidence_counts": dict.fromkeys(EVIDENCE_KINDS, 0),
                "required_work_count": 0,
                "program_execution_receipt": "not_observed",
                "semantic_verification": "not_assessed",
                "observation_basis": "native_runtime_snapshot",
            }
        ],
    }


@pytest.mark.parametrize(
    "family,field,value",
    [
        ("response", "read_only", 1),
        ("response", "truncated", 0),
        ("response", "contract", "v2"),
        ("response", "observed_at", "2026-11-14T00:00:00"),
        ("task", "epoch", True),
        ("task", "required_work_count", -1),
        ("task", "objective_revision", 0),
        ("task", "state", "complete"),
        ("task", "semantic_verification", "verified"),
        ("task", "submission_confirmed", False),
        ("task", "task_id", "private\ntext"),
        ("task", "evidence_counts", {"file": 1}),
        ("task", "evidence_counts", {k: True for k in EVIDENCE_KINDS}),
        ("task", "evidence_counts", {k: 2**53 for k in EVIDENCE_KINDS}),
        ("task", "session_id", "other-session"),
        ("task", "objective", "Not metadata"),
    ],
)
def test_ambiguous_or_authoritative_looking_wire_is_not_an_observation(family, field, value):
    value_wire = wire()
    target = value_wire if family == "response" else value_wire["tasks"][0]
    target[field] = value
    with pytest.raises(ValidationError):
        TaskContextResponse.model_validate(value_wire)


def test_source_and_requested_task_bindings_are_required_even_for_well_formed_wire():
    value = wire()
    response = TaskContextResponse.model_validate(value)
    source = TaskContextSource.model_validate(value["source"])
    assert response.validate_request(TaskContextRequest(source=source)) is response
    with pytest.raises(TaskContextUnavailable):
        response.validate_request(TaskContextRequest(source=source, task_ids=["other-task"]))
    with pytest.raises(TaskContextUnavailable):
        response.validate_request(
            TaskContextRequest(source=source.model_copy(update={"source_revision": 2}))
        )
    for ids in ([], ["same", "same"], [f"task-{n}" for n in range(17)]):
        with pytest.raises(ValidationError):
            TaskContextRequest(source=source, task_ids=ids)
    value["tasks"] *= 2
    with pytest.raises(ValidationError):
        TaskContextResponse.model_validate(value)
