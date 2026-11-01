"""Owner capability availability and exact installed-extension activation."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.contracts.agent_tools import NATIVE_TOOL_ORDER

MAX_REVISION = 9_007_199_254_740_990


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ToolAvailabilityAction(_Closed):
    operation: Literal["set_tools"]
    disabled: list[str] = Field(max_length=15)

    @field_validator("disabled")
    @classmethod
    def known(cls, value):
        if (
            len(set(value)) != len(value)
            or any(name not in NATIVE_TOOL_ORDER or name == "finish" for name in value)
            or value != [name for name in NATIVE_TOOL_ORDER if name in value]
        ):
            raise ValueError("Use unique configurable tools in native protocol order")
        return value


class SkillActivationAction(_Closed):
    operation: Literal["enable_skill", "disable_skill"]
    skill_id: str = Field(min_length=1, max_length=265)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", min_length=71, max_length=71)

    @field_validator("skill_id")
    @classmethod
    def safe_id(cls, value):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Invalid installed skill identity")
        value.encode("utf-8")
        return value


class PluginActivationAction(_Closed):
    operation: Literal["evaluate_plugin", "promote_plugin", "disable_plugin", "rollback_plugin"]
    plugin_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$", max_length=64)
    version_id: str = Field(pattern=r"^v-[0-9a-f]{24}$", min_length=26, max_length=26)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    report_id: str | None = Field(default=None, pattern=r"^rep-[A-Za-z0-9-]{1,60}$", max_length=64)
    reviewed: bool = False

    @field_validator("plugin_id", "report_id")
    @classmethod
    def exact_token(cls, value, info):
        pattern = (
            r"[a-z][a-z0-9_-]{0,63}"
            if info.field_name == "plugin_id"
            else r"rep-[A-Za-z0-9-]{1,60}"
        )
        if value is not None and re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid installed plugin identity")
        return value


CapabilityAction = Annotated[
    ToolAvailabilityAction | SkillActivationAction | PluginActivationAction,
    Field(discriminator="operation"),
]


class CapabilityChange(_Closed):
    schema_version: Literal["1.0"]
    expected_backend: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    expected_instance: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    expected_revision: int = Field(ge=0, lt=MAX_REVISION)
    expected_state: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,128}$", max_length=128)
    action: CapabilityAction

    @field_validator("idempotency_key")
    @classmethod
    def exact_key(cls, value):
        if re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", value) is None:
            raise ValueError("Invalid capability change identity")
        return value


def action_section(action):
    if isinstance(action, ToolAvailabilityAction):
        return "tools"
    return "skills" if isinstance(action, SkillActivationAction) else "plugins"
