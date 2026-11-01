"""Protected route configuration; reads never construct adapters or resolve keys."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from packages.contracts.auxiliary_routes import (
    MAX_ROUTE_REVISION,
    AuxiliaryRoute,
    AuxiliaryRouteChange,
    AuxiliaryRouteScope,
)
from services.settings.provider_factory import inspect_recipe

MAX_ROUTES = 1000
MAX_RECEIPTS = 10000
MAX_METADATA_BYTES = 8192


class AuxiliaryRouteConflict(ValueError):
    pass


class AuxiliaryRouteUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Auxiliary route outcome is unconfirmed")


class UnavailableAuxiliaryRoutes:
    """No configuration/selection claim when the protected store cannot open."""

    def snapshot(self, *args, **kwargs):
        raise AuxiliaryRouteUnavailable

    change = lookup = receipt = configured = snapshot

    def status(self):
        return {
            "readable": False,
            "writable": False,
            "revision": None,
            "error": "Auxiliary route configuration is unavailable; auxiliary purposes are disabled for new tasks",
        }

    def close(self):
        pass


class UnconfiguredAuxiliaryRoutes(UnavailableAuxiliaryRoutes):
    """Legacy embedding without a worker-workspace authority for a new store.

    Only affirmative absence permits the unchanged single-model bindings. An
    existing/newly observed file, including a dangling symlink, stays unknown.
    This owner cannot acknowledge ordered-route reads or writes.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.unknown = False

    def _absent(self):
        if self.unknown:
            raise AuxiliaryRouteUnavailable
        try:
            self.path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            pass
        self.unknown = True
        raise AuxiliaryRouteUnavailable

    def configured(self, harness_version, purposes):
        self._absent()
        return 0, {}

    def status(self):
        try:
            self._absent()
        except AuxiliaryRouteUnavailable:
            return super().status()
        return {
            "readable": False,
            "writable": False,
            "revision": None,
            "error": "Ordered auxiliary routes are not configured in this embedding; existing single-model choices remain in use",
        }


