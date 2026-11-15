"""Owner-configured Converse instructions, independent of model/task authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversePromptConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    contract_version: Literal[1] = 1
    text: str = Field(max_length=65536)
    content_scope: Literal["local_only", "public_synthetic", "private_cloud_authorized"] = (
        "local_only"
    )

    @field_validator("contract_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported Converse prompt version")
        return value

    @field_validator("text")
    @classmethod
    def bounded_utf8(cls, value):
        if len(value.encode("utf-8")) > 65536 or "\x00" in value:
            raise ValueError("Converse prompt exceeds its text contract")
        return value
