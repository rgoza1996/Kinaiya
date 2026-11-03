"""Independent synthetic decoder DTO oracles from frozen media v3.

These vectors describe metadata. No file, decoder, qualification, original PTS,
permission, owner join, actual EOF or physical cleanup is established by a DTO.
Production decoder-contract bodies were not read while authoring this module.
"""

import copy
import hashlib
import itertools
import json

import pytest

from packages.contracts import media

_SAFE = 9_007_199_254_740_991
_ID = "a" * 32
_HASH = "b" * 64
_CODE_SHA = "c" * 64
_SOURCE = {"backend_id": "0" * 32, "receipt_id": "1" * 32, "sha256": "2" * 64}
_OWNER = {"backend_id": "0" * 32, "receipt_id": "3" * 32, "sha256": "4" * 64}
_CONSUMER = {"consumer_id": "5" * 32, "generation": 1}
_FENCE = {
    "session": {"session_id": "synthetic-session", "thread_id": "synthetic-thread", "epoch": 1},
    "source_revision": 1,
    "generation": 1,
    "lease_generation": 1,
}
_NAMES = (
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
)


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(name, value):
    return hashlib.sha256(b"kinaiya.media.v1/" + name.encode() + b"\0" + _json(value)).hexdigest()


def _self_hash(name, value, field):
    result = copy.deepcopy(value)
    result.pop(field, None)
    result[field] = _digest(name, result)
    return result


def _code():
    return {
        "commit": "6" * 40,
        "path": "synthetic/decoder.py",
        "git_blob": "7" * 40,
        "sha256": _CODE_SHA,
    }


def _asset(index=0, *, kind="runtime_library", size=7):
    return {
        "kind": kind,
        "path": f"/synthetic/asset-{index}",
        "guest": f"/runtime/asset-{index}",
        "bytes": size,
        "sha256": _HASH,
    }


def _runtime(assets=None):
    if assets is None:
        assets = [
            _asset(0, kind="python_exec"),
            _asset(1, kind="sandbox_exec"),
            _asset(2),
            _asset(3, kind="driver_source"),
        ]
    return _self_hash(
        "Runtime",
        {"v": 1, "kind": "sealed_asset_set", "assets": assets, "distribution_lock": _code()},
        "manifest_sha256",
    )


def _audio():
    return {"kind": "audio", "container": "raw", "codec": "pcm_s16le", "rate": 16000, "channels": 1}


def _image():
    return {"kind": "image", "container": "png", "codec": "png", "pixel_format": "rgb24"}


def _clip():
    return {"kind": "video_clip", "container": "webm", "video_codec": "vp9", "audio_codec": "opus"}


def _transform():
    return {
        "v": 1,
        "kind": "identity",
        "audio_rate": None,
        "audio_channels": None,
        "video_rate": None,
        "width": None,
        "height": None,
        "owner_setting_ref": None,
    }


