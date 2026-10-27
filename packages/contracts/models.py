"""Shared wire models; service authentication supplies provenance, never request JSON."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"


def new_id() -> str:
    return str(uuid4())


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = SCHEMA_VERSION


class SessionRequest(WireModel):
    channel: Literal["converse", "agent"] = "converse"
    name: str = Field(default="", max_length=200)


class Session(WireModel):
    session_id: str = Field(default_factory=new_id)
    thread_id: str = Field(default_factory=new_id)
    channel: Literal["converse", "agent"]
    epoch: int = Field(default=1, ge=1)
    name: str = ""


class AppendRequest(WireModel):
    session_id: str
    thread_id: str
    epoch: int = Field(ge=1)
    kind: str = Field(max_length=80)
    payload: dict[str, Any]
    causes: list[str] = Field(default_factory=list, max_length=32)
    task_refs: list[str] = Field(default_factory=list, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=256)


class JournalEntry(AppendRequest):
    event_id: str = Field(default_factory=new_id)
    sequence: int = Field(ge=1)
    origin: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskState(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    PAUSED = "paused"


TERMINAL_STATES = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}


class TaskRequest(WireModel):
    session_id: str
    thread_id: str
    epoch: int = Field(ge=1)
    objective: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_event_ids: list[str] = Field(default_factory=list, max_length=32)
    source_revision: int = Field(default=1, ge=1)


class Evidence(WireModel):
    evidence_id: str = Field(default_factory=new_id)
    kind: Literal["file", "command", "read", "search", "test", "extension"]
    summary: str
    path: str | None = None
    sha256: str | None = None
    exit_code: int | None = None
    output: str = ""
    objective_revision: int = Field(default=1, ge=1)


class Task(WireModel):
    task_id: str = Field(default_factory=new_id)
    session_id: str
    thread_id: str
    epoch: int = Field(ge=1)
    objective: str
    objective_revision: int = Field(default=1, ge=1)
    state: TaskState = TaskState.ACCEPTED
    summary: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    remaining_required_work: list[str] = Field(default_factory=list)
    source_event_ids: list[str] = Field(default_factory=list)
    idempotency_key: str
    harness_version: str = "seed-v1"
    error: str | None = None


class SteerRequest(WireModel):
    instruction: str = Field(min_length=1, max_length=16000)
    expected_objective_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_event_ids: list[str] = Field(default_factory=list, max_length=32)


class InterruptRequest(WireModel):
    expected_objective_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="User requested cancellation", max_length=2000)
    source_event_ids: list[str] = Field(default_factory=list, max_length=32)
    source_revision: int = Field(default=1, ge=1)


class Decision(WireModel):
    decision_id: str = Field(default_factory=new_id)
    source_event_ids: list[str] = Field(min_length=1, max_length=32)
    source_revision: int = Field(default=1, ge=1)
    route: Literal["ignore", "queue", "steer", "interrupt"]
    target_task_id: str | None = None
    expected_objective_revision: int | None = Field(default=None, ge=1)
    instruction: str = Field(default="", max_length=16000)
    reason_code: str = Field(default="classified", max_length=120)
    confidence: float | None = Field(default=None, ge=0, le=1)
    idempotency_key: str

    @model_validator(mode="after")
    def targeted_routes(self):
        if self.route in {"steer", "interrupt"} and (
            not self.target_task_id or not self.expected_objective_revision
        ):
            raise ValueError("steer/interrupt require an explicit task and objective revision")
        if self.route in {"queue", "steer"} and not self.instruction.strip():
            raise ValueError("queue/steer require an instruction")
        return self


class ConverseRequest(WireModel):
    session_id: str
    thread_id: str
    epoch: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=256)
    input_mode: Literal["text", "voice"] = "text"


class DeliveryRequest(WireModel):
    event_id: str
    generation_epoch: int = Field(ge=1)
    chunk_id: str
    status: Literal["generated", "playing", "played", "interrupted", "delivery_unknown"]
    played_seconds: float = Field(default=0, ge=0)
    estimated_text_fraction: float | None = Field(default=None, ge=0, le=1)


class ApprovalResolution(WireModel):
    approved: bool
    action_digest: str
    objective_revision: int = Field(ge=1)


class CommandRequest(WireModel):
    session_id: str
    thread_id: str
    epoch: int = Field(ge=1)
    input: str = Field(min_length=1, max_length=16000)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ToolAction(WireModel):
    tool: Literal["read_file", "write_file", "search", "shell", "read_transcript", "finish"]
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProviderInfo(WireModel):
    provider: str
    model: str
    local: bool
    streaming: bool = False
