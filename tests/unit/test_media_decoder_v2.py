"""Independent decoder-v2 handshake oracles frozen against baseline a1296e4.

Only the selected public interface, baseline production and old synthetic
metadata constructors were read before authoring. Prospective candidate source
was not read. These are contract/wire/planning checks, not actual child clocks,
native budget resolution, process cleanup, inference or runtime qualification.
New APIs are looked up inside tests so cold collection needs no new imports;
API absence is feature absence, not a meaningful cold regression result.
"""

import copy
import hashlib
import json
import struct

import pytest
import test_media_decoder_contracts as fixtures
import test_media_decoder_wire as wire_fixtures

from packages.contracts import media
from packages.resources import media_decode_plan as planner
from services.common import media_decoder_wire as wire


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _frame(value, body=b""):
    header = value if type(value) is bytes else _json(value)
    return struct.pack(">I", len(header)) + header + body


def _typed(name, raw):
    return media.media_json(_json(raw), getattr(media, name))


def _ref(digit):
    return {"backend_id": "0" * 32, "receipt_id": digit * 32, "sha256": digit * 64}


def _case(*, exchange_audio_ids=False):
    profile = fixtures._profile()
    audio0 = fixtures._audio()
    audio1 = {**audio0, "rate": 24000, "container": "wav", "codec": "pcm_s24le"}
    image = fixtures._image()
    video = {**image, "kind": "video_frame", "container": "jpeg", "codec": "mjpeg"}
    profile["driver"] = "pyav17_timed_fd_v2"
    profile["protocol"]["kind"] = "decoder_fd_v2"
    profile["formats"] = [video, audio1, image, audio0]
    profile = fixtures._self_hash("DecoderProfileV2", profile, "profile_sha256")
    profile_hash = profile["profile_sha256"]
    probe = fixtures._probe()
    probe["container"] = "mp4"
    probe["tracks"] = [
        fixtures._probe_track(7),
        {
            **fixtures._probe_track(3),
            "rate": 24000,
            "codec": "aac",
            "timebase": {"num": 1, "den": 24000},
        },
        {
            **fixtures._probe_track(11),
            "kind": "video",
            "codec": "h264",
            "rate": None,
            "channels": None,
            "width": 640,
            "height": 480,
            "timebase": {"num": 1, "den": 30},
        },
        {
            **fixtures._probe_track(4),
            "kind": "image",
            "codec": "png",
            "rate": None,
            "channels": None,
            "width": 320,
            "height": 200,
            "timebase": {"num": 1, "den": 7},
        },
    ]
    audio_ids = ["1" * 32, "f" * 32] if exchange_audio_ids else ["f" * 32, "1" * 32]
    formats = {7: audio0, 3: audio1, 11: video, 4: image}
    ids = {7: audio_ids[0], 3: audio_ids[1], 11: "8" * 32, 4: "3" * 32}
    tracks = {}
    for original in probe["tracks"]:
        index = original["index"]
        tracks[index] = {
            **fixtures._track(),
            "track_id": ids[index],
            "kind": original["kind"],
            "format_sha256": fixtures._digest("Format", formats[index]),
            "timebase": copy.deepcopy(original["timebase"]),
            "common_origin": {"num": 0, "den": 1} if index == 4 else {"num": -1, "den": 2},
            "clock_provenance": "untimed_image" if index == 4 else "container",
        }
    observation = {
        "v": 2,
        "source": copy.deepcopy(fixtures._SOURCE),
        "seal": copy.deepcopy(fixtures._OWNER),
        "child_owner": _ref("6"),
        "profile_sha256": profile_hash,
        "probe": probe,
        "clocks": [
            {
                "probe_index": 3,
                "first_pts": 6000,
                "timebase": {"num": 1, "den": 24000},
                "provenance": "container",
            },
            {
                "probe_index": 4,
                "first_pts": 0,
                "timebase": {"num": 1, "den": 7},
                "provenance": "untimed_image",
            },
            {
                "probe_index": 11,
                "first_pts": -3,
                "timebase": {"num": 1, "den": 30},
                "provenance": "container",
            },
            {
                "probe_index": 7,
                "first_pts": -8000,
                "timebase": {"num": 1, "den": 16000},
                "provenance": "container",
            },
        ],
    }
    ready = {
        "v": 1,
        "source": copy.deepcopy(fixtures._SOURCE),
        "seal": copy.deepcopy(fixtures._OWNER),
        "probe": copy.deepcopy(probe),
        "decoder_profile_sha256": profile_hash,
        "tracks": [copy.deepcopy(tracks[index]) for index in (3, 4, 7, 11)],
    }
    segment = {
        **fixtures._segment(),
        "profile_sha256": profile_hash,
        "track_ids": [ids[index] for index in (11, 7, 4, 3)],
    }
    plan = {
        "v": 2,
        "observation": observation,
        "ready": ready,
        "selections": [
            {
                "probe_index": index,
                "track": copy.deepcopy(tracks[index]),
                "format": copy.deepcopy(formats[index]),
            }
            for index in (11, 3, 4, 7)
        ],
        "segment": segment,
        "budget_ref": _ref("9"),
        "budget": {
            "v": 1,
            "segment_id": segment["segment_id"],
            "child_owner": copy.deepcopy(observation["child_owner"]),
            "previous": None,
            "cpu_baseline_ms": 20,
            "wall_baseline_ms": 30,
            "cpu_spent_ms": 2,
            "wall_spent_ms": 3,
            "state": "running",
        },
        "next_source_seq": 17,
        "cursors": [{"track_id": ids[index], "next_seq": 5 + index} for index in (4, 11, 3, 7)],
    }
    admission = {
        "v": 1,
        "admission_id": "d" * 32,
        "source_id": fixtures._ID,
        "source_equivalence_id": None,
        "source_revision": 1,
        "session": copy.deepcopy(fixtures._FENCE["session"]),
        "kind": "recording",
        "purpose": "analyze",
        "anchor": copy.deepcopy(fixtures._OWNER),
        "replaces_source": None,
        "capture_instance": None,
        "client_instance": "f" * 32,
        "scope": "public_synthetic",
        "reservation": {
            "reservation_id": "8" * 32,
            "owner_spool_bytes": probe["input_bytes"],
            "global_spool_bytes": probe["input_bytes"],
            "metadata_bytes": 0,
            "child_slots": 1,
            "policy_sha256": fixtures._digest("Limits", profile["limits"]),
        },
        "decoder_profile_sha256": profile_hash,
        "fence": copy.deepcopy(fixtures._FENCE),
        "lease_expires_at_ms": 1794000000000,
        "raw_retain_until_ms": None,
    }
    seal = {
        "v": 1,
        "source": copy.deepcopy(fixtures._SOURCE),
        "source_equivalence_id": "b" * 32,
        "fence": copy.deepcopy(fixtures._FENCE),
        "reservation_id": "8" * 32,
        "bytes": probe["input_bytes"],
        "source_sha256": probe["source_sha256"],
        "decoder_profile_sha256": profile_hash,
        "state": "sealed",
    }
    return {"profile": profile, "plan": plan, "admission": admission, "seal": seal}


