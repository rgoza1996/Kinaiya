"""Codex-authored standing-work owner grammar and explicit-clock civil time.

These pure contracts grant no native admission, producer readiness or spending
cap. Occurrence selection is not dispatch deduplication, and saved proof never
establishes that accepted work or external effects have settled.
"""

import hashlib
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, ClassVar, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.agent_control import (
    BackendId,
    ContentScope,
    ControlInstance,
    ControlKey,
    Digest,
    NextRevision,
    PositiveInteger,
    Revision,
    SafeId,
    agent_control_json,
)
from packages.contracts.workspace_settings import WorkspacePin

MAX_STANDING_REQUEST_BYTES = 131072
MAX_STANDING_CANONICAL_BYTES = 114688
MAX_STANDING_RECEIPT_BYTES = 131072
MAX_STANDING_SNAPSHOT_BYTES = 147456
MAX_STANDING_PROOF_BYTES = 262144
# Two canonical requests leave this total reserve for receipt/proof metadata.
STANDING_PROOF_RESERVE_BYTES = 32768
MAX_STANDING_CIVIL_DATES = 32
MAX_STANDING_SECONDS = 30 * 86400
MAX_QUIET_ADVANCE_MINUTES = 2880

_MAX_INT = 9_007_199_254_740_991
_ERROR = "Invalid standing work contract"
_PROOF_ERROR = "Standing work proof does not match the original request"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


class StandingConflict(ValueError):
    """A definite native authority conflict, without an input-bearing reason."""

    def __init__(self):
        super().__init__("Standing work conflicts with retained authority")


class StandingUnavailable(RuntimeError):
    """Native proof or persistence is unavailable, not confirmed absent."""

    def __init__(self):
        super().__init__("Standing work authority is unavailable")


def _tree(value: Any) -> None:
    pending = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 12 or nodes > 2048:
            raise ValueError(_ERROR)
        if isinstance(item, BaseModel):
            # model_copy can place forbidden fields in the actual instance;
            # model_dump silently drops them. Inspect closure before any dump,
            # including nested models and Pydantic's separate field metadata.
            fields = type(item).model_fields
            values = getattr(item, "__dict__", None)
            supplied = getattr(item, "__pydantic_fields_set__", None)
            if (
                type(values) is not dict
                or values.keys() != fields.keys()
                or type(supplied) is not set
                or not supplied <= fields.keys()
                or getattr(item, "__pydantic_extra__", {}) is not None
            ):
                raise ValueError(_ERROR)
            item = values
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError(_ERROR)
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif type(item) in (list, tuple):
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            item.encode("utf-8")
        elif type(item) is int:
            if abs(item) > _MAX_INT:
                raise ValueError(_ERROR)
        elif item is not None and type(item) is not bool:
            raise ValueError(_ERROR)


def _encode(value: Any) -> bytes:
    def model(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="python", warnings=False)
        raise ValueError(_ERROR)

    _tree(value)
    return json.dumps(
        value,
        default=model,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def standing_json(raw: bytes, maximum: int = MAX_STANDING_REQUEST_BYTES) -> dict:
    """Decode one bounded UTF-8 object without duplicates, floats or coercion."""
    try:
        return agent_control_json(raw, maximum=maximum)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


class _Closed(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        hide_input_in_errors=True,
    )
    wire_bytes: ClassVar[int] = MAX_STANDING_REQUEST_BYTES

    @model_validator(mode="wrap")
    @classmethod
    def checked_object(cls, value, handler):
        try:
            _tree(value)
            if isinstance(value, cls):
                value = dict(value.__dict__)
            if type(value) is not dict:
                raise ValueError(_ERROR)
            result = handler(value)
            if len(_encode(result)) > cls.wire_bytes:
                raise ValueError(_ERROR)
            return result
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError(_ERROR) from None

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            maximum = max(MAX_STANDING_REQUEST_BYTES, cls.wire_bytes)
            return cls.model_validate(standing_json(raw, maximum=maximum))
        except (ValueError, TypeError, RecursionError):
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self)


def canonical_bytes(model: BaseModel) -> bytes:
    """Revalidate complete constructed/copied models before identity comparison."""
    try:
        if not isinstance(model, BaseModel):
            raise ValueError(_ERROR)
        value = _checked(model, type(model))
        return _encode(value)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


def _civil_date(value: str) -> date:
    try:
        if type(value) is not str or _DATE.fullmatch(value) is None:
            raise ValueError(_ERROR)
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(_ERROR) from None


def _civil_time(value: str) -> time:
    if type(value) is not str or _TIME.fullmatch(value) is None:
        raise ValueError(_ERROR)
    return time.fromisoformat(value)


