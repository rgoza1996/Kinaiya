"""Pinned local llama.cpp multimodal transport for Converse and Decisions.

Supports only the retained local protocols qualified at upstream llama.cpp
commit ddf48f85c933c1f438212a02eb21e7b001d2d37d:
- Converse buffered multimodal input (`POST /v1/chat/completions?autoload=false`)
  with ordered text, PNG `image_url`, canonical PCM WAV `input_audio`, and MP4
  `input_video`, returning one terminal `finish_reason == "stop"` text message.
- Decisions direct media classification (`POST /v1/systemone?autoload=false`)
  with `{model, state, files: [data URI], questions: {...}}` for up to 8
  PNG/WAV inputs and typed choice/noul answers.

No model discovery, autoloading, implicit fallback, cloud endpoint, or native
speech-output claim is permitted here.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import inspect
import json
import math
import re
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

import httpx

from packages.contracts.media import (
    MediaConsumerRef,
    MediaNativeRef,
    MediaQ,
    MediaSourcePTS,
    _interval,
)
from packages.contracts.models import ProviderInfo
from packages.providers.complete_http import RequestBoundAsyncClient, complete_stream
from packages.providers.prompt_budget import PromptCountUnavailable
from packages.providers.text import (
    ProviderCleanupError,
    ProviderError,
    ProviderRefusal,
    _OwnedHTTPStream,
)
from packages.providers.whistle_ipc import JsonProtocolError, decode_json

UPSTREAM_LLAMA_COMMIT = "ddf48f85c933c1f438212a02eb21e7b001d2d37d"
PINNED_LLAMA_CPP_COMMIT = UPSTREAM_LLAMA_COMMIT

MAX_MEDIA_PARTS = 16
MAX_SYSTEMONE_FILES = 8
MAX_TEXT_PART_CHARS = 16_000
MAX_SYSTEM_PROMPT_CHARS = 16_000
MAX_STATE_CHARS = 36_000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BYTES = 384_000
MAX_VIDEO_CLIP_BYTES = 16 * 1024 * 1024
MAX_TOTAL_MEDIA_BYTES = 16 * 1024 * 1024
MAX_REQUEST_WIRE_BYTES = 32 * 1024 * 1024
MAX_CHAT_RESPONSE_BYTES = 512 * 1024
MAX_SYSTEMONE_RESPONSE_BYTES = 128 * 1024
MAX_CHAT_OUTPUT_CHARS = 16_000
MAX_CHAT_OUTPUT_BYTES = 64 * 1024
MAX_SAFE_INT = 2**53 - 1
MAX_EXCHANGE_OBSERVATIONS = 64
MAX_BODY_READERS = 16
MAX_PUBLICATION_OWNERS = 16

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_WAV_RATES = frozenset({16000, 24000, 48000})
_WAV_CHANNELS = frozenset({1, 2})
_WAV_BITS = frozenset({16, 24, 32})
_PART_KINDS = frozenset({"text", "image", "video_frame", "audio", "video_clip"})
_SYSTEMONE_PART_KINDS = frozenset({"image", "video_frame", "audio"})
_COMPONENT_PROOFS = MappingProxyType(
    {
        "local_task": "task_join",
        "transport": "sole_transport_close",
        "body": "body_fd_close",
        "reader": "reader_join",
        "publication": "publication_settlement",
    }
)


class LlamaMediaProtocolError(ProviderRefusal):
    """Protocol, schema, or provenance violation in local llama.cpp media exchange."""


class LlamaMediaTimeoutError(ProviderError, TimeoutError):
    """The local llama.cpp media exchange exceeded its total deadline."""


def _compact_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise LlamaMediaProtocolError("Local media JSON payload is invalid") from exc
    return raw


def _strict_json_object(raw: bytes, limit: int, label: str) -> dict:
    if type(raw) is not bytes or not 1 <= len(raw) <= limit:
        raise LlamaMediaProtocolError(f"{label} exceeded or violated byte bounds")
    try:
        value = decode_json(raw, limit)
        _compact_json(value)
    except (
        JsonProtocolError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
    ) as exc:
        raise LlamaMediaProtocolError(f"{label} was not a valid closed JSON object") from exc
    if type(value) is not dict:
        raise LlamaMediaProtocolError(f"{label} must decode to a JSON object")
    return value


def _validate_utf8_text(value: object, maximum_chars: int, label: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum_chars:
        raise LlamaMediaProtocolError(f"{label} must be a non-empty bounded string")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise LlamaMediaProtocolError(f"{label} contains invalid Unicode") from exc
    if "\x00" in value:
        raise LlamaMediaProtocolError(f"{label} cannot contain NUL bytes")
    return value


def _validate_positive_int(value: object, label: str, *, maximum: int = MAX_SAFE_INT) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise LlamaMediaProtocolError(f"{label} must be a bounded positive integer")
    return value


def _validate_nonnegative_int(value: object, label: str, *, maximum: int = MAX_SAFE_INT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise LlamaMediaProtocolError(f"{label} must be a bounded non-negative integer")
    return value


def _validate_probability(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and 0.0 <= float(value) <= 1.0


def _validate_hex32(value: object, label: str) -> str:
    if type(value) is not str or _HEX32_RE.fullmatch(value) is None:
        raise LlamaMediaProtocolError(f"{label} must be a 32-character lowercase hex ID")
    return value


def _validate_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise LlamaMediaProtocolError(f"{label} must be a 64-character lowercase hex SHA-256")
    return value


def _max_bytes_for_kind(kind: str) -> int:
    if kind in {"image", "video_frame"}:
        return MAX_IMAGE_BYTES
    if kind == "audio":
        return MAX_AUDIO_BYTES
    if kind == "video_clip":
        return MAX_VIDEO_CLIP_BYTES
    raise LlamaMediaProtocolError("Unsupported binary media part kind")


def _validate_png_bytes(data: bytes) -> None:
    if (
        type(data) is not bytes
        or len(data) < 33
        or len(data) > MAX_IMAGE_BYTES
        or not data.startswith(_PNG_SIG)
        or not data.endswith(_PNG_IEND)
        or data[12:16] != b"IHDR"
    ):
        raise LlamaMediaProtocolError("Local media image part requires valid bounded PNG bytes")
    ihdr_len = struct.unpack(">I", data[8:12])[0]
    width, height = struct.unpack(">II", data[16:24])
    if ihdr_len != 13 or not (1 <= width <= 8192 and 1 <= height <= 8192):
        raise LlamaMediaProtocolError("Local media PNG dimensions or IHDR header are invalid")


def _validate_wav_bytes(data: bytes) -> None:
    if (
        type(data) is not bytes
        or len(data) <= 44
        or len(data) > MAX_AUDIO_BYTES
        or data[:4] != b"RIFF"
        or data[8:16] != b"WAVEfmt "
        or data[36:40] != b"data"
    ):
        raise LlamaMediaProtocolError(
            "Local media audio part requires a canonical 44-byte header PCM WAV"
        )
    riff_size = struct.unpack("<I", data[4:8])[0]
    fmt_size, audio_fmt, channels, sample_rate, byte_rate, block_align, bits = struct.unpack(
        "<IHHIIHH", data[16:36]
    )
    data_size = struct.unpack("<I", data[40:44])[0]
    payload_bytes = len(data) - 44
    expected_align = channels * (bits // 8)
    expected_byte_rate = sample_rate * expected_align
    if (
        riff_size != len(data) - 8
        or fmt_size != 16
        or audio_fmt != 1
        or channels not in _WAV_CHANNELS
        or sample_rate not in _WAV_RATES
        or bits not in _WAV_BITS
        or block_align != expected_align
        or byte_rate != expected_byte_rate
        or data_size != payload_bytes
        or payload_bytes % block_align != 0
    ):
        raise LlamaMediaProtocolError("Local media WAV header does not match canonical PCM framing")


def _validate_mp4_bytes(data: bytes) -> None:
    if (
        type(data) is not bytes
        or len(data) < 16
        or len(data) > MAX_VIDEO_CLIP_BYTES
        or data[4:8] != b"ftyp"
    ):
        raise LlamaMediaProtocolError(
            "Local media video_clip part requires valid bounded MP4 bytes"
        )
    box_size = struct.unpack(">I", data[:4])[0]
    if box_size < 8 or box_size > len(data):
        raise LlamaMediaProtocolError("Local media MP4 ftyp box size is invalid")


def _decode_strict_base64(b64_text: object, max_raw_bytes: int, label: str) -> bytes:
    if type(b64_text) is not str or not b64_text:
        raise LlamaMediaProtocolError(f"{label} must be a non-empty base64 string")
    max_b64_chars = ((max_raw_bytes + 2) // 3) * 4
    if len(b64_text) > max_b64_chars or not b64_text.isascii():
        raise LlamaMediaProtocolError(f"{label} exceeded base64 length bound")
    try:
        raw = base64.b64decode(b64_text.encode("ascii"), validate=True)
    except Exception as exc:
        raise LlamaMediaProtocolError(f"{label} is not valid canonical base64") from exc
    if not 1 <= len(raw) <= max_raw_bytes:
        raise LlamaMediaProtocolError(f"{label} decoded bytes violated size bounds")
    if base64.b64encode(raw).decode("ascii") != b64_text:
        raise LlamaMediaProtocolError(f"{label} is not canonical base64 encoding")
    return raw


def _validate_loopback_endpoint(base_url: str, *, allow_cloud: object = False) -> str:
    if allow_cloud is not False:
        raise ProviderError("Native llama media provider is local-only")
    if (
        type(base_url) is not str
        or not base_url
        or len(base_url) > 256
        or base_url != base_url.strip()
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) > 126 or ch == "\\" for ch in base_url)
    ):
        raise ProviderError("Use a clean ASCII loopback base URL")
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderError("Invalid native llama media endpoint port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProviderError("Use an explicit loopback root URL with a port and no path or query")
    return base_url.rstrip("/")


@dataclass(frozen=True, slots=True)
class LlamaMediaPart:
    kind: Literal["text", "image", "video_frame", "audio", "video_clip"]
    text: str | None = None
    data: bytes | None = None
    mime_type: Literal["image/png", "audio/wav", "video/mp4"] | None = None
    source: MediaNativeRef | None = None
    range: MediaNativeRef | None = None
    track_id: str | None = None
    source_seq: int | None = None
    through_source_seq: int | None = None
    pts: MediaSourcePTS | None = None
    start: MediaQ | None = None
    end: MediaQ | None = None
    body_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _PART_KINDS:
            raise LlamaMediaProtocolError("Unsupported local media part kind")
        if self.kind == "text":
            _validate_utf8_text(self.text, MAX_TEXT_PART_CHARS, "Text media part")
            if any(
                value is not None
                for value in (
                    self.data,
                    self.mime_type,
                    self.source,
                    self.range,
                    self.track_id,
                    self.source_seq,
                    self.through_source_seq,
                    self.pts,
                    self.start,
                    self.end,
                    self.body_sha256,
                )
            ):
                raise LlamaMediaProtocolError("Text part cannot carry binary media metadata")
            return

        if self.text is not None:
            raise LlamaMediaProtocolError("Binary media part cannot carry inline text")
        max_kind_bytes = _max_bytes_for_kind(self.kind)
        if type(self.data) is not bytes or not 1 <= len(self.data) <= max_kind_bytes:
            raise LlamaMediaProtocolError("Binary media part violated per-kind byte bounds")
        if type(self.source) is not MediaNativeRef:
            raise LlamaMediaProtocolError(
                "Binary media part requires an exact source MediaNativeRef"
            )
        if type(self.start) is not MediaQ or type(self.end) is not MediaQ:
            raise LlamaMediaProtocolError(
                "Binary media part requires exact start and end MediaQ bounds"
            )
        source_seq = _validate_positive_int(self.source_seq, "source_seq")
        through_seq = _validate_positive_int(self.through_source_seq, "through_source_seq")
        if through_seq < source_seq:
            raise LlamaMediaProtocolError(
                "Binary media part through_source_seq preceded source_seq"
            )
        _validate_sha256(self.body_sha256, "Binary media part body_sha256")

        try:
            _interval(
                self.start,
                self.end,
                10 if self.kind == "video_clip" else (1 if self.kind == "audio" else None),
            )
        except Exception as exc:
            raise LlamaMediaProtocolError(
                "Binary media part interval violated contract bounds"
            ) from exc

        if self.kind == "video_clip":
            if self.mime_type != "video/mp4":
                raise LlamaMediaProtocolError("video_clip part requires mime_type='video/mp4'")
            _validate_mp4_bytes(self.data)
            if type(self.range) is not MediaNativeRef:
                raise LlamaMediaProtocolError(
                    "video_clip part requires an exact range MediaNativeRef"
                )
            if self.track_id is not None or self.pts is not None:
                raise LlamaMediaProtocolError(
                    "video_clip part cannot carry single-track pts or track_id"
                )
        else:
            if self.range is not None:
                raise LlamaMediaProtocolError(
                    "Single-packet media part cannot carry a clip range ref"
                )
            if source_seq != through_seq:
                raise LlamaMediaProtocolError(
                    "Single-packet media part requires source_seq == through_source_seq"
                )
            if type(self.track_id) is not str or not _ID_RE.fullmatch(self.track_id):
                raise LlamaMediaProtocolError("Single-packet media part requires a valid track_id")
            if type(self.pts) is not MediaSourcePTS:
                raise LlamaMediaProtocolError(
                    "Single-packet media part requires an exact MediaSourcePTS"
                )

            if self.kind in {"image", "video_frame"}:
                if self.mime_type != "image/png":
                    raise LlamaMediaProtocolError("Image/frame part requires mime_type='image/png'")
                _validate_png_bytes(self.data)
                if self.pts.frame_index is None or (
                    self.kind == "image" and self.pts.frame_index != 0
                ):
                    raise LlamaMediaProtocolError("Image/frame part PTS frame_index is invalid")
            elif self.kind == "audio":
                if self.mime_type != "audio/wav":
                    raise LlamaMediaProtocolError("Audio part requires mime_type='audio/wav'")
                _validate_wav_bytes(self.data)
                if self.pts.sample_count is None:
                    raise LlamaMediaProtocolError("Audio part PTS requires sample_count")

        digest = hashlib.sha256(self.data).hexdigest()
        if self.body_sha256 != digest:
            raise LlamaMediaProtocolError(
                "Binary media part body_sha256 did not match payload bytes"
            )


@dataclass(frozen=True, slots=True)
class LlamaMediaPartProvenance:
    index: int
    kind: Literal["text", "image", "video_frame", "audio", "video_clip"]
    mime_type: Literal["image/png", "audio/wav", "video/mp4"] | None
    source: MediaNativeRef | None
    range: MediaNativeRef | None
    track_id: str | None
    source_seq: int | None
    through_source_seq: int | None
    pts: MediaSourcePTS | None
    start: MediaQ | None
    end: MediaQ | None
    body_bytes: int
    body_sha256: str


@dataclass(frozen=True, slots=True)
class LlamaMediaRequestSpec:
    mode: Literal["converse_chat", "decisions_systemone"]
    model: str
    parts: Sequence[LlamaMediaPart]
    system_prompt: str | None = None
    temperature: int | float = 0
    max_tokens: int = 128
    reasoning_budget: int | None = None
    state: str | None = None
    questions: dict[str, dict] | None = None
    upstream_commit: str = UPSTREAM_LLAMA_COMMIT


@dataclass(frozen=True, slots=True)
class LlamaMediaPreparedRequest:
    mode: Literal["converse_chat", "decisions_systemone"]
    model: str
    path: Literal["/v1/chat/completions", "/v1/systemone"]
    query: Literal["autoload=false"]
    upstream_commit: str
    body: dict
    raw_body: bytes
    request_sha256: str
    provenance: tuple[LlamaMediaPartProvenance, ...]
    questions: dict[str, dict] | None


@dataclass(frozen=True, slots=True)
class LlamaMediaChatResult:
    mode: Literal["converse_chat"]
    model: str
    text: str
    finish_reason: Literal["stop"]
    usage: dict[str, int] | None
    request_sha256: str
    response_sha256: str
    provenance: tuple[LlamaMediaPartProvenance, ...]


@dataclass(frozen=True, slots=True)
class LlamaMediaSystemOneResult:
    mode: Literal["decisions_systemone"]
    model: str
    answers: dict[str, dict]
    usage: dict[str, int]
    request_sha256: str
    response_sha256: str
    provenance: tuple[LlamaMediaPartProvenance, ...]


def _validate_body_storage(storage: object) -> dict[str, object]:
    if type(storage) is not dict or "kind" not in storage:
        raise LlamaMediaProtocolError("LocalBodyOwner.body requires a closed storage descriptor")
    kind = storage.get("kind")
    if kind == "wire_buffer":
        if set(storage.keys()) != {"kind"}:
            raise LlamaMediaProtocolError("wire_buffer storage descriptor takes no extra fields")
        return {"kind": "wire_buffer"}
    if kind == "owned_fd":
        if set(storage.keys()) != {"kind", "fd_owner_id"}:
            raise LlamaMediaProtocolError("owned_fd storage descriptor requires fd_owner_id")
        fd_owner_id = _validate_hex32(storage["fd_owner_id"], "BodyStorage.fd_owner_id")
        return {"kind": "owned_fd", "fd_owner_id": fd_owner_id}
    raise LlamaMediaProtocolError("Unsupported BodyStorage kind")


def _validate_exchange_liability(liability: object) -> dict[str, object]:
    if type(liability) is not dict or "kind" not in liability:
        raise LlamaMediaProtocolError(
            "LocalExchangeCleanupV2.liability requires a closed descriptor"
        )
    kind = liability.get("kind")
    if kind == "local_no_remote":
        if set(liability.keys()) != {"kind"}:
            raise LlamaMediaProtocolError("local_no_remote liability takes no extra fields")
        return {"kind": "local_no_remote"}
    if kind == "disclosed_remote":
        if (
            set(liability.keys()) != {"kind", "remote_liability"}
            or type(liability.get("remote_liability")) is not MediaNativeRef
        ):
            raise LlamaMediaProtocolError(
                "disclosed_remote liability requires a MediaNativeRef remote_liability"
            )
        return {"kind": "disclosed_remote", "remote_liability": liability["remote_liability"]}
    raise LlamaMediaProtocolError("Unsupported ExchangeLiability kind")


@dataclass(frozen=True, slots=True)
class LocalBodyOwner:
    v: Literal[1]
    exchange_id: str
    body_owner_id: str
    source: MediaNativeRef
    consumer: MediaConsumerRef
    request_sha256: str
    body_bytes: int
    body_sha256: str
    reader_owner_ids: tuple[str, ...]
    body: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.v) is not int or self.v != 1:
            raise LlamaMediaProtocolError("LocalBodyOwner.v must be literal 1")
        _validate_hex32(self.exchange_id, "LocalBodyOwner.exchange_id")
        _validate_hex32(self.body_owner_id, "LocalBodyOwner.body_owner_id")
        if type(self.source) is not MediaNativeRef:
            raise LlamaMediaProtocolError("LocalBodyOwner.source must be a MediaNativeRef")
        if type(self.consumer) is not MediaConsumerRef:
            raise LlamaMediaProtocolError("LocalBodyOwner.consumer must be a MediaConsumerRef")
        _validate_sha256(self.request_sha256, "LocalBodyOwner.request_sha256")
        _validate_positive_int(
            self.body_bytes, "LocalBodyOwner.body_bytes", maximum=MAX_REQUEST_WIRE_BYTES
        )
        _validate_sha256(self.body_sha256, "LocalBodyOwner.body_sha256")
        if self.body_sha256 != self.request_sha256:
            raise LlamaMediaProtocolError(
                "LocalBodyOwner.body_sha256 must match exact encoded request_sha256"
            )
        if not isinstance(self.reader_owner_ids, (list, tuple)) or not (
            1 <= len(self.reader_owner_ids) <= MAX_BODY_READERS
        ):
            raise LlamaMediaProtocolError("LocalBodyOwner requires 1..16 reader_owner_ids")
        readers = tuple(
            _validate_hex32(rid, "LocalBodyOwner.reader_owner_ids") for rid in self.reader_owner_ids
        )
        if len(set(readers)) != len(readers):
            raise LlamaMediaProtocolError("LocalBodyOwner.reader_owner_ids must be distinct")
        object.__setattr__(self, "reader_owner_ids", readers)
        object.__setattr__(self, "body", _validate_body_storage(self.body))


@dataclass(frozen=True, slots=True)
class LocalBodyClose:
    v: Literal[1]
    body_owner: MediaNativeRef
    state: Literal["settled", "unknown"]
    proof: Literal["wire_buffer_retired", "original_body_fd_closed", "none"]
    request_sha256: str
    binding_sha256: str

    def __post_init__(self) -> None:
        if type(self.v) is not int or self.v != 1:
            raise LlamaMediaProtocolError("LocalBodyClose.v must be literal 1")
        if type(self.body_owner) is not MediaNativeRef:
            raise LlamaMediaProtocolError("LocalBodyClose.body_owner must be a MediaNativeRef")
        if self.state not in {"settled", "unknown"}:
            raise LlamaMediaProtocolError("LocalBodyClose.state must be 'settled' or 'unknown'")
        if self.proof not in {"wire_buffer_retired", "original_body_fd_closed", "none"}:
            raise LlamaMediaProtocolError("LocalBodyClose.proof is invalid")
        if self.state == "settled" and self.proof == "none":
            raise LlamaMediaProtocolError("Settled LocalBodyClose requires a positive proof")
        if self.state == "unknown" and self.proof != "none":
            raise LlamaMediaProtocolError("Unknown LocalBodyClose requires proof='none'")
        _validate_sha256(self.request_sha256, "LocalBodyClose.request_sha256")
        _validate_sha256(self.binding_sha256, "LocalBodyClose.binding_sha256")


@dataclass(frozen=True, slots=True)
class LocalCloseObservation:
    v: Literal[1]
    exchange_owner: MediaNativeRef
    component: Literal["local_task", "transport", "body", "reader", "publication"]
    owner_id: str
    state: Literal["settled", "unknown"]
    proof: Literal[
        "task_join",
        "sole_transport_close",
        "body_fd_close",
        "reader_join",
        "publication_settlement",
        "none",
    ]
    request_sha256: str
    binding_sha256: str

    def __post_init__(self) -> None:
        if type(self.v) is not int or self.v != 1:
            raise LlamaMediaProtocolError("LocalCloseObservation.v must be literal 1")
        if type(self.exchange_owner) is not MediaNativeRef:
            raise LlamaMediaProtocolError(
                "LocalCloseObservation.exchange_owner must be a MediaNativeRef"
            )
        if self.component not in _COMPONENT_PROOFS:
            raise LlamaMediaProtocolError("LocalCloseObservation.component is invalid")
        _validate_hex32(self.owner_id, "LocalCloseObservation.owner_id")
        if self.state not in {"settled", "unknown"}:
            raise LlamaMediaProtocolError(
                "LocalCloseObservation.state must be 'settled' or 'unknown'"
            )
        expected_proof = _COMPONENT_PROOFS[self.component]
        if self.state == "settled" and self.proof != expected_proof:
            raise LlamaMediaProtocolError(
                "Settled LocalCloseObservation proof must match component"
            )
        if self.state == "unknown" and self.proof != "none":
            raise LlamaMediaProtocolError("Unknown LocalCloseObservation requires proof='none'")
        _validate_sha256(self.request_sha256, "LocalCloseObservation.request_sha256")
        _validate_sha256(self.binding_sha256, "LocalCloseObservation.binding_sha256")


@dataclass(frozen=True, slots=True)
class LocalExchangeCleanupV2:
    v: Literal[1]
    kind: Literal["local_exchange_v2"]
    operation_id: str
    child_instance: str
    binding_sha256: str
    original_owner: MediaNativeRef
    exchange_owner: MediaNativeRef
    body_owner: MediaNativeRef
    body_observation: MediaNativeRef
    state: Literal["settled", "unknown"]
    outcome: Literal["completed", "cancelled", "failed", "not_started"] | None
    local_task_joined: bool
    transport_sole_closed: bool
    body_settled: bool
    all_readers_settled: bool
    publications_settled: bool
    observations: tuple[MediaNativeRef, ...]
    liability: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.v) is not int or self.v != 1:
            raise LlamaMediaProtocolError("LocalExchangeCleanupV2.v must be literal 1")
        if self.kind != "local_exchange_v2":
            raise LlamaMediaProtocolError("LocalExchangeCleanupV2.kind must be 'local_exchange_v2'")
        _validate_hex32(self.operation_id, "LocalExchangeCleanupV2.operation_id")
        _validate_hex32(self.child_instance, "LocalExchangeCleanupV2.child_instance")
        _validate_sha256(self.binding_sha256, "LocalExchangeCleanupV2.binding_sha256")
        for ref_name, ref_val in (
            ("original_owner", self.original_owner),
            ("exchange_owner", self.exchange_owner),
            ("body_owner", self.body_owner),
            ("body_observation", self.body_observation),
        ):
            if type(ref_val) is not MediaNativeRef:
                raise LlamaMediaProtocolError(
                    f"LocalExchangeCleanupV2.{ref_name} must be a MediaNativeRef"
                )
        for flag_name in (
            "local_task_joined",
            "transport_sole_closed",
            "body_settled",
            "all_readers_settled",
            "publications_settled",
        ):
            if type(getattr(self, flag_name)) is not bool:
                raise LlamaMediaProtocolError(
                    f"LocalExchangeCleanupV2.{flag_name} must be a boolean"
                )
        if not isinstance(self.observations, (list, tuple)):
            raise LlamaMediaProtocolError("LocalExchangeCleanupV2.observations must be a sequence")
        obs_tuple = tuple(self.observations)
        if any(type(ref) is not MediaNativeRef for ref in obs_tuple):
            raise LlamaMediaProtocolError(
                "LocalExchangeCleanupV2.observations must contain MediaNativeRef items"
            )
        combined = (*obs_tuple, self.body_observation)
        if len(combined) > MAX_EXCHANGE_OBSERVATIONS:
            raise LlamaMediaProtocolError(
                "LocalExchangeCleanupV2 combined observations exceeded 64-ref cap"
            )
        ref_keys = {(r.backend_id, r.receipt_id, r.sha256) for r in combined}
        if len(ref_keys) != len(combined):
            raise LlamaMediaProtocolError(
                "LocalExchangeCleanupV2 observations and body_observation must be distinct"
            )
        object.__setattr__(self, "observations", obs_tuple)
        object.__setattr__(self, "liability", _validate_exchange_liability(self.liability))

        all_five = (
            self.local_task_joined
            and self.transport_sole_closed
            and self.body_settled
            and self.all_readers_settled
            and self.publications_settled
        )
        if self.state == "settled":
            if not all_five or self.outcome not in {
                "completed",
                "cancelled",
                "failed",
                "not_started",
            }:
                raise LlamaMediaProtocolError(
                    "LocalExchangeCleanupV2 settled state requires all five booleans and non-null outcome"
                )
        elif self.state == "unknown":
            if self.outcome is not None:
                raise LlamaMediaProtocolError(
                    "LocalExchangeCleanupV2 unknown state must retain outcome=None"
                )
        else:
            raise LlamaMediaProtocolError(
                "LocalExchangeCleanupV2.state must be 'settled' or 'unknown'"
            )


@dataclass(frozen=True, slots=True)
class LlamaExchangeRegistration:
    operation_id: str
    child_instance: str
    binding_sha256: str
    original_owner: MediaNativeRef
    exchange_owner: MediaNativeRef
    body_owner_ref: MediaNativeRef
    body_owner: LocalBodyOwner
    local_task_id: str
    transport_owner_id: str
    publication_owner_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_hex32(self.operation_id, "LlamaExchangeRegistration.operation_id")
        _validate_hex32(self.child_instance, "LlamaExchangeRegistration.child_instance")
        _validate_sha256(self.binding_sha256, "LlamaExchangeRegistration.binding_sha256")
        if type(self.original_owner) is not MediaNativeRef:
            raise LlamaMediaProtocolError("original_owner must be a MediaNativeRef")
        if type(self.exchange_owner) is not MediaNativeRef:
            raise LlamaMediaProtocolError("exchange_owner must be a MediaNativeRef")
        if type(self.body_owner_ref) is not MediaNativeRef:
            raise LlamaMediaProtocolError("body_owner_ref must be a MediaNativeRef")
        if type(self.body_owner) is not LocalBodyOwner:
            raise LlamaMediaProtocolError("body_owner must be a LocalBodyOwner")
        _validate_hex32(self.local_task_id, "LlamaExchangeRegistration.local_task_id")
        _validate_hex32(self.transport_owner_id, "LlamaExchangeRegistration.transport_owner_id")
        if not isinstance(self.publication_owner_ids, (list, tuple)) or not (
            1 <= len(self.publication_owner_ids) <= MAX_PUBLICATION_OWNERS
        ):
            raise LlamaMediaProtocolError(
                "LlamaExchangeRegistration requires 1..16 publication_owner_ids"
            )
        pubs = tuple(
            _validate_hex32(pid, "LlamaExchangeRegistration.publication_owner_ids")
            for pid in self.publication_owner_ids
        )
        if len(set(pubs)) != len(pubs):
            raise LlamaMediaProtocolError("publication_owner_ids must be distinct")
        object.__setattr__(self, "publication_owner_ids", pubs)


@dataclass(frozen=True, slots=True)
class LlamaExchangeSettlementBundle:
    body_close: LocalBodyClose
    body_observation: MediaNativeRef
    component_observations: tuple[LocalCloseObservation, ...]
    observation_refs: tuple[MediaNativeRef, ...]
    cleanup: LocalExchangeCleanupV2

    def __post_init__(self) -> None:
        if type(self.body_close) is not LocalBodyClose:
            raise LlamaMediaProtocolError("body_close must be a LocalBodyClose")
        if type(self.body_observation) is not MediaNativeRef:
            raise LlamaMediaProtocolError("body_observation must be a MediaNativeRef")
        if not isinstance(self.component_observations, (list, tuple)):
            raise LlamaMediaProtocolError("component_observations must be a sequence")
        if not isinstance(self.observation_refs, (list, tuple)):
            raise LlamaMediaProtocolError("observation_refs must be a sequence")
        comp_obs = tuple(self.component_observations)
        obs_refs = tuple(self.observation_refs)
        if len(comp_obs) != len(obs_refs):
            raise LlamaMediaProtocolError(
                "component_observations and observation_refs must have equal length"
            )
        if any(type(obs) is not LocalCloseObservation for obs in comp_obs):
            raise LlamaMediaProtocolError(
                "component_observations must contain LocalCloseObservation items"
            )
        if any(type(ref) is not MediaNativeRef for ref in obs_refs):
            raise LlamaMediaProtocolError("observation_refs must contain MediaNativeRef items")
        if type(self.cleanup) is not LocalExchangeCleanupV2:
            raise LlamaMediaProtocolError("cleanup must be a LocalExchangeCleanupV2")
        object.__setattr__(self, "component_observations", comp_obs)
        object.__setattr__(self, "observation_refs", obs_refs)


@dataclass(frozen=True, slots=True)
class LlamaLocalExchangeSettlement:
    exchange_id: str
    local_task_id: str
    transport_owner_id: str
    body_owner_id: str
    request_sha256: str
    model: str
    upstream_commit: str
    local_task_joined: bool
    transport_sole_closed: bool
    body_settled: bool
    all_readers_settled: bool
    publications_settled: bool
    state: Literal["settled", "unknown"]
    outcome: Literal["completed", "cancelled", "failed", "not_started"] | None
    attempt_disposition: Literal[
        "completed",
        "cancelled",
        "timed_out",
        "failed",
        "cleanup_unconfirmed",
        "not_started",
    ] = "not_started"
    kind: Literal["local_exchange_v2"] = "local_exchange_v2"
    binding_sha256: str | None = None
    body_storage: dict[str, object] = field(default_factory=lambda: {"kind": "wire_buffer"})
    body_proof: Literal["wire_buffer_retired", "original_body_fd_closed", "none"] = "none"
    liability: dict[str, object] = field(default_factory=lambda: {"kind": "local_no_remote"})
    body_owner_record: LocalBodyOwner | None = None
    body_close_record: LocalBodyClose | None = None
    cleanup_receipt: LocalExchangeCleanupV2 | None = None

    @property
    def body_fd_closed(self) -> bool:
        return self.body_settled and self.body_proof == "original_body_fd_closed"

    @property
    def transport_disposition(self) -> str:
        return self.attempt_disposition


def _validate_questions(questions: object) -> dict[str, dict]:
    if type(questions) is not dict or not 1 <= len(questions) <= 16:
        raise LlamaMediaProtocolError("SystemOne media request requires 1..16 typed questions")
    validated: dict[str, dict] = {}
    for name, spec in questions.items():
        if type(name) is not str or not _ID_RE.fullmatch(name) or type(spec) is not dict:
            raise LlamaMediaProtocolError("SystemOne question key or definition is malformed")
        qtype = spec.get("type")
        instructions = spec.get("instructions")
        _validate_utf8_text(instructions, 2000, f"Question {name} instructions")
        if qtype == "choice":
            if set(spec.keys()) != {"type", "instructions", "criteria"}:
                raise LlamaMediaProtocolError(f"Choice question {name} has unexpected fields")
            criteria = spec.get("criteria")
            if type(criteria) is not dict or not 2 <= len(criteria) <= 16:
                raise LlamaMediaProtocolError(f"Choice question {name} requires 2..16 criteria")
            clean_criteria: dict[str, str] = {}
            for label, desc in criteria.items():
                if type(label) is not str or not _ID_RE.fullmatch(label):
                    raise LlamaMediaProtocolError(f"Choice question {name} has invalid label")
                clean_criteria[label] = _validate_utf8_text(
                    desc, 1000, f"Choice question {name} criterion {label}"
                )
            validated[name] = {
                "type": "choice",
                "instructions": instructions,
                "criteria": clean_criteria,
            }
        elif qtype == "noul":
            if set(spec.keys()) != {"type", "instructions"}:
                raise LlamaMediaProtocolError(f"Noul question {name} has unexpected fields")
            validated[name] = {
                "type": "noul",
                "instructions": instructions,
            }
        else:
            raise LlamaMediaProtocolError(f"Unsupported SystemOne question type for {name}")
    return validated


def _prevalidate_raw_part_bounds(
    raw_part: object,
    *,
    mode: Literal["converse_chat", "decisions_systemone"],
) -> int:
    if isinstance(raw_part, LlamaMediaPart):
        kind = raw_part.kind
        data = raw_part.data
        text = raw_part.text
    elif type(raw_part) is dict:
        allowed_part_keys = {
            "kind",
            "text",
            "data",
            "mime_type",
            "source",
            "range",
            "track_id",
            "source_seq",
            "through_source_seq",
            "pts",
            "start",
            "end",
            "body_sha256",
        }
        if not set(raw_part.keys()) <= allowed_part_keys or "kind" not in raw_part:
            raise LlamaMediaProtocolError("Local media request part dict has invalid keys")
        kind = raw_part.get("kind")
        data = raw_part.get("data")
        text = raw_part.get("text")
    else:
        raise LlamaMediaProtocolError("Every request part must be a LlamaMediaPart or dict")

    if kind not in _PART_KINDS:
        raise LlamaMediaProtocolError("Unsupported local media part kind")
    if mode == "decisions_systemone" and kind not in _SYSTEMONE_PART_KINDS:
        raise LlamaMediaProtocolError(
            "decisions_systemone files only accept PNG image/video_frame and PCM WAV audio parts"
        )
    if kind == "text":
        _validate_utf8_text(text, MAX_TEXT_PART_CHARS, "Text media part")
        return 0

    max_kind_bytes = _max_bytes_for_kind(kind)
    if type(data) is not bytes or not 1 <= len(data) <= max_kind_bytes:
        raise LlamaMediaProtocolError("Binary media part violated per-kind byte bounds")
    return len(data)


def build_request(spec: LlamaMediaRequestSpec | dict) -> LlamaMediaPreparedRequest:
    """Validate and freeze exact local llama.cpp multimodal wire bytes."""
    if isinstance(spec, dict):
        allowed_keys = {
            "mode",
            "model",
            "parts",
            "system_prompt",
            "temperature",
            "max_tokens",
            "reasoning_budget",
            "state",
            "questions",
            "upstream_commit",
        }
        if (
            not set(spec.keys()) <= allowed_keys
            or "mode" not in spec
            or "model" not in spec
            or "parts" not in spec
        ):
            raise LlamaMediaProtocolError(
                "Local media request spec dict has missing or unknown keys"
            )
        mode = spec["mode"]
        model = spec["model"]
        raw_parts = spec["parts"]
        system_prompt = spec.get("system_prompt")
        temperature = spec.get("temperature", 0)
        max_tokens = spec.get("max_tokens", 128)
        reasoning_budget = spec.get("reasoning_budget")
        state = spec.get("state")
        questions = spec.get("questions")
        upstream_commit = spec.get("upstream_commit", UPSTREAM_LLAMA_COMMIT)
    elif isinstance(spec, LlamaMediaRequestSpec):
        mode = spec.mode
        model = spec.model
        raw_parts = spec.parts
        system_prompt = spec.system_prompt
        temperature = spec.temperature
        max_tokens = spec.max_tokens
        reasoning_budget = spec.reasoning_budget
        state = spec.state
        questions = spec.questions
        upstream_commit = spec.upstream_commit
    else:
        raise LlamaMediaProtocolError("build_request requires a LlamaMediaRequestSpec or dict")

    if mode not in {"converse_chat", "decisions_systemone"}:
        raise LlamaMediaProtocolError("Unsupported local media request mode")
    if upstream_commit != UPSTREAM_LLAMA_COMMIT:
        raise LlamaMediaProtocolError(
            "Local media request upstream_commit does not match pinned runtime"
        )
    if type(model) is not str or not _MODEL_RE.fullmatch(model):
        raise LlamaMediaProtocolError("Local media request requires an explicit model alias")

    max_parts = MAX_MEDIA_PARTS if mode == "converse_chat" else MAX_SYSTEMONE_FILES
    if not isinstance(raw_parts, (list, tuple)) or not 1 <= len(raw_parts) <= max_parts:
        raise LlamaMediaProtocolError(
            f"Local media request for {mode} requires 1..{max_parts} ordered parts"
        )

    # Enforce per-part and aggregate byte bounds BEFORE normalization, copying, or hashing.
    pre_media_bytes = 0
    for raw_part in raw_parts:
        pre_media_bytes += _prevalidate_raw_part_bounds(raw_part, mode=mode)
        if pre_media_bytes > MAX_TOTAL_MEDIA_BYTES:
            raise LlamaMediaProtocolError("Aggregate local media payload bytes exceeded 16 MiB cap")

    parts: list[LlamaMediaPart] = []
    provenance: list[LlamaMediaPartProvenance] = []
    total_media_bytes = 0
    last_seq_by_source: dict[tuple[str, str, str], int] = {}

    for idx, raw_part in enumerate(raw_parts):
        part = raw_part if isinstance(raw_part, LlamaMediaPart) else LlamaMediaPart(**raw_part)
        parts.append(part)
        if part.kind == "text":
            assert part.text is not None
            encoded_text = part.text.encode("utf-8")
            provenance.append(
                LlamaMediaPartProvenance(
                    index=idx,
                    kind="text",
                    mime_type=None,
                    source=None,
                    range=None,
                    track_id=None,
                    source_seq=None,
                    through_source_seq=None,
                    pts=None,
                    start=None,
                    end=None,
                    body_bytes=len(encoded_text),
                    body_sha256=hashlib.sha256(encoded_text).hexdigest(),
                )
            )
        else:
            assert part.data is not None and part.source is not None
            assert part.source_seq is not None and part.through_source_seq is not None
            assert part.body_sha256 is not None
            total_media_bytes += len(part.data)
            if total_media_bytes > MAX_TOTAL_MEDIA_BYTES:
                raise LlamaMediaProtocolError(
                    "Aggregate local media payload bytes exceeded 16 MiB cap"
                )
            source_key = (part.source.backend_id, part.source.receipt_id, part.source.sha256)
            prev_seq = last_seq_by_source.get(source_key)
            if prev_seq is not None and part.source_seq < prev_seq:
                raise LlamaMediaProtocolError(
                    "Local media parts for a source must be in non-decreasing sequence order"
                )
            last_seq_by_source[source_key] = part.through_source_seq
            provenance.append(
                LlamaMediaPartProvenance(
                    index=idx,
                    kind=part.kind,
                    mime_type=part.mime_type,
                    source=part.source,
                    range=part.range,
                    track_id=part.track_id,
                    source_seq=part.source_seq,
                    through_source_seq=part.through_source_seq,
                    pts=part.pts,
                    start=part.start,
                    end=part.end,
                    body_bytes=len(part.data),
                    body_sha256=part.body_sha256,
                )
            )

    if mode == "converse_chat":
        if state is not None or questions is not None:
            raise LlamaMediaProtocolError(
                "converse_chat request cannot include SystemOne state or questions"
            )
        if (
            type(temperature) not in {int, float}
            or not math.isfinite(temperature)
            or not 0.0 <= float(temperature) <= 2.0
        ):
            raise LlamaMediaProtocolError(
                "converse_chat temperature must be a finite number in [0, 2]"
            )
        checked_max_tokens = _validate_positive_int(max_tokens, "max_tokens", maximum=4096)
        if reasoning_budget is not None and (
            type(reasoning_budget) is not int or reasoning_budget != 0
        ):
            raise LlamaMediaProtocolError(
                "converse_chat reasoning_budget only supports 0 when specified"
            )
        if not any(part.kind != "text" for part in parts):
            raise LlamaMediaProtocolError(
                "converse_chat media request requires at least one media part"
            )

        messages: list[dict] = []
        if system_prompt is not None:
            messages.append(
                {
                    "role": "system",
                    "content": _validate_utf8_text(
                        system_prompt, MAX_SYSTEM_PROMPT_CHARS, "system_prompt"
                    ),
                }
            )

        content_items: list[dict] = []
        for part in parts:
            if part.kind == "text":
                content_items.append({"type": "text", "text": part.text})
            elif part.kind in {"image", "video_frame"}:
                assert part.data is not None
                b64 = base64.b64encode(part.data).decode("ascii")
                content_items.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    }
                )
            elif part.kind == "audio":
                assert part.data is not None
                b64 = base64.b64encode(part.data).decode("ascii")
                content_items.append(
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64, "format": "wav"},
                    }
                )
            elif part.kind == "video_clip":
                assert part.data is not None
                b64 = base64.b64encode(part.data).decode("ascii")
                content_items.append(
                    {
                        "type": "input_video",
                        "input_video": {"data": b64},
                    }
                )

        messages.append({"role": "user", "content": content_items})

        body: dict = {
            "model": model,
            "temperature": temperature,
            "max_tokens": checked_max_tokens,
            "stream": False,
            "messages": messages,
        }
        if reasoning_budget is not None:
            body["reasoning_budget"] = reasoning_budget
        path: Literal["/v1/chat/completions", "/v1/systemone"] = "/v1/chat/completions"
        validated_questions = None

    else:
        if (
            system_prompt is not None
            or reasoning_budget is not None
            or temperature != 0
            or max_tokens != 128
        ):
            raise LlamaMediaProtocolError(
                "decisions_systemone request cannot include chat generation parameters"
            )
        state_text = _validate_utf8_text(state, MAX_STATE_CHARS, "SystemOne state")
        validated_questions = _validate_questions(questions)
        if len(parts) > MAX_SYSTEMONE_FILES:
            raise LlamaMediaProtocolError(
                "decisions_systemone supports at most 8 media files per request"
            )
        files: list[str] = []
        for part in parts:
            if part.kind in {"image", "video_frame"}:
                assert part.data is not None
                b64 = base64.b64encode(part.data).decode("ascii")
                files.append(f"data:image/png;base64,{b64}")
            elif part.kind == "audio":
                assert part.data is not None
                b64 = base64.b64encode(part.data).decode("ascii")
                files.append(f"data:audio/wav;base64,{b64}")
            else:
                raise LlamaMediaProtocolError(
                    "decisions_systemone files only accept PNG image/video_frame and PCM WAV audio parts"
                )
        body = {
            "model": model,
            "state": state_text,
            "files": files,
            "questions": copy.deepcopy(validated_questions),
        }
        path = "/v1/systemone"

    raw_body = _compact_json(body)
    if len(raw_body) > MAX_REQUEST_WIRE_BYTES:
        raise LlamaMediaProtocolError("Encoded local media request exceeded wire byte cap")
    return LlamaMediaPreparedRequest(
        mode=mode,
        model=model,
        path=path,
        query="autoload=false",
        upstream_commit=upstream_commit,
        body=copy.deepcopy(body),
        raw_body=raw_body,
        request_sha256=hashlib.sha256(raw_body).hexdigest(),
        provenance=tuple(provenance),
        questions=copy.deepcopy(validated_questions),
    )


def _validate_provenance_entry(
    prov: object,
    expected_index: int,
    *,
    mode: Literal["converse_chat", "decisions_systemone"],
    last_seq_by_source: dict[tuple[str, str, str], int],
) -> int:
    if not isinstance(prov, LlamaMediaPartProvenance):
        raise LlamaMediaProtocolError("Prepared request provenance entry must be typed")
    if type(prov.index) is not int or prov.index != expected_index:
        raise LlamaMediaProtocolError("Prepared request provenance index is out of order")
    if prov.kind not in _PART_KINDS:
        raise LlamaMediaProtocolError("Prepared request provenance kind is invalid")
    if mode == "decisions_systemone" and prov.kind not in _SYSTEMONE_PART_KINDS:
        raise LlamaMediaProtocolError(
            "SystemOne prepared request provenance contains an unsupported part kind"
        )
    _validate_sha256(prov.body_sha256, "Provenance body_sha256")
    if prov.kind == "text":
        _validate_positive_int(
            prov.body_bytes, "Text provenance body_bytes", maximum=MAX_TEXT_PART_CHARS * 4
        )
        if any(
            val is not None
            for val in (
                prov.mime_type,
                prov.source,
                prov.range,
                prov.track_id,
                prov.source_seq,
                prov.through_source_seq,
                prov.pts,
                prov.start,
                prov.end,
            )
        ):
            raise LlamaMediaProtocolError("Text provenance entry cannot carry binary metadata")
        return 0

    max_kind_bytes = _max_bytes_for_kind(prov.kind)
    _validate_positive_int(prov.body_bytes, "Binary provenance body_bytes", maximum=max_kind_bytes)
    if type(prov.source) is not MediaNativeRef:
        raise LlamaMediaProtocolError("Binary provenance entry requires source MediaNativeRef")
    if type(prov.start) is not MediaQ or type(prov.end) is not MediaQ:
        raise LlamaMediaProtocolError("Binary provenance entry requires MediaQ start and end")
    source_seq = _validate_positive_int(prov.source_seq, "Provenance source_seq")
    through_seq = _validate_positive_int(prov.through_source_seq, "Provenance through_source_seq")
    if through_seq < source_seq:
        raise LlamaMediaProtocolError("Provenance through_source_seq preceded source_seq")
    try:
        _interval(
            prov.start,
            prov.end,
            10 if prov.kind == "video_clip" else (1 if prov.kind == "audio" else None),
        )
    except Exception as exc:
        raise LlamaMediaProtocolError("Provenance interval violated contract bounds") from exc

    if prov.kind == "video_clip":
        if (
            prov.mime_type != "video/mp4"
            or type(prov.range) is not MediaNativeRef
            or prov.track_id is not None
            or prov.pts is not None
        ):
            raise LlamaMediaProtocolError("video_clip provenance metadata is invalid")
    else:
        if (
            prov.range is not None
            or source_seq != through_seq
            or type(prov.track_id) is not str
            or not _ID_RE.fullmatch(prov.track_id)
            or type(prov.pts) is not MediaSourcePTS
        ):
            raise LlamaMediaProtocolError("Single-packet provenance metadata is invalid")
        if prov.kind in {"image", "video_frame"}:
            if (
                prov.mime_type != "image/png"
                or prov.pts.frame_index is None
                or (prov.kind == "image" and prov.pts.frame_index != 0)
            ):
                raise LlamaMediaProtocolError("Image/frame provenance metadata is invalid")
        elif prov.kind == "audio":
            if prov.mime_type != "audio/wav" or prov.pts.sample_count is None:
                raise LlamaMediaProtocolError("Audio provenance metadata is invalid")

    source_key = (prov.source.backend_id, prov.source.receipt_id, prov.source.sha256)
    prev_seq = last_seq_by_source.get(source_key)
    if prev_seq is not None and source_seq < prev_seq:
        raise LlamaMediaProtocolError("Prepared request provenance sequence order is invalid")
    last_seq_by_source[source_key] = through_seq
    return prov.body_bytes


def _validate_prepared_request(
    prepared: object,
    *,
    expected_mode: Literal["converse_chat", "decisions_systemone"] | None = None,
    expected_model: str | None = None,
    expected_commit: str = UPSTREAM_LLAMA_COMMIT,
) -> LlamaMediaPreparedRequest:
    """Revalidate a prepared request's path, query, commit, wire bytes, and provenance."""
    if not isinstance(prepared, LlamaMediaPreparedRequest):
        raise LlamaMediaProtocolError("Expected a LlamaMediaPreparedRequest")
    if prepared.mode not in {"converse_chat", "decisions_systemone"}:
        raise LlamaMediaProtocolError("Prepared media request mode is invalid")
    if expected_mode is not None and prepared.mode != expected_mode:
        raise LlamaMediaProtocolError("Prepared media request mode did not match bound provider")
    if type(prepared.model) is not str or _MODEL_RE.fullmatch(prepared.model) is None:
        raise LlamaMediaProtocolError("Prepared media request model alias is invalid")
    if expected_model is not None and prepared.model != expected_model:
        raise LlamaMediaProtocolError("Prepared media request model did not match bound provider")
    if (
        type(prepared.upstream_commit) is not str
        or prepared.upstream_commit != expected_commit
        or prepared.upstream_commit != UPSTREAM_LLAMA_COMMIT
    ):
        raise LlamaMediaProtocolError("Prepared media request upstream_commit is not pinned")

    expected_path = "/v1/chat/completions" if prepared.mode == "converse_chat" else "/v1/systemone"
    if (
        type(prepared.path) is not str
        or prepared.path != expected_path
        or "://" in prepared.path
        or prepared.path.startswith("//")
        or "\\" in prepared.path
        or ".." in prepared.path
        or "?" in prepared.path
        or "#" in prepared.path
    ):
        raise LlamaMediaProtocolError("Prepared media request path is invalid for mode")
    if type(prepared.query) is not str or prepared.query != "autoload=false":
        raise LlamaMediaProtocolError("Prepared media request query must be 'autoload=false'")

    if type(prepared.raw_body) is not bytes or not (
        1 <= len(prepared.raw_body) <= MAX_REQUEST_WIRE_BYTES
    ):
        raise LlamaMediaProtocolError("Prepared media request raw_body violated byte bounds")
    _validate_sha256(prepared.request_sha256, "Prepared media request_sha256")
    if hashlib.sha256(prepared.raw_body).hexdigest() != prepared.request_sha256:
        raise LlamaMediaProtocolError("Prepared media request_sha256 did not match raw_body digest")
    if type(prepared.body) is not dict or _compact_json(prepared.body) != prepared.raw_body:
        raise LlamaMediaProtocolError(
            "Prepared media request body was mutated or did not match raw_body"
        )
    decoded_body = _strict_json_object(
        prepared.raw_body, MAX_REQUEST_WIRE_BYTES, "Prepared local media request"
    )
    if decoded_body != prepared.body or _compact_json(decoded_body) != prepared.raw_body:
        raise LlamaMediaProtocolError("Prepared media request JSON encoding is not canonical")

    max_parts = MAX_MEDIA_PARTS if prepared.mode == "converse_chat" else MAX_SYSTEMONE_FILES
    if type(prepared.provenance) is not tuple or not (1 <= len(prepared.provenance) <= max_parts):
        raise LlamaMediaProtocolError(
            f"Prepared media request provenance must contain 1..{max_parts} entries"
        )

    total_media_bytes = 0
    last_seq_by_source: dict[tuple[str, str, str], int] = {}
    for idx, prov in enumerate(prepared.provenance):
        total_media_bytes += _validate_provenance_entry(
            prov, idx, mode=prepared.mode, last_seq_by_source=last_seq_by_source
        )
        if total_media_bytes > MAX_TOTAL_MEDIA_BYTES:
            raise LlamaMediaProtocolError("Prepared media request exceeded 16 MiB total media cap")

    if prepared.mode == "converse_chat":
        if prepared.questions is not None:
            raise LlamaMediaProtocolError("converse_chat prepared request cannot carry questions")
        allowed_keys = {
            "model",
            "temperature",
            "max_tokens",
            "stream",
            "messages",
            "reasoning_budget",
        }
        required_keys = {"model", "temperature", "max_tokens", "stream", "messages"}
        if not required_keys <= set(decoded_body.keys()) <= allowed_keys:
            raise LlamaMediaProtocolError("converse_chat body keys are invalid")
        if decoded_body["model"] != prepared.model or decoded_body["stream"] is not False:
            raise LlamaMediaProtocolError("converse_chat body model or stream flag is invalid")
        temp = decoded_body["temperature"]
        if (
            type(temp) not in {int, float}
            or not math.isfinite(temp)
            or not 0.0 <= float(temp) <= 2.0
        ):
            raise LlamaMediaProtocolError("converse_chat body temperature is invalid")
        _validate_positive_int(decoded_body["max_tokens"], "max_tokens", maximum=4096)
        if "reasoning_budget" in decoded_body and (
            type(decoded_body["reasoning_budget"]) is not int
            or decoded_body["reasoning_budget"] != 0
        ):
            raise LlamaMediaProtocolError("converse_chat body reasoning_budget is invalid")
        if not any(prov.kind != "text" for prov in prepared.provenance):
            raise LlamaMediaProtocolError(
                "converse_chat prepared request requires at least one media part"
            )

        messages = decoded_body["messages"]
        if type(messages) is not list or len(messages) not in {1, 2}:
            raise LlamaMediaProtocolError("converse_chat messages list is invalid")
        if len(messages) == 2:
            sys_msg = messages[0]
            if (
                type(sys_msg) is not dict
                or set(sys_msg.keys()) != {"role", "content"}
                or sys_msg.get("role") != "system"
            ):
                raise LlamaMediaProtocolError("converse_chat system message is invalid")
            _validate_utf8_text(sys_msg.get("content"), MAX_SYSTEM_PROMPT_CHARS, "system_prompt")
        user_msg = messages[-1]
        if (
            type(user_msg) is not dict
            or set(user_msg.keys()) != {"role", "content"}
            or user_msg.get("role") != "user"
            or type(user_msg.get("content")) is not list
            or len(user_msg["content"]) != len(prepared.provenance)
        ):
            raise LlamaMediaProtocolError("converse_chat user message content is invalid")

        for prov, item in zip(prepared.provenance, user_msg["content"], strict=True):
            if type(item) is not dict:
                raise LlamaMediaProtocolError("converse_chat content item must be a dict")
            if prov.kind == "text":
                if set(item.keys()) != {"type", "text"} or item.get("type") != "text":
                    raise LlamaMediaProtocolError("converse_chat text item is invalid")
                text_val = _validate_utf8_text(
                    item.get("text"), MAX_TEXT_PART_CHARS, "Text media part"
                )
                encoded_text = text_val.encode("utf-8")
                if (
                    len(encoded_text) != prov.body_bytes
                    or hashlib.sha256(encoded_text).hexdigest() != prov.body_sha256
                ):
                    raise LlamaMediaProtocolError(
                        "converse_chat text item did not match provenance"
                    )
            elif prov.kind in {"image", "video_frame"}:
                img_obj = item.get("image_url")
                if (
                    set(item.keys()) != {"type", "image_url"}
                    or item.get("type") != "image_url"
                    or type(img_obj) is not dict
                    or set(img_obj.keys()) != {"url"}
                    or type(img_obj.get("url")) is not str
                    or not img_obj["url"].startswith("data:image/png;base64,")
                ):
                    raise LlamaMediaProtocolError("converse_chat image_url item is invalid")
                raw_bytes = _decode_strict_base64(
                    img_obj["url"][len("data:image/png;base64,") :],
                    MAX_IMAGE_BYTES,
                    "converse_chat image_url",
                )
                if (
                    len(raw_bytes) != prov.body_bytes
                    or hashlib.sha256(raw_bytes).hexdigest() != prov.body_sha256
                ):
                    raise LlamaMediaProtocolError(
                        "converse_chat image bytes did not match provenance"
                    )
                _validate_png_bytes(raw_bytes)
            elif prov.kind == "audio":
                aud_obj = item.get("input_audio")
                if (
                    set(item.keys()) != {"type", "input_audio"}
                    or item.get("type") != "input_audio"
                    or type(aud_obj) is not dict
                    or set(aud_obj.keys()) != {"data", "format"}
                    or aud_obj.get("format") != "wav"
                ):
                    raise LlamaMediaProtocolError("converse_chat input_audio item is invalid")
                raw_bytes = _decode_strict_base64(
                    aud_obj.get("data"), MAX_AUDIO_BYTES, "converse_chat input_audio"
                )
                if (
                    len(raw_bytes) != prov.body_bytes
                    or hashlib.sha256(raw_bytes).hexdigest() != prov.body_sha256
                ):
                    raise LlamaMediaProtocolError(
                        "converse_chat audio bytes did not match provenance"
                    )
                _validate_wav_bytes(raw_bytes)
            elif prov.kind == "video_clip":
                vid_obj = item.get("input_video")
                if (
                    set(item.keys()) != {"type", "input_video"}
                    or item.get("type") != "input_video"
                    or type(vid_obj) is not dict
                    or set(vid_obj.keys()) != {"data"}
                ):
                    raise LlamaMediaProtocolError("converse_chat input_video item is invalid")
                raw_bytes = _decode_strict_base64(
                    vid_obj.get("data"), MAX_VIDEO_CLIP_BYTES, "converse_chat input_video"
                )
                if (
                    len(raw_bytes) != prov.body_bytes
                    or hashlib.sha256(raw_bytes).hexdigest() != prov.body_sha256
                ):
                    raise LlamaMediaProtocolError(
                        "converse_chat video_clip bytes did not match provenance"
                    )
                _validate_mp4_bytes(raw_bytes)
        validated_questions = None

    else:
        if set(decoded_body.keys()) != {"model", "state", "files", "questions"}:
            raise LlamaMediaProtocolError("decisions_systemone body keys are invalid")
        if decoded_body["model"] != prepared.model:
            raise LlamaMediaProtocolError("decisions_systemone body model did not match")
        _validate_utf8_text(decoded_body["state"], MAX_STATE_CHARS, "SystemOne state")
        validated_questions = _validate_questions(decoded_body["questions"])
        if type(prepared.questions) is not dict or prepared.questions != validated_questions:
            raise LlamaMediaProtocolError(
                "decisions_systemone prepared questions did not match body"
            )
        files = decoded_body["files"]
        if (
            type(files) is not list
            or not 1 <= len(files) <= MAX_SYSTEMONE_FILES
            or len(files) != len(prepared.provenance)
        ):
            raise LlamaMediaProtocolError(
                "decisions_systemone files list violated 1..8 count or provenance match"
            )
        for prov, uri in zip(prepared.provenance, files, strict=True):
            if type(uri) is not str:
                raise LlamaMediaProtocolError("decisions_systemone file entry must be a data URI")
            if prov.kind in {"image", "video_frame"}:
                prefix = "data:image/png;base64,"
                if not uri.startswith(prefix):
                    raise LlamaMediaProtocolError(
                        "decisions_systemone image file requires PNG data URI"
                    )
                raw_bytes = _decode_strict_base64(
                    uri[len(prefix) :], MAX_IMAGE_BYTES, "decisions_systemone PNG file"
                )
                if (
                    len(raw_bytes) != prov.body_bytes
                    or hashlib.sha256(raw_bytes).hexdigest() != prov.body_sha256
                ):
                    raise LlamaMediaProtocolError(
                        "decisions_systemone PNG file did not match provenance"
                    )
                _validate_png_bytes(raw_bytes)
            elif prov.kind == "audio":
                prefix = "data:audio/wav;base64,"
                if not uri.startswith(prefix):
                    raise LlamaMediaProtocolError(
                        "decisions_systemone audio file requires WAV data URI"
                    )
                raw_bytes = _decode_strict_base64(
                    uri[len(prefix) :], MAX_AUDIO_BYTES, "decisions_systemone WAV file"
                )
                if (
                    len(raw_bytes) != prov.body_bytes
                    or hashlib.sha256(raw_bytes).hexdigest() != prov.body_sha256
                ):
                    raise LlamaMediaProtocolError(
                        "decisions_systemone WAV file did not match provenance"
                    )
                _validate_wav_bytes(raw_bytes)
            else:
                raise LlamaMediaProtocolError(
                    "decisions_systemone provenance contains unsupported file kind"
                )

    return LlamaMediaPreparedRequest(
        mode=prepared.mode,
        model=prepared.model,
        path=expected_path,
        query="autoload=false",
        upstream_commit=prepared.upstream_commit,
        body=decoded_body,
        raw_body=prepared.raw_body,
        request_sha256=prepared.request_sha256,
        provenance=prepared.provenance,
        questions=copy.deepcopy(validated_questions),
    )


