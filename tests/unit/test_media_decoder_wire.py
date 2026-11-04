"""Unit tests for the closed FD3 media decoder request/response frame codec."""

from __future__ import annotations

import hashlib
import json
import struct

import pytest

from packages.contracts import media
from services.common.media_decoder_wire import (
    MediaDecoderWireError,
    decoder_request_bytes,
    decoder_request_json,
    decoder_response_bytes,
    decoder_response_json,
)

_ID_0 = "0" * 32
_ID_1 = "1" * 32
_ID_2 = "2" * 32
_ID_3 = "3" * 32
_ID_4 = "4" * 32
_ID_5 = "5" * 32
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64

_LITERAL_PROBE_REQUEST_JSON = (
    b'{"child_owner":{"backend_id":"00000000000000000000000000000000",'
    b'"receipt_id":"22222222222222222222222222222222",'
    b'"sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},'
    b'"command":"probe","consumer":{"consumer_id":"33333333333333333333333333333333",'
    b'"generation":1},"fence":{"generation":1,"lease_generation":1,'
    b'"session":{"epoch":1,"session_id":"synthetic-session","thread_id":"synthetic-thread"},'
    b'"source_revision":1},"operation_id":"44444444444444444444444444444444",'
    b'"profile_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
    b'"segment":null,"seq":1,"source":{"backend_id":"00000000000000000000000000000000",'
    b'"receipt_id":"11111111111111111111111111111111",'
    b'"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"v":1}'
)
_LITERAL_PROBE_REQUEST_FRAME = (
    struct.pack(">I", len(_LITERAL_PROBE_REQUEST_JSON)) + _LITERAL_PROBE_REQUEST_JSON
)

_LITERAL_CANCEL_ACK_JSON = (
    b'{"end":null,"error":null,"operation_id":"44444444444444444444444444444444",'
    b'"packet":null,"probe":null,"result":"cancel_ack","segment_id":null,"seq":4,"v":1}'
)
_LITERAL_CANCEL_ACK_FRAME = (
    struct.pack(">I", len(_LITERAL_CANCEL_ACK_JSON)) + _LITERAL_CANCEL_ACK_JSON
)


