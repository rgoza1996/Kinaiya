"""Independently pinned additive schemas and bounded v2 metadata."""

import hashlib
import json
from pathlib import Path

import pytest

from packages.contracts.live_windows import (
    MAX_CANDIDATE_BYTES,
    MAX_REQUEST_BYTES,
    LiveWindowPublication,
    LiveWindowPublicationRef,
    live_window_json,
)

NAMES = {
    "LiveWindowPublication",
    "LiveWindowPublicationRef",
    "LiveWindowScope",
    "LiveWindowCapture",
    "LiveWindowPublicationProof",
    "LiveWindowPublicationState",
    "LiveWindowCandidate",
}


def test_independent_schema_family_bytes_and_closed_required_native_fields():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    body = b"".join(
        (name + ".json").encode() + b"\0" + (root / (name + ".json")).read_bytes() + b"\0"
        for name in sorted(NAMES)
    )
    assert (
        hashlib.sha256(body).hexdigest()
        == "cbc638d762e21642e40d331b0460c6d99d481ccc72444f868a39dc1a36a3b3b2"
    )
    for name in NAMES:
        schema = json.loads((root / (name + ".json")).read_bytes())
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["properties"]["schema_version"]["const"] == "2.0"


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"null",
        b'{"x":0,"x":1}',
        b'{"x":NaN}',
        b'{"x":1.0}',
        b'{"x":' + b"[" * 10 + b"0" + b"]" * 10 + b"}",
        b" " * (MAX_REQUEST_BYTES + 1),
    ],
)
def test_metadata_parser_rejects_nonobjects_duplicates_floats_depth_and_size(raw):
    with pytest.raises(ValueError):
        live_window_json(raw)


def test_reference_is_closed_strict_and_cannot_supply_execution_authority():
    data = dict(
        schema_version="2.0",
        protocol="live_window_publication_v2",
        owner_instance="a" * 32,
        capture_id="b" * 32,
        session_id="public-session",
        thread_id="public-thread",
        epoch=1,
        utterance_id="public-utterance",
        idempotency_key="public-key",
        request_sha256="c" * 64,
    )
    assert LiveWindowPublicationRef.model_validate(data).epoch == 1
    for change in (
        {"epoch": True},
        {"epoch": 1.0},
        {"capture_id": "unknown"},
        {"may_execute": True},
        {"schema_version": "1.0"},
    ):
        with pytest.raises(ValueError):
            LiveWindowPublicationRef.model_validate(data | change)
    assert MAX_REQUEST_BYTES < MAX_CANDIDATE_BYTES


def test_source_request_is_not_extended_with_native_authority():
    assert set(LiveWindowPublication.model_fields) == {
        "schema_version",
        "protocol",
        "owner_instance",
        "capture_id",
        "request",
    }
