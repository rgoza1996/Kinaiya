"""Shared wire models; service authentication supplies provenance, never request JSON."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

SCHEMA_VERSION = "1.0"
ContentScope = Literal["local_only", "public_synthetic", "private_cloud_authorized"]
AuxiliaryTask = Literal[
    "analysis", "compression", "title_generation", "delegation", "harness_design"
]
# Flutter web/JSON numbers represent every integer only through this value.
MAX_PLAYBACK_GENERATION = 9_007_199_254_740_991


def new_id() -> str:
    return str(uuid4())


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = SCHEMA_VERSION


class SessionRequest(WireModel):
    channel: Literal["converse", "agent"] = "converse"
    name: str = Field(default="", max_length=200)
    harness_version: str | None = Field(default=None, pattern=r"^(seed-v1|v-[a-f0-9]{24})$")

    @model_validator(mode="after")
    def agent_harness_only(self):
        if self.harness_version is not None and self.channel != "agent":
            raise ValueError("Only Agent sessions may pin a harness")
        return self


class Session(WireModel):
    session_id: str = Field(default_factory=new_id)
    thread_id: str = Field(default_factory=new_id)
    channel: Literal["converse", "agent"]
    epoch: int = Field(default=1, ge=1)
    name: str = ""
    harness_version: str | None = Field(default=None, pattern=r"^(seed-v1|v-[a-f0-9]{24})$")

    @model_validator(mode="after")
    def agent_harness_only(self):
        if self.harness_version is not None and self.channel != "agent":
            raise ValueError("Only Agent sessions may pin a harness")
        return self


class PlaybackGeneration(WireModel):
    session_id: str = Field(strict=True, min_length=1, max_length=128)
    thread_id: str = Field(strict=True, min_length=1, max_length=128)
    epoch: int = Field(strict=True, ge=1)
    maximum_generation: int = Field(strict=True, ge=0, le=MAX_PLAYBACK_GENERATION)


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
    content_scope: ContentScope = "local_only"
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_event_ids: list[str] = Field(default_factory=list, max_length=32)
    source_revision: int = Field(default=1, ge=1)


class Evidence(WireModel):
    evidence_id: str = Field(default_factory=new_id)
    # "external" is a broker receipt for untrusted MCP/skill content.
    # It binds the observed payload, never proves task effects, and alone
    # cannot support completion.
    # A native artifact receipt proves only persisted UI publication. It is
    # never evidence for a claimed filesystem or external side effect.
    # Inspection receipts bind metadata observations only. They never count
    # as work evidence in completion or Converse announcements.
    kind: Literal[
        "file",
        "command",
        "read",
        "search",
        "test",
        "extension",
        "external",
        "artifact",
        "inspection",
    ]
    summary: str
    path: str | None = None
    sha256: str | None = None
    exit_code: int | None = None
    output: str = ""
    objective_revision: int = Field(default=1, ge=1)


class CatalogModelBinding(BaseModel):
    """Explicit immutable native provider reference, never a display alias."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    generation: int = Field(ge=1, le=64)
    configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentModelPin(WireModel):
    """Native model selection for one accepted task; never request authority."""

    profile_id: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    local: bool
    reasoning: str | None = Field(default=None, max_length=16)
    settings_revision: int = Field(ge=0)
    catalog_binding: CatalogModelBinding | None = None
    catalog_revision: int | None = Field(default=None, ge=0, le=9_007_199_254_740_990)

    @model_serializer(mode="wrap")
    def preserve_historical_catalog_shape(self, handler):
        result = handler(self)
        # Legacy accepted pins omitted these additive fields. Preserve that
        # representation, including inside Task, rather than rewriting history
        # with defaults. Explicit nulls and exact catalog references stay exact.
        for name in ("catalog_binding", "catalog_revision"):
            if name not in self.model_fields_set:
                result.pop(name, None)
        return result

    @model_validator(mode="after")
    def exact_catalog_pin(self):
        if (self.catalog_binding is None) != (self.catalog_revision is None):
            raise ValueError("Catalog pin requires an exact reference and capture revision")
        if self.catalog_binding is not None and self.profile_id != self.catalog_binding.id:
            raise ValueError("Catalog pin profile identity does not match its reference")
        return self


class Task(WireModel):
    task_id: str = Field(default_factory=new_id)
    session_id: str
    thread_id: str
    epoch: int = Field(ge=1)
    objective: str
    content_scope: ContentScope = "local_only"
    objective_revision: int = Field(default=1, ge=1)
    state: TaskState = TaskState.ACCEPTED
    summary: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    remaining_required_work: list[str] = Field(default_factory=list)
    source_event_ids: list[str] = Field(default_factory=list)
    idempotency_key: str
    harness_version: str = "seed-v1"
    model_pin: AgentModelPin | None = None
    auxiliary_models: dict[AuxiliaryTask, AgentModelPin] = Field(default_factory=dict, max_length=5)
    error: str | None = None


class SteerRequest(WireModel):
    instruction: str = Field(min_length=1, max_length=16000)
    content_scope: ContentScope = "local_only"
    expected_objective_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    source_event_ids: list[str] = Field(default_factory=list, max_length=32)


class InterruptRequest(WireModel):
    expected_objective_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="User requested cancellation", max_length=2000)
    source_event_ids: list[str] = Field(default_factory=list, max_length=32)
    source_revision: int = Field(default=1, ge=1)