def _limits():
    return {
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


def _protocol():
    return {
        "kind": "decoder_fd_v1",
        "implementation": _code(),
        "request_schema_sha256": "8" * 64,
        "response_schema_sha256": "9" * 64,
        "wire": "u32be_json_then_body",
        "peer": "owned_pid_incarnation_nonce",
        "pts": "original_rational",
        "cleanup": "pidfd_reap",
        "segments": "retained_child",
    }


def _profile():
    return _self_hash(
        "DecoderProfile",
        {
            "v": 1,
            "id": _ID,
            "generation": 1,
            "driver": "pyav17_timed_fd_v1",
            "implementation": _code(),
            "executables": [_asset(0, kind="python_exec"), _asset(1, kind="sandbox_exec")],
            "runtime": _runtime(),
            "formats": [_audio(), _image(), _clip()],
            "transform": _transform(),
            "limits": _limits(),
            "protocol": _protocol(),
        },
        "profile_sha256",
    )


def _probe_track(index=0):
    return {
        "index": index,
        "kind": "audio",
        "codec": "pcm_s16le",
        "rate": 16000,
        "channels": 1,
        "width": None,
        "height": None,
        "timebase": {"num": 1, "den": 16000},
        "duration": {"num": 1, "den": 1},
    }


def _probe():
    return {
        "v": 1,
        "source_sha256": _HASH,
        "input_bytes": 32044,
        "container": "wav",
        "tracks": [_probe_track()],
    }


def _track(index=0):
    return {
        "track_id": f"{index:032x}",
        "source_id": _ID,
        "kind": "audio",
        "format_sha256": _digest("Format", _audio()),
        "timebase": {"num": 1, "den": 16000},
        "common_origin": {"num": -1, "den": 2},
        "clock_provenance": "container",
        "command_lane": False,
    }


def _segment():
    return {
        "segment_id": "d" * 32,
        "index": 0,
        "source": copy.deepcopy(_SOURCE),
        "source_revision": 1,
        "generation": 1,
        "profile_sha256": _profile()["profile_sha256"],
        "continuity_id": "e" * 32,
        "start": {"num": 1, "den": 3},
        "end": {"num": 181, "den": 3},
        "track_ids": [_track()["track_id"]],
    }


def _track_end(index=0, state="eof"):
    return {
        "track_id": _track(index)["track_id"],
        "through_track_seq": 1,
        "through_source_seq": 1,
        "last_pts": {
            "pts": -8000,
            "time_base_num": 1,
            "time_base_den": 16000,
            "sample_start": 0,
            "sample_count": 16000,
            "frame_index": None,
        },
        "state": state,
    }


def _end(kind="source_eof"):
    return {
        "v": 1,
        "segment_id": "d" * 32,
        "source": copy.deepcopy(_SOURCE),
        "sealed_source": copy.deepcopy(_OWNER),
        "child_owner": copy.deepcopy(_OWNER),
        "profile_sha256": _profile()["profile_sha256"],
        "kind": kind,
        "through_packet_sequence": 1,
        "tracks": [_track_end(state="eof" if kind == "source_eof" else "segment_boundary")],
    }


def _packet():
    return {
        "v": 1,
        "operation_id": _ID,
        "consumer": copy.deepcopy(_CONSUMER),
        "source": copy.deepcopy(_SOURCE),
        "fence": copy.deepcopy(_FENCE),
        "payload": "audio",
        "track_id": _track()["track_id"],
        "source_seq": 1,
        "through_source_seq": 1,
        "track_seq": 1,
        "pts": _track_end()["last_pts"],
        "range": None,
        "start": {"num": 0, "den": 1},
        "end": {"num": 1, "den": 1},
        "format_sha256": _digest("Format", _audio()),
        "body_bytes": 3,
        "body_sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        "continuity_id": "e" * 32,
        "sequence": 1,
    }


def _response(result="end"):
    return {
        "v": 1,
        "operation_id": _ID,
        "seq": 1,
        "segment_id": "d" * 32 if result in ("packet", "end") else None,
        "result": result,
        "probe": _probe() if result == "probe" else None,
        "packet": _packet() if result == "packet" else None,
        "end": _end() if result == "end" else None,
        "error": {"v": 1, "error": "unsupported"} if result == "error" else None,
    }


def _vectors():
    track = _track()
    return {
        "CodePin": _code(),
        "AssetPin": _asset(),
        "Runtime": _runtime(),
        "AudioFormat": _audio(),
        "ImageFormat": _image(),
        "ClipFormat": _clip(),
        "Transform": _transform(),
        "ProbeTrack": _probe_track(),
        "ProbeResult": _probe(),
        "DecoderProtocol": _protocol(),
        "DecoderProfile": _profile(),
        "Track": track,
        "CaptureOffer": {
            "v": 1,
            "key": "capture-0",
            "source": copy.deepcopy(_SOURCE),
            "fence": copy.deepcopy(_FENCE),
            "common_origin": {"num": -1, "den": 2},
            "tracks": [
                {
                    "client_track_key": "microphone",
                    "format_sha256": track["format_sha256"],
                    "timebase": track["timebase"],
                    "command_lane": False,
                }
            ],
        },
        "CaptureReady": {
            "v": 1,
            "source": copy.deepcopy(_SOURCE),
            "fence": copy.deepcopy(_FENCE),
            "capture_instance": "f" * 32,
            "decoder_profile_sha256": _profile()["profile_sha256"],
            "tracks": [copy.deepcopy(track)],
        },
        "DecodeReady": {
            "v": 1,
            "source": copy.deepcopy(_SOURCE),
            "seal": copy.deepcopy(_OWNER),
            "probe": _probe(),
            "decoder_profile_sha256": _profile()["profile_sha256"],
            "tracks": [copy.deepcopy(track)],
        },
        "Segment": _segment(),
        "DecodeRequest": {
            "v": 1,
            "operation_id": _ID,
            "source": copy.deepcopy(_SOURCE),
            "consumer": copy.deepcopy(_CONSUMER),
            "fence": copy.deepcopy(_FENCE),
            "child_owner": copy.deepcopy(_OWNER),
            "profile_sha256": _profile()["profile_sha256"],
            "seq": 1,
            "command": "open",
            "segment": _segment(),
        },
        "TrackEnd": _track_end(),
        "DecodeEnd": _end(),
        "DecodeResponse": _response(),
        "InputEnd": {
            "v": 1,
            "source": copy.deepcopy(_SOURCE),
            "fence": copy.deepcopy(_FENCE),
            "kind": "finite_eof",
            "seal": copy.deepcopy(_OWNER),
            "capture_ready": None,
            "decoder_end": copy.deepcopy(_OWNER),
            "tracks": [_track_end()],
            "through_source_seq": 1,
        },
        "DecodeBudget": {
            "v": 1,
            "segment_id": "d" * 32,
            "child_owner": copy.deepcopy(_OWNER),
            "previous": None,
            "cpu_baseline_ms": 0,
            "wall_baseline_ms": 0,
            "cpu_spent_ms": 7,
            "wall_spent_ms": 11,
            "state": "running",
        },
    }


def _kind(name):
    return getattr(media, "Media" + name)


def _valid(name, raw):
    result = media.media_json(_json(raw), _kind(name))
    assert media.media_bytes(result) == _json(raw)
    return result


def _reject(name, raw, code="bad_packet"):
    with pytest.raises(ValueError) as failure:
        media.media_json(_json(raw), _kind(name))
    assert failure.value.code == code


@pytest.mark.parametrize("name", _NAMES)
def test_manual_complete_decoder_records_roundtrip_without_runtime_evidence(name):
    raw = _vectors()[name]
    first = _valid(name, raw)
    assert type(first) is _kind(name)
    assert media.media_json(_json(raw), _kind(name)) is not first


@pytest.mark.parametrize("name", _NAMES)
def test_decoder_records_are_closed_and_every_field_including_null_is_required(name):
    raw = _vectors()[name]
    _reject(name, {**raw, "owner_authorized": True})
    for field in raw:
        _reject(name, {key: value for key, value in raw.items() if key != field})


@pytest.mark.parametrize("name", _NAMES)
def test_decoder_literal_version_and_integer_fields_never_coerce_booleans(name):
    raw = _vectors()[name]
    for field, value in raw.items():
        if type(value) is int:
            for changed in (True, str(value), float(value)):
                _reject(name, {**raw, field: changed})


@pytest.mark.parametrize("path", ["/absolute.py", "../x", "a/../b", "a\0b"])
def test_code_pin_lexical_path_cannot_escape_repository(path):
    _reject("CodePin", {**_code(), "path": path})


def test_code_pin_and_asset_paths_are_utf8_bounded_metadata_not_filesystem_evidence():
    _valid("CodePin", {**_code(), "path": "é" * 128})
    _reject("CodePin", {**_code(), "path": "é" * 129}, "bound")
    _valid("AssetPin", {**_asset(), "path": "/not-opened/" + "é" * 2042})
    _reject("AssetPin", {**_asset(), "path": "/" + "é" * 2048}, "bound")
    _reject("AssetPin", {**_asset(), "guest": "/" + "é" * 128}, "bound")
    for field in ("commit", "git_blob"):
        _reject("CodePin", {**_code(), field: "A" * 40})


@pytest.mark.parametrize("field", ["path", "guest"])
def test_asset_paths_have_narrow_lexical_checks_without_inventing_filesystem_validation(field):
    _valid("AssetPin", {**_asset(), field: "synthetic/relative-metadata"})
    for path in ("a/../b", "../outside", "a\0b"):
        _reject("AssetPin", {**_asset(), field: path})


def test_independently_fixed_format_runtime_and_profile_hashes():
    expected = {
        "AudioFormat": "bb23a421f626bd9f117db54a3cb564deb9f5e3c23f16b306f2b6fd616fc08dba",
        "ImageFormat": "adf1a5dff28c523f4c610afd716ee21de2e12c176ac14032514159a9558c1da3",
        "ClipFormat": "dacc4007f87208d8ec635e44d705db57e4a53d9e3bd43d93a5c6f168cc97910f",
    }
    for name, digest in expected.items():
        assert media.media_digest("Format", _vectors()[name]) == digest
    assert (
        _runtime()["manifest_sha256"]
        == "231abaaeb067e736835a172941f6a1903913fe4905de0070b5d95a5be48dda0e"
    )
    assert (
        _profile()["profile_sha256"]
        == "4ba0ff6a5ef46892ec9492282ace79b57cc2e076c904c56121667aa064a19d52"
    )


@pytest.mark.parametrize(
    "container,codec,rate,channels",
    itertools.product(
        ("raw", "wav"), ("pcm_s16le", "pcm_s24le", "pcm_s32le"), (16000, 24000, 48000), (1, 2)
    ),
)
def test_audio_format_is_exact_pcm_lattice(container, codec, rate, channels):
    raw = dict(kind="audio", container=container, codec=codec, rate=rate, channels=channels)
    _valid("AudioFormat", raw)
    assert media.media_bytes(media.media_json(_json(raw), media.MediaFormat)) == _json(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"container": "mp3"},
        {"codec": "aac"},
        {"rate": 44100},
        {"channels": 3},
        {"rate": True},
        {"channels": True},
        {"rate": 16000.0},
        {"kind": "audio_url"},
    ],
)
def test_audio_format_does_not_coerce_or_invent_supported_modes(changes):
    _reject("AudioFormat", {**_audio(), **changes})


