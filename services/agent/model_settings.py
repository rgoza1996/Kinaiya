"""Protected owner defaults; each accepted task captures its own provider.

Host profiles and exact native catalog generations are selectable. This store
contains references and reasoning, never endpoints, keys, prompts or permissions.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from core.policy import PolicyDenied
from packages.contracts.models import AgentModelPin, AuxiliaryTask, CatalogModelBinding
from packages.providers.disclosure import scope_allowed
from services.settings.provider_catalog import _storage as _protected_storage
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
_MAX_STORED_JSON_DEPTH = 16


class ModelSettingsConflict(ValueError):
    pass


class ModelSettingsUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Agent model settings outcome is unconfirmed")


def _embedding_storage(path):
    """Keep trusted path-only embeddings separate from protected host storage."""
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    created = False
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        # Never recreate an existing authority that disappears during opening.
        fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError("Agent model storage must be an owned regular file")
        # Older trusted embeddings created SQLite files with ordinary umask
        # permissions. Preserve their existing conversion, on the pinned FD.
        os.fchmod(fd, 0o600)
        db = sqlite3.connect(str(path))
        try:
            actual = path.stat(follow_symlinks=False)
            if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("Agent model storage identity changed")
        except BaseException:
            db.close()
            raise
        return db, created
    finally:
        os.close(fd)


@contextmanager
def _opening_storage(path, workspace):
    if path is None:
        yield sqlite3.connect(":memory:"), True
        return
    path = Path(os.path.abspath(path))
    if workspace is not None and (
        path != path.resolve() or path.is_relative_to(Path(workspace).resolve())
    ):
        # Check before creating or locking any directory. The actual protected
        # opener repeats all host containment/ownership checks unchanged.
        raise ValueError("Provider storage must be outside the worker workspace without aliases")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ModelSettingsUnavailable from None
                time.sleep(0.01)
        actual, pinned = path.parent.stat(), os.fstat(directory)
        if (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError("Agent model storage directory identity changed")
        # Serialize native creation through initialization, not just SQLite's
        # transaction: another starter must not observe the creator's empty
        # file before that creator has published its first authority.
        if workspace is None:
            yield _embedding_storage(path)
        else:
            yield _protected_storage(path, workspace, report_created=True)
    finally:
        os.close(directory)


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

    @model_serializer(mode="wrap")
    def preserve_historical_catalog_shape(self, handler):
        result = handler(self)
        # Host-only historical requests omitted this additive field, including
        # bound 1.1 requests. Defaults must not rewrite their public wire shape.
        # Explicit nulls and immutable catalog references keep their own shape.
        if "catalog_binding" not in self.model_fields_set:
            result.pop("catalog_binding", None)
        return result

    @model_validator(mode="after")
    def distinct_sources(self):
        if self.profile_id is not None and self.catalog_binding is not None:
            raise ValueError("Choose a host profile or an exact catalog generation")
        return self


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
        catalog=None,
        resource_state=None,
        workspace=None,
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
        self.auxiliary_routes = None
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
        self._failed = False
        self._closed = False
        self.backend_id = None
        self.instance = secrets.token_hex(16)
        try:
            with _opening_storage(path, workspace) as (self.db, created):
                self._execute("PRAGMA synchronous=FULL")
                with self._transaction():
                    self._initialize_authority(created=created)
        except BaseException:
            self._poison()
            raise
        from services.agent.auxiliary_routes import (
            AgentAuxiliaryRoutes,
            AuxiliaryRouteUnavailable,
            UnavailableAuxiliaryRoutes,
            UnconfiguredAuxiliaryRoutes,
        )

        try:
            route_path = (
                path.with_name(path.name + ".auxiliary-routes.sqlite") if path is not None else None
            )
            if path is not None:
                # A pre-release stem-derived name can alias distinct backends.
                # Never migrate, choose it, or infer empty configuration from
                # its existence; an owner must resolve that ambiguous authority.
                old_path = path.with_name(path.stem + "-auxiliary-routes.sqlite")
                try:
                    old_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise AuxiliaryRouteUnavailable
            if route_path is not None and workspace is None:
                self.auxiliary_routes = UnconfiguredAuxiliaryRoutes(route_path)
            else:
                self.auxiliary_routes = AgentAuxiliaryRoutes(self, route_path, workspace=workspace)
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            self.auxiliary_routes = UnavailableAuxiliaryRoutes()

    def _ensure_store(self):
        if self._failed or self._closed:
            raise ModelSettingsUnavailable

    def _storage(self, invoke):
        self._ensure_store()
        try:
            return invoke()
        except sqlite3.Error:
            raise
        except Exception:
            # A storage adapter can fail after its operation actually ran.
            # Keep that uncertainty distinct from native choice validation.
            raise ModelSettingsUnavailable from None

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
            self._execute("BEGIN IMMEDIATE")
            yield
            self._storage(lambda: self.db.commit())
        except BaseException:
            self._rollback()
            raise

    def _initialize_authority(self, *, created):
        marker = self._one("PRAGMA user_version")[0]
        tables = {
            row[0]
            for row in self._all(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        legacy = {"settings", "operations"}
        if marker == 0 and not tables:
            if not created:
                # Empty/truncated existing storage is lost authority, not a
                # new host-default selection or permission to mint a backend.
                raise ModelSettingsUnavailable
            self._execute(
                "CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), "
                "revision INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            self._execute(
                "CREATE TABLE operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                "receipt TEXT NOT NULL)"
            )
            self._execute(
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
                for row in self._all(f"PRAGMA table_info({table})")
            ]
            if actual != columns:
                raise ModelSettingsUnavailable
        self._read()
        if marker == 0:
            self._execute(
                "CREATE TABLE model_write_authority (id INTEGER PRIMARY KEY CHECK(id=1), "
                "backend_id TEXT NOT NULL, active_instance TEXT NOT NULL)"
            )
            self._execute(
                "INSERT INTO model_write_authority VALUES (1, ?, ?)",
                (secrets.token_hex(16), self.instance),
            )
            self._execute("PRAGMA user_version=1")
        else:
            self._authority()
            self._execute(
                "UPDATE model_write_authority SET active_instance=? WHERE id=1",
                (self.instance,),
            )
        backend, active = self._authority()
        if active != self.instance:
            raise ModelSettingsUnavailable
        self.backend_id = backend

    def _authority(self):
        self._ensure_store()
        if self._one("PRAGMA user_version")[0] != 1:
            raise ModelSettingsUnavailable
        rows = self._all("SELECT id, backend_id, active_instance FROM model_write_authority")
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
                if depth > _MAX_STORED_JSON_DEPTH:
                    raise ValueError("Stored JSON nesting exceeds its bound")
            elif char in "}]":
                depth -= 1
                if depth < 0:
                    raise ValueError("Invalid stored JSON nesting")
        try:
            return json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)
        except RecursionError:
            raise ValueError("Stored JSON decoding exceeded its bound") from None

    def _read(self):
        self._ensure_store()
        try:
            if self._one("SELECT count(*) FROM settings")[0] != 1:
                raise ModelSettingsUnavailable
            return self._read_settings()
        except (ValueError, TypeError, IndexError, KeyError):
            raise ModelSettingsUnavailable from None

    def _read_settings(self):
        row = self._one("SELECT revision, body FROM settings WHERE id=1")
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
            exclude={
                field
                for field in ("auxiliary_task", "catalog_binding")
                if getattr(request, field) is None
            }
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
            "binding": {
                "catalog_binding": request.catalog_binding.model_dump(),
                "reasoning": request.reasoning,
            }
            if request.catalog_binding is not None
            else {"profile_id": request.profile_id, "reasoning": request.reasoning}
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
        row = self._one("SELECT digest, receipt FROM operations WHERE key=?", (key,))
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
                type(binding) is not dict
                or set(binding)
                not in (
                    {"profile_id", "reasoning"},
                    {"catalog_binding", "reasoning"},
                )
            ):
                raise ValueError("Invalid stored binding")
            raw = {
                "schema_version": receipt["schema_version"],
                "expected_revision": receipt["revision"] - 1,
                "idempotency_key": key,
                "harness_version": receipt["harness_version"],
                "profile_id": binding.get("profile_id") if binding is not None else None,
                "reasoning": binding["reasoning"] if binding is not None else None,
                **(
                    {"catalog_binding": binding["catalog_binding"]}
                    if binding is not None and "catalog_binding" in binding
                    else {}
                ),
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
                        (
                            request.catalog_binding is None
                            and not self.valid_profile_id(request.profile_id)
                        )
                        or (request.reasoning is not None and request.reasoning not in EFFORTS)
                    )
                )
            ):
                raise ValueError("Invalid stored receipt")
            return request, receipt, digest
        except (ValueError, TypeError, KeyError, IndexError):
            raise ModelSettingsUnavailable from None

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
        return any(
            lease.retiring or lease.failed or (lease.cleanup is not None and not lease.settled)
            for lease in self.catalog_leases
        )

    def task_selection(self, harness_version, content_scope):
        """Capture fresh catalog adapters only for genuinely new accepted work."""
        from services.agent.task_model_lease import TaskModelLease

        if self.catalog_cleanup_busy:
            # An uncertain native operation can use the same backend as a
            # host profile. Changing selection cannot release its ownership.
            raise ValueError("Catalog provider cleanup is unconfirmed; no new work accepted")
        revision, body = self._read()
        lease = None

        def ensure_lease():
            nonlocal lease
            if lease is None:
                if len(self.catalog_leases) >= 64 or any(
                    item.failed for item in self.catalog_leases
                ):
                    raise ValueError("Catalog model ownership requires cleanup or capacity")
                lease = TaskModelLease(self.catalog_leases.discard)
                self.catalog_leases.add(lease)
            return lease

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
            ensure_lease()
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
            try:
                route_revision, routes = self.auxiliary_routes.configured(
                    harness_version, AUXILIARY_TASKS
                )
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                # Optional route state cannot disable independent primary work.
                # Unknown overrides also cannot authorize legacy replacement.
                route_revision, routes = None, None
            for purpose in AUXILIARY_TASKS:
                captures = None
                try:
                    if routes is None:
                        continue
                    if purpose not in routes:
                        auxiliary[purpose], pins[purpose] = select(purpose)
                        continue
                    from packages.contracts.auxiliary_routes import AuxiliaryRoute
                    from services.agent.auxiliary_route_task import CapturedAuxiliaryRoute
                    from services.settings.provider_factory import capture_recipes

                    route = AuxiliaryRoute.model_validate(routes[purpose])
                    ensure_lease()
                    captures = capture_recipes(
                        self.catalog,
                        [
                            (candidate.catalog_binding, candidate.reasoning)
                            for candidate in route.candidates
                        ],
                    )
                    captured = CapturedAuxiliaryRoute(
                        purpose=purpose,
                        harness_version=harness_version,
                        settings_revision=revision,
                        route_revision=route_revision,
                        candidates=route.candidates,
                        captures=captures,
                        lease=lease,
                        resource_state=self.resource_state,
                    )
                    lease.auxiliary_routes[purpose] = captured
                    captures = None  # The lease now owns every captured key.
                    auxiliary[purpose], pins[purpose] = captured, captured.pin()
                except (ValueError, RuntimeError, OSError, sqlite3.Error, SystemExit):
                    if captures is not None:
                        for capture in captures:
                            capture.release()
                    # An unavailable purpose stays unavailable; no replacement.
                    continue
            if lease is not None and not lease.adapters and not lease.auxiliary_routes:
                # No adapter, native operation or captured authority exists.
                self.catalog_leases.discard(lease)
                lease.settled = True
                lease = None
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
            if self.auxiliary_routes is not None:
                _, routes = self.auxiliary_routes.configured(harness_version, AUXILIARY_TASKS)
                if routes:
                    return True
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
            "auxiliary_routes_state": self.auxiliary_routes.status(),
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
                    bool(item.retiring)
                    or (item.cleanup is not None and not item.settled and not item.failed)
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
            previous_bytes = self._one(
                "SELECT length(CAST(body AS BLOB)) FROM settings WHERE id=1"
            )[0]
            if revision != request.expected_revision:
                raise ModelSettingsConflict("Agent model settings changed; refresh before saving")
            if revision >= 9_007_199_254_740_990:
                raise ValueError("Agent model settings revision space is exhausted")
            if self._one("SELECT count(*) FROM operations")[0] >= 10000:
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
            receipt = self._change_receipt(request)
            encoded = json.dumps(body)
            encoded_bytes = len(encoded.encode("utf-8"))
            if encoded_bytes > MAX_SETTINGS_BYTES and encoded_bytes >= previous_bytes:
                # Existing larger host-only stores remain readable, and the
                # owner may remove overrides until they fit. New references
                # cannot grow the Options response beyond its client budget.
                raise ValueError("Agent model settings exceed the 512 KiB state limit")
            self._execute(
                "UPDATE settings SET revision=?, body=? WHERE id=1",
                (revision + 1, encoded),
            )
            self._execute(
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
            return self.selection_info(None)[0].model_dump()
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            return None

    async def close(self):
        self._closed = True
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
            if self.auxiliary_routes is not None:
                self.auxiliary_routes.close()
            self.db.close()
