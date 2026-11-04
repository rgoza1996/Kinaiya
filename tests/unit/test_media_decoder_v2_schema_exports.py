"""Source-aware compatibility checks for the separate decoder-v2 descriptions.

The original decoder family keeps its independent 69-schema byte pin. These
checks cover the exact additive inventory and new type/hash-domain boundaries;
they establish no native catalog, launch, budget or physical cleanup authority.
"""

import hashlib
import json
from pathlib import Path

import pytest
import test_media_decoder_contracts as metadata
import test_media_decoder_v2 as fixtures

from packages.contracts import media

_ROOT = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
_SHORT_NAMES = (
    "DecoderProtocolV2",
    "DecoderProfileV2",
    "ProbeClock",
    "ProbeObservation",
    "DecodeSelection",
    "DecodeTrackCursor",
    "DecodeOpenPlan",
    "DecodeRequestV2",
    "DecodeResponseV2",
)
_NAMES = {f"Media{prefix}{name}" for name in _SHORT_NAMES for prefix in ("", "Receipt", "Found")}


def _digest(short, value):
    return hashlib.sha256(
        ("kinaiya.media.v1/" + short + "\0").encode("ascii") + fixtures._json(value)
    ).hexdigest()


def test_v2_schema_inventory_is_exactly_the_nine_registered_records_and_wrappers():
    assert len(_NAMES) == 27
    assert _NAMES <= set(media.MEDIA_SCHEMAS)
    for name in sorted(_NAMES):
        schema = json.loads((_ROOT / f"{name}.json").read_bytes())
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


def test_versioned_profile_and_protocol_schemas_keep_exact_closed_fields():
    protocol = json.loads((_ROOT / "MediaDecoderProtocolV2.json").read_bytes())
    assert set(protocol["properties"]) == {
        "kind",
        "implementation",
        "request_schema_sha256",
        "response_schema_sha256",
        "wire",
        "peer",
        "pts",
        "cleanup",
        "segments",
    }
    assert protocol["properties"]["kind"]["const"] == "decoder_fd_v2"
    profile = json.loads((_ROOT / "MediaDecoderProfileV2.json").read_bytes())
    assert set(profile["properties"]) == {
        "v",
        "id",
        "generation",
        "profile_sha256",
        "driver",
        "implementation",
        "executables",
        "runtime",
        "formats",
        "transform",
        "limits",
        "protocol",
    }
    assert profile["properties"]["v"]["const"] == 1
    assert profile["properties"]["driver"]["const"] == "pyav17_timed_fd_v2"
    assert profile["properties"]["protocol"]["$ref"] == "#/$defs/MediaDecoderProtocolV2"


def test_v2_profile_and_protocol_use_separate_self_hash_and_wrapper_domains():
    raw = fixtures._case()["profile"]
    encoded = fixtures._json(raw)
    profile = media.media_json(encoded, media.MediaDecoderProfileV2)
    assert type(profile) is media.MediaDecoderProfileV2
    assert type(profile.protocol) is media.MediaDecoderProtocolV2
    assert media.media_bytes(profile) == encoded
    unhashed = {key: value for key, value in raw.items() if key != "profile_sha256"}
    expected = _digest("DecoderProfileV2", unhashed)
    assert raw["profile_sha256"] == expected
    assert media.media_digest("DecoderProfileV2", unhashed) == expected
    assert media.media_digest("DecoderProfileV2", profile) == expected
    assert expected != _digest("DecoderProfile", unhashed)
    assert media.media_digest("DecoderProtocolV2", profile.protocol) == _digest(
        "DecoderProtocolV2", raw["protocol"]
    )
    for short, kind in (
        ("DecoderProfileV2", media.MediaDecoderProfileV2),
        ("DecoderProtocolV2", media.MediaDecoderProtocolV2),
    ):
        body = raw if short == "DecoderProfileV2" else raw["protocol"]
        receipt = {
            "v": 1,
            "id": "2" * 32,
            "key": "synthetic-v2-profile",
            "request_sha256": "3" * 64,
            "body": body,
        }
        receipt["sha256"] = _digest(f"R<{short}>", receipt)
        typed = media.media_json(fixtures._json(receipt), media.MediaReceipt[kind])
        assert media.media_bytes(typed) == fixtures._json(receipt)
        found = {"v": 1, "found": True, "value": receipt}
        assert media.media_bytes(
            media.media_json(fixtures._json(found), media.MediaFound[kind])
        ) == fixtures._json(found)
    for old_kind, value in (
        (media.MediaDecoderProfile, raw),
        (media.MediaDecoderProtocol, raw["protocol"]),
    ):
        with pytest.raises(media.MediaContractError):
            media.media_json(fixtures._json(value), old_kind)
    old = metadata._profile()
    with pytest.raises(media.MediaContractError):
        media.media_json(fixtures._json(old), media.MediaDecoderProfileV2)


def test_v2_profile_self_digest_still_validates_the_full_asset_and_nested_graph():
    raw = fixtures._case()["profile"]
    raw.pop("profile_sha256")
    raw["executables"] = [{**raw["executables"][0], "kind": "runtime_library"}]
    with pytest.raises(media.MediaContractError):
        media.media_digest("DecoderProfileV2", raw)
    raw = fixtures._case()["profile"]
    raw.pop("profile_sha256")
    raw["protocol"]["kind"] = "decoder_fd_v1"
    with pytest.raises(media.MediaContractError):
        media.media_digest("DecoderProfileV2", raw)
