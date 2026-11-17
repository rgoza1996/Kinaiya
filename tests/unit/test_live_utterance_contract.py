"""#623: additive closed native boundary; old v2 begin stays closed and unchanged."""

import json
from pathlib import Path

import pytest

from packages.contracts.live_streams import (
    LiveStreamBegin,
    LiveStreamNextUtterance,
    LiveStreamProof,
    LiveUtteranceCapability,
)
from packages.contracts.stt_settlement import digest

ROOT = Path(__file__).resolve().parents[2]


def fixture():
    return json.loads(
        (ROOT / "apps/kinaiya_flutter/test/fixtures/live_utterance_native.json").read_text()
    )


def test_actual_native_boundary_and_previous_final_match_client_fixture():
    native = fixture()
    request = LiveStreamNextUtterance.model_validate(native["request"])
    proof = LiveStreamProof.model_validate(native["proof"])
    assert proof.request_sha256 == digest(request.model_dump(mode="json"))
    assert request.previous_finish_sha256 == digest(native["previous_finish_request"])
    assert proof.capture.capture_open
    assert proof.capture.model_dump(mode="json") == native["previous_finish_proof"]["capture"]
    with pytest.raises(ValueError):
        LiveStreamBegin.model_validate(native["request"])


def test_new_boundary_schema_is_exact_and_requires_every_original_field():
    capability = json.loads(
        (ROOT / "packages/contracts/schemas/LiveUtteranceCapability.json").read_text()
    )
    assert capability == LiveUtteranceCapability.model_json_schema()
    assert capability["additionalProperties"] is False
    schema = json.loads(
        (ROOT / "packages/contracts/schemas/LiveStreamNextUtterance.json").read_text()
    )
    assert schema == LiveStreamNextUtterance.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    native = fixture()["request"]
    for key in native:
        with pytest.raises(ValueError):
            LiveStreamNextUtterance.model_validate(
                {name: value for name, value in native.items() if name != key}
            )
