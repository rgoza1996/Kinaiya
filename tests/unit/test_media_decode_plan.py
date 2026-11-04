"""Source-aware native planning regressions; no decoder/model execution.

Existing public synthetic DTO constructors supply metadata only. These checks
do not assert that their invented asset pins or clock observations are genuine.
"""

import copy
import hashlib

import pytest
import test_media_decoder_contracts as fixtures

from packages.contracts import media
from packages.resources.media_decode_plan import (
    MediaDecodePlanError,
    ProbeClock,
    TrackSelection,
    build_decode_plan,
)


def _typed(kind, value):
    return media.media_json(fixtures._json(value), kind)


def _inputs(*, profile=None, probe=None, tracks=None, clocks=None, selections=None):
    profile = copy.deepcopy(profile or fixtures._profile())
    probe = copy.deepcopy(probe or fixtures._probe())
    tracks = copy.deepcopy(tracks or [fixtures._track()])
    profile_hash = profile["profile_sha256"]
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
    ready = {
        "v": 1,
        "source": copy.deepcopy(fixtures._SOURCE),
        "seal": copy.deepcopy(fixtures._OWNER),
        "probe": probe,
        "decoder_profile_sha256": profile_hash,
        "tracks": tracks,
    }
    return {
        "source_ref": _typed(media.MediaNativeRef, fixtures._SOURCE),
        "seal_ref": _typed(media.MediaNativeRef, fixtures._OWNER),
        "admission": _typed(media.MediaSourceAdmission, admission),
        "seal": _typed(media.MediaSeal, seal),
        "profile": _typed(media.MediaDecoderProfile, profile),
        "probe": _typed(media.MediaProbeResult, probe),
        "ready": _typed(media.MediaDecodeReady, ready),
        "selections": selections
        or (
            TrackSelection(0, tracks[0]["track_id"], _typed(media.MediaFormat, fixtures._audio())),
        ),
        "clocks": clocks or (ProbeClock(0, -8000, 1, 16000, "container"),),
    }


def _reprofile(profile):
    return fixtures._self_hash("DecoderProfile", profile, "profile_sha256")


def _two_audio_inputs():
    profile = fixtures._profile()
    other_format = {**fixtures._audio(), "rate": 24000}
    profile["formats"].append(other_format)
    profile = _reprofile(profile)
    probe = fixtures._probe()
    probe["tracks"] = [
        {**fixtures._probe_track(), "index": 7},
        {
            **fixtures._probe_track(),
            "index": 3,
            "rate": 24000,
            "timebase": {"num": 1, "den": 24000},
        },
    ]
    first = fixtures._track(1)
    second = {
        **fixtures._track(2),
        "format_sha256": fixtures._digest("Format", other_format),
        "timebase": {"num": 1, "den": 24000},
    }
    return _inputs(
        profile=profile,
        probe=probe,
        tracks=[second, first],
        selections=(
            TrackSelection(3, second["track_id"], _typed(media.MediaFormat, other_format)),
            TrackSelection(7, first["track_id"], _typed(media.MediaFormat, fixtures._audio())),
        ),
        clocks=(
            ProbeClock(3, 6000, 1, 24000, "container"),
            ProbeClock(7, -8000, 1, 16000, "container"),
        ),
    )


def test_explicit_indices_bind_same_kind_tracks_independent_of_array_order():
    args = _two_audio_inputs()
    plan = build_decode_plan(**args)
    assert [item.probe_index for item in plan.tracks] == [7, 3]
    assert [
        media.media_json(item.track_pin, media.MediaTrack).track_id for item in plan.tracks
    ] == [fixtures._track(1)["track_id"], fixtures._track(2)["track_id"]]
    assert [media.media_json(item.format_pin, media.MediaFormat).rate for item in plan.tracks] == [
        16000,
        24000,
    ]
    assert [item.first_pts for item in plan.tracks] == [-8000, 6000]
    assert plan.common_origin == (-1, 2)
    assert build_decode_plan(**args) == plan


def test_plan_does_not_retain_mutable_models_or_selection_objects():
    args = _inputs()
    plan = build_decode_plan(**args)
    original = plan.profile_pin
    args["profile"].formats[0].__dict__["rate"] = 24000
    args["ready"].tracks[0].__dict__["track_id"] = "f" * 32
    assert plan.profile_pin == original
    assert media.media_json(plan.profile_pin, media.MediaDecoderProfile).formats[0].rate == 16000
    assert media.media_json(plan.tracks[0].track_pin, media.MediaTrack).track_id == "0" * 32


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("seal", "source_sha256", "f" * 64),
        ("seal", "bytes", 32045),
        ("seal", "reservation_id", "f" * 32),
        ("ready", "decoder_profile_sha256", "f" * 64),
        ("source_ref", "receipt_id", "f" * 32),
        ("seal_ref", "receipt_id", "f" * 32),
    ],
)
def test_changed_native_source_seal_or_profile_binding_is_refused(target, field, value):
    args = _inputs()
    args[target] = args[target].model_copy(update={field: value})
    with pytest.raises(MediaDecodePlanError, match="Invalid native decoder plan"):
        build_decode_plan(**args)


@pytest.mark.parametrize(
    "clocks",
    [
        (ProbeClock(9, -8000, 1, 16000, "container"),),
        (ProbeClock(0, -8000, 1, 16000, "container"),) * 2,
        (ProbeClock(0, None, 1, 16000, "container"),),
        (ProbeClock(0, True, 1, 16000, "container"),),
        (ProbeClock(0, -8000, 1, 24000, "container"),),
        (ProbeClock(0, -8000, 1, 16000, "provider_reported"),),
    ],
)
def test_missing_duplicate_wrong_or_invented_original_clock_is_refused(clocks):
    args = _inputs()
    args["clocks"] = clocks
    with pytest.raises(MediaDecodePlanError):
        build_decode_plan(**args)


