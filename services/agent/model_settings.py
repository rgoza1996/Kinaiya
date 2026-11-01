"""Protected owner defaults; each accepted task captures its own provider.

Host profiles and exact native catalog generations are selectable. This store
contains references and reasoning, never endpoints, keys, prompts or permissions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.policy import PolicyDenied
from packages.contracts.models import AgentModelPin, AuxiliaryTask, CatalogModelBinding
from packages.providers.disclosure import scope_allowed
from services.settings.provider_factory import (
    UnavailableHostProvider,
    declaration_info,
    inspect_recipe,
    open_recipe,
)

HOST_PROFILE = "host-default"
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
MAX_SETTINGS_BYTES = 512 * 1024
AUXILIARY_TASKS = {
    "analysis": "Analysis",
    "compression": "Context compression",
    "title_generation": "Title generation",
    "delegation": "Delegated reasoning",
    "harness_design": "Harness design",
}


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
    auxiliary_task: AuxiliaryTask | None = None
    catalog_binding: CatalogModelBinding | None = None

    @model_validator(mode="after")
    def distinct_sources(self):
        if self.profile_id is not None and self.catalog_binding is not None:
            raise ValueError("Choose a host profile or an exact catalog generation")
        return self


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
        catalog=None,
        resource_state=None,
    ):
        if (
            len(factories) > 31
            or HOST_PROFILE in factories
            or any(not self.valid_profile_id(key) for key in factories)
        ):
            raise ValueError("Agent profile names must be unique bounded labels")
        self.factories = dict(factories)
        self.catalog = catalog
        self.resource_state = resource_state
        self.catalog_leases = set()
        self.host_factory = host_factory
        self.auxiliary_factories = dict(auxiliary_factories or {})
        self.auxiliary_host_factory = auxiliary_host_factory
        if set(self.auxiliary_factories) - set(factories):
            raise ValueError("Auxiliary factories refer to unknown Agent profiles")
        self.auxiliary_instances = {}
        self.auxiliary_failures = set()
        self.instances = {(HOST_PROFILE, None): provider}
        self.base = (
            {} if isinstance(provider, UnavailableHostProvider) else {HOST_PROFILE: provider}
        )
        self.failures = {HOST_PROFILE} if isinstance(provider, UnavailableHostProvider) else set()
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
            if not isinstance(binding, dict) or set(binding) not in (
                {"profile_id", "reasoning"},
                {"catalog_binding", "reasoning"},
            ):
                raise ValueError("Stored Agent model binding is invalid")
            if "catalog_binding" in binding:
                CatalogModelBinding.model_validate(binding["catalog_binding"])
            elif not self.valid_profile_id(binding["profile_id"]):
                raise ValueError("Stored Agent model binding is invalid")
            if "reasoning" not in binding or (
                binding["reasoning"] is not None
                and (type(binding["reasoning"]) is not str or binding["reasoning"] not in EFFORTS)
            ):
                raise ValueError("Stored Agent model binding is invalid")
        return revision, body

    def resolve(self, binding):
        if "catalog_binding" in binding:
            raise ValueError("Catalog providers require a task-owned model lease")
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
        if "catalog_binding" in binding:
            raise ValueError("Catalog auxiliary providers require a task-owned model lease")
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

    @staticmethod
    def _binding(body, harness_version, auxiliary_task=None):
        binding = body["harnesses"].get(harness_version, body["default"])
        if auxiliary_task is not None:
            if auxiliary_task not in AUXILIARY_TASKS:
                raise ValueError("Unknown auxiliary task")
            binding = (
                body["harness_auxiliary"]
                .get(harness_version, {})
                .get(auxiliary_task, body["auxiliary"].get(auxiliary_task, binding))
            )
        return binding

    def selection(self, harness_version, auxiliary_task=None):
        revision, body = self._read()
        binding = self._binding(body, harness_version, auxiliary_task)
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

    def selection_info(self, harness_version, auxiliary_task=None):
        """Inspect declarations without constructing any adapter or key."""
        revision, body = self._read()
        binding = self._binding(body, harness_version, auxiliary_task)
        if "catalog_binding" not in binding:
            name, effort = binding["profile_id"], binding["reasoning"]
            if name not in self.base or (effort is not None and effort not in self.levels[name]):
                raise ValueError("Selected Agent model profile is unavailable")
            info = self.base[name].info
            return info, AgentModelPin(
                profile_id=name,
                provider=info.provider,
                model=info.model,
                local=info.local,
                reasoning=self.effort(self.base[name]) if effort is None else effort,
                settings_revision=revision,
            )
        recipe = inspect_recipe(self.catalog, binding["catalog_binding"], binding["reasoning"])
        info = recipe.info
        return info, AgentModelPin(
            profile_id=recipe.binding.id,
            provider=info.provider,
            model=info.model,
            local=info.local,
            reasoning=recipe.reasoning,
            settings_revision=revision,
            catalog_binding=recipe.binding,
            catalog_revision=recipe.catalog_revision,
        )

    @property
    def catalog_cleanup_busy(self):
        return any(lease.cleanup is not None and not lease.settled for lease in self.catalog_leases)

    def task_selection(self, harness_version, content_scope):
        """Capture fresh catalog adapters only for genuinely new accepted work."""
        from services.agent.task_model_lease import TaskModelLease

        if self.catalog_cleanup_busy:
            # An uncertain native operation can use the same backend as a
            # host profile. Changing selection cannot release its ownership.
            raise ValueError("Catalog provider cleanup is unconfirmed; no new work accepted")
        revision, body = self._read()
        lease = None

        def select(purpose=None):
            nonlocal lease
            binding = self._binding(body, harness_version, purpose)
            if "catalog_binding" not in binding:
                provider = (
                    self.resolve(binding) if purpose is None else self.resolve_auxiliary(binding)
                )
                pin = AgentModelPin(
                    profile_id=binding["profile_id"],
                    provider=provider.info.provider,
                    model=provider.info.model,
                    local=provider.info.local,
                    reasoning=self.effort(provider),
                    settings_revision=revision,
                )
                return provider, pin
            recipe = inspect_recipe(self.catalog, binding["catalog_binding"], binding["reasoning"])
            if not scope_allowed(recipe.info, content_scope):
                if purpose is None:
                    raise PolicyDenied("Input disclosure does not permit the catalog model")
                raise ValueError("Input disclosure does not permit the catalog model")
            if lease is None:
                if len(self.catalog_leases) >= 64 or any(
                    item.failed for item in self.catalog_leases
                ):
                    raise ValueError("Catalog model ownership requires cleanup or capacity")
                lease = TaskModelLease(self.catalog_leases.discard)
                self.catalog_leases.add(lease)
            provider, recipe = open_recipe(
                self.catalog,
                binding["catalog_binding"],
                binding["reasoning"],
                auxiliary=purpose is not None,
                resource_state=self.resource_state,
            )
            lease.own(provider)
            info = recipe.info
            if provider.info != info or self.effort(provider) != recipe.reasoning:
                raise ValueError("Catalog adapter differs from its captured recipe")
            return provider, AgentModelPin(
                profile_id=recipe.binding.id,
                provider=info.provider,
                model=info.model,
                local=info.local,
                reasoning=recipe.reasoning,
                settings_revision=revision,
                catalog_binding=recipe.binding,
                catalog_revision=recipe.catalog_revision,
            )

        try:
            provider, pin = select()
            if not scope_allowed(provider.info, content_scope):
                raise PolicyDenied("Input disclosure does not permit the selected model")
            auxiliary, pins = {}, {}
            for purpose in AUXILIARY_TASKS:
                try:
                    auxiliary[purpose], pins[purpose] = select(purpose)
                except (ValueError, RuntimeError, OSError, sqlite3.Error, SystemExit):
                    # An unavailable purpose stays unavailable; no replacement.
                    continue
            return provider, pin, auxiliary, pins, lease
        except BaseException:
            if lease is not None:
                lease.start_close()
            raise

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
        """Configured native interface only; never construct or probe on reads."""
        return self._host_auxiliary_declared({"profile_id": name, "reasoning": None})

    def _host_auxiliary_declared(self, binding):
        name, effort = binding["profile_id"], binding["reasoning"]
        if (
            name not in self.base
            or (effort is not None and effort not in self.levels[name])
            or (name, effort) in self.auxiliary_failures
        ):
            return False
        cached = self.auxiliary_instances.get((name, effort))
        provider = self.base[name] if cached is None else cached
        adapter = getattr(provider, "provider", provider)
        factory = (
            self.auxiliary_host_factory
            if name == HOST_PROFILE
            else self.auxiliary_factories.get(name)
        )
        return callable(getattr(adapter, "prepare_complete", None)) and (
            not getattr(adapter, "structured_actions", False)
            or (cached is None and factory is not None)
        )

    def auxiliary_configured(self, harness_version):
        """Effective declared routes, not model readiness or task qualification."""
        try:
            _, body = self._read()
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            return False
        for purpose in AUXILIARY_TASKS:
            binding = self._binding(body, harness_version, purpose)
            if "catalog_binding" not in binding:
                if self._host_auxiliary_declared(binding):
                    return True
                continue
            try:
                inspect_recipe(self.catalog, binding["catalog_binding"], binding["reasoning"])
                return True
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                continue
        return False

    def snapshot(self):
        revision, body = self._read()
        catalog_error = None
        try:
            catalog_profiles = self._catalog_profiles()
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            catalog_profiles = []
            catalog_error = "Provider catalog is unavailable; host profiles remain independent"
        return {
            "schema_version": "1.0",
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
                    "qualification": "not_checked",
                    "readiness": "unknown",
                }
                for name in [HOST_PROFILE, *sorted(self.factories)]
            ],
            "catalog_profiles": catalog_profiles,
            "catalog_error": catalog_error,
            "limits": {"settings_bytes": MAX_SETTINGS_BYTES},
            "catalog_model_ownership": {
                "leases": len(self.catalog_leases),
                "limit": 64,
                "cleanup_pending": sum(
                    item.cleanup is not None and not item.settled and not item.failed
                    for item in self.catalog_leases
                ),
                "cleanup_failed": sum(item.failed for item in self.catalog_leases),
            },
        }

    def _catalog_profiles(self):
        if self.catalog is None:
            return []
        profiles = self.catalog.snapshot()["profiles"]
        items = []
        for profile in profiles:
            reference = {key: profile[key] for key in ("id", "generation", "configuration_sha256")}
            config = profile["config"]
            selectable = False
            reason = "Catalog generation is unavailable for new selection"
            if profile["removed"]:
                reason = "Catalog generation is retired for new tasks"
            elif not profile["credential_available"]:
                reason = "Credential generation is retired or unavailable"
            elif config["kind"] in {"compatible", "laya"}:
                reason = "This adapter has no measured Agent complete interface"
            elif config["kind"] == "meta" and config["context_tokens"] is None:
                reason = "An explicit Meta context limit is required for measured requests"
            try:
                inspect_recipe(self.catalog, reference)
                selectable = True
                reason = "Measured completion interface; readiness and model quality are unverified"
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                pass
            kind = config["kind"]
            items.append(
                {
                    "id": profile["id"],
                    "label": profile["label"],
                    "catalog_binding": reference,
                    "selectable": selectable,
                    "auxiliary_selectable": selectable,
                    "provider": declaration_info(config).model_dump(),
                    "reasoning": config.get("reasoning_effort"),
                    "reasoning_options": ["none", "minimal", "low", "medium", "high", "xhigh"]
                    if kind == "meta"
                    else ["none"]
                    if kind == "lmstudio"
                    else [],
                    "reason": reason,
                    "qualification": "not_checked",
                    "readiness": "unknown",
                }
            )
        return items

    def change(self, request: ModelSettingsChange):
        # Preserve exact legacy receipts for requests without the additive field.
        excluded = {
            field
            for field in ("auxiliary_task", "catalog_binding")
            if getattr(request, field) is None
        }
        payload = request.model_dump(exclude=excluded)
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
            previous_bytes = self.db.execute(
                "SELECT length(CAST(body AS BLOB)) FROM settings WHERE id=1"
            ).fetchone()[0]
            if revision != request.expected_revision:
                raise ModelSettingsConflict("Agent model settings changed; refresh before saving")
            if revision >= 9_007_199_254_740_990:
                raise ValueError("Agent model settings revision space is exhausted")
            if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 10000:
                raise ValueError("Model settings receipt storage needs owner maintenance")
            target = request.harness_version
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
                if request.catalog_binding is not None:
                    binding = {
                        "catalog_binding": request.catalog_binding.model_dump(),
                        "reasoning": request.reasoning,
                    }
                    inspect_recipe(self.catalog, binding["catalog_binding"], binding["reasoning"])
                    routes[purpose] = binding
                elif request.profile_id is None:
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
            elif request.catalog_binding is not None:
                binding = {
                    "catalog_binding": request.catalog_binding.model_dump(),
                    "reasoning": request.reasoning,
                }
                inspect_recipe(self.catalog, binding["catalog_binding"], binding["reasoning"])
                if target is None:
                    body["default"] = binding
                else:
                    if target not in body["harnesses"] and len(body["harnesses"]) >= 1000:
                        raise ValueError("Too many harness model overrides")
                    body["harnesses"][target] = binding
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
            receipt = {
                "schema_version": "1.0",
                "idempotency_key": request.idempotency_key,
                "revision": revision + 1,
                "harness_version": target,
                "binding": binding,
                "change_boundary": "new_tasks",
                **({"auxiliary_task": purpose} if purpose is not None else {}),
            }
            encoded = json.dumps(body)
            encoded_bytes = len(encoded.encode("utf-8"))
            if encoded_bytes > MAX_SETTINGS_BYTES and encoded_bytes >= previous_bytes:
                # Existing larger host-only stores remain readable, and the
                # owner may remove overrides until they fit. New references
                # cannot grow the Options response beyond its client budget.
                raise ValueError("Agent model settings exceed the 512 KiB state limit")
            self.db.execute(
                "UPDATE settings SET revision=?, body=? WHERE id=1",
                (revision + 1, encoded),
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
            return self.selection_info(None)[0].model_dump()
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            return None

    async def close(self):
        try:
            # Only the task owner may start closing its adapters after actual
            # request cleanup. Shutdown can wait for existing closes, never
            # cut off an active provider because a worker deadline elapsed.
            closing = [lease.cleanup for lease in self.catalog_leases if lease.cleanup is not None]
            if closing:
                await asyncio.wait(closing, timeout=5)
            for instance in {
                id(getattr(value, "provider", value)): getattr(value, "provider", value)
                for value in [*self.instances.values(), *self.auxiliary_instances.values()]
            }.values():
                close = getattr(instance, "close", None)
                if close is not None:
                    await close()
        finally:
            self.db.close()
