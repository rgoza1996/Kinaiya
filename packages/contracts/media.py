"""Closed media packets. References describe provenance, never grant authority.

Source intake, decoder descriptors and bounded protocol records do not establish
runtime availability. Native owners resolve references, permissions, policy,
currentness, original files/processes and cleanup.
"""

import base64
import binascii
import hashlib
import json
import re
import types
from math import gcd
from types import MappingProxyType
from typing import Annotated, Generic, Literal, Self, TypeVar, Union, get_args, get_origin

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

MAX_MEDIA_INTEGER = 9_007_199_254_740_991
MAX_MEDIA_CONTROL_BYTES = 65_536
MAX_MEDIA_BODY_BYTES = 16_777_216
MAX_MEDIA_DEPTH = 24
MAX_MEDIA_NODES = 8_192
MAX_MEDIA_ADMISSION_BYTES = 262_144
MAX_MEDIA_ADMISSION_DEPTH = 32
MAX_MEDIA_ADMISSION_NODES = 32_768
_ERROR = "Invalid media contract"


class MediaContractError(ValueError):
    """Fixed boundary classification; never includes untrusted input text."""

    def __init__(self, code: Literal["bad_packet", "bound"] = "bad_packet") -> None:
        if code not in ("bad_packet", "bound"):
            code = "bad_packet"
        self.code = code
        super().__init__(_ERROR)


def _bad() -> None:
    raise MediaContractError()


def _bound() -> None:
    raise MediaContractError("bound")


def _version_values(annotation):
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        if args:
            annotation = args[0]
            origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)
    return ()


def _check_version(value, annotation) -> None:
    allowed = _version_values(annotation)
    if type(value) is not int or value not in allowed:
        _bad()


