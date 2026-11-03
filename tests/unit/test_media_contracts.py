"""Independent synthetic oracles for the frozen media v3 intake contract.

Fixtures come from the selected wire grammar, not a production encoder. Native
references here are structural examples and confer no permission or cleanup.
"""

import copy
import hashlib
import json

import pytest

from packages.contracts import media

_MAX_INTEGER = 9_007_199_254_740_991
_ABC_SHA = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
_UPLOAD_DIGEST = "e4fed27764578e60283fba9ed7f9ee14c94cc34ce788bbdd1562b3a6d13bdf10"
_RECEIPT_DIGEST = "0346257782180496b5647f5a5010f777124021f0dffd0c0948dc01e4a2508d01"
_UPLOAD_JSON = (
    b'{"body_bytes":3,"body_sha256":"'
    b"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    b'","fence":{"generation":1,"lease_generation":1,"session":{"epoch":1,'
    b'"session_id":"synthetic-session","thread_id":"synthetic-thread"},'
    b'"source_revision":1},"index":0,"key":"upload-0","offset":0,"source":{'
    b'"backend_id":"00000000000000000000000000000000",'
    b'"receipt_id":"11111111111111111111111111111111",'
    b'"sha256":"2222222222222222222222222222222222222222222222222222222222222222"},'
    b'"v":1}'
)


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _upload(**changes):
    result = json.loads(_UPLOAD_JSON)
    result.update(changes)
    return result


def _rejected(raw, kind, code="bad_packet"):
    with pytest.raises(ValueError) as failure:
        media.media_json(raw, kind)
    assert failure.value.code == code


def _ack():
    return {
        "v": 1,
        "source": _upload()["source"],
        "index": 0,
        "offset": 0,
        "body_bytes": 3,
        "body_sha256": _ABC_SHA,
        "next_offset": 3,
    }


def _receipt():
    return {
        "v": 1,
        "id": "a" * 32,
        "key": "upload-0",
        "request_sha256": _UPLOAD_DIGEST,
        "body": _ack(),
        "sha256": _RECEIPT_DIGEST,
    }


def test_fixed_upload_bytes_and_domain_digest_are_exact():
    assert len(_UPLOAD_JSON) == 476
    value = media.media_json(_UPLOAD_JSON, media.MediaUploadPut)
    assert type(value) is media.MediaUploadPut
    assert value.body_bytes == 3
    assert value.fence.session.session_id == "synthetic-session"
    assert media.media_bytes(value) == _UPLOAD_JSON
    assert media.media_digest("UploadPut", value) == _UPLOAD_DIGEST
    assert media.media_digest("UploadPut", _upload()) == _UPLOAD_DIGEST
    assert media.media_json(_UPLOAD_JSON, media.MediaUploadPut) is not value


def test_codec_canonicalizes_valid_noncanonical_json_without_weakening_wire():
    raw = _upload(key="é")
    reordered = dict(reversed(list(raw.items())))
    incoming = b" \n" + json.dumps(reordered, ensure_ascii=True).encode("utf-8") + b"\n"
    assert incoming != _json(raw)
    value = media.media_json(incoming, media.MediaUploadPut)
    assert media.media_bytes(value) == _json(raw)


@pytest.mark.parametrize(
    "raw",
    [
        _UPLOAD_JSON.replace(b'"body_bytes":3', b'"body_bytes":3,"body_bytes":3'),
        _UPLOAD_JSON.replace(b'"generation":1', b'"generation":1,"generation":1'),
        _json(_upload(owner_authorized=True)),
        _json({k: v for k, v in _upload().items() if k != "key"}),
        _json(_upload(v=True)),
        _json(_upload(index=True)),
        _json(_upload(index=0.0)),
        _json(_upload(index="0")),
        _json(_upload(body_sha256="A" * 64)),
        _UPLOAD_JSON.replace(b'"body_bytes":3', b'"body_bytes":NaN'),
        _UPLOAD_JSON.replace(b'"body_bytes":3', b'"body_bytes":Infinity'),
        _UPLOAD_JSON.replace(b"upload-0", b"\xff"),
        _UPLOAD_JSON + b"{}",
    ],
    ids=[
        "duplicate-top",
        "duplicate-nested",
        "unknown-owner-authority",
        "missing-key",
        "boolean-version",
        "boolean-integer",
        "float-integer",
        "string-integer",
        "uppercase-hash",
        "nan",
        "infinity",
        "invalid-utf8",
        "trailing-json",
    ],
)
def test_closed_upload_rejects_malformed_or_coerced_fields(raw):
    _rejected(raw, media.MediaUploadPut)


def test_nested_unknown_field_cannot_be_lost_by_model_dump():
    raw = _upload()
    raw["fence"]["owner_authorized"] = True
    _rejected(_json(raw), media.MediaUploadPut)
    value = media.media_json(_UPLOAD_JSON, media.MediaUploadPut)
    forged = value.model_copy(
        update={"fence": value.fence.model_copy(update={"owner_authorized": True})}
    )
    with pytest.raises(ValueError):
        media.media_bytes(forged)
    with pytest.raises(ValueError):
        media.media_bytes(value.model_copy(update={"index": True}))


def test_byte_limits_count_utf8_without_normalizing_identity():
    raw = _upload(key="é" * 128)
    value = media.media_json(_json(raw), media.MediaUploadPut)
    assert media.media_bytes(value) == _json(raw)
    _rejected(_json(_upload(key="é" * 129)), media.MediaUploadPut, "bound")
    decomposed = media.media_json(_json(_upload(key="e\u0301")), media.MediaUploadPut)
    composed = media.media_json(_json(_upload(key="é")), media.MediaUploadPut)
    assert media.media_digest("UploadPut", decomposed) != media.media_digest("UploadPut", composed)


def test_exact_max_integer_and_reduced_signed_time_are_preserved():
    session = {"session_id": "s", "thread_id": "t", "epoch": _MAX_INTEGER}
    value = media.media_json(_json(session), media.MediaSession)
    assert value.epoch == _MAX_INTEGER
    _rejected(_json({**session, "epoch": _MAX_INTEGER + 1}), media.MediaSession, "bound")
    signed = media.media_json(b'{"den":2,"num":-1}', media.MediaSignedQ)
    assert media.media_bytes(signed) == b'{"den":2,"num":-1}'
    _rejected(b'{"den":0,"num":1}', media.MediaQ, "bound")
    _rejected(b'{"den":4,"num":2}', media.MediaQ)
    _rejected(b'{"den":2,"num":0}', media.MediaQ)


@pytest.mark.parametrize("field", ["body_bytes", "index", "offset"])
def test_upload_integer_bounds_do_not_coerce_or_wrap(field):
    _rejected(_json(_upload(**{field: _MAX_INTEGER + 1})), media.MediaUploadPut, "bound")
    _rejected(_json(_upload(**{field: -1})), media.MediaUploadPut, "bound")
    if field == "body_bytes":
        _rejected(_json(_upload(body_bytes=0)), media.MediaUploadPut, "bound")


@pytest.mark.parametrize(
    "raw",
    [
        b" " * (65537 - len(_UPLOAD_JSON)) + _UPLOAD_JSON,
        b'{"bomb":[' + b",".join([b"0"] * 8192) + b"]}",
        b'{"bomb":' + b"[" * 23 + b"0" + b"]" * 23 + b"}",
    ],
    ids=["control-byte-ceiling", "node-ceiling-before-closed-shape", "depth-ceiling"],
)
def test_untrusted_json_resource_caps_precede_schema_shape(raw):
    _rejected(raw, media.MediaUploadPut, "bound")


def test_codec_accepts_only_bytes_and_exact_registered_kinds():
    for raw in (_UPLOAD_JSON.decode(), bytearray(_UPLOAD_JSON), memoryview(_UPLOAD_JSON)):
        with pytest.raises(ValueError):
            media.media_json(raw, media.MediaUploadPut)
    for kind in (dict, object, bool):
        with pytest.raises(ValueError):
            media.media_json(_UPLOAD_JSON, kind)

    class UnregisteredUpload(media.MediaUploadPut):
        pass

    with pytest.raises(ValueError):
        media.media_json(_UPLOAD_JSON, UnregisteredUpload)
    with pytest.raises(ValueError):
        media.media_bytes(_upload())
    with pytest.raises(ValueError):
        media.media_digest("CallerChosenDomain", _upload())
    with pytest.raises(ValueError):
        media.media_digest("CapturePut", media.media_json(_UPLOAD_JSON, media.MediaUploadPut))


def test_receipt_domain_omits_only_registered_outer_self_hash():
    raw = _receipt()
    kind = media.MediaReceipt[media.MediaUploadAck]
    value = media.media_json(_json(raw), kind)
    assert type(value) is kind
    assert media.media_bytes(value) == _json(raw)
    assert media.media_digest("R<UploadAck>", value) == _RECEIPT_DIGEST
    without_self = {k: v for k, v in raw.items() if k != "sha256"}
    assert media.media_digest("R<UploadAck>", without_self) == _RECEIPT_DIGEST

    changed = copy.deepcopy(without_self)
    changed["body"]["body_sha256"] = "b" * 64
    expected = hashlib.sha256(b"kinaiya.media.v1/R<UploadAck>\0" + _json(changed)).hexdigest()
    assert expected != _RECEIPT_DIGEST
    assert media.media_digest("R<UploadAck>", changed) == expected
    _rejected(_json({**raw, "body": changed["body"]}), kind)
    _rejected(_json({**raw, "sha256": "0" * 64}), kind)


def test_found_receipt_relation_and_required_null_are_exact():
    kind = media.MediaFound[media.MediaUploadAck]
    for raw in (
        {"v": 1, "found": False, "value": None},
        {"v": 1, "found": True, "value": _receipt()},
    ):
        assert media.media_bytes(media.media_json(_json(raw), kind)) == _json(raw)
    for raw in (
        {"v": 1, "found": True, "value": None},
        {"v": 1, "found": False, "value": _receipt()},
        {"v": 1, "found": False},
    ):
        _rejected(_json(raw), kind)


def test_recording_is_evidence_and_cannot_claim_owner_voice():
    raw = {
        "v": 1,
        "key": "recording-0",
        "session": _upload()["fence"]["session"],
        "kind": "recording",
        "purpose": "analyze",
        "anchor": _upload()["source"],
        "replaces_source": None,
        "declared_bytes": 3,
        "declared_sha256": _ABC_SHA,
        "decoder_profile_sha256": "f" * 64,
        "scope": "public_synthetic",
        "raw_retain_until_ms": None,
    }
    assert media.media_bytes(media.media_json(_json(raw), media.MediaAdmit)) == _json(raw)
    _rejected(_json({**raw, "purpose": "owner_voice", "anchor": None}), media.MediaAdmit)
    _rejected(_json({**raw, "anchor": None}), media.MediaAdmit)
    for nullable in ("replaces_source", "raw_retain_until_ms"):
        _rejected(_json({k: v for k, v in raw.items() if k != nullable}), media.MediaAdmit)


def test_selected_limits_remain_explicit_complete_native_policy():
    raw = {
        "v": 1,
        "source": dict(
            file_bytes=8589934592,
            file_duration_ms=21600000,
            upload_chunk_bytes=4194304,
            owner_spool_bytes=17179869184,
            global_spool_bytes=34359738368,
            unsealed_uploads_per_owner=2,
            operations_global=2,
            live_spool_bytes=536870912,
            live_buffer_target_ms=120000,
            sealed_unstarted_ms=1800000,
        ),
        "decode": dict(
            segment_ms=60000,
            cpu_ms=120000,
            wall_ms=300000,
            address_space_bytes=805306368,
            cpu_affinity_count=1,
            children_per_operation=1,
            children_global=2,
            track_limit=8,
            frames_per_track_segment=3600,
            pixels_per_frame=16777216,
            max_edge=4096,
            frame_rate={"num": 60, "den": 1},
            probe_metadata_bytes=1048576,
            stderr_bytes=65536,
        ),
        "wire": dict(
            control_bytes=65536,
            body_bytes=16777216,
            audio_packet_ms=1000,
            image_bytes=8388608,
            clip_bytes=16777216,
            clip_ms=10000,
            consumer_packets=8,
            consumer_bytes=16777216,
            total_pending_bytes=67108864,
            live_backlog_ms=2000,
        ),
    }
    assert media.media_bytes(media.media_json(_json(raw), media.MediaLimits)) == _json(raw)
    incomplete = copy.deepcopy(raw)
    del incomplete["decode"]["children_per_operation"]
    _rejected(_json(incomplete), media.MediaLimits)
    forged = copy.deepcopy(raw)
    forged["wire"]["ignore_body_bytes"] = True
    _rejected(_json(forged), media.MediaLimits)
