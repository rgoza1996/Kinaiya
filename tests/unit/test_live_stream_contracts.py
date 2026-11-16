"""Independent v2 microphone/readiness bytes and strict original wire bounds."""

import hashlib
import json
from pathlib import Path

import pytest

from packages.contracts import live_streams
from packages.contracts.live_readiness import LiveDecisionsReadiness

NAMES = (
    "LiveStreamCapability",
    "LiveStreamIdentity",
    "LiveStreamScope",
    "LiveStreamBegin",
    "LiveStreamPcm",
    "LiveStreamFinish",
    "LiveStreamCancel",
    "LiveStreamLookup",
    "LiveStreamProof",
    "LiveStreamCancellation",
)


def test_readiness_and_live_stream_exports_have_independent_closed_required_bytes():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    names = sorted((*NAMES, "LiveDecisionsReadiness"))
    raw = b"".join(
        (name + ".json").encode() + b"\0" + (root / (name + ".json")).read_bytes() + b"\0"
        for name in names
    )
    assert (
        hashlib.sha256(raw).hexdigest()
        == "e7453211778e17609023c0488655584ed8d2bf9581873cd7c125c0303e813f34"
    )
    for name in names:
        cls = (
            LiveDecisionsReadiness
            if name == "LiveDecisionsReadiness"
            else getattr(live_streams, name)
        )
        schema = json.loads((root / (name + ".json")).read_bytes())
        assert schema == cls.model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize(
    "change",
    [
        {"epoch": True},
        {"epoch": 1.0},
        {"segment_index": False},
        {"segment_index": 0.0},
        {"segment_index": -1},
        {"segment_index": 1000001},
        {"schema_version": "1.0"},
        {"protocol": "converse_stream_owner_v1"},
        {"content_scope": "untrusted-authority"},
        {"provider_authorized": True},
        {"model_quality": "qualified"},
    ],
)
def test_live_begin_cannot_coerce_originals_or_grant_extra_authority(change):
    request = dict(
        schema_version="2.0",
        protocol=live_streams.PROTOCOL,
        owner_instance="a" * 32,
        capture_id="b" * 32,
        session_id="public-session",
        thread_id="public-thread",
        epoch=1,
        utterance_id="public-utterance",
        content_scope="local_only",
        segment_index=0,
    )
    with pytest.raises(ValueError):
        live_streams.LiveStreamBegin.model_validate(request | change)


@pytest.mark.parametrize("value", [1, 1.0, "true", False])
def test_cancellation_does_not_coerce_a_local_fence_flag(value):
    with pytest.raises(ValueError):
        live_streams.LiveStreamCancellation(
            schema_version="2.0",
            protocol=live_streams.PROTOCOL,
            owner_instance="a" * 32,
            capture_id="b" * 32,
            fenced=value,
            capture=None,
            cleanup_confirmed=False,
        )