def parse_response(
    raw: bytes,
    prepared: LlamaMediaPreparedRequest,
) -> LlamaMediaChatResult | LlamaMediaSystemOneResult:
    """Validate raw response bytes against the exact prepared local media request."""
    validated_prepared = _validate_prepared_request(prepared)

    if validated_prepared.mode == "converse_chat":
        payload = _strict_json_object(raw, MAX_CHAT_RESPONSE_BYTES, "Converse media response")
        if payload.get("error") is not None:
            raise LlamaMediaProtocolError("Converse media response returned an error payload")
        observed_model = payload.get("model")
        if (
            type(observed_model) is not str
            or not observed_model
            or _MODEL_RE.fullmatch(observed_model) is None
            or observed_model != validated_prepared.model
        ):
            raise LlamaMediaProtocolError(
                "Converse media response requires an explicit model matching selected alias"
            )
        choices = payload.get("choices")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise LlamaMediaProtocolError(
                "Converse media response requires exactly one choice object"
            )
        choice = choices[0]
        if "index" in choice and (type(choice["index"]) is not int or choice["index"] != 0):
            raise LlamaMediaProtocolError("Converse media response choice index must be 0")
        if any(choice.get(key) is not None for key in ("tool_calls", "function_call", "refusal")):
            raise LlamaMediaProtocolError(
                "Converse media response cannot emit tool calls or refusals"
            )
        if choice.get("finish_reason") != "stop":
            raise LlamaMediaProtocolError(
                "Converse media response requires terminal finish_reason='stop'"
            )
        message = choice.get("message")
        if type(message) is not dict:
            raise LlamaMediaProtocolError(
                "Converse media response choice requires a message object"
            )
        role = message.get("role")
        if role is not None and role != "assistant":
            raise LlamaMediaProtocolError(
                "Converse media response message role must be 'assistant'"
            )
        if any(
            message.get(key) is not None
            for key in ("tool_calls", "function_call", "refusal", "audio")
        ):
            raise LlamaMediaProtocolError(
                "Converse media response message contains unsupported output fields"
            )
        content = message.get("content")
        _validate_utf8_text(content, MAX_CHAT_OUTPUT_CHARS, "Converse media response content")
        if len(content.encode("utf-8")) > MAX_CHAT_OUTPUT_BYTES:
            raise LlamaMediaProtocolError("Converse media response content exceeded UTF-8 byte cap")

        usage_obj = payload.get("usage")
        parsed_usage: dict[str, int] | None = None
        if usage_obj is not None:
            if type(usage_obj) is not dict:
                raise LlamaMediaProtocolError("Converse media response usage must be an object")
            prompt_tokens = _validate_nonnegative_int(
                usage_obj.get("prompt_tokens"), "usage.prompt_tokens"
            )
            completion_tokens = _validate_nonnegative_int(
                usage_obj.get("completion_tokens"), "usage.completion_tokens"
            )
            total_tokens = _validate_nonnegative_int(
                usage_obj.get("total_tokens"), "usage.total_tokens"
            )
            if total_tokens != prompt_tokens + completion_tokens:
                raise LlamaMediaProtocolError(
                    "Converse media response usage token totals are inconsistent"
                )
            parsed_usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }

        return LlamaMediaChatResult(
            mode="converse_chat",
            model=observed_model,
            text=content,
            finish_reason="stop",
            usage=parsed_usage,
            request_sha256=validated_prepared.request_sha256,
            response_sha256=hashlib.sha256(raw).hexdigest(),
            provenance=validated_prepared.provenance,
        )

    assert validated_prepared.questions is not None
    payload = _strict_json_object(raw, MAX_SYSTEMONE_RESPONSE_BYTES, "SystemOne media response")
    if set(payload.keys()) != {"model", "answers", "usage"}:
        raise LlamaMediaProtocolError(
            "SystemOne media response keys did not match {model, answers, usage}"
        )
    observed_model = payload.get("model")
    if (
        type(observed_model) is not str
        or not observed_model
        or _MODEL_RE.fullmatch(observed_model) is None
        or observed_model != validated_prepared.model
    ):
        raise LlamaMediaProtocolError(
            "SystemOne media response requires an explicit model matching selected alias"
        )
    usage = payload["usage"]
    if type(usage) is not dict or set(usage.keys()) != {"input_tokens", "output_tokens"}:
        raise LlamaMediaProtocolError("SystemOne media response usage was malformed")
    input_tokens = _validate_positive_int(usage["input_tokens"], "usage.input_tokens")
    output_tokens = _validate_nonnegative_int(
        usage["output_tokens"], "usage.output_tokens", maximum=0
    )
    answers = payload["answers"]
    if type(answers) is not dict or set(answers.keys()) != set(validated_prepared.questions.keys()):
        raise LlamaMediaProtocolError(
            "SystemOne media response answers did not match requested questions"
        )

    validated_answers: dict[str, dict] = {}
    for name, qspec in validated_prepared.questions.items():
        entry = answers.get(name)
        if type(entry) is not dict:
            raise LlamaMediaProtocolError(f"SystemOne answer for {name} must be an object")
        if qspec["type"] == "choice":
            labels = list(qspec["criteria"].keys())
            if (
                set(entry.keys()) != {"type", "choice", "probabilities", "confidence"}
                or entry["type"] != "choice"
                or type(entry["choice"]) is not str
                or entry["choice"] not in labels
                or type(entry["probabilities"]) is not dict
                or set(entry["probabilities"].keys()) != set(labels)
                or not all(_validate_probability(v) for v in entry["probabilities"].values())
                or not _validate_probability(entry["confidence"])
            ):
                raise LlamaMediaProtocolError(f"SystemOne choice answer for {name} was malformed")
            probs = {k: float(v) for k, v in entry["probabilities"].items()}
            choice = entry["choice"]
            selected = probs[choice]
            conf = float(entry["confidence"])
            if (
                abs(sum(probs.values()) - 1.0) > len(labels) * 0.0001 + 1e-6
                or selected + 0.0001 < max(probs.values())
                or abs(conf - selected) > 0.001
            ):
                raise LlamaMediaProtocolError(
                    f"SystemOne choice probabilities for {name} were inconsistent"
                )
            validated_answers[name] = {
                "type": "choice",
                "choice": choice,
                "probabilities": probs,
                "confidence": conf,
            }
        elif qspec["type"] == "noul":
            if (
                set(entry.keys()) != {"type", "noul"}
                or entry["type"] != "noul"
                or not _validate_probability(entry["noul"])
            ):
                raise LlamaMediaProtocolError(f"SystemOne noul answer for {name} was malformed")
            validated_answers[name] = {
                "type": "noul",
                "noul": float(entry["noul"]),
            }

    return LlamaMediaSystemOneResult(
        mode="decisions_systemone",
        model=observed_model,
        answers=validated_answers,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        request_sha256=validated_prepared.request_sha256,
        response_sha256=hashlib.sha256(raw).hexdigest(),
        provenance=validated_prepared.provenance,
    )