@pytest.mark.parametrize(
    "kind,container,codec",
    itertools.product(("image", "video_frame"), ("png", "jpeg"), ("png", "mjpeg")),
)
def test_image_format_pairs_are_exact(kind, container, codec):
    raw = dict(kind=kind, container=container, codec=codec, pixel_format="rgb24")
    if (container, codec) in (("png", "png"), ("jpeg", "mjpeg")):
        _valid("ImageFormat", raw)
        assert media.media_bytes(media.media_json(_json(raw), media.MediaFormat)) == _json(raw)
    else:
        _reject("ImageFormat", raw)
    _reject("ImageFormat", {**raw, "pixel_format": "rgba"})


@pytest.mark.parametrize(
    "container,video,audio",
    itertools.product(("mp4", "webm"), ("h264", "hevc", "vp9", "av1"), ("aac", "opus", "none")),
)
def test_clip_format_cross_product_refuses_unselected_container_codec_pairs(
    container, video, audio
):
    raw = dict(kind="video_clip", container=container, video_codec=video, audio_codec=audio)
    allowed = (
        container == "mp4" and video in ("h264", "hevc", "av1") and audio in ("aac", "none")
    ) or (container == "webm" and video in ("vp9", "av1") and audio in ("opus", "none"))
    if allowed:
        _valid("ClipFormat", raw)
        assert media.media_digest("Format", raw) == _digest("Format", raw)
    else:
        _reject("ClipFormat", raw)


