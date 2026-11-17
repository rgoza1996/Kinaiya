"""#623: retained actual owner wire stays valid under native versioned contracts."""

import json
from pathlib import Path

import pytest

from packages.contracts.live_streams import (
    LiveStreamBegin,
    LiveStreamCancel,
    LiveStreamCancellation,
    LiveStreamCapability,
    LiveStreamFinish,
    LiveStreamPcm,
    LiveStreamProof,
)
from packages.contracts.stt_settlement import digest

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "apps/kinaiya_flutter/test/fixtures/live_stream_native.json"
)


@pytest.mark.parametrize("index", range(4))
def test_actual_native_wire_and_original_digest_match_flutter_controls(index):
    fixture = json.loads(FIXTURE.read_text())
    capability = LiveStreamCapability.model_validate(fixture["capability"])
    row = fixture["operations"][index]
    model = {"begin": LiveStreamBegin, "pcm": LiveStreamPcm, "finish": LiveStreamFinish}[
        row["phase"]
    ]
    request = model.model_validate(row["request"])
    proof = LiveStreamProof.model_validate(row["proof"])
    assert proof.owner_instance == capability.owner_instance == request.owner_instance
    assert proof.request_sha256 == digest(request.model_dump(mode="json"))
    assert proof.capture.capture_open and proof.native.outcome == "confirmed"
    if index == 3:
        assert proof.segment_index == 1 and proof.segment_sample_offset == 160
        assert proof.capture.model_dump(mode="json") == fixture["operations"][0]["proof"]["capture"]


def test_unconfirmed_transport_can_retain_native_publication_without_audio_acknowledgment():
    proof = json.loads(FIXTURE.read_text())["operations"][1]["proof"]
    proof["native"].update(outcome="publication_pending", result=None)
    actual = LiveStreamProof.model_validate(proof)
    assert actual.native.outcome == "publication_pending" and actual.publication is not None


def test_actual_native_cancellation_keeps_permission_and_cleanup_separate():
    row = json.loads(FIXTURE.read_text())["cancel"]
    request = LiveStreamCancel.model_validate(row["request"])
    proof = LiveStreamCancellation.model_validate(row["proof"])
    assert proof.owner_instance == request.owner_instance
    assert proof.capture.original.capture_id == request.capture_id
    assert not proof.capture.capture_open
    assert proof.cleanup_confirmed
    assert proof.capture.fence.request.reason == request.reason
