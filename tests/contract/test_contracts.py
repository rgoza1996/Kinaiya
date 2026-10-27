import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from packages.contracts.models import AppendRequest, Decision, JournalEntry, Task


def test_source_cannot_supply_provenance():
    with pytest.raises(ValidationError):
        AppendRequest(
            session_id="s",
            thread_id="t",
            epoch=1,
            kind="agent.outcome",
            payload={},
            idempotency_key="k",
            origin="agent-supervisor",
        )


def test_targeted_routing_requires_revision():
    with pytest.raises(ValidationError):
        Decision(source_event_ids=["e"], route="interrupt", idempotency_key="k")


@pytest.mark.parametrize("model", [AppendRequest, Decision, JournalEntry, Task])
def test_exported_contract_matches_python(model):
    root = Path(__file__).resolve().parents[2]
    stored = json.loads((root / f"packages/contracts/schemas/{model.__name__}.json").read_text())
    assert stored == model.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(stored)