def test_unhashable_native_track_selection_is_bounded_refusal_not_typeerror():
    args = _inputs()
    args["selections"] = (TrackSelection(0, [], args["selections"][0].format),)
    with pytest.raises(MediaDecodePlanError) as failure:
        build_decode_plan(**args)
    assert failure.value.code == "bad_packet"


def test_format_cannot_be_inferred_or_silently_resampled_for_same_kind_track():
    args = _two_audio_inputs()
    first, second = args["selections"]
    args["selections"] = (
        TrackSelection(first.probe_index, second.track_id, first.format),
        TrackSelection(second.probe_index, first.track_id, second.format),
    )
    with pytest.raises(MediaDecodePlanError) as failure:
        build_decode_plan(**args)
    assert failure.value.code == "conflict"


def _image_inputs(*, video=False, width=640, height=480, scale=False):
    profile = fixtures._profile()
    format_ = {**fixtures._image(), "kind": "video_frame" if video else "image"}
    profile["formats"] = [format_]
    if scale:
        profile["transform"] = {
            **fixtures._transform(),
            "kind": "video_sample_scale",
            "width": 320,
            "height": 240,
            "owner_setting_ref": "synthetic-owner-setting",
        }
    profile = _reprofile(profile)
    probe = fixtures._probe()
    probe["container"] = "mp4" if video else "png"
    probe["tracks"] = [
        {
            **fixtures._probe_track(),
            "kind": "video" if video else "image",
            "codec": "h264" if video else "png",
            "rate": None,
            "channels": None,
            "width": width,
            "height": height,
            "timebase": {"num": 1, "den": 30},
        }
    ]
    track = {
        **fixtures._track(),
        "kind": "video" if video else "image",
        "format_sha256": fixtures._digest("Format", format_),
        "timebase": {"num": 1, "den": 30},
        "common_origin": {"num": 0, "den": 1},
        "clock_provenance": "container" if video else "untimed_image",
    }
    return _inputs(
        profile=profile,
        probe=probe,
        tracks=[track],
        selections=(TrackSelection(0, track["track_id"], _typed(media.MediaFormat, format_)),),
        clocks=(ProbeClock(0, 0, 1, 30, track["clock_provenance"]),),
    )


def test_untimed_image_keeps_nonunit_declared_timebase_and_zero_origin():
    plan = build_decode_plan(**_image_inputs())
    assert plan.common_origin == (0, 1)
    assert (plan.tracks[0].timebase_num, plan.tracks[0].timebase_den) == (1, 30)
    assert (plan.tracks[0].width, plan.tracks[0].height) == (640, 480)


def test_explicit_scale_preserves_original_clock_and_selects_declared_geometry():
    plan = build_decode_plan(**_image_inputs(video=True, scale=True))
    assert (plan.tracks[0].width, plan.tracks[0].height) == (320, 240)
    assert (plan.tracks[0].timebase_num, plan.tracks[0].timebase_den) == (1, 30)


def test_scale_does_not_waive_original_decode_dimensions():
    with pytest.raises(MediaDecodePlanError) as failure:
        build_decode_plan(**_image_inputs(video=True, width=8192, scale=True))
    assert failure.value.code == "bound"


def test_missing_runtime_metadata_remains_unavailable():
    profile = fixtures._profile()
    profile["runtime"] = None
    with pytest.raises(MediaDecodePlanError) as failure:
        build_decode_plan(**_inputs(profile=_reprofile(profile)))
    assert failure.value.code == "unavailable"


def test_policy_hash_and_profile_track_limit_are_enforced():
    args = _inputs()
    args["admission"].reservation.__dict__["policy_sha256"] = "f" * 64
    with pytest.raises(MediaDecodePlanError) as failure:
        build_decode_plan(**args)
    assert failure.value.code == "conflict"
    args = _two_audio_inputs()
    raw = copy.deepcopy(args["profile"].model_dump())
    raw["limits"]["decode"]["track_limit"] = 1
    profile = _reprofile(raw)
    args = _inputs(
        profile=profile,
        probe=args["probe"].model_dump(),
        tracks=[track.model_dump() for track in args["ready"].tracks],
        selections=args["selections"],
        clocks=args["clocks"],
    )
    with pytest.raises(MediaDecodePlanError) as failure:
        build_decode_plan(**args)
    assert failure.value.code == "bound"


def test_unknown_typed_metadata_cannot_disappear_in_planning():
    args = _inputs()
    args["ready"].tracks[0].__dict__["owner_authorized"] = True
    with pytest.raises(MediaDecodePlanError) as failure:
        build_decode_plan(**args)
    assert failure.value.code == "bad_packet"


def test_plan_digest_includes_signed_original_clock_even_if_origin_is_same():
    args = _two_audio_inputs()
    first = build_decode_plan(**args)
    old = args["clocks"][0]
    args["clocks"] = (ProbeClock(old.probe_index, 6001, 1, 24000, "container"), args["clocks"][1])
    second = build_decode_plan(**args)
    assert first.common_origin == second.common_origin == (-1, 2)
    assert first.sha256 != second.sha256
    assert len(first.sha256) == hashlib.sha256().digest_size * 2
