"""Closed media packets. References describe provenance, never grant authority.

This first family supplies source intake and bounded packet transport. Native
owners still resolve references, permissions, policy, currentness and cleanup.
"""

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
_N = Annotated[int, Field(ge=0, le=MAX_MEDIA_INTEGER)]
_P = Annotated[int, Field(ge=1, le=MAX_MEDIA_INTEGER)]
_I = Annotated[int, Field(ge=-MAX_MEDIA_INTEGER, le=MAX_MEDIA_INTEGER)]
_Key = _text(256)
_Ref = _text(128)
_Scope = Literal["local_only", "public_synthetic", "private_cloud_authorized"]
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
        _graph(value)
        if type(value) is dict and "v" in cls.model_fields:
            if type(value.get("v")) is not int or value["v"] != 1:
                _bad()
        return value

    @model_validator(mode="after")
    def _complete_size(self) -> Self:
        _canonical(self)
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
)
_short_names = {kind: kind.__name__.removeprefix("Media") for kind in _RECORDS}
_schemas = {kind.__name__: kind for kind in _RECORDS}
for _kind in _RECORDS:
    _receipt = MediaReceipt[_kind]
    _found = MediaFound[_kind]
    _short_names[_receipt] = f"R<{_short_names[_kind]}>"
    _short_names[_found] = f"Found<{_short_names[_kind]}>"
    _schemas[f"MediaReceipt{_short_names[_kind]}"] = _receipt
    _schemas[f"MediaFound{_short_names[_kind]}"] = _found
_SHORT_NAMES = MappingProxyType(_short_names)
_BY_SHORT = MappingProxyType({name: kind for kind, name in _short_names.items()})
MEDIA_SCHEMAS = MappingProxyType(_schemas)
del _short_names, _schemas, _kind, _receipt, _found


def _graph(value) -> None:
    """Inspect actual metadata and values before any model can normalize them."""
    pending = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > MAX_MEDIA_DEPTH or nodes > MAX_MEDIA_NODES:
            _bound()
        if isinstance(item, BaseModel):
            kind = type(item)
            if kind not in _SHORT_NAMES:
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
            for key, child in item.items():
                if type(key) is not str:
                    _bad()
                key.encode("utf-8")
                pending.append((child, depth + 1))
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            item.encode("utf-8")
        elif type(item) is int:
            if abs(item) > MAX_MEDIA_INTEGER:
                _bound()
        elif item is not None and type(item) is not bool:
            _bad()


def _field(value, annotation, typed: bool):
    origin = get_origin(annotation)
    if origin is Annotated:
        base = get_args(annotation)[0]
    else:
        base = annotation
    if get_origin(base) in (Union, types.UnionType):
        variants = get_args(base)
        if type(None) in variants:
            if value is None:
                return None
            nonnull = [variant for variant in variants if variant is not type(None)]
            if len(nonnull) == 1:
                return _field(value, nonnull[0], typed)
        _bad()
    if isinstance(base, type) and issubclass(base, _MediaModel):
        if typed and type(value) is not base:
            _bad()
        return _rebuild(value, base)
    return TypeAdapter(annotation).validate_python(value, strict=True)


def _rebuild(value, kind):
    typed = isinstance(value, BaseModel)
    if typed:
        if type(value) is not kind:
            _bad()
        value = value.__dict__
    if type(value) is not dict or value.keys() != kind.model_fields.keys():
        _bad()
    # Literal adapters may use equality; reject bool before they can normalize it.
    if "v" in kind.model_fields and (type(value["v"]) is not int or value["v"] != 1):
        _bad()
    fields = {
        name: _field(value[name], field.rebuild_annotation(), typed)
        for name, field in kind.model_fields.items()
    }
    return kind(**fields)


def _canonical(value) -> bytes:
    def actual(item):
        if isinstance(item, BaseModel):
            return item.__dict__
        raise MediaContractError()

    _graph(value)
    raw = json.dumps(
        value,
        default=actual,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > MAX_MEDIA_CONTROL_BYTES:
        _bound()
    return raw


def _hash(short_type: str, body, omitted: str | None = None) -> str:
    # Only a registered root self-field is removed. All nested hashes survive.
    value = body if omitted is None else {key: item for key, item in body.items() if key != omitted}
    return hashlib.sha256(
        ("kinaiya.media.v1/" + short_type + "\0").encode("utf-8") + _canonical(value)
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


def _lexical_bounds(text: str) -> None:
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
        if depth > MAX_MEDIA_DEPTH or nodes > MAX_MEDIA_NODES:
            _bound()
    if stack:
        _bad()


def media_json(raw: bytes, kind: type[_T]) -> _T:
    """Parse strict JSON (ordering/whitespace may differ); return a fresh DTO."""
    try:
        if type(raw) is not bytes or not isinstance(kind, type) or kind not in _SHORT_NAMES:
            _bad()
        if len(raw) > MAX_MEDIA_CONTROL_BYTES:
            _bound()
        text = raw.decode("utf-8")
        _lexical_bounds(text)
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
        _graph(value)
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
        _graph(model)
        return _canonical(_rebuild(model, type(model)))
    except MediaContractError:
        raise
    except ValidationError as error:
        raise _normalize_error(error) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise MediaContractError() from None


def media_digest(short_type: str, body: _MediaModel | dict) -> str:
    """Hash a registered complete body, or the exact prehash Receipt fields."""
    try:
        if type(short_type) is not str or short_type not in _BY_SHORT:
            _bad()
        kind = _BY_SHORT[short_type]
        _graph(body)
        if short_type.startswith("R<"):
            typed = isinstance(body, BaseModel)
            if typed and type(body) is not kind:
                _bad()
            values = body.__dict__ if typed else body
            if type(values) is not dict:
                _bad()
            expected = kind.model_fields.keys() - {"sha256"}
            if values.keys() not in (expected, kind.model_fields.keys()):
                _bad()
            if type(values["v"]) is not int or values["v"] != 1:
                _bad()
            fields = {
                name: _field(values[name], kind.model_fields[name].rebuild_annotation(), typed)
                for name in expected
            }
            if "sha256" in values:
                _field(values["sha256"], kind.model_fields["sha256"].rebuild_annotation(), typed)
            if type(fields["v"]) is not int or fields["v"] != 1:
                _bad()
            return _hash(short_type, fields)
        return _hash(short_type, _rebuild(body, kind))
    except MediaContractError:
        raise
    except ValidationError as error:
        raise _normalize_error(error) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise MediaContractError() from None
