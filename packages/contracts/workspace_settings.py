"""Owner-selected native workspace authority; no generated path grants.

Saving is an explicit owner root grant. Reads never select a directory or
create one. The first implementation uses mandatory Bubblewrap, with owner
read-only overlays added to immutable host guards. Historical save receipts do
not prove that the selection is currently active. Task pins omit host paths.
"""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_WORKSPACE_REVISION = 9_007_199_254_740_990
MAX_PATH_BYTES = 4096
MAX_RELATIVE_PATH_BYTES = 512
MAX_READONLY_PATHS = 32
MAX_WORKSPACE_REQUEST_BYTES = 65_536
MAX_WORKSPACE_METADATA_BYTES = 262_144


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


def _path(value, *, relative):
    maximum = MAX_RELATIVE_PATH_BYTES if relative else MAX_PATH_BYTES
    if (
        type(value) is not str
        or not 1 <= len(value.encode("utf-8")) <= maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Invalid native workspace path")
    path = Path(value)
    if (
        path.is_absolute() == relative
        or ".." in path.parts
        or path.as_posix() != value
        or (not relative and value.startswith("//"))
    ):
        raise ValueError("Workspace paths require canonical spelling")
    return value


class WorkspaceSelection(_Closed):
    root: str = Field(min_length=1, max_length=MAX_PATH_BYTES)
    readonly_paths: list[str] = Field(max_length=MAX_READONLY_PATHS)

    @field_validator("root")
    @classmethod
    def root_path(cls, value):
        return _path(value, relative=False)

    @field_validator("readonly_paths")
    @classmethod
    def readonly_path_list(cls, value):
        for path in value:
            _path(path, relative=True)
        if value != sorted(set(value)):
            raise ValueError("Read-only workspace paths must be sorted and unique")
        return value


class WorkspaceChange(_Closed):
    schema_version: Literal["1.0"]
    expected_backend: str = Field(min_length=32, max_length=32)
    expected_instance: str = Field(min_length=32, max_length=32)
    expected_revision: int = Field(ge=0, lt=MAX_WORKSPACE_REVISION)
    expected_state: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    settings: WorkspaceSelection

    @field_validator("expected_backend", "expected_instance", "expected_state", "idempotency_key")
    @classmethod
    def exact_identity(cls, value, info):
        pattern = {
            "expected_backend": r"[a-f0-9]{32}",
            "expected_instance": r"[a-f0-9]{32}",
            "expected_state": r"[a-f0-9]{64}",
            "idempotency_key": r"[A-Za-z0-9:._-]{1,128}",
        }[info.field_name]
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid workspace request identity")
        return value

    def bounded_body(self):
        body = self.model_dump(mode="json")
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_WORKSPACE_REQUEST_BYTES:
            raise ValueError("Workspace request exceeds its native byte bound")
        return body


class WorkspacePin(_Closed):
    schema_version: Literal["1.0"]
    workspace_id: str = Field(min_length=64, max_length=64)
    root_identity: str = Field(min_length=64, max_length=64)
    settings_revision: int = Field(ge=0, le=MAX_WORKSPACE_REVISION)
    source: Literal["host", "owner_saved"]
    shell_backend: Literal["bubblewrap"]
    mode: Literal["direct"]

    @field_validator("workspace_id", "root_identity")
    @classmethod
    def exact_digest(cls, value):
        if re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise ValueError("Invalid native workspace pin")
        return value
