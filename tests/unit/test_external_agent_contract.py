"""Transport-neutral schema and control grammar for issue #547."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.contracts.external_agent import ExternalAgentRequest, ExternalAgentResponse

SCHEMAS = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
TARGET = {"attachment_id": "a" * 32, "backend_session_id": "pi-session"}
SUBMIT = {
    "contract_version": 1,
    "request_id": "request-1",
    "operation": "submit",
    "target": TARGET,
    "idempotency_key": "route:original",
    "source": {"event_ids": ["conversation-turn-1"], "revision": 1},
    "instruction": "/fixture literal\r\nnext \u2028part",
    "content_scope": "local_only",
    "execution_policy": "backend_owned",
    "queue_if_busy": True,
}
REPLAY = {
    "contract_version": 1,
    "request_id": "request-2",
    "kind": "events",
    "target": TARGET,
    "after": 9,
    "next_after": 11,
    "has_more": False,
    "events": [
        {
            "cursor": 10,
            "task_id": "task-1",
            "kind": "task_state",
            "state": "running",
            "revision": 1,
        },
        {
            "cursor": 11,
            "task_id": "task-1",
            "kind": "assistant_message",
            "text": SUBMIT["instruction"],
        },
    ],
}


@pytest.mark.parametrize("packet", [ExternalAgentRequest, ExternalAgentResponse])
def test_external_agent_export_matches_exact_schema(packet):
    exported = (SCHEMAS / f"{packet.__name__}.json").read_bytes()
    assert exported == (json.dumps(packet.model_json_schema(), indent=2) + "\n").encode()


def test_bridge_round_trip_retains_scope_source_and_literal_text():
    request = ExternalAgentRequest.model_validate_json(json.dumps(SUBMIT))
    assert request.root.instruction == SUBMIT["instruction"]
    assert request.root.source.event_ids == ("conversation-turn-1",)
    assert json.loads(request.model_dump_json()) == SUBMIT
    replay = ExternalAgentResponse.model_validate_json(json.dumps(REPLAY))
    assert json.loads(replay.model_dump_json()) == REPLAY


@pytest.mark.parametrize(
    "change",
    [
        {"contract_version": True},
        {"contract_version": 2},
        {"operation": "execute_tool"},
        {"execution_policy": "inherit_kinaiya_permissions"},
        {"target": {"attachment_id": "a" * 32, "backend_session_id": "foreign\nidentity"}},
        {"target": {**TARGET, "workspace": "/unauthorized"}},
        {"source": {"event_ids": [], "revision": 1}},
        {"source": {"event_ids": ["event", "event"], "revision": 1}},
        {"source": {"event_ids": ["event"], "revision": True}},
        {"queue_if_busy": 1},
    ],
)
def test_bridge_refuses_other_authority_shapes_and_loose_identities(change):
    with pytest.raises(ValidationError):
        ExternalAgentRequest.model_validate_json(json.dumps({**SUBMIT, **change}))


def test_replay_refuses_an_undeclared_gap():
    bad = copy.deepcopy(REPLAY)
    bad["events"][1]["cursor"] = 12
    bad["next_after"] = 12
    with pytest.raises(ValidationError, match="replay page is discontinuous"):
        ExternalAgentResponse.model_validate_json(json.dumps(bad))


def test_transcript_cursor_cannot_resume_past_its_snapshot():
    values = {
        "contract_version": 1,
        "request_id": "request-3",
        "operation": "read_transcript",
        "target": TARGET,
        "after": {"sequence": 27, "byte_offset": 5},
        "through_sequence": 27,
        "limit": 16,
    }
    valid = ExternalAgentRequest.model_validate_json(json.dumps(values))
    assert valid.root.after.byte_offset == 5
    for after in ({"sequence": 28, "byte_offset": 0}, {"sequence": 0, "byte_offset": 5}):
        with pytest.raises(ValidationError):
            ExternalAgentRequest.model_validate_json(json.dumps({**values, "after": after}))
