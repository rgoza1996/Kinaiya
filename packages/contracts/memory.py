"""Strict wire contracts for the optional Converse memory core (issue 113)."""

import unicodedata
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import MAX_PLAYBACK_GENERATION, WireModel

MAX_SAFE = MAX_PLAYBACK_GENERATION
MAX_EXPECTED = MAX_SAFE

Identifier = Annotated[
    str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
]
FactKey = Annotated[
    str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")
]
IdempotencyKey = Annotated[
    str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[!-~]+$")
]
SafePositive = Annotated[int, Field(strict=True, ge=1, le=MAX_SAFE)]
SafeNonNeg = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE)]
ExpectedRevision = Annotated[int, Field(strict=True, ge=0, le=MAX_EXPECTED)]
NodeCount = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE)]
StrictBool = Annotated[bool, Field(strict=True)]
BodyText = Annotated[str, Field(strict=True, min_length=1)]
FactString = Annotated[str, Field(strict=True)]
FactInt = Annotated[int, Field(strict=True, ge=-MAX_SAFE, le=MAX_SAFE)]
FactBool = Annotated[bool, Field(strict=True)]
FactValue = FactString | FactInt | FactBool | None

MemoryKind = Literal["assertion", "observation", "inference"]
MemoryStatus = Literal["ready", "quarantined"]
MemoryStateName = Literal["active", "invalidated", "tainted", "forgotten"]


def _utf8_bytes(value: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("malformed Unicode") from exc


def _check_body(value: str) -> str:
    raw = _utf8_bytes(value)
    if not 1 <= len(raw) <= 2048:
        raise ValueError("body must use 1..2048 UTF-8 bytes")
    if not value.strip():
        raise ValueError("body must not be whitespace-only")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in value):
        raise ValueError("body permits newline/tab only among controls")
    return value


def _check_fact_string(value: str) -> str:
    raw = _utf8_bytes(value)
    if len(raw) > 512:
        raise ValueError("fact string must use at most 512 UTF-8 bytes")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in value):
        raise ValueError("fact string permits newline/tab only among controls")
    return value


def _check_distinct(values: list[str], label: str) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must be distinct")
    return values


class MemoryWire(WireModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryBinding(MemoryWire):
    session_id: Identifier
    thread_id: Identifier
    epoch: SafePositive


class MemoryState(MemoryBinding):
    enabled: StrictBool
    revision: SafeNonNeg
    node_count: NodeCount
    status: MemoryStatus


class MemoryConfigure(MemoryBinding):
    expected_revision: ExpectedRevision
    enabled: StrictBool


class MemoryFact(MemoryWire):
    key: FactKey
    value: FactValue

    @field_validator("value")
    @classmethod
    def _value_bounds(cls, v: FactValue) -> FactValue:
        if isinstance(v, str):
            return _check_fact_string(v)
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            # Bounds are enforced by the annotated type; keep explicit guard.
            if not -MAX_SAFE <= v <= MAX_SAFE:
                raise ValueError("fact int out of safe range")
            return v
        if v is None:
            return v
        raise ValueError("fact value must be string, safe int, bool, or null")


class MemoryCapture(MemoryBinding):
    expected_revision: ExpectedRevision
    idempotency_key: IdempotencyKey
    kind: MemoryKind
    source_event_id: Identifier
    source_revision: SafePositive
    body: BodyText
    fact: MemoryFact | None
    prerequisite_ids: list[Identifier] = Field(default_factory=list, max_length=8)

    @field_validator("body")
    @classmethod
    def _body_bounds(cls, v: str) -> str:
        return _check_body(v)

    @model_validator(mode="after")
    def _kind_rules(self) -> "MemoryCapture":
        _check_distinct(self.prerequisite_ids, "prerequisite_ids")
        if self.kind == "inference":
            if self.fact is not None:
                raise ValueError("inference forbids fact")
            if not self.prerequisite_ids:
                raise ValueError("inference requires parents")
        else:
            if self.fact is None:
                raise ValueError("assertion/observation require fact")
        return self


class MemoryInspect(MemoryBinding):
    node_id: Identifier


class MemoryQuery(MemoryBinding):
    seed_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    fact_keys: list[FactKey] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _selector_rules(self) -> "MemoryQuery":
        seed_nonempty = len(self.seed_ids) > 0
        key_nonempty = len(self.fact_keys) > 0
        if seed_nonempty == key_nonempty:
            raise ValueError("query requires exactly one nonempty selector")
        if seed_nonempty:
            if not 1 <= len(self.seed_ids) <= 8:
                raise ValueError("seed_ids must list 1..8 ids")
            _check_distinct(self.seed_ids, "seed_ids")
        else:
            if not 1 <= len(self.fact_keys) <= 8:
                raise ValueError("fact_keys must list 1..8 keys")
            _check_distinct(self.fact_keys, "fact_keys")
        return self


class MemoryForget(MemoryBinding):
    expected_revision: ExpectedRevision
    idempotency_key: IdempotencyKey
    node_id: Identifier


class MemoryNode(MemoryBinding):
    node_id: Identifier
    ordinal: SafePositive
    kind: MemoryKind
    source_event_id: Identifier
    source_revision: SafePositive
    source_sequence: SafePositive
    prerequisite_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    body: BodyText | None
    fact: MemoryFact | None
    state: MemoryStateName
    content_scope: Literal["local_only"]
    provenance: Literal["owner_curated"]

    @field_validator("body")
    @classmethod
    def _node_body(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _check_body(v)

    @model_validator(mode="after")
    def _node_rules(self) -> "MemoryNode":
        _check_distinct(self.prerequisite_ids, "prerequisite_ids")
        if self.state == "forgotten":
            if self.body is not None:
                raise ValueError("forgotten body must be null")
            if self.fact is not None:
                raise ValueError("forgotten fact must be null")
            return self
        if self.body is None:
            raise ValueError("body is required unless forgotten")
        if self.kind == "inference":
            if self.fact is not None:
                raise ValueError("inference forbids fact")
            if not self.prerequisite_ids:
                raise ValueError("inference requires parents")
        else:
            if self.fact is None:
                raise ValueError("assertion/observation require fact")
        return self


class MemoryCaptureResult(MemoryWire):
    state: MemoryState
    node: MemoryNode


class MemoryQueryResult(MemoryWire):
    state: MemoryState
    nodes: list[MemoryNode] = Field(max_length=64)


class MemoryForgetResult(MemoryWire):
    state: MemoryState
    forgotten_ids: list[Identifier] = Field(max_length=256)
