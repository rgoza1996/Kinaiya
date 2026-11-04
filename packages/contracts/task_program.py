"""Additive task-owned executable route; arguments never grant authority."""

from typing import Literal

from pydantic import Field, field_validator, model_validator

from packages.contracts.agent_media import Digest, source_path
from packages.contracts.models import WireModel


class RunTaskProgramArguments(WireModel):
    interpreter: Literal["python3", "bash", "direct"]
    source_path: str
    source_sha256: Digest
    entrypoint_path: str
    entrypoint_sha256: Digest
    input_path: str
    input_sha256: Digest
    output_path: str
    argv: list[str] = Field(min_length=2, max_length=32)
    timeout_s: int = Field(ge=1, le=120)

    _paths = field_validator("source_path", "entrypoint_path", "input_path", "output_path")(
        source_path
    )

    @field_validator("argv")
    @classmethod
    def bounded_arguments(cls, values):
        if (
            any(
                type(value) is not str
                or len(value.encode("utf-8")) > 4096
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
                for value in values
            )
            or sum(len(value.encode("utf-8")) for value in values) > 16384
        ):
            raise ValueError("Program arguments exceed their native bound")
        if values.count("{input}") != 1 or values.count("{output}") != 1:
            raise ValueError("Program requires exact input and output path placeholders")
        return values

    @model_validator(mode="after")
    def separate_output(self):
        if self.output_path in {self.source_path, self.entrypoint_path, self.input_path}:
            raise ValueError("Program output must not replace its source, entrypoint or input")
        return self


class RunTaskProgramAction(WireModel):
    tool: Literal["run_task_program"]
    arguments: RunTaskProgramArguments
