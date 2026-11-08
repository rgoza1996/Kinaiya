"""Independent owner TTS selection; configuration grants no speech disclosure."""

import re
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import CatalogModelBinding, STTModelPin

MAX_TTS_REVISION = 9_007_199_254_740_990
TTS_SETTLEMENT_PROTOCOL: Final = "native_tts_settlement_v1"
_WORKER_ID = r"^[a-f0-9]{32}$"
_OPERATION_ID = r"^tts_op_[a-f0-9]{32}$"
_SHA256 = r"^[a-f0-9]{64}$"


def _validate_loopback_origin(value: str) -> str:
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value):
        raise ValueError("TTS worker origin must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("TTS worker origin must be an explicit private loopback HTTP origin")
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    if value != f"http://{host}:{parsed.port}":
        raise ValueError("TTS worker origin must be canonical")
    return value


class TTSModelSelection(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    source: Literal["host", "catalog"]
    catalog_binding: CatalogModelBinding | None

    @model_validator(mode="after")
    def exact_source(self):
        if (self.source == "host") != (self.catalog_binding is None):
            raise ValueError("TTS requires its exact host or catalog source")
        if self.catalog_binding is not None:
            binding = CatalogModelBinding.model_validate(self.catalog_binding.model_dump())
            if (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", binding.id) is None
                or re.fullmatch(r"[a-f0-9]{64}", binding.configuration_sha256) is None
            ):
                raise ValueError("Invalid TTS catalog identity")
        return self


class TTSModelChange(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    schema_version: Literal["1.0"]
    expected_backend: str = Field(min_length=32, max_length=32)
    expected_instance: str = Field(min_length=32, max_length=32)
    expected_revision: int = Field(ge=0, lt=MAX_TTS_REVISION)
    expected_state: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    selection: TTSModelSelection

    @field_validator("expected_backend", "expected_instance", "expected_state", "idempotency_key")
    @classmethod
    def exact_tokens(cls, value, info):
        pattern = (
            r"[A-Za-z0-9:._-]{1,128}"
            if info.field_name == "idempotency_key"
            else r"[a-f0-9]{64}"
            if info.field_name == "expected_state"
            else r"[a-f0-9]{32}"
        )
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid TTS owner operation identity")
        return value

    @model_validator(mode="after")
    def bounded_request(self):
        if len(self.model_dump_json().encode("utf-8")) > 4096:
            raise ValueError("TTS owner request exceeds its bound")
        return self


class TTSModelPin(STTModelPin):
    """Captured speech target; undeclared host voice remains explicitly unknown."""

    role: Literal["tts"] = "tts"
    profile: str | None = Field(min_length=1, max_length=256)
    voice: str | None = Field(min_length=1, max_length=256)

    @field_validator("profile", "voice")
    @classmethod
    def public_voice(cls, value):
        return STTModelPin.display_identity(value)

    @model_validator(mode="after")
    def exact_catalog_voice(self):
        if self.source == "catalog" and (
            self.provider,
            self.model,
            self.local,
            self.profile,
            self.voice,
        ) != ("openrouter", "hexgrad/kokoro-82m", False, "Kokoro 82M (OpenRouter)", "af_heart"):
            raise ValueError("Unsupported captured TTS catalog voice")
        return self


class TTSSynthesisAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_event_id: str = Field(min_length=1, max_length=128)
    generation_epoch: int = Field(ge=1, le=9_007_199_254_740_990)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tts_model: TTSModelPin
    content_scope: Literal["local_only", "public_synthetic", "private_cloud_authorized"]

    @field_validator("operation_id", "text_sha256", "source_event_id")
    @classmethod
    def exact_synthesis_identity(cls, value, info):
        if info.field_name == "source_event_id":
            if len(value.encode("utf-8")) > 128 or any(
                ord(char) < 32 or ord(char) == 127 for char in value
            ):
                raise ValueError("Invalid synthesis source identity")
        elif (
            re.fullmatch(
                r"[a-f0-9]{32}" if info.field_name == "operation_id" else r"[a-f0-9]{64}", value
            )
            is None
        ):
            raise ValueError("Invalid synthesis identity")
        return value


class _StrictTTSModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


class TTSWorkerReservation(_StrictTTSModel):
    protocol: Literal["native_tts_settlement_v1"] = TTS_SETTLEMENT_PROTOCOL
    text_sha256: str = Field(pattern=_SHA256)
    profile: str = Field(min_length=1, max_length=64)
    request_sha256: str = Field(pattern=_SHA256)


class TTSWorkerOperation(_StrictTTSModel):
    protocol: Literal["native_tts_settlement_v1"] = TTS_SETTLEMENT_PROTOCOL
    worker_instance: str = Field(pattern=_WORKER_ID)
    operation_id: str = Field(pattern=_OPERATION_ID)
    text_sha256: str = Field(pattern=_SHA256)
    profile: str = Field(min_length=1, max_length=64)
    request_sha256: str = Field(pattern=_SHA256)


class TTSWorkerProof(_StrictTTSModel):
    protocol: Literal["native_tts_settlement_v1"] = TTS_SETTLEMENT_PROTOCOL
    worker_instance: str = Field(pattern=_WORKER_ID)
    operation_id: str = Field(pattern=_OPERATION_ID)
    text_sha256: str = Field(pattern=_SHA256)
    profile: str = Field(min_length=1, max_length=64)
    request_sha256: str = Field(pattern=_SHA256)
    state: Literal["reserved", "running", "settled", "unknown"]
    outcome: Literal["not_dispatched", "completed", "cancelled", "failed"] | None = None
    settled: bool

    @model_validator(mode="after")
    def validate_settlement(self):
        if self.settled:
            if self.state != "settled" or self.outcome not in (
                "not_dispatched",
                "completed",
                "cancelled",
                "failed",
            ):
                raise ValueError("Settled TTS worker proof requires a terminal outcome")
        elif self.state == "settled" or self.outcome is not None:
            raise ValueError("Unsettled TTS worker proof cannot declare a terminal outcome")
        return self


class TTSWorkerBinding(_StrictTTSModel):
    protocol: Literal["native_tts_settlement_v1"] = TTS_SETTLEMENT_PROTOCOL
    origin: str = Field(min_length=1, max_length=256)
    worker_instance: str = Field(pattern=_WORKER_ID)
    operation_id: str = Field(pattern=_OPERATION_ID)
    tts_operation_id: str = Field(pattern=_WORKER_ID)
    synthesis_event_id: str = Field(min_length=1, max_length=128)
    synthesis_sequence: int = Field(ge=1, le=MAX_TTS_REVISION)
    source_event_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=MAX_TTS_REVISION)
    generation_epoch: int = Field(ge=1, le=MAX_TTS_REVISION)
    text_sha256: str = Field(pattern=_SHA256)
    profile: str = Field(min_length=1, max_length=64)
    request_sha256: str = Field(pattern=_SHA256)
    audio_exposed: bool = False

    @model_validator(mode="after")
    def validate_binding(self):
        _validate_loopback_origin(self.origin)
        for field_name in ("synthesis_event_id", "source_event_id", "session_id", "thread_id"):
            value = getattr(self, field_name)
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise ValueError(f"Invalid TTS worker binding {field_name}")
        return self


class TTSAdmissionReconcile(_StrictTTSModel):
    schema_version: Literal["1.0"] = "1.0"
    expected_backend_id: str = Field(pattern=_WORKER_ID)
    admission_id: str = Field(pattern=_WORKER_ID)
    pin_sha256: str = Field(pattern=_SHA256)


class TTSAdmissionResult(_StrictTTSModel):
    schema_version: Literal["1.0"] = "1.0"
    backend_id: str = Field(pattern=_WORKER_ID)
    admission_id: str = Field(pattern=_WORKER_ID)
    pin_sha256: str = Field(pattern=_SHA256)
    worker_instance: str = Field(pattern=_WORKER_ID)
    operation_id: str = Field(pattern=_OPERATION_ID)
    worker_outcome: Literal["not_dispatched", "completed", "cancelled", "failed"]
    state: Literal["retired"] = "retired"
    replayed: bool
