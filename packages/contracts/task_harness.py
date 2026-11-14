"""Additive task-orchestrator action; legacy ToolAction remains unchanged."""

from typing import Any, Literal

from pydantic import Field, field_validator

from packages.contracts.models import WireModel


class RunTaskHarnessArguments(WireModel):
    source_path: str = Field(min_length=1, max_length=1024)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: dict[str, Any]

    @field_validator("source_path")
    @classmethod
    def relative_source(cls, value):
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
        ):
            raise ValueError("Harness source requires a relative workspace path")
        value.encode("utf-8")
        return value


class RunTaskHarnessAction(WireModel):
    tool: Literal["run_task_harness"]
    arguments: RunTaskHarnessArguments