@pytest.mark.parametrize("name", ["AudioFormat", "ImageFormat", "ClipFormat"])
def test_format_union_receipt_and_found_use_exact_format_hash_domain(name):
    raw = _vectors()[name]
    value = media.media_json(_json(raw), media.MediaFormat)
    expected = _digest("Format", raw)
    assert media.media_digest("Format", raw) == media.media_digest("Format", value) == expected
    assert expected != _digest(name, raw)
    receipt = {"v": 1, "id": _ID, "key": "format-0", "request_sha256": _HASH, "body": raw}
    receipt["sha256"] = _digest("R<Format>", receipt)
    assert (
        media.media_digest("R<Format>", {k: v for k, v in receipt.items() if k != "sha256"})
        == receipt["sha256"]
    )
    kind = media.MediaReceipt[media.MediaFormat]
    assert media.media_bytes(media.media_json(_json(receipt), kind)) == _json(receipt)
    found = {"v": 1, "found": True, "value": receipt}
    assert media.media_bytes(
        media.media_json(_json(found), media.MediaFound[media.MediaFormat])
    ) == _json(found)
    absent = {"v": 1, "found": False, "value": None}
    assert media.media_bytes(
        media.media_json(_json(absent), media.MediaFound[media.MediaFormat])
    ) == _json(absent)
    for invalid in ({"v": 1, "found": False}, {**absent, "found": True}):
        with pytest.raises(ValueError):
            media.media_json(_json(invalid), media.MediaFound[media.MediaFormat])
    wrong_domain = _digest("R<" + name + ">", {k: v for k, v in receipt.items() if k != "sha256"})
    with pytest.raises(ValueError):
        media.media_json(_json({**receipt, "sha256": wrong_domain}), kind)
    for changed in ({**raw, "unknown": 1}, {**raw, "kind": "unknown"}):
        _reject("Format", changed)


