"""Independent owner STT selection; configuration grants no audio disclosure."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import CatalogModelBinding

MAX_STT_REVISION = 9_007_199_254_740_990


class STTModelSelection(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    source: Literal["host", "catalog"]
    catalog_binding: CatalogModelBinding | None

    @model_validator(mode="after")
    def exact_source(self):
        if (self.source == "host") != (self.catalog_binding is None):
            raise ValueError("STT requires its exact host or catalog source")
        if self.catalog_binding is not None:
            binding = CatalogModelBinding.model_validate(self.catalog_binding.model_dump())
            if (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", binding.id) is None
                or re.fullmatch(r"[a-f0-9]{64}", binding.configuration_sha256) is None
            ):
                raise ValueError("Invalid STT catalog identity")
        return self


class STTModelChange(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    schema_version: Literal["1.0"]
    expected_backend: str = Field(min_length=32, max_length=32)
    expected_instance: str = Field(min_length=32, max_length=32)
    expected_revision: int = Field(ge=0, lt=MAX_STT_REVISION)
    expected_state: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    selection: STTModelSelection

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
            raise ValueError("Invalid STT owner operation identity")
        return value

    @model_validator(mode="after")
    def bounded_request(self):
        if len(self.model_dump_json().encode("utf-8")) > 4096:
            raise ValueError("STT owner request exceeds its bound")
        return self