class TaskRetirement(WireModel):
    """Native administrative receipt; never a new-epoch execution outcome."""

    task_id: str = Field(min_length=1, max_length=128)
    task_session_id: str = Field(min_length=1, max_length=128)
    task_thread_id: str = Field(min_length=1, max_length=128)
    task_epoch: int = Field(ge=1, strict=True)
    objective_revision: int = Field(ge=1, strict=True)
    request_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["cancelled"] = "cancelled"
    effect_status: Literal["unreconciled"] = "unreconciled"


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
    content_scope: ContentScope = "local_only"
    idempotency_key: str = Field(min_length=1, max_length=256)
    input_mode: Literal["text", "voice"] = "text"


class SpeechWindow(WireModel):
    """Authenticated STT hypothesis; persistence does not depend on classification."""

    session_id: str
    thread_id: str
    epoch: int = Field(ge=1, strict=True)
    utterance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    window_index: int = Field(ge=0, le=1_000_000, strict=True)
    revision: int = Field(default=1, ge=1, strict=True)
    text: str = Field(default="", max_length=16000)
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(ge=0, strict=True)
    user_speaking: bool = Field(strict=True)
    capture_ended: bool = Field(default=False, strict=True)
    audio_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_scope: Literal["local_only", "public_synthetic", "private_cloud_authorized"] = (
        "local_only"
    )

    @model_validator(mode="after")
    def window_bounds(self):
        if not 0 <= self.end_ms - self.start_ms <= 60000:
            raise ValueError("A transcript window must cover at most 60 seconds")
        if self.capture_ended and self.user_speaking:
            raise ValueError("Ended capture cannot still report speaking")
        return self


class WindowDecision(WireModel):
    """Separate conversation timing and work routing, bound to one native window."""

    decision_id: str = Field(default_factory=new_id)
    prompt_chars: int = Field(default=0, ge=0, le=200000, strict=True)
    history_chars: int = Field(default=0, ge=0, le=48000, strict=True)
    context_entries: int = Field(default=0, ge=0, le=12, strict=True)
    classification_reused: bool = Field(default=False, strict=True)
    source_event_id: str = Field(min_length=1, max_length=128)
    source_sequence: int = Field(ge=1, strict=True)
    source_revision: int = Field(ge=1, strict=True)
    conversation: Literal[
        "IGNORE",
        "WAIT_FOR_NEXT_WINDOW",
        "RESPOND_INTERRUPT_USER",
        "RESPOND_WITHOUT_INTERRUPTING_USER",
    ]
    agent_route: Literal[
        "IGNORE_FOR_IMMEDIATE_ROUTING",
        "STEER_CURRENT_TASK",
        "INTERRUPT_CURRENT_TASK",
        "QUEUE_UNTIL_TASK_DONE",
    ]
    target_task_id: str | None = Field(default=None, max_length=128)
    expected_objective_revision: int | None = Field(default=None, ge=1, strict=True)
    reason_code: str = Field(default="classified", max_length=120)
    classifier_ms: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def targets(self):
        if self.agent_route in {"STEER_CURRENT_TASK", "INTERRUPT_CURRENT_TASK"} and (
            not self.target_task_id or self.expected_objective_revision is None
        ):
            raise ValueError("Targeted routing needs a task and revision")
        if self.target_task_id and self.expected_objective_revision is None:
            raise ValueError("A queue boundary must bind the observed task revision")
        if self.expected_objective_revision is not None and not self.target_task_id:
            raise ValueError("Revision needs a task")
        if self.agent_route == "IGNORE_FOR_IMMEDIATE_ROUTING" and self.target_task_id:
            raise ValueError("Ignore cannot carry a task target")
        return self


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


class PendingApproval(WireModel):
    approval_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    session_id: str
    thread_id: str
    epoch: int = Field(ge=1)
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    action_digest: str
    objective_revision: int = Field(ge=1)
    expires_at: float = Field(gt=0)
    reason: Literal["tool_permission", "uncommitted_speech"] = "tool_permission"
    origin: Literal["agent-supervisor"] = "agent-supervisor"


class PendingApprovalPage(WireModel):
    approvals: list[PendingApproval] = Field(default_factory=list, max_length=100)
    next_after: str | None = Field(default=None, max_length=128)


class CommandRequest(WireModel):
    session_id: str
    thread_id: str
    epoch: int = Field(ge=1)
    input: str = Field(min_length=1, max_length=16000)
    content_scope: ContentScope = "local_only"
    idempotency_key: str = Field(min_length=1, max_length=256)


class ToolAction(WireModel):
    tool: Literal[
        "read_file",
        "write_file",
        "search",
        "shell",
        "read_transcript",
        "select_context",
        "mcp_list",
        "mcp_call",
        "skills_list",
        "skill_load",
        "plugins_list",
        "plugin_call",
        "publish_ui",
        "consult_model",
        "tool_catalog",
        "finish",
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)


class ProviderInfo(WireModel):
    provider: str
    model: str
    local: bool
    streaming: bool = False


class PluginControl(WireModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    operation: Literal[
        "register",
        "inspect",
        "evaluate",
        "promote",
        "rollback",
        "disable",
        "remove",
        "scope",
        "run",
    ]
    path: str | None = Field(default=None, min_length=1, max_length=1024)
    manifest: dict | None = None
    plugin_id: str | None = Field(default=None, min_length=1, max_length=64)
    version_id: str | None = Field(default=None, min_length=1, max_length=128)
    report_id: str | None = Field(default=None, min_length=1, max_length=128)
    reviewed: bool = False
    content_scope: Literal["local_only", "public_synthetic", "private_cloud_authorized"] | None = (
        None
    )
    arguments: dict | None = None


class MCPControl(WireModel):
    operation: Literal["configure", "enable", "disable", "remove", "test", "reconnect", "reconcile"]
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=0, le=2147483647, strict=True)
    server_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    config: dict | None = None
    reviewed: bool = Field(default=False, strict=True)
