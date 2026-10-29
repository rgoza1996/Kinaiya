"""New companion schemas remain strict and language-neutral."""

import inspect
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from packages.contracts import companion
from packages.contracts.models import MAX_PLAYBACK_GENERATION

MODELS = [
    cls
    for name, cls in vars(companion).items()
    if inspect.isclass(cls)
    and cls.__module__ == companion.__name__
    and issubclass(cls, companion.CompanionWire)
]


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
def test_exported_companion_schema(model):
    path = (
        Path(__file__).resolve().parents[2]
        / "packages/contracts/schemas"
        / (model.__name__ + ".json")
    )
    stored = json.loads(path.read_text())
    assert stored == model.model_json_schema()
    assert stored["additionalProperties"] is False
    jsonschema.Draft202012Validator.check_schema(stored)


@pytest.mark.parametrize("revision", [True, 1.0, "1", 0, -1, MAX_PLAYBACK_GENERATION + 1])
def test_native_revision_wire_is_strict_and_bounded(revision):
    with pytest.raises(ValidationError):
        companion.CallAction(action="answer", expected_revision=revision, idempotency_key="public")


@pytest.mark.parametrize(
    "extra", [{"session_id": "alternate"}, {"scope": "owner"}, {"media": True}]
)
def test_call_request_cannot_supply_authority(extra):
    with pytest.raises(ValidationError):
        companion.CallRequest(
            device_id="synthetic", title="Invitation", idempotency_key="call", **extra
        )