def _request(case, command="open"):
    return {
        "v": 2,
        "operation_id": fixtures._ID,
        "source": copy.deepcopy(fixtures._SOURCE),
        "seal": copy.deepcopy(fixtures._OWNER),
        "consumer": copy.deepcopy(fixtures._CONSUMER),
        "fence": copy.deepcopy(fixtures._FENCE),
        "child_owner": _ref("6"),
        "profile_sha256": case["profile"]["profile_sha256"],
        "seq": 2,
        "command": command,
        "profile": copy.deepcopy(case["profile"]) if command == "probe" else None,
        "plan": copy.deepcopy(case["plan"]) if command == "open" else None,
    }


def _response(case, result="probe", *, payload="audio", body=b"abc"):
    value = wire_fixtures._response_dict(result, payload=payload, body=body)
    value["v"] = 2
    if result == "probe":
        value["probe"] = copy.deepcopy(case["plan"]["observation"])
    return value


def _native_args(case, *, profile_kind="MediaDecoderProfileV2"):
    return {
        "source_ref": _typed("MediaNativeRef", fixtures._SOURCE),
        "seal_ref": _typed("MediaNativeRef", fixtures._OWNER),
        "admission": _typed("MediaSourceAdmission", case["admission"]),
        "seal": _typed("MediaSeal", case["seal"]),
        "profile": _typed(profile_kind, case["profile"]),
        "open_plan": _typed("MediaDecodeOpenPlan", case["plan"]),
    }


