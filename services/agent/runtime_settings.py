"""Protected owner execution preferences; no model, capture or workspace relocation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.policy import PolicyDenied
from services.agent.tools import bwrap_path, shell_directory


class RuntimeSettingsConflict(ValueError):
    pass


class ExecutionPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file_writes: Literal["ask", "allow", "deny"]
    shell_commands: Literal["ask", "allow", "deny"]
    tool_network: bool
    max_steps: int = Field(ge=1, le=2000)
    shell_timeout_s: int = Field(ge=1, le=120)
    working_directory: str = Field(min_length=1, max_length=512)

    @field_validator("working_directory")
    @classmethod
    def relative_directory(cls, value):
        path = Path(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Use a relative directory inside the approved workspace")
        return path.as_posix()


class RuntimeSettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    expected_revision: int = Field(ge=0, le=9_007_199_254_740_990)
    expected_state: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,128}$")
    settings: ExecutionPreferences
    # This remains process-local, including when the persisted settings change.
    yolo: bool


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def execution_metadata(runtime):
    """Owner inspection only: requested isolation is distinct from a live proof."""
    policy = runtime.policy

    def paths(values):
        return {
            "paths": [str(value) for value in values[:64]],
            "total": len(values),
            "truncated": len(values) > 64,
        }

    try:
        directory = str(shell_directory(policy))
        directory_valid = True
    except (PolicyDenied, OSError, ValueError):
        directory, directory_valid = None, False
    readonly_workspace = policy.deny_write or any(
        root == policy.workspace or root in policy.workspace.parents
        for root in policy.readonly_roots
    )
    return {
        "workspace": str(policy.workspace),
        "shell_workspace_access": "unavailable"
        if not directory_valid
        else "read_only"
        if readonly_workspace
        else "read_write",
        "shell_directory": directory,
        "shell_directory_valid": directory_valid,
        "readonly": paths(policy.readonly_roots),
        "protected": paths(policy.protected_roots),
        "shell_backend": "bubblewrap",
        "shell_binary_detected": bwrap_path() is not None,
        "isolation_verified": False,
        "network_namespace": "shared_host" if policy.network else "isolated",
        "environment": {"clear_host_environment": True, "PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        "sandbox_optional": False,
        "workspace_mutable": False,
    }


class AgentRuntimeSettings:
    def __init__(self, runtime, *, path=None):
        self.runtime = runtime
        self.instance = str(uuid.uuid4())
        if path is not None:
            path = Path(path).resolve()
            if path.is_relative_to(runtime.policy.workspace):
                raise ValueError("Execution settings must be outside the worker workspace")
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path is not None else ":memory:")
        if path is not None:
            path.chmod(0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), "
            "revision INTEGER NOT NULL, body TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
            "receipt TEXT NOT NULL)"
        )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO settings VALUES (1,0,?)",
                (self._current().model_dump_json(),),
            )
        revision, saved = self._read()
        if revision:
            # A removed directory leaves shell unavailable; opening Options can
            # still repair it. Never silently substitute a different directory.
            self._apply(saved, runtime.policy.yolo)
        else:
            # Until the first owner save, startup configuration remains source.
            with self.db:
                self.db.execute(
                    "UPDATE settings SET body=? WHERE id=1", (self._current().model_dump_json(),)
                )

    def _current(self):
        policy = self.runtime.policy
        cap = policy.shell_timeout_s
        if (
            type(cap) not in {int, float}
            or not math.isfinite(cap)
            or not 1 <= cap <= 120
            or int(cap) != cap
        ):
            raise ValueError("Shell timeout preferences require whole seconds in 1..120")
        return ExecutionPreferences(
            file_writes="deny" if policy.deny_write else "allow" if policy.allow_write else "ask",
            shell_commands="deny"
            if policy.deny_shell
            else "allow"
            if policy.allow_shell
            else "ask",
            tool_network=policy.network,
            max_steps=self.runtime.max_steps,
            shell_timeout_s=int(cap),
            working_directory=policy.shell_working_directory,
        )

    def _read(self):
        revision, body = self.db.execute("SELECT revision,body FROM settings WHERE id=1").fetchone()
        if type(revision) is not int or not 0 <= revision <= 9_007_199_254_740_990:
            raise ValueError("Invalid execution settings revision")
        return revision, ExecutionPreferences.model_validate_json(body)

    def _state(self):
        policy = self.runtime.policy
        return _digest(
            {
                "instance": self.instance,
                "settings": self._current().model_dump(),
                "yolo": policy.yolo,
                "workspace": str(policy.workspace),
                "protected": [str(root) for root in policy.protected_roots],
                "readonly": [str(root) for root in policy.readonly_roots],
            }
        )

    def snapshot(self):
        revision, saved = self._read()
        current = self._current()
        return {
            "schema_version": "1.0",
            "revision": revision,
            "state_token": self._state(),
            "instance_id": self.instance,
            "source": "owner_saved" if revision else "host_configuration",
            "settings": current.model_dump(),
            "saved_settings": saved.model_dump(),
            "active_matches_saved": current == saved,
            "yolo": self.runtime.policy.yolo,
            "yolo_scope": "service_instance",
            "effective_write": self.runtime.policy.effective_write,
            "effective_shell": self.runtime.policy.effective_shell,
            "change_boundary": "settled_service",
            "busy": self.runtime.runtime_settings_busy(),
            "execution": execution_metadata(self.runtime),
        }

    def _check(self, request):
        revision, _ = self._read()
        if revision != request.expected_revision or self._state() != request.expected_state:
            raise RuntimeSettingsConflict("Execution settings changed; refresh before saving")
        if revision >= 9_007_199_254_740_990:
            raise ValueError("Execution settings revision space is exhausted")
        if self.runtime.runtime_settings_busy():
            raise RuntimeSettingsConflict(
                "Finish, stop or retire pending work before saving execution settings"
            )
        shell_directory(self.runtime.policy, request.settings.working_directory)

    def _apply(self, preferences, yolo):
        policy = self.runtime.policy
        policy.allow_write = preferences.file_writes == "allow"
        policy.deny_write = preferences.file_writes == "deny"
        policy.allow_shell = preferences.shell_commands == "allow"
        policy.deny_shell = preferences.shell_commands == "deny"
        policy.network = preferences.tool_network
        policy.yolo = yolo
        policy.shell_working_directory = preferences.working_directory
        policy.shell_timeout_s = preferences.shell_timeout_s
        self.runtime.max_steps = preferences.max_steps

    async def change(self, request):
        digest = _digest(request.model_dump())
        prior = self.db.execute(
            "SELECT digest,receipt FROM operations WHERE key=?", (request.idempotency_key,)
        ).fetchone()
        if prior:
            if prior[0] != digest:
                raise RuntimeSettingsConflict("Request key belongs to a different execution change")
            return json.loads(prior[1])
        self._check(request)
        controller = self.runtime.mcp_controller
        if controller is not None:
            await controller.retire_connections(actor="owner")
        elif self.runtime.mcp_host is not None:
            await self.runtime.mcp_host.reset_connections(actor="owner")
        # Retirement yields; recheck native policy and worker ownership before
        # committing. No await separates commit from the synchronous apply.
        self._check(request)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._check(request)
            if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 10000:
                raise ValueError("Execution receipt storage needs owner maintenance")
            receipt = {
                "schema_version": "1.0",
                "status": "settings_saved",
                "revision": request.expected_revision + 1,
                "idempotency_key": request.idempotency_key,
                "settings": request.settings.model_dump(),
                "requested_yolo": request.yolo,
                "service_instance": self.instance,
                "change_boundary": "settled_service",
            }
            self.db.execute(
                "UPDATE settings SET revision=?,body=? WHERE id=1",
                (receipt["revision"], request.settings.model_dump_json()),
            )
            self.db.execute(
                "INSERT INTO operations VALUES (?,?,?)",
                (request.idempotency_key, digest, json.dumps(receipt)),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self._apply(request.settings, request.yolo)
        return receipt

    def receipt(self, key):
        row = self.db.execute("SELECT receipt FROM operations WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
