"""Exact stream operations and positive journal proofs, separate from unary STT."""

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import JournalEntry
from packages.contracts.stt_settlement import digest, encode

PROTOCOL = "whistle_stream_owner_v1"
PUBLICATION_PROTOCOL = "whistle_stream_publication_v1"
Phase = Literal["begin", "pcm", "finish", "cancel"]
ORIGINAL_HEADER = "x-kinaiya-stream-original"
PCM_HEADERS = (
    "x-kinaiya-stream-id",
    "x-kinaiya-worker-incarnation",
    "x-kinaiya-session-id",
    "x-kinaiya-thread-id",
    "x-kinaiya-epoch",
    "x-kinaiya-utterance-id",
    "x-kinaiya-content-scope",
    "x-kinaiya-sequence",
    "x-kinaiya-sample-offset",
    "x-kinaiya-pcm-sha256",
)


def pcm_request_digest(headers, pcm16):
    return digest(
        {
            "headers": {name: headers.get(name) for name in PCM_HEADERS},
            "pcm_sha256": hashlib.sha256(pcm16).hexdigest(),
        }
    )


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class WhistleStreamCapability(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["whistle_stream_owner_v1"]
    broker_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    active_limit: Literal[64]
    history_limit: Literal[1024]
    reservation_seconds: Literal[30]

    @field_validator("active_limit", "history_limit", "reservation_seconds", mode="before")
    @classmethod
    def integer_limits(cls, value):
        if type(value) is not int:
            raise ValueError("Stream limits require native integers")
        return value


class ConverseStreamLookup(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["converse_stream_owner_v1"]
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)


class ConverseStreamCapability(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["converse_stream_owner_v1"]
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)


class WhistleStreamReservation(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["whistle_stream_owner_v1"]
    broker_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    phase: Phase
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)


class WhistleStreamOriginal(WhistleStreamReservation):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)


class WhistleStreamAcknowledgment(WhistleStreamOriginal):
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)


class ConverseStreamProof(ConverseStreamCapability):
    phase: Literal["begin", "pcm", "finish"]
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)
    outcome: Literal["confirmed", "publication_pending", "in_progress", "unknown", "not_dispatched"]
    result: dict[str, Any] | None
    native_original: WhistleStreamOriginal | None
    decoder_finished: bool
    process_released: bool
    never_dispatched: bool
    ownership_retired: bool

    @model_validator(mode="after")
    def exact_result(self):
        if (self.outcome == "confirmed") != (self.result is not None):
            raise ValueError("Stream confirmation requires its original acknowledged result")
        if self.result is not None and len(encode(self.result).encode("utf-8")) > 272 * 1024:
            raise ValueError("Stream owner result exceeds its bound")
        if self.native_original is not None and self.native_original.phase != self.phase:
            raise ValueError("Stream proof changed its original operation phase")
        return self


class WhistleStreamProof(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["whistle_stream_owner_v1"]
    original: WhistleStreamOriginal
    state: Literal["reserved", "running", "completed", "acknowledged", "not_dispatched", "unknown"]
    result: dict[str, Any] | None
    result_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)
    decoder_finished: bool
    process_released: bool

    @model_validator(mode="after")
    def bounded_result(self):
        if self.state == "completed":
            if (
                self.result is None
                or len(encode(self.result).encode("utf-8")) > 128 * 1024
                or digest(self.result) != self.result_sha256
            ):
                raise ValueError("Stream result does not bind its original receipt")
        elif self.result is not None:
            raise ValueError("Only a retained completed operation exposes its result")
        if (self.result_sha256 is not None) != (self.state in {"completed", "acknowledged"}):
            raise ValueError("Stream result receipt has inconsistent state")
        if self.decoder_finished and (
            self.original.phase != "finish" or self.state not in {"completed", "acknowledged"}
        ):
            raise ValueError("Decoder completion requires the original final result")
        return self


class StreamPublication(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["whistle_stream_publication_v1"]
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=(1 << 53) - 1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)


class StreamPublicationProof(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["whistle_stream_publication_v1"]
    original: StreamPublication
    entry: JournalEntry | None

    @model_validator(mode="after")
    def exact_entry(self):
        entry, original = self.entry, self.original
        if entry is not None and (
            (entry.session_id, entry.thread_id, entry.epoch, entry.idempotency_key)
            != (
                original.session_id,
                original.thread_id,
                original.epoch,
                original.idempotency_key,
            )
            or entry.origin != "authenticated-user"
            or entry.kind != "conversation.window"
            or type(entry.sequence) is not int
            or not 1 <= entry.sequence <= 9_007_199_254_740_990
            or not 1 <= len(entry.event_id.encode("utf-8")) <= 128
            or digest(
                entry.model_dump(
                    mode="json", exclude={"event_id", "sequence", "origin", "created_at"}
                )
            )
            != original.request_sha256
        ):
            raise ValueError("Stream publication does not bind its original append")
        return self
