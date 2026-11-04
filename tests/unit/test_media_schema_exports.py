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
_DECODER_NAMES = {
    f"Media{prefix}{name}.json"
    for prefix in ("", "Receipt", "Found")
    for name in (
        "CodePin",
        "AssetPin",
        "Runtime",
        "AudioFormat",
        "ImageFormat",
        "ClipFormat",
        "Transform",
        "ProbeTrack",
        "ProbeResult",
        "DecoderProtocol",
        "DecoderProfile",
        "Track",
        "CaptureOffer",
        "CaptureReady",
        "DecodeReady",
        "Segment",
        "DecodeRequest",
        "TrackEnd",
        "DecodeEnd",
        "DecodeResponse",
        "InputEnd",
        "DecodeBudget",
        "Format",
    )
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


def test_decoder_schema_bytes_are_pinned_without_changing_the_initial_family():
    assert len(_DECODER_NAMES) == 69
    assert not (_NAMES & _DECODER_NAMES)
    body = bytearray()
    for name in sorted(_DECODER_NAMES):
        body.extend(name.encode() + b"\0" + (_ROOT / name).read_bytes() + b"\0")
    assert hashlib.sha256(body).hexdigest() == (
        "a57d19e47d4b4484d59f830751c32df9bb6fa00071e1f3410e2b16418cb245d4"
    )


def test_decoder_objects_are_closed_required_without_defaults_and_resolve_local_refs():
    for name in sorted(_DECODER_NAMES):
        schema = json.loads((_ROOT / name).read_bytes())
        pending = [schema]
        while pending:
            value = pending.pop()
            if type(value) is dict:
                assert "default" not in value, name
                if "properties" in value:
                    assert value["additionalProperties"] is False, name
                    assert set(value["required"]) == set(value["properties"]), name
                if "$ref" in value:
                    reference = value["$ref"]
                    assert reference.startswith("#/$defs/"), (name, reference)
                    target = schema
                    for part in reference[2:].split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                    assert type(target) is dict, (name, reference)
                pending.extend(value.values())
            elif type(value) is list:
                pending.extend(value)


def test_format_alias_and_its_wrappers_preserve_the_tagged_wire_shape():
    standalone = json.loads((_ROOT / "MediaFormat.json").read_bytes())
    assert set(standalone) == {"$defs", "discriminator", "oneOf"}
    assert set(standalone["$defs"]) == {
        "MediaAudioFormat",
        "MediaImageFormat",
        "MediaClipFormat",
    }
    expected_discriminator = {
        "propertyName": "kind",
        "mapping": {
            "audio": "#/$defs/MediaAudioFormat",
            "image": "#/$defs/MediaImageFormat",
            "video_frame": "#/$defs/MediaImageFormat",
            "video_clip": "#/$defs/MediaClipFormat",
        },
    }
    expected_choices = [
        {"$ref": "#/$defs/MediaAudioFormat"},
        {"$ref": "#/$defs/MediaImageFormat"},
        {"$ref": "#/$defs/MediaClipFormat"},
    ]
    receipt = json.loads((_ROOT / "MediaReceiptFormat.json").read_bytes())
    found = json.loads((_ROOT / "MediaFoundFormat.json").read_bytes())
    wrapped_receipt = found["properties"]["value"]["anyOf"]
    assert len(wrapped_receipt) == 2
    assert wrapped_receipt[1] == {"type": "null"}
    receipt_ref = wrapped_receipt[0]["$ref"]
    nested_receipt = found["$defs"][receipt_ref.removeprefix("#/$defs/")]
    for value in (
        standalone,
        receipt["properties"]["body"],
        nested_receipt["properties"]["body"],
    ):
        assert value["discriminator"] == expected_discriminator
        assert value["oneOf"] == expected_choices
        assert "properties" not in value
        assert "anyOf" not in value
    assert set(receipt["properties"]) == {
        "v",
        "id",
        "key",
        "request_sha256",
        "body",
        "sha256",
    }
    assert set(found["properties"]) == {"v", "found", "value"}
