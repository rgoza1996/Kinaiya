"""Pure standing initial-entry proof; no admission, launch or settlement authority."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Literal, Self

from pydantic import TypeAdapter, field_validator, model_validator

from packages.contracts.agent_control import Digest, PositiveInteger, SafeId
from packages.contracts.models import JournalEntry
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionIntent,
    standing_initial_request,
)
from packages.contracts.standing_work import (
    _checked,
    _Closed,
    _encode,
    _tree,
    canonical_bytes,
    standing_json,
)

MAX_STANDING_INITIAL_PROOF_BYTES = 163840
MAX_STANDING_TASK_ADMISSION_BYTES = 180224
_MAX_REQUEST_BYTES = 65536
_MAX_ENTRY_BYTES = 69632
_ERROR = "Invalid standing initial proof"
_FAILURES = (ValueError, TypeError, UnicodeError, RecursionError, OverflowError)
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "session_id",
        "thread_id",
        "epoch",
        "kind",
        "payload",
        "causes",
        "task_refs",
        "idempotency_key",
    }
)
_ENTRY_FIELDS = _REQUEST_FIELDS | {"event_id", "sequence", "origin", "created_at"}
_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{6}))?Z"
)
_EVENT_ID_ADAPTER = TypeAdapter(SafeId)
_SEQUENCE_ADAPTER = TypeAdapter(PositiveInteger)


def _timestamp(value):
    if type(value) is not str:
        raise ValueError(_ERROR)
    match = _TIMESTAMP.fullmatch(value)
    if match is None or match[7] == "000000":
        raise ValueError(_ERROR)
    return datetime(
        *(int(match[index]) for index in range(1, 7)),
        microsecond=int(match[7] or "0"),
        tzinfo=UTC,
    )


def _event_id(value):
    if type(value) is not str or "\0" in value:
        raise ValueError(_ERROR)
    value.encode("utf-8")
    return _EVENT_ID_ADAPTER.validate_python(value, strict=True)


def _sequence(value):
    if type(value) is not int:
        raise ValueError(_ERROR)
    return _SEQUENCE_ADAPTER.validate_python(value, strict=True)


def _json_graph(value):
    # The common walk enforces Unicode, safe integers and aggregate graph
    # bounds. The entry additionally forbids tuples and typed payload values:
    # an old JournalEntry dump must not turn non-JSON input into accepted JSON.
    _tree(value)
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError(_ERROR)
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
        elif item is not None and type(item) not in (str, int, bool):
            raise ValueError(_ERROR)


def _entry_json(entry):
    if type(entry) is dict:
        _json_graph(entry)
        raw = dict(entry)
    elif type(entry) is JournalEntry:
        values = getattr(entry, "__dict__", None)
        supplied = getattr(entry, "__pydantic_fields_set__", None)
        if (
            type(values) is not dict
            or any(type(key) is not str for key in values)
            or values.keys() != _ENTRY_FIELDS
            or type(supplied) is not set
            or any(type(key) is not str for key in supplied)
            or not supplied <= _ENTRY_FIELDS
            or getattr(entry, "__pydantic_extra__", False) is not None
        ):
            raise ValueError(_ERROR)
        raw = dict(values)
        created = raw.pop("created_at")
        if (
            type(created) is not datetime
            or created.tzinfo is None
            or created.utcoffset() != timedelta(0)
        ):
            raise ValueError(_ERROR)
        _json_graph(raw)
        # Consume the existing native datetime only after inspecting every
        # actual field. Never invoke event/time defaults or normalize raw text.
        raw["created_at"] = entry.model_dump(mode="json", warnings=False)["created_at"]
        _json_graph(raw)
    else:
        raise ValueError(_ERROR)
    if raw.keys() != _ENTRY_FIELDS or raw["schema_version"] != "1.0":
        raise ValueError(_ERROR)
    _event_id(raw["event_id"])
    _sequence(raw["sequence"])
    _timestamp(raw["created_at"])
    if raw["origin"] != "standing-supervisor":
        raise ValueError(_ERROR)
    return raw


def _request_json(claim):
    request = standing_initial_request(claim)
    if len(request.model_dump_json().encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ValueError(_ERROR)
    raw = request.model_dump(mode="json")
    _json_graph(raw)
    if raw.keys() != _REQUEST_FIELDS:
        raise ValueError(_ERROR)
    return raw


def standing_entry(entry, claim: StandingExecutionClaim) -> JournalEntry:
    """Validate an existing complete entry; never infer native provenance."""
    try:
        claim = _checked(claim, StandingExecutionClaim)
        raw = _entry_json(entry)
        expected = _request_json(claim)
        actual = {key: raw[key] for key in _REQUEST_FIELDS}
        # JSON identity distinguishes bool/int and every nested field. Python
        # equality alone would incorrectly equate True with 1 and tuples/lists.
        if _encode(actual) != _encode(expected):
            raise ValueError(_ERROR)
        detached = json.loads(_encode(raw))
        detached["created_at"] = _timestamp(detached["created_at"])
        result = JournalEntry.model_validate(detached, strict=True)
        if len(result.model_dump_json().encode("utf-8")) > _MAX_ENTRY_BYTES:
            raise ValueError(_ERROR)
        return result
    except _FAILURES:
        raise ValueError(_ERROR) from None


def standing_entry_digest(
    claim: StandingExecutionClaim, event_id: str, sequence: int, created_at: str
) -> str:
    """Hash complete claim-bound entry identity without minting metadata."""
    try:
        claim = _checked(claim, StandingExecutionClaim)
        _event_id(event_id)
        _sequence(sequence)
        _timestamp(created_at)
        entry = standing_entry(
            {
                **_request_json(claim),
                "event_id": event_id,
                "sequence": sequence,
                "origin": "standing-supervisor",
                "created_at": created_at,
            },
            claim,
        )
        body = {
            "claim_id": claim.claim_id,
            "claim_digest": claim.claim_digest,
            "entry": entry.model_dump(mode="json"),
        }
        return hashlib.sha256(b"kinaiya.standing.initial.v1\0" + _encode(body)).hexdigest()
    except _FAILURES:
        raise ValueError(_ERROR) from None


class _Initial(_Closed):
    schema_version: Literal["1.0"]

    @model_validator(mode="wrap")
    @classmethod
    def initial_object(cls, value, handler):
        try:
            return handler(value)
        except _FAILURES:
            raise ValueError(_ERROR) from None

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            return cls.model_validate(standing_json(raw, maximum=cls.wire_bytes))
        except _FAILURES:
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        try:
            return canonical_bytes(self)
        except _FAILURES:
            raise ValueError(_ERROR) from None


class StandingInitialProof(_Initial):
    """Immutable metadata proof, never an admission or worker launch signal."""

    wire_bytes: ClassVar[int] = MAX_STANDING_INITIAL_PROOF_BYTES
    claim: StandingExecutionClaim
    event_id: SafeId
    sequence: PositiveInteger
    created_at: str
    entry_digest: Digest

    @field_validator("event_id")
    @classmethod
    def exact_event(cls, value):
        return _event_id(value)

    @field_validator("created_at")
    @classmethod
    def exact_created_at(cls, value):
        _timestamp(value)
        return value

    @model_validator(mode="after")
    def exact_entry(self):
        if self.entry_digest != standing_entry_digest(
            self.claim, self.event_id, self.sequence, self.created_at
        ):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: StandingExecutionIntent) -> Self:
        try:
            result = _checked(self, StandingInitialProof)
            result.claim.validate_request(original)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None

    def journal_entry(self) -> JournalEntry:
        try:
            proof = _checked(self, StandingInitialProof)
            return standing_entry(
                {
                    **_request_json(proof.claim),
                    "event_id": proof.event_id,
                    "sequence": proof.sequence,
                    "origin": "standing-supervisor",
                    "created_at": proof.created_at,
                },
                proof.claim,
            )
        except _FAILURES:
            raise ValueError(_ERROR) from None


class StandingTaskAdmission(_Initial):
    """Native disposition grammar; parsing cannot prove a successful COMMIT."""

    wire_bytes: ClassVar[int] = MAX_STANDING_TASK_ADMISSION_BYTES
    disposition: Literal["created", "retained"]
    proof: StandingInitialProof

    def validate_request(self, original: StandingExecutionIntent) -> Self:
        try:
            result = _checked(self, StandingTaskAdmission)
            result.proof.validate_request(original)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


def standing_initial_proof(claim: StandingExecutionClaim, entry) -> StandingInitialProof:
    """Construct detached metadata proof from one already existing entry."""
    try:
        claim = _checked(claim, StandingExecutionClaim)
        raw = standing_entry(entry, claim).model_dump(mode="json")
        return StandingInitialProof(
            schema_version="1.0",
            claim=claim,
            event_id=raw["event_id"],
            sequence=raw["sequence"],
            created_at=raw["created_at"],
            entry_digest=standing_entry_digest(
                claim, raw["event_id"], raw["sequence"], raw["created_at"]
            ),
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None
