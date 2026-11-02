"""Protected independent classifier choice and actual operation ownership."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from packages.contracts.decisions_models import (
    MAX_DECISIONS_MODEL_REVISION,
    DecisionsModelChange,
    DecisionsModelPin,
    DecisionsModelSelection,
)
from packages.providers.decision_capture import CaptureNativeUnavailable
from packages.providers.text import ProviderCleanupError, owned_http_stream_cleanup
from services.decisions.provider_factory import (
    inspect_recipe,
    open_recipe,
    provider_info,
    provider_protocol,
)
from services.settings.provider_catalog import _storage

BOUNDARY = "settled_decisions_operations"
LEASE_LIMIT, RECEIPT_LIMIT = 64, 10000
_TABLES = {
    "decisions_model_state": "CREATE TABLE decisions_model_state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, backend_id TEXT NOT NULL, selection TEXT NOT NULL)",
    "decisions_model_operations": "CREATE TABLE decisions_model_operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, request TEXT NOT NULL, receipt TEXT NOT NULL)",
}


class DecisionsModelUnavailable(CaptureNativeUnavailable):
    def __init__(self):
        super().__init__("Decisions model ownership is unavailable")


class DecisionsModelConflict(ValueError):
    pass


def _encode(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _digest(value):
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _decode(raw, bound=8192):
    if type(raw) is not str or not 1 <= len(raw.encode("utf-8")) <= bound:
        raise DecisionsModelUnavailable

    def pairs(rows):
        value = {}
        for key, child in rows:
            if key in value:
                raise DecisionsModelUnavailable
            value[key] = child
        return value

    def invalid(_):
        raise DecisionsModelUnavailable

    value = json.loads(raw, object_pairs_hook=pairs, parse_float=invalid, parse_constant=invalid)
    pending, count = [(value, 0)], 0
    while pending:
        child, depth = pending.pop()
        count += 1
        if count > 128 or depth > 8:
            raise DecisionsModelUnavailable
        if type(child) is dict:
            pending.extend((item, depth + 1) for item in child.values())
        elif type(child) is list:
            pending.extend((item, depth + 1) for item in child)
        elif type(child) is str:
            child.encode("utf-8")
    return value


class DecisionsModelLease:
    def __init__(self, owner, provider, adapter, pin):
        self.owner, self.provider, self.adapter, self.pin = owner, provider, adapter, pin
        self.cleanup = None
        self.failed = False

    def current(self):
        if self.owner.closed or self not in self.owner.leases or self.cleanup is not None:
            return False
        try:
            from services.resources.providers import ResourceBoundProvider

            if isinstance(self.provider, ResourceBoundProvider):
                self.provider._check()
            revision, _, selection = self.owner._state()
            info = provider_info(self.provider)
            return (
                revision == self.pin.settings_revision
                and selection == self.pin.selection
                and (info.provider, info.model, info.local)
                == (self.pin.provider, self.pin.model, self.pin.local)
            )
        except Exception:
            return False

    async def _close(self):
        try:
            if self.adapter is not None:
                await self.adapter.close()
                self.adapter.http.headers.clear()
            if self.failed:
                raise ProviderCleanupError("Decisions provider cleanup is unconfirmed")
        except BaseException:
            self.failed = True
            raise
        self.owner.leases.discard(self)
        self.owner._release_store()

    async def close(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
            self.cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self.cleanup)


class DecisionsModelSettings:
    def __init__(self, host_provider, catalog, *, path=None, workspace=None, resource_state=None):
        self.host_provider, self.catalog, self.resource_state = (
            host_provider,
            catalog,
            resource_state,
        )
        self.instance_id = secrets.token_hex(16)
        self.backend_id = None
        self.closed = self.failed = False
        self.leases = set()
        self._preparing = 0
        self._operations = {"finalized": 0, "windows": 0, "health": 0}
        self._owner_fd = None
        self._db_closed = False
        self._host_cleanup = None
        self._host_closed = False
        if path is None:
            self.db, created = sqlite3.connect(":memory:"), True
        else:
            if workspace is None:
                raise ValueError("Classifier storage requires the worker workspace")
            path = Path(os.path.abspath(path))
            self.db, created = _storage(path, workspace, report_created=True)
        try:
            if path is not None:
                fd = self._owner_fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                info, actual = os.fstat(fd), path.stat(follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
                    raise DecisionsModelUnavailable
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db.execute("PRAGMA busy_timeout=500")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            if created:
                if (
                    self.db.execute("PRAGMA user_version").fetchone() != (0,)
                    or self.db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is not None
                ):
                    raise DecisionsModelUnavailable
                for statement in _TABLES.values():
                    self.db.execute(statement)
                self.db.execute(
                    "INSERT INTO decisions_model_state VALUES (1,0,?,?)",
                    (
                        secrets.token_hex(16),
                        _encode({"source": "host", "catalog_binding": None}),
                    ),
                )
                self.db.execute("PRAGMA user_version=1")
            self.backend_id = self._state()[1]
            self._validate_history()
            self.db.commit()
        except BaseException:
            self.db.close()
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None
            raise

    def _schema(self):
        if self.closed or self.failed or self._db_closed:
            raise DecisionsModelUnavailable
        if self.db.execute("PRAGMA user_version").fetchone() != (1,):
            raise DecisionsModelUnavailable
        rows = self.db.execute(
            "SELECT type,CASE WHEN length(CAST(name AS BLOB))<=128 THEN name END,"
            "CASE WHEN length(CAST(tbl_name AS BLOB))<=128 THEN tbl_name END,"
            "CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN sql END FROM sqlite_schema LIMIT 4"
        ).fetchall()
        expected = {("table", name) for name in _TABLES} | {
            ("index", "sqlite_autoindex_decisions_model_operations_1")
        }
        if len(rows) != 3 or {(row[0], row[1]) for row in rows} != expected:
            raise DecisionsModelUnavailable
        for kind, name, table, sql in rows:
            if kind == "index":
                if table != "decisions_model_operations" or sql is not None:
                    raise DecisionsModelUnavailable
            elif (
                table != name
                or type(sql) is not str
                or re.sub(r"\s+", "", sql).lower() != re.sub(r"\s+", "", _TABLES[name]).lower()
            ):
                raise DecisionsModelUnavailable

    def _state(self):
        try:
            self._schema()
            rows = self.db.execute(
                "SELECT id,CASE WHEN typeof(revision)='integer' THEN revision END,"
                "CASE WHEN typeof(backend_id)='text' AND length(CAST(backend_id AS BLOB))=32 THEN backend_id END,"
                "CASE WHEN typeof(selection)='text' AND length(CAST(selection AS BLOB))<=4096 THEN selection END FROM decisions_model_state LIMIT 2"
            ).fetchall()
            if len(rows) != 1:
                raise DecisionsModelUnavailable
            identity, revision, backend, raw = rows[0]
            selection = DecisionsModelSelection.model_validate(_decode(raw, 4096))
            if (
                identity != 1
                or type(revision) is not int
                or not 0 <= revision <= MAX_DECISIONS_MODEL_REVISION
                or type(backend) is not str
                or re.fullmatch(r"[a-f0-9]{32}", backend) is None
                or (self.backend_id is not None and backend != self.backend_id)
            ):
                raise DecisionsModelUnavailable
            return revision, backend, selection
        except (sqlite3.Error, ValueError, TypeError, UnicodeError, RecursionError):
            raise DecisionsModelUnavailable from None

    def _token(self, state):
        return _digest(
            {
                "backend": state[1],
                "instance": self.instance_id,
                "revision": state[0],
                "selection": state[2].model_dump(),
            }
        )

    def _selection_info(self, selection):
        if selection.source == "host":
            return provider_info(self.host_provider), None
        recipe = inspect_recipe(self.catalog, selection)
        return recipe.info, recipe

    def cache_stamp(self):
        try:
            state = self._state()
            _, recipe = self._selection_info(state[2])
            return _digest(
                {
                    "state": self._token(state),
                    "catalog_revision": None if recipe is None else recipe.catalog_revision,
                }
            )
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            raise DecisionsModelUnavailable from None

    def operation_state(self):
        return {
            "preparing": self._preparing,
            **self._operations,
            "leases": len(self.leases),
            "cleanup_pending": sum(
                lease.cleanup is not None and not lease.cleanup.done() for lease in self.leases
            ),
            "cleanup_failed": sum(lease.failed for lease in self.leases),
            "closed": self.closed,
        }

    def busy(self):
        return self.closed or self.failed or self._preparing != 0 or bool(self.leases)

    @contextmanager
    def preparing(self, kind):
        self._state()
        if kind not in self._operations or any(lease.cleanup is not None for lease in self.leases):
            raise DecisionsModelUnavailable
        self._preparing += 1
        self._operations[kind] += 1
        try:
            yield
        finally:
            self._preparing -= 1
            self._operations[kind] -= 1
            self._release_store()

    def _capture(self):
        state = self._state()
        if len(self.leases) >= LEASE_LIMIT or any(
            lease.cleanup is not None for lease in self.leases
        ):
            raise DecisionsModelUnavailable
        selection = state[2]
        adapter = recipe = None
        if selection.source == "catalog":
            try:
                provider, adapter, recipe = open_recipe(
                    self.catalog, selection, resource_state=self.resource_state
                )
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                raise DecisionsModelUnavailable from None
        else:
            provider = self.host_provider
        try:
            info = provider_info(provider)
            pin = DecisionsModelPin(
                backend_id=state[1],
                instance_id=self.instance_id,
                settings_revision=state[0],
                selection=selection,
                catalog_revision=None if recipe is None else recipe.catalog_revision,
                provider=info.provider,
                model=info.model,
                local=info.local,
                protocol=provider_protocol(info.provider),
            )
        except Exception:
            if adapter is not None:
                # The locally constructed adapter owns no request yet. Retain
                # its cleanup just like an admitted operation if validation fails.
                lease = DecisionsModelLease(self, provider, adapter, None)
                self.leases.add(lease)
                lease.cleanup = asyncio.create_task(lease._close())
                lease.cleanup.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
            raise DecisionsModelUnavailable from None
        lease = DecisionsModelLease(self, provider, adapter, pin)
        self.leases.add(lease)
        return lease

    @asynccontextmanager
    async def operation(self):
        lease = self._capture()
        try:
            with owned_http_stream_cleanup():
                yield lease
        except ProviderCleanupError:
            lease.failed = True
            raise
        finally:
            await lease.close()

    def snapshot(self):
        state = self._state()
        try:
            host_info = provider_info(self.host_provider).model_dump()
        except Exception:
            host_info = None
        try:
            info, _ = self._selection_info(state[2])
            selected_info, selected_error = info.model_dump(), None
        except Exception:
            selected_info, selected_error = None, "Selected classifier declaration is unavailable"
        profiles, catalog_error = [], None
        try:
            if self.catalog is None:
                raise DecisionsModelUnavailable
            for profile in self.catalog.snapshot()["profiles"]:
                binding = {
                    key: profile[key] for key in ("id", "generation", "configuration_sha256")
                }
                declaration = DecisionsModelSelection(source="catalog", catalog_binding=binding)
                try:
                    recipe = inspect_recipe(self.catalog, declaration)
                    selectable, reason, metadata = (
                        True,
                        "Typed Laya declaration; runtime not checked"
                        if recipe.info.provider == "laya"
                        else "Native d1 declaration; runtime not checked",
                        recipe.info.model_dump(),
                    )
                except Exception:
                    selectable, reason, metadata = (
                        False,
                        "Only an available exact typed classifier generation is eligible",
                        None,
                    )
                profiles.append(
                    {
                        "id": profile["id"],
                        "label": profile["label"],
                        "catalog_binding": binding,
                        "selectable": selectable,
                        "provider": metadata,
                        "reason": reason,
                        "protocol": provider_protocol(metadata["provider"]) if selectable else None,
                        "qualification": "not_checked",
                        "readiness": "unknown",
                    }
                )
        except Exception:
            profiles, catalog_error = [], "Classifier catalog declarations are unavailable"
        return {
            "schema_version": "1.0",
            "backend_id": state[1],
            "instance_id": self.instance_id,
            "revision": state[0],
            "state_token": self._token(state),
            "selection": state[2].model_dump(),
            "selected_info": selected_info,
            "selected_error": selected_error,
            "host": {
                "provider": host_info,
                "selectable": host_info is not None,
                "protocol": provider_protocol(host_info["provider"])
                if host_info
                else "host_configured",
            },
            "catalog_profiles": profiles,
            "catalog_error": catalog_error,
            "busy": self.busy(),
            "operation_state": self.operation_state(),
            "change_boundary": BOUNDARY,
            "qualification": "not_checked",
            "readiness": "unknown",
            "limits": {"leases": LEASE_LIMIT, "receipts": RECEIPT_LIMIT},
        }

    @staticmethod
    def _receipt(request):
        body = request.model_dump()
        return {
            "schema_version": "1.0",
            "status": "model_selection_saved",
            "role": "decisions",
            "backend_id": body["expected_backend"],
            "instance_id": body["expected_instance"],
            **{
                key: body[key]
                for key in ("idempotency_key", "expected_revision", "expected_state", "selection")
            },
            "revision": request.expected_revision + 1,
            "change_boundary": BOUNDARY,
        }

    def _recorded(self, key):
        state = self._state()
        row = self.db.execute(
            "SELECT CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 THEN digest END,"
            "CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=4096 THEN request END,"
            "CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=8192 THEN receipt END FROM decisions_model_operations WHERE key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        request = DecisionsModelChange.model_validate(_decode(row[1], 4096))
        receipt = _decode(row[2])
        if (
            type(receipt) is not dict
            or type(receipt.get("expected_revision")) is not int
            or type(receipt.get("revision")) is not int
            or request.idempotency_key != key
            or request.expected_backend != state[1]
            or row[0] != _digest(request.model_dump())
            or _encode(receipt) != _encode(self._receipt(request))
            or receipt["revision"] > state[0]
        ):
            raise DecisionsModelUnavailable
        return request, receipt

    def _validate_history(self):
        state = self._state()
        rows = self.db.execute(
            "SELECT CASE WHEN typeof(key)='text' AND length(CAST(key AS BLOB))<=128 THEN key END FROM decisions_model_operations LIMIT 10001"
        ).fetchall()
        if len(rows) != state[0] or len(rows) > RECEIPT_LIMIT:
            raise DecisionsModelUnavailable
        revisions, latest = set(), None
        for (key,) in rows:
            recorded = self._recorded(key)
            if recorded is None or recorded[1]["revision"] in revisions:
                raise DecisionsModelUnavailable
            revisions.add(recorded[1]["revision"])
            if latest is None or latest["revision"] < recorded[1]["revision"]:
                latest = recorded[1]
        if latest is not None and latest["selection"] != state[2].model_dump():
            raise DecisionsModelUnavailable

    def receipt(self, key):
        if type(key) is not str or re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", key) is None:
            raise ValueError("Invalid classifier receipt key")
        value = self._recorded(key)
        return None if value is None else value[1]

    def lookup(self, request):
        request = DecisionsModelChange.model_validate(request)
        state = self._state()
        if request.expected_backend != state[1]:
            raise DecisionsModelConflict("Classifier request belongs to another backend")
        recorded = self._recorded(request.idempotency_key)
        if recorded is not None and recorded[0] != request:
            raise DecisionsModelConflict("Classifier key belongs to another exact request")
        return {
            "schema_version": "1.0",
            "backend_id": state[1],
            "request": request.model_dump(),
            "status": "recorded" if recorded else "unobserved",
            "receipt": None if recorded is None else recorded[1],
            "mutation_fenced": recorded is not None
            or request.expected_instance != self.instance_id
            or state[0] > request.expected_revision,
        }

    def change(self, request):
        request = DecisionsModelChange.model_validate(request)
        recorded = self._recorded(request.idempotency_key)
        if recorded is not None:
            if recorded[0] != request:
                raise DecisionsModelConflict("Classifier key belongs to another exact request")
            return recorded[1]
        state = self._state()
        if self.busy():
            raise DecisionsModelConflict("Classifier work or actual cleanup is still owned")
        if (
            request.expected_backend,
            request.expected_instance,
            request.expected_revision,
            request.expected_state,
        ) != (state[1], self.instance_id, state[0], self._token(state)):
            raise DecisionsModelConflict("Classifier selection changed; refresh before saving")
        self._selection_info(request.selection)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self._state()
            if self.busy() or (
                request.expected_backend,
                request.expected_instance,
                request.expected_revision,
                request.expected_state,
            ) != (state[1], self.instance_id, state[0], self._token(state)):
                raise DecisionsModelConflict("Classifier admission changed before saving")
            if (
                self.db.execute("SELECT count(*) FROM decisions_model_operations").fetchone()[0]
                >= RECEIPT_LIMIT
            ):
                raise ValueError("Classifier receipts require owner maintenance")
            receipt = self._receipt(request)
            self.db.execute(
                "UPDATE decisions_model_state SET revision=?,selection=? WHERE id=1",
                (receipt["revision"], _encode(request.selection.model_dump())),
            )
            self.db.execute(
                "INSERT INTO decisions_model_operations VALUES (?,?,?,?)",
                (
                    request.idempotency_key,
                    _digest(request.model_dump()),
                    _encode(request.model_dump()),
                    _encode(receipt),
                ),
            )
            self.db.commit()
            return receipt
        except BaseException:
            try:
                self.db.rollback()
                if self.db.in_transaction:
                    raise DecisionsModelUnavailable
            except BaseException:
                self.failed = True
            raise

    def _release_store(self):
        if (
            self.closed
            and self._preparing == 0
            and not any(lease.adapter is None for lease in self.leases)
            and self._host_cleanup is None
        ):
            self._host_cleanup = asyncio.create_task(self._close_host())
            self._host_cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        if (
            self.closed
            and not self.leases
            and self._preparing == 0
            and self._host_closed
            and not self._db_closed
        ):
            self.db.close()
            self._db_closed = True
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None

    async def _close_host(self):
        provider = self.host_provider
        from services.resources.providers import ResourceBoundProvider

        if isinstance(provider, ResourceBoundProvider):
            provider = provider.provider
        close = getattr(provider, "close", None)
        if close is not None:
            await close()
        self._host_closed = True
        self._release_store()

    async def close(self):
        self.closed = True
        self._release_store()
        if self._host_cleanup is not None:
            await asyncio.shield(self._host_cleanup)
        self._release_store()


class UnavailableDecisionsModels:
    """A failed owner cannot impersonate an empty role or select the host."""

    def __init__(self, host_provider):
        self.host_provider = host_provider
        self._cleanup = None

    def _unavailable(self, *args, **kwargs):
        raise DecisionsModelUnavailable

    snapshot = change = receipt = lookup = cache_stamp = _unavailable

    @contextmanager
    def preparing(self, kind):
        raise DecisionsModelUnavailable
        yield

    @asynccontextmanager
    async def operation(self):
        raise DecisionsModelUnavailable
        yield

    async def _close(self):
        from services.resources.providers import ResourceBoundProvider

        provider = self.host_provider
        if isinstance(provider, ResourceBoundProvider):
            provider = provider.provider
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    async def close(self):
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._close())
            self._cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self._cleanup)
