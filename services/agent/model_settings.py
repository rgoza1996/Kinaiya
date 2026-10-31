"""Protected owner defaults; each accepted task captures its own provider.

Only host-configured profiles are selectable. This store contains references
and reasoning labels, never endpoints, credentials, prompts or permissions.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from packages.contracts.models import AgentModelPin

HOST_PROFILE = "host-default"
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


class ModelSettingsConflict(ValueError):
    pass


class ModelSettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    expected_revision: int = Field(ge=0, le=9_007_199_254_740_990)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")
    harness_version: str | None = Field(default=None, pattern=r"^(seed-v1|v-[a-f0-9]{24})$")
    profile_id: str | None = Field(default=None, min_length=1, max_length=80)
    reasoning: str | None = Field(default=None, min_length=1, max_length=16)


class AgentModelSettings:
    def __init__(self, provider, factories, *, path=None, reasoning_levels=None, host_factory=None):
        if (
            len(factories) > 31
            or HOST_PROFILE in factories
            or any(not self.valid_profile_id(key) for key in factories)
        ):
            raise ValueError("Agent profile names must be unique bounded labels")
        self.factories = dict(factories)
        self.host_factory = host_factory
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
                "INSERT OR IGNORE INTO settings VALUES (1, 0, ?)",
                (
                    json.dumps(
                        {
                            "default": {"profile_id": HOST_PROFILE, "reasoning": None},
                            "harnesses": {},
                        }
                    ),
                ),
            )
        self._read()

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

    def _read(self):
        row = self.db.execute("SELECT revision, body FROM settings WHERE id=1").fetchone()
        revision, body = row[0], json.loads(row[1])
        if (
            type(revision) is not int
            or not 0 <= revision <= 9_007_199_254_740_990
            or not isinstance(body, dict)
            or set(body) != {"default", "harnesses"}
            or not isinstance(body["harnesses"], dict)
            or len(body["harnesses"]) > 1000
        ):
            raise ValueError("Stored Agent model settings are invalid")
        for target, binding in [(None, body["default"]), *body["harnesses"].items()]:
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

    def selection(self, harness_version):
        revision, body = self._read()
        binding = body["harnesses"].get(harness_version, body["default"])
        provider = self.resolve(binding)
        return provider, AgentModelPin(
            profile_id=binding["profile_id"],
            provider=provider.info.provider,
            model=provider.info.model,
            local=provider.info.local,
            reasoning=self.effort(provider),
            settings_revision=revision,
        )

    def snapshot(self):
        revision, body = self._read()
        return {
            "schema_version": "1.0",
            "revision": revision,
            "change_boundary": "new_tasks",
            "default": body["default"],
            "harnesses": body["harnesses"],
            "profiles": [
                {
                    "id": name,
                    "selectable": name in self.base,
                    "provider": self.base[name].info.model_dump() if name in self.base else None,
                    "reasoning": self.effort(self.base[name]) if name in self.base else None,
                    "reasoning_options": list(self.levels[name]),
                }
                for name in [HOST_PROFILE, *sorted(self.factories)]
            ],
        }

    def change(self, request: ModelSettingsChange):
        payload = request.model_dump()
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        # A single transaction binds the state and receipt. Lost responses can
        # be retried with the same key/body without applying another change.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute(
                "SELECT digest, receipt FROM operations WHERE key=?", (request.idempotency_key,)
            ).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ModelSettingsConflict(
                        "The request key belongs to a different model change"
                    )
                self.db.rollback()
                return json.loads(prior[1])
            revision, body = self._read()
            if revision != request.expected_revision:
                raise ModelSettingsConflict("Agent model settings changed; refresh before saving")
            if revision >= 9_007_199_254_740_990:
                raise ValueError("Agent model settings revision space is exhausted")
            if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 10000:
                raise ValueError("Model settings receipt storage needs owner maintenance")
            target = request.harness_version
            if request.profile_id is None:
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
            receipt = {
                "schema_version": "1.0",
                "idempotency_key": request.idempotency_key,
                "revision": revision + 1,
                "harness_version": target,
                "binding": binding,
                "change_boundary": "new_tasks",
            }
            self.db.execute(
                "UPDATE settings SET revision=?, body=? WHERE id=1",
                (revision + 1, json.dumps(body)),
            )
            self.db.execute(
                "INSERT INTO operations VALUES (?, ?, ?)",
                (request.idempotency_key, digest, json.dumps(receipt)),
            )
            self.db.commit()
            return receipt
        except BaseException:
            self.db.rollback()
            raise

    def receipt(self, key):
        row = self.db.execute("SELECT receipt FROM operations WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def default_info(self):
        try:
            return self.selection(None)[0].info.model_dump()
        except ValueError:
            return None

    async def close(self):
        try:
            for instance in {id(value): value for value in self.instances.values()}.values():
                instance = getattr(instance, "provider", instance)
                close = getattr(instance, "close", None)
                if close is not None:
                    await close()
        finally:
            self.db.close()
