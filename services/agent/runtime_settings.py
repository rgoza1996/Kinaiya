"""Protected owner execution preferences; no model, capture or workspace relocation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.policy import PolicyDenied
from services.agent.tools import bwrap_path, shell_directory

_UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
_MAX_REVISION = 9_007_199_254_740_990


class RuntimeSettingsConflict(ValueError):
    pass


class RuntimeSettingsUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Execution settings outcome is unconfirmed")


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


class BoundRuntimeSettingsChange(RuntimeSettingsChange):
    schema_version: Literal["1.1"]
    expected_backend: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    expected_instance: str = Field(min_length=36, max_length=36, pattern=f"^{_UUID}$")


class _RuntimeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    status: Literal["settings_saved"]
    revision: int = Field(ge=1, le=_MAX_REVISION)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")
    settings: ExecutionPreferences
    requested_yolo: bool
    service_instance: str = Field(min_length=36, max_length=36, pattern=f"^{_UUID}$")
    change_boundary: Literal["settled_service"]


class _BoundRuntimeReceipt(_RuntimeReceipt):
    schema_version: Literal["1.1"]
    backend_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")


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
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise RuntimeSettingsUnavailable from None
        self._failed = False
        self._closed = False
        self.backend_id = None
        try:
            self.db = self._storage(
                lambda: sqlite3.connect(str(path) if path is not None else ":memory:")
            )
            if path is not None:
                self._storage(lambda: path.chmod(0o600))
            self._execute("PRAGMA synchronous=FULL")
            with self._transaction():
                self._initialize_authority()
            # Observe the committed identity before exposing a usable controller.
            with self._transaction():
                _, active = self._authority()
                if active != self.instance:
                    raise RuntimeSettingsUnavailable
                revision, saved = self._read()
            if revision:
                # A removed directory remains unavailable; never substitute it.
                # YOLO still comes from this service's startup configuration.
                self._apply(saved, runtime.policy.yolo)
        except BaseException:
            self._poison()
            raise

    def _ensure_store(self):
        if self._failed or self._closed:
            raise RuntimeSettingsUnavailable

    def _storage(self, invoke):
        self._ensure_store()
        try:
            return invoke()
        except Exception:
            # Adapters can raise non-SQL exceptions after actual DML or commit.
            raise RuntimeSettingsUnavailable from None

    def _execute(self, sql, parameters=()):
        return self._storage(lambda: self.db.execute(sql, parameters))

    def _one(self, sql, parameters=()):
        return self._storage(lambda: self.db.execute(sql, parameters).fetchone())

    def _all(self, sql, parameters=()):
        return self._storage(lambda: self.db.execute(sql, parameters).fetchall())

    def _poison(self):
        self._failed = True
        try:
            self.db.close()
        except BaseException:
            # This handle stays unusable even if cleanup itself fails.
            pass

    def _rollback(self):
        try:
            self.db.rollback()
            if self.db.in_transaction:
                raise RuntimeSettingsUnavailable
        except BaseException:
            self._poison()
            raise RuntimeSettingsUnavailable from None

    @contextmanager
    def _transaction(self):
        self._ensure_store()
        if self._storage(lambda: self.db.in_transaction):
            self._poison()
            raise RuntimeSettingsUnavailable
        try:
            self._execute("BEGIN IMMEDIATE")
            yield
            self._storage(lambda: self.db.commit())
            if self._storage(lambda: self.db.in_transaction):
                raise RuntimeSettingsUnavailable
        except BaseException:
            self._rollback()
            raise

    def _initialize_authority(self):
        marker = self._one("PRAGMA user_version")[0]
        objects = dict(
            self._all("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        )
        legacy = {"settings": "table", "operations": "table"}
        if marker == 0 and not objects:
            self._execute(
                "CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), "
                "revision INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            self._execute(
                "CREATE TABLE operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                "receipt TEXT NOT NULL)"
            )
            self._execute(
                "INSERT INTO settings VALUES (1,0,?)", (self._current().model_dump_json(),)
            )
            objects = legacy
        if (marker, objects) not in (
            (0, legacy),
            (1, {**legacy, "runtime_write_authority": "table"}),
        ):
            raise RuntimeSettingsUnavailable
        columns = {
            "settings": [
                ("id", "INTEGER", 0, 1),
                ("revision", "INTEGER", 1, 0),
                ("body", "TEXT", 1, 0),
            ],
            "operations": [
                ("key", "TEXT", 0, 1),
                ("digest", "TEXT", 1, 0),
                ("receipt", "TEXT", 1, 0),
            ],
        }
        if marker == 1:
            columns["runtime_write_authority"] = [
                ("id", "INTEGER", 0, 1),
                ("backend_id", "TEXT", 1, 0),
                ("active_instance", "TEXT", 1, 0),
            ]
        for table, expected in columns.items():
            actual = self._all(f"PRAGMA table_info({table})")
            if [(row[1], row[2].upper(), row[3], row[5]) for row in actual] != expected or any(
                row[4] is not None for row in actual
            ):
                raise RuntimeSettingsUnavailable
        revision, _ = self._read()
        if marker == 0:
            self._execute(
                "CREATE TABLE runtime_write_authority (id INTEGER PRIMARY KEY CHECK(id=1), "
                "backend_id TEXT NOT NULL, active_instance TEXT NOT NULL)"
            )
            self._execute(
                "INSERT INTO runtime_write_authority VALUES (1,?,?)",
                (secrets.token_hex(16), self.instance),
            )
            self._execute("PRAGMA user_version=1")
        else:
            self._authority()
            self._execute(
                "UPDATE runtime_write_authority SET active_instance=? WHERE id=1", (self.instance,)
            )
        backend, active = self._authority()
        if active != self.instance:
            raise RuntimeSettingsUnavailable
        self.backend_id = backend
        if revision == 0:
            # Until the first owner save, startup configuration remains source.
            self._execute(
                "UPDATE settings SET body=? WHERE id=1", (self._current().model_dump_json(),)
            )

    def _authority(self):
        marker = self._one("PRAGMA user_version")[0]
        rows = self._all("SELECT id,backend_id,active_instance FROM runtime_write_authority")
        if (
            marker != 1
            or len(rows) != 1
            or type(rows[0][0]) is not int
            or rows[0][0] != 1
            or type(rows[0][1]) is not str
            or re.fullmatch(r"[a-f0-9]{32}", rows[0][1]) is None
            or type(rows[0][2]) is not str
            or re.fullmatch(_UUID, rows[0][2]) is None
            or (self.backend_id is not None and rows[0][1] != self.backend_id)
        ):
            raise RuntimeSettingsUnavailable
        return rows[0][1], rows[0][2]

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
        try:
            if self._one("SELECT count(*) FROM settings")[0] != 1:
                raise RuntimeSettingsUnavailable
            revision, raw = self._one("SELECT revision,body FROM settings WHERE id=1")
            body = self._stored_json(raw)
            preferences = ExecutionPreferences.model_validate(body)
            if (
                type(revision) is not int
                or not 0 <= revision <= _MAX_REVISION
                or preferences.model_dump() != body
            ):
                raise ValueError("Invalid stored execution settings")
            return revision, preferences
        except (ValueError, TypeError, KeyError, IndexError):
            raise RuntimeSettingsUnavailable from None

    @staticmethod
    def _stored_json(value):
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("Duplicate stored field")
                result[key] = item
            return result

        def invalid_constant(value):
            raise ValueError("Invalid stored number")

        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Invalid stored number")
            return number

        if type(value) is not str:
            raise ValueError("Invalid stored JSON")
        value.encode("utf-8")
        depth, quoted, escaped = 0, False, False
        for char in value:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "{[":
                depth += 1
                if depth > 16:
                    raise ValueError("Stored JSON nesting exceeds its bound")
            elif char in "}]":
                depth -= 1
                if depth < 0:
                    raise ValueError("Invalid stored JSON nesting")
        try:
            result = json.loads(
                value,
                object_pairs_hook=unique,
                parse_constant=invalid_constant,
                parse_float=finite_float,
            )
            pending = [result]
            while pending:
                item = pending.pop()
                if type(item) is str:
                    item.encode("utf-8")
                elif type(item) is dict:
                    pending.extend(item.keys())
                    pending.extend(item.values())
                elif type(item) is list:
                    pending.extend(item)
            return result
        except RecursionError:
            raise ValueError("Stored JSON decoding exceeded its bound") from None

    @staticmethod
    def _canonical_request(request):
        raw = request.model_dump() if isinstance(request, BaseModel) else request
        if type(raw) is not dict:
            raise ValueError("Invalid execution settings change")
        if raw.get("schema_version") == "1.0":
            cls = RuntimeSettingsChange
        elif raw.get("schema_version") == "1.1":
            cls = BoundRuntimeSettingsChange
        else:
            raise ValueError("Unsupported execution settings change version")
        request = cls.model_validate(raw)
        payload = request.model_dump()
        return request, payload, _digest(payload)

    @staticmethod
    def _change_receipt(request, instance):
        return {
            "schema_version": request.schema_version,
            "status": "settings_saved",
            "revision": request.expected_revision + 1,
            "idempotency_key": request.idempotency_key,
            "settings": request.settings.model_dump(),
            "requested_yolo": request.yolo,
            "service_instance": instance,
            "change_boundary": "settled_service",
            **(
                {"backend_id": request.expected_backend}
                if isinstance(request, BoundRuntimeSettingsChange)
                else {}
            ),
        }

    def _operation(self, key, revision):
        row = self._one("SELECT digest,receipt FROM operations WHERE key=?", (key,))
        if row is None:
            return None
        try:
            raw = self._stored_json(row[1])
            cls = _BoundRuntimeReceipt if raw["schema_version"] == "1.1" else _RuntimeReceipt
            receipt = cls.model_validate(raw)
            if (
                type(row[0]) is not str
                or re.fullmatch(r"[a-f0-9]{64}", row[0]) is None
                or receipt.idempotency_key != key
                or receipt.revision > revision
                or receipt.model_dump() != raw
                or (
                    isinstance(receipt, _BoundRuntimeReceipt)
                    and receipt.backend_id != self.backend_id
                )
            ):
                raise ValueError("Invalid stored execution receipt")
            return row[0], raw
        except (ValueError, TypeError, KeyError, IndexError):
            raise RuntimeSettingsUnavailable from None

    def _proof(self, request, digest, revision):
        prior = self._operation(request.idempotency_key, revision)
        if prior is None:
            return None
        if prior[0] != digest:
            raise RuntimeSettingsConflict("Request key belongs to a different execution change")
        receipt = prior[1]
        if receipt != self._change_receipt(request, receipt["service_instance"]) or (
            isinstance(request, BoundRuntimeSettingsChange)
            and receipt["service_instance"] != request.expected_instance
        ):
            raise RuntimeSettingsUnavailable
        # Legacy expected_state is opaque: never reconstruct its original inputs.
        return receipt

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
        with self._transaction():
            backend, active = self._authority()
            revision, saved = self._read()
        current = self._current()
        return {
            "schema_version": "1.0",
            "write_authority": {
                "mutation_version": "1.1",
                "backend_id": backend,
                "instance_id": self.instance,
                "writable": active == self.instance,
            },
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
        backend, active = self._authority()
        if active != self.instance or (
            isinstance(request, BoundRuntimeSettingsChange)
            and (request.expected_backend != backend or request.expected_instance != self.instance)
        ):
            raise RuntimeSettingsConflict(
                "Execution write authority changed; refresh before saving"
            )
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

    def _check_or_replay(self, request, digest):
        with self._transaction():
            self._authority()
            revision, _ = self._read()
            prior = self._proof(request, digest, revision)
            if prior is None:
                self._check(request)
            return prior

    async def change(self, request):
        request, _, digest = self._canonical_request(request)
        prior = self._check_or_replay(request, digest)
        if prior is not None:
            return prior
        controller = self.runtime.mcp_controller
        try:
            if controller is not None:
                await controller.retire_connections(actor="owner")
            elif self.runtime.mcp_host is not None:
                await self.runtime.mcp_host.reset_connections(actor="owner")
        except Exception:
            raise RuntimeSettingsUnavailable from None
        # Keep the post-retirement observation and the final transaction check.
        # Each reconciles an exact concurrent receipt before current eligibility.
        prior = self._check_or_replay(request, digest)
        if prior is not None:
            return prior
        # No await separates this final check, commit and native apply.
        with self._transaction():
            self._authority()
            revision, _ = self._read()
            prior = self._proof(request, digest, revision)
            if prior is not None:
                return prior
            self._check(request)
            if self._one("SELECT count(*) FROM operations")[0] >= 10000:
                raise ValueError("Execution receipt storage needs owner maintenance")
            receipt = self._change_receipt(request, self.instance)
            self._execute(
                "UPDATE settings SET revision=?,body=? WHERE id=1",
                (receipt["revision"], request.settings.model_dump_json()),
            )
            self._execute(
                "INSERT INTO operations VALUES (?,?,?)",
                (request.idempotency_key, digest, json.dumps(receipt)),
            )
        self._apply(request.settings, request.yolo)
        return receipt

    def lookup(self, request):
        request, payload, digest = self._canonical_request(request)
        with self._transaction():
            backend, active = self._authority()
            bound = isinstance(request, BoundRuntimeSettingsChange)
            if bound and request.expected_backend != backend:
                raise RuntimeSettingsConflict("Execution change belongs to another backend")
            revision, _ = self._read()
            receipt = self._proof(request, digest, revision)
            return {
                "schema_version": "1.1",
                "backend_id": backend,
                "request": payload,
                "status": "recorded" if receipt is not None else "unobserved",
                "receipt": receipt,
                "mutation_fenced": receipt is not None
                or (
                    bound
                    and (
                        active != request.expected_instance or revision > request.expected_revision
                    )
                ),
            }

    def receipt(self, key):
        with self._transaction():
            self._authority()
            revision, _ = self._read()
            row = self._operation(key, revision)
            return row[1] if row is not None else None

    def close(self):
        if not self._closed:
            self._closed = True
            self._poison()