def _utc(value: str) -> datetime:
    try:
        if type(value) is not str or _INSTANT.fullmatch(value) is None:
            raise ValueError(_ERROR)
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, TypeError):
        raise ValueError(_ERROR) from None


def _zone(value: str) -> ZoneInfo:
    try:
        if type(value) is not str or not value:
            raise ValueError(_ERROR)
        return ZoneInfo(value)
    except (ValueError, TypeError, ZoneInfoNotFoundError, UnicodeError, OSError):
        raise ValueError(_ERROR) from None


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _nominal_range(schedule) -> tuple[date, date]:
    start = _civil_date(schedule.start_local_date)
    end = _utc(schedule.valid_until_utc).astimezone(_zone(schedule.timezone)).date()
    if not 0 <= (end - start).days < MAX_STANDING_CIVIL_DATES:
        raise ValueError(_ERROR)
    return start, end


class StandingTaskSession(_Closed):
    session_id: SafeId
    thread_id: SafeId
    epoch: PositiveInteger

    @model_validator(mode="after")
    def native_ids(self):
        if "\0" in self.session_id or "\0" in self.thread_id:
            raise ValueError(_ERROR)
        return self


class StandingQuietHours(_Closed):
    timezone: str
    start_local_time: str
    end_local_time: str

    @model_validator(mode="after")
    def civil_interval(self):
        _zone(self.timezone)
        if _civil_time(self.start_local_time) == _civil_time(self.end_local_time):
            raise ValueError(_ERROR)
        return self


class StandingDelivery(_Closed):
    kind: Literal["in_app"]
    attention: Literal["routine", "important"]
    preview: Literal["status_only"]
    quiet_hours: StandingQuietHours | None


class DailySchedule(_Closed):
    kind: Literal["daily_local"]
    timezone: str
    local_time: str
    fold_policy: Literal["earlier"]
    gap_policy: Literal["skip"]
    grace_seconds: Annotated[int, Field(ge=1, le=86400)]
    missed_run_policy: Literal["latest_within_grace"]
    start_local_date: str
    valid_until_utc: str

    @model_validator(mode="after")
    def civil_window(self):
        _civil_time(self.local_time)
        _nominal_range(self)
        return self


class StandingAuthorization(_Closed):
    label: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=16000)
    content_scope: ContentScope
    enabled: bool
    task_session: StandingTaskSession
    workspace_pin: WorkspacePin
    schedule: DailySchedule
    delivery: StandingDelivery
    overlap_policy: Literal["one_unresolved_per_authorization"]
    maximum_runs: Annotated[int, Field(ge=1, le=32)]
    inference_authorization: Literal["configured_agent_profile"]


class StandingChange(_Closed):
    wire_bytes: ClassVar[int] = MAX_STANDING_CANONICAL_BYTES
    schema_version: Literal["1.0"]
    idempotency_key: ControlKey
    expected_backend: BackendId
    expected_instance: ControlInstance
    expected_revision: NextRevision
    responsibility_id: ControlInstance
    authorization: StandingAuthorization


def authorization_digest(request: StandingChange, revision: int) -> str:
    """Bind the entire immutable owner request to its native saved revision."""
    try:
        if not isinstance(request, StandingChange) or type(revision) is not int:
            raise ValueError(_ERROR)
        if not 1 <= revision <= _MAX_INT:
            raise ValueError(_ERROR)
        original = _checked(request, StandingChange)
        body = {
            "responsibility_id": original.responsibility_id,
            "revision": revision,
            "request": original.model_dump(mode="json"),
        }
        return hashlib.sha256(b"kinaiya.standing.authorization.v1\0" + _encode(body)).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


class StandingChangeReceipt(_Closed):
    wire_bytes: ClassVar[int] = MAX_STANDING_RECEIPT_BYTES
    schema_version: Literal["1.0"]
    status: Literal["responsibility_saved"]
    backend_id: BackendId
    service_instance: ControlInstance
    revision: PositiveInteger
    request: StandingChange
    authorization_digest: Digest

    @model_validator(mode="after")
    def original_proof(self):
        if (
            self.backend_id != self.request.expected_backend
            or self.service_instance != self.request.expected_instance
            or self.revision != self.request.expected_revision + 1
            or self.authorization_digest != authorization_digest(self.request, self.revision)
        ):
            raise ValueError(_PROOF_ERROR)
        # The actual echo closes under the response bound. Request validation
        # already reserves 32768 bytes beyond its two canonical copies.
        proof = {"request": self.request, "receipt": self}
        if len(_encode(proof)) > MAX_STANDING_PROOF_BYTES:
            raise ValueError(_ERROR)
        return self

    def validate_request(self, request: StandingChange) -> Self:
        try:
            result = _checked(self, type(self))
            if not isinstance(request, StandingChange):
                raise ValueError(_PROOF_ERROR)
            if canonical_bytes(result.request) != canonical_bytes(request):
                raise ValueError(_PROOF_ERROR)
            return result
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError(_PROOF_ERROR) from None


