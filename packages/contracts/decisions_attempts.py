"""Version 1 charged finalized Decisions attempts, separate from inspection."""

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.decisions import MAX_OUTPUT_BYTES, MAX_RESPONSE_BYTES, encoded
from packages.contracts.decisions_models import DecisionsModelPin

MAX_ATTEMPTS = 16
MAX_ADMISSION_BYTES = 131072
MAX_ATTEMPT_BYTES = MAX_RESPONSE_BYTES + MAX_OUTPUT_BYTES + MAX_ADMISSION_BYTES + 8192


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def admission_binding(value, source):
    """Shared wire invariants; each explicit protocol supplies its bare source."""
    if value.disclosure_sources[0] != source:
        raise ValueError("The original source must be disclosed first")
    if any(
        (s.session_id, s.thread_id, s.epoch) != (source.session_id, source.thread_id, source.epoch)
        for s in value.disclosure_sources
    ):
        raise ValueError("Attempt disclosure crossed native scope")
    if value.model_pin is not None and (
        value.model_pin.provider,
        value.model_pin.model,
        value.model_pin.local,
    ) != (value.provider.provider, value.provider.model, value.provider.local):
        raise ValueError("Attempt model pin mismatch")
    if (
        value.model_pin is not None
        and value.model_pin.protocol != "host_configured"
        and value.request_format != value.model_pin.protocol
    ):
        raise ValueError("Attempt protocol mismatch")
    if len(encoded(value.model_dump(mode="json"))) > MAX_ADMISSION_BYTES:
        raise ValueError("Attempt admission exceeds its byte bound")
    return value


class _Wire(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


class DecisionsSource(_Wire):
    source_event_id: str = Field(min_length=1, max_length=128)
    source_sequence: int = Field(ge=1, le=9007199254740991)
    source_revision: int = Field(ge=1, le=9007199254740991)
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=9007199254740991)

    @classmethod
    def of(cls, entry):
        return cls(
            source_event_id=entry.event_id,
            source_sequence=entry.sequence,
            source_revision=entry.payload.get("revision", 1),
            session_id=entry.session_id,
            thread_id=entry.thread_id,
            epoch=entry.epoch,
        )


class DecisionsAttemptLookup(_Wire):
    contract_version: Literal[1] = 1
    source: DecisionsSource


class DecisionsAttemptProvider(_Wire):
    schema_version: Literal["1.0"] = "1.0"
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    local: bool
    streaming: bool = False


class DecisionsAttemptAdmit(DecisionsAttemptLookup):
    attempt_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    owner_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    expected_attempts: int = Field(ge=0, le=MAX_ATTEMPTS)
    max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS)
    provider: DecisionsAttemptProvider
    model_pin: DecisionsModelPin | None
    request_format: Literal[
        "laya_systemone", "native_d1_systemone", "chat_completions", "responses"
    ]
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    transport_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    disclosure_sources: list[DecisionsSource] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def binding(self):
        return admission_binding(self, self.source)


class DecisionsAttemptFinish(DecisionsAttemptLookup):
    attempt_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    owner_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    admission_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["completed", "refused", "error", "uncertain"]
    raw_response: str | None = None
    validated_output: Any = None
    error_type: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    reason_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,120}$")

    @model_validator(mode="after")
    def bounds(self):
        if (
            len(encoded(self.raw_response)) > MAX_RESPONSE_BYTES + 2
            or len(encoded(self.validated_output)) > MAX_OUTPUT_BYTES
        ):
            raise ValueError("Attempt completion exceeds its bound")
        if self.status != "completed" and self.validated_output is not None:
            raise ValueError("Only completed attempts have a result")
        return self


class DecisionsAttempt(_Wire):
    admission: DecisionsAttemptAdmit
    admission_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt: int = Field(ge=1, le=MAX_ATTEMPTS)
    completion: DecisionsAttemptFinish | None

    @model_validator(mode="after")
    def binding(self):
        if digest(self.admission.model_dump(mode="json")) != self.admission_sha256:
            raise ValueError("Attempt admission digest mismatch")
        if self.completion is not None and (
            self.completion.source,
            self.completion.attempt_id,
            self.completion.owner_id,
            self.completion.admission_sha256,
        ) != (
            self.admission.source,
            self.admission.attempt_id,
            self.admission.owner_id,
            self.admission_sha256,
        ):
            raise ValueError("Attempt completion binding mismatch")
        return self


class DecisionsAttemptState(DecisionsAttemptLookup):
    state: Literal[
        "known_absent",
        "admitted",
        "completed",
        "refused",
        "retryable_error",
        "exhausted",
        "uncertain",
        "legacy_uncertain",
    ]
    max_attempts: int = Field(ge=1, le=MAX_ATTEMPTS)
    charged_attempts: int = Field(ge=0, le=MAX_ATTEMPTS)
    last_attempt: DecisionsAttempt | None

    @model_validator(mode="after")
    def binding(self):
        last = self.last_attempt
        if self.charged_attempts > self.max_attempts:
            raise ValueError("Attempt allowance exceeded")
        if self.state == "known_absent" and (last is not None or self.charged_attempts):
            raise ValueError("Absence contradicts admission")
        if last is not None and (
            last.admission.source != self.source
            or last.attempt != self.charged_attempts
            or digest(last.admission.model_dump(mode="json")) != last.admission_sha256
        ):
            raise ValueError("Attempt state binding mismatch")
        if last is None and (
            self.charged_attempts != 0 or self.state not in {"known_absent", "legacy_uncertain"}
        ):
            raise ValueError("Attempt state lacks its original admission")
        if last is not None:
            expected = (
                "admitted"
                if last.completion is None
                else {
                    "completed": "completed",
                    "refused": "refused",
                    "uncertain": "uncertain",
                    "error": "exhausted"
                    if self.charged_attempts >= self.max_attempts
                    else "retryable_error",
                }[last.completion.status]
            )
            if self.state != expected:
                raise ValueError("Attempt state contradicts its original completion")
        return self