def encode(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def decode(value):
    if type(value) is not str or len(value.encode("utf-8")) > MAX_METADATA_BYTES:
        raise AuxiliaryRouteUnavailable
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
            if depth > 12:
                raise AuxiliaryRouteUnavailable
        elif char in "}]":
            depth -= 1
            if depth < 0:
                raise AuxiliaryRouteUnavailable

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise AuxiliaryRouteUnavailable
            result[key] = item
        return result

    def invalid(_):
        raise AuxiliaryRouteUnavailable

    try:
        return json.loads(value, object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise AuxiliaryRouteUnavailable from None


class AgentAuxiliaryRoutes:
    def __init__(self, models, path=None, *, workspace=None):
        self.models = models
        self.instance = secrets.token_hex(16)
        self.backend_id = None
        self._failed = False
        self._closed = False
        if path is None:
            self.db = sqlite3.connect(":memory:", timeout=0.5)
            self._created = True
        else:
            from services.settings.provider_catalog import _storage

            self.db, self._created = _storage(
                path, Path.cwd() if workspace is None else workspace, report_created=True
            )
        try:
            self._sql("PRAGMA busy_timeout=500")
            self._sql("PRAGMA synchronous=FULL")
            with self._transaction():
                self._initialize()
        except BaseException:
            self._poison()
            raise

    def _ensure(self):
        if self._failed or self._closed:
            raise AuxiliaryRouteUnavailable

    def _storage(self, function):
        self._ensure()
        try:
            return function()
        except sqlite3.Error:
            raise
        except Exception:
            raise AuxiliaryRouteUnavailable from None

    def _sql(self, sql, parameters=()):
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
            pass

    @contextmanager
    def _transaction(self):
        self._ensure()
        try:
            self._sql("BEGIN IMMEDIATE")
            yield
            self._storage(self.db.commit)
        except BaseException:
            try:
                self.db.rollback()
                if self.db.in_transaction:
                    raise AuxiliaryRouteUnavailable
            except BaseException:
                self._poison()
                raise AuxiliaryRouteUnavailable from None
            raise

    def _initialize(self):
        marker = self._one("PRAGMA user_version")[0]
        tables = {
            row[0]
            for row in self._all(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if marker == 0 and not tables and self._created:
            self._sql(
                "CREATE TABLE route_state (id INTEGER PRIMARY KEY CHECK(id=1), "
                "revision INTEGER NOT NULL, backend_id TEXT NOT NULL, active_instance TEXT NOT NULL)"
            )
            self._sql(
                "CREATE TABLE routes (scope_id TEXT PRIMARY KEY, scope TEXT NOT NULL, route TEXT NOT NULL)"
            )
            self._sql(
                "CREATE TABLE operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                "request TEXT NOT NULL, receipt TEXT NOT NULL)"
            )
            self._sql(
                "INSERT INTO route_state VALUES (1,0,?,?)",
                (secrets.token_hex(16), self.instance),
            )
            self._sql("PRAGMA user_version=1")
        elif marker != 1 or tables != {"route_state", "routes", "operations"}:
            raise AuxiliaryRouteUnavailable
        columns = {
            "route_state": [
                ("id", "INTEGER", 0, 1),
                ("revision", "INTEGER", 1, 0),
                ("backend_id", "TEXT", 1, 0),
                ("active_instance", "TEXT", 1, 0),
            ],
            "routes": [
                ("scope_id", "TEXT", 0, 1),
                ("scope", "TEXT", 1, 0),
                ("route", "TEXT", 1, 0),
            ],
            "operations": [
                ("key", "TEXT", 0, 1),
                ("digest", "TEXT", 1, 0),
                ("request", "TEXT", 1, 0),
                ("receipt", "TEXT", 1, 0),
            ],
        }
        for table, expected in columns.items():
            actual = [
                (r[1], r[2].upper(), r[3], r[5]) for r in self._all(f"PRAGMA table_info({table})")
            ]
            if actual != expected:
                raise AuxiliaryRouteUnavailable
        self._state()
        self._sql("UPDATE route_state SET active_instance=? WHERE id=1", (self.instance,))
        self.backend_id = self._state()[1]

    def _state(self):
        rows = self._all("SELECT id,revision,backend_id,active_instance FROM route_state")
        if (
            self._one("PRAGMA user_version")[0] != 1
            or len(rows) != 1
            or type(rows[0][0]) is not int
            or rows[0][0] != 1
            or type(rows[0][1]) is not int
            or not 0 <= rows[0][1] <= MAX_ROUTE_REVISION
            or any(
                type(v) is not str or re.fullmatch(r"[a-f0-9]{32}", v) is None for v in rows[0][2:]
            )
            or (self.backend_id is not None and rows[0][2] != self.backend_id)
        ):
            raise AuxiliaryRouteUnavailable
        return rows[0][1:]

    @staticmethod
    def _scope(scope):
        return AuxiliaryRouteScope.model_validate(scope).model_dump(mode="json")

    def _route(self, scope):
        scope = self._scope(scope)
        row = self._one(
            "SELECT CASE WHEN length(CAST(scope AS BLOB))<=8192 THEN scope END, "
            "CASE WHEN length(CAST(route AS BLOB))<=8192 THEN route END FROM routes WHERE scope_id=?",
            (digest(scope),),
        )
        if row is None:
            return None
        if decode(row[0]) != scope:
            raise AuxiliaryRouteUnavailable
        try:
            return AuxiliaryRoute.model_validate(decode(row[1])).model_dump(mode="json")
        except ValueError:
            raise AuxiliaryRouteUnavailable from None

    def _selection(self, scope):
        own = self._route(scope)
        if own is not None:
            return own, own, "global" if scope["harness_version"] is None else "harness"
        inherited = self._route({**scope, "harness_version": None})
        return own, inherited, "global" if inherited is not None else "legacy"

    def _token(self, scope, state):
        own, effective, source = self._selection(scope)
        return digest(
            {
                "state": state,
                "instance": self.instance,
                "scope": scope,
                "route": own,
                "effective_route": effective,
                "source": source,
            }
        )

    @staticmethod
    def validate_candidate(catalog, candidate):
        recipe = inspect_recipe(catalog, candidate.catalog_binding, candidate.reasoning)
        if recipe.info.local:
            if candidate.cloud_scope != "local_only":
                raise ValueError("A local route candidate uses the local-only ceiling")
        elif recipe.info.provider == "meta":
            if candidate.cloud_scope != "public_synthetic":
                raise ValueError("Meta auxiliary routes accept public synthetic input only")
        else:
            raise ValueError("This cloud adapter is not qualified for auxiliary routes")
        return recipe

    def snapshot(self, scope):
        scope = self._scope(scope)
        with self._transaction():
            state = self._state()
            own, effective, source = self._selection(scope)
            token = self._token(scope, state)
        error = None
        try:
            profiles = self.models._catalog_profiles()
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            profiles, error = [], "Provider catalog is unavailable"
        return {
            "schema_version": "1.0",
            "backend_id": state[1],
            "instance_id": self.instance,
            "writable": state[2] == self.instance,
            "revision": state[0],
            "state_token": token,
            "scope": scope,
            "route": own,
            "effective_route": effective,
            "source": source,
            "catalog_profiles": profiles,
            "catalog_error": error,
            "change_boundary": "new_tasks",
            "qualification": "not_checked",
            "readiness": "unknown",
            "limits": {"candidates": 3, "routes": MAX_ROUTES, "receipts": MAX_RECEIPTS},
        }

    @staticmethod
    def _receipt(request):
        return {
            "schema_version": "1.0",
            "status": "auxiliary_route_saved",
            "backend_id": request.expected_backend,
            "instance_id": request.expected_instance,
            "idempotency_key": request.idempotency_key,
            "expected_revision": request.expected_revision,
            "expected_state": request.expected_state,
            "revision": request.expected_revision + 1,
            "scope": request.scope.model_dump(mode="json"),
            "route": request.route.model_dump(mode="json") if request.route is not None else None,
            "change_boundary": "new_tasks",
        }

    def _operation(self, key):
        row = self._one(
            "SELECT digest, CASE WHEN length(CAST(request AS BLOB))<=8192 THEN request END, "
            "CASE WHEN length(CAST(receipt AS BLOB))<=8192 THEN receipt END FROM operations WHERE key=?",
            (key,),
        )
        if row is None:
            return None
        try:
            request = AuxiliaryRouteChange.model_validate(decode(row[1]))
            receipt = decode(row[2])
            if (
                request.idempotency_key != key
                or request.expected_backend != self.backend_id
                or digest(request.model_dump(mode="json")) != row[0]
                or receipt != self._receipt(request)
                or receipt["revision"] > self._state()[0]
            ):
                raise AuxiliaryRouteUnavailable
            return request, receipt, row[0]
        except ValueError:
            raise AuxiliaryRouteUnavailable from None

    def change(self, request, *, validate_harness=None):
        request = AuxiliaryRouteChange.model_validate(request)
        body = request.model_dump(mode="json")
        stamp = digest(body)
        with self._transaction():
            state = self._state()
            prior = self._operation(request.idempotency_key)
            if prior is not None:
                if prior[2] != stamp:
                    raise AuxiliaryRouteConflict("Request key belongs to another route change")
                return prior[1]
            scope = request.scope.model_dump(mode="json")
            if (
                request.expected_backend != state[1]
                or request.expected_instance != self.instance
                or request.expected_instance != state[2]
                or request.expected_revision != state[0]
                or request.expected_state != self._token(scope, state)
            ):
                raise AuxiliaryRouteConflict(
                    "Auxiliary route state changed; inspect the exact receipt"
                )
            if (
                state[0] >= MAX_ROUTE_REVISION
                or self._one("SELECT count(*) FROM operations")[0] >= MAX_RECEIPTS
            ):
                raise ValueError("Auxiliary route receipt capacity requires owner maintenance")
            if request.route is not None:
                if request.scope.harness_version is not None and validate_harness is not None:
                    validate_harness(request.scope.harness_version)
                for candidate in request.route.candidates:
                    self.validate_candidate(self.models.catalog, candidate)
                if (
                    self._route(scope) is None
                    and self._one("SELECT count(*) FROM routes")[0] >= MAX_ROUTES
                ):
                    raise ValueError("Auxiliary route capacity requires owner maintenance")
                self._sql(
                    "INSERT OR REPLACE INTO routes VALUES (?,?,?)",
                    (digest(scope), encode(scope), encode(body["route"])),
                )
            else:
                self._sql("DELETE FROM routes WHERE scope_id=?", (digest(scope),))
            receipt = self._receipt(request)
            self._sql("UPDATE route_state SET revision=? WHERE id=1", (state[0] + 1,))
            self._sql(
                "INSERT INTO operations VALUES (?,?,?,?)",
                (request.idempotency_key, stamp, encode(body), encode(receipt)),
            )
            return receipt

    def lookup(self, request):
        request = AuxiliaryRouteChange.model_validate(request)
        body = request.model_dump(mode="json")
        with self._transaction():
            state = self._state()
            if request.expected_backend != state[1]:
                raise AuxiliaryRouteConflict("Route request belongs to another backend")
            prior = self._operation(request.idempotency_key)
            if prior is not None and prior[2] != digest(body):
                raise AuxiliaryRouteConflict("Request key belongs to another route change")
            return {
                "schema_version": "1.0",
                "backend_id": state[1],
                "request": body,
                "status": "recorded" if prior is not None else "unobserved",
                "receipt": prior[1] if prior is not None else None,
                "mutation_fenced": prior is not None
                or state[2] != request.expected_instance
                or state[0] > request.expected_revision,
            }

    def receipt(self, key):
        with self._transaction():
            self._state()
            prior = self._operation(key)
            return prior[1] if prior is not None else None

    def configured(self, harness_version, purposes):
        with self._transaction():
            state = self._state()
            return state[0], {
                purpose: route
                for purpose in purposes
                if (
                    route := self._selection(
                        {"purpose": purpose, "harness_version": harness_version}
                    )[1]
                )
                is not None
            }

    def status(self):
        try:
            with self._transaction():
                state = self._state()
            return {
                "readable": True,
                "writable": state[2] == self.instance,
                "revision": state[0],
                "error": None,
            }
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            return UnavailableAuxiliaryRoutes().status()

    def close(self):
        self._closed = True
        self.db.close()