def record_digest(
    receipt: StandingChangeReceipt,
    activation: Literal["pending", "armed"],
    authorized_from_utc: str | None,
) -> str:
    """Bind a saved definition to an explicit post-commit activation observation."""
    try:
        receipt = _checked(receipt, StandingChangeReceipt)
        if type(activation) is not str or activation not in ("pending", "armed"):
            raise ValueError(_ERROR)
        if activation == "pending":
            if authorized_from_utc is not None:
                raise ValueError(_ERROR)
        else:
            _utc(authorized_from_utc)
            validate_authorization_at(receipt.request.authorization, authorized_from_utc)
        body = {
            "receipt": receipt.model_dump(mode="json"),
            "activation": activation,
            "authorized_from_utc": authorized_from_utc,
        }
        return hashlib.sha256(b"kinaiya.standing.record.v1\0" + _encode(body)).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


class StandingRecord(_Closed):
    """Saved definition and activation proof; neither is proof of execution."""

    wire_bytes: ClassVar[int] = MAX_STANDING_RECEIPT_BYTES
    schema_version: Literal["1.0"]
    receipt: StandingChangeReceipt
    activation: Literal["pending", "armed"]
    authorized_from_utc: str | None
    record_digest: Digest

    @model_validator(mode="after")
    def activation_proof(self):
        if self.record_digest != record_digest(
            self.receipt, self.activation, self.authorized_from_utc
        ):
            raise ValueError(_PROOF_ERROR)
        return self


class StandingCurrent(_Closed):
    """Minimal current revision/authority projection without another full request."""

    wire_bytes: ClassVar[int] = 4096
    schema_version: Literal["1.0"]
    backend_id: BackendId
    instance_id: ControlInstance
    responsibility_id: ControlInstance
    revision: Revision
    control_writable: bool


class StandingResponsibilitySnapshot(StandingCurrent):
    """Current definition with a separately observed Session writer generation."""

    wire_bytes: ClassVar[int] = MAX_STANDING_SNAPSHOT_BYTES
    record: StandingRecord | None

    @model_validator(mode="after")
    def current_definition(self):
        if self.record is None:
            if self.revision != 0:
                raise ValueError(_PROOF_ERROR)
        elif (
            self.revision != self.record.receipt.revision
            or self.backend_id != self.record.receipt.backend_id
            or self.responsibility_id != self.record.receipt.request.responsibility_id
        ):
            raise ValueError(_PROOF_ERROR)
        return self


class StandingChangeLookup(_Closed):
    """Full-original proof; only native issued-generation history proves retirement."""

    wire_bytes: ClassVar[int] = MAX_STANDING_PROOF_BYTES
    schema_version: Literal["1.0"]
    status: Literal["recorded", "unobserved"]
    request: StandingChange
    current: StandingCurrent
    request_instance: Literal["current", "retired"]
    receipt: StandingChangeReceipt | None
    mutation_fenced: bool

    @model_validator(mode="after")
    def original_observation(self):
        if (
            self.current.backend_id != self.request.expected_backend
            or self.current.responsibility_id != self.request.responsibility_id
            or (self.request_instance == "current")
            != (self.current.instance_id == self.request.expected_instance)
        ):
            raise ValueError(_PROOF_ERROR)
        if self.status == "recorded":
            if self.receipt is None or self.mutation_fenced:
                raise ValueError(_PROOF_ERROR)
            self.receipt.validate_request(self.request)
            if self.current.revision < self.receipt.revision:
                raise ValueError(_PROOF_ERROR)
        else:
            fenced = (
                self.request_instance == "retired"
                or self.current.revision > self.request.expected_revision
            )
            if self.receipt is not None or self.mutation_fenced != fenced:
                raise ValueError(_PROOF_ERROR)
        return self

    def validate_request(self, request: StandingChange) -> Self:
        try:
            result = _checked(self, type(self))
            if not isinstance(request, StandingChange):
                raise ValueError(_PROOF_ERROR)
            if canonical_bytes(result.request) != canonical_bytes(request):
                raise ValueError(_PROOF_ERROR)
            return result
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError(_PROOF_ERROR) from None