@pytest.mark.parametrize(
    "name,field", [("Runtime", "manifest_sha256"), ("DecoderProfile", "profile_sha256")]
)
def test_self_hash_constructor_omits_only_its_named_root_field(name, field):
    raw = _vectors()[name]
    unhashed = {key: value for key, value in raw.items() if key != field}
    assert raw[field] == _digest(name, unhashed)
    assert (
        media.media_digest(name, unhashed)
        == media.media_digest(name, _valid(name, raw))
        == raw[field]
    )
    _reject(name, {**raw, field: "0" * 64})
    for unknown_field in ("sha256", "owner_authorized"):
        with pytest.raises(ValueError):
            media.media_digest(name, {**unhashed, unknown_field: _HASH})
    for key in unhashed:
        incomplete = {k: v for k, v in unhashed.items() if k != key}
        with pytest.raises(ValueError):
            media.media_digest(name, incomplete)


def test_profile_hash_includes_runtime_self_hash_and_nested_asset_and_code_hashes():
    original = _profile()
    changed = copy.deepcopy(original)
    changed["runtime"]["assets"][2]["sha256"] = "d" * 64
    changed["runtime"] = _self_hash("Runtime", changed["runtime"], "manifest_sha256")
    changed = _self_hash("DecoderProfile", changed, "profile_sha256")
    assert changed["runtime"]["manifest_sha256"] != original["runtime"]["manifest_sha256"]
    assert changed["profile_sha256"] != original["profile_sha256"]
    _valid("DecoderProfile", changed)
    assert (
        media.media_digest(
            "DecoderProfile", {k: v for k, v in changed.items() if k != "profile_sha256"}
        )
        == changed["profile_sha256"]
    )
    stale = copy.deepcopy(changed)
    stale["runtime"]["manifest_sha256"] = original["runtime"]["manifest_sha256"]
    with pytest.raises(ValueError):
        media.media_digest(
            "DecoderProfile", {k: v for k, v in stale.items() if k != "profile_sha256"}
        )
    missing = copy.deepcopy(original)
    del missing["runtime"]["assets"][0]["sha256"]
    with pytest.raises(ValueError):
        media.media_digest(
            "DecoderProfile", {k: v for k, v in missing.items() if k != "profile_sha256"}
        )
    changed_code = copy.deepcopy(original)
    changed_code["implementation"]["sha256"] = "e" * 64
    changed_code = _self_hash("DecoderProfile", changed_code, "profile_sha256")
    assert changed_code["profile_sha256"] != original["profile_sha256"]
    _valid("DecoderProfile", changed_code)


def test_transform_variants_are_explicit_and_never_implicit_permission():
    identity = _transform()
    _valid("Transform", identity)
    audio = {
        **identity,
        "kind": "audio_resample_downmix",
        "audio_rate": 24000,
        "audio_channels": 1,
        "owner_setting_ref": "synthetic-owner-setting",
    }
    video = {
        **identity,
        "kind": "video_sample_scale",
        "video_rate": {"num": 30, "den": 1},
        "width": 640,
        "height": 480,
        "owner_setting_ref": "synthetic-owner-setting",
    }
    for raw in (
        audio,
        video,
        {**video, "video_rate": None},
        {**video, "width": None, "height": None},
    ):
        _valid("Transform", raw)
    for raw in (
        {**identity, "audio_rate": 16000},
        {**audio, "owner_setting_ref": None},
        {**audio, "audio_rate": 44100},
        {**audio, "audio_channels": 3},
        {**audio, "width": 640},
        {**video, "audio_rate": 16000},
        {**video, "height": None},
        {**video, "owner_setting_ref": None},
        {**video, "video_rate": {"num": 0, "den": 1}, "width": None, "height": None},
    ):
        _reject("Transform", raw)


def test_nullable_unknown_profile_metadata_remains_structural_and_unqualified():
    raw = _profile()
    raw.update(driver="ffmpeg_cli_fd_v1", implementation=None, runtime=None, executables=[])
    raw["protocol"].update(
        implementation=None,
        request_schema_sha256=None,
        response_schema_sha256=None,
        segments="one_segment_child",
    )
    _valid("DecoderProfile", _self_hash("DecoderProfile", raw, "profile_sha256"))
    _valid(
        "Runtime",
        _self_hash("Runtime", {**_runtime(), "distribution_lock": None}, "manifest_sha256"),
    )


