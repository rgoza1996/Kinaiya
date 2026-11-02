"""Independent classifier selection; no dialogue, capture or disclosure grant."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import CatalogModelBinding

MAX_DECISIONS_MODEL_REVISION = 9_007_199_254_740_990


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


class DecisionsModelSelection(_Closed):
    source: Literal["host", "catalog"]
    catalog_binding: CatalogModelBinding | None

    @model_validator(mode="after")
    def exact_source(self):
        if (self.source == "host") != (self.catalog_binding is None):
            raise ValueError("Classifier selection requires its exact source")
        if self.catalog_binding is not None:
            binding = CatalogModelBinding.model_validate(self.catalog_binding.model_dump())
            if (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", binding.id) is None
                or re.fullmatch(r"[a-f0-9]{64}", binding.configuration_sha256) is None
            ):
                raise ValueError("Classifier catalog identity is invalid")
        return self


class DecisionsModelChange(_Closed):
    schema_version: Literal["1.0"]
    expected_backend: str = Field(min_length=32, max_length=32)
    expected_instance: str = Field(min_length=32, max_length=32)
    expected_revision: int = Field(ge=0, lt=MAX_DECISIONS_MODEL_REVISION)
    expected_state: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    selection: DecisionsModelSelection

    @field_validator("expected_backend", "expected_instance", "expected_state", "idempotency_key")
    @classmethod
    def identity(cls, value, info):
        pattern = (
            r"[A-Za-z0-9:._-]{1,128}"
            if info.field_name == "idempotency_key"
            else r"[a-f0-9]{64}"
            if info.field_name == "expected_state"
            else r"[a-f0-9]{32}"
        )
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Classifier request identity is invalid")
        return value


class DecisionsModelPin(_Closed):
    role: Literal["decisions"] = "decisions"
    backend_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    instance_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    settings_revision: int = Field(ge=0, le=MAX_DECISIONS_MODEL_REVISION)
    selection: DecisionsModelSelection
    catalog_revision: int | None = Field(ge=0, le=MAX_DECISIONS_MODEL_REVISION)
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    local: bool
    protocol: Literal["laya_systemone", "native_d1_systemone", "host_configured"]

    @field_validator("provider", "model")
    @classmethod
    def display(cls, value):
        if len(value.encode("utf-8")) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Classifier display identity is invalid")
        return value

    @model_validator(mode="after")
    def exact_catalog(self):
        if (self.selection.source == "host") != (self.catalog_revision is None):
            raise ValueError("Classifier pin requires its exact catalog provenance")
        if self.provider == "native_d1" and (
            self.protocol != "native_d1_systemone" or self.local is not True
        ):
            raise ValueError("Native d1 requires its exact protocol")
        if self.protocol == "native_d1_systemone" and self.provider != "native_d1":
            raise ValueError("Native d1 protocol requires its exact provider")
        if self.selection.source == "catalog" and (self.provider, self.protocol) not in {
            ("laya", "laya_systemone"),
            ("native_d1", "native_d1_systemone"),
        }:
            raise ValueError("Only exact typed classifier protocol pairs are supported")
        return self