def _put(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = copy.deepcopy(replacement)


def _rehash_profile(case, *, short="DecoderProfileV2"):
    profile = fixtures._self_hash(short, case["profile"], "profile_sha256")
    case["profile"] = profile
    pin = profile["profile_sha256"]
    case["admission"]["decoder_profile_sha256"] = pin
    case["seal"]["decoder_profile_sha256"] = pin
    case["plan"]["observation"]["profile_sha256"] = pin
    case["plan"]["ready"]["decoder_profile_sha256"] = pin
    case["plan"]["segment"]["profile_sha256"] = pin


@pytest.mark.parametrize("command", ["probe", "open", "next", "cancel"])
def test_v2_request_frames_are_exact_external_canonical_bytes(command):
    raw = _request(_case(), command)
    request = _typed("MediaDecodeRequestV2", raw)
    expected = _frame(raw)
    assert wire.decoder_request_v2_bytes(request) == expected
    recovered = wire.decoder_request_v2_json(expected)
    assert type(recovered) is media.MediaDecodeRequestV2
    assert media.media_bytes(recovered) == _json(raw)


@pytest.mark.parametrize("result", ["probe", "end", "cancel_ack", "error"])
def test_v2_nonpacket_response_frames_roundtrip_without_a_body(result):
    raw = _response(_case(), result)
    value = _typed("MediaDecodeResponseV2", raw)
    expected = _frame(raw)
    assert wire.decoder_response_v2_bytes(value, b"") == expected
    recovered, body = wire.decoder_response_v2_json(expected)
    assert type(recovered) is media.MediaDecodeResponseV2
    assert body == b""
    assert media.media_bytes(recovered) == _json(raw)
    with pytest.raises(wire.MediaDecoderWireError):
        wire.decoder_response_v2_json(expected + b"x")
    with pytest.raises(wire.MediaDecoderWireError):
        wire.decoder_response_v2_bytes(value, b"x")


@pytest.mark.parametrize("payload", ["audio", "image", "video_frame", "video_clip"])
def test_v2_packet_preserves_exact_binary_body_and_refuses_changed_or_extra_bytes(payload):
    body = b"\x00\xff\x00synthetic-payload"
    raw = _response(_case(), "packet", payload=payload, body=body)
    assert raw["packet"]["body_sha256"] == hashlib.sha256(body).hexdigest()
    response = _typed("MediaDecodeResponseV2", raw)
    expected = _frame(raw, body)
    assert wire.decoder_response_v2_bytes(response, body) == expected
    recovered, actual = wire.decoder_response_v2_json(expected)
    assert actual == body
    assert media.media_bytes(recovered) == _json(raw)
    for bad in (expected[:-1], expected + b"x", expected[:-1] + b"!"):
        with pytest.raises(wire.MediaDecoderWireError):
            wire.decoder_response_v2_json(bad)
    with pytest.raises(wire.MediaDecoderWireError):
        wire.decoder_response_v2_bytes(response, body[:-1] + b"!")
    with pytest.raises(wire.MediaDecoderWireError):
        wire.decoder_response_v2_bytes(response, bytearray(body))


def test_v1_literal_wire_vectors_remain_exact_and_neither_codec_auto_selects_version():
    old_frame = wire_fixtures._LITERAL_PROBE_REQUEST_FRAME
    old = wire.decoder_request_json(old_frame)
    assert wire.decoder_request_bytes(old) == old_frame
    old_response = wire_fixtures._LITERAL_CANCEL_ACK_FRAME
    response, body = wire.decoder_response_json(old_response)
    assert body == b""
    assert wire.decoder_response_bytes(response, body) == old_response
    new = _typed("MediaDecodeRequestV2", _request(_case(), "cancel"))
    new_frame = wire.decoder_request_v2_bytes(new)
    for call, value in (
        (wire.decoder_request_json, new_frame),
        (wire.decoder_request_v2_json, old_frame),
        (wire.decoder_request_bytes, new),
        (wire.decoder_request_v2_bytes, old),
        (wire.decoder_response_v2_json, old_response),
        (wire.decoder_response_json, _frame(_response(_case(), "cancel_ack"))),
    ):
        with pytest.raises(wire.MediaDecoderWireError):
            call(value)


@pytest.mark.parametrize("version", [1, True, 2.0, "2", None])
@pytest.mark.parametrize(
    "name",
    [
        "MediaProbeObservation",
        "MediaDecodeOpenPlan",
        "MediaDecodeRequestV2",
        "MediaDecodeResponseV2",
    ],
)
def test_v2_version_is_class_specific_exact_integer_in_raw_and_typed_serialization(name, version):
    case = _case()
    raw = {
        "MediaProbeObservation": case["plan"]["observation"],
        "MediaDecodeOpenPlan": case["plan"],
        "MediaDecodeRequestV2": _request(case),
        "MediaDecodeResponseV2": _response(case),
    }[name]
    original = _typed(name, raw)
    raw["v"] = version
    with pytest.raises(media.MediaContractError):
        _typed(name, raw)
    with pytest.raises(media.MediaContractError):
        media.media_bytes(original.model_copy(update={"v": version}))


@pytest.mark.parametrize("version", [2, True, 1.0, "1"])
def test_v2_addition_does_not_relax_nested_or_existing_v1_versions(version):
    raw = wire_fixtures._base_request_dict()
    raw["v"] = version
    with pytest.raises(media.MediaContractError):
        _typed("MediaDecodeRequest", raw)
    observation = _case()["plan"]["observation"]
    observation["probe"]["v"] = version
    with pytest.raises(media.MediaContractError):
        _typed("MediaProbeObservation", observation)


@pytest.mark.parametrize("driver", ["pyav17_timed_fd_v1", "ffmpeg_cli_fd_v1"])
def test_both_original_profile_drivers_remain_v1_and_exact(driver):
    profile = fixtures._profile()
    profile["driver"] = driver
    profile = fixtures._self_hash("DecoderProfile", profile, "profile_sha256")
    assert media.media_bytes(_typed("MediaDecoderProfile", profile)) == _json(profile)


@pytest.mark.parametrize(
    "driver,protocol",
    [
        ("pyav17_timed_fd_v2", "decoder_fd_v1"),
        ("pyav17_timed_fd_v1", "decoder_fd_v2"),
        ("ffmpeg_cli_fd_v1", "decoder_fd_v2"),
    ],
)
def test_v2_profile_and_protocol_must_be_selected_together(driver, protocol):
    profile = fixtures._profile()
    profile["driver"] = driver
    profile["protocol"]["kind"] = protocol
    profile = fixtures._self_hash("DecoderProfileV2", profile, "profile_sha256")
    with pytest.raises(media.MediaContractError):
        _typed("MediaDecoderProfileV2", profile)


@pytest.mark.parametrize(
    "fault",
    ["missing", "duplicate", "foreign", "timebase", "provenance", "image_pts", "zero_timebase"],
)
def test_probe_observation_requires_exact_original_clock_set(fault):
    raw = _case()["plan"]["observation"]
    if fault == "missing":
        raw["clocks"].pop()
    elif fault == "duplicate":
        raw["clocks"].append(copy.deepcopy(raw["clocks"][0]))
    elif fault == "foreign":
        raw["clocks"][0]["probe_index"] = 99
    elif fault == "timebase":
        raw["clocks"][0]["timebase"] = {"num": 1, "den": 16000}
    elif fault == "provenance":
        raw["clocks"][0]["provenance"] = "untimed_image"
        raw["clocks"][0]["first_pts"] = 0
    elif fault == "image_pts":
        raw["clocks"][1]["first_pts"] = 1
    else:
        raw["clocks"][0]["timebase"] = {"num": 0, "den": 1}
    with pytest.raises(media.MediaContractError):
        _typed("MediaProbeObservation", raw)


@pytest.mark.parametrize(
    "path,value",
    [
        (("ready", "source"), _ref("d")),
        (("ready", "seal"), _ref("d")),
        (("ready", "decoder_profile_sha256"), "d" * 64),
        (("ready", "probe", "source_sha256"), "d" * 64),
        (("segment", "source"), _ref("d")),
        (("segment", "profile_sha256"), "d" * 64),
        (("segment", "track_ids"), ["d" * 32]),
        (("budget", "child_owner"), _ref("d")),
        (("budget", "segment_id"), "e" * 32),
        (("budget", "state"), "ended"),
        (("next_source_seq",), 0),
    ],
)
def test_open_plan_refuses_foreign_bindings_or_nonrunning_budget(path, value):
    raw = _case()["plan"]
    _put(raw, path, value)
    with pytest.raises(media.MediaContractError):
        _typed("MediaDecodeOpenPlan", raw)


@pytest.mark.parametrize(
    "collection,fault",
    [
        ("selections", "missing"),
        ("selections", "duplicate"),
        ("selections", "index"),
        ("selections", "track"),
        ("cursors", "missing"),
        ("cursors", "duplicate"),
        ("cursors", "track"),
    ],
)
def test_open_plan_requires_complete_distinct_selection_and_cursor_sets(collection, fault):
    raw = _case()["plan"]
    items = raw[collection]
    if fault == "missing":
        items.pop()
    elif fault == "duplicate":
        items.append(copy.deepcopy(items[0]))
    elif fault == "index":
        items[0]["probe_index"] = 99
    elif collection == "cursors":
        items[0]["track_id"] = "d" * 32
    else:
        items[0]["track"] = copy.deepcopy(items[1]["track"])
    with pytest.raises(media.MediaContractError):
        _typed("MediaDecodeOpenPlan", raw)


@pytest.mark.parametrize("fault", ["format_digest", "kind", "command_lane", "ready_record"])
def test_selection_cannot_substitute_format_or_native_track_record(fault):
    raw = _case()["plan"]
    selection = raw["selections"][0]
    if fault == "format_digest":
        selection["track"]["format_sha256"] = "d" * 64
    elif fault == "kind":
        selection["format"]["kind"] = "image"
    elif fault == "command_lane":
        selection["track"]["command_lane"] = True
    else:
        selection["track"]["common_origin"] = {"num": 0, "den": 1}
    with pytest.raises(media.MediaContractError):
        _typed("MediaDecodeOpenPlan", raw)


@pytest.mark.parametrize(
    "path,value",
    [
        (("source",), _ref("d")),
        (("seal",), _ref("d")),
        (("child_owner",), _ref("d")),
        (("profile_sha256",), "d" * 64),
        (("fence", "generation"), 2),
        (("fence", "source_revision"), 2),
        (("profile",), {}),
    ],
)
def test_open_request_binds_plan_to_its_original_request(path, value):
    raw = _request(_case())
    _put(raw, path, value)
    with pytest.raises(media.MediaContractError):
        _typed("MediaDecodeRequestV2", raw)


@pytest.mark.parametrize("command", ["probe", "open", "next", "cancel"])
def test_request_nullable_fields_are_required_and_command_specific(command):
    raw = _request(_case(), command)
    for field in ("profile", "plan", "seal"):
        missing = copy.deepcopy(raw)
        missing.pop(field)
        with pytest.raises(media.MediaContractError):
            _typed("MediaDecodeRequestV2", missing)
    wrong = copy.deepcopy(raw)
    wrong["profile"] = _case()["profile"] if command != "probe" else None
    with pytest.raises(media.MediaContractError):
        _typed("MediaDecodeRequestV2", wrong)


@pytest.mark.parametrize("exchange_audio_ids", [False, True])
def test_native_planner_preserves_arbitrary_same_kind_mapping_formats_and_signed_clocks(
    exchange_audio_ids,
):
    case = _case(exchange_audio_ids=exchange_audio_ids)
    plan = planner.build_decode_plan_from_open(**_native_args(case))
    actual = {track.probe_index: track for track in plan.tracks}
    assert set(actual) == {7, 3, 11, 4}
    assert plan.common_origin == (-1, 2)
    expected_pts = {7: -8000, 3: 6000, 11: -3, 4: 0}
    for selection in case["plan"]["selections"]:
        index = selection["probe_index"]
        track = actual[index]
        assert track.track_pin == _json(selection["track"])
        assert track.format_pin == _json(selection["format"])
        assert track.first_pts == expected_pts[index]
        assert (track.timebase_num, track.timebase_den) == (
            selection["track"]["timebase"]["num"],
            selection["track"]["timebase"]["den"],
        )
    assert (actual[11].width, actual[11].height) == (640, 480)
    assert (actual[4].width, actual[4].height) == (320, 200)
    assert actual[4].clock_provenance == "untimed_image"


@pytest.mark.parametrize(
    "target,path,value",
    [
        ("seal", ("source_sha256",), "d" * 64),
        ("seal", ("reservation_id",), "d" * 32),
        ("seal", ("bytes",), 32045),
        ("seal", ("fence", "generation"), 2),
        ("admission", ("fence", "generation"), 2),
        ("admission", ("reservation", "policy_sha256"), "d" * 64),
        ("plan", ("segment", "generation"), 2),
        ("plan", ("segment", "source_revision"), 2),
    ],
)
def test_native_planner_rechecks_original_admission_seal_and_segment(target, path, value):
    case = _case()
    _put(case[target], path, value)
    args = _native_args(case)
    with pytest.raises(planner.MediaDecodePlanError):
        planner.build_decode_plan_from_open(**args)


@pytest.mark.parametrize("target", ["source_ref", "seal_ref"])
def test_native_planner_rejects_foreign_resolved_reference(target):
    args = _native_args(_case())
    args[target] = _typed("MediaNativeRef", _ref("d"))
    with pytest.raises(planner.MediaDecodePlanError):
        planner.build_decode_plan_from_open(**args)


@pytest.mark.parametrize(
    "fault", ["origin", "geometry", "audio_rate", "source_id", "runtime", "code_pin", "v1_profile"]
)
def test_native_planner_keeps_existing_origin_geometry_format_and_runtime_guards(fault):
    case = _case()
    profile_kind = "MediaDecoderProfileV2"
    raw = case["plan"]
    if fault == "origin":
        for track in raw["ready"]["tracks"]:
            track["common_origin"] = {"num": 0, "den": 1}
        by_id = {track["track_id"]: track for track in raw["ready"]["tracks"]}
        for selection in raw["selections"]:
            selection["track"] = copy.deepcopy(by_id[selection["track"]["track_id"]])
    elif fault == "geometry":
        for probe in (raw["observation"]["probe"], raw["ready"]["probe"]):
            probe["tracks"][2]["width"] = 4097
    elif fault == "audio_rate":
        for probe in (raw["observation"]["probe"], raw["ready"]["probe"]):
            probe["tracks"][0]["rate"] = 48000
    elif fault == "source_id":
        for track in raw["ready"]["tracks"]:
            track["source_id"] = "d" * 32
        for selection in raw["selections"]:
            selection["track"]["source_id"] = "d" * 32
    elif fault == "v1_profile":
        case["profile"]["driver"] = "pyav17_timed_fd_v1"
        case["profile"]["protocol"]["kind"] = "decoder_fd_v1"
        # A valid V1 DTO must reach the planner's V2-only boundary.
        profile_kind = "MediaDecoderProfile"
        _rehash_profile(case, short="DecoderProfile")
    else:
        case["profile"]["runtime" if fault == "runtime" else "implementation"] = None
        _rehash_profile(case)
    args = _native_args(case, profile_kind=profile_kind)
    with pytest.raises(planner.MediaDecodePlanError):
        planner.build_decode_plan_from_open(**args)


@pytest.mark.parametrize("direction", ["request", "response"])
@pytest.mark.parametrize(
    "fault",
    [
        "unknown",
        "missing",
        "duplicate_key",
        "whitespace",
        "trailing",
        "wrong_version",
        "short_header",
        "zero_header",
        "large_header",
    ],
)
def test_v2_wire_refuses_noncanonical_or_nonclosed_frames(direction, fault):
    raw = (
        _request(_case(), "cancel") if direction == "request" else _response(_case(), "cancel_ack")
    )
    if fault == "unknown":
        raw["synthetic_untrusted_detail"] = "private-detail"
    elif fault == "missing":
        raw.pop("plan" if direction == "request" else "probe")
    elif fault == "wrong_version":
        raw["v"] = 1
    header = _json(raw)
    if fault == "duplicate_key":
        header = header[:-1] + b',"v":2}'
    elif fault == "whitespace":
        header += b" "
    frame = _frame(header)
    if fault == "trailing":
        frame += b"x"
    elif fault == "short_header":
        frame = b"\x00\x00\x00"
    elif fault == "zero_header":
        frame = struct.pack(">I", 0)
    elif fault == "large_header":
        frame = struct.pack(">I", 65537)
    decode = (
        wire.decoder_request_v2_json if direction == "request" else wire.decoder_response_v2_json
    )
    with pytest.raises(wire.MediaDecoderWireError) as failure:
        decode(frame)
    assert failure.value.code in {"bad_packet", "bound"}
    assert str(failure.value) == "Invalid media decoder frame"


@pytest.mark.parametrize(
    "fault", ["unknown", "operation", "segment", "nullability", "nested_version"]
)
def test_v2_response_preserves_closed_nested_v1_and_linkage_rules(fault):
    raw = _response(_case(), "packet")
    if fault == "unknown":
        raw["unexpected"] = True
    elif fault == "operation":
        raw["packet"]["operation_id"] = "d" * 32
    elif fault == "segment":
        raw["segment_id"] = None
    elif fault == "nullability":
        raw["error"] = {"v": 1, "error": "unavailable"}
    else:
        raw["packet"]["v"] = 2
    with pytest.raises(media.MediaContractError):
        _typed("MediaDecodeResponseV2", raw)


def test_all_seven_new_closed_records_are_registered_and_reject_extra_fields():
    case = _case()
    plan = case["plan"]
    records = (
        ("MediaProbeClock", plan["observation"]["clocks"][0]),
        ("MediaProbeObservation", plan["observation"]),
        ("MediaDecodeSelection", plan["selections"][0]),
        ("MediaDecodeTrackCursor", plan["cursors"][0]),
        ("MediaDecodeOpenPlan", plan),
        ("MediaDecodeRequestV2", _request(case)),
        ("MediaDecodeResponseV2", _response(case)),
    )
    for name, raw in records:
        assert media.media_bytes(_typed(name, raw)) == _json(raw)
        with pytest.raises(media.MediaContractError):
            _typed(name, {**raw, "unselected_field": 1})


def test_wire_does_not_grant_selected_type_privileges_to_dynamic_subclasses():
    class OtherRequest(media.MediaDecodeRequestV2):
        pass

    with pytest.raises((media.MediaContractError, wire.MediaDecoderWireError)):
        request = OtherRequest(**_request(_case(), "cancel"))
        wire.decoder_request_v2_bytes(request)