def test_runtime_count_per_asset_total_and_executable_caps_are_conjunctive():
    _valid("AssetPin", _asset(size=268435456))
    _reject("AssetPin", _asset(size=268435457), "bound")
    _reject("AssetPin", _asset(size=0), "bound")
    _valid("Runtime", _runtime([_asset(i, size=268435456) for i in range(4)]))
    _reject(
        "Runtime",
        _runtime([_asset(i, size=268435456) for i in range(4)] + [_asset(4, size=1)]),
        "bound",
    )
    at_count = _runtime([_asset(i, size=1) for i in range(256)])
    assert len(_json(at_count)) <= 65536
    _valid("Runtime", at_count)
    _reject("Runtime", _runtime([_asset(i, size=1) for i in range(257)]), "bound")
    _valid("Runtime", _runtime([_asset(i, kind="python_exec") for i in range(3)]))
    _reject("Runtime", _runtime([_asset(i, kind="python_exec") for i in range(4)]), "bound")


def test_profile_format_and_executable_counts_do_not_increase_selected_hard_caps():
    profile = _profile()
    formats = [
        dict(kind="audio", container=c, codec=k, rate=r, channels=n)
        for c, k, r, n in itertools.product(
            ("raw", "wav"), ("pcm_s16le", "pcm_s24le", "pcm_s32le"), (16000, 24000, 48000), (1, 2)
        )
    ]
    for size in (16, 17):
        raw = _self_hash("DecoderProfile", {**profile, "formats": formats[:size]}, "profile_sha256")
        if size == 16:
            _valid("DecoderProfile", raw)
        else:
            _reject("DecoderProfile", raw, "bound")
    raw = _self_hash(
        "DecoderProfile",
        {**profile, "executables": [_asset(i, kind="python_exec") for i in range(4)]},
        "profile_sha256",
    )
    _reject("DecoderProfile", raw, "bound")


def test_probe_media_fields_and_track_indexes_are_not_interchangeable():
    audio = _probe_track()
    for bad in (
        {**audio, "width": 1},
        {**audio, "rate": None},
        {**audio, "codec": "h264"},
        {**audio, "timebase": {"num": 0, "den": 1}},
    ):
        _reject("ProbeTrack", bad)
    video = {
        **audio,
        "kind": "video",
        "codec": "h264",
        "rate": None,
        "channels": None,
        "width": 640,
        "height": 480,
        "timebase": {"num": 1, "den": 90000},
    }
    image = {**video, "kind": "image", "codec": "png", "duration": None}
    _valid("ProbeTrack", video)
    _valid("ProbeTrack", image)
    _reject("ProbeTrack", {**video, "height": None})
    _reject("ProbeResult", {**_probe(), "tracks": [audio, audio]})


@pytest.mark.parametrize(
    "name", ["ProbeResult", "CaptureOffer", "CaptureReady", "DecodeReady", "DecodeEnd", "InputEnd"]
)
def test_track_lists_keep_eight_distinct_entries_and_refuse_nine(name):
    raw = _vectors()[name]

    def item(index):
        if name == "ProbeResult":
            return _probe_track(index)
        if name == "CaptureOffer":
            return {**raw["tracks"][0], "client_track_key": f"track-{index}"}
        if name in ("CaptureReady", "DecodeReady"):
            return _track(index)
        return _track_end(index)

    at_limit = {**raw, "tracks": [item(i) for i in range(8)]}
    if name == "DecodeReady":
        at_limit["probe"]["tracks"] = [_probe_track(i) for i in range(8)]
    _valid(name, at_limit)
    _reject(name, {**raw, "tracks": [item(i) for i in range(9)]}, "bound")
    _reject(name, {**raw, "tracks": [item(0), item(0)]})


def test_capture_offer_track_keys_are_bounded_and_no_missing_nullable_field_is_defaulted():
    raw = _vectors()["CaptureOffer"]
    raw["tracks"][0]["client_track_key"] = "é" * 40
    _valid("CaptureOffer", raw)
    raw["tracks"][0]["client_track_key"] += "é"
    _reject("CaptureOffer", raw, "bound")
    for name in ("Runtime", "DecoderProtocol", "DecoderProfile", "TrackEnd"):
        raw = _vectors()[name]
        for field, value in raw.items():
            if value is None:
                _reject(name, {k: v for k, v in raw.items() if k != field})


