"""Additive interface-first decode-plan boundary oracles.

Only the public planning document, root API clarifications, frozen media DTOs
and earlier independent fixture constructors were read. Plan implementation and
root test bodies were not read. Metadata acceptance proves no native authority,
original clock observation, executable decoder or physical cleanup.
"""

import copy
import hashlib
import json
from fractions import Fraction

import pytest

from packages.contracts import media
from packages.resources.media_decode_plan import (
    MediaDecodePlanError,
    ProbeClock,
    TrackSelection,
    build_decode_plan,
)

_MAX = 9_007_199_254_740_991
_ID = "a" * 32
_HASH = "b" * 64
_CODE_SHA = "c" * 64
_SOURCE = {"backend_id": "0" * 32, "receipt_id": "1" * 32, "sha256": "2" * 64}
_SEAL_REF = {"backend_id": "0" * 32, "receipt_id": "3" * 32, "sha256": "4" * 64}
_SESSION = {"session_id": "synthetic-session", "thread_id": "synthetic-thread", "epoch": 1}
_FENCE = {
    "session": _SESSION,
    "source_revision": 1,
    "generation": 1,
    "lease_generation": 1,
}


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


def _dto(name, value):
    return media.media_json(_json(value), getattr(media, "Media" + name))


def _case(specs=((0, 1, 16000),)):
    """Fresh strict records; specs are (signed first PTS, timebase num, den)."""
    absolute = [Fraction(pts * num, den) for pts, num, den in specs]
    origin = min(absolute)
    assert abs(origin.numerator) <= _MAX and origin.denominator <= _MAX
    common = {"num": origin.numerator, "den": origin.denominator}
    profile = _profile()
    probe_tracks, ready_tracks = [], []
    clocks, selections = [], []
    for index, (pts, num, den) in enumerate(specs):
        timebase = {"num": num, "den": den}
        track_id = f"{index + 16:032x}"
        # Multiple AAC tracks in a finite MP4 are legitimate metadata. These
        # unusual rational clocks do not assert that any actual file was probed.
        probe_tracks.append(
            {
                "index": index,
                "kind": "audio",
                "codec": "aac",
                "rate": 16000,
                "channels": 1,
                "width": None,
                "height": None,
                "timebase": timebase,
                "duration": {"num": 1, "den": 1},
            }
        )
        ready_tracks.append(
            {
                "track_id": track_id,
                "source_id": _ID,
                "kind": "audio",
                "format_sha256": _digest("Format", _audio()),
                "timebase": timebase,
                "common_origin": common,
                "clock_provenance": "container",
                "command_lane": False,
            }
        )
        clocks.append(
            ProbeClock(
                probe_index=index,
                pts=pts,
                timebase_num=num,
                timebase_den=den,
                provenance="container",
            )
        )
        selections.append(
            TrackSelection(
                probe_index=index, track_id=track_id, format=_dto("AudioFormat", _audio())
            )
        )
    probe = {
        "v": 1,
        "source_sha256": _HASH,
        "input_bytes": 32044,
        "container": "mp4",
        "tracks": probe_tracks,
    }
    reservation = {
        "reservation_id": "5" * 32,
        "owner_spool_bytes": 32044,
        "global_spool_bytes": 32044,
        "metadata_bytes": 4096,
        "child_slots": 1,
        "policy_sha256": _digest("Limits", _limits()),
    }
    admission = {
        "v": 1,
        "admission_id": "6" * 32,
        "source_id": _ID,
        "source_equivalence_id": None,
        "source_revision": 1,
        "session": copy.deepcopy(_SESSION),
        "kind": "recording",
        "purpose": "analyze",
        "anchor": {"backend_id": "0" * 32, "receipt_id": "7" * 32, "sha256": "8" * 64},
        "replaces_source": None,
        "capture_instance": None,
        "client_instance": "9" * 32,
        "scope": "local_only",
        "reservation": reservation,
        "decoder_profile_sha256": profile["profile_sha256"],
        "fence": copy.deepcopy(_FENCE),
        "lease_expires_at_ms": 30000,
        "raw_retain_until_ms": None,
    }
    seal = {
        "v": 1,
        "source": copy.deepcopy(_SOURCE),
        "source_equivalence_id": "e" * 32,
        "fence": copy.deepcopy(_FENCE),
        "reservation_id": reservation["reservation_id"],
        "bytes": 32044,
        "source_sha256": _HASH,
        "decoder_profile_sha256": profile["profile_sha256"],
        "state": "sealed",
    }
    ready = {
        "v": 1,
        "source": copy.deepcopy(_SOURCE),
        "seal": copy.deepcopy(_SEAL_REF),
        "probe": copy.deepcopy(probe),
        "decoder_profile_sha256": profile["profile_sha256"],
        "tracks": ready_tracks,
    }
    return {
        "source_ref": _dto("NativeRef", _SOURCE),
        "seal_ref": _dto("NativeRef", _SEAL_REF),
        "admission": _dto("SourceAdmission", admission),
        "seal": _dto("Seal", seal),
        "profile": _dto("DecoderProfile", profile),
        "probe": _dto("ProbeResult", probe),
        "ready": _dto("DecodeReady", ready),
        "selections": tuple(selections),
        "clocks": tuple(clocks),
    }


