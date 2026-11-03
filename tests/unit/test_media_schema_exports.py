"""Published intake schema invariants for independent non-Python clients."""

import hashlib
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
_INTAKE_NAMES = (
    "Q",
    "SignedQ",
    "Session",
    "NativeRef",
    "ConsumerRef",
    "Fence",
    "Reservation",
    "SourceLimits",
    "DecodeLimits",
    "WireLimits",
    "Limits",
    "Admit",
    "SourceAdmission",
    "UploadPut",
    "UploadAck",
    "SealRequest",
    "Seal",
    "SourcePTS",
    "CapturePut",
    "CaptureAck",
    "PacketHeader",
    "ASRSession",
    "ASRFeed",
    "ASRFinish",
    "Error",
)
_NAMES = {
    f"Media{prefix}{name}.json" for name in _INTAKE_NAMES for prefix in ("", "Receipt", "Found")
}


def test_initial_media_schema_bytes_are_pinned_independently_of_registry():
    assert len(_NAMES) == 75
    body = bytearray()
    for name in sorted(_NAMES):
        body.extend(name.encode() + b"\0" + (_ROOT / name).read_bytes() + b"\0")
    assert hashlib.sha256(body).hexdigest() == (
        "1f8e19520d07aa5cc6f7f36a26b2728b3d1f34d68a3a4256c56f96db2ef2d63b"
    )


def test_all_exported_intake_objects_remain_closed_and_every_field_required():
    for name in sorted(_NAMES):
        pending = [json.loads((_ROOT / name).read_bytes())]
        while pending:
            value = pending.pop()
            if type(value) is dict:
                if "properties" in value:
                    assert value["additionalProperties"] is False, name
                    assert set(value["required"]) == set(value["properties"]), name
                    assert all("default" not in item for item in value["properties"].values())
                pending.extend(value.values())
            elif type(value) is list:
                pending.extend(value)


def test_intake_schema_fields_do_not_gain_authority_or_implicit_media_defaults():
    expected = {
        "MediaNativeRef": {"backend_id", "receipt_id", "sha256"},
        "MediaUploadPut": {
            "v",
            "key",
            "source",
            "fence",
            "index",
            "offset",
            "body_bytes",
            "body_sha256",
        },
        "MediaPacketHeader": {
            "v",
            "operation_id",
            "consumer",
            "source",
            "fence",
            "payload",
            "track_id",
            "source_seq",
            "through_source_seq",
            "track_seq",
            "pts",
            "range",
            "start",
            "end",
            "format_sha256",
            "body_bytes",
            "body_sha256",
            "continuity_id",
            "sequence",
        },
        "MediaReceiptUploadAck": {"v", "id", "key", "request_sha256", "body", "sha256"},
        "MediaFoundUploadAck": {"v", "found", "value"},
    }
    for name, fields in expected.items():
        schema = json.loads((_ROOT / f"{name}.json").read_bytes())
        assert set(schema["properties"]) == fields
        assert set(schema["required"]) == fields
    packet = json.loads((_ROOT / "MediaPacketHeader.json").read_bytes())["properties"]
    for name in ("track_id", "track_seq", "pts", "range"):
        assert {"type": "null"} in packet[name]["anyOf"]
    assert packet["payload"]["enum"] == ["audio", "image", "video_frame", "video_clip"]