def test_untimed_image_is_zero_origin_with_positive_unforced_timebase():
    raw = {
        **_track(),
        "kind": "image",
        "common_origin": {"num": 0, "den": 1},
        "clock_provenance": "untimed_image",
        "timebase": {"num": 1, "den": 1000},
        "format_sha256": _digest("Format", _image()),
    }
    _valid("Track", raw)
    _reject("Track", {**raw, "kind": "video"})
    _reject("Track", {**raw, "common_origin": {"num": -1, "den": 2}})
    _reject("Track", {**raw, "timebase": {"num": 0, "den": 1}})
    ready = _vectors()["DecodeReady"]
    ready["tracks"][0]["command_lane"] = True
    _reject("DecodeReady", ready)


def test_command_lane_is_an_exact_boolean_without_creating_owner_authority():
    for value in (0, 1, "false", "true", None):
        _reject("Track", {**_track(), "command_lane": value})
        offer = _vectors()["CaptureOffer"]
        offer["tracks"][0]["command_lane"] = value
        _reject("CaptureOffer", offer)


def test_segment_exact_rational_sixty_second_boundary_and_track_limit():
    raw = _segment()
    _valid("Segment", raw)  # 181/3 - 1/3 = exactly 60 seconds.
    _reject("Segment", {**raw, "end": {"num": 182, "den": 3}}, "bound")
    _reject(
        "Segment",
        {
            **raw,
            "start": {"num": 0, "den": 1},
            "end": {"num": 6000000000000001, "den": 100000000000000},
        },
        "bound",
    )
    _reject("Segment", {**raw, "end": raw["start"]})
    _reject("Segment", {**raw, "start": raw["end"], "end": raw["start"]})
    _reject("Segment", {**raw, "start": {"num": 2, "den": 6}})
    _reject("Segment", {**raw, "start": {"num": 1.0, "den": 3}})
    _valid("Segment", {**raw, "track_ids": [f"{i:032x}" for i in range(8)]})
    _reject("Segment", {**raw, "track_ids": [f"{i:032x}" for i in range(9)]}, "bound")
    _reject("Segment", {**raw, "track_ids": ["0" * 32, "0" * 32]})


@pytest.mark.parametrize("command", ["probe", "open", "next", "cancel"])
def test_decode_command_has_segment_only_for_open(command):
    raw = {
        **_vectors()["DecodeRequest"],
        "command": command,
        "segment": _segment() if command == "open" else None,
    }
    _valid("DecodeRequest", raw)
    _reject("DecodeRequest", {**raw, "segment": None if command == "open" else _segment()})
    _reject("DecodeRequest", {**raw, "seq": True})


@pytest.mark.parametrize("result", ["probe", "packet", "end", "cancel_ack", "error"])
def test_response_requires_exact_discriminated_payload_and_segment_identity(result):
    raw = _response(result)
    _valid("DecodeResponse", raw)
    for payload in ("probe", "packet", "end", "error"):
        if payload == result:
            _reject("DecodeResponse", {**raw, payload: None})
        else:
            _reject("DecodeResponse", {**raw, payload: _response(payload)[payload]})
    if result == "probe":
        _reject("DecodeResponse", {**raw, "segment_id": "d" * 32})
    if result in ("packet", "end"):
        _reject("DecodeResponse", {**raw, "segment_id": None})
    if result == "end":
        _reject("DecodeResponse", {**raw, "segment_id": "e" * 32})


def test_segment_end_is_not_source_eof_or_downstream_completion():
    boundary = _end("segment_end")
    _valid("DecodeEnd", boundary)
    _reject("DecodeEnd", {**boundary, "kind": "source_eof"})
    mixed = _end()
    mixed["tracks"].append(_track_end(1, "segment_boundary"))
    _reject("DecodeEnd", mixed)
    _valid("DecodeEnd", _end())
    for invented in ("complete", "cancelled", "reaped"):
        _reject("DecodeEnd", {**_end(), "kind": invented})
    # Ref lookup, finite sealed-source identity, original all-track observation,
    # runtime child retirement and publication are deliberately native checks.


