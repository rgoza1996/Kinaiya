"""Exact initial probe-to-native-track bindings, without launch authority.

The native owner supplies already resolved admission/seal/ready records and
original decoder clock observations. This module checks their correspondence;
it does not qualify assets, authenticate a peer, grant a budget or start decode.
Returned byte pins and tuples do not retain mutable caller-owned models.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction

from packages.contracts import media


class MediaDecodePlanError(ValueError):
    def __init__(self, code="bad_packet"):
        self.code = (
            code
            if type(code) is str and code in {"bad_packet", "bound", "conflict", "unavailable"}
            else "unavailable"
        )
        super().__init__("Invalid native decoder plan")


def _refuse(code="bad_packet"):
    raise MediaDecodePlanError(code)


@dataclass(frozen=True, slots=True)
class ProbeClock:
    probe_index: int
    pts: int
    timebase_num: int
    timebase_den: int
    provenance: str


@dataclass(frozen=True, slots=True)
class TrackSelection:
    probe_index: int
    track_id: str
    format: media.MediaFormat


@dataclass(frozen=True, slots=True)
class DecodeTrackPlan:
    probe_index: int
    track_pin: bytes
    format_pin: bytes
    first_pts: int
    timebase_num: int
    timebase_den: int
    clock_provenance: str
    width: int | None
    height: int | None


@dataclass(frozen=True, slots=True)
class DecodePlan:
    source_ref_pin: bytes
    seal_ref_pin: bytes
    admission_pin: bytes
    seal_pin: bytes
    profile_pin: bytes
    probe_pin: bytes
    ready_pin: bytes
    common_origin: tuple[int, int]
    tracks: tuple[DecodeTrackPlan, ...]
    sha256: str


def _pin(value, kind):
    if kind is not media.MediaFormat and type(value) is not kind:
        _refuse()
    raw = media.media_bytes(value)
    return raw, media.media_json(raw, kind)


def _integer(value, *, signed=False, positive=False):
    if type(value) is not int:
        _refuse()
    low = -media.MAX_MEDIA_INTEGER if signed else int(positive)
    if not low <= value <= media.MAX_MEDIA_INTEGER:
        _refuse("bound")
    return value


def _sequence(value, kind):
    if type(value) is not tuple:
        _refuse()
    if not 1 <= len(value) <= 8:
        _refuse("bound")
    if any(type(item) is not kind for item in value):
        _refuse()
    result = {}
    for item in value:
        index = _integer(item.probe_index)
        if index in result:
            _refuse("conflict")
        result[index] = item
    return result


def _selected_geometry(probe, format_, transform):
    if probe.kind == "audio":
        if type(format_) is not media.MediaAudioFormat:
            _refuse("conflict")
        rate, channels = probe.rate, probe.channels
        if transform.kind == "audio_resample_downmix":
            rate, channels = transform.audio_rate, transform.audio_channels
        if (format_.rate, format_.channels) != (rate, channels):
            _refuse("conflict")
        return None, None
    if type(format_) is not media.MediaImageFormat:
        _refuse("conflict")
    if format_.kind != ("image" if probe.kind == "image" else "video_frame"):
        _refuse("conflict")
    if transform.kind == "video_sample_scale" and transform.width is not None:
        return transform.width, transform.height
    return probe.width, probe.height


def _clock(item, probe):
    pts = _integer(item.pts, signed=True)
    num = _integer(item.timebase_num, positive=True)
    den = _integer(item.timebase_den, positive=True)
    if (num, den) != (probe.timebase.num, probe.timebase.den):
        _refuse("conflict")
    expected = "untimed_image" if probe.kind == "image" else "container"
    if type(item.provenance) is not str or item.provenance != expected:
        _refuse("conflict")
    if expected == "untimed_image" and pts != 0:
        _refuse("conflict")
    return pts, num, den, expected


def build_decode_plan(
    *,
    source_ref: media.MediaNativeRef,
    seal_ref: media.MediaNativeRef,
    admission: media.MediaSourceAdmission,
    seal: media.MediaSeal,
    profile: media.MediaDecoderProfile,
    probe: media.MediaProbeResult,
    ready: media.MediaDecodeReady,
    selections: tuple[TrackSelection, ...],
    clocks: tuple[ProbeClock, ...],
) -> DecodePlan:
    """Bind every original probe index explicitly, including same-kind tracks.

    Only initial finite-source planning is performed. Native lookup/currentness,
    qualification, charged probe/segment budgets and continuation counters remain
    separate required guards. A structurally valid plan grants none of them.
    """
    try:
        pins = []
        values = []
        for value, kind in (
            (source_ref, media.MediaNativeRef),
            (seal_ref, media.MediaNativeRef),
            (admission, media.MediaSourceAdmission),
            (seal, media.MediaSeal),
            (profile, media.MediaDecoderProfile),
            (probe, media.MediaProbeResult),
            (ready, media.MediaDecodeReady),
        ):
            raw, fresh = _pin(value, kind)
            pins.append(raw)
            values.append(fresh)
        source_ref, seal_ref, admission, seal, profile, probe, ready = values
        if admission.kind != "recording" or admission.capture_instance is not None:
            _refuse("conflict")
        if (
            ready.source != source_ref
            or seal.source != source_ref
            or ready.seal != seal_ref
            or seal.fence != admission.fence
            or seal.reservation_id != admission.reservation.reservation_id
            or (
                admission.source_equivalence_id is not None
                and admission.source_equivalence_id != seal.source_equivalence_id
            )
            or probe.source_sha256 != seal.source_sha256
            or probe.input_bytes != seal.bytes
            or ready.probe != probe
            or admission.reservation.policy_sha256 != media.media_digest("Limits", profile.limits)
            or any(
                value != profile.profile_sha256
                for value in (
                    admission.decoder_profile_sha256,
                    seal.decoder_profile_sha256,
                    ready.decoder_profile_sha256,
                )
            )
        ):
            _refuse("conflict")
        if seal.bytes > min(
            admission.reservation.owner_spool_bytes,
            admission.reservation.global_spool_bytes,
            profile.limits.source.file_bytes,
        ):
            _refuse("bound")
        if (
            profile.driver != "pyav17_timed_fd_v1"
            or profile.runtime is None
            or profile.implementation is None
            or profile.protocol.implementation is None
            or profile.protocol.request_schema_sha256 is None
            or profile.protocol.response_schema_sha256 is None
            or not profile.formats
        ):
            _refuse("unavailable")
        choices = _sequence(selections, TrackSelection)
        clock_items = _sequence(clocks, ProbeClock)
        indices = {track.index for track in probe.tracks}
        if choices.keys() != indices or clock_items.keys() != indices:
            _refuse("conflict")
        if len(indices) > profile.limits.decode.track_limit:
            _refuse("bound")
        if any(type(value.track_id) is not str for value in selections):
            _refuse()
        if len(ready.tracks) != len(indices) or len({v.track_id for v in selections}) != len(
            indices
        ):
            _refuse("conflict")
        by_id = {track.track_id: track for track in ready.tracks}
        formats = {media.media_bytes(item) for item in profile.formats}
        captured = []
        timed_origins = []
        for original in probe.tracks:
            if original.duration is not None and (
                Fraction(original.duration.num * 1000, original.duration.den)
                > profile.limits.source.file_duration_ms
            ):
                _refuse("bound")
            if original.width is not None and (
                original.width > profile.limits.decode.max_edge
                or original.height > profile.limits.decode.max_edge
                or original.width * original.height > profile.limits.decode.pixels_per_frame
            ):
                _refuse("bound")
            choice = choices[original.index]
            if type(choice.track_id) is not str or choice.track_id not in by_id:
                _refuse("conflict")
            track = by_id[choice.track_id]
            format_pin, format_ = _pin(choice.format, media.MediaFormat)
            if format_pin not in formats:
                _refuse("conflict")
            if (
                track.source_id != admission.source_id
                or track.kind != original.kind
                or track.timebase != original.timebase
                or track.command_lane
                or track.format_sha256 != media.media_digest("Format", format_)
            ):
                _refuse("conflict")
            clock = _clock(clock_items[original.index], original)
            if track.clock_provenance != clock[3]:
                _refuse("conflict")
            if clock[3] != "untimed_image":
                timed_origins.append(Fraction(clock[0] * clock[1], clock[2]))
            width, height = _selected_geometry(original, format_, profile.transform)
            if width is not None and (
                width > profile.limits.decode.max_edge
                or height > profile.limits.decode.max_edge
                or width * height > profile.limits.decode.pixels_per_frame
            ):
                _refuse("bound")
            captured.append(
                DecodeTrackPlan(
                    original.index,
                    media.media_bytes(track),
                    format_pin,
                    *clock,
                    width,
                    height,
                )
            )
        origin = min(timed_origins) if timed_origins else Fraction(0)
        if max(abs(origin.numerator), origin.denominator) > media.MAX_MEDIA_INTEGER:
            _refuse("bound")
        pair = (origin.numerator, origin.denominator)
        for track in ready.tracks:
            expected = (0, 1) if track.clock_provenance == "untimed_image" else pair
            if (track.common_origin.num, track.common_origin.den) != expected:
                _refuse("conflict")
        digest = hashlib.sha256(b"kinaiya.media.decode-plan.v1\0")
        for raw in pins:
            digest.update(len(raw).to_bytes(4, "big"))
            digest.update(raw)
        tail = json.dumps(
            {
                "origin": pair,
                "tracks": [
                    [
                        v.probe_index,
                        hashlib.sha256(v.track_pin).hexdigest(),
                        hashlib.sha256(v.format_pin).hexdigest(),
                        v.first_pts,
                        v.timebase_num,
                        v.timebase_den,
                        v.clock_provenance,
                        v.width,
                        v.height,
                    ]
                    for v in captured
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(tail)
        return DecodePlan(*pins, pair, tuple(captured), digest.hexdigest())
    except media.MediaContractError as error:
        raise MediaDecodePlanError(error.code) from None