def _text(limit: int):
    def check(value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            _bad()
        if size > limit:
            _bound()
        return value

    return Annotated[str, Field(min_length=1, max_length=limit), AfterValidator(check)]


_Id = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
_Hash = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
_Git = Annotated[str, Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")]
_N = Annotated[int, Field(ge=0, le=MAX_MEDIA_INTEGER)]
_P = Annotated[int, Field(ge=1, le=MAX_MEDIA_INTEGER)]
_I = Annotated[int, Field(ge=-MAX_MEDIA_INTEGER, le=MAX_MEDIA_INTEGER)]
_Key = _text(256)
_Ref = _text(128)
_Scope = Literal["local_only", "public_synthetic", "private_cloud_authorized"]
_Role = Literal[
    "converse",
    "decisions",
    "input_asr",
    "assistant_asr",
    "tts",
    "playback",
    "agent_media",
    "decoder",
]
_Purpose = Literal["owner_voice", "observe", "analyze"]
_Kind = Literal["recording", "live"]


def _normalize_error(error: ValidationError) -> MediaContractError:
    for item in error.errors(include_url=False, include_input=False):
        cause = item.get("ctx", {}).get("error")
        if isinstance(cause, MediaContractError) and cause.code == "bound":
            return MediaContractError("bound")
        if item["type"] in (
            "greater_than",
            "greater_than_equal",
            "less_than",
            "less_than_equal",
            "string_too_long",
            "too_long",
        ):
            return MediaContractError("bound")
    return MediaContractError()


class _MediaModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        hide_input_in_errors=True,
    )

    def __init__(self, **data) -> None:
        try:
            super().__init__(**data)
        except MediaContractError:
            raise
        except ValidationError as error:
            raise _normalize_error(error) from None
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise MediaContractError() from None

    @model_validator(mode="before")
    @classmethod
    def _strict_version(cls, value):
        _checked_graph(value, cls, construction=True)
        if type(value) is dict and "v" in cls.model_fields:
            _check_version(value.get("v"), cls.model_fields["v"].annotation)
        return value

    @model_validator(mode="after")
    def _complete_size(self) -> Self:
        _canonical(self, construction=True)
        return self


class MediaQ(_MediaModel):
    num: _N
    den: _P

    @model_validator(mode="after")
    def _reduced(self) -> Self:
        if gcd(self.num, self.den) != 1:
            _bad()
        return self


class MediaSignedQ(_MediaModel):
    num: _I
    den: _P

    @model_validator(mode="after")
    def _reduced(self) -> Self:
        if gcd(self.num, self.den) != 1:
            _bad()
        return self


class MediaSession(_MediaModel):
    session_id: _Ref
    thread_id: _Ref
    epoch: _P


class MediaNativeRef(_MediaModel):
    backend_id: _Id
    receipt_id: _Id
    sha256: _Hash


class MediaConsumerRef(_MediaModel):
    consumer_id: _Id
    generation: _P


class MediaFence(_MediaModel):
    session: MediaSession
    source_revision: _P
    generation: _P
    lease_generation: _P


class MediaReservation(_MediaModel):
    reservation_id: _Id
    owner_spool_bytes: _N
    global_spool_bytes: _N
    metadata_bytes: _N
    child_slots: _N
    policy_sha256: _Hash


class MediaSourceLimits(_MediaModel):
    file_bytes: _P
    file_duration_ms: _P
    upload_chunk_bytes: _P
    owner_spool_bytes: _P
    global_spool_bytes: _P
    unsealed_uploads_per_owner: _P
    operations_global: _P
    live_spool_bytes: _P
    live_buffer_target_ms: _P
    sealed_unstarted_ms: _P


class MediaDecodeLimits(_MediaModel):
    segment_ms: _P
    cpu_ms: _P
    wall_ms: _P
    address_space_bytes: _P
    cpu_affinity_count: _P
    children_per_operation: _P
    children_global: _P
    track_limit: _P
    frames_per_track_segment: _P
    pixels_per_frame: _P
    max_edge: _P
    frame_rate: MediaQ
    probe_metadata_bytes: _P
    stderr_bytes: _P

    @model_validator(mode="after")
    def _positive_rate(self) -> Self:
        if self.frame_rate.num == 0:
            _bad()
        return self


class MediaWireLimits(_MediaModel):
    control_bytes: _P
    body_bytes: _P
    audio_packet_ms: _P
    image_bytes: _P
    clip_bytes: _P
    clip_ms: _P
    consumer_packets: _P
    consumer_bytes: _P
    total_pending_bytes: _P
    live_backlog_ms: _P


class MediaLimits(_MediaModel):
    v: Literal[1]
    source: MediaSourceLimits
    decode: MediaDecodeLimits
    wire: MediaWireLimits


def _purpose(kind, purpose, anchor) -> None:
    if purpose == "owner_voice":
        if kind != "live" or anchor is not None:
            _bad()
    elif anchor is None:
        _bad()


class MediaAdmit(_MediaModel):
    v: Literal[1]
    key: _Key
    session: MediaSession
    kind: _Kind
    purpose: _Purpose
    anchor: MediaNativeRef | None
    replaces_source: MediaNativeRef | None
    declared_bytes: _N | None
    declared_sha256: _Hash | None
    decoder_profile_sha256: _Hash
    scope: _Scope
    raw_retain_until_ms: _N | None

    @model_validator(mode="after")
    def _admission_shape(self) -> Self:
        _purpose(self.kind, self.purpose, self.anchor)
        if self.kind == "live" and (
            self.declared_bytes is not None or self.declared_sha256 is not None
        ):
            _bad()
        return self


class MediaSourceAdmission(_MediaModel):
    v: Literal[1]
    admission_id: _Id
    source_id: _Id
    source_equivalence_id: _Id | None
    source_revision: _P
    session: MediaSession
    kind: _Kind
    purpose: _Purpose
    anchor: MediaNativeRef | None
    replaces_source: MediaNativeRef | None
    capture_instance: _Id | None
    client_instance: _Id
    scope: _Scope
    reservation: MediaReservation
    decoder_profile_sha256: _Hash
    fence: MediaFence
    lease_expires_at_ms: _N
    raw_retain_until_ms: _N | None

    @model_validator(mode="after")
    def _source_shape(self) -> Self:
        _purpose(self.kind, self.purpose, self.anchor)
        if self.fence.session != self.session or self.fence.source_revision != self.source_revision:
            _bad()
        if self.kind == "live":
            if self.source_equivalence_id is None or self.capture_instance is None:
                _bad()
        elif self.source_equivalence_id is not None:
            _bad()
        return self


class MediaUploadPut(_MediaModel):
    v: Literal[1]
    key: _Key
    source: MediaNativeRef
    fence: MediaFence
    index: _N
    offset: _N
    body_bytes: Annotated[int, Field(ge=1, le=4_194_304)]
    body_sha256: _Hash


class MediaUploadAck(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    index: _N
    offset: _N
    body_bytes: Annotated[int, Field(ge=1, le=4_194_304)]
    body_sha256: _Hash
    next_offset: _N

    @model_validator(mode="after")
    def _progress(self) -> Self:
        if self.offset + self.body_bytes > MAX_MEDIA_INTEGER:
            _bound()
        if self.next_offset != self.offset + self.body_bytes:
            _bad()
        return self


class MediaSealRequest(_MediaModel):
    v: Literal[1]
    key: _Key
    source: MediaNativeRef
    fence: MediaFence
    final_bytes: _P
    source_sha256: _Hash


class MediaSeal(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    source_equivalence_id: _Id
    fence: MediaFence
    reservation_id: _Id
    bytes: _P
    source_sha256: _Hash
    decoder_profile_sha256: _Hash
    state: Literal["sealed"]


class MediaSourcePTS(_MediaModel):
    pts: _I
    time_base_num: _P
    time_base_den: _P
    sample_start: _N | None
    sample_count: _P | None
    frame_index: _N | None

    @model_validator(mode="after")
    def _clock(self) -> Self:
        if gcd(self.time_base_num, self.time_base_den) != 1:
            _bad()
        audio = self.sample_start is not None and self.sample_count is not None
        video = self.frame_index is not None
        if audio == video or (
            not audio and (self.sample_start is not None or self.sample_count is not None)
        ):
            _bad()
        if audio and self.sample_start + self.sample_count > MAX_MEDIA_INTEGER:
            _bound()
        return self


class MediaCapturePut(_MediaModel):
    v: Literal[1]
    key: _Key
    source: MediaNativeRef
    fence: MediaFence
    capture_instance: _Id
    track_id: _Id
    track_seq: _P
    pts: MediaSourcePTS
    format_sha256: _Hash
    body_bytes: Annotated[int, Field(ge=1, le=MAX_MEDIA_BODY_BYTES)]
    body_sha256: _Hash

    @model_validator(mode="after")
    def _elementary_bound(self) -> Self:
        limit = 384_000 if self.pts.sample_count is not None else 8_388_608
        if self.body_bytes > limit:
            _bound()
        return self


class MediaCaptureAck(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    track_id: _Id
    track_seq: _P
    source_seq: _P
    blob: MediaNativeRef


def _interval(start: MediaQ, end: MediaQ, maximum_seconds: int | None = None) -> None:
    numerator = end.num * start.den - start.num * end.den
    denominator = end.den * start.den
    if numerator < 0:
        _bad()
    divisor = gcd(numerator, denominator)
    if numerator // divisor > MAX_MEDIA_INTEGER or denominator // divisor > MAX_MEDIA_INTEGER:
        _bound()
    if maximum_seconds is not None and numerator > maximum_seconds * denominator:
        _bound()


class MediaPacketHeader(_MediaModel):
    v: Literal[1]
    operation_id: _Id
    consumer: MediaConsumerRef
    source: MediaNativeRef
    fence: MediaFence
    payload: Literal["audio", "image", "video_frame", "video_clip"]
    track_id: _Id | None
    source_seq: _P
    through_source_seq: _P
    track_seq: _P | None
    pts: MediaSourcePTS | None
    range: MediaNativeRef | None
    start: MediaQ
    end: MediaQ
    format_sha256: _Hash
    body_bytes: Annotated[int, Field(ge=1, le=MAX_MEDIA_BODY_BYTES)]
    body_sha256: _Hash
    continuity_id: _Id
    sequence: _P

    @model_validator(mode="after")
    def _payload_shape(self) -> Self:
        _interval(self.start, self.end, 10 if self.payload == "video_clip" else None)
        if self.through_source_seq < self.source_seq:
            _bad()
        if self.payload == "video_clip":
            if self.range is None or any(
                x is not None for x in (self.track_id, self.track_seq, self.pts)
            ):
                _bad()
        else:
            if self.range is not None or any(
                x is None for x in (self.track_id, self.track_seq, self.pts)
            ):
                _bad()
            if self.source_seq != self.through_source_seq:
                _bad()
            if self.payload == "audio":
                if self.pts.sample_count is None:
                    _bad()
                _interval(self.start, self.end, 1)
                if self.body_bytes > 384_000:
                    _bound()
            else:
                if self.pts.frame_index is None or (
                    self.payload == "image" and self.pts.frame_index != 0
                ):
                    _bad()
                if self.body_bytes > 8_388_608:
                    _bound()
        return self


class MediaASRSession(_MediaModel):
    v: Literal[1]
    worker_instance: _Id
    operation_id: _Id
    source: MediaNativeRef
    fence: MediaFence
    track_id: _Id
    consumer: MediaConsumerRef
    continuity_id: _Id
    first_sample: _N
    request_sha256: _Hash
    retention: Literal["retained_backend", "declared_segmented_fallback"]


class MediaASRFeed(_MediaModel):
    v: Literal[1]
    key: _Key
    session: MediaASRSession
    sequence: _P
    packet: MediaPacketHeader

    @model_validator(mode="after")
    def _session_packet(self) -> Self:
        if self.packet.payload != "audio":
            _bad()
        for name in ("operation_id", "source", "fence", "track_id", "consumer", "continuity_id"):
            if getattr(self.packet, name) != getattr(self.session, name):
                _bad()
        return self


class MediaASRFinish(_MediaModel):
    v: Literal[1]
    key: _Key
    session: MediaASRSession
    through_sequence: _N


class MediaError(_MediaModel):
    v: Literal[1]
    error: Literal[
        "bad_packet",
        "denied",
        "unknown_ref",
        "stale",
        "expired",
        "unsupported",
        "bound",
        "capacity",
        "unavailable",
        "conflict",
    ]


def _path(value: str, *, relative: bool = False) -> None:
    if "\0" in value or ".." in value.split("/") or (relative and value.startswith("/")):
        _bad()


def _items(kind, maximum: int):
    return Annotated[list[kind], Field(max_length=maximum)]


def _distinct(items, name: str | None = None) -> None:
    values = [getattr(item, name) for item in items] if name is not None else items
    if len(set(values)) != len(values):
        _bad()


def _literal_int(value):
    if type(value) is not int:
        _bad()
    return value


_AudioRate = Annotated[Literal[16000, 24000, 48000], BeforeValidator(_literal_int)]
_AudioChannels = Annotated[Literal[1, 2], BeforeValidator(_literal_int)]
_EXECUTABLE_KINDS = frozenset({"decoder_exec", "probe_exec", "sandbox_exec", "python_exec"})
_PROBE_CODECS = MappingProxyType(
    {
        "audio": frozenset({"pcm_s16le", "pcm_s24le", "pcm_s32le", "aac", "opus"}),
        "image": frozenset({"png", "mjpeg"}),
        "video": frozenset({"png", "mjpeg", "h264", "hevc", "vp9", "av1"}),
    }
)


class MediaCodePin(_MediaModel):
    commit: _Git
    path: _text(256)
    git_blob: _Git
    sha256: _Hash

    @model_validator(mode="after")
    def _relative_path(self) -> Self:
        _path(self.path, relative=True)
        return self


class MediaAssetPin(_MediaModel):
    kind: Literal[
        "decoder_exec",
        "probe_exec",
        "sandbox_exec",
        "python_exec",
        "runtime_library",
        "driver_source",
    ]
    path: _text(4096)
    guest: _text(256)
    bytes: Annotated[int, Field(ge=1, le=268_435_456)]
    sha256: _Hash

    @model_validator(mode="after")
    def _asset_paths(self) -> Self:
        _path(self.path)
        _path(self.guest)
        return self


class MediaRuntime(_MediaModel):
    v: Literal[1]
    kind: Literal["sealed_asset_set"]
    manifest_sha256: _Hash
    assets: _items(MediaAssetPin, 256)
    distribution_lock: MediaCodePin | None

    @model_validator(mode="after")
    def _closure(self) -> Self:
        if sum(asset.bytes for asset in self.assets) > 1_073_741_824:
            _bound()
        if sum(asset.kind in _EXECUTABLE_KINDS for asset in self.assets) > 3:
            _bound()
        _distinct(self.assets, "guest")
        if self.manifest_sha256 != _hash("Runtime", self.__dict__, "manifest_sha256"):
            _bad()
        return self


class MediaAudioFormat(_MediaModel):
    kind: Literal["audio"]
    container: Literal["raw", "wav"]
    codec: Literal["pcm_s16le", "pcm_s24le", "pcm_s32le"]
    rate: _AudioRate
    channels: _AudioChannels


class MediaImageFormat(_MediaModel):
    kind: Literal["image", "video_frame"]
    container: Literal["png", "jpeg"]
    codec: Literal["png", "mjpeg"]
    pixel_format: Literal["rgb24"]

    @model_validator(mode="after")
    def _pair(self) -> Self:
        if (self.container, self.codec) not in (("png", "png"), ("jpeg", "mjpeg")):
            _bad()
        return self


class MediaClipFormat(_MediaModel):
    kind: Literal["video_clip"]
    container: Literal["mp4", "webm"]
    video_codec: Literal["h264", "hevc", "vp9", "av1"]
    audio_codec: Literal["aac", "opus", "none"]

    @model_validator(mode="after")
    def _pair(self) -> Self:
        if self.container == "mp4":
            if self.video_codec not in ("h264", "hevc", "av1") or self.audio_codec not in (
                "aac",
                "none",
            ):
                _bad()
        elif self.video_codec not in ("vp9", "av1") or self.audio_codec not in ("opus", "none"):
            _bad()
        return self


MediaFormat = Annotated[
    MediaAudioFormat | MediaImageFormat | MediaClipFormat, Field(discriminator="kind")
]


class MediaTransform(_MediaModel):
    v: Literal[1]
    kind: Literal["identity", "audio_resample_downmix", "video_sample_scale"]
    audio_rate: _N | None
    audio_channels: _N | None
    video_rate: MediaQ | None
    width: _N | None
    height: _N | None
    owner_setting_ref: _Ref | None

    @model_validator(mode="after")
    def _selected_transform(self) -> Self:
        if self.kind == "identity":
            if any(
                value is not None
                for name, value in self.__dict__.items()
                if name not in ("v", "kind")
            ):
                _bad()
        else:
            if self.owner_setting_ref is None:
                _bad()
            if self.kind == "audio_resample_downmix":
                if self.audio_rate not in (16000, 24000, 48000) or self.audio_channels not in (
                    1,
                    2,
                ):
                    _bad()
                if any(value is not None for value in (self.video_rate, self.width, self.height)):
                    _bad()
            else:
                if self.audio_rate is not None or self.audio_channels is not None:
                    _bad()
                if self.video_rate is not None and self.video_rate.num == 0:
                    _bad()
                dimensions = self.width is not None and self.height is not None
                if dimensions:
                    if self.width == 0 or self.height == 0:
                        _bad()
                elif self.width is not None or self.height is not None:
                    _bad()
                if self.video_rate is None and not dimensions:
                    _bad()
        return self


class MediaProbeTrack(_MediaModel):
    index: _N
    kind: Literal["audio", "video", "image"]
    codec: Literal[
        "pcm_s16le",
        "pcm_s24le",
        "pcm_s32le",
        "aac",
        "opus",
        "png",
        "mjpeg",
        "h264",
        "hevc",
        "vp9",
        "av1",
    ]
    rate: _P | None
    channels: _P | None
    width: _P | None
    height: _P | None
    timebase: MediaQ
    duration: MediaQ | None

    @model_validator(mode="after")
    def _track_shape(self) -> Self:
        if self.codec not in _PROBE_CODECS[self.kind]:
            _bad()
        if self.timebase.num == 0:
            _bad()
        audio = self.kind == "audio"
        if audio != (self.rate is not None and self.channels is not None):
            _bad()
        if audio and (self.width is not None or self.height is not None):
            _bad()
        if not audio and (
            self.width is None
            or self.height is None
            or self.rate is not None
            or self.channels is not None
        ):
            _bad()
        return self


class MediaProbeResult(_MediaModel):
    v: Literal[1]
    source_sha256: _Hash
    input_bytes: _P
    container: Literal["wav", "mp4", "webm", "png", "jpeg"]
    tracks: _items(MediaProbeTrack, 8)

    @model_validator(mode="after")
    def _indices(self) -> Self:
        _distinct(self.tracks, "index")
        return self


class MediaDecoderProtocol(_MediaModel):
    kind: Literal["decoder_fd_v1"]
    implementation: MediaCodePin | None
    request_schema_sha256: _Hash | None
    response_schema_sha256: _Hash | None
    wire: Literal["u32be_json_then_body"]
    peer: Literal["owned_pid_incarnation_nonce"]
    pts: Literal["original_rational"]
    cleanup: Literal["pidfd_reap"]
    segments: Literal["retained_child", "one_segment_child"]


class MediaDecoderProfile(_MediaModel):
    v: Literal[1]
    id: _Id
    generation: _P
    profile_sha256: _Hash
    driver: Literal["pyav17_timed_fd_v1", "ffmpeg_cli_fd_v1"]
    implementation: MediaCodePin | None
    executables: _items(MediaAssetPin, 3)
    runtime: MediaRuntime | None
    formats: _items(MediaFormat, 16)
    transform: MediaTransform
    limits: MediaLimits
    protocol: MediaDecoderProtocol

    @model_validator(mode="after")
    def _profile(self) -> Self:
        if any(asset.kind not in _EXECUTABLE_KINDS for asset in self.executables):
            _bad()
        _distinct(self.executables, "guest")
        if self.profile_sha256 != _hash("DecoderProfile", self.__dict__, "profile_sha256"):
            _bad()
        return self


class MediaDecoderProtocolV2(_MediaModel):
    kind: Literal["decoder_fd_v2"]
    implementation: MediaCodePin | None
    request_schema_sha256: _Hash | None
    response_schema_sha256: _Hash | None
    wire: Literal["u32be_json_then_body"]
    peer: Literal["owned_pid_incarnation_nonce"]
    pts: Literal["original_rational"]
    cleanup: Literal["pidfd_reap"]
    segments: Literal["retained_child", "one_segment_child"]


class MediaDecoderProfileV2(_MediaModel):
    v: Literal[1]
    id: _Id
    generation: _P
    profile_sha256: _Hash
    driver: Literal["pyav17_timed_fd_v2"]
    implementation: MediaCodePin | None
    executables: _items(MediaAssetPin, 3)
    runtime: MediaRuntime | None
    formats: _items(MediaFormat, 16)
    transform: MediaTransform
    limits: MediaLimits
    protocol: MediaDecoderProtocolV2

    @model_validator(mode="after")
    def _profile(self) -> Self:
        if any(asset.kind not in _EXECUTABLE_KINDS for asset in self.executables):
            _bad()
        _distinct(self.executables, "guest")
        if self.profile_sha256 != _hash("DecoderProfileV2", self.__dict__, "profile_sha256"):
            _bad()
        return self


class MediaTrack(_MediaModel):
    track_id: _Id
    source_id: _Id
    kind: Literal["audio", "video", "image"]
    format_sha256: _Hash
    timebase: MediaQ
    common_origin: MediaSignedQ
    clock_provenance: Literal[
        "container",
        "native_capture",
        "native_audio_samples",
        "client_reported",
        "provider_reported",
        "untimed_image",
    ]
    command_lane: bool

    @model_validator(mode="after")
    def _clock_shape(self) -> Self:
        if self.timebase.num == 0:
            _bad()
        if self.clock_provenance == "untimed_image" and (
            self.kind != "image" or self.common_origin.num != 0
        ):
            _bad()
        return self


def _tracks(tracks: list[MediaTrack]) -> None:
    _distinct(tracks, "track_id")
    if len({track.source_id for track in tracks}) > 1:
        _bad()
    origins = {
        (track.common_origin.num, track.common_origin.den)
        for track in tracks
        if track.clock_provenance != "untimed_image"
    }
    if len(origins) > 1:
        _bad()


class MediaCaptureTrackOffer(_MediaModel):
    client_track_key: _text(80)
    format_sha256: _Hash
    timebase: MediaQ
    command_lane: bool

    @model_validator(mode="after")
    def _positive_timebase(self) -> Self:
        if self.timebase.num == 0:
            _bad()
        return self


class MediaCaptureOffer(_MediaModel):
    v: Literal[1]
    key: _Key
    source: MediaNativeRef
    fence: MediaFence
    common_origin: MediaSignedQ
    tracks: _items(MediaCaptureTrackOffer, 8)

    @model_validator(mode="after")
    def _offered_tracks(self) -> Self:
        _distinct(self.tracks, "client_track_key")
        return self


class MediaCaptureReady(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    fence: MediaFence
    capture_instance: _Id
    decoder_profile_sha256: _Hash
    tracks: _items(MediaTrack, 8)

    @model_validator(mode="after")
    def _capture_tracks(self) -> Self:
        _tracks(self.tracks)
        return self


class MediaDecodeReady(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    seal: MediaNativeRef
    probe: MediaProbeResult
    decoder_profile_sha256: _Hash
    tracks: _items(MediaTrack, 8)

    @model_validator(mode="after")
    def _decoded_tracks(self) -> Self:
        _tracks(self.tracks)
        if any(track.command_lane for track in self.tracks):
            _bad()
        if sorted(track.kind for track in self.tracks) != sorted(
            track.kind for track in self.probe.tracks
        ):
            _bad()
        return self


class MediaSegment(_MediaModel):
    segment_id: _Id
    index: _N
    source: MediaNativeRef
    source_revision: _P
    generation: _P
    profile_sha256: _Hash
    continuity_id: _Id
    start: MediaQ
    end: MediaQ
    track_ids: _items(_Id, 8)

    @model_validator(mode="after")
    def _segment_shape(self) -> Self:
        _interval(self.start, self.end, 60)
        _distinct(self.track_ids)
        return self


class MediaDecodeRequest(_MediaModel):
    v: Literal[1]
    operation_id: _Id
    source: MediaNativeRef
    consumer: MediaConsumerRef
    fence: MediaFence
    child_owner: MediaNativeRef
    profile_sha256: _Hash
    seq: _P
    command: Literal["probe", "open", "next", "cancel"]
    segment: MediaSegment | None

    @model_validator(mode="after")
    def _command_shape(self) -> Self:
        if (self.command == "open") != (self.segment is not None):
            _bad()
        if self.segment is not None and (
            self.segment.source != self.source
            or self.segment.source_revision != self.fence.source_revision
            or self.segment.generation != self.fence.generation
            or self.segment.profile_sha256 != self.profile_sha256
        ):
            _bad()
        return self


class MediaTrackEnd(_MediaModel):
    track_id: _Id
    through_track_seq: _N
    through_source_seq: _N
    last_pts: MediaSourcePTS | None
    state: Literal["segment_boundary", "eof"]

    @model_validator(mode="after")
    def _sequence_bounds(self) -> Self:
        if self.through_track_seq > self.through_source_seq:
            _bad()
        return self


class MediaDecodeEnd(_MediaModel):
    v: Literal[1]
    segment_id: _Id
    source: MediaNativeRef
    sealed_source: MediaNativeRef | None
    child_owner: MediaNativeRef
    profile_sha256: _Hash
    kind: Literal["segment_end", "source_eof"]
    through_packet_sequence: _N
    tracks: _items(MediaTrackEnd, 8)

    @model_validator(mode="after")
    def _end_shape(self) -> Self:
        _distinct(self.tracks, "track_id")
        if self.kind == "source_eof" and any(track.state != "eof" for track in self.tracks):
            _bad()
        return self


class MediaDecodeResponse(_MediaModel):
    v: Literal[1]
    operation_id: _Id
    seq: _P
    segment_id: _Id | None
    result: Literal["probe", "packet", "end", "cancel_ack", "error"]
    probe: MediaProbeResult | None
    packet: MediaPacketHeader | None
    end: MediaDecodeEnd | None
    error: MediaError | None

    @model_validator(mode="after")
    def _result_shape(self) -> Self:
        for name in ("probe", "packet", "end", "error"):
            if (getattr(self, name) is not None) != (name == self.result):
                _bad()
        if self.result == "probe" and self.segment_id is not None:
            _bad()
        if self.result in ("packet", "end") and self.segment_id is None:
            _bad()
        if self.packet is not None and self.packet.operation_id != self.operation_id:
            _bad()
        if self.end is not None and self.end.segment_id != self.segment_id:
            _bad()
        return self


class MediaInputEnd(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    fence: MediaFence
    kind: Literal["finite_eof", "capture_closed"]
    seal: MediaNativeRef | None
    capture_ready: MediaNativeRef | None
    decoder_end: MediaNativeRef | None
    tracks: _items(MediaTrackEnd, 8)
    through_source_seq: _N

    @model_validator(mode="after")
    def _input_end_shape(self) -> Self:
        _distinct(self.tracks, "track_id")
        if any(
            track.state != "eof" or track.through_source_seq > self.through_source_seq
            for track in self.tracks
        ):
            _bad()
        if self.kind == "finite_eof":
            if self.seal is None or self.decoder_end is None or self.capture_ready is not None:
                _bad()
        elif self.capture_ready is None or self.seal is not None or self.decoder_end is not None:
            _bad()
        return self


class MediaDecodeBudget(_MediaModel):
    v: Literal[1]
    segment_id: _Id
    child_owner: MediaNativeRef
    previous: MediaNativeRef | None
    cpu_baseline_ms: _N
    wall_baseline_ms: _N
    cpu_spent_ms: _N
    wall_spent_ms: _N
    state: Literal["running", "ended", "unknown"]


class MediaDisclosure(_MediaModel):
    owner_event: _Ref
    role: _Role
    selection_sha256: _Hash
    protocol_sha256: _Hash
    scope: _Scope


class MediaConsumer(_MediaModel):
    v: Literal[1]
    consumer: MediaConsumerRef
    source: MediaNativeRef
    fence: MediaFence
    role: _Role
    instance: _Id
    capability_sha256: _Hash | None
    decoder_profile_sha256: _Hash | None
    scope: _Scope
    disclosure: MediaDisclosure | None
    anchor: MediaNativeRef | None
    task_admission: MediaNativeRef | None
    owner_intent: MediaNativeRef | None
    ranges: _items(MediaNativeRef, 64)
    permit_sha256: _Hash

    @model_validator(mode="after")
    def _consumer_shape(self) -> Self:
        if len({(ref.backend_id, ref.receipt_id, ref.sha256) for ref in self.ranges}) != len(
            self.ranges
        ):
            _bad()
        if self.disclosure is not None:
            if self.disclosure.role != self.role:
                _bad()
            if self.disclosure.scope != self.scope:
                _bad()
        if self.role == "decoder":
            if self.decoder_profile_sha256 is None or self.capability_sha256 is not None:
                _bad()
        if self.permit_sha256 != _hash("Consumer", self.__dict__, "permit_sha256"):
            _bad()
        return self


class MediaBlob(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    generation: _P
    track_id: _Id
    source_seq: _P
    track_seq: _P
    pts: MediaSourcePTS
    start: MediaQ
    end: MediaQ
    format_sha256: _Hash
    body_bytes: _P
    body_sha256: _Hash
    raw_state: Literal["buffered", "retained", "expired", "delete_pending", "deleted"]

    @model_validator(mode="after")
    def _blob_shape(self) -> Self:
        _interval(self.start, self.end)
        if self.pts.sample_count is not None:
            _interval(self.start, self.end, 1)
            if self.body_bytes > 384_000:
                _bound()
        else:
            if self.pts.frame_index is None:
                _bad()
            if self.body_bytes > 8_388_608:
                _bound()
        return self


class MediaTrackRange(_MediaModel):
    track_id: _Id
    first_track_seq: _P
    through_track_seq: _P
    first_sample: _N | None
    sample_count: _N | None
    first_frame: _N | None
    frame_count: _N | None

    @model_validator(mode="after")
    def _track_range_shape(self) -> Self:
        if self.through_track_seq < self.first_track_seq:
            _bad()
        sample_pair = (self.first_sample is not None, self.sample_count is not None)
        frame_pair = (self.first_frame is not None, self.frame_count is not None)
        if sample_pair not in ((True, True), (False, False)):
            _bad()
        if frame_pair not in ((True, True), (False, False)):
            _bad()
        if (sample_pair == (True, True)) == (frame_pair == (True, True)):
            _bad()
        return self


class MediaUnit(_MediaModel):
    unit_id: _Id
    start: MediaQ
    end: MediaQ
    first_source_seq: _P
    through_source_seq: _P
    tracks: _items(MediaTrackRange, 8)

    @model_validator(mode="after")
    def _unit_shape(self) -> Self:
        _interval(self.start, self.end, 10)
        if self.through_source_seq < self.first_source_seq:
            _bad()
        if not self.tracks:
            _bad()
        _distinct(self.tracks, "track_id")
        return self


class MediaManifestPage(_MediaModel):
    v: Literal[1]
    manifest_id: _Id
    source: MediaNativeRef
    generation: _P
    index: _N
    previous_page: MediaNativeRef | None
    units: _items(MediaUnit, 64)

    @model_validator(mode="after")
    def _page_shape(self) -> Self:
        if (self.index == 0) != (self.previous_page is None):
            _bad()
        _distinct(self.units, "unit_id")
        for previous, current in zip(self.units, self.units[1:], strict=False):
            if previous.end.num * current.start.den > current.start.num * previous.end.den:
                _bad()
            if current.first_source_seq <= previous.through_source_seq:
                _bad()
        return self


class MediaManifestSeal(_MediaModel):
    v: Literal[1]
    manifest_id: _Id
    source: MediaNativeRef
    generation: _P
    last_page: MediaNativeRef
    page_count: _P
    unit_count: _P
    through_source_seq: _P
    input_end: MediaNativeRef | None
    complete: bool

    @model_validator(mode="after")
    def _seal_shape(self) -> Self:
        if self.complete and self.input_end is None:
            _bad()
        return self


class MediaRange(_MediaModel):
    v: Literal[1]
    range_id: _Id
    source: MediaNativeRef
    source_equivalence_id: _Id
    source_revision: _P
    generation: _P
    manifest_id: _Id
    through_page: MediaNativeRef
    tracks: _items(MediaTrackRange, 8)
    start: MediaQ
    end: MediaQ
    first_source_seq: _P
    through_source_seq: _P

    @model_validator(mode="after")
    def _history_shape(self) -> Self:
        _interval(self.start, self.end)
        if self.through_source_seq < self.first_source_seq:
            _bad()
        if not self.tracks:
            _bad()
        _distinct(self.tracks, "track_id")
        return self


class MediaProbeClock(_MediaModel):
    probe_index: _N
    first_pts: _I
    timebase: MediaQ
    provenance: Literal["container", "untimed_image"]

    @model_validator(mode="after")
    def _clock_shape(self) -> Self:
        if self.timebase.num == 0:
            _bad()
        if self.provenance == "untimed_image" and self.first_pts != 0:
            _bad()
        return self


class MediaProbeObservation(_MediaModel):
    v: Literal[2]
    source: MediaNativeRef
    seal: MediaNativeRef
    child_owner: MediaNativeRef
    profile_sha256: _Hash
    probe: MediaProbeResult
    clocks: _items(MediaProbeClock, 8)

    @model_validator(mode="after")
    def _observation_shape(self) -> Self:
        _distinct(self.clocks, "probe_index")
        if {item.probe_index for item in self.clocks} != {
            track.index for track in self.probe.tracks
        }:
            _bad()
        by_index = {track.index: track for track in self.probe.tracks}
        for item in self.clocks:
            track = by_index[item.probe_index]
            if (item.timebase.num, item.timebase.den) != (
                track.timebase.num,
                track.timebase.den,
            ):
                _bad()
            expected = "untimed_image" if track.kind == "image" else "container"
            if item.provenance != expected:
                _bad()
        return self


class MediaDecodeSelection(_MediaModel):
    probe_index: _N
    track: MediaTrack
    format: MediaFormat

    @model_validator(mode="after")
    def _selection_shape(self) -> Self:
        if self.track.command_lane:
            _bad()
        if self.track.format_sha256 != media_digest("Format", self.format):
            _bad()
        expected = {"audio": "audio", "image": "image", "video": "video_frame"}[self.track.kind]
        if self.format.kind != expected:
            _bad()
        return self


class MediaDecodeTrackCursor(_MediaModel):
    track_id: _Id
    next_seq: _P


class MediaDecodeOpenPlan(_MediaModel):
    v: Literal[2]
    observation: MediaProbeObservation
    ready: MediaDecodeReady
    selections: _items(MediaDecodeSelection, 8)
    segment: MediaSegment
    budget_ref: MediaNativeRef
    budget: MediaDecodeBudget
    next_source_seq: _P
    cursors: _items(MediaDecodeTrackCursor, 8)

    @model_validator(mode="after")
    def _open_shape(self) -> Self:
        if (
            self.ready.source != self.observation.source
            or self.ready.seal != self.observation.seal
            or self.ready.probe != self.observation.probe
            or self.ready.decoder_profile_sha256 != self.observation.profile_sha256
        ):
            _bad()
        if (
            self.segment.source != self.ready.source
            or self.segment.profile_sha256 != self.ready.decoder_profile_sha256
            or set(self.segment.track_ids) != {track.track_id for track in self.ready.tracks}
        ):
            _bad()
        _distinct(self.selections, "probe_index")
        _distinct([item.track.track_id for item in self.selections], None)
        if {item.probe_index for item in self.selections} != {
            track.index for track in self.observation.probe.tracks
        }:
            _bad()
        by_id = {track.track_id: track for track in self.ready.tracks}
        if {item.track.track_id for item in self.selections} != set(by_id):
            _bad()
        for item in self.selections:
            if item.track != by_id[item.track.track_id]:
                _bad()
        _tracks(self.ready.tracks)
        probe_by_index = {track.index: track for track in self.observation.probe.tracks}
        clock_by_index = {item.probe_index: item for item in self.observation.clocks}
        for item in self.selections:
            probe = probe_by_index[item.probe_index]
            clock = clock_by_index[item.probe_index]
            if item.track.kind != probe.kind:
                _bad()
            if (item.track.timebase.num, item.track.timebase.den) != (
                probe.timebase.num,
                probe.timebase.den,
            ):
                _bad()
            if (item.track.timebase.num, item.track.timebase.den) != (
                clock.timebase.num,
                clock.timebase.den,
            ):
                _bad()
            if item.track.clock_provenance != clock.provenance:
                _bad()
        _distinct(self.cursors, "track_id")
        if {item.track_id for item in self.cursors} != set(by_id):
            _bad()
        if (
            self.budget.segment_id != self.segment.segment_id
            or self.budget.child_owner != self.observation.child_owner
            or self.budget.state != "running"
        ):
            _bad()
        return self


class MediaDecodeRequestV2(_MediaModel):
    v: Literal[2]
    operation_id: _Id
    source: MediaNativeRef
    seal: MediaNativeRef
    consumer: MediaConsumerRef
    fence: MediaFence
    child_owner: MediaNativeRef
    profile_sha256: _Hash
    seq: _P
    command: Literal["probe", "open", "next", "cancel"]
    profile: MediaDecoderProfileV2 | None
    plan: MediaDecodeOpenPlan | None

    @model_validator(mode="after")
    def _command_shape(self) -> Self:
        if self.command == "probe":
            if self.profile is None or self.plan is not None:
                _bad()
            if self.profile.driver != "pyav17_timed_fd_v2":
                _bad()
            if self.profile.protocol.kind != "decoder_fd_v2":
                _bad()
            if self.profile.profile_sha256 != self.profile_sha256:
                _bad()
        elif self.command == "open":
            if self.plan is None or self.profile is not None:
                _bad()
            if self.plan.observation.source != self.source:
                _bad()
            if self.plan.observation.seal != self.seal:
                _bad()
            if self.plan.observation.child_owner != self.child_owner:
                _bad()
            if self.plan.observation.profile_sha256 != self.profile_sha256:
                _bad()
            if self.plan.segment.source != self.source:
                _bad()
            if self.plan.segment.profile_sha256 != self.profile_sha256:
                _bad()
            if self.plan.segment.source_revision != self.fence.source_revision:
                _bad()
            if self.plan.segment.generation != self.fence.generation:
                _bad()
        elif self.profile is not None or self.plan is not None:
            _bad()
        return self


class MediaDecodeResponseV2(_MediaModel):
    v: Literal[2]
    operation_id: _Id
    seq: _P
    segment_id: _Id | None
    result: Literal["probe", "packet", "end", "cancel_ack", "error"]
    probe: MediaProbeObservation | None
    packet: MediaPacketHeader | None
    end: MediaDecodeEnd | None
    error: MediaError | None

    @model_validator(mode="after")
    def _result_shape(self) -> Self:
        for name in ("probe", "packet", "end", "error"):
            if (getattr(self, name) is not None) != (name == self.result):
                _bad()
        if self.result == "probe" and self.segment_id is not None:
            _bad()
        if self.result in ("packet", "end") and self.segment_id is None:
            _bad()
        if self.packet is not None and self.packet.operation_id != self.operation_id:
            _bad()
        if self.end is not None and self.end.segment_id != self.segment_id:
            _bad()
        return self


class MediaOwnerTextObjective(_MediaModel):
    kind: Literal["owner_text"]
    event_ref: _Ref


class MediaOwnerMediaObjective(_MediaModel):
    kind: Literal["owner_media"]
    intent: MediaNativeRef


MediaOwnerObjective = Annotated[
    MediaOwnerTextObjective | MediaOwnerMediaObjective, Field(discriminator="kind")
]


class MediaTaskProofRequest(_MediaModel):
    v: Literal[1]
    key: _Key
    session: MediaSession
    original_admission_id: _Ref


class MediaTaskProof(_MediaModel):
    v: Literal[1]
    session: MediaSession
    task_id: _Ref
    objective_revision: _P
    owner_objective: MediaOwnerObjective
    request_sha256: _Hash
    original_admission_id: _Ref
    state: Literal["accepted", "unknown"]


class MediaAnalyzeTaskAnchor(_MediaModel):
    v: Literal[1]
    owner_objective: MediaOwnerObjective
    session: MediaSession
    task_id: _Ref
    initial_objective_revision: _P
    task_admission: MediaNativeRef
    scope: _Scope
    allowed_operation: Literal["evidence_only"]


class MediaLegacyTaskBody(_MediaModel):
    contract: Literal["TaskRequest"]
    canonical_utf8_b64: _text(87_384)
    sha256: _Hash

    @model_validator(mode="after")
    def _legacy(self) -> Self:
        # The exception is an inline envelope allowance, not a larger or
        # different legacy TaskRequest parser or an admission authority.
        from packages.contracts.models import TaskRequest

        try:
            raw = base64.b64decode(self.canonical_utf8_b64, validate=True)
            if base64.b64encode(raw).decode("ascii") != self.canonical_utf8_b64:
                _bad()
            if len(raw) > MAX_MEDIA_CONTROL_BYTES:
                _bound()
            if hashlib.sha256(raw).hexdigest() != self.sha256:
                _bad()
            text = raw.decode("utf-8")
            _lexical_bounds(text)
            value = json.loads(
                text,
                object_pairs_hook=_pairs,
                parse_int=_integer,
                parse_float=_reject_number,
                parse_constant=_reject_number,
            )
            if _canonical(value) != raw:
                _bad()
            TaskRequest.model_validate(value)
        except MediaContractError:
            raise
        except ValidationError as error:
            raise _normalize_error(error) from None
        except (binascii.Error, UnicodeError, ValueError, TypeError):
            raise MediaContractError() from None
        return self


class MediaAnchorAdmission(_MediaModel):
    v: Literal[1]
    key: _Key
    owner_objective: MediaOwnerObjective
    scope: _Scope
    submit: MediaLegacyTaskBody | None
    task_admission: MediaNativeRef | None

    @model_validator(mode="after")
    def _one_admission(self) -> Self:
        if (self.submit is None) == (self.task_admission is None):
            _bad()
        if self.submit is not None and type(self.owner_objective) is not MediaOwnerTextObjective:
            _bad()
        return self


class MediaProcessIdentity(_MediaModel):
    pid: _P
    start_tick: _N
    boot_id: _Id
    nonce: _Id
    pidfd_owner: _Id


class MediaLocalProcessPeer(_MediaModel):
    kind: Literal["local_process"]
    identity: MediaProcessIdentity


class MediaNativeTaskPeer(_MediaModel):
    kind: Literal["native_task"]
    backend_instance: _Id
    task_id: _Id
    request_sha256: _Hash


class MediaOwnedWorkerPeer(_MediaModel):
    kind: Literal["owned_worker"]
    instance: _Id
    request_sha256: _Hash


class MediaClientPlaybackPeer(_MediaModel):
    kind: Literal["client_playback"]
    client_instance: _Id
    generation_epoch: _P


class MediaExternalTransportPeer(_MediaModel):
    kind: Literal["external_transport"]
    exchange_id: _Id


MediaPeer = Annotated[
    MediaLocalProcessPeer
    | MediaNativeTaskPeer
    | MediaOwnedWorkerPeer
    | MediaClientPlaybackPeer
    | MediaExternalTransportPeer,
    Field(discriminator="kind"),
]
_ChildKind = Literal[
    "upload",
    "decoder",
    "converse_provider",
    "decisions_provider",
    "input_asr",
    "assistant_asr",
    "tts_provider",
    "playback",
    "agent_media",
    "agent_delivery",
]
_Outcome = Literal["completed", "cancelled", "failed", "not_started"]
_Reason = Literal[
    "none",
    "denied",
    "stale",
    "superseded",
    "target_changed",
    "owner_stop",
    "disconnect",
    "lease_expired",
    "reset",
    "replacement",
    "eof",
    "expired",
    "deleted",
    "overrun",
    "quota",
    "decode_error",
    "provider_error",
    "publication_unknown",
    "cleanup_unknown",
    "representation_conflict",
]


class MediaChildOwner(_MediaModel):
    v: Literal[1]
    kind: _ChildKind
    instance: _Id
    operation_id: _Id
    source: MediaNativeRef
    consumer: MediaConsumerRef | None
    binding_sha256: _Hash
    backend_id: _Id
    peer: MediaPeer


class MediaChild(_MediaModel):
    kind: _ChildKind
    instance: _Id
    operation_id: _Id
    owner: MediaNativeRef
    binding_sha256: _Hash
    state: Literal["reserved", "running", "fenced", "closing", "settled", "unknown"]
    outcome: _Outcome | None
    cleanup: MediaNativeRef | None

    @model_validator(mode="after")
    def _settlement_shape(self) -> Self:
        if (self.state == "settled") != (self.outcome is not None):
            _bad()
        if self.state == "settled" and self.cleanup is None:
            _bad()
        return self


class MediaWorkerCleanup(_MediaModel):
    v: Literal[1]
    kind: Literal["native_owner"]
    operation_id: _Id
    child_instance: _Id
    binding_sha256: _Hash
    original_owner: MediaNativeRef
    state: Literal["settled", "unknown"]
    outcome: _Outcome | None
    proof: Literal[
        "local_join",
        "pidfd_reap",
        "owned_worker_receipt",
        "client_playback",
        "transport_only",
        "none",
    ]
    publication: Literal["settled", "unknown"]
    scope: Literal[
        "native_task", "local_process", "owned_worker", "client_playback", "external_unknown"
    ]

    @model_validator(mode="after")
    def _settlement_shape(self) -> Self:
        if self.state == "unknown":
            if self.outcome is not None:
                _bad()
        elif (
            self.outcome is None
            or self.publication != "settled"
            or {
                "native_task": "local_join",
                "local_process": "pidfd_reap",
                "owned_worker": "owned_worker_receipt",
                "client_playback": "client_playback",
            }.get(self.scope)
            != self.proof
        ):
            _bad()
        return self


class MediaLocalExchangeCleanup(_MediaModel):
    v: Literal[1]
    kind: Literal["local_exchange"]
    operation_id: _Id
    child_instance: _Id
    binding_sha256: _Hash
    original_owner: MediaNativeRef
    exchange_owner: MediaNativeRef
    state: Literal["settled", "unknown"]
    outcome: _Outcome | None
    local_task_joined: bool
    transport_sole_closed: bool
    body_fd_closed: bool
    all_readers_settled: bool
    publications_settled: bool
    observations: Annotated[list[MediaNativeRef], Field(max_length=64)]
    remote_liability: MediaNativeRef

    @model_validator(mode="after")
    def _settlement_shape(self) -> Self:
        if self.state == "unknown":
            if self.outcome is not None:
                _bad()
        elif (
            self.outcome is None
            or not all(
                (
                    self.local_task_joined,
                    self.transport_sole_closed,
                    self.body_fd_closed,
                    self.all_readers_settled,
                    self.publications_settled,
                )
            )
            or not self.observations
        ):
            _bad()
        if len({(ref.backend_id, ref.receipt_id) for ref in self.observations}) != len(
            self.observations
        ):
            _bad()
        return self


MediaCleanupReceipt = Annotated[
    MediaWorkerCleanup | MediaLocalExchangeCleanup, Field(discriminator="kind")
]


class MediaStatus(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    fence: MediaFence
    desired: Literal["running", "stopped", "deleted"]
    local_actual: Literal[
        "reserved",
        "uploading",
        "sealed",
        "ready",
        "running",
        "fenced",
        "closing",
        "settled",
        "unknown",
        "deleted",
    ]
    children: Annotated[list[MediaChild], Field(max_length=16)]
    remote_state: Literal["none", "unknown"]
    remote_liability_head: MediaNativeRef | None
    reason: _Reason
    reservation: MediaReservation
    raw_state: Literal["buffered", "retained", "expired", "delete_pending", "deleted"]

    @model_validator(mode="after")
    def _status_shape(self) -> Self:
        if (self.remote_state == "none") != (self.remote_liability_head is None):
            _bad()
        if len({child.instance for child in self.children}) != len(self.children):
            _bad()
        if self.local_actual in ("settled", "deleted") and any(
            child.state != "settled" for child in self.children
        ):
            _bad()
        if self.local_actual == "deleted" and self.raw_state != "deleted":
            _bad()
        return self


class MediaControl(_MediaModel):
    v: Literal[1]
    key: _Key
    source: MediaNativeRef
    fence: MediaFence
    command: Literal["start", "heartbeat", "stop", "seek", "reset", "resume"]
    position: MediaQ | None

    @model_validator(mode="after")
    def _position_shape(self) -> Self:
        if (self.command == "seek") != (self.position is not None):
            _bad()
        return self


class MediaDeleteRequest(_MediaModel):
    v: Literal[1]
    key: _Key
    source: MediaNativeRef
    fence: MediaFence


class MediaDeleteReceipt(_MediaModel):
    v: Literal[1]
    source: MediaNativeRef
    fence: MediaFence
    state: Literal["local_delete_pending", "local_deleted"]
    local_children_settled: bool
    local_spool_unlinked: bool
    local_processing_reservation_released: bool
    remote_state: Literal["none", "unknown"]
    remote_liability_head: MediaNativeRef | None

    @model_validator(mode="after")
    def _deletion_shape(self) -> Self:
        if (self.remote_state == "none") != (self.remote_liability_head is None):
            _bad()
        if self.state == "local_deleted" and not all(
            (
                self.local_children_settled,
                self.local_spool_unlinked,
                self.local_processing_reservation_released,
            )
        ):
            _bad()
        return self


_LookupKind = Literal[
    "source",
    "upload_ack",
    "capture_ack",
    "seal",
    "capture_ready",
    "decode_ready",
    "decode_end",
    "decode_budget",
    "input_end",
    "task_proof",
    "anchor",
    "owner_arming",
    "arming_revocation",
    "utterance_lineage",
    "intent",
    "consumer",
    "range",
    "blob",
    "record",
    "manifest_page",
    "manifest_seal",
    "word_page",
    "alignment_page",
    "speaker",
    "derived_source",
    "reply",
    "native_duplex_timing",
    "decision",
    "evidence",
    "text_bridge",
    "admission_request",
    "action",
    "native_admission",
    "dependency_head",
    "delivery",
    "apply_head",
    "agent_owner",
    "dispatcher_owner",
    "agent_observation",
    "dispatcher_observation",
    "delivery_retirement",
    "admission_retirement",
    "supervisor",
    "asr",
    "asr_backend_state",
    "stream_entry",
    "producer_set_page",
    "publication_plan",
    "producer_watermark",
    "watermark_page",
    "stream_seal",
    "stream_snapshot",
    "local_exchange_owner",
    "local_close_observation",
    "remote_liability",
    "child_owner",
    "cleanup",
    "status",
    "delete",
    "check",
    "qualification_summary",
    "artifact",
]


class MediaLookup(_MediaModel):
    v: Literal[1]
    kind: _LookupKind
    ref: MediaNativeRef


class MediaKeyLookup(_MediaModel):
    v: Literal[1]
    kind: _LookupKind
    key: _Key
    request_sha256: _Hash


class MediaChildOwnerAppend(_MediaModel):
    v: Literal[1]
    key: _Key
    consumer: MediaConsumerRef | None
    source: MediaNativeRef | None
    fence: MediaFence | None
    value: MediaChildOwner


class MediaWorkerCleanupAppend(_MediaModel):
    v: Literal[1]
    key: _Key
    consumer: MediaConsumerRef | None
    source: MediaNativeRef | None
    fence: MediaFence | None
    value: MediaWorkerCleanup


class MediaStatusAppend(_MediaModel):
    v: Literal[1]
    key: _Key
    consumer: MediaConsumerRef | None
    source: MediaNativeRef | None
    fence: MediaFence | None
    value: MediaStatus


_T = TypeVar("_T", bound=_MediaModel)


class MediaReceipt(_MediaModel, Generic[_T]):
    v: Literal[1]
    id: _Id
    key: _Key
    request_sha256: _Hash
    body: _T
    sha256: _Hash

    @model_validator(mode="after")
    def _receipt_digest(self) -> Self:
        short = _SHORT_NAMES.get(type(self))
        if short is None or self.sha256 != _hash(short, self.__dict__, "sha256"):
            _bad()
        return self


class MediaFound(_MediaModel, Generic[_T]):
    v: Literal[1]
    found: bool
    value: MediaReceipt[_T] | None

    @model_validator(mode="after")
    def _present(self) -> Self:
        if self.found != (self.value is not None):
            _bad()
        return self


# Explicit exact types: no subclass discovery and no caller-selected privilege.
_RECORDS = (
    MediaQ,
    MediaSignedQ,
    MediaSession,
    MediaNativeRef,
    MediaConsumerRef,
    MediaFence,
    MediaReservation,
    MediaSourceLimits,
    MediaDecodeLimits,
    MediaWireLimits,
    MediaLimits,
    MediaAdmit,
    MediaSourceAdmission,
    MediaUploadPut,
    MediaUploadAck,
    MediaSealRequest,
    MediaSeal,
    MediaSourcePTS,
    MediaCapturePut,
    MediaCaptureAck,
    MediaPacketHeader,
    MediaASRSession,
    MediaASRFeed,
    MediaASRFinish,
    MediaError,
    MediaCodePin,
    MediaAssetPin,
    MediaRuntime,
    MediaAudioFormat,
    MediaImageFormat,
    MediaClipFormat,
    MediaTransform,
    MediaProbeTrack,
    MediaProbeResult,
    MediaDecoderProtocol,
    MediaDecoderProfile,
    MediaTrack,
    MediaCaptureOffer,
    MediaCaptureReady,
    MediaDecodeReady,
    MediaSegment,
    MediaDecodeRequest,
    MediaTrackEnd,
    MediaDecodeEnd,
    MediaDecodeResponse,
    MediaInputEnd,
    MediaDecodeBudget,
    MediaDecoderProtocolV2,
    MediaDecoderProfileV2,
    MediaProbeClock,
    MediaProbeObservation,
    MediaDecodeSelection,
    MediaDecodeTrackCursor,
    MediaDecodeOpenPlan,
    MediaDecodeRequestV2,
    MediaDecodeResponseV2,
    MediaOwnerTextObjective,
    MediaOwnerMediaObjective,
    MediaTaskProofRequest,
    MediaTaskProof,
    MediaAnalyzeTaskAnchor,
    MediaLegacyTaskBody,
    MediaAnchorAdmission,
    MediaProcessIdentity,
    MediaLocalProcessPeer,
    MediaNativeTaskPeer,
    MediaOwnedWorkerPeer,
    MediaClientPlaybackPeer,
    MediaExternalTransportPeer,
    MediaChildOwner,
    MediaChild,
    MediaWorkerCleanup,
    MediaLocalExchangeCleanup,
    MediaStatus,
    MediaControl,
    MediaDeleteRequest,
    MediaDeleteReceipt,
    MediaLookup,
    MediaKeyLookup,
    MediaChildOwnerAppend,
    MediaWorkerCleanupAppend,
    MediaStatusAppend,
    MediaDisclosure,
    MediaConsumer,
    MediaBlob,
    MediaTrackRange,
    MediaUnit,
    MediaManifestPage,
    MediaManifestSeal,
    MediaRange,
)
_short_names = {kind: kind.__name__.removeprefix("Media") for kind in _RECORDS}
_short_names[MediaRange] = "MediaRange"
_schemas = {kind.__name__: kind for kind in _RECORDS}
for _kind in _RECORDS:
    _receipt = MediaReceipt[_kind]
    _found = MediaFound[_kind]
    _short_names[_receipt] = f"R<{_short_names[_kind]}>"
    _short_names[_found] = f"Found<{_short_names[_kind]}>"
    _schemas[f"MediaReceipt{_short_names[_kind]}"] = _receipt
    _schemas[f"MediaFound{_short_names[_kind]}"] = _found
_ALIASES = MappingProxyType(
    {
        "MediaFormat": MediaFormat,
        "MediaOwnerObjective": MediaOwnerObjective,
        "MediaPeer": MediaPeer,
        "MediaCleanupReceipt": MediaCleanupReceipt,
    }
)
for _name, _alias in _ALIASES.items():
    _short = _name.removeprefix("Media")
    _receipt = MediaReceipt[_alias]
    _found = MediaFound[_alias]
    _short_names[_receipt] = f"R<{_short}>"
    _short_names[_found] = f"Found<{_short}>"
    _schemas[f"MediaReceipt{_short}"] = _receipt
    _schemas[f"MediaFound{_short}"] = _found
_SHORT_NAMES = MappingProxyType(_short_names)
_BY_SHORT = MappingProxyType(
    {
        **{name: kind for kind, name in _short_names.items()},
        **{name.removeprefix("Media"): alias for name, alias in _ALIASES.items()},
    }
)
_GRAPH_KINDS = frozenset(_short_names) | {MediaCaptureTrackOffer}
_FORMAT_KINDS = (MediaAudioFormat, MediaImageFormat, MediaClipFormat)
_FORMAT_TAGS = MappingProxyType(
    {
        "audio": MediaAudioFormat,
        "image": MediaImageFormat,
        "video_frame": MediaImageFormat,
        "video_clip": MediaClipFormat,
    }
)
_OWNER_KINDS = (MediaOwnerTextObjective, MediaOwnerMediaObjective)
_PEER_KINDS = (
    MediaLocalProcessPeer,
    MediaNativeTaskPeer,
    MediaOwnedWorkerPeer,
    MediaClientPlaybackPeer,
    MediaExternalTransportPeer,
)
_CLEANUP_KINDS = (MediaWorkerCleanup, MediaLocalExchangeCleanup)
_UNIONS = (
    (MediaFormat, _FORMAT_KINDS, _FORMAT_TAGS),
    (
        MediaOwnerObjective,
        _OWNER_KINDS,
        MappingProxyType(
            {
                "owner_text": MediaOwnerTextObjective,
                "owner_media": MediaOwnerMediaObjective,
            }
        ),
    ),
    (
        MediaPeer,
        _PEER_KINDS,
        MappingProxyType(
            {
                "local_process": MediaLocalProcessPeer,
                "native_task": MediaNativeTaskPeer,
                "owned_worker": MediaOwnedWorkerPeer,
                "client_playback": MediaClientPlaybackPeer,
                "external_transport": MediaExternalTransportPeer,
            }
        ),
    ),
    (
        MediaCleanupReceipt,
        _CLEANUP_KINDS,
        MappingProxyType(
            {
                "native_owner": MediaWorkerCleanup,
                "local_exchange": MediaLocalExchangeCleanup,
            }
        ),
    ),
)
_INLINE_PATHS = MappingProxyType(
    {
        MediaAnchorAdmission: ("submit", "canonical_utf8_b64"),
        MediaReceipt[MediaAnchorAdmission]: ("body", "submit", "canonical_utf8_b64"),
        MediaFound[MediaAnchorAdmission]: ("value", "body", "submit", "canonical_utf8_b64"),
    }
)
_SELF = MappingProxyType(
    {
        MediaRuntime: "manifest_sha256",
        MediaDecoderProfile: "profile_sha256",
        MediaConsumer: "permit_sha256",
        MediaDecoderProfileV2: "profile_sha256",
        **{kind: "sha256" for kind in _short_names if issubclass(kind, MediaReceipt)},
    }
)
MEDIA_SCHEMAS = MappingProxyType(_schemas)
MEDIA_SCHEMA_ALIASES = _ALIASES
del _short_names, _schemas, _kind, _receipt, _found, _name, _alias, _short


def _graph(
    value,
    *,
    depth_limit=MAX_MEDIA_DEPTH,
    nodes_limit=MAX_MEDIA_NODES,
    text_limit=MAX_MEDIA_CONTROL_BYTES,
) -> None:
    """Inspect actual metadata and values before any model can normalize them."""
    pending = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > depth_limit or nodes > nodes_limit:
            _bound()
        if isinstance(item, BaseModel):
            kind = type(item)
            if kind not in _GRAPH_KINDS:
                _bad()
            fields = kind.model_fields
            values = getattr(item, "__dict__", None)
            supplied = getattr(item, "__pydantic_fields_set__", None)
            if (
                type(values) is not dict
                or values.keys() != fields.keys()
                or type(supplied) is not set
                or supplied != fields.keys()
                or any(type(name) is not str for name in supplied)
                or getattr(item, "__pydantic_extra__", False) is not None
                or getattr(item, "__pydantic_private__", False) is not None
            ):
                _bad()
            item = values
        if type(item) is dict:
            if nodes + len(pending) + len(item) > nodes_limit:
                _bound()
            for key, child in item.items():
                if type(key) is not str:
                    _bad()
                if len(key) > MAX_MEDIA_CONTROL_BYTES:
                    _bound()
                key.encode("utf-8")
                pending.append((child, depth + 1))
        elif type(item) is list:
            if nodes + len(pending) + len(item) > nodes_limit:
                _bound()
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > text_limit:
                _bound()
            item.encode("utf-8")
        elif type(item) is int:
            if item < -MAX_MEDIA_INTEGER or item > MAX_MEDIA_INTEGER:
                _bound()
        elif item is not None and type(item) is not bool:
            _bad()


def _field(value, annotation, typed: bool):
    origin = get_origin(annotation)
    if origin is Annotated:
        base, *metadata = get_args(annotation)
    else:
        base = annotation
        metadata = []
    if get_origin(base) is list:
        if type(value) is not list:
            _bad()
        maxima = [getattr(marker, "max_length", None) for marker in metadata]
        maxima = [maximum for maximum in maxima if type(maximum) is int]
        if not maxima:
            _bad()  # Only explicitly bounded declared lists belong to this wire.
        if len(value) > min(maxima):
            _bound()
        child = get_args(base)[0]
        return [_field(item, child, typed) for item in value]
    if get_origin(base) in (Union, types.UnionType):
        variants = get_args(base)
        if type(None) in variants:
            if value is None:
                return None
            nonnull = [variant for variant in variants if variant is not type(None)]
            if len(nonnull) == 1:
                return _field(value, nonnull[0], typed)
        for _, exact_variants, tags in _UNIONS:
            if variants == exact_variants:
                return _rebuild(value, _variant_kind(value, typed, exact_variants, tags))
        _bad()
    if isinstance(base, type) and issubclass(base, _MediaModel):
        if typed and type(value) is not base:
            _bad()
        return _rebuild(value, base)
    return TypeAdapter(annotation).validate_python(value, strict=True)


def _format_kind(value, typed: bool):
    return _variant_kind(value, typed, _FORMAT_KINDS, _FORMAT_TAGS)


def _variant_kind(value, typed: bool, variants, tags):
    if isinstance(value, BaseModel):
        if type(value) not in variants:
            _bad()
        return type(value)
    if typed or type(value) is not dict or type(value.get("kind")) is not str:
        _bad()
    kind = tags.get(value["kind"])
    if kind is None:
        _bad()
    return kind


def _rebuild(value, kind):
    for alias, variants, tags in _UNIONS:
        if kind is alias:
            kind = _variant_kind(value, False, variants, tags)
            break
    typed = isinstance(value, BaseModel)
    if typed:
        if type(value) is not kind:
            _bad()
        value = value.__dict__
    if type(value) is not dict or value.keys() != kind.model_fields.keys():
        _bad()
    # Literal adapters may use equality; reject bool before they can normalize it.
    if "v" in kind.model_fields:
        _check_version(value["v"], kind.model_fields["v"].annotation)
    fields = {
        name: _field(value[name], field.rebuild_annotation(), typed)
        for name, field in kind.model_fields.items()
    }
    return kind(**fields)


def _inline_metadata(value, path):
    """Copy only the declared inline path; all other metadata stays bounded."""
    if isinstance(value, BaseModel):
        value = value.__dict__
    if type(value) is not dict or path[0] not in value:
        return None
    child = value[path[0]]
    if len(path) == 1:
        if type(child) is not str:
            return None
        replacement = ""
    else:
        replacement = _inline_metadata(child, path[1:])
        if replacement is None:
            return None
    return {**value, path[0]: replacement}


def _checked_graph(value, kind=None, *, construction=False):
    path = _INLINE_PATHS.get(kind)
    if construction and kind is MediaLegacyTaskBody:
        path = ("canonical_utf8_b64",)
    if path is None:
        _graph(value)
        return MAX_MEDIA_CONTROL_BYTES
    # The generous outer bound is available only to these exact roots and the
    # one declared base64 field. Removing that field must leave ordinary-sized
    # metadata; no other nested record gains a larger cap.
    _graph(
        value,
        depth_limit=MAX_MEDIA_ADMISSION_DEPTH,
        nodes_limit=MAX_MEDIA_ADMISSION_NODES,
        text_limit=87_384,
    )
    metadata = _inline_metadata(value, path)
    if metadata is None:
        _graph(value)
        return MAX_MEDIA_CONTROL_BYTES
    _canonical(metadata)
    return MAX_MEDIA_ADMISSION_BYTES


def _canonical(value, kind=None, *, construction=False) -> bytes:
    def actual(item):
        if isinstance(item, BaseModel):
            return item.__dict__
        raise MediaContractError()

    limit = _checked_graph(value, type(value) if kind is None else kind, construction=construction)
    raw = json.dumps(
        value,
        default=actual,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > limit:
        _bound()
    return raw


def _hash(short_type: str, body, omitted: str | None = None) -> str:
    # Only a registered root self-field is removed. All nested hashes survive.
    value = body if omitted is None else {key: item for key, item in body.items() if key != omitted}
    return hashlib.sha256(
        ("kinaiya.media.v1/" + short_type + "\0").encode("utf-8")
        + _canonical(value, _BY_SHORT.get(short_type))
    ).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _bad()
        result[key] = value
    return result


def _integer(token: str) -> int:
    if len(token.lstrip("-")) > 16:
        _bound()
    value = int(token)
    if abs(value) > MAX_MEDIA_INTEGER:
        _bound()
    return value


def _reject_number(_token):
    _bad()


def _lexical_bounds(text: str, *, depth_limit=MAX_MEDIA_DEPTH, nodes_limit=MAX_MEDIA_NODES) -> None:
    """Bound nesting/nodes before the JSON decoder builds a recursive graph."""
    stack = []
    nodes = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char in ",:":
            index += 1
            continue
        if char in "}]":
            if not stack or stack.pop() != ("{" if char == "}" else "["):
                _bad()
            index += 1
            continue
        depth = len(stack) + 1
        if char == '"':
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                elif text[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            else:
                _bad()
            lookahead = index
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] == ":":
                continue  # Object keys are not JSON nodes.
        elif char in "{[":
            stack.append(char)
            index += 1
        else:
            match = re.match(r"[^\s,\[\]{}:]+", text[index:])
            if match is None:
                _bad()
            token = match.group()
            if re.fullmatch(r"-?[0-9]+", token):
                if re.fullmatch(r"-?(0|[1-9][0-9]*)", token) is None:
                    _bad()
                _integer(token)
            index += len(token)
        nodes += 1
        if depth > depth_limit or nodes > nodes_limit:
            _bound()
    if stack:
        _bad()


def media_json(raw: bytes, kind: type[_T]) -> _T:
    """Parse a registered DTO or exact MediaFormat alias into fresh typed values."""
    try:
        alias = any(kind is candidate for candidate in _ALIASES.values())
        if type(raw) is not bytes or (
            not alias and (not isinstance(kind, type) or kind not in _SHORT_NAMES)
        ):
            _bad()
        inline = kind in _INLINE_PATHS
        if len(raw) > (MAX_MEDIA_ADMISSION_BYTES if inline else MAX_MEDIA_CONTROL_BYTES):
            _bound()
        text = raw.decode("utf-8")
        _lexical_bounds(
            text,
            depth_limit=MAX_MEDIA_ADMISSION_DEPTH if inline else MAX_MEDIA_DEPTH,
            nodes_limit=MAX_MEDIA_ADMISSION_NODES if inline else MAX_MEDIA_NODES,
        )
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
        if len(raw) > _checked_graph(value, kind):
            _bound()
        rebuilt = _rebuild(value, kind)
        _canonical(rebuilt)
        return rebuilt
    except MediaContractError:
        raise
    except ValidationError as error:
        raise _normalize_error(error) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise MediaContractError() from None


def media_bytes(model: _T) -> bytes:
    """Revalidate the complete actual typed closure before canonical encoding."""
    try:
        if type(model) not in _SHORT_NAMES:
            _bad()
        _checked_graph(model, type(model))
        return _canonical(_rebuild(model, type(model)))
    except MediaContractError:
        raise
    except ValidationError as error:
        raise _normalize_error(error) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise MediaContractError() from None


def media_digest(short_type: str, body: _MediaModel | dict) -> str:
    """Hash a closed body, omitting only an explicitly registered root self hash."""
    try:
        if type(short_type) is not str or short_type not in _BY_SHORT:
            _bad()
        kind = _BY_SHORT[short_type]
        _checked_graph(body, kind)
        self_field = _SELF.get(kind)
        if self_field is not None:
            typed = isinstance(body, BaseModel)
            if typed and type(body) is not kind:
                _bad()
            values = body.__dict__ if typed else body
            if type(values) is not dict:
                _bad()
            expected = kind.model_fields.keys() - {self_field}
            if values.keys() not in (expected, kind.model_fields.keys()):
                _bad()
            _check_version(values["v"], kind.model_fields["v"].annotation)
            fields = {
                name: _field(values[name], kind.model_fields[name].rebuild_annotation(), typed)
                for name in expected
            }
            if self_field in values:
                _field(
                    values[self_field], kind.model_fields[self_field].rebuild_annotation(), typed
                )
            _check_version(fields["v"], kind.model_fields["v"].annotation)
            digest = _hash(short_type, fields)
            if kind in (
                MediaRuntime,
                MediaDecoderProfile,
                MediaDecoderProfileV2,
                MediaConsumer,
            ):
                # Construction helpers still execute every complete root semantic
                # check and full-envelope cap with the computed self hash present.
                kind(**fields, **{self_field: digest})
            return digest
        return _hash(short_type, _rebuild(body, kind))
    except MediaContractError:
        raise
    except ValidationError as error:
        raise _normalize_error(error) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise MediaContractError() from None
