"""Independent malformed wire vectors and a frozen additive export pin (#654)."""

import hashlib
import json
from pathlib import Path

import pytest

from packages.contracts import whistle_stream_owner as contract
from packages.contracts.stt_settlement import digest

ORIGINAL = {
    "schema_version": "1.0",
    "protocol": "whistle_stream_owner_v1",
    "broker_instance": "a" * 32,
    "phase": "pcm",
    "request_sha256": "b" * 64,
    "operation_id": "0" * 31 + "1",
}
NAMES = sorted(
    (
        "WhistleStreamCapability",
        "WhistleStreamReservation",
        "WhistleStreamOriginal",
        "WhistleStreamAcknowledgment",
        "WhistleStreamProof",
        "ConverseStreamLookup",
        "ConverseStreamCapability",
        "ConverseStreamProof",
        "StreamPublication",
        "StreamPublicationProof",
    )
)


def test_incremental_exports_have_their_own_frozen_family_pin():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    pin = hashlib.sha256()
    for name in NAMES:
        path = root / (name + ".json")
        pin.update(path.name.encode() + b"\0" + path.read_bytes())
        assert json.loads(path.read_bytes()) == getattr(contract, name).model_json_schema()
    assert pin.hexdigest() == "02e12dd7abc249efd3e7d09ebfa11ece050333d1582e04ed8f898699c86a6268"


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "native_stt_settlement_v1"},
        {"broker_instance": "a" * 31},
        {"operation_id": 1},
        {"operation_id": "1" * 33},
        {"phase": "transcribe"},
        {"request_sha256": "B" * 64},
        {"request_sha256": None},
        {"audio": "private-input"},
    ],
)
def test_exact_original_refuses_malformed_or_extra_authority(change):
    with pytest.raises(ValueError):
        contract.WhistleStreamOriginal.model_validate({**ORIGINAL, **change})


@pytest.mark.parametrize("value", [64.0, "64", True, None, -1, 65])
def test_capability_has_native_integer_bounds(value):
    with pytest.raises(ValueError):
        contract.WhistleStreamCapability.model_validate(
            {
                "schema_version": "1.0",
                "protocol": "whistle_stream_owner_v1",
                "broker_instance": "a" * 32,
                "active_limit": value,
                "history_limit": 1024,
                "reservation_seconds": 30,
            }
        )


def test_completed_exchange_is_separate_from_decoder_and_physical_cleanup():
    result = {"public_synthetic_result": "unchanged"}
    wire = {
        "schema_version": "1.0",
        "protocol": "whistle_stream_owner_v1",
        "original": ORIGINAL,
        "state": "completed",
        "result": result,
        "result_sha256": digest(result),
        "decoder_finished": False,
        "process_released": False,
    }
    assert contract.WhistleStreamProof.model_validate(wire).result == result
    for change in (
        {"state": "unknown"},
        {"result_sha256": "c" * 64},
        {"decoder_finished": True},
        {"result": None},
        {"process_released": "true"},
        {"result": {"public_synthetic_result": "changed"}},
    ):
        with pytest.raises(ValueError):
            contract.WhistleStreamProof.model_validate({**wire, **change})
    empty = {**wire, "state": "unknown", "result": None, "result_sha256": None}
    unknown = contract.WhistleStreamProof.model_validate(empty)
    assert unknown.state == "unknown" and unknown.process_released is False
    huge = {"text": "\u754c" * 65536}
    with pytest.raises(ValueError):
        contract.WhistleStreamProof.model_validate(
            {**wire, "result": huge, "result_sha256": digest(huge)}
        )


def test_native_validation_error_does_not_expose_private_result_text():
    marker = "PUBLIC-SYNTHETIC-PRIVATE-TRANSCRIPT-MARKER"
    with pytest.raises(ValueError) as error:
        contract.WhistleStreamProof.model_validate(
            {
                "schema_version": "1.0",
                "protocol": "whistle_stream_owner_v1",
                "original": ORIGINAL,
                "state": "completed",
                "result": marker,
                "result_sha256": "f" * 64,
                "decoder_finished": False,
                "process_released": False,
            }
        )
    assert marker not in str(error.value)
