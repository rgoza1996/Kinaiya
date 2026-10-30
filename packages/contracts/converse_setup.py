"""Versioned, read-only model declarations for owner setup (#138).

Configuration is not a model-load or readiness receipt. This contract deliberately
contains no provider endpoint, local artifact path, credential, or inference grant.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.models import MAX_PLAYBACK_GENERATION

SetupText = Annotated[str, Field(min_length=1, max_length=256)]
SetupCount = Annotated[int, Field(ge=0, le=MAX_PLAYBACK_GENERATION)]


class SetupWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SetupOption(SetupWire):
    id: SetupText
    label: SetupText


class SetupRole(SetupWire):
    selected_id: SetupText | None
    label: SetupText | None
    configured: bool
    source: Literal["runtime", "host_configuration"]
    options: list[SetupOption] = Field(max_length=1)
    mutable: Literal[False] = False
    reason: SetupText

    @model_validator(mode="after")
    def declared_selection(self):
        if self.configured:
            if (
                self.selected_id is None
                or self.label is None
                or len(self.options) != 1
                or self.options[0].id != self.selected_id
                or self.options[0].label != self.label
            ):
                raise ValueError("Configured role requires its exact declared option")
        elif self.selected_id is not None or self.label is not None or self.options:
            raise ValueError("Undeclared role cannot advertise a selection")
        return self


class SetupRoles(SetupWire):
    conversation: SetupRole
    stt: SetupRole
    tts: SetupRole
    decisions: SetupRole


class SetupInputs(SetupWire):
    supported: list[Literal["text"]] = Field(min_length=1, max_length=1)
    selected: list[Literal["text"]] = Field(min_length=1, max_length=1)
    direct_audio: Literal[False] = False
    stt_required: Literal[True] = True


class SetupReasoning(SetupWire):
    selected: SetupText
    options: list[SetupText] = Field(min_length=1, max_length=1)
    mutable: Literal[False] = False
    reason: SetupText

    @model_validator(mode="after")
    def exact_selection(self):
        if self.options != [self.selected]:
            raise ValueError("Reasoning advertises only the declared host setting")
        return self


class SetupTextOperations(SetupWire):
    active: SetupCount
    cleanup_pending: SetupCount
    cleanup_failed: SetupCount
    admitting: SetupCount
    capacity: SetupCount


class SetupSpeechOperations(SetupWire):
    active: SetupCount
    cancellation_pending: SetupCount
    cleanup_failed: SetupCount


class SetupOperationState(SetupWire):
    text: SetupTextOperations
    speech: SetupSpeechOperations


class ConverseSetup(SetupWire):
    contract_version: Literal[1] = 1
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    roles: SetupRoles
    inputs: SetupInputs
    reasoning: SetupReasoning
    operation_state: SetupOperationState
