"""Required routing-runtime readiness, separate from model and task outcomes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DecisionsReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal[1]
    service: Literal["decisions"]
    consumer: Literal["converse"]
    instance_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    phase: Literal["starting", "ready", "unavailable", "stopped"]
    ready: bool

    @field_validator("contract_version", mode="before")
    @classmethod
    def version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported Decisions readiness version")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.ready != (self.phase == "ready"):
            raise ValueError("Inconsistent Decisions readiness")
        return self
