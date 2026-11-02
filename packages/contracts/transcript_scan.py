"""Strict bounded raw transcript scans; packets grant no disclosure authority."""

import json
import math
import re
import unicodedata
from datetime import datetime
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import JournalEntry

MAX_TRANSCRIPT_SCAN_REQUEST_BYTES = 8192
MAX_TRANSCRIPT_SCAN_PAGE_BYTES = 262144
MAX_TRANSCRIPT_INTEGER = 9_007_199_254_740_991
_MAX_DEPTH = 512
_MAX_NODES = 262144
_ERROR = "Invalid transcript scan contract"
_FAILURES = (ValueError, TypeError, UnicodeError, RecursionError, OverflowError)
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})\Z"
)
_Counter = Annotated[int, Field(ge=0, le=MAX_TRANSCRIPT_INTEGER)]
_Positive = Annotated[int, Field(ge=1, le=MAX_TRANSCRIPT_INTEGER)]


class TranscriptScanConflict(ValueError):
    def __init__(self):
        super().__init__("Transcript scan scope or cursor conflicts with native history")


class TranscriptScanUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Transcript scan is unavailable")


def _text(value, maximum, *, controls=False):
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ValueError(_ERROR)
    value.encode("utf-8")
    if not controls and any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError(_ERROR)
    return value


def _json_tree(value):
    pending = [(value, 1)]
    nodes = 1
    while pending:
        item, depth = pending.pop()
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError(_ERROR)
        if type(item) is dict:
            nodes += len(item) * 2
            if nodes > _MAX_NODES:
                raise ValueError(_ERROR)
            if any(type(key) is not str for key in item):
                raise ValueError(_ERROR)
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif type(item) is list:
            nodes += len(item)
            if nodes > _MAX_NODES:
                raise ValueError(_ERROR)
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > MAX_TRANSCRIPT_SCAN_PAGE_BYTES:
                raise ValueError(_ERROR)
            item.encode("utf-8")
        elif type(item) is int:
            if abs(item) > MAX_TRANSCRIPT_INTEGER:
                raise ValueError(_ERROR)
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError(_ERROR)
        elif item is not None and type(item) is not bool:
            raise ValueError(_ERROR)


def _values(value, kind):
    if isinstance(value, BaseModel):
        if type(value) is not kind:
            raise ValueError(_ERROR)
        fields = kind.model_fields
        values = getattr(value, "__dict__", None)
        supplied = getattr(value, "__pydantic_fields_set__", None)
        if (
            type(values) is not dict
            or values.keys() != fields.keys()
            or type(supplied) is not set
            or any(type(name) is not str for name in supplied)
            or not supplied <= fields.keys()
            or kind is not JournalEntry
            and supplied != fields.keys()
            or getattr(value, "__pydantic_extra__", {}) is not None
            or getattr(value, "__pydantic_private__", None) is not None
        ):
            raise ValueError(_ERROR)
        if kind is TranscriptScanPage and (
            type(values["request"]) is not TranscriptScanRequest
            or type(values["entries"]) is not list
            or any(type(entry) is not JournalEntry for entry in values["entries"])
        ):
            # A materialized typed page must retain its typed child graph.
            # Only a raw page dictionary may parse raw child dictionaries.
            raise ValueError(_ERROR)
        value = values
    if type(value) is not dict or value.keys() != kind.model_fields.keys():
        raise ValueError(_ERROR)
    if any(type(name) is not str for name in value):
        raise ValueError(_ERROR)
    return dict(value)


def _time(value):
    if type(value) is str:
        value.encode("utf-8")
        if _TIME.fullmatch(value) is None:
            raise ValueError(_ERROR)
        value = datetime.fromisoformat(value.replace("z", "Z").replace("Z", "+00:00"))
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(_ERROR)
    if _TIME.fullmatch(value.isoformat()) is None:
        raise ValueError(_ERROR)
    return value


