"""Explicit five-outcome classification; neither labels nor bindings grant permission."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Conversation = Literal[
    "IGNORE",
    "WAIT_FOR_NEXT_WINDOW",
    "RESPOND_INTERRUPT_USER",
    "RESPOND_WITHOUT_INTERRUPTING_USER",
    "WAIT_WITH_BACKCHANNEL",
]
AgentRoute = Literal[
    "IGNORE_FOR_IMMEDIATE_ROUTING",
    "STEER_CURRENT_TASK",
    "INTERRUPT_CURRENT_TASK",
    "QUEUE_UNTIL_TASK_DONE",
]
MAX_INTEGER = (1 << 53) - 1


class LiveWindowLabels(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )
    schema_version: Literal["2.0"]
    conversation: Conversation
    agent_route: AgentRoute
    target_task_id: str | None = Field(max_length=128)
    expected_objective_revision: int | None = Field(ge=1, le=MAX_INTEGER)
    reason_code: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def exact_target(self):
        if not self.reason_code.strip() or "\x00" in self.reason_code:
            raise ValueError("A live decision requires a bounded reason label")
        self.reason_code.encode("utf-8")
        if self.agent_route in {"STEER_CURRENT_TASK", "INTERRUPT_CURRENT_TASK"} and (
            not self.target_task_id or self.expected_objective_revision is None
        ):
            raise ValueError("Targeted live routing requires its observed task and revision")
        if (self.target_task_id is None) != (self.expected_objective_revision is None):
            raise ValueError("A live task target and revision are one binding")
        if self.target_task_id is not None and not self.target_task_id:
            raise ValueError("A live task target cannot be empty")
        if self.agent_route == "IGNORE_FOR_IMMEDIATE_ROUTING" and self.target_task_id is not None:
            raise ValueError("Ignore cannot carry an Agent target")
        return self


class LiveWindowDecision(LiveWindowLabels):
    """Native bindings are added after validation, never accepted from a model."""

    decision_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=MAX_INTEGER)
    utterance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    window_index: int = Field(ge=0, le=1_000_000)
    source_event_id: str = Field(min_length=1, max_length=128)
    source_sequence: int = Field(ge=1, le=MAX_INTEGER)
    source_revision: int = Field(ge=1, le=MAX_INTEGER)
    prompt_chars: int = Field(ge=0, le=200_000)
    history_chars: int = Field(ge=0, le=48_000)
    context_entries: int = Field(ge=0, le=12)
    classifier_ms: float = Field(ge=0, allow_inf_nan=False)
