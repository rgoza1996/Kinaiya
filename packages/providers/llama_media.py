"""Pinned local llama.cpp multimodal transport for Converse and Decisions.

Supports only the retained local protocols qualified at upstream llama.cpp
commit ddf48f85c933c1f438212a02eb21e7b001d2d37d:
- Converse buffered multimodal input (`POST /v1/chat/completions?autoload=false`)
  with ordered text, PNG `image_url`, canonical PCM WAV `input_audio`, and MP4
  `input_video`, returning one terminal `finish_reason == "stop"` text message.
- Decisions direct media classification (`POST /v1/systemone?autoload=false`)
  with `{model, state, files: [data URI], questions: {...}}` for PNG/WAV inputs
  and typed choice/noul answers.

No model discovery, autoloading, implicit fallback, cloud endpoint, or native
speech-output claim is permitted here.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import re
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

from packages.contracts.media import MediaNativeRef, MediaQ, MediaSourcePTS, _interval
from packages.contracts.models import ProviderInfo
from packages.providers.complete_http import RequestBoundAsyncClient, complete_stream
from packages.providers.text import (
    ProviderCleanupError,
    ProviderError,
    ProviderRefusal,
    _OwnedHTTPStream,
)
from packages.providers.whistle_ipc import JsonProtocolError, decode_json

UPSTREAM_LLAMA_COMMIT = "ddf48f85c933c1f438212a02eb21e7b001d2d37d"

MAX_MEDIA_PARTS = 16
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

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_WAV_RATES = frozenset({16000, 24000, 48000})
_WAV_CHANNELS = frozenset({1, 2})
_WAV_BITS = frozenset({16, 24, 32})


def _compact_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ProviderRefusal("Local media JSON payload is invalid") from exc
    return raw


def _strict_json_object(raw: bytes, limit: int, label: str) -> dict:
    if type(raw) is not bytes or not 1 <= len(raw) <= limit:
        raise ProviderRefusal(f"{label} exceeded or violated byte bounds")
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
        raise ProviderRefusal(f"{label} was not a valid closed JSON object") from exc
    if type(value) is not dict:
        raise ProviderRefusal(f"{label} must decode to a JSON object")
    return value


def _validate_utf8_text(value: object, maximum_chars: int, label: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum_chars:
        raise ProviderRefusal(f"{label} must be a non-empty bounded string")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ProviderRefusal(f"{label} contains invalid Unicode") from exc
    if "\x00" in value:
        raise ProviderRefusal(f"{label} cannot contain NUL bytes")
    return value


def _validate_positive_int(value: object, label: str, *, maximum: int = MAX_SAFE_INT) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ProviderRefusal(f"{label} must be a bounded positive integer")
    return value


def _validate_nonnegative_int(value: object, label: str, *, maximum: int = MAX_SAFE_INT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProviderRefusal(f"{label} must be a bounded non-negative integer")
    return value


def _validate_probability(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and 0.0 <= float(value) <= 1.0


def _validate_png_bytes(data: bytes) -> None:
    if (
        len(data) < 33
        or len(data) > MAX_IMAGE_BYTES
        or not data.startswith(_PNG_SIG)
        or not data.endswith(_PNG_IEND)
        or data[12:16] != b"IHDR"
    ):
        raise ProviderRefusal("Local media image part requires valid bounded PNG bytes")
    ihdr_len = struct.unpack(">I", data[8:12])[0]
    width, height = struct.unpack(">II", data[16:24])
    if ihdr_len != 13 or not (1 <= width <= 8192 and 1 <= height <= 8192):
        raise ProviderRefusal("Local media PNG dimensions or IHDR header are invalid")


def _validate_wav_bytes(data: bytes) -> None:
    if (
        len(data) <= 44
        or len(data) > MAX_AUDIO_BYTES
        or data[:4] != b"RIFF"
        or data[8:16] != b"WAVEfmt "
        or data[36:40] != b"data"
    ):
        raise ProviderRefusal("Local media audio part requires a canonical 44-byte header PCM WAV")
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
        raise ProviderRefusal("Local media WAV header does not match canonical PCM framing")


def _validate_mp4_bytes(data: bytes) -> None:
    if len(data) < 16 or len(data) > MAX_VIDEO_CLIP_BYTES or data[4:8] != b"ftyp":
        raise ProviderRefusal("Local media video_clip part requires valid bounded MP4 bytes")
    box_size = struct.unpack(">I", data[:4])[0]
    if box_size < 8 or box_size > len(data):
        raise ProviderRefusal("Local media MP4 ftyp box size is invalid")


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
                raise ProviderRefusal("Text part cannot carry binary media metadata")
            return

        if self.text is not None:
            raise ProviderRefusal("Binary media part cannot carry inline text")
        if type(self.data) is not bytes or len(self.data) == 0:
            raise ProviderRefusal("Binary media part requires non-empty bytes")
        if type(self.source) is not MediaNativeRef:
            raise ProviderRefusal("Binary media part requires an exact source MediaNativeRef")
        if type(self.start) is not MediaQ or type(self.end) is not MediaQ:
            raise ProviderRefusal("Binary media part requires exact start and end MediaQ bounds")
        source_seq = _validate_positive_int(self.source_seq, "source_seq")
        through_seq = _validate_positive_int(self.through_source_seq, "through_source_seq")
        if through_seq < source_seq:
            raise ProviderRefusal("Binary media part through_source_seq preceded source_seq")
        digest = hashlib.sha256(self.data).hexdigest()
        if (
            type(self.body_sha256) is not str
            or not _SHA256_RE.fullmatch(self.body_sha256)
            or self.body_sha256 != digest
        ):
            raise ProviderRefusal("Binary media part body_sha256 did not match payload bytes")

        try:
            _interval(
                self.start,
                self.end,
                10 if self.kind == "video_clip" else (1 if self.kind == "audio" else None),
            )
        except Exception as exc:
            raise ProviderRefusal("Binary media part interval violated contract bounds") from exc

        if self.kind == "video_clip":
            if self.mime_type != "video/mp4":
                raise ProviderRefusal("video_clip part requires mime_type='video/mp4'")
            _validate_mp4_bytes(self.data)
            if type(self.range) is not MediaNativeRef:
                raise ProviderRefusal("video_clip part requires an exact range MediaNativeRef")
            if self.track_id is not None or self.pts is not None:
                raise ProviderRefusal("video_clip part cannot carry single-track pts or track_id")
            return

        if self.range is not None:
            raise ProviderRefusal("Single-packet media part cannot carry a clip range ref")
        if source_seq != through_seq:
            raise ProviderRefusal(
                "Single-packet media part requires source_seq == through_source_seq"
            )
        if type(self.track_id) is not str or not _ID_RE.fullmatch(self.track_id):
            raise ProviderRefusal("Single-packet media part requires a valid track_id")
        if type(self.pts) is not MediaSourcePTS:
            raise ProviderRefusal("Single-packet media part requires an exact MediaSourcePTS")

        if self.kind in {"image", "video_frame"}:
            if self.mime_type != "image/png":
                raise ProviderRefusal("Image/frame part requires mime_type='image/png'")
            _validate_png_bytes(self.data)
            if self.pts.frame_index is None or (self.kind == "image" and self.pts.frame_index != 0):
                raise ProviderRefusal("Image/frame part PTS frame_index is invalid")
        elif self.kind == "audio":
            if self.mime_type != "audio/wav":
                raise ProviderRefusal("Audio part requires mime_type='audio/wav'")
            _validate_wav_bytes(self.data)
            if self.pts.sample_count is None:
                raise ProviderRefusal("Audio part PTS requires sample_count")
        else:
            raise ProviderRefusal("Unsupported local media part kind")


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
    body_fd_closed: bool
    all_readers_settled: bool
    publications_settled: bool
    state: Literal["settled", "unknown"]
    outcome: Literal["completed", "cancelled", "failed"] | None


def _validate_questions(questions: object) -> dict[str, dict]:
    if type(questions) is not dict or not 1 <= len(questions) <= 16:
        raise ProviderRefusal("SystemOne media request requires 1..16 typed questions")
    validated: dict[str, dict] = {}
    for name, spec in questions.items():
        if type(name) is not str or not _ID_RE.fullmatch(name) or type(spec) is not dict:
            raise ProviderRefusal("SystemOne question key or definition is malformed")
        qtype = spec.get("type")
        instructions = spec.get("instructions")
        _validate_utf8_text(instructions, 2000, f"Question {name} instructions")
        if qtype == "choice":
            if set(spec.keys()) != {"type", "instructions", "criteria"}:
                raise ProviderRefusal(f"Choice question {name} has unexpected fields")
            criteria = spec.get("criteria")
            if type(criteria) is not dict or not 2 <= len(criteria) <= 16:
                raise ProviderRefusal(f"Choice question {name} requires 2..16 criteria")
            clean_criteria: dict[str, str] = {}
            for label, desc in criteria.items():
                if type(label) is not str or not _ID_RE.fullmatch(label):
                    raise ProviderRefusal(f"Choice question {name} has invalid label")
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
                raise ProviderRefusal(f"Noul question {name} has unexpected fields")
            validated[name] = {
                "type": "noul",
                "instructions": instructions,
            }
        else:
            raise ProviderRefusal(f"Unsupported SystemOne question type for {name}")
    return validated


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
            raise ProviderRefusal("Local media request spec dict has missing or unknown keys")
        raw_parts = spec["parts"]
        if not isinstance(raw_parts, (list, tuple)):
            raise ProviderRefusal("Local media request parts must be a sequence")
        typed_parts = [
            part if isinstance(part, LlamaMediaPart) else LlamaMediaPart(**part)
            for part in raw_parts
        ]
        spec = LlamaMediaRequestSpec(
            mode=spec["mode"],
            model=spec["model"],
            parts=tuple(typed_parts),
            system_prompt=spec.get("system_prompt"),
            temperature=spec.get("temperature", 0),
            max_tokens=spec.get("max_tokens", 128),
            reasoning_budget=spec.get("reasoning_budget"),
            state=spec.get("state"),
            questions=spec.get("questions"),
            upstream_commit=spec.get("upstream_commit", UPSTREAM_LLAMA_COMMIT),
        )
    elif not isinstance(spec, LlamaMediaRequestSpec):
        raise ProviderRefusal("build_request requires a LlamaMediaRequestSpec or dict")

    if spec.upstream_commit != UPSTREAM_LLAMA_COMMIT:
        raise ProviderRefusal("Local media request upstream_commit does not match pinned runtime")
    if type(spec.model) is not str or not _MODEL_RE.fullmatch(spec.model):
        raise ProviderRefusal("Local media request requires an explicit model alias")
    if not isinstance(spec.parts, (list, tuple)) or not 1 <= len(spec.parts) <= MAX_MEDIA_PARTS:
        raise ProviderRefusal("Local media request requires 1..16 ordered parts")

    parts: list[LlamaMediaPart] = []
    provenance: list[LlamaMediaPartProvenance] = []
    total_media_bytes = 0
    last_seq_by_source: dict[tuple[str, str, str], int] = {}

    for idx, part in enumerate(spec.parts):
        if not isinstance(part, LlamaMediaPart):
            raise ProviderRefusal("Every request part must be a validated LlamaMediaPart")
        parts.append(part)
        if part.kind == "text":
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
                raise ProviderRefusal("Aggregate local media payload bytes exceeded 16 MiB cap")
            source_key = (part.source.backend_id, part.source.receipt_id, part.source.sha256)
            prev_seq = last_seq_by_source.get(source_key)
            if prev_seq is not None and part.source_seq < prev_seq:
                raise ProviderRefusal(
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

    if spec.mode == "converse_chat":
        if spec.state is not None or spec.questions is not None:
            raise ProviderRefusal(
                "converse_chat request cannot include SystemOne state or questions"
            )
        if (
            type(spec.temperature) not in {int, float}
            or not math.isfinite(spec.temperature)
            or not 0.0 <= float(spec.temperature) <= 2.0
        ):
            raise ProviderRefusal("converse_chat temperature must be a finite number in [0, 2]")
        max_tokens = _validate_positive_int(spec.max_tokens, "max_tokens", maximum=4096)
        if spec.reasoning_budget is not None and (
            type(spec.reasoning_budget) is not int or spec.reasoning_budget != 0
        ):
            raise ProviderRefusal("converse_chat reasoning_budget only supports 0 when specified")
        if not any(part.kind != "text" for part in parts):
            raise ProviderRefusal("converse_chat media request requires at least one media part")

        content_items: list[dict] = []
        for part in parts:
            if part.kind == "text":
                content_items.append({"type": "text", "text": part.text})
            elif part.kind in {"image", "video_frame"}:
                b64 = base64.b64encode(part.data).decode("ascii")
                content_items.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    }
                )
            elif part.kind == "audio":
                b64 = base64.b64encode(part.data).decode("ascii")
                content_items.append(
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64, "format": "wav"},
                    }
                )
            elif part.kind == "video_clip":
                b64 = base64.b64encode(part.data).decode("ascii")
                content_items.append(
                    {
                        "type": "input_video",
                        "input_video": {"data": b64},
                    }
                )

        messages: list[dict] = []
        if spec.system_prompt is not None:
            messages.append(
                {
                    "role": "system",
                    "content": _validate_utf8_text(
                        spec.system_prompt, MAX_SYSTEM_PROMPT_CHARS, "system_prompt"
                    ),
                }
            )
        messages.append({"role": "user", "content": content_items})

        body: dict = {
            "model": spec.model,
            "temperature": spec.temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "messages": messages,
        }
        if spec.reasoning_budget is not None:
            body["reasoning_budget"] = spec.reasoning_budget
        path: Literal["/v1/chat/completions", "/v1/systemone"] = "/v1/chat/completions"
        validated_questions = None

    elif spec.mode == "decisions_systemone":
        if (
            spec.system_prompt is not None
            or spec.reasoning_budget is not None
            or spec.temperature != 0
            or spec.max_tokens != 128
        ):
            raise ProviderRefusal(
                "decisions_systemone request cannot include chat generation parameters"
            )
        state_text = _validate_utf8_text(spec.state, MAX_STATE_CHARS, "SystemOne state")
        validated_questions = _validate_questions(spec.questions)
        files: list[str] = []
        for part in parts:
            if part.kind in {"image", "video_frame"}:
                b64 = base64.b64encode(part.data).decode("ascii")
                files.append(f"data:image/png;base64,{b64}")
            elif part.kind == "audio":
                b64 = base64.b64encode(part.data).decode("ascii")
                files.append(f"data:audio/wav;base64,{b64}")
            else:
                raise ProviderRefusal(
                    "decisions_systemone files only accept PNG image/video_frame and PCM WAV audio parts"
                )
        body = {
            "model": spec.model,
            "state": state_text,
            "files": files,
            "questions": copy.deepcopy(validated_questions),
        }
        path = "/v1/systemone"
    else:
        raise ProviderRefusal("Unsupported local media request mode")

    raw_body = _compact_json(body)
    if len(raw_body) > MAX_REQUEST_WIRE_BYTES:
        raise ProviderRefusal("Encoded local media request exceeded wire byte cap")
    return LlamaMediaPreparedRequest(
        mode=spec.mode,
        model=spec.model,
        path=path,
        query="autoload=false",
        upstream_commit=spec.upstream_commit,
        body=body,
        raw_body=raw_body,
        request_sha256=hashlib.sha256(raw_body).hexdigest(),
        provenance=tuple(provenance),
        questions=validated_questions,
    )


def parse_response(
    raw: bytes,
    prepared: LlamaMediaPreparedRequest,
) -> LlamaMediaChatResult | LlamaMediaSystemOneResult:
    """Validate raw response bytes against the exact prepared local media request."""
    if not isinstance(prepared, LlamaMediaPreparedRequest):
        raise ProviderRefusal("parse_response requires a LlamaMediaPreparedRequest")

    if prepared.mode == "converse_chat":
        payload = _strict_json_object(raw, MAX_CHAT_RESPONSE_BYTES, "Converse media response")
        if payload.get("error") is not None:
            raise ProviderRefusal("Converse media response returned an error payload")
        if "model" in payload and payload["model"] != prepared.model:
            raise ProviderRefusal("Converse media response model did not match selected alias")
        choices = payload.get("choices")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise ProviderRefusal("Converse media response requires exactly one choice object")
        choice = choices[0]
        if "index" in choice and (type(choice["index"]) is not int or choice["index"] != 0):
            raise ProviderRefusal("Converse media response choice index must be 0")
        if any(choice.get(key) is not None for key in ("tool_calls", "function_call", "refusal")):
            raise ProviderRefusal("Converse media response cannot emit tool calls or refusals")
        if choice.get("finish_reason") != "stop":
            raise ProviderRefusal("Converse media response requires terminal finish_reason='stop'")
        message = choice.get("message")
        if type(message) is not dict:
            raise ProviderRefusal("Converse media response choice requires a message object")
        role = message.get("role")
        if role is not None and role != "assistant":
            raise ProviderRefusal("Converse media response message role must be 'assistant'")
        if any(
            message.get(key) is not None
            for key in ("tool_calls", "function_call", "refusal", "audio")
        ):
            raise ProviderRefusal(
                "Converse media response message contains unsupported output fields"
            )
        content = message.get("content")
        _validate_utf8_text(content, MAX_CHAT_OUTPUT_CHARS, "Converse media response content")
        if len(content.encode("utf-8")) > MAX_CHAT_OUTPUT_BYTES:
            raise ProviderRefusal("Converse media response content exceeded UTF-8 byte cap")

        usage_obj = payload.get("usage")
        parsed_usage: dict[str, int] | None = None
        if usage_obj is not None:
            if type(usage_obj) is not dict:
                raise ProviderRefusal("Converse media response usage must be an object")
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
                raise ProviderRefusal("Converse media response usage token totals are inconsistent")
            parsed_usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }

        return LlamaMediaChatResult(
            mode="converse_chat",
            model=prepared.model,
            text=content,
            finish_reason="stop",
            usage=parsed_usage,
            request_sha256=prepared.request_sha256,
            response_sha256=hashlib.sha256(raw).hexdigest(),
            provenance=prepared.provenance,
        )

    if prepared.mode == "decisions_systemone":
        assert prepared.questions is not None
        payload = _strict_json_object(raw, MAX_SYSTEMONE_RESPONSE_BYTES, "SystemOne media response")
        if set(payload.keys()) != {"model", "answers", "usage"}:
            raise ProviderRefusal(
                "SystemOne media response keys did not match {model, answers, usage}"
            )
        if payload["model"] != prepared.model:
            raise ProviderRefusal("SystemOne media response model did not match selected alias")
        usage = payload["usage"]
        if type(usage) is not dict or set(usage.keys()) != {"input_tokens", "output_tokens"}:
            raise ProviderRefusal("SystemOne media response usage was malformed")
        input_tokens = _validate_positive_int(usage["input_tokens"], "usage.input_tokens")
        output_tokens = _validate_nonnegative_int(
            usage["output_tokens"], "usage.output_tokens", maximum=0
        )
        answers = payload["answers"]
        if type(answers) is not dict or set(answers.keys()) != set(prepared.questions.keys()):
            raise ProviderRefusal(
                "SystemOne media response answers did not match requested questions"
            )

        validated_answers: dict[str, dict] = {}
        for name, qspec in prepared.questions.items():
            entry = answers.get(name)
            if type(entry) is not dict:
                raise ProviderRefusal(f"SystemOne answer for {name} must be an object")
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
                    raise ProviderRefusal(f"SystemOne choice answer for {name} was malformed")
                probs = {k: float(v) for k, v in entry["probabilities"].items()}
                choice = entry["choice"]
                selected = probs[choice]
                conf = float(entry["confidence"])
                if (
                    abs(sum(probs.values()) - 1.0) > len(labels) * 0.0001 + 1e-6
                    or selected + 0.0001 < max(probs.values())
                    or abs(conf - selected) > 0.001
                ):
                    raise ProviderRefusal(
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
                    raise ProviderRefusal(f"SystemOne noul answer for {name} was malformed")
                validated_answers[name] = {
                    "type": "noul",
                    "noul": float(entry["noul"]),
                }

        return LlamaMediaSystemOneResult(
            mode="decisions_systemone",
            model=prepared.model,
            answers=validated_answers,
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            request_sha256=prepared.request_sha256,
            response_sha256=hashlib.sha256(raw).hexdigest(),
            provenance=prepared.provenance,
        )

    raise ProviderRefusal("Unsupported prepared request mode")


class NativeLlamaMediaProvider:
    """Loopback-only native llama.cpp multimodal adapter with exchange settlement."""

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
        self.upstream_commit = upstream_commit
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
        self._exchange_seq = 0
        self.last_exchange: LlamaLocalExchangeSettlement | None = None

    async def close(self) -> None:
        await self.http.aclose()

    def stream(self, *_args, **_kwargs):
        raise ProviderError("Native llama media provider does not stream responses")

    async def health(self) -> dict:
        return {
            "ready": False,
            "readiness": "unknown",
            "qualification": "not_checked",
            "model": self.model,
            "mode": self.mode,
            "upstream_commit": self.upstream_commit,
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

    async def complete_media(
        self,
        spec: LlamaMediaRequestSpec | LlamaMediaPreparedRequest | dict,
        *,
        current: Callable[[], bool] | None = None,
        admission: Callable[[str, str], object] | None = None,
    ) -> LlamaMediaChatResult | LlamaMediaSystemOneResult:
        if current is not None and not callable(current):
            raise ProviderError("Native llama media current guard must be callable")
        self._check_current(current)

        if isinstance(spec, LlamaMediaPreparedRequest):
            prepared = spec
        else:
            if isinstance(spec, dict):
                merged = {"mode": self.mode, "model": self.model, **spec}
                prepared = build_request(merged)
            elif isinstance(spec, LlamaMediaRequestSpec):
                prepared = build_request(spec)
            else:
                raise ProviderRefusal("complete_media requires a request spec or prepared request")

        if prepared.mode != self.mode or prepared.model != self.model:
            raise ProviderRefusal(
                "Prepared media request mode or model did not match bound provider"
            )

        self._exchange_seq += 1
        seq = self._exchange_seq
        exchange_id = f"llama-media-ex-{seq}"
        local_task_id = f"llama-media-task-{seq}"
        transport_owner_id = f"llama-media-transport-{seq}"
        body_owner_id = f"llama-media-body-{seq}"

        client = self.http
        info_snapshot = self.info.model_dump()
        headers_snapshot = dict(client.headers)
        origin_snapshot = str(client.base_url)

        def check_boundary() -> None:
            self._check_current(current)
            if (
                self.http is not client
                or self.info.model_dump() != info_snapshot
                or dict(client.headers) != headers_snapshot
                or str(client.base_url) != origin_snapshot
            ):
                raise ProviderRefusal("Native llama media transport binding changed")

        max_response_bytes = (
            MAX_CHAT_RESPONSE_BYTES
            if prepared.mode == "converse_chat"
            else MAX_SYSTEMONE_RESPONSE_BYTES
        )
        path_with_query = f"{prepared.path.lstrip('/')}?{prepared.query}"

        body_closed = False
        transport_closed = False

        try:
            async with asyncio.timeout(self.timeout):
                async with complete_stream(
                    client,
                    path_with_query,
                    prepared.body,
                    check_boundary,
                    _OwnedHTTPStream,
                    admission=admission,
                    purpose=prepared.mode,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise ProviderError(
                            f"Native llama media endpoint returned HTTP {response.status_code}"
                        )
                    encoding = response.headers.get("content-encoding", "identity").strip().lower()
                    if encoding not in {"", "identity"}:
                        raise ProviderRefusal(
                            "Native llama media response used a non-identity encoding"
                        )
                    data = bytearray()
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
                    body_closed = True
                transport_closed = True
        except ProviderCleanupError:
            self.last_exchange = LlamaLocalExchangeSettlement(
                exchange_id=exchange_id,
                local_task_id=local_task_id,
                transport_owner_id=transport_owner_id,
                body_owner_id=body_owner_id,
                request_sha256=prepared.request_sha256,
                model=prepared.model,
                upstream_commit=prepared.upstream_commit,
                local_task_joined=True,
                transport_sole_closed=False,
                body_fd_closed=body_closed,
                all_readers_settled=False,
                publications_settled=False,
                state="unknown",
                outcome=None,
            )
            raise
        except asyncio.CancelledError:
            self.last_exchange = LlamaLocalExchangeSettlement(
                exchange_id=exchange_id,
                local_task_id=local_task_id,
                transport_owner_id=transport_owner_id,
                body_owner_id=body_owner_id,
                request_sha256=prepared.request_sha256,
                model=prepared.model,
                upstream_commit=prepared.upstream_commit,
                local_task_joined=True,
                transport_sole_closed=True,
                body_fd_closed=True,
                all_readers_settled=True,
                publications_settled=True,
                state="settled",
                outcome="cancelled",
            )
            raise
        except ProviderError:
            self.last_exchange = LlamaLocalExchangeSettlement(
                exchange_id=exchange_id,
                local_task_id=local_task_id,
                transport_owner_id=transport_owner_id,
                body_owner_id=body_owner_id,
                request_sha256=prepared.request_sha256,
                model=prepared.model,
                upstream_commit=prepared.upstream_commit,
                local_task_joined=True,
                transport_sole_closed=True,
                body_fd_closed=True,
                all_readers_settled=True,
                publications_settled=True,
                state="settled",
                outcome="failed",
            )
            raise
        except TimeoutError as exc:
            self.last_exchange = LlamaLocalExchangeSettlement(
                exchange_id=exchange_id,
                local_task_id=local_task_id,
                transport_owner_id=transport_owner_id,
                body_owner_id=body_owner_id,
                request_sha256=prepared.request_sha256,
                model=prepared.model,
                upstream_commit=prepared.upstream_commit,
                local_task_joined=True,
                transport_sole_closed=True,
                body_fd_closed=True,
                all_readers_settled=True,
                publications_settled=True,
                state="settled",
                outcome="failed",
            )
            raise ProviderError("Native llama media request exceeded its total deadline") from exc
        except httpx.HTTPError as exc:
            self.last_exchange = LlamaLocalExchangeSettlement(
                exchange_id=exchange_id,
                local_task_id=local_task_id,
                transport_owner_id=transport_owner_id,
                body_owner_id=body_owner_id,
                request_sha256=prepared.request_sha256,
                model=prepared.model,
                upstream_commit=prepared.upstream_commit,
                local_task_joined=True,
                transport_sole_closed=True,
                body_fd_closed=True,
                all_readers_settled=True,
                publications_settled=True,
                state="settled",
                outcome="failed",
            )
            raise ProviderError("Native llama media endpoint is unavailable") from exc

        try:
            result = parse_response(bytes(data), prepared)
            check_boundary()
        except Exception:
            self.last_exchange = LlamaLocalExchangeSettlement(
                exchange_id=exchange_id,
                local_task_id=local_task_id,
                transport_owner_id=transport_owner_id,
                body_owner_id=body_owner_id,
                request_sha256=prepared.request_sha256,
                model=prepared.model,
                upstream_commit=prepared.upstream_commit,
                local_task_joined=True,
                transport_sole_closed=transport_closed,
                body_fd_closed=body_closed,
                all_readers_settled=True,
                publications_settled=True,
                state="settled" if transport_closed and body_closed else "unknown",
                outcome="failed" if transport_closed and body_closed else None,
            )
            raise

        self.last_exchange = LlamaLocalExchangeSettlement(
            exchange_id=exchange_id,
            local_task_id=local_task_id,
            transport_owner_id=transport_owner_id,
            body_owner_id=body_owner_id,
            request_sha256=prepared.request_sha256,
            model=prepared.model,
            upstream_commit=prepared.upstream_commit,
            local_task_joined=True,
            transport_sole_closed=transport_closed,
            body_fd_closed=body_closed,
            all_readers_settled=True,
            publications_settled=True,
            state="settled",
            outcome="completed",
        )
        return result
