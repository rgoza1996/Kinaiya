"""Closed owner rules for three native operations, never filesystem-wide grants."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PERMISSION_REVISION = 9_007_199_254_740_990
MAX_SCOPE_REVISION = 9_007_199_254_740_991
NATIVE_PERMISSION_TOOLS = ("read_file", "write_file", "shell")
PermissionTool = Literal["read_file", "write_file", "shell"]
PermissionMode = Literal["allow", "ask", "deny"]


class PermissionScope(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )

    kind: Literal["default", "workspace", "session", "task"]
    workspace_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    thread_id: str | None = Field(default=None, min_length=1, max_length=128)
    epoch: int | None = Field(default=None, ge=1, le=MAX_SCOPE_REVISION)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    objective_revision: int | None = Field(default=None, ge=1, le=MAX_SCOPE_REVISION)

    @field_validator("session_id", "thread_id", "task_id")
    @classmethod
    def safe_identity(cls, value):
        if value is not None:
            if "\0" in value:
                raise ValueError("Permission scope identity contains a NUL")
            value.encode("utf-8")
        return value

    @field_validator("workspace_id")
    @classmethod
    def exact_workspace_id(cls, value):
        if value is not None and (len(value) != 64 or re.fullmatch(r"[a-f0-9]{64}", value) is None):
            raise ValueError("Invalid native workspace identity")
        return value

    @model_validator(mode="after")
    def exact_scope(self):
        names = set(self.model_dump(exclude_none=True))
        expected = {"kind"}
        if self.kind != "default":
            expected.add("workspace_id")
        if self.kind in {"session", "task"}:
            expected.update({"session_id", "thread_id", "epoch"})
        if self.kind == "task":
            expected.update({"task_id", "objective_revision"})
        if names != expected:
            raise ValueError("Permission scope does not match its native kind")
        encoded = json.dumps(
            self.model_dump(exclude_none=True), ensure_ascii=False, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > 2048:
            raise ValueError("Permission scope exceeds its native byte bound")
        return self


class ToolPermissionChange(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )

    schema_version: Literal["1.0"]
    expected_revision: int = Field(ge=0, le=MAX_PERMISSION_REVISION)
    expected_state: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]{1,128}$")
    scope: PermissionScope
    tool: PermissionTool
    mode: PermissionMode | None

    @field_validator("expected_state", "idempotency_key")
    @classmethod
    def exact_token(cls, value, info):
        pattern = (
            r"[a-f0-9]{64}" if info.field_name == "expected_state" else r"[A-Za-z0-9:._-]{1,128}"
        )
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid native permission token")
        return value
