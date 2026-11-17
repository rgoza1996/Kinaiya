"""#623: a negative original is independently versioned and grants no positive work."""

import json
from pathlib import Path

import pytest

from packages.contracts.live_streams import (
    LiveStreamAbort,
    LiveStreamAbortCapability,
    LiveStreamAbortProof,
)

ROOT = Path(__file__).resolve().parents[2]


def fixture():
    return json.loads(
        (ROOT / "apps/kinaiya_flutter/test/fixtures/live_stream_abort_native.json").read_text()
    )


def test_actual_native_negative_original_and_closed_required_exports():
    native = fixture()
    request = LiveStreamAbort.model_validate(native["request"])
    proof = LiveStreamAbortProof.model_validate(native["proof"])
    assert proof.original == request and proof.capture.receipt is None
    assert proof.cleanup_confirmed and proof.capture.fence is not None
    for model in (LiveStreamAbort, LiveStreamAbortCapability, LiveStreamAbortProof):
        schema = json.loads(
            (ROOT / "packages/contracts/schemas" / (model.__name__ + ".json")).read_text()
        )
        assert schema == model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize(
    "patch",
    [
        {"schema_version": "2.0"},
        {"protocol": "converse_live_stream_v2"},
        {"reason": "new_permission"},
        {"capture_allowed": True},
    ],
)
def test_negative_original_cannot_change_protocol_or_grant_authority(patch):
    with pytest.raises(ValueError):
        LiveStreamAbort.model_validate({**fixture()["request"], **patch})


@pytest.mark.parametrize(
    "patch",
    [
        {"segment_index": 1},
        {"segment_index": False},
        {"epoch": True},
        {"utterance_id": ""},
        {"content_scope": "model_authorized"},
        {"extra": True},
    ],
)
def test_negative_original_requires_the_closed_exact_initial_begin(patch):
    request = fixture()["request"]
    with pytest.raises(ValueError):
        LiveStreamAbort.model_validate({**request, "initial": {**request["initial"], **patch}})


@pytest.mark.parametrize(
    "patch",
    [
        {"outcome": "unknown"},
        {"outcome": "in_progress"},
        {"capture": None},
        {"request_sha256": "a" * 64},
        {"cleanup_confirmed": 1},
    ],
)
def test_cleanup_cannot_be_confirmed_without_exact_negative_native_evidence(patch):
    with pytest.raises(ValueError):
        LiveStreamAbortProof.model_validate({**fixture()["proof"], **patch})