class _TrackedOwnedHTTPStream(_OwnedHTTPStream):
    """Track whether the single owned HTTP response stream confirmed close."""

    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        super().__init__(stream)
        self.close_attempts = 0
        self.sole_closed = False

    async def aclose(self) -> None:
        if self.cleanup is None:
            self.close_attempts += 1
        try:
            await super().aclose()
        except ProviderCleanupError:
            self.sole_closed = False
            raise
        except BaseException:
            if (
                self.cleanup is not None
                and self.cleanup.done()
                and not self.cleanup.cancelled()
                and self.cleanup.exception() is None
                and self.close_attempts == 1
            ):
                self.sole_closed = True
            raise
        else:
            if self.close_attempts == 1:
                self.sole_closed = True


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _validate_registration_against_request(
    registration: object,
    prepared: LlamaMediaPreparedRequest,
) -> LlamaExchangeRegistration:
    if not isinstance(registration, LlamaExchangeRegistration):
        raise LlamaMediaProtocolError(
            "exchange_owner_bridge.register_exchange must return a LlamaExchangeRegistration"
        )
    body_owner = registration.body_owner
    if (
        body_owner.body != {"kind": "wire_buffer"}
        or body_owner.request_sha256 != prepared.request_sha256
        or body_owner.body_bytes != len(prepared.raw_body)
        or body_owner.body_sha256 != prepared.request_sha256
    ):
        raise LlamaMediaProtocolError(
            "Registered LocalBodyOwner does not match encoded request wire_buffer"
        )
    owner_refs = (
        registration.original_owner,
        registration.exchange_owner,
        registration.body_owner_ref,
    )
    if len({(r.backend_id, r.receipt_id, r.sha256) for r in owner_refs}) != 3:
        raise LlamaMediaProtocolError(
            "Registered original_owner, exchange_owner, and body_owner_ref must be distinct"
        )
    total_expected_obs = (
        2 + len(body_owner.reader_owner_ids) + len(registration.publication_owner_ids) + 1
    )
    if total_expected_obs > MAX_EXCHANGE_OBSERVATIONS:
        raise LlamaMediaProtocolError("Registered exchange inventory exceeds 64 observation cap")
    return registration


