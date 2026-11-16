"""Native capture permission only; never physical stop or provider authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.live_windows import LiveWindowPublicationRef
from packages.contracts.stt_settlement import encode, settlement_json

PROTOCOL = "native_live_capture_v2"
MAX_REQUEST_BYTES = 4096
MAX_RESULT_BYTES = 16384
MAX_INTEGER = (1 << 53) - 1
MAX_ACTIVE = 64
MAX_RECORDS = 10000


def capture_json(raw, maximum=MAX_REQUEST_BYTES):
    return settlement_json(raw, maximum)


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def bounded(self):
        if len(encode(self.model_dump(mode="json")).encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("Native capture metadata exceeds its bound")
        return self


class LiveCaptureBackend(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_capture_v2"]
    backend_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    controller_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)


class LiveCaptureRegistration(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_capture_v2"]
    backend: LiveCaptureBackend
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    capture_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=MAX_INTEGER)


class LiveCaptureReceipt(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_capture_v2"]
    original: LiveCaptureRegistration
    registration_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    registered_sequence: int = Field(ge=0, le=MAX_INTEGER)


class LiveCaptureFence(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_capture_v2"]
    original: LiveCaptureRegistration
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    reason: Literal["owner_off", "cancelled", "disconnected", "replaced", "shutdown"]


class LiveCaptureFenceReceipt(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_capture_v2"]
    request: LiveCaptureFence
    fence_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)


class LiveCaptureState(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_capture_v2"]
    original: LiveCaptureRegistration
    backend: LiveCaptureBackend
    receipt: LiveCaptureReceipt | None
    fence: LiveCaptureFenceReceipt | None
    scope_current: bool
    capture_open: bool

    @model_validator(mode="after")
    def exact_original(self):
        if self.receipt is not None and self.receipt.original != self.original:
            raise ValueError("Native capture receipt changed its original")
        if self.fence is not None and self.fence.request.original != self.original:
            raise ValueError("Native capture fence changed its original")
        expected = (
            self.receipt is not None
            and self.fence is None
            and self.scope_current
            and self.original.backend == self.backend
        )
        if self.capture_open != expected:
            raise ValueError("Native capture permission contradicts its original state")
        return self


class LiveCaptureSourceState(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_capture_v2"]
    original: LiveWindowPublicationRef
    source_sequence: int | None = Field(ge=1, le=MAX_INTEGER)
    capture: LiveCaptureState | None
    capture_permits_source: bool

    @model_validator(mode="after")
    def exact_source(self):
        current = self.capture
        if current is not None:
            original, bound = self.original, current.original
            if (
                bound.owner_instance,
                bound.capture_id,
                bound.session_id,
                bound.thread_id,
                bound.epoch,
            ) != (
                original.owner_instance,
                original.capture_id,
                original.session_id,
                original.thread_id,
                original.epoch,
            ):
                raise ValueError("Native capture lookup changed its source scope")
        expected = bool(
            current is not None
            and current.capture_open
            and current.receipt is not None
            and self.source_sequence is not None
            and self.source_sequence > current.receipt.registered_sequence
        )
        if self.capture_permits_source != expected:
            raise ValueError("Native source permission contradicts its registration boundary")
        return self
