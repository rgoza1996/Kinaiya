"""Strict native history pages for measured Converse context (ADR 0057)."""

import json
from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import MAX_PLAYBACK_GENERATION, JournalEntry, WireModel

MAX_CONTEXT_PAGE_BYTES = 262144
MAX_CONTEXT_REQUEST_BYTES = 131072
MAX_CONTEXT_RESPONSE_BYTES = MAX_CONTEXT_PAGE_BYTES * 2
CONTEXT_KINDS = frozenset({"conversation.user", "conversation.window", "conversation.converse"})
QUALIFICATION_KINDS = frozenset({"conversation.revision", "delivery.updated"})
ContextIdentifier = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
ContextSequence = Annotated[int, Field(strict=True, ge=1, le=MAX_PLAYBACK_GENERATION)]
ContextBaseIds = Annotated[list[ContextIdentifier], Field(min_length=1, max_length=200)]


def context_json(raw, *, maximum):
    """Reject ambiguous or oversized protocol JSON before model validation."""
    if len(raw) > maximum:
        raise ValueError("Native context JSON exceeds its byte limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate native context JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite native context JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


class ContextHistoryRequest(WireModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_event_id: ContextIdentifier
    through_sequence: ContextSequence | None = None
    before_sequence: ContextSequence | None = None
    limit: int = Field(default=100, strict=True, ge=1, le=200)

    @model_validator(mode="after")
    def cursor_pair(self):
        if (self.through_sequence is None) != (self.before_sequence is None):
            raise ValueError("A continuation requires both watermark and before cursor")
        if self.before_sequence is not None and self.before_sequence > self.through_sequence:
            raise ValueError("The before cursor exceeds the watermark")
        return self


class ContextHistoryPage(WireModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entry_kinds: ClassVar[frozenset[str]] = CONTEXT_KINDS

    schema_version: Literal["1.0"] = Field(...)
    source_event_id: ContextIdentifier
    session_id: ContextIdentifier
    thread_id: ContextIdentifier
    epoch: ContextSequence
    through_sequence: ContextSequence
    entries: list[JournalEntry] = Field(max_length=200)
    next_before_sequence: ContextSequence | None
    exhausted: bool = Field(strict=True)

    @field_validator("entries", mode="before")
    @classmethod
    def strict_native_entries(cls, values):
        # JournalEntry is a legacy permissive DTO. Do not let its coercions turn
        # a boolean/float counter or numeric identity into valid paging evidence.
        if not isinstance(values, list):
            raise ValueError("Native context entries must be a list")
        parsed = []
        for entry in values:
            raw = entry.model_dump(mode="python") if isinstance(entry, JournalEntry) else entry
            if not isinstance(raw, dict):
                raise ValueError("Invalid native context entry")
            if any(
                not isinstance(raw.get(key), str)
                for key in (
                    "schema_version",
                    "event_id",
                    "session_id",
                    "thread_id",
                    "kind",
                    "origin",
                    "idempotency_key",
                )
            ) or any(
                type(raw.get(key)) is not int or not 1 <= raw[key] <= MAX_PLAYBACK_GENERATION
                for key in ("sequence", "epoch")
            ):
                raise ValueError("Native context entry identities and counters must be strict")
            for key in ("causes", "task_refs"):
                if not isinstance(raw.get(key), list) or any(
                    not isinstance(value, str) for value in raw[key]
                ):
                    raise ValueError("Native context entry references must be strict")
            if isinstance(entry, JournalEntry):
                parsed.append(entry)
            else:
                # A before-validator materializes Python dictionaries even in
                # JSON validation mode. Restore native JSON-mode timestamp
                # validation without allowing legacy scalar coercions.
                if isinstance(raw.get("created_at"), datetime):
                    raw = {**raw, "created_at": raw["created_at"].isoformat()}
                parsed.append(JournalEntry.model_validate_json(json.dumps(raw), strict=True))
        return parsed

    @model_validator(mode="after")
    def native_page(self):
        previous = self.through_sequence + 1
        size = 0
        for entry in self.entries:
            if (
                type(entry.sequence) is not int
                or not 1 <= entry.sequence < previous
                or (entry.session_id, entry.thread_id, entry.epoch)
                != (self.session_id, self.thread_id, self.epoch)
                or entry.kind not in self.entry_kinds
                or entry.event_id == self.source_event_id
            ):
                raise ValueError("Invalid native history scope, kind or descending order")
            previous = entry.sequence
            size += len(entry.model_dump_json().encode())
        if size > MAX_CONTEXT_PAGE_BYTES:
            raise ValueError("Native history page exceeds its byte limit")
        if self.exhausted:
            if self.next_before_sequence is not None:
                raise ValueError("An exhausted page cannot have a continuation")
        elif not self.entries or self.next_before_sequence != self.entries[-1].sequence:
            raise ValueError("A continuation must progress past the last returned entry")
        return self

    def validate_request(self, request: ContextHistoryRequest):
        """Bind a validated response to this call; scope continuity is checked by the builder."""
        if self.source_event_id != request.source_event_id or len(self.entries) > request.limit:
            raise ValueError("Native history page does not match the request")
        if (
            request.through_sequence is not None
            and self.through_sequence != request.through_sequence
        ):
            raise ValueError("Native history watermark changed")
        if request.before_sequence is not None and (
            any(entry.sequence >= request.before_sequence for entry in self.entries)
            or (
                self.next_before_sequence is not None
                and self.next_before_sequence >= request.before_sequence
            )
        ):
            raise ValueError("Native history watermark or cursor changed")
        return self


class ContextQualificationRequest(WireModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_event_id: ContextIdentifier
    through_sequence: ContextSequence
    base_event_ids: ContextBaseIds
    before_sequence: ContextSequence | None = None
    limit: int = Field(default=100, strict=True, ge=1, le=200)

    @field_validator("base_event_ids")
    @classmethod
    def distinct_ids(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Context base identities must be distinct")
        return values

    @model_validator(mode="after")
    def bounded_cursor(self):
        if self.before_sequence is not None and self.before_sequence > self.through_sequence:
            raise ValueError("The before cursor exceeds the watermark")
        return self


class ContextQualificationPage(ContextHistoryPage):
    entry_kinds: ClassVar[frozenset[str]] = QUALIFICATION_KINDS
    base_event_ids: ContextBaseIds

    @field_validator("base_event_ids")
    @classmethod
    def distinct_ids(cls, values):
        return ContextQualificationRequest.distinct_ids(values)

    def validate_request(self, request: ContextQualificationRequest):
        super().validate_request(request)
        if self.base_event_ids != request.base_event_ids:
            raise ValueError("Native qualification base identities changed")
        return self