def _refused(arguments, code):
    with pytest.raises(MediaDecodePlanError) as failure:
        build_decode_plan(**arguments)
    assert isinstance(failure.value, ValueError)
    assert failure.value.code == code
    assert str(failure.value) == "Invalid native decoder plan"


@pytest.mark.parametrize(
    "family,missing",
    [
        ("clock", None),
        ("clock", "probe_index"),
        ("clock", "pts"),
        ("clock", "timebase_num"),
        ("clock", "timebase_den"),
        ("clock", "provenance"),
        ("selection", None),
        ("selection", "probe_index"),
        ("selection", "track_id"),
        ("selection", "format"),
    ],
)
def test_exact_dataclass_with_absent_slots_has_fixed_bad_packet_refusal(family, missing):
    arguments = _case()
    key, kind = ("clocks", ProbeClock) if family == "clock" else ("selections", TrackSelection)
    original = arguments[key][0]
    if missing is None:
        malformed = object.__new__(kind)
    else:
        malformed = original
        object.__delattr__(malformed, missing)
    assert type(malformed) is kind
    arguments[key] = (malformed,)
    _refused(arguments, "bad_packet")


@pytest.mark.parametrize(
    "specs,expected_origin,expected_positions",
    [
        (((_MAX, 1, 1), ((_MAX + 1) // 2, 2, 1)), (_MAX, 1), ((0, 1), (1, 1))),
        (((_MAX - 1, _MAX, _MAX - 1),), (_MAX, 1), ((0, 1),)),
        (((-(_MAX - 1), _MAX, _MAX - 1),), (-_MAX, 1), ((0, 1),)),
        (
            ((-(_MAX - 1), 1, _MAX), (1, 1, _MAX)),
            (-(_MAX - 1), _MAX),
            ((0, 1), (1, 1)),
        ),
        (((-1, 1, _MAX), (1, 1, _MAX)), (-1, _MAX), ((0, 1), (2, _MAX))),
        (
            ((_MAX, 1, 1), (_MAX - 1, _MAX, _MAX - 1)),
            (_MAX, 1),
            ((0, 1), (0, 1)),
        ),
    ],
    ids=[
        "absolute-product-over-max-but-rebased-one",
        "large-positive-product-reduces-to-max",
        "large-negative-product-reduces-to-minus-max",
        "large-rebase-cross-products-reduce-to-one",
        "large-shared-denominators-reduce-in-bounds",
        "equal-large-original-times-share-exact-origin",
    ],
)
def test_large_intermediates_are_accepted_when_represented_origin_and_positions_fit(
    specs, expected_origin, expected_positions
):
    # Independent integer/rational arithmetic establishes the fixed vectors.
    absolute = [Fraction(pts * num, den) for pts, num, den in specs]
    origin = Fraction(*expected_origin)
    assert min(absolute) == origin
    rebased = tuple(value - origin for value in absolute)
    assert tuple((value.numerator, value.denominator) for value in rebased) == expected_positions
    assert all(0 <= value.numerator <= _MAX and value.denominator <= _MAX for value in rebased)
    arguments = _case(specs)
    plan = build_decode_plan(**arguments)
    assert plan.common_origin == expected_origin
    assert tuple(track.probe_index for track in plan.tracks) == tuple(range(len(specs)))
    for track, (pts, num, den) in zip(plan.tracks, specs, strict=True):
        assert (track.first_pts, track.timebase_num, track.timebase_den) == (pts, num, den)
        assert track.clock_provenance == "container"
        assert track.width is None and track.height is None
        native_track = media.media_json(track.track_pin, media.MediaTrack)
        assert (native_track.common_origin.num, native_track.common_origin.den) == expected_origin
        assert native_track.timebase.num == num and native_track.timebase.den == den


@pytest.mark.parametrize(
    "specs,overflow_component",
    [
        (((-_MAX, 1, 1), (_MAX, 1, 1)), "numerator"),
        (((1, 1, _MAX), (1, 1, _MAX - 2)), "denominator"),
        (((0, 1, 1), (_MAX - 2, _MAX, _MAX - 1)), "numerator"),
    ],
    ids=[
        "opposite-signed-extremes-rebase-to-two-max",
        "small-positive-times-have-unrepresentable-rebased-denominator",
        "sub-max-value-still-has-unrepresentable-reduced-numerator",
    ],
)
def test_rebased_reduced_components_overflow_even_with_representable_common_origin(
    specs, overflow_component
):
    absolute = [Fraction(pts * num, den) for pts, num, den in specs]
    origin = min(absolute)
    assert abs(origin.numerator) <= _MAX and origin.denominator <= _MAX
    positions = [value - origin for value in absolute]
    assert any(getattr(value, overflow_component) > _MAX for value in positions)
    _refused(_case(specs), "bound")
