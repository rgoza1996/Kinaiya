"""Native task budgets; public requests cannot choose their accepted pin."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TASK_LIMIT_REVISION = 9_007_199_254_740_990


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TaskLimitsSettings(_Closed):
    max_concurrent_tasks: int = Field(ge=1, le=64)
    max_duration_s: int = Field(ge=1, le=86400)


class TaskLimitsChange(_Closed):
    schema_version: Literal["1.0"]
    expected_backend: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    expected_instance: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    expected_revision: int = Field(ge=0, lt=MAX_TASK_LIMIT_REVISION)
    expected_state: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    settings: TaskLimitsSettings

    @field_validator("idempotency_key")
    @classmethod
    def exact_key(cls, value):
        if re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", value) is None:
            raise ValueError("Invalid task-limit change identity")
        return value


class TaskLimitsPin(_Closed):
    schema_version: Literal["1.0"] = "1.0"
    backend_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    instance_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    slot_id: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    settings_revision: int = Field(ge=0, le=MAX_TASK_LIMIT_REVISION)
    max_concurrent_tasks: int = Field(ge=1, le=64)
    max_duration_s: int = Field(ge=1, le=86400)
    max_steps: int = Field(ge=1, le=2000)
    # Display/provenance only. Native duration enforcement uses monotonic time;
    # recovery never turns this wall clock into a new worker or fresh budget.
    reserved_at_unix_ms: int = Field(ge=0, le=MAX_TASK_LIMIT_REVISION)
