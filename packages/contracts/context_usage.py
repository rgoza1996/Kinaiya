"""Read-only observations of already measured native prompts (ADR 0104)."""

import json
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_COUNT = 9_007_199_254_740_991
Count = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNT)]
Identity = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Hash = Annotated[str, Field(strict=True, min_length=64, max_length=64)]


class StrictObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def text_controls(cls, value):
        if isinstance(value, str) and (
            len(value.encode("utf-8")) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Invalid observation text")
        return value


class ContextModel(StrictObservation):
    source: Literal["host", "catalog", "unmanaged"]
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    local: bool
    reasoning: str | None = Field(min_length=1, max_length=16)
    settings_revision: Count | None
    catalog_revision: Count | None
    catalog_binding: "ContextCatalogBinding | None"
    profile_id: str | None = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def pin_shape(self):
        if self.source == "catalog":
            if self.catalog_binding is None or self.catalog_revision is None:
                raise ValueError("Catalog observation requires its exact pin")
        elif self.catalog_binding is not None or self.catalog_revision is not None:
            raise ValueError("Noncatalog observation cannot claim a catalog pin")
        if (self.source == "unmanaged") != (self.settings_revision is None):
            raise ValueError("Observation model ownership is inconsistent")
        return self


class ContextCatalogBinding(StrictObservation):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", min_length=1, max_length=64)
    generation: int = Field(ge=1, le=64)
    configuration_sha256: Hash

    @field_validator("id", "configuration_sha256")
    @classmethod
    def exact_token(cls, value, info):
        pattern = (
            r"[a-f0-9]{64}"
            if info.field_name == "configuration_sha256"
            else r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
        )
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid catalog observation pin")
        return value


class ContextScope(StrictObservation):
    session_id: Identity
    thread_id: Identity
    epoch: int = Field(ge=1, le=MAX_COUNT)
    source_event_ids: tuple[Identity, ...] = Field(max_length=128)
    task_id: Identity | None
    objective_revision: int | None = Field(ge=1, le=MAX_COUNT)
    harness_version: Identity | None
    through_sequence: int | None = Field(ge=1, le=MAX_COUNT)

    @model_validator(mode="after")
    def task_shape(self):
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("Duplicate observation source")
        if any(
            any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in self.source_event_ids
        ):
            raise ValueError("Invalid observation source")
        if (self.task_id is None) != (self.objective_revision is None):
            raise ValueError("Incomplete task observation")
        return self


class ContextMeasurement(StrictObservation):
    input_tokens: int = Field(ge=1, le=MAX_COUNT)
    context_tokens: int = Field(ge=64, le=MAX_COUNT)
    reserved_output_tokens: int = Field(ge=16, le=MAX_COUNT)
    headroom_tokens: Count
    request_sha256: Hash
    binding_sha256: Hash

    @field_validator("request_sha256", "binding_sha256")
    @classmethod
    def exact_hash(cls, value):
        if re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise ValueError("Invalid observation digest")
        return value

    @model_validator(mode="after")
    def exact_headroom(self):
        if (
            self.context_tokens - self.input_tokens - self.reserved_output_tokens
            != self.headroom_tokens
        ):
            raise ValueError("Invalid measured headroom")
        return self


class ContextSelection(StrictObservation):
    unit: Literal["message", "exchange"]
    kept: Count
    dropped: Count
    examined: Count
    history_complete: bool
    measurement_attempts: int = Field(ge=1, le=513)

    @model_validator(mode="after")
    def exact_counts(self):
        if self.kept + self.dropped != self.examined:
            raise ValueError("Invalid context selection counts")
        return self


class ContextObservation(StrictObservation):
    schema_version: Literal["1.0"]
    id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    sequence: int = Field(ge=1, le=MAX_COUNT)
    role: Literal["agent", "converse"]
    phase: Literal["prepared", "generation_export_admitted"]
    observed_at: str = Field(min_length=20, max_length=40)
    scope: ContextScope
    model: ContextModel
    measurement: ContextMeasurement
    selection: ContextSelection

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value):
        if (
            not value.endswith("Z")
            or datetime.fromisoformat(value[:-1] + "+00:00").utcoffset().total_seconds() != 0
        ):
            raise ValueError("Observation time requires UTC")
        return value

    @model_validator(mode="after")
    def role_scope(self):
        if self.role == "agent":
            if self.scope.task_id is None or self.selection.unit != "exchange":
                raise ValueError("Invalid Agent observation scope")
        elif (
            self.scope.task_id is not None
            or self.selection.unit != "message"
            or self.phase != "prepared"
        ):
            raise ValueError("Invalid Converse observation scope")
        if (
            len(
                json.dumps(
                    self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            > 8000
        ):
            raise ValueError("Observation exceeds its metadata bound")
        return self


class ContextObservationRow(StrictObservation):
    observation: ContextObservation
    configuration_status: Literal["current", "obsolete", "unknown"]


class ContextUsageSnapshot(StrictObservation):
    schema_version: Literal["1.0"]
    role: Literal["agent", "converse"]
    instance_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    latest_sequence: Count
    retained_count: int = Field(ge=0, le=128)
    storage: Literal["process_memory"]
    measurement_mode: Literal["measured", "legacy_unmeasured", "unknown"]
    observations: tuple[ContextObservationRow, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def exact_page(self):
        sequences = [row.observation.sequence for row in self.observations]
        if (
            self.retained_count != min(self.latest_sequence, 128)
            or len(sequences) != min(self.retained_count, 32)
            or sequences
            != list(range(self.latest_sequence, self.latest_sequence - len(sequences), -1))
            or any(row.observation.role != self.role for row in self.observations)
        ):
            raise ValueError("Invalid observation snapshot")
        return self