def test_input_end_variants_require_all_eof_and_their_exact_original_reference_fields():
    finite = _vectors()["InputEnd"]
    capture = {
        **finite,
        "kind": "capture_closed",
        "seal": None,
        "decoder_end": None,
        "capture_ready": copy.deepcopy(_OWNER),
    }
    _valid("InputEnd", finite)
    _valid("InputEnd", capture)
    for raw in (finite, capture):
        _reject("InputEnd", {**raw, "tracks": [_track_end(state="segment_boundary")]})
    _reject("InputEnd", {**finite, "seal": None})
    _reject("InputEnd", {**finite, "decoder_end": None})
    _reject("InputEnd", {**finite, "capture_ready": copy.deepcopy(_OWNER)})
    _reject("InputEnd", {**capture, "capture_ready": None})
    _reject("InputEnd", {**capture, "decoder_end": copy.deepcopy(_OWNER)})


def test_decode_budget_preserves_unknown_measurements_without_minting_replacement_budget():
    raw = _vectors()["DecodeBudget"]
    for state in ("running", "ended", "unknown"):
        _valid(
            "DecodeBudget",
            {
                **raw,
                "state": state,
                "previous": copy.deepcopy(_OWNER),
                "cpu_baseline_ms": _SAFE,
                "wall_baseline_ms": _SAFE,
            },
        )
    for field in ("cpu_baseline_ms", "wall_baseline_ms", "cpu_spent_ms", "wall_spent_ms"):
        _reject("DecodeBudget", {**raw, field: -1}, "bound")
        _reject("DecodeBudget", {**raw, field: _SAFE + 1}, "bound")
    _reject("DecodeBudget", {**raw, "state": "reaped"})


def test_protocol_describes_original_fd_owner_without_enabling_unknown_modes():
    raw = _protocol()
    _valid(
        "DecoderProtocol",
        {
            **raw,
            "segments": "one_segment_child",
            "implementation": None,
            "request_schema_sha256": None,
            "response_schema_sha256": None,
        },
    )
    for field, changed in (
        ("wire", "url"),
        ("peer", "pid_only"),
        ("pts", "frame_index"),
        ("cleanup", "response_close"),
        ("segments", "automatic_retry"),
    ):
        _reject("DecoderProtocol", {**raw, field: changed})


def test_typed_profile_detaches_input_and_revalidates_mutated_lists_and_extras():
    raw = _profile()
    value = _valid("DecoderProfile", raw)
    expected = _json(raw)
    raw["formats"].append(_audio())
    assert media.media_bytes(value) == expected
    for changed in (
        value.model_copy(update={"formats": tuple(value.formats)}),
        value.model_copy(update={"formats": [object()]}),
        value.model_copy(update={"owner_authorized": True}),
        value.model_copy(
            update={
                "runtime": value.runtime.model_copy(update={"assets": tuple(value.runtime.assets)})
            }
        ),
    ):
        with pytest.raises(ValueError) as failure:
            media.media_bytes(changed)
        assert failure.value.code == "bad_packet"
    value.formats.append(media.media_json(_json(_audio()), media.MediaAudioFormat))
    with pytest.raises(ValueError):
        media.media_bytes(
            value
        )  # Actual modified graph no longer matches its original profile hash.


def test_hostile_list_subclasses_are_rejected_without_invoking_their_iterators():
    touches = []

    class HostileList(list):
        def __iter__(self):
            touches.append("iter")
            raise AssertionError("untrusted iterator must not run")

    value = _valid("DecoderProfile", _profile())
    changed = value.model_copy(update={"formats": HostileList(value.formats)})
    with pytest.raises(ValueError) as failure:
        media.media_bytes(changed)
    assert failure.value.code == "bad_packet"
    assert touches == []


def test_global_node_preflight_precedes_unknown_extra_and_child_inspection():
    touches = []

    class HostileChild:
        def __getattribute__(self, name):
            touches.append(name)
            raise AssertionError("unknown child must not be inspected")

    value = _valid("DecoderProfile", _profile())
    # A small synthetic in-memory graph exceeds 8192 nodes before unknown-shape
    # validation. The unknown first child cannot short-circuit that global cap.
    changed = value.model_copy(update={"unknown": [HostileChild()] + [0] * 8192})
    with pytest.raises(ValueError) as failure:
        media.media_bytes(changed)
    assert failure.value.code == "bound"
    assert touches == []
    raw = b'{"unknown":[' + b",".join([b"0"] * 8192) + b"]}"
    with pytest.raises(ValueError) as failure:
        media.media_json(raw, media.MediaDecoderProfile)
    assert failure.value.code == "bound"
