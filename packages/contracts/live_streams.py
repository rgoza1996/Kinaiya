"""Explicit live microphone/decoder originals; native v1 STT stays unchanged."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.live_captures import LiveCaptureState
from packages.contracts.live_windows import LiveWindowPublicationProof
from packages.contracts.models import ContentScope
from packages.contracts.whistle_stream_owner import ConverseStreamProof

PROTOCOL = "converse_live_stream_v2"


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class LiveStreamCapability(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["converse_live_stream_v2"]
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$")
    available: bool


class LiveUtteranceCapability(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["native_live_utterance_v1"]
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$")
    available: bool


class LiveStreamIdentity(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["converse_live_stream_v2"]
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$")
    capture_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class LiveStreamScope(LiveStreamIdentity):
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=(1 << 53) - 1)
    utterance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    content_scope: ContentScope

    def native_scope(self):
        return self.model_dump(
            include={"session_id", "thread_id", "epoch", "utterance_id", "content_scope"}
        )


class LiveStreamBegin(LiveStreamScope):
    segment_index: int = Field(ge=0, le=1000000)


class LiveStreamNextUtterance(LiveStreamBegin):
    boundary_protocol: Literal["native_live_utterance_v1"]
    segment_index: int = Field(ge=1, le=1000000)
    previous_utterance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    previous_finish_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def distinct_domain(self):
        # Monotonic native segments make these names unique without keeping an
        # unbounded set of every utterance encountered during a long Call.
        if (
            self.utterance_id != f"{self.capture_id}_{self.segment_index}"
            or self.utterance_id == self.previous_utterance_id
        ):
            raise ValueError("An explicit utterance boundary requires its new native domain")
        return self


class LiveStreamPcm(LiveStreamBegin):
    stream_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    worker_incarnation: str = Field(pattern=r"^[a-f0-9]{64}$")
    sequence: int = Field(ge=1, le=100000)
    sample_offset: int = Field(ge=0, le=960000)
    user_speaking: bool
    pcm16_base64: str = Field(min_length=1, max_length=64000)


class LiveStreamFinish(LiveStreamBegin):
    stream_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    worker_incarnation: str = Field(pattern=r"^[a-f0-9]{64}$")
    sequence: int = Field(ge=1, le=100000)
    sample_offset: int = Field(ge=0, le=960000)
    user_speaking: bool
    capture_ended: bool

    @model_validator(mode="after")
    def ended(self):
        if self.capture_ended and self.user_speaking:
            raise ValueError("Ended capture cannot report speaking")
        return self


class LiveStreamCancel(LiveStreamIdentity):
    reason: Literal["owner_off", "cancelled", "disconnected", "replaced", "shutdown"]


class LiveStreamLookup(LiveStreamIdentity):
    phase: Literal["begin", "pcm", "finish"]
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class LiveStreamProof(LiveStreamIdentity):
    phase: Literal["begin", "pcm", "finish"]
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    segment_index: int | None = Field(ge=0, le=1000000)
    segment_sample_offset: int | None = Field(ge=0, le=(1 << 53) - 1)
    capture: LiveCaptureState | None
    native: ConverseStreamProof
    publication: LiveWindowPublicationProof | None

    @model_validator(mode="after")
    def exact_original(self):
        if (self.native.phase, self.native.request_sha256) != (self.phase, self.request_sha256):
            raise ValueError("Live stream proof changed its original native operation")
        if self.native.outcome == "confirmed" and (
            self.capture is None or self.capture.receipt is None
        ):
            raise ValueError("Live stream confirmation requires its original registration")
        if self.capture is not None and (
            self.capture.original.owner_instance,
            self.capture.original.capture_id,
        ) != (self.owner_instance, self.capture_id):
            raise ValueError("Live stream proof changed its capture owner")
        if self.native.outcome == "confirmed" and self.phase != "begin":
            if self.publication is None or self.native.result.get(
                "entry"
            ) != self.publication.source.model_dump(mode="json"):
                raise ValueError("Live stream confirmation requires the exact paired source")
        if self.publication is not None and (
            self.publication.original.owner_instance,
            self.publication.original.capture_id,
        ) != (self.owner_instance, self.capture_id):
            raise ValueError("Live stream publication changed its capture owner")
        if self.publication is not None:
            if self.phase == "begin" or self.capture is None:
                raise ValueError("Live publication requires its recognized original capture")
            bound, source = self.capture.original, self.publication.original
            if (bound.session_id, bound.thread_id, bound.epoch) != (
                source.session_id,
                source.thread_id,
                source.epoch,
            ):
                raise ValueError("Live publication changed its original registration scope")
        if self.native.outcome == "confirmed" and self.phase == "begin":
            scope = self.capture.original
            if any(
                self.native.result.get(k) != getattr(scope, k)
                for k in ("session_id", "thread_id", "epoch")
            ):
                raise ValueError("Live decoder startup changed its original registration scope")
        return self


class LiveStreamCancellation(LiveStreamIdentity):
    fenced: Literal[True]
    capture: LiveCaptureState | None
    cleanup_confirmed: bool

    @field_validator("fenced", mode="before")
    @classmethod
    def native_fence_flag(cls, value):
        if value is not True:
            raise ValueError("A live cancellation requires its native boolean fence flag")
        return value

    @model_validator(mode="after")
    def exact_capture(self):
        if self.capture is not None and (
            self.capture.original.owner_instance,
            self.capture.original.capture_id,
        ) != (self.owner_instance, self.capture_id):
            raise ValueError("Live cancellation changed its capture owner")
        if self.capture is not None and self.capture.capture_open:
            raise ValueError("Live cancellation cannot claim open capture permission")
        return self