def _json(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _frame(raw_header: bytes, body: bytes = b"") -> bytes:
    return struct.pack(">I", len(raw_header)) + raw_header + body


def _assert_wire_error(exc_info, expected_code: str) -> None:
    err = exc_info.value
    assert isinstance(err, MediaDecoderWireError)
    assert isinstance(err, ValueError)
    assert err.code == expected_code
    assert str(err) == "Invalid media decoder frame"
    assert "synthetic-untrusted-detail" not in str(err)


def _base_request_dict(command: str = "probe") -> dict:
    base = json.loads(_LITERAL_PROBE_REQUEST_JSON)
    base["command"] = command
    if command == "open":
        base["segment"] = {
            "segment_id": _ID_5,
            "index": 0,
            "source": base["source"],
            "source_revision": 1,
            "generation": 1,
            "profile_sha256": _HASH_C,
            "continuity_id": _ID_0,
            "start": {"num": 0, "den": 1},
            "end": {"num": 10, "den": 1},
            "track_ids": [_ID_1],
        }
    return base


def _packet_header_dict(
    body: bytes = b"\x00\xff\x00pcm",
    *,
    payload: str = "audio",
) -> dict:
    base = _base_request_dict("probe")
    pts = (
        {
            "pts": 0,
            "time_base_num": 1,
            "time_base_den": 16000,
            "sample_start": 0,
            "sample_count": 2,
            "frame_index": None,
        }
        if payload == "audio"
        else (
            {
                "pts": 0,
                "time_base_num": 1,
                "time_base_den": 30,
                "sample_start": None,
                "sample_count": None,
                "frame_index": 0 if payload == "image" else 3,
            }
            if payload in ("image", "video_frame")
            else None
        )
    )
    return {
        "v": 1,
        "operation_id": base["operation_id"],
        "consumer": base["consumer"],
        "source": base["source"],
        "fence": base["fence"],
        "payload": payload,
        "track_id": None if payload == "video_clip" else _ID_1,
        "source_seq": 1,
        "through_source_seq": 2 if payload == "video_clip" else 1,
        "track_seq": None if payload == "video_clip" else 1,
        "pts": pts,
        "range": (
            {"backend_id": _ID_0, "receipt_id": _ID_3, "sha256": _HASH_A}
            if payload == "video_clip"
            else None
        ),
        "start": {"num": 0, "den": 1},
        "end": {"num": 1, "den": 16000 if payload == "audio" else 1},
        "format_sha256": _HASH_B,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "continuity_id": _ID_0,
        "sequence": 1,
    }


def _response_dict(
    result: str,
    *,
    body: bytes = b"\x00\xff\x00pcm",
    payload: str = "audio",
    end_kind: str = "segment_end",
) -> dict:
    base = _base_request_dict("probe")
    if result == "probe":
        return {
            "v": 1,
            "operation_id": base["operation_id"],
            "seq": 1,
            "segment_id": None,
            "result": "probe",
            "probe": {
                "v": 1,
                "source_sha256": _HASH_A,
                "input_bytes": 4096,
                "container": "wav",
                "tracks": [
                    {
                        "index": 0,
                        "kind": "audio",
                        "codec": "pcm_s16le",
                        "rate": 16000,
                        "channels": 1,
                        "width": None,
                        "height": None,
                        "timebase": {"num": 1, "den": 16000},
                        "duration": {"num": 1, "den": 1},
                    }
                ],
            },
            "packet": None,
            "end": None,
            "error": None,
        }
    if result == "packet":
        return {
            "v": 1,
            "operation_id": base["operation_id"],
            "seq": 2,
            "segment_id": _ID_5,
            "result": "packet",
            "probe": None,
            "packet": _packet_header_dict(body, payload=payload),
            "end": None,
            "error": None,
        }
    if result == "end":
        track_state = "eof" if end_kind == "source_eof" else "segment_boundary"
        return {
            "v": 1,
            "operation_id": base["operation_id"],
            "seq": 3,
            "segment_id": _ID_5,
            "result": "end",
            "probe": None,
            "packet": None,
            "end": {
                "v": 1,
                "segment_id": _ID_5,
                "source": base["source"],
                "sealed_source": base["source"],
                "child_owner": base["child_owner"],
                "profile_sha256": _HASH_C,
                "kind": end_kind,
                "through_packet_sequence": 2,
                "tracks": [
                    {
                        "track_id": _ID_1,
                        "through_track_seq": 1,
                        "through_source_seq": 1,
                        "last_pts": {
                            "pts": 0,
                            "time_base_num": 1,
                            "time_base_den": 16000,
                            "sample_start": 0,
                            "sample_count": 2,
                            "frame_index": None,
                        },
                        "state": track_state,
                    }
                ],
            },
            "error": None,
        }
    if result == "cancel_ack":
        return json.loads(_LITERAL_CANCEL_ACK_JSON)
    if result == "error":
        return {
            "v": 1,
            "operation_id": base["operation_id"],
            "seq": 5,
            "segment_id": _ID_5,
            "result": "error",
            "probe": None,
            "packet": None,
            "end": None,
            "error": {"v": 1, "error": "unsupported"},
        }
    raise AssertionError(f"Unexpected result variant: {result}")


def test_literal_external_request_and_response_frames_match_exact_bytes():
    parsed_req = decoder_request_json(_LITERAL_PROBE_REQUEST_FRAME)
    assert type(parsed_req) is media.MediaDecodeRequest
    assert decoder_request_bytes(parsed_req) == _LITERAL_PROBE_REQUEST_FRAME

    parsed_resp, body = decoder_response_json(_LITERAL_CANCEL_ACK_FRAME)
    assert type(parsed_resp) is media.MediaDecodeResponse
    assert body == b""
    assert decoder_response_bytes(parsed_resp, b"") == _LITERAL_CANCEL_ACK_FRAME


@pytest.mark.parametrize("command", ["probe", "open", "next", "cancel"])
def test_all_decode_request_commands_roundtrip_exact_canonical_frames(command):
    raw_json = _json(_base_request_dict(command))
    expected_frame = _frame(raw_json)
    model = media.media_json(raw_json, media.MediaDecodeRequest)
    encoded = decoder_request_bytes(model)
    assert encoded == expected_frame
    decoded = decoder_request_json(encoded)
    assert decoded == model
    assert media.media_bytes(decoded) == raw_json


@pytest.mark.parametrize(
    "result,payload,end_kind,body",
    [
        ("probe", "audio", "segment_end", b""),
        ("packet", "audio", "segment_end", b"\x00\x01\x00\xff\x00pcm-nul"),
        ("packet", "image", "segment_end", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
        ("packet", "video_frame", "segment_end", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"),
        ("packet", "video_clip", "segment_end", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"),
        ("end", "audio", "segment_end", b""),
        ("end", "audio", "source_eof", b""),
        ("cancel_ack", "audio", "segment_end", b""),
        ("error", "audio", "segment_end", b""),
    ],
)
def test_all_decode_response_variants_preserve_exact_headers_and_nul_bodies(
    result, payload, end_kind, body
):
    raw_json = _json(_response_dict(result, body=body, payload=payload, end_kind=end_kind))
    expected_frame = _frame(raw_json, body)
    model = media.media_json(raw_json, media.MediaDecodeResponse)
    encoded = decoder_response_bytes(model, body)
    assert encoded == expected_frame
    decoded, decoded_body = decoder_response_json(encoded)
    assert decoded == model
    assert decoded_body == body
    assert media.media_bytes(decoded) == raw_json


@pytest.mark.parametrize("prefix", [b"", b"\x00", b"\x00\x00", b"\x00\x00\x01"])
def test_short_prefix_fails_as_bad_packet(prefix):
    with pytest.raises(MediaDecoderWireError) as req_exc:
        decoder_request_json(prefix)
    _assert_wire_error(req_exc, "bad_packet")

    with pytest.raises(MediaDecoderWireError) as resp_exc:
        decoder_response_json(prefix)
    _assert_wire_error(resp_exc, "bad_packet")


@pytest.mark.parametrize(
    "non_bytes",
    [
        bytearray(_LITERAL_PROBE_REQUEST_FRAME),
        memoryview(_LITERAL_PROBE_REQUEST_FRAME),
        "frame",
        None,
        123,
    ],
)
def test_non_bytes_inputs_fail_as_bad_packet(non_bytes):
    with pytest.raises(MediaDecoderWireError) as req_exc:
        decoder_request_json(non_bytes)  # type: ignore[arg-type]
    _assert_wire_error(req_exc, "bad_packet")

    with pytest.raises(MediaDecoderWireError) as resp_exc:
        decoder_response_json(non_bytes)  # type: ignore[arg-type]
    _assert_wire_error(resp_exc, "bad_packet")


@pytest.mark.parametrize("header_len", [0, 65_537, 2**32 - 1])
def test_zero_and_oversize_header_lengths_fail_as_bound(header_len):
    frame = struct.pack(">I", header_len) + b"synthetic-untrusted-detail"
    with pytest.raises(MediaDecoderWireError) as req_exc:
        decoder_request_json(frame)
    _assert_wire_error(req_exc, "bound")

    with pytest.raises(MediaDecoderWireError) as resp_exc:
        decoder_response_json(frame)
    _assert_wire_error(resp_exc, "bound")


def test_exact_64kib_header_length_passes_prefix_cap_and_64kib_plus_one_fails_bound():
    raw_64k = b"{" + b" " * 65_534 + b"}"
    assert len(raw_64k) == 65_536
    with pytest.raises(MediaDecoderWireError) as exc_64k:
        decoder_request_json(_frame(raw_64k))
    # 65,536-byte header passes the prefix bound check and fails schema validation as bad_packet.
    _assert_wire_error(exc_64k, "bad_packet")

    raw_over = b"{" + b" " * 65_535 + b"}"
    assert len(raw_over) == 65_537
    with pytest.raises(MediaDecoderWireError) as exc_over:
        decoder_request_json(_frame(raw_over))
    _assert_wire_error(exc_over, "bound")


def test_truncated_header_fails_as_bad_packet():
    truncated = _LITERAL_PROBE_REQUEST_FRAME[:-1]
    with pytest.raises(MediaDecoderWireError) as req_exc:
        decoder_request_json(truncated)
    _assert_wire_error(req_exc, "bad_packet")

    truncated_resp = _LITERAL_CANCEL_ACK_FRAME[:-1]
    with pytest.raises(MediaDecoderWireError) as resp_exc:
        decoder_response_json(truncated_resp)
    _assert_wire_error(resp_exc, "bad_packet")


@pytest.mark.parametrize(
    "mutated_header",
    [
        b" " + _LITERAL_PROBE_REQUEST_JSON,
        _LITERAL_PROBE_REQUEST_JSON + b"\n",
        json.dumps(
            dict(reversed(list(json.loads(_LITERAL_PROBE_REQUEST_JSON).items()))),
            separators=(",", ":"),
        ).encode("utf-8"),
        _LITERAL_PROBE_REQUEST_JSON.replace(b'"probe"', b'"\\u0070robe"'),
        _LITERAL_PROBE_REQUEST_JSON.replace(b'"seq":1', b'"seq":1,"seq":1'),
        _LITERAL_PROBE_REQUEST_JSON.replace(b'"seq":1', b'"seq":true'),
        _LITERAL_PROBE_REQUEST_JSON.replace(b'"seq":1', b'"seq":1.0'),
        _LITERAL_PROBE_REQUEST_JSON.replace(b"synthetic-session", b"synthetic-\xff"),
        _LITERAL_PROBE_REQUEST_JSON.replace(b'"v":1', b'"extra":1,"v":1'),
        _json({k: v for k, v in json.loads(_LITERAL_PROBE_REQUEST_JSON).items() if k != "seq"}),
    ],
)
def test_noncanonical_duplicate_unknown_or_malformed_request_headers_fail_as_bad_packet(
    mutated_header,
):
    with pytest.raises(MediaDecoderWireError) as exc_info:
        decoder_request_json(_frame(mutated_header))
    _assert_wire_error(exc_info, "bad_packet")


@pytest.mark.parametrize(
    "bomb_header",
    [
        b'{"bomb":[' + b",".join([b"0"] * 8192) + b"]}",
        b'{"bomb":' + b"[" * 24 + b"0" + b"]" * 24 + b"}",
    ],
)
def test_nested_graph_depth_and_node_caps_fail_as_bound_before_trailing_check(bomb_header):
    with pytest.raises(MediaDecoderWireError) as req_exc:
        decoder_request_json(_frame(bomb_header, b"trailing"))
    _assert_wire_error(req_exc, "bound")

    with pytest.raises(MediaDecoderWireError) as resp_exc:
        decoder_response_json(_frame(bomb_header, b"trailing"))
    _assert_wire_error(resp_exc, "bound")


def test_wrong_top_level_wire_type_is_rejected():
    # Request frame passed to response decoder and vice versa.
    with pytest.raises(MediaDecoderWireError) as resp_exc:
        decoder_response_json(_LITERAL_PROBE_REQUEST_FRAME)
    _assert_wire_error(resp_exc, "bad_packet")

    with pytest.raises(MediaDecoderWireError) as req_exc:
        decoder_request_json(_LITERAL_CANCEL_ACK_FRAME)
    _assert_wire_error(req_exc, "bad_packet")

    session = media.MediaSession(session_id="s", thread_id="t", epoch=1)
    with pytest.raises(MediaDecoderWireError) as enc_req_exc:
        decoder_request_bytes(session)  # type: ignore[arg-type]
    _assert_wire_error(enc_req_exc, "bad_packet")

    with pytest.raises(MediaDecoderWireError) as enc_resp_exc:
        decoder_response_bytes(session, b"")  # type: ignore[arg-type]
    _assert_wire_error(enc_resp_exc, "bad_packet")


@pytest.mark.parametrize("suffix", [b"x", b"\x00", _LITERAL_PROBE_REQUEST_FRAME])
def test_trailing_bytes_or_concatenated_frames_fail_on_request(suffix):
    with pytest.raises(MediaDecoderWireError) as exc_info:
        decoder_request_json(_LITERAL_PROBE_REQUEST_FRAME + suffix)
    _assert_wire_error(exc_info, "bad_packet")


@pytest.mark.parametrize("result", ["probe", "end", "cancel_ack", "error"])
def test_non_packet_responses_reject_extra_body_on_encode_and_decode(result):
    raw_json = _json(_response_dict(result))
    model = media.media_json(raw_json, media.MediaDecodeResponse)

    with pytest.raises(MediaDecoderWireError) as enc_exc:
        decoder_response_bytes(model, b"x")
    _assert_wire_error(enc_exc, "bad_packet")

    with pytest.raises(MediaDecoderWireError) as dec_exc:
        decoder_response_json(_frame(raw_json, b"x"))
    _assert_wire_error(dec_exc, "bad_packet")


@pytest.mark.parametrize(
    "bad_body", [b"", b"\x00", b"\x00\xff\x00pc", b"\x00\xff\x00pcm!", b"\x00\xff\x00pcX"]
)
def test_packet_response_rejects_short_long_or_hash_mismatched_body(bad_body):
    good_body = b"\x00\xff\x00pcm"
    raw_json = _json(_response_dict("packet", body=good_body))
    model = media.media_json(raw_json, media.MediaDecodeResponse)

    with pytest.raises(MediaDecoderWireError) as enc_exc:
        decoder_response_bytes(model, bad_body)
    _assert_wire_error(enc_exc, "bad_packet")

    with pytest.raises(MediaDecoderWireError) as dec_exc:
        decoder_response_json(_frame(raw_json, bad_body))
    _assert_wire_error(dec_exc, "bad_packet")


@pytest.mark.parametrize(
    "payload,exact_limit",
    [
        ("audio", 384_000),
        ("image", 8_388_608),
        ("video_frame", 8_388_608),
        ("video_clip", 16_777_216),
    ],
)
def test_packet_payload_exact_bounds_succeed_and_one_over_fails_as_bound(payload, exact_limit):
    body = b"\x00" * exact_limit
    raw_ok = _json(_response_dict("packet", body=body, payload=payload))
    decoded, decoded_body = decoder_response_json(_frame(raw_ok, body))
    assert decoded.packet is not None
    assert decoded.packet.body_bytes == exact_limit
    assert len(decoded_body) == exact_limit

    over_dict = _response_dict("packet", body=b"\x00", payload=payload)
    over_dict["packet"]["body_bytes"] = exact_limit + 1
    with pytest.raises(MediaDecoderWireError) as exc_over:
        decoder_response_json(_frame(_json(over_dict), b"\x00"))
    _assert_wire_error(exc_over, "bound")


def test_nested_dto_segment_and_track_bounds_preserve_bound_classification():
    # Segment duration > 60s -> bound
    over_segment = _base_request_dict("open")
    over_segment["segment"]["end"] = {"num": 61, "den": 1}
    with pytest.raises(MediaDecoderWireError) as seg_exc:
        decoder_request_json(_frame(_json(over_segment)))
    _assert_wire_error(seg_exc, "bound")

    # Segment track_ids > 8 -> bound
    over_tracks = _base_request_dict("open")
    over_tracks["segment"]["track_ids"] = [f"{i:032x}" for i in range(9)]
    with pytest.raises(MediaDecoderWireError) as track_exc:
        decoder_request_json(_frame(_json(over_tracks)))
    _assert_wire_error(track_exc, "bound")


def test_encoders_revalidate_mutated_typed_instances():
    req = media.media_json(_LITERAL_PROBE_REQUEST_JSON, media.MediaDecodeRequest)
    req.__dict__["seq"] = 0
    with pytest.raises(MediaDecoderWireError) as req_bound_exc:
        decoder_request_bytes(req)
    _assert_wire_error(req_bound_exc, "bound")

    req_bad = media.media_json(_LITERAL_PROBE_REQUEST_JSON, media.MediaDecodeRequest)
    req_bad.__dict__["command"] = "open"  # open without segment -> bad_packet
    with pytest.raises(MediaDecoderWireError) as req_bad_exc:
        decoder_request_bytes(req_bad)
    _assert_wire_error(req_bad_exc, "bad_packet")

    resp = media.media_json(_LITERAL_CANCEL_ACK_JSON, media.MediaDecodeResponse)
    object.__setattr__(resp, "__pydantic_extra__", {"injected": 1})
    with pytest.raises(MediaDecoderWireError) as resp_extra_exc:
        decoder_response_bytes(resp, b"")
    _assert_wire_error(resp_extra_exc, "bad_packet")
