"""Bounded private Decisions inspection records; never routing authority."""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
MAX_REQUEST_BYTES = 262144
MAX_RESPONSE_BYTES = 1048576
MAX_OUTPUT_BYTES = 65536
MAX_CAPTURE_BYTES = MAX_REQUEST_BYTES + MAX_RESPONSE_BYTES + MAX_OUTPUT_BYTES + 8192


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


class CaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class DecisionsCallStart(CaptureModel):
    call_id: Identifier
    attempt_id: Identifier
    attempt: int = Field(ge=1, le=9007199254740991)
    source_event_id: Identifier
    source_sequence: int = Field(ge=1, le=9007199254740991)
    source_revision: int = Field(ge=1, le=9007199254740991)
    session_id: Identifier
    thread_id: Identifier
    epoch: int = Field(ge=1, le=9007199254740991)
    provider: str | None = Field(default=None, max_length=256)
    model: str | None = Field(default=None, max_length=256)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    request_format: Literal[
        "laya_systemone", "chat_completions", "responses", "not_submitted", "reused"
    ]
    request: dict[str, Any] | None = None
    reused_from_call_id: Identifier | None = None

    @model_validator(mode="after")
    def bounds(self):
        if len(encoded(self.request)) > MAX_REQUEST_BYTES:
            raise ValueError("Decisions request exceeds its capture bound")
        if self.reused_from_call_id is not None and self.request_format != "reused":
            raise ValueError("Reuse requires an explicit reuse disposition")
        if self.request_format in {"not_submitted", "reused"} and self.request is not None:
            raise ValueError("An unsubmitted call has no provider request")
        if self.request_format not in {"not_submitted", "reused"} and self.request is None:
            raise ValueError("A submitted call requires its actual request")
        if self.request_format == "reused" and self.reused_from_call_id is None:
            raise ValueError("Reuse requires the original call identity")
        return self


class DecisionsCallFinish(CaptureModel):
    source_event_id: Identifier
    status: Literal["completed", "refused", "error", "cancelled", "reused"]
    raw_response: str | None = None
    validated_output: Any = None
    error_type: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bounds(self):
        if self.raw_response is not None and len(self.raw_response.encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("Decisions response exceeds its capture bound")
        if len(encoded(self.raw_response)) > MAX_RESPONSE_BYTES + 2:
            raise ValueError("Encoded Decisions response exceeds its capture bound")
        if len(encoded(self.validated_output)) > MAX_OUTPUT_BYTES:
            raise ValueError("Decisions output exceeds its capture bound")
        return self
