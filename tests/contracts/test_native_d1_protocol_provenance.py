"""Review-derived regressions for the new provider's unpinned trace identity."""

import pytest
from pydantic import ValidationError

from packages.contracts.decisions import DecisionsCallStart


def request(request_format, *, provider="native_d1"):
    return {
        "call_id": "review-native-call",
        "attempt_id": "review-native-attempt",
        "attempt": 1,
        "source_event_id": "review-native-source",
        "source_sequence": 1,
        "source_revision": 1,
        "session_id": "review-native-session",
        "thread_id": "review-native-thread",
        "epoch": 1,
        "provider": provider,
        "model": "public-d1",
        "request_format": request_format,
        "request": {
            "model": "public-d1",
            "state": "Public synthetic instruction",
            "questions": {"route": {"type": "choice"}},
        },
        "model_pin": None,
    }


@pytest.mark.parametrize("request_format", ["laya_systemone", "chat_completions", "responses"])
def test_unpinned_native_capture_cannot_claim_another_submitted_protocol(request_format):
    # These same new-provider requests were accepted before the review fix.
    with pytest.raises(ValidationError):
        DecisionsCallStart.model_validate(request(request_format))


@pytest.mark.parametrize("provider", ["laya", "openai-compatible", None])
def test_native_submitted_protocol_cannot_claim_another_unpinned_provider(provider):
    with pytest.raises(ValidationError):
        DecisionsCallStart.model_validate(request("native_d1_systemone", provider=provider))


@pytest.mark.parametrize("request_format", ["laya_systemone", "chat_completions", "responses"])
def test_historical_unpinned_legacy_formats_keep_their_original_shape(request_format):
    value = request(request_format, provider="legacy-fixture")
    capture = DecisionsCallStart.model_validate(value)
    assert capture.provider == "legacy-fixture" and capture.request_format == request_format
    assert capture.model_pin is None and capture.request == value["request"]


def test_actual_native_capture_has_no_invented_artifact_receipt():
    value = request("native_d1_systemone")
    capture = DecisionsCallStart.model_validate(value)
    assert capture.request == value["request"] and capture.artifact_sha256 is None
    with pytest.raises(ValidationError):
        DecisionsCallStart.model_validate({**value, "artifact_sha256": "a" * 64})