def _entry_fields(value):
    typed = isinstance(value, BaseModel)
    raw = _values(value, JournalEntry)
    if type(raw["schema_version"]) is not str or raw["schema_version"] != "1.0":
        raise ValueError(_ERROR)
    for name in ("session_id", "thread_id", "event_id"):
        _text(raw[name], 128)
    _text(raw["kind"], 80)
    _text(raw["origin"], 128)
    _text(raw["idempotency_key"], 256, controls=True)
    for name in ("epoch", "sequence"):
        if type(raw[name]) is not int or not 1 <= raw[name] <= MAX_TRANSCRIPT_INTEGER:
            raise ValueError(_ERROR)
    for name in ("causes", "task_refs"):
        if type(raw[name]) is not list or len(raw[name]) > 32:
            raise ValueError(_ERROR)
        for item in raw[name]:
            _text(item, 128)
    if type(raw["payload"]) is not dict:
        raise ValueError(_ERROR)
    _json_tree(raw["payload"])
    if typed and type(raw["created_at"]) is not datetime:
        raise ValueError(_ERROR)
    raw["created_at"] = _time(raw["created_at"])
    return raw


def checked_entry(value):
    """Rebuild a complete actual JournalEntry without invoking metadata defaults."""
    try:
        return JournalEntry.model_validate(_entry_fields(value), strict=True)
    except _FAILURES:
        raise ValueError(_ERROR) from None


def _plain(value):
    # Iterative conversion keeps the frozen depth512 contract independent of
    # Python comprehension/call-stack depth, including deep legacy payloads.
    root = [None]
    pending = [(value, root, 0, 1)]
    nodes = 1
    while pending:
        item, parent, key, depth = pending.pop()
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError(_ERROR)
        if isinstance(item, BaseModel):
            if type(item) is JournalEntry:
                item = _entry_fields(item)
                item["created_at"] = item["created_at"].isoformat().replace("+00:00", "Z")
            elif type(item) in _PACKETS:
                item = _values(item, type(item))
            else:
                raise ValueError(_ERROR)
        if type(item) is dict:
            nodes += len(item) * 2
            if nodes > _MAX_NODES:
                raise ValueError(_ERROR)
            result = {}
            parent[key] = result
            for name, child in item.items():
                if type(name) is not str or depth + 1 > _MAX_DEPTH:
                    raise ValueError(_ERROR)
                _json_tree(name)
                pending.append((child, result, name, depth + 1))
        elif type(item) is list:
            nodes += len(item)
            if nodes > _MAX_NODES:
                raise ValueError(_ERROR)
            result = [None] * len(item)
            parent[key] = result
            pending.extend((child, result, index, depth + 1) for index, child in enumerate(item))
        else:
            _json_tree(item)
            parent[key] = item
    return root[0]


def _json_shape(raw):
    """Inspect lexical nesting/token counts before invoking the JSON parser."""
    index, depth, nodes = 0, 0, 0
    while index < len(raw):
        char = raw[index]
        if char in " \t\r\n,:":
            index += 1
            continue
        if char in "}]":
            depth -= 1
            if depth < 0:
                raise ValueError(_ERROR)
            index += 1
            continue
        nodes += 1
        if nodes > _MAX_NODES:
            raise ValueError(_ERROR)
        if char in "{[":
            depth += 1
            if depth > _MAX_DEPTH:
                raise ValueError(_ERROR)
            index += 1
        elif char == '"':
            if depth + 1 > _MAX_DEPTH:
                raise ValueError(_ERROR)
            index += 1
            while index < len(raw):
                char = raw[index]
                index += 1
                if char == "\\":
                    index += 1
                elif char == '"':
                    break
        else:
            if depth + 1 > _MAX_DEPTH:
                raise ValueError(_ERROR)
            while index < len(raw) and raw[index] not in " \t\r\n,:{}[]":
                index += 1
            if index < len(raw) and raw[index] in "{[":
                raise ValueError(_ERROR)
    if depth:
        raise ValueError(_ERROR)


def transcript_bytes(value):
    """Canonical complete packet or entry bytes; no old serializer can drop fields."""
    try:
        if type(value) is JournalEntry:
            value = checked_entry(value)
        elif type(value) in _PACKETS:
            value = checked_scan(value, type(value))
        else:
            raise ValueError(_ERROR)
        return _encode(value)
    except _FAILURES:
        raise ValueError(_ERROR) from None


