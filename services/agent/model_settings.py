"""Protected owner defaults; each accepted task captures its own provider.

Only host-configured profiles are selectable. This store contains references
and reasoning labels, never endpoints, credentials, prompts or permissions.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.contracts.models import AgentModelPin, AuxiliaryTask

HOST_PROFILE = "host-default"
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
AUXILIARY_TASKS = {
    "analysis": "Analysis",
    "compression": "Context compression",
    "title_generation": "Title generation",
    "delegation": "Delegated reasoning",
    "harness_design": "Harness design",
}


class ModelSettingsConflict(ValueError):
    pass


class ModelSettingsUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Agent model settings outcome is unconfirmed")


class ModelSettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    expected_revision: int = Field(ge=0, le=9_007_199_254_740_990)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")
    harness_version: str | None = Field(default=None, pattern=r"^(seed-v1|v-[a-f0-9]{24})$")
    profile_id: str | None = Field(default=None, min_length=1, max_length=80)
    reasoning: str | None = Field(default=None, min_length=1, max_length=16)
    auxiliary_task: AuxiliaryTask | None = None


class BoundModelSettingsChange(ModelSettingsChange):
    schema_version: Literal["1.1"]
    expected_backend: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    expected_instance: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")


class AgentModelSettings:
    def __init__(
        self,
        provider,
        factories,
        *,
        path=None,
        reasoning_levels=None,
        host_factory=None,
        auxiliary_factories=None,
        auxiliary_host_factory=None,
    ):
        if (
            len(factories) > 31
            or HOST_PROFILE in factories
            or any(not self.valid_profile_id(key) for key in factories)
        ):
            raise ValueError("Agent profile names must be unique bounded labels")
        self.factories = dict(factories)
        self.host_factory = host_factory
        self.auxiliary_factories = dict(auxiliary_factories or {})
        self.auxiliary_host_factory = auxiliary_host_factory
        if set(self.auxiliary_factories) - set(factories):
            raise ValueError("Auxiliary factories refer to unknown Agent profiles")
        self.auxiliary_instances = {}
        self.auxiliary_failures = set()
        self.instances = {(HOST_PROFILE, None): provider}
        self.base = {HOST_PROFILE: provider}
        self.failures = set()
        self.levels = {}
        declarations = reasoning_levels or {}
        if set(declarations) - {HOST_PROFILE, *factories}:
            raise ValueError("Reasoning declaration refers to an unknown Agent profile")
        for name, factory in factories.items():
            try:
                self.base[name] = factory(None)
                self.instances[name, None] = self.base[name]
            except (Exception, SystemExit):
                # A broken optional profile must not hide the working default.
                self.failures.add(name)
        for name in [HOST_PROFILE, *factories]:
            levels = declarations.get(name, [])
            if (
                not isinstance(levels, list)
                or len(levels) > 6
                or any(type(level) is not str or level not in EFFORTS for level in levels)
                or len(set(levels)) != len(levels)
                or (levels and name == HOST_PROFILE and host_factory is None)
            ):
                raise ValueError("Invalid host reasoning options")
            self.levels[name] = tuple(levels)
        if path is not None:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path is not None else ":memory:")
        self._failed = False
        self._closed = False
        self.backend_id = None
        self.instance = secrets.token_hex(16)
        try:
            if path is not None:
                path.chmod(0o600)
            self.db.execute("PRAGMA synchronous=FULL")
            with self._transaction():
                self._initialize_authority()
        except BaseException:
            self._poison()
            raise

    def _ensure_store(self):
        if self._failed or self._closed:
            raise ModelSettingsUnavailable

    def _poison(self):
        self._failed = True
        try:
            self.db.close()
        except BaseException:
            # Even a failed close cannot make this connection usable again.
            pass

    def _rollback(self):
        try:
            self.db.rollback()
            if self.db.in_transaction:
                raise ModelSettingsUnavailable
        except BaseException:
            self._poison()
            raise ModelSettingsUnavailable from None

    @contextmanager
    def _transaction(self):
        self._ensure_store()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.commit()
        except BaseException:
            self._rollback()
            raise

    def _initialize_authority(self):
        marker = self.db.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        legacy = {"settings", "operations"}
        if marker == 0 and not tables:
            self.db.execute(
                "CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), "
                "revision INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                "receipt TEXT NOT NULL)"
            )
            self.db.execute(
                "INSERT INTO settings VALUES (1, 0, ?)",
                (
                    json.dumps(
                        {
                            "default": {"profile_id": HOST_PROFILE, "reasoning": None},
                            "harnesses": {},
                        }
                    ),
                ),
            )
            tables = legacy
        if (marker, tables) not in (
            (0, legacy),
            (1, legacy | {"model_write_authority"}),
        ):
            raise ModelSettingsUnavailable
        expected_columns = {
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
            expected_columns["model_write_authority"] = [
                ("id", "INTEGER", 0, 1),
                ("backend_id", "TEXT", 1, 0),
                ("active_instance", "TEXT", 1, 0),
            ]
        for table, columns in expected_columns.items():
            actual = [
                (row[1], row[2].upper(), row[3], row[5])
                for row in self.db.execute(f"PRAGMA table_info({table})")
            ]
            if actual != columns:
                raise ModelSettingsUnavailable
        self._read()
        if marker == 0:
            self.db.execute(
                "CREATE TABLE model_write_authority (id INTEGER PRIMARY KEY CHECK(id=1), "
                "backend_id TEXT NOT NULL, active_instance TEXT NOT NULL)"
            )
            self.db.execute(
                "INSERT INTO model_write_authority VALUES (1, ?, ?)",
                (secrets.token_hex(16), self.instance),
            )
            self.db.execute("PRAGMA user_version=1")
        else:
            self._authority()
            self.db.execute(
                "UPDATE model_write_authority SET active_instance=? WHERE id=1",
                (self.instance,),
            )
        backend, active = self._authority()
        if active != self.instance:
            raise ModelSettingsUnavailable
        self.backend_id = backend

    def _authority(self):
        self._ensure_store()
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ModelSettingsUnavailable
        rows = self.db.execute(
            "SELECT id, backend_id, active_instance FROM model_write_authority"
        ).fetchall()
        if (
            len(rows) != 1
            or type(rows[0][0]) is not int
            or rows[0][0] != 1
            or any(
                type(value) is not str or re.fullmatch(r"[a-f0-9]{32}", value) is None
                for value in rows[0][1:]
            )
            or (self.backend_id is not None and rows[0][1] != self.backend_id)
        ):
            raise ModelSettingsUnavailable
        return rows[0][1], rows[0][2]

    @staticmethod
    def valid_profile_id(value):
        return (
            type(value) is str
            and 1 <= len(value) <= 80
            and bool(value.strip())
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )

    @staticmethod
    def effort(provider):
        # ResourceBoundProvider forwards identity but intentionally not arbitrary
        # mutable attributes. Read its protected adapter, never mutate it.
        adapter = getattr(provider, "provider", provider)
        value = getattr(adapter, "reasoning_effort", getattr(adapter, "effort", None))
        return value if type(value) is str and value in EFFORTS else None

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

        if type(value) is not str:
            raise ValueError("Invalid stored JSON")
        return json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)

    def _read(self):
        self._ensure_store()
        try:
            if self.db.execute("SELECT count(*) FROM settings").fetchone()[0] != 1:
                raise ModelSettingsUnavailable
            return self._read_settings()
        except (ValueError, TypeError, IndexError, KeyError):
            raise ModelSettingsUnavailable from None

    def _read_settings(self):
        row = self.db.execute("SELECT revision, body FROM settings WHERE id=1").fetchone()
        revision, body = row[0], self._stored_json(row[1])
        if (
            type(revision) is not int
            or not 0 <= revision <= 9_007_199_254_740_990
            or not isinstance(body, dict)
            or set(body)
            not in (
                {"default", "harnesses"},
                {"default", "harnesses", "auxiliary", "harness_auxiliary"},
            )
            or not isinstance(body["harnesses"], dict)
            or len(body["harnesses"]) > 1000
        ):
            raise ValueError("Stored Agent model settings are invalid")
        body.setdefault("auxiliary", {})
        body.setdefault("harness_auxiliary", {})
        if (
            type(body["auxiliary"]) is not dict
            or set(body["auxiliary"]) - AUXILIARY_TASKS.keys()
            or type(body["harness_auxiliary"]) is not dict
            or len(body["harness_auxiliary"]) > 1000
            or any(
                type(routes) is not dict or set(routes) - AUXILIARY_TASKS.keys()
                for routes in body["harness_auxiliary"].values()
            )
        ):
            raise ValueError("Stored auxiliary model settings are invalid")
        bindings = [
            (None, body["default"]),
            *body["harnesses"].items(),
            *[(None, value) for value in body["auxiliary"].values()],
            *[
                (target, value)
                for target, routes in body["harness_auxiliary"].items()
                for value in routes.values()
            ],
        ]
        for target in body["harness_auxiliary"]:
            if re.fullmatch(r"seed-v1|v-[a-f0-9]{24}", target) is None:
                raise ValueError("Stored auxiliary harness identity is invalid")
        for target, binding in bindings:
            if target is not None and re.fullmatch(r"seed-v1|v-[a-f0-9]{24}", target) is None:
                raise ValueError("Stored harness model identity is invalid")
            if (
                not isinstance(binding, dict)
                or set(binding) != {"profile_id", "reasoning"}
                or not self.valid_profile_id(binding["profile_id"])
                or (
                    binding["reasoning"] is not None
                    and (
                        type(binding["reasoning"]) is not str or binding["reasoning"] not in EFFORTS
                    )
                )
            ):
                raise ValueError("Stored Agent model binding is invalid")
        return revision, body

    @staticmethod
    def _canonical_request(request):
        raw = request.model_dump() if isinstance(request, BaseModel) else request
        if type(raw) is not dict:
            raise ValueError("Invalid model settings change")
        if raw.get("schema_version") == "1.0":
            cls = ModelSettingsChange
        elif raw.get("schema_version") == "1.1":
            cls = BoundModelSettingsChange
        else:
            raise ValueError("Unsupported model settings change version")
        request = cls.model_validate(raw)
        payload = request.model_dump(
            exclude={"auxiliary_task"} if request.auxiliary_task is None else set()
        )
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return request, payload, digest

    @staticmethod
    def _change_receipt(request):
        return {
            "schema_version": request.schema_version,
            "idempotency_key": request.idempotency_key,
            "revision": request.expected_revision + 1,
            "harness_version": request.harness_version,
            "binding": {"profile_id": request.profile_id, "reasoning": request.reasoning}
            if request.profile_id is not None
            else None,
            "change_boundary": "new_tasks",
            **(
                {"auxiliary_task": request.auxiliary_task}
                if request.auxiliary_task is not None
                else {}
            ),
            **(
                {
                    "backend_id": request.expected_backend,
                    "service_instance": request.expected_instance,
                }
                if isinstance(request, BoundModelSettingsChange)
                else {}
            ),
        }

    def _operation(self, key):
        row = self.db.execute(
            "SELECT digest, receipt FROM operations WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = self._stored_json(row[1])
            if (
                type(row[0]) is not str
                or re.fullmatch(r"[a-f0-9]{64}", row[0]) is None
                or type(receipt) is not dict
                or type(receipt.get("revision")) is not int
                or not 1 <= receipt["revision"] <= 9_007_199_254_740_990
                or receipt.get("idempotency_key") != key
            ):
                raise ValueError("Invalid stored receipt")
            binding = receipt["binding"]
            if binding is not None and (
                type(binding) is not dict or set(binding) != {"profile_id", "reasoning"}
            ):
                raise ValueError("Invalid stored binding")
            raw = {
                "schema_version": receipt["schema_version"],
                "expected_revision": receipt["revision"] - 1,
                "idempotency_key": key,
                "harness_version": receipt["harness_version"],
                "profile_id": binding["profile_id"] if binding is not None else None,
                "reasoning": binding["reasoning"] if binding is not None else None,
                **(
                    {"auxiliary_task": receipt["auxiliary_task"]}
                    if "auxiliary_task" in receipt
                    else {}
                ),
            }
            if receipt["schema_version"] == "1.1":
                raw.update(
                    expected_backend=receipt["backend_id"],
                    expected_instance=receipt["service_instance"],
                )
                if receipt["backend_id"] != self.backend_id:
                    raise ValueError("Invalid stored backend")
            request, _, digest = self._canonical_request(raw)
            if (
                digest != row[0]
                or receipt != self._change_receipt(request)
                or (
                    binding is None
                    and request.harness_version is None
                    and request.auxiliary_task is None
                )
                or (
                    binding is not None
                    and (
                        not self.valid_profile_id(request.profile_id)
                        or (request.reasoning is not None and request.reasoning not in EFFORTS)
                    )
                )
            ):
                raise ValueError("Invalid stored receipt")
            return request, receipt, digest
        except (ValueError, TypeError, KeyError, IndexError):
            raise ModelSettingsUnavailable from None

    def resolve(self, binding):
        name, effort = binding["profile_id"], binding["reasoning"]
        if name not in self.base or (effort is not None and effort not in self.levels[name]):
            raise ValueError("Selected Agent model profile is unavailable; choose another profile")
        key = name, effort
        if key not in self.instances:
            factory = self.host_factory if name == HOST_PROFILE else self.factories[name]
            candidate = factory(effort)
            if candidate.info != self.base[name].info or self.effort(candidate) != effort:
                raise ValueError("Reasoning variant changed the configured provider identity")
            self.instances[key] = candidate
        return self.instances[key]

    def resolve_auxiliary(self, binding):
        name, effort = binding["profile_id"], binding["reasoning"]
        if name not in self.base or (effort is not None and effort not in self.levels[name]):
            raise ValueError("Selected auxiliary profile is unavailable")
        key = name, effort
        if key in self.auxiliary_failures:
            raise ValueError("Auxiliary profile capability is unavailable")
        if key not in self.auxiliary_instances:
            factory = (
                self.auxiliary_host_factory
                if name == HOST_PROFILE
                else self.auxiliary_factories.get(name)
            )
            try:
                primary = self.resolve(binding)
                candidate = factory(effort) if factory is not None else primary
                if factory is not None:
                    # Own optional adapter cleanup before any capability check.
                    self.instances[("auxiliary-owner", name, effort)] = candidate
            except (Exception, SystemExit):
                self.auxiliary_failures.add(key)
                raise ValueError("Auxiliary profile construction failed") from None
            adapter = getattr(candidate, "provider", candidate)
            if (
                candidate.info != self.base[name].info
                or self.effort(candidate) != self.effort(primary)
                or getattr(adapter, "structured_actions", False)
                or not callable(getattr(adapter, "prepare_complete", None))
            ):
                self.auxiliary_failures.add(key)
                raise ValueError("Profile cannot serve measured auxiliary text requests")
            self.auxiliary_instances[key] = candidate
        return self.auxiliary_instances[key]

    def selection(self, harness_version, auxiliary_task=None):
        revision, body = self._read()
        binding = body["harnesses"].get(harness_version, body["default"])
        if auxiliary_task is not None:
            if auxiliary_task not in AUXILIARY_TASKS:
                raise ValueError("Unknown auxiliary task")
            binding = (
                body["harness_auxiliary"]
                .get(harness_version, {})
                .get(auxiliary_task, body["auxiliary"].get(auxiliary_task, binding))
            )
        provider = (
            self.resolve(binding) if auxiliary_task is None else self.resolve_auxiliary(binding)
        )
        return provider, AgentModelPin(
            profile_id=binding["profile_id"],
            provider=provider.info.provider,
            model=provider.info.model,
            local=provider.info.local,
            reasoning=self.effort(provider),
            settings_revision=revision,
        )

    def auxiliary_selection(self, harness_version):
        providers, pins = {}, {}
        for purpose in AUXILIARY_TASKS:
            try:
                providers[purpose], pins[purpose] = self.selection(harness_version, purpose)
            except (ValueError, RuntimeError, SystemExit):
                # Unavailable auxiliary routes never silently fall back and do
                # not hide independently available primary Agent work.
                continue
        return providers, pins

    def auxiliary_available(self, name):
        try:
            self.resolve_auxiliary({"profile_id": name, "reasoning": None})
            return True
        except (Exception, SystemExit):
            return False

    def snapshot(self):
        revision, body = self._read()
        backend, active = self._authority()
        return {
            "schema_version": "1.0",
            "write_authority": {
                "mutation_version": "1.1",
                "backend_id": backend,
                "instance_id": self.instance,
                "writable": active == self.instance,
            },
            "revision": revision,
            "change_boundary": "new_tasks",
            "default": body["default"],
            "harnesses": body["harnesses"],
            "auxiliary": body["auxiliary"],
            "harness_auxiliary": body["harness_auxiliary"],
            "auxiliary_tasks": [
                {"id": key, "label": label} for key, label in AUXILIARY_TASKS.items()
            ],
            "profiles": [
                {
                    "id": name,
                    "selectable": name in self.base,
                    "provider": self.base[name].info.model_dump() if name in self.base else None,
                    "reasoning": self.effort(self.base[name]) if name in self.base else None,
                    "reasoning_options": list(self.levels[name]),
                    "auxiliary_selectable": name in self.base and self.auxiliary_available(name),
                }
                for name in [HOST_PROFILE, *sorted(self.factories)]
            ],
        }

    def change(self, request, *, validate_harness=None):
        return self.change_with_status(request, validate_harness=validate_harness)[0]

    def change_with_status(self, request, *, validate_harness=None):
        request, _, digest = self._canonical_request(request)
        with self._transaction():
            backend, active = self._authority()
            prior = self._operation(request.idempotency_key)
            if prior is not None:
                if prior[2] != digest:
                    raise ModelSettingsConflict(
                        "The request key belongs to a different model change"
                    )
                revision, _ = self._read()
                if prior[1]["revision"] > revision:
                    raise ModelSettingsUnavailable
                return prior[1], False
            if (
                not isinstance(request, BoundModelSettingsChange)
                or request.expected_backend != backend
                or request.expected_instance != self.instance
                or request.expected_instance != active
            ):
                raise ModelSettingsConflict(
                    "Agent model write authority changed; refresh before saving"
                )
            revision, body = self._read()
            if revision != request.expected_revision:
                raise ModelSettingsConflict("Agent model settings changed; refresh before saving")
            if revision >= 9_007_199_254_740_990:
                raise ValueError("Agent model settings revision space is exhausted")
            if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 10000:
                raise ValueError("Model settings receipt storage needs owner maintenance")
            target = request.harness_version
            if target is not None and validate_harness is not None:
                validate_harness(target)
            purpose = request.auxiliary_task
            if purpose is not None:
                if target is None:
                    routes = body["auxiliary"]
                else:
                    if (
                        target not in body["harness_auxiliary"]
                        and len(body["harness_auxiliary"]) >= 1000
                    ):
                        raise ValueError("Too many harness auxiliary overrides")
                    routes = body["harness_auxiliary"].setdefault(target, {})
                if request.profile_id is None:
                    if request.reasoning is not None:
                        raise ValueError("Inherited auxiliary choice cannot override reasoning")
                    routes.pop(purpose, None)
                    binding = None
                else:
                    binding = {"profile_id": request.profile_id, "reasoning": request.reasoning}
                    self.resolve_auxiliary(binding)
                    routes[purpose] = binding
                if target is not None and not routes:
                    body["harness_auxiliary"].pop(target, None)
            elif request.profile_id is None:
                if target is None or request.reasoning is not None:
                    raise ValueError("Only a harness can inherit the Agent default")
                body["harnesses"].pop(target, None)
                binding = None
            else:
                binding = {"profile_id": request.profile_id, "reasoning": request.reasoning}
                self.resolve(binding)
                if target is None:
                    body["default"] = binding
                else:
                    if target not in body["harnesses"] and len(body["harnesses"]) >= 1000:
                        raise ValueError("Too many harness model overrides")
                    body["harnesses"][target] = binding
            receipt = self._change_receipt(request)
            self.db.execute(
                "UPDATE settings SET revision=?, body=? WHERE id=1",
                (revision + 1, json.dumps(body)),
            )
            self.db.execute(
                "INSERT INTO operations VALUES (?, ?, ?)",
                (request.idempotency_key, digest, json.dumps(receipt)),
            )
            return receipt, True

    def lookup(self, request):
        request, payload, digest = self._canonical_request(request)
        if not isinstance(request, BoundModelSettingsChange):
            raise ValueError("Model change lookup requires a bound request")
        with self._transaction():
            backend, active = self._authority()
            if request.expected_backend != backend:
                raise ModelSettingsConflict("Model change belongs to another backend")
            revision, _ = self._read()
            prior = self._operation(request.idempotency_key)
            if prior is not None and prior[1]["revision"] > revision:
                raise ModelSettingsUnavailable
            if prior is not None and prior[2] != digest:
                raise ModelSettingsConflict("The request key belongs to a different model change")
            return {
                "schema_version": "1.1",
                "backend_id": backend,
                "request": payload,
                "status": "recorded" if prior is not None else "unobserved",
                "receipt": prior[1] if prior is not None else None,
                "mutation_fenced": prior is not None
                or active != request.expected_instance
                or revision > request.expected_revision,
            }

    def recorded_change(self, key):
        with self._transaction():
            self._authority()
            revision, _ = self._read()
            prior = self._operation(key)
            if prior is not None and prior[1]["revision"] > revision:
                raise ModelSettingsUnavailable
            return prior[:2] if prior is not None else None

    def receipt(self, key):
        prior = self.recorded_change(key)
        return prior[1] if prior is not None else None

    def default_info(self):
        try:
            return self.selection(None)[0].info.model_dump()
        except ValueError:
            return None

    async def close(self):
        self._closed = True
        try:
            for instance in {
                id(getattr(value, "provider", value)): getattr(value, "provider", value)
                for value in [*self.instances.values(), *self.auxiliary_instances.values()]
            }.values():
                close = getattr(instance, "close", None)
                if close is not None:
                    await close()
        finally:
            self.db.close()