def _verify_settlement_bundle(
    bundle: object,
    registration: LlamaExchangeRegistration,
    prepared: LlamaMediaPreparedRequest,
    *,
    transport_closed: bool,
    wire_buffer_retired: bool,
    requested_outcome: Literal["completed", "cancelled", "failed", "not_started"] | None,
) -> LlamaLocalExchangeSettlement:
    """Verify native bridge observations against actual local transport/buffer custody."""
    body_owner = registration.body_owner

    def _unknown_fallback() -> LlamaLocalExchangeSettlement:
        return LlamaLocalExchangeSettlement(
            exchange_id=body_owner.exchange_id,
            local_task_id=registration.local_task_id,
            transport_owner_id=registration.transport_owner_id,
            body_owner_id=body_owner.body_owner_id,
            request_sha256=prepared.request_sha256,
            binding_sha256=registration.binding_sha256,
            model=prepared.model,
            upstream_commit=prepared.upstream_commit,
            local_task_joined=False,
            transport_sole_closed=transport_closed,
            body_settled=False,
            all_readers_settled=False,
            publications_settled=False,
            state="unknown",
            outcome=None,
            body_storage=dict(body_owner.body),
            body_proof="none",
            liability={"kind": "local_no_remote"},
            body_owner_record=body_owner,
            body_close_record=None,
            cleanup_receipt=None,
        )

    if not isinstance(bundle, LlamaExchangeSettlementBundle):
        return _unknown_fallback()

    body_close = bundle.body_close
    if (
        body_close.body_owner != registration.body_owner_ref
        or body_close.request_sha256 != prepared.request_sha256
        or body_close.binding_sha256 != registration.binding_sha256
        or body_close.proof == "original_body_fd_closed"
    ):
        return _unknown_fallback()

    actual_body_settled = bool(
        wire_buffer_retired
        and body_close.state == "settled"
        and body_close.proof == "wire_buffer_retired"
    )
    actual_body_proof: Literal["wire_buffer_retired", "original_body_fd_closed", "none"] = (
        "wire_buffer_retired" if actual_body_settled else "none"
    )

    task_obs: list[LocalCloseObservation] = []
    transport_obs: list[LocalCloseObservation] = []
    reader_obs: dict[str, LocalCloseObservation] = {}
    pub_obs: dict[str, LocalCloseObservation] = {}

    for obs in bundle.component_observations:
        if (
            obs.exchange_owner != registration.exchange_owner
            or obs.request_sha256 != prepared.request_sha256
            or obs.binding_sha256 != registration.binding_sha256
            or obs.component == "body"
        ):
            return _unknown_fallback()
        if obs.component == "local_task":
            task_obs.append(obs)
        elif obs.component == "transport":
            transport_obs.append(obs)
        elif obs.component == "reader":
            if obs.owner_id in reader_obs:
                return _unknown_fallback()
            reader_obs[obs.owner_id] = obs
        elif obs.component == "publication":
            if obs.owner_id in pub_obs:
                return _unknown_fallback()
            pub_obs[obs.owner_id] = obs
        else:
            return _unknown_fallback()

    if (
        len(task_obs) != 1
        or task_obs[0].owner_id != registration.local_task_id
        or len(transport_obs) != 1
        or transport_obs[0].owner_id != registration.transport_owner_id
        or set(reader_obs.keys()) != set(body_owner.reader_owner_ids)
        or set(pub_obs.keys()) != set(registration.publication_owner_ids)
    ):
        return _unknown_fallback()

    actual_task_joined = bool(task_obs[0].state == "settled" and task_obs[0].proof == "task_join")
    actual_transport_closed = bool(
        transport_closed
        and transport_obs[0].state == "settled"
        and transport_obs[0].proof == "sole_transport_close"
    )
    actual_readers_settled = bool(
        wire_buffer_retired
        and all(
            obs.state == "settled" and obs.proof == "reader_join" for obs in reader_obs.values()
        )
    )
    actual_publications_settled = bool(
        len(pub_obs) >= 1
        and all(
            obs.state == "settled" and obs.proof == "publication_settlement"
            for obs in pub_obs.values()
        )
    )

    all_five = (
        actual_task_joined
        and actual_transport_closed
        and actual_body_settled
        and actual_readers_settled
        and actual_publications_settled
    )
    expected_state: Literal["settled", "unknown"] = (
        "settled" if (all_five and requested_outcome is not None) else "unknown"
    )
    expected_outcome = requested_outcome if expected_state == "settled" else None

    cleanup = bundle.cleanup
    if (
        cleanup.operation_id != registration.operation_id
        or cleanup.child_instance != registration.child_instance
        or cleanup.binding_sha256 != registration.binding_sha256
        or cleanup.original_owner != registration.original_owner
        or cleanup.exchange_owner != registration.exchange_owner
        or cleanup.body_owner != registration.body_owner_ref
        or cleanup.body_observation != bundle.body_observation
        or cleanup.observations != bundle.observation_refs
        or cleanup.liability != {"kind": "local_no_remote"}
        or cleanup.local_task_joined != actual_task_joined
        or cleanup.transport_sole_closed != actual_transport_closed
        or cleanup.body_settled != actual_body_settled
        or cleanup.all_readers_settled != actual_readers_settled
        or cleanup.publications_settled != actual_publications_settled
        or cleanup.state != expected_state
        or cleanup.outcome != expected_outcome
    ):
        return LlamaLocalExchangeSettlement(
            exchange_id=body_owner.exchange_id,
            local_task_id=registration.local_task_id,
            transport_owner_id=registration.transport_owner_id,
            body_owner_id=body_owner.body_owner_id,
            request_sha256=prepared.request_sha256,
            binding_sha256=registration.binding_sha256,
            model=prepared.model,
            upstream_commit=prepared.upstream_commit,
            local_task_joined=actual_task_joined,
            transport_sole_closed=actual_transport_closed,
            body_settled=actual_body_settled,
            all_readers_settled=actual_readers_settled,
            publications_settled=actual_publications_settled,
            state="unknown",
            outcome=None,
            body_storage=dict(body_owner.body),
            body_proof=actual_body_proof,
            liability={"kind": "local_no_remote"},
            body_owner_record=body_owner,
            body_close_record=body_close if actual_body_settled else None,
            cleanup_receipt=None,
        )

    return LlamaLocalExchangeSettlement(
        exchange_id=body_owner.exchange_id,
        local_task_id=registration.local_task_id,
        transport_owner_id=registration.transport_owner_id,
        body_owner_id=body_owner.body_owner_id,
        request_sha256=prepared.request_sha256,
        binding_sha256=registration.binding_sha256,
        model=prepared.model,
        upstream_commit=prepared.upstream_commit,
        local_task_joined=actual_task_joined,
        transport_sole_closed=actual_transport_closed,
        body_settled=actual_body_settled,
        all_readers_settled=actual_readers_settled,
        publications_settled=actual_publications_settled,
        state=expected_state,
        outcome=expected_outcome,
        body_storage=dict(body_owner.body),
        body_proof=actual_body_proof,
        liability=dict(cleanup.liability),
        body_owner_record=body_owner,
        body_close_record=body_close,
        cleanup_receipt=cleanup,
    )


