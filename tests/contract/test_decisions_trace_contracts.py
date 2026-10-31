import json
from pathlib import Path

import jsonschema
import pytest

from packages.contracts.decisions import DecisionsCallFinish, DecisionsCallStart


@pytest.mark.parametrize("model", [DecisionsCallStart, DecisionsCallFinish])
def test_exported_decisions_capture_schema(model):
    stored = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "packages/contracts/schemas"
            / f"{model.__name__}.json"
        ).read_text()
    )
    assert stored == model.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(stored)


def test_capture_models_revalidate_copied_instances_and_encoded_bounds():
    body = DecisionsCallFinish(source_event_id="source", status="completed")
    with pytest.raises(ValueError):
        DecisionsCallFinish.model_validate(body.model_copy(update={"raw_response": "\0" * 200000}))
    with pytest.raises(ValueError):
        DecisionsCallFinish(
            source_event_id="source",
            status="completed",
            validated_output={"confidence": float("nan")},
        )