def _encode(value):
    plain = _plain(value)
    _json_tree(plain)
    raw = json.dumps(
        plain, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(raw) > MAX_TRANSCRIPT_SCAN_PAGE_BYTES:
        raise ValueError(_ERROR)
    return raw


def transcript_json(raw, maximum=MAX_TRANSCRIPT_SCAN_PAGE_BYTES):
    try:
        if (
            type(raw) is not bytes
            or type(maximum) is not int
            or not 1 <= maximum <= MAX_TRANSCRIPT_SCAN_PAGE_BYTES
            or len(raw) > maximum
        ):
            raise ValueError(_ERROR)

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError(_ERROR)
                result[key] = value
            return result

        def invalid(_):
            raise ValueError(_ERROR)

        decoded = raw.decode("utf-8")
        _json_shape(decoded)
        result = json.loads(decoded, object_pairs_hook=pairs, parse_constant=invalid)
        if type(result) is not dict:
            raise ValueError(_ERROR)
        _json_tree(result)
        return result
    except _FAILURES:
        raise ValueError(_ERROR) from None


def checked_scan(value, kind):
    try:
        if kind not in _PACKETS or type(value) is not kind:
            raise ValueError(_ERROR)
        return kind.model_validate(value, strict=True)
    except _FAILURES:
        raise ValueError(_ERROR) from None


class _Scan(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )
    schema_version: Literal["1.0"]
    wire_bytes: ClassVar[int]

    def __setattr__(self, name, value):
        raise ValueError(_ERROR) from None

    def __delattr__(self, name):
        raise ValueError(_ERROR) from None

    @model_validator(mode="wrap")
    @classmethod
    def complete_graph(cls, value, handler):
        try:
            if cls not in _PACKETS:
                raise ValueError(_ERROR)
            # Inspect the actual graph before Pydantic can normalize scalar
            # subclasses or discard forged state on a nested old model.
            _json_tree(_plain(value))
            result = handler(_values(value, cls))
            if len(_encode(result)) > cls.wire_bytes:
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            return cls.model_validate(transcript_json(raw, cls.wire_bytes), strict=True)
        except _FAILURES:
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        return transcript_bytes(self)


class TranscriptScanRequest(_Scan):
    wire_bytes: ClassVar[int] = MAX_TRANSCRIPT_SCAN_REQUEST_BYTES
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: _Positive
    after: _Counter
    through_sequence: _Counter | None
    limit: Annotated[int, Field(ge=1, le=20)]

    @field_validator("session_id", "thread_id", mode="before")
    @classmethod
    def identifiers(cls, value):
        return _text(value, 128)


class TranscriptScanPage(_Scan):
    wire_bytes: ClassVar[int] = MAX_TRANSCRIPT_SCAN_PAGE_BYTES
    request: TranscriptScanRequest
    through_sequence: _Counter
    entries: list[JournalEntry] = Field(max_length=20)
    next_after: _Positive | None
    exhausted: bool

    @field_validator("request", mode="before")
    @classmethod
    def exact_request(cls, value):
        return TranscriptScanRequest.model_validate(
            _values(value, TranscriptScanRequest), strict=True
        )

    @field_validator("entries", mode="before")
    @classmethod
    def exact_entries(cls, value):
        if type(value) is not list or len(value) > 20:
            raise ValueError(_ERROR)
        return [checked_entry(item) for item in value]

    @model_validator(mode="after")
    def page_bounds(self):
        original = self.request
        if (
            original.after > self.through_sequence
            or original.through_sequence is not None
            and original.through_sequence != self.through_sequence
            or len(self.entries) > original.limit
        ):
            raise ValueError(_ERROR)
        previous, identities = original.after, set()
        for entry in self.entries:
            if (
                (entry.session_id, entry.thread_id, entry.epoch)
                != (original.session_id, original.thread_id, original.epoch)
                or not previous < entry.sequence <= self.through_sequence
                or entry.event_id in identities
            ):
                raise ValueError(_ERROR)
            previous = entry.sequence
            identities.add(entry.event_id)
        if self.exhausted:
            if self.next_after is not None:
                raise ValueError(_ERROR)
        elif not self.entries or self.next_after != previous:
            raise ValueError(_ERROR)
        return self

    def validate_request(self, request: TranscriptScanRequest) -> Self:
        try:
            result = checked_scan(self, TranscriptScanPage)
            request = checked_scan(request, TranscriptScanRequest)
            if _encode(result.request) != _encode(request):
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


_PACKETS = (TranscriptScanRequest, TranscriptScanPage)
