"""Exact owner text-model selection; this never grants capture or disclosure."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import CatalogModelBinding

MAX_MODEL_REVISION = 9_007_199_254_740_990
Reasoning = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class ConverseModelSelection(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    source: Literal["host", "catalog"]
    catalog_binding: CatalogModelBinding | None
    reasoning: Reasoning | None

    @model_validator(mode="after")
    def exact_source(self):
        if self.source == "host":
            if self.catalog_binding is not None or self.reasoning is not None:
                raise ValueError("The host selection keeps its native configured reasoning")
        else:
            binding = self.catalog_binding
            if binding is None:
                raise ValueError("Catalog selection requires its exact native generation")
            # Revalidate even an in-process, unchecked model_construct instance.
            binding = CatalogModelBinding.model_validate(binding.model_dump())
            if (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", binding.id) is None
                or re.fullmatch(r"[a-f0-9]{64}", binding.configuration_sha256) is None
            ):
                raise ValueError("Invalid catalog text-model identity")
        return self


class ConverseModelChange(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    schema_version: Literal["1.0"]
    expected_revision: int = Field(ge=0, lt=MAX_MODEL_REVISION)
    expected_state: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")
    selection: ConverseModelSelection

    @field_validator("expected_state", "idempotency_key")
    @classmethod
    def exact_token(cls, value, info):
        pattern = (
            r"[a-f0-9]{64}" if info.field_name == "expected_state" else r"[A-Za-z0-9:._-]{1,128}"
        )
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid model-selection token")
        return value


class RoleModelPin(BaseModel):
    """Actual operation target, not a readiness receipt or credential reference."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    role: Literal["converse"] = "converse"
    source: Literal["host", "catalog"]
    settings_revision: int = Field(ge=0, le=MAX_MODEL_REVISION)
    catalog_revision: int | None = Field(ge=0, le=MAX_MODEL_REVISION)
    catalog_binding: CatalogModelBinding | None
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    local: bool
    reasoning: str | None = Field(min_length=1, max_length=16)

    @field_validator("provider", "model", "reasoning")
    @classmethod
    def bounded_text(cls, value, info):
        if value is not None and (
            len(value.encode("utf-8")) > (16 if info.field_name == "reasoning" else 256)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Invalid text-model display identity")
        return value

    @model_validator(mode="after")
    def exact_source(self):
        if self.source == "host":
            if self.catalog_revision is not None or self.catalog_binding is not None:
                raise ValueError("Host model pins cannot claim a catalog generation")
        else:
            if self.catalog_revision is None or self.catalog_binding is None:
                raise ValueError("Catalog model pins require both exact native identities")
            ConverseModelSelection(
                source="catalog", catalog_binding=self.catalog_binding, reasoning=None
            )
            if self.reasoning not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
                raise ValueError("Catalog model pin reasoning is unavailable")
        return self
