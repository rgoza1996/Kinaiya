"""Core-owned next-launch selection; neither readiness nor execution authority."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.agent_control import agent_control_json

MAX_CORE_SUBSYSTEM_CHANGES = 1000
MAX_CORE_SUBSYSTEM_BYTES = 4096
MAX_CORE_SUBSYSTEM_RESPONSE_BYTES = 16384
MAX_CORE_SUBSYSTEM_INTEGER = 9_007_199_254_740_991
CoreSubsystemMode = Literal[
    "core", "agent", "decisions", "agent-decisions", "converse-decisions", "all"
]
Identity = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]
ChangeKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9:._-]+$", min_length=1, max_length=128)]
Generation = Annotated[int, Field(ge=0, le=MAX_CORE_SUBSYSTEM_INTEGER)]
Revision = Annotated[int, Field(ge=0, le=MAX_CORE_SUBSYSTEM_CHANGES)]


class _CoreSubsystemWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CoreSubsystemSelection(_CoreSubsystemWire):
    source: Literal["host", "saved"]
    mode: CoreSubsystemMode | None = None

    @model_validator(mode="after")
    def source_matches(self) -> Self:
        if (self.source == "host") != (self.mode is None):
            raise ValueError("Invalid Core subsystem selection")
        return self


class CoreSubsystemChange(_CoreSubsystemWire):
    contract: Literal["core_subsystems.v1"] = "core_subsystems.v1"
    backend_id: Identity
    authority_epoch: Identity
    authority_generation: Generation
    expected_revision: Revision
    state_token: Digest
    expected_host_mode: CoreSubsystemMode | None
    idempotency_key: ChangeKey
    selection: CoreSubsystemSelection


class CoreSubsystemSnapshot(_CoreSubsystemWire):
    contract: Literal["core_subsystems.v1"] = "core_subsystems.v1"
    backend_id: Identity
    authority_epoch: Identity
    authority_generation: Generation
    revision: Revision
    state_token: Digest
    selection: CoreSubsystemSelection
    host_mode: CoreSubsystemMode | None
    selected_mode: CoreSubsystemMode | None
    startup_mode: CoreSubsystemMode | None
    running_state: Literal["not_observed"] = "not_observed"
    change_boundary: Literal["next_managed_host_launch"] = "next_managed_host_launch"
    writable: bool
    max_changes: Literal[1000] = MAX_CORE_SUBSYSTEM_CHANGES

    @model_validator(mode="after")
    def selected_source(self) -> Self:
        expected = self.host_mode if self.selection.source == "host" else self.selection.mode
        if self.selected_mode != expected:
            raise ValueError("Invalid Core subsystem snapshot")
        return self


class CoreSubsystemReceipt(_CoreSubsystemWire):
    contract: Literal["core_subsystems.v1"] = "core_subsystems.v1"
    status: Literal["saved"] = "saved"
    backend_id: Identity
    authority_epoch: Identity
    authority_generation: Generation
    revision: Annotated[int, Field(ge=1, le=MAX_CORE_SUBSYSTEM_CHANGES)]
    idempotency_key: ChangeKey
    selection: CoreSubsystemSelection
    host_mode_observed: CoreSubsystemMode | None
    selected_mode_observed: CoreSubsystemMode | None
    change_boundary: Literal["next_managed_host_launch"] = "next_managed_host_launch"

    @model_validator(mode="after")
    def selected_source(self) -> Self:
        expected = (
            self.host_mode_observed if self.selection.source == "host" else self.selection.mode
        )
        if self.selected_mode_observed != expected:
            raise ValueError("Invalid Core subsystem receipt")
        return self


class CoreSubsystemLookup(_CoreSubsystemWire):
    contract: Literal["core_subsystems.v1"] = "core_subsystems.v1"
    backend_id: Identity
    authority_epoch: Identity
    authority_generation: Generation
    request_sha256: Digest
    outcome: Literal["saved", "unobserved"]
    receipt: CoreSubsystemReceipt | None
    mutation_fenced: bool

    @model_validator(mode="after")
    def original_receipt(self) -> Self:
        if (self.outcome == "saved") != (self.receipt is not None):
            raise ValueError("Invalid Core subsystem lookup")
        if self.receipt is not None and (
            self.receipt.backend_id != self.backend_id or self.mutation_fenced
        ):
            raise ValueError("Invalid Core subsystem lookup")
        return self


def core_subsystem_json(raw: bytes, *, maximum=MAX_CORE_SUBSYSTEM_BYTES):
    """Reuse the strict bounded integer-only JSON grammar; never reflect errors."""
    return agent_control_json(raw, maximum=maximum)