class NativeLlamaMediaProvider:
    """Loopback-only native llama.cpp multimodal adapter with local_exchange_v2 custody."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        mode: Literal["converse_chat", "decisions_systemone"] = "converse_chat",
        timeout: float = 30.0,
        api_key: str = "",
        allow_cloud: bool = False,
        upstream_commit: str = UPSTREAM_LLAMA_COMMIT,
        transport: httpx.AsyncBaseTransport | None = None,
        exchange_owner_bridge: object | None = None,
    ) -> None:
        root = _validate_loopback_endpoint(base_url, allow_cloud=allow_cloud)
        if type(model) is not str or not _MODEL_RE.fullmatch(model):
            raise ProviderError("Use an explicit native llama media model alias")
        if mode not in {"converse_chat", "decisions_systemone"}:
            raise ProviderError("Unsupported native llama media mode")
        if upstream_commit != UPSTREAM_LLAMA_COMMIT:
            raise ProviderError("Unsupported upstream llama.cpp commit pin")
        if (
            type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or not 0 < float(timeout) <= 120
        ):
            raise ProviderError("Invalid native llama media timeout")
        if (
            type(api_key) is not str
            or len(api_key) > 8192
            or any(ch in api_key for ch in "\x00\r\n")
        ):
            raise ProviderError("Invalid native llama media API key")

        self.base_url = root
        self.model = model
        self.mode = mode
        self.timeout = float(timeout)
        self.allow_cloud = False
        self.upstream_commit = upstream_commit
        self._exchange_owner_bridge = exchange_owner_bridge
        self.info = ProviderInfo(
            provider="native_llama_media",
            model=model,
            local=True,
            streaming=False,
        )
        headers = {"Accept-Encoding": "identity"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.http = RequestBoundAsyncClient(
            base_url=root + "/",
            headers=headers,
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._original_http = self.http
        self._original_info = self.info
        self._declaration = self._policy()
        self._headers = tuple(self.http.headers.raw)
        self._origin = str(self.http.base_url)
        self._expected_path = (
            "v1/chat/completions?autoload=false"
            if mode == "converse_chat"
            else "v1/systemone?autoload=false"
        )
        self._transport = httpx.AsyncClient._transport_for_url(
            self.http, self.http.base_url.join(self._expected_path)
        )
        self._cleanup: asyncio.Task[None] | None = None
        self._exchange_seq = 0
        self._exchanges: list[LlamaLocalExchangeSettlement] = []
        self._exchanges_by_id: dict[str, LlamaLocalExchangeSettlement] = {}
        self.last_exchange: LlamaLocalExchangeSettlement | None = None

    def _policy(self) -> tuple[tuple[type, object], ...]:
        return tuple(
            (type(value), value)
            for value in (
                self.base_url,
                self.model,
                self.mode,
                self.allow_cloud,
                self.timeout,
                self.upstream_commit,
            )
        )

    @property
    def exchanges(self) -> tuple[LlamaLocalExchangeSettlement, ...]:
        return tuple(self._exchanges)

    @property
    def exchanges_by_id(self) -> MappingProxyType[str, LlamaLocalExchangeSettlement]:
        return MappingProxyType(dict(self._exchanges_by_id))

    @property
    def unresolved_exchanges(self) -> tuple[LlamaLocalExchangeSettlement, ...]:
        return tuple(ex for ex in self._exchanges if ex.state != "settled")

    async def close(self) -> None:
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._original_http.aclose())
            self._cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self._cleanup)

    def stream(self, *_args, **_kwargs):
        raise ProviderError("Native llama media provider does not stream responses")

    async def health(self) -> dict:
        return {
            "ready": False,
            "readiness": "unknown",
            "qualification": "not_checked",
            "model": self._declaration[1][1],
            "mode": self._declaration[2][1],
            "upstream_commit": self._declaration[5][1],
        }

    def _check_current(self, current: Callable[[], bool] | None) -> None:
        if current is None:
            return
        try:
            active = current()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Native llama media lease check failed") from exc
        if active is not True:
            raise ProviderError("Native llama media lease is no longer current")

    async def _finalize_and_store_exchange(
        self,
        *,
        seq: int,
        prepared: LlamaMediaPreparedRequest,
        bridge: object | None,
        registration: LlamaExchangeRegistration | None,
        transport_closed: bool,
        wire_buffer_retired: bool,
        attempt_disposition: Literal[
            "completed",
            "cancelled",
            "timed_out",
            "failed",
            "cleanup_unconfirmed",
            "not_started",
        ],
        bridge_outcome: Literal["completed", "cancelled", "failed", "not_started"] | None,
        candidate: LlamaMediaChatResult | LlamaMediaSystemOneResult | None = None,
    ) -> LlamaLocalExchangeSettlement:
        if bridge is not None and registration is not None:
            try:
                settle_fn = getattr(bridge, "settle_exchange", None)
                if callable(settle_fn):
                    bundle = await _maybe_await(
                        settle_fn(
                            registration=registration,
                            prepared=prepared,
                            outcome=bridge_outcome,
                            transport_sole_closed=transport_closed,
                            wire_buffer_retired=wire_buffer_retired,
                            candidate=candidate,
                        )
                    )
                    settlement = _verify_settlement_bundle(
                        bundle,
                        registration,
                        prepared,
                        transport_closed=transport_closed,
                        wire_buffer_retired=wire_buffer_retired,
                        requested_outcome=bridge_outcome,
                    )
                    settlement = LlamaLocalExchangeSettlement(
                        exchange_id=settlement.exchange_id,
                        local_task_id=settlement.local_task_id,
                        transport_owner_id=settlement.transport_owner_id,
                        body_owner_id=settlement.body_owner_id,
                        request_sha256=settlement.request_sha256,
                        binding_sha256=settlement.binding_sha256,
                        model=settlement.model,
                        upstream_commit=settlement.upstream_commit,
                        local_task_joined=settlement.local_task_joined,
                        transport_sole_closed=settlement.transport_sole_closed,
                        body_settled=settlement.body_settled,
                        all_readers_settled=settlement.all_readers_settled,
                        publications_settled=settlement.publications_settled,
                        state=settlement.state,
                        outcome=settlement.outcome,
                        attempt_disposition=attempt_disposition,
                        kind="local_exchange_v2",
                        body_storage=settlement.body_storage,
                        body_proof=settlement.body_proof,
                        liability=settlement.liability,
                        body_owner_record=settlement.body_owner_record,
                        body_close_record=settlement.body_close_record,
                        cleanup_receipt=settlement.cleanup_receipt,
                    )
                else:
                    settlement = _verify_settlement_bundle(
                        None,
                        registration,
                        prepared,
                        transport_closed=transport_closed,
                        wire_buffer_retired=wire_buffer_retired,
                        requested_outcome=None,
                    )
                    settlement = LlamaLocalExchangeSettlement(
                        exchange_id=settlement.exchange_id,
                        local_task_id=settlement.local_task_id,
                        transport_owner_id=settlement.transport_owner_id,
                        body_owner_id=settlement.body_owner_id,
                        request_sha256=settlement.request_sha256,
                        binding_sha256=settlement.binding_sha256,
                        model=settlement.model,
                        upstream_commit=settlement.upstream_commit,
                        local_task_joined=False,
                        transport_sole_closed=transport_closed,
                        body_settled=False,
                        all_readers_settled=False,
                        publications_settled=False,
                        state="unknown",
                        outcome=None,
                        attempt_disposition=attempt_disposition,
                        kind="local_exchange_v2",
                        body_storage=settlement.body_storage,
                        body_proof="none",
                        liability=settlement.liability,
                        body_owner_record=settlement.body_owner_record,
                        body_close_record=None,
                        cleanup_receipt=None,
                    )
            except Exception:
                settlement = LlamaLocalExchangeSettlement(
                    exchange_id=registration.body_owner.exchange_id,
                    local_task_id=registration.local_task_id,
                    transport_owner_id=registration.transport_owner_id,
                    body_owner_id=registration.body_owner.body_owner_id,
                    request_sha256=prepared.request_sha256,
                    binding_sha256=registration.binding_sha256,
                    model=prepared.model,
                    upstream_commit=prepared.upstream_commit,
                    local_task_joined=False,
                    transport_sole_closed=transport_closed,
                    body_settled=False,
                    all_readers_settled=False,
                    publications_settled=False,
                    state="unknown",
                    outcome=None,
                    attempt_disposition=attempt_disposition,
                    kind="local_exchange_v2",
                    body_storage=dict(registration.body_owner.body),
                    body_proof="none",
                    liability={"kind": "local_no_remote"},
                    body_owner_record=registration.body_owner,
                    body_close_record=None,
                    cleanup_receipt=None,
                )
        else:
            seed = f"{seq}:{prepared.request_sha256}"
            exchange_id = hashlib.sha256(f"ex:{seed}".encode()).hexdigest()[:32]
            local_task_id = hashlib.sha256(f"task:{seed}".encode()).hexdigest()[:32]
            transport_owner_id = hashlib.sha256(f"tr:{seed}".encode()).hexdigest()[:32]
            body_owner_id = hashlib.sha256(f"body:{seed}".encode()).hexdigest()[:32]
            settlement = LlamaLocalExchangeSettlement(
                exchange_id=exchange_id,
                local_task_id=local_task_id,
                transport_owner_id=transport_owner_id,
                body_owner_id=body_owner_id,
                request_sha256=prepared.request_sha256,
                binding_sha256=None,
                model=prepared.model,
                upstream_commit=prepared.upstream_commit,
                local_task_joined=False,
                transport_sole_closed=transport_closed,
                body_settled=False,
                all_readers_settled=False,
                publications_settled=False,
                state="unknown",
                outcome=None,
                attempt_disposition=attempt_disposition,
                kind="local_exchange_v2",
                body_storage={"kind": "wire_buffer"},
                body_proof="none",
                liability={"kind": "local_no_remote"},
                body_owner_record=None,
                body_close_record=None,
                cleanup_receipt=None,
            )

        self._exchanges.append(settlement)
        self._exchanges_by_id[settlement.exchange_id] = settlement
        self.last_exchange = settlement
        return settlement

    async def complete_media(
        self,
        spec: LlamaMediaRequestSpec | LlamaMediaPreparedRequest | dict,
        *,
        current: Callable[[], bool] | None = None,
        admission: Callable[[str, str], object] | None = None,
        exchange_owner_bridge: object | None = None,
    ) -> LlamaMediaChatResult | LlamaMediaSystemOneResult:
        owner_task = asyncio.current_task()
        cancel_baseline = owner_task.cancelling() if owner_task is not None else 0

        if current is not None and not callable(current):
            raise ProviderError("Native llama media current guard must be callable")
        self._check_current(current)

        if isinstance(spec, LlamaMediaPreparedRequest):
            prepared = _validate_prepared_request(
                spec,
                expected_mode=self.mode,
                expected_model=self.model,
                expected_commit=self.upstream_commit,
            )
        elif isinstance(spec, dict):
            if "mode" in spec and spec["mode"] != self.mode:
                raise LlamaMediaProtocolError(
                    "Prepared media request mode or model did not match bound provider"
                )
            if "model" in spec and spec["model"] != self.model:
                raise LlamaMediaProtocolError(
                    "Prepared media request mode or model did not match bound provider"
                )
            merged = {"mode": self.mode, "model": self.model, **spec}
            prepared = _validate_prepared_request(
                build_request(merged),
                expected_mode=self.mode,
                expected_model=self.model,
                expected_commit=self.upstream_commit,
            )
        elif isinstance(spec, LlamaMediaRequestSpec):
            if spec.mode != self.mode or spec.model != self.model:
                raise LlamaMediaProtocolError(
                    "Prepared media request mode or model did not match bound provider"
                )
            prepared = _validate_prepared_request(
                build_request(spec),
                expected_mode=self.mode,
                expected_model=self.model,
                expected_commit=self.upstream_commit,
            )
        else:
            raise LlamaMediaProtocolError(
                "complete_media requires a request spec or prepared request"
            )

        detached_body = copy.deepcopy(prepared.body)
        wire = prepared.raw_body
        path_with_query = f"{prepared.path.lstrip('/')}?{prepared.query}"
        if path_with_query != self._expected_path:
            raise LlamaMediaProtocolError("Prepared media request target path did not match mode")

        client = self._original_http
        info = self._original_info
        info_snapshot = info.model_dump()
        hooks_snapshot = tuple((name, tuple(values)) for name, values in client.event_hooks.items())
        timeout_snapshot = client.timeout.as_dict()

        def check_boundary() -> None:
            self._check_current(current)
            try:
                unchanged = (
                    self.http is client
                    and self.info is info
                    and info.model_dump() == info_snapshot
                    and info_snapshot
                    == ProviderInfo(
                        provider="native_llama_media",
                        model=prepared.model,
                        local=True,
                        streaming=False,
                    ).model_dump()
                    and self._policy() == self._declaration
                    and self._cleanup is None
                    and not client.is_closed
                    and tuple(client.headers.raw) == self._headers
                    and str(client.base_url) == self._origin
                    and client.follow_redirects is False
                    and client.auth is None
                    and not client.params
                    and not client.cookies
                    and client.timeout.as_dict() == timeout_snapshot
                    and tuple((name, tuple(values)) for name, values in client.event_hooks.items())
                    == hooks_snapshot
                    and httpx.AsyncClient._transport_for_url(
                        client, client.base_url.join(path_with_query)
                    )
                    is self._transport
                    and _compact_json(detached_body) == wire
                )
            except (AttributeError, TypeError, ValueError, ProviderError):
                unchanged = False
            if not unchanged:
                raise LlamaMediaProtocolError("Native llama media transport binding changed")

        check_boundary()
        probe_request = client.build_request(
            "POST",
            path_with_query,
            json=detached_body,
            headers={"Accept-Encoding": "identity"},
        )
        if (
            probe_request.content != wire
            or hashlib.sha256(probe_request.content).hexdigest() != prepared.request_sha256
            or probe_request.url.scheme != client.base_url.scheme
            or probe_request.url.host != client.base_url.host
            or probe_request.url.port != client.base_url.port
            or probe_request.url.path != prepared.path
            or probe_request.url.query != prepared.query.encode("ascii")
        ):
            raise LlamaMediaProtocolError(
                "Native llama media HTTP encoding or origin does not match frozen request"
            )

        bridge = (
            exchange_owner_bridge
            if exchange_owner_bridge is not None
            else self._exchange_owner_bridge
        )
        registration: LlamaExchangeRegistration | None = None
        if bridge is not None:
            register_fn = getattr(bridge, "register_exchange", None)
            if not callable(register_fn):
                raise LlamaMediaProtocolError(
                    "exchange_owner_bridge must provide callable register_exchange"
                )
            raw_reg = await _maybe_await(
                register_fn(prepared=prepared, mode=self.mode, model=self.model)
            )
            registration = _validate_registration_against_request(raw_reg, prepared)
            check_boundary()

        self._exchange_seq += 1
        seq = self._exchange_seq

        max_response_bytes = (
            MAX_CHAT_RESPONSE_BYTES
            if prepared.mode == "converse_chat"
            else MAX_SYSTEMONE_RESPONSE_BYTES
        )

        owned_streams: list[_TrackedOwnedHTTPStream] = []

        def own_stream(inner_stream: httpx.AsyncByteStream) -> _TrackedOwnedHTTPStream:
            tracked = _TrackedOwnedHTTPStream(inner_stream)
            owned_streams.append(tracked)
            return tracked

        def _transport_sole_closed() -> bool:
            return len(owned_streams) == 1 and owned_streams[0].sole_closed

        raw_response = b""
        wire_buffer_retired = False
        deadline = asyncio.timeout(self.timeout)

        try:
            async with deadline:
                async with complete_stream(
                    client,
                    path_with_query,
                    detached_body,
                    check_boundary,
                    own_stream,
                    admission=admission,
                    purpose=prepared.mode,
                ) as response:
                    try:
                        if not 200 <= response.status_code < 300:
                            raise ProviderError(
                                f"Native llama media endpoint returned HTTP {response.status_code}"
                            )
                        encoding = (
                            response.headers.get("content-encoding", "identity").strip().lower()
                        )
                        if encoding not in {"", "identity"}:
                            raise LlamaMediaProtocolError(
                                "Native llama media response used a non-identity encoding"
                            )
                        data = bytearray()
                        try:
                            chunks = (
                                response.aiter_bytes(chunk_size=4096)
                                if response.is_stream_consumed
                                else response.aiter_raw(chunk_size=4096)
                            )
                            async for chunk in chunks:
                                check_boundary()
                                if len(data) + len(chunk) > max_response_bytes:
                                    raise ProviderError(
                                        "Native llama media response exceeded the bounded size"
                                    )
                                data.extend(chunk)
                            raw_response = bytes(data)
                        finally:
                            data.clear()
                    finally:
                        if owned_streams and owned_streams[0].cleanup is None:
                            await response.stream.aclose()
                wire_buffer_retired = _transport_sole_closed()
        except ProviderCleanupError:
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=False,
                wire_buffer_retired=False,
                attempt_disposition="cleanup_unconfirmed",
                bridge_outcome=None,
            )
            raise
        except asyncio.CancelledError:
            sole_closed = _transport_sole_closed()
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=sole_closed,
                attempt_disposition="cancelled",
                bridge_outcome="cancelled",
            )
            raise
        except PromptCountUnavailable as exc:
            sole_closed = _transport_sole_closed()
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=sole_closed,
                attempt_disposition="failed",
                bridge_outcome="failed",
            )
            raise LlamaMediaProtocolError(
                "Native llama media HTTP request binding changed"
            ) from exc
        except TimeoutError as exc:
            sole_closed = _transport_sole_closed()
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=sole_closed,
                attempt_disposition="timed_out",
                bridge_outcome="failed",
            )
            raise LlamaMediaTimeoutError(
                "Native llama media request exceeded its total deadline"
            ) from exc
        except ProviderError:
            sole_closed = _transport_sole_closed()
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=sole_closed,
                attempt_disposition="failed",
                bridge_outcome="failed",
            )
            raise
        except httpx.HTTPError as exc:
            sole_closed = _transport_sole_closed()
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=sole_closed,
                attempt_disposition="failed",
                bridge_outcome="failed",
            )
            raise ProviderError("Native llama media endpoint is unavailable") from exc

        # A transport or stream may suppress CancelledError while settling its
        # work. Retire the original response close first, then reject any newly
        # requested caller cancellation or expired deadline before parsing or
        # returning a usable result or completed disposition.
        sole_closed = _transport_sole_closed()
        wire_buffer_retired = wire_buffer_retired and sole_closed
        if owner_task is not None and owner_task.cancelling() > cancel_baseline:
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=wire_buffer_retired,
                attempt_disposition="cancelled",
                bridge_outcome="cancelled",
            )
            raise asyncio.CancelledError
        if deadline.expired():
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=wire_buffer_retired,
                attempt_disposition="timed_out",
                bridge_outcome="failed",
            )
            raise LlamaMediaTimeoutError("Native llama media request exceeded its total deadline")

        try:
            result = parse_response(raw_response, prepared)
            check_boundary()
            if bridge is not None and registration is not None:
                deliver_fn = getattr(bridge, "deliver_candidate", None)
                if callable(deliver_fn):
                    await _maybe_await(deliver_fn(registration=registration, candidate=result))
                    check_boundary()
        except asyncio.CancelledError:
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=wire_buffer_retired,
                attempt_disposition="cancelled",
                bridge_outcome="cancelled",
            )
            raise
        except Exception:
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=wire_buffer_retired,
                attempt_disposition="failed",
                bridge_outcome="failed",
            )
            raise

        if owner_task is not None and owner_task.cancelling() > cancel_baseline:
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=wire_buffer_retired,
                attempt_disposition="cancelled",
                bridge_outcome="cancelled",
            )
            raise asyncio.CancelledError
        if deadline.expired():
            await self._finalize_and_store_exchange(
                seq=seq,
                prepared=prepared,
                bridge=bridge,
                registration=registration,
                transport_closed=sole_closed,
                wire_buffer_retired=wire_buffer_retired,
                attempt_disposition="timed_out",
                bridge_outcome="failed",
            )
            raise LlamaMediaTimeoutError("Native llama media request exceeded its total deadline")

        await self._finalize_and_store_exchange(
            seq=seq,
            prepared=prepared,
            bridge=bridge,
            registration=registration,
            transport_closed=sole_closed,
            wire_buffer_retired=wire_buffer_retired,
            attempt_disposition="completed",
            bridge_outcome="completed",
            candidate=result,
        )
        return result
