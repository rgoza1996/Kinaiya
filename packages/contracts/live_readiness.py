"""Live routing-loop readiness; no model, audio, permission or success claim."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LiveDecisionsReadiness(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )

    contract_version: Literal[2]
    protocol: Literal["native_live_decisions_v2"]
    service: Literal["decisions"]
    consumer: Literal["converse"]
    instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    phase: Literal["disabled", "starting", "ready", "unavailable", "stopped"]
    ready: bool

    @field_validator("contract_version", mode="before")
    @classmethod
    def version(cls, value):
        if type(value) is not int or value != 2:
            raise ValueError("Unsupported live Decisions readiness version")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.ready != (self.phase == "ready"):
            raise ValueError("Inconsistent live routing readiness")
        return self