class StandingResponsibilityPage(_Closed):
    """Byte/node-bounded whole records; the cursor names the last returned ID."""

    wire_bytes: ClassVar[int] = MAX_STANDING_PROOF_BYTES
    schema_version: Literal["1.0"]
    backend_id: BackendId
    instance_id: ControlInstance
    control_writable: bool
    items: tuple[StandingRecord, ...] = Field(max_length=20)
    next_cursor: ControlInstance | None

    @field_validator("items", mode="before")
    @classmethod
    def immutable_items(cls, value):
        if type(value) not in (list, tuple):
            raise ValueError(_ERROR)
        return tuple(value)

    @model_validator(mode="after")
    def ordered_records(self):
        previous = None
        for record in self.items:
            current = record.receipt.request.responsibility_id
            if record.receipt.backend_id != self.backend_id or (
                previous is not None and current <= previous
            ):
                raise ValueError(_PROOF_ERROR)
            previous = current
        if self.next_cursor is not None and self.next_cursor != previous:
            raise ValueError(_PROOF_ERROR)
        return self


class DailyOccurrence(_Closed):
    local_date: str
    due_utc: str

    @model_validator(mode="after")
    def exact_time(self):
        _civil_date(self.local_date)
        _utc(self.due_utc)
        return self


def _checked(model, kind):
    if not isinstance(model, kind):
        raise ValueError(_ERROR)
    _tree(model)
    # Preserve nested typed instances for strict revalidation instead of first
    # flattening an incorrectly typed copied value into an acceptable dict.
    return kind.model_validate(dict(model.__dict__))


def _candidates(local: datetime, zone: ZoneInfo) -> list[datetime]:
    values = set()
    for fold in (0, 1):
        instant = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            values.add(instant)
    return sorted(values)


def resolve_daily(schedule: DailySchedule, local_date: str) -> DailyOccurrence | None:
    """Resolve a nominal date once: skip gaps and choose the earlier fold."""
    try:
        schedule = _checked(schedule, DailySchedule)
        day = _civil_date(local_date)
        start, end = _nominal_range(schedule)
        if not start <= day <= end:
            return None
        local = datetime.combine(day, _civil_time(schedule.local_time))
        candidates = _candidates(local, _zone(schedule.timezone))
        if not candidates or candidates[0] >= _utc(schedule.valid_until_utc):
            return None
        return DailyOccurrence(local_date=local_date, due_utc=_utc_text(candidates[0]))
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


def validate_authorization_at(authorization: StandingAuthorization, saved_at_utc: str) -> None:
    """Apply the finite UTC grant bound using an explicit native save clock."""
    try:
        authorization = _checked(authorization, StandingAuthorization)
        saved = _utc(saved_at_utc)
        expiry = _utc(authorization.schedule.valid_until_utc)
        if not timedelta(0) < expiry - saved <= timedelta(seconds=MAX_STANDING_SECONDS):
            raise ValueError(_ERROR)
        _nominal_range(authorization.schedule)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


def latest_relevant(
    schedule: DailySchedule, saved_at_utc: str, now_utc: str
) -> DailyOccurrence | None:
    """Select at most the latest in-grace due instant, never pre-save backlog."""
    try:
        schedule = _checked(schedule, DailySchedule)
        saved, now = _utc(saved_at_utc), _utc(now_utc)
        if now < saved or now >= _utc(schedule.valid_until_utc):
            return None
        start, end = _nominal_range(schedule)
        latest = None
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            occurrence = resolve_daily(schedule, day.isoformat())
            if occurrence is not None:
                due = _utc(occurrence.due_utc)
                if saved <= due <= now and (latest is None or due > _utc(latest.due_utc)):
                    latest = occurrence
        if latest is None or (now - _utc(latest.due_utc)).total_seconds() > schedule.grace_seconds:
            return None
        return latest
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


def _quiet_boundary(local: datetime, zone: ZoneInfo, *, latest: bool) -> datetime:
    for minute in range(MAX_QUIET_ADVANCE_MINUTES + 1):
        candidates = _candidates(local + timedelta(minutes=minute), zone)
        if candidates:
            return candidates[-1] if latest else candidates[0]
    raise ValueError(_ERROR)


def quiet_until(quiet: StandingQuietHours | None, now_utc: str) -> str | None:
    """Return the actual exclusive UTC end of the currently active interval."""
    try:
        now = _utc(now_utc)
        if quiet is None:
            return None
        quiet = _checked(quiet, StandingQuietHours)
        zone = _zone(quiet.timezone)
        today = now.astimezone(zone).date()
        start_time = _civil_time(quiet.start_local_time)
        end_time = _civil_time(quiet.end_local_time)
        ends = []
        # Ordinary intervals need yesterday/today. Two additional nominal days
        # cover the bounded 48-hour gap advance, including a skipped civil day.
        for previous in range(4):
            if today.toordinal() <= previous:
                continue
            day = today - timedelta(days=previous)
            end_day = day + timedelta(days=int(end_time < start_time))
            start = _quiet_boundary(datetime.combine(day, start_time), zone, latest=False)
            end = _quiet_boundary(datetime.combine(end_day, end_time), zone, latest=True)
            if start <= now < end:
                ends.append(end)
        return _utc_text(max(ends)) if ends else None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None
