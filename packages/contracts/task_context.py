"""Bounded native task observations for Converse; never execution authority."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import JournalEntry

MAX_TASK_CONTEXT_REQUEST_BYTES = 8192
MAX_TASK_CONTEXT_RESPONSE_BYTES = 65536
MAX_TASK_CONTEXT_TASKS = 16
MAX_TASK_CONTEXT_COUNTER = 9_007_199_254_740_991
EVIDENCE_KINDS = frozenset(
    {"file", "command", "read", "search", "test", "extension", "external", "artifact", "inspection"}
)
Identifier = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_TASK_CONTEXT_COUNTER)]
Positive = Annotated[int, Field(strict=True, ge=1, le=MAX_TASK_CONTEXT_COUNTER)]
Scope = Literal["local_only", "public_synthetic", "private_cloud_authorized"]
ObservedState = Literal[
    "accepted",
    "queued",
    "running",
    "verifying",
    "awaiting_approval",
    "blocked",
    "succeeded",
    "failed",
    "cancel_requested",
    "cancelled",
    "paused",
    "unknown",
]


class TaskContextUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Native task context is unavailable")


class TaskContextConflict(ValueError):
    def __init__(self):
        super().__init__("Native task context source is no longer current")


class _Context(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


class TaskContextSource(_Context):
    source_event_id: Identifier
    source_sequence: Positive
    source_revision: Positive
    session_id: Identifier
    thread_id: Identifier
    epoch: Positive

    @classmethod
    def of(cls, source: JournalEntry):
        return cls(
            source_event_id=source.event_id,
            source_sequence=source.sequence,
            source_revision=source.payload.get("revision", 1),
            session_id=source.session_id,
            thread_id=source.thread_id,
            epoch=source.epoch,
        )


class TaskContextRequest(_Context):
    source: TaskContextSource
    task_ids: list[Identifier] | None = Field(default=None, min_length=1, max_length=16)

    @field_validator("task_ids")
    @classmethod
    def distinct_ids(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Duplicate task context identity")
        return value


class TaskContextObservation(_Context):
    task_id: Identifier
    session_id: Identifier
    thread_id: Identifier
    epoch: Positive
    objective_revision: Positive
    state: ObservedState
    submission_confirmed: bool
    completion_publication_pending: bool
    content_scope: Scope
    evidence_counts: dict[str, Counter] = Field(min_length=9, max_length=9)
    required_work_count: Counter
    program_execution_receipt: Literal["observed", "not_observed", "unavailable"]
    semantic_verification: Literal["not_assessed"]
    observation_basis: Literal["native_runtime_snapshot"]

    @field_validator("evidence_counts")
    @classmethod
    def exact_kinds(cls, value):
        if set(value) != EVIDENCE_KINDS:
            raise ValueError("Unknown evidence context family")
        return value

    @model_validator(mode="after")
    def unconfirmed_is_unknown(self):
        if not self.submission_confirmed and self.state != "unknown":
            raise ValueError("Unconfirmed submission cannot establish a task state")
        return self


class TaskContextResponse(_Context):
    contract: Literal["converse_task_context.v1"]
    source: TaskContextSource
    instance_id: Identifier
    observed_at: str = Field(min_length=20, max_length=40)
    tasks: list[TaskContextObservation] = Field(max_length=MAX_TASK_CONTEXT_TASKS)
    truncated: bool
    read_only: Literal[True]

    @field_validator("read_only", mode="before")
    @classmethod
    def literal_read_only(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Task context is read-only")
        return value

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("Task observations require a UTC time")
        return value

    @model_validator(mode="after")
    def scoped_tasks(self):
        scope = self.source.session_id, self.source.thread_id, self.source.epoch
        if len({task.task_id for task in self.tasks}) != len(self.tasks) or any(
            (task.session_id, task.thread_id, task.epoch) != scope for task in self.tasks
        ):
            raise ValueError("Task context crosses its source scope")
        return self

    def validate_request(self, request):
        if self.source != request.source or (
            request.task_ids is not None
            and any(task.task_id not in request.task_ids for task in self.tasks)
        ):
            raise TaskContextUnavailable()
        return self
