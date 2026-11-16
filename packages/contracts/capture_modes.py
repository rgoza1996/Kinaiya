"""Converse-owned capture selection; neither microphone nor execution authority."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.agent_control import agent_control_json

MAX_CAPTURE_MODE_CHANGES = 1000
MAX_CAPTURE_MODE_BYTES = 4096
MAX_CAPTURE_MODE_RESPONSE_BYTES = 16384
MAX_CAPTURE_MODE_INTEGER = 9_007_199_254_740_991
CAPTURE_MODES = ("adaptive_live", "rolling_windows", "legacy_silence")
CaptureMode = Literal["adaptive_live", "rolling_windows", "legacy_silence"]
Identity = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]
ChangeKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9:._-]+$", min_length=1, max_length=128)]
Generation = Annotated[int, Field(ge=0, le=MAX_CAPTURE_MODE_INTEGER)]
Revision = Annotated[int, Field(ge=0, le=MAX_CAPTURE_MODE_CHANGES)]


class _CaptureModeWire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CaptureModeSelection(_CaptureModeWire):
    source: Literal["host", "saved"]
    mode: CaptureMode | None = None

    @model_validator(mode="after")
    def source_matches(self) -> Self:
        if (self.source == "host") != (self.mode is None):
            raise ValueError("Invalid Converse capture mode selection")
        return self


class CaptureModeChange(_CaptureModeWire):
    contract: Literal["converse_capture.v1"] = "converse_capture.v1"
    backend_id: Identity
    authority_epoch: Identity
    authority_generation: Generation
    expected_revision: Revision
    state_token: Digest
    expected_default_mode: CaptureMode | None
    idempotency_key: ChangeKey
    selection: CaptureModeSelection


class CaptureModeSnapshot(_CaptureModeWire):
    contract: Literal["converse_capture.v1"] = "converse_capture.v1"
    backend_id: Identity
    authority_epoch: Identity
    authority_generation: Generation
    revision: Revision
    state_token: Digest
    selection: CaptureModeSelection
    default_mode: CaptureMode | None
    selected_mode: CaptureMode | None
    capture_state: Literal["not_observed"] = "not_observed"
    adaptive_live_enabled: bool = False
    change_boundary: Literal["next_capture"] = "next_capture"
    writable: bool
    max_changes: Literal[1000] = MAX_CAPTURE_MODE_CHANGES

    @model_validator(mode="after")
    def selected_source(self) -> Self:
        expected = self.default_mode if self.selection.source == "host" else self.selection.mode
        if self.selected_mode != expected:
            raise ValueError("Invalid Converse capture mode snapshot")
        return self


class CaptureModeReceipt(_CaptureModeWire):
    contract: Literal["converse_capture.v1"] = "converse_capture.v1"
    status: Literal["saved"] = "saved"
    backend_id: Identity
    authority_epoch: Identity
    authority_generation: Generation
    revision: Annotated[int, Field(ge=1, le=MAX_CAPTURE_MODE_CHANGES)]
    idempotency_key: ChangeKey
    selection: CaptureModeSelection
    default_mode_observed: CaptureMode | None
    selected_mode_observed: CaptureMode | None
    change_boundary: Literal["next_capture"] = "next_capture"

    @model_validator(mode="after")
    def selected_source(self) -> Self:
        expected = (
            self.default_mode_observed if self.selection.source == "host" else self.selection.mode
        )
        if self.selected_mode_observed != expected:
            raise ValueError("Invalid Converse capture mode receipt")
        return self


class CaptureModeLookup(_CaptureModeWire):
    contract: Literal["converse_capture.v1"] = "converse_capture.v1"
    backend_id: Identity
    authority_epoch: Identity
    authority_generation: Generation
    request_sha256: Digest
    outcome: Literal["saved", "unobserved"]
    receipt: CaptureModeReceipt | None
    mutation_fenced: bool

    @model_validator(mode="after")
    def original_receipt(self) -> Self:
        if (self.outcome == "saved") != (self.receipt is not None):
            raise ValueError("Invalid Converse capture mode lookup")
        if self.receipt is not None and (
            self.receipt.backend_id != self.backend_id or self.mutation_fenced
        ):
            raise ValueError("Invalid Converse capture mode lookup")
        return self


def capture_mode_json(raw: bytes, *, maximum=MAX_CAPTURE_MODE_BYTES):
    """Reuse the strict bounded integer-only JSON grammar; never reflect errors."""
    return agent_control_json(raw, maximum=maximum)
