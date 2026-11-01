"""Protected Converse text selection and real per-operation adapter ownership.

Inspection and saving perform no discovery, count, model load or inference. A
new typed/window operation captures one exact generation/key. Later catalog
rotation cannot retarget that owner, and uncertain cleanup cannot free its slot.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

from packages.contracts.converse_models import (
    MAX_MODEL_REVISION,
    ConverseModelChange,
    ConverseModelSelection,
    RoleModelPin,
)
from packages.contracts.models import ProviderInfo
from packages.providers.text import ProviderCleanupError, ProviderError
from services.settings.provider_catalog import _storage
from services.settings.provider_factory import (
    EFFORTS,
    declaration_info,
    inspect_recipe,
    open_recipe,
)

BOUNDARY = "settled_converse_operations"
OPERATION_NAMES = (
    "preparing",
    "text",
    "text_admitting",
    "windows",
    "speech",
    "stt",
    "stt_cleanup",
    "playback_pending",
    "memory_pending",
)
LEASE_LIMIT = 64
RECEIPT_LIMIT = 10000


class ConverseModelConflict(ValueError):
    pass


def _encode(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value):
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _decode(value, limit):
    if type(value) is not str or len(value.encode("utf-8")) > limit:
        raise ValueError("Stored model-selection metadata exceeds its bound")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate model-selection field")
            result[key] = item
        return result

    def no_fraction(_):
        raise ValueError("Model-selection metadata requires integers")

    try:
        result = json.loads(
            value, object_pairs_hook=pairs, parse_float=no_fraction, parse_constant=no_fraction
        )
        pending, nodes = [(result, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 6 or nodes > 128:
                raise ValueError("Stored model-selection structure exceeds its bound")
            if type(item) is dict:
                pending.extend((child, depth + 1) for child in item.values())
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item)
        return result
    except (RecursionError, UnicodeError, OverflowError) as exc:
        raise ValueError("Stored model-selection metadata is invalid") from exc


def _info(provider):
    raw = provider.info.model_dump()
    if (
        set(raw) != {"schema_version", "provider", "model", "local", "streaming"}
        or raw["schema_version"] != "1.0"
        or type(raw["local"]) is not bool
        or type(raw["streaming"]) is not bool
    ):
        raise ValueError("Model declaration is unavailable")
    for name in ("provider", "model"):
        value = raw[name]
        if (
            type(value) is not str
            or not 1 <= len(value.encode("utf-8")) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Model declaration is unavailable")
    return ProviderInfo.model_validate(raw, strict=True)


class ConverseModelLease:
    def __init__(self, owner, provider, pin, selection):
        self.owner, self.provider, self.pin = owner, provider, pin
        self.selection = selection
        self.cleanup = None
        self.cleanup_failed = False
        self.settled = False

    def current(self):
        if (
            self.owner.closed
            or self.pin is None
            or self not in self.owner.leases
            or self.cleanup is not None
            or self.cleanup_failed
        ):
            return False
        try:
            revision, selection = self.owner._read()
            info = _info(self.provider)
            return (
                revision == self.pin.settings_revision
                and selection == self.selection
                and (info.provider, info.model, info.local)
                == (self.pin.provider, self.pin.model, self.pin.local)
            )
        except Exception:
            return False

    def start_close(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
            self.cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self.cleanup

    async def close(self):
        await asyncio.shield(self.start_close())

    async def _close(self):
        try:
            if self.selection.source == "catalog":
                adapter = getattr(self.provider, "provider", self.provider)
                await adapter.close()
                client = getattr(adapter, "http", None)
                if client is not None:
                    client.headers.clear()
            if self.cleanup_failed:
                raise ProviderCleanupError("Converse provider cleanup is unconfirmed")
        except BaseException:
            self.cleanup_failed = True
            raise
        self.settled = True
        self.owner._released(self)


class ConverseModelSettings:
    def __init__(
        self,
        runtime,
        catalog,
        path=None,
        host_reasoning=None,
        *,
        resource_state=None,
        workspace=None,
    ):
        self.runtime, self.catalog = runtime, catalog
        self.host_provider = runtime.provider
        if host_reasoning is not None and (
            type(host_reasoning) is not str
            or not 1 <= len(host_reasoning.encode("utf-8")) <= 16
            or any(ord(char) < 32 or ord(char) == 127 for char in host_reasoning)
        ):
            raise ValueError("Invalid host reasoning declaration")
        self.host_reasoning, self.resource_state = host_reasoning, resource_state
        self.instance_id = str(uuid.uuid4())
        self.leases = set()
        self.closed = False
        self._failed = False
        self._owner_fd = None
        self._db_closed = False
        existing = False
        if path is not None:
            if workspace is None:
                raise ValueError("Protected Converse model settings require the worker workspace")
            path = Path(os.path.abspath(path))
            existing = path.exists()
            self.db = _storage(path, workspace)
        else:
            self.db = sqlite3.connect(":memory:")
        try:
            if path is not None:
                # SQLite's transaction locks do not protect operation ownership
                # between requests. A second native process must not apply a
                # selection while this process owns inference or late cleanup.
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                self._owner_fd = fd
                info, actual = os.fstat(fd), path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)
                ):
                    raise ValueError("Converse model storage identity changed")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db.execute("PRAGMA busy_timeout=500")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._initialize(existing=existing)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        except BaseException:
            self.db.close()
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None
            raise

    def _initialize(self, *, existing):
        marker = self.db.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not existing and marker == 0 and not tables:
            self.db.execute(
                "CREATE TABLE converse_model_state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, selection TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE converse_model_operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL)"
            )
            self.db.execute(
                "INSERT INTO converse_model_state VALUES (1,0,?)",
                (_encode({"source": "host", "catalog_binding": None, "reasoning": None}),),
            )
            self.db.execute("PRAGMA user_version=1")
        elif marker != 1 or tables != {"converse_model_state", "converse_model_operations"}:
            # This is the first published format. Missing tables/metadata in an
            # existing file cannot authorize replacing the owner's selection.
            raise ValueError("Stored Converse model schema is unavailable")
        columns = {
            "converse_model_state": [
                ("id", "INTEGER", 0, 1),
                ("revision", "INTEGER", 1, 0),
                ("selection", "TEXT", 1, 0),
            ],
            "converse_model_operations": [
                ("key", "TEXT", 0, 1),
                ("digest", "TEXT", 1, 0),
                ("receipt", "TEXT", 1, 0),
            ],
        }
        for name, expected in columns.items():
            actual = [
                (row[1], row[2].upper(), row[3], row[5])
                for row in self.db.execute(f"PRAGMA table_info({name})")
            ]
            if actual != expected:
                raise ValueError("Stored Converse model schema is unavailable")
        if (
            self.db.execute("SELECT COUNT(*) FROM converse_model_operations").fetchone()[0]
            > RECEIPT_LIMIT
        ):
            raise ValueError("Stored Converse model receipts exceed their bound")
        self._read()

    def _released(self, lease):
        self.leases.discard(lease)
        self._finish_close()

    def _finish_close(self):
        if self.closed and not self.leases:
            if not self._db_closed:
                self.db.close()
                self._db_closed = True
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None

    def _ensure_store(self):
        if self.closed or self._failed or self._db_closed:
            raise RuntimeError("Converse model settings are unavailable")

    def _poison(self):
        self.closed = self._failed = True
        try:
            self.db.close()
        except BaseException:
            # Neither dirty reads nor a replacement writer are safe when the
            # connection's real retirement cannot be established.
            return
        self._db_closed = True
        self._finish_close()

    def _read(self):
        self._ensure_store()
        if self.db.execute("SELECT COUNT(*) FROM converse_model_state").fetchone()[0] != 1:
            raise ValueError("Stored Converse model state is unavailable")
        row = self.db.execute(
            "SELECT CASE WHEN typeof(revision)='integer' THEN revision END, "
            "CASE WHEN typeof(selection)='text' AND length(CAST(selection AS BLOB))<=2048 THEN selection END "
            "FROM converse_model_state WHERE id=1"
        ).fetchone()
        if row is None or type(row[0]) is not int or not 0 <= row[0] <= MAX_MODEL_REVISION:
            raise ValueError("Stored Converse model state is invalid")
        selection = ConverseModelSelection.model_validate(_decode(row[1], 2048))
        return row[0], selection

    def _host_info(self):
        try:
            info = _info(self.host_provider)
        except ProviderError as exc:
            raise ValueError("Host text stream is unavailable") from exc
        if not info.streaming or not callable(getattr(self.host_provider, "stream", None)):
            raise ValueError("Host text stream is unavailable")
        return info

    def _selection_info(self, selection):
        if selection.source == "host":
            return self._host_info(), self.host_reasoning
        recipe = inspect_recipe(
            self.catalog, selection.catalog_binding, selection.reasoning, role="converse"
        )
        return recipe.info, recipe.reasoning

    def selected_info(self):
        return self._selection_info(self._read()[1])[0]

    def selection(self):
        return self._read()[1]

    def selected_reasoning(self):
        return self._selection_info(self._read()[1])[1]

    def state_token(self):
        revision, selection = self._read()
        try:
            host = self._host_info().model_dump()
        except Exception:
            host = None
        return _digest(
            {
                "instance_id": self.instance_id,
                "revision": revision,
                "selection": selection.model_dump(),
                "host": host,
                "host_reasoning": self.host_reasoning,
            }
        )

    def _operations(self):
        observed = self.runtime.model_operations()
        if (
            type(observed) is not dict
            or set(observed) != set(OPERATION_NAMES)
            or any(
                type(count) is not int or not 0 <= count <= MAX_MODEL_REVISION
                for count in observed.values()
            )
        ):
            raise ValueError("Converse operation ownership is unavailable")
        return {
            **observed,
            "leases": len(self.leases),
            "cleanup_pending": sum(
                lease.cleanup is not None and not lease.settled and not lease.cleanup_failed
                for lease in self.leases
            ),
            "cleanup_failed": sum(lease.cleanup_failed for lease in self.leases),
        }

    def _catalog_profiles(self):
        if self.catalog is None:
            return []
        profiles = self.catalog.snapshot()["profiles"]
        if type(profiles) is not list or len(profiles) > 32:
            raise ValueError("Provider profile inventory exceeds its bound")
        result = []
        for profile in profiles:
            reference = {
                name: profile[name] for name in ("id", "generation", "configuration_sha256")
            }
            config = profile["config"]
            selectable = False
            reason = "This generation is unavailable for measured Converse text"
            try:
                inspect_recipe(self.catalog, reference, role="converse")
                selectable = True
                reason = "Measured text interface; readiness and model quality are unverified"
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                pass
            result.append(
                {
                    "id": profile["id"],
                    "label": profile["label"],
                    "catalog_binding": reference,
                    "selectable": selectable,
                    "provider": declaration_info(config).model_dump(),
                    "reasoning": config.get("reasoning_effort"),
                    "reasoning_options": list(EFFORTS)
                    if config["kind"] == "meta"
                    else ["none"]
                    if config["kind"] == "lmstudio"
                    else [],
                    "reason": reason,
                    "qualification": "not_checked",
                    "readiness": "unknown",
                }
            )
        return result

    def snapshot(self):
        revision, selection = self._read()
        try:
            host = self._host_info().model_dump()
        except Exception:
            host = None
        try:
            selected, reasoning = self._selection_info(selection)
            selected, selected_error = selected.model_dump(), None
        except Exception:
            selected, reasoning = None, None
            selected_error = (
                "The saved text-model selection is unavailable; choose an available model"
            )
        catalog_error = None
        try:
            profiles = self._catalog_profiles()
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            profiles = []
            catalog_error = (
                "Provider catalog is unavailable; the host selection remains independent"
            )
        operations = self._operations()
        return {
            "schema_version": "1.0",
            "instance_id": self.instance_id,
            "revision": revision,
            "state_token": self.state_token(),
            "selection": selection.model_dump(),
            "selected_info": selected,
            "selected_reasoning": reasoning,
            "selected_error": selected_error,
            "host": {
                "provider": host,
                "reasoning": self.host_reasoning,
                "selectable": host is not None,
            },
            "catalog_profiles": profiles,
            "catalog_error": catalog_error,
            "busy": self.closed or any(operations.values()),
            "operation_state": operations,
            "change_boundary": BOUNDARY,
            "qualification": "not_checked",
            "readiness": "unknown",
            "limits": {"leases": LEASE_LIMIT, "receipts": RECEIPT_LIMIT},
        }

    def receipt(self, key):
        if type(key) is not str or re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", key) is None:
            raise ValueError("Invalid Converse model operation key")
        recorded = self._recorded(key)
        return None if recorded is None else recorded[0]

    def _receipt(self, value, key):
        fields = {
            "schema_version",
            "status",
            "role",
            "idempotency_key",
            "expected_revision",
            "expected_state",
            "revision",
            "selection",
            "change_boundary",
        }
        if type(value) is not dict or set(value) != fields:
            raise ValueError("Stored Converse model receipt is invalid")
        request = ConverseModelChange.model_validate(
            {
                name: value[name]
                for name in (
                    "schema_version",
                    "idempotency_key",
                    "expected_revision",
                    "expected_state",
                    "selection",
                )
            }
        )
        if (
            value["status"] != "model_selection_saved"
            or value["role"] != "converse"
            or value["idempotency_key"] != key
            or value["change_boundary"] != BOUNDARY
            or type(value["revision"]) is not int
            or value["revision"] != request.expected_revision + 1
            or value["revision"] > MAX_MODEL_REVISION
        ):
            raise ValueError("Stored Converse model receipt identity is invalid")
        return {**value, "selection": request.selection.model_dump()}

    def _recorded(self, key):
        self._ensure_store()
        row = self.db.execute(
            "SELECT CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 THEN digest END, "
            "CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=8192 THEN receipt END "
            "FROM converse_model_operations WHERE key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = self._receipt(_decode(row[1], 8192), key)
            original = {
                name: receipt[name]
                for name in (
                    "schema_version",
                    "idempotency_key",
                    "expected_revision",
                    "expected_state",
                    "selection",
                )
            }
            if (
                type(row[0]) is not str
                or re.fullmatch(r"[a-f0-9]{64}", row[0]) is None
                or row[0] != _digest(original)
                or receipt["revision"] > self._read()[0]
            ):
                raise ValueError("Stored Converse model receipt identity is unavailable")
            return receipt, row[0]
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise RuntimeError("Stored Converse model receipt is unavailable") from None

    def _prior(self, request, digest):
        recorded = self._recorded(request.idempotency_key)
        if recorded is None:
            return None
        receipt, recorded_digest = recorded
        if recorded_digest != digest:
            raise ConverseModelConflict("Model-selection key was reused with a different request")
        if any(
            receipt[name] != request.model_dump()[name]
            for name in ("expected_revision", "expected_state", "selection")
        ):
            raise ValueError("Stored model receipt does not match its original request")
        return receipt

    def change(self, request):
        request = ConverseModelChange.model_validate(request)
        digest = _digest(request.model_dump())
        prior = self._prior(request, digest)
        if prior is not None:
            return prior
        if self.closed or any(self._operations().values()):
            raise ConverseModelConflict(
                "Converse operations or cleanup must settle before changing text models"
            )
        try:
            self.db.execute("BEGIN IMMEDIATE")
            prior = self._prior(request, digest)
            if prior is not None:
                self.db.commit()
                return prior
            if self.closed or any(self._operations().values()):
                raise ConverseModelConflict("Converse operations changed before saving")
            revision, _ = self._read()
            if (
                revision != request.expected_revision
                or self.state_token() != request.expected_state
            ):
                raise ConverseModelConflict(
                    "Converse model settings changed; refresh before saving"
                )
            if (
                self.db.execute("SELECT COUNT(*) FROM converse_model_operations").fetchone()[0]
                >= RECEIPT_LIMIT
            ):
                raise ValueError("Converse model receipt capacity is exhausted")
            # Validate native capability and current exact generation without
            # constructing a provider, importing credentials or opening I/O.
            self._selection_info(request.selection)
            value = {
                "schema_version": "1.0",
                "status": "model_selection_saved",
                "role": "converse",
                "idempotency_key": request.idempotency_key,
                "expected_revision": revision,
                "expected_state": request.expected_state,
                "revision": revision + 1,
                "selection": request.selection.model_dump(),
                "change_boundary": BOUNDARY,
            }
            self.db.execute(
                "UPDATE converse_model_state SET revision=?,selection=? WHERE id=1",
                (revision + 1, _encode(value["selection"])),
            )
            self.db.execute(
                "INSERT INTO converse_model_operations VALUES (?,?,?)",
                (request.idempotency_key, digest, _encode(value)),
            )
            self.db.commit()
            return value
        except BaseException:
            try:
                self.db.rollback()
                if self.db.in_transaction:
                    raise RuntimeError("Converse model rollback is unconfirmed")
            except BaseException:
                self._poison()
                raise RuntimeError("Converse model settings are unavailable") from None
            raise

    def capture(self):
        # Every locally constructed async adapter must have a real cleanup
        # owner even if its later identity validation refuses the capture.
        asyncio.get_running_loop()
        if (
            self.closed
            or len(self.leases) >= LEASE_LIMIT
            or any(lease.cleanup is not None or lease.cleanup_failed for lease in self.leases)
        ):
            raise ValueError("Converse provider cleanup or ownership is busy")
        revision, selection = self._read()
        if selection.source == "host":
            provider, info, reasoning, recipe = (
                self.host_provider,
                self._host_info(),
                self.host_reasoning,
                None,
            )
            lease = None
        else:
            provider, recipe = open_recipe(
                self.catalog,
                selection.catalog_binding,
                selection.reasoning,
                role="converse",
                resource_state=self.resource_state,
            )
            lease = ConverseModelLease(self, provider, None, selection)
            self.leases.add(lease)
        try:
            if recipe is not None:
                info, reasoning = _info(provider), recipe.reasoning
                if info != recipe.info:
                    raise ValueError("Captured Converse provider identity differs from its recipe")
            pin = RoleModelPin(
                source=selection.source,
                settings_revision=revision,
                catalog_revision=None if recipe is None else recipe.catalog_revision,
                catalog_binding=selection.catalog_binding,
                provider=info.provider,
                model=info.model,
                local=info.local,
                reasoning=reasoning,
            )
            if lease is None:
                lease = ConverseModelLease(self, provider, pin, selection)
                self.leases.add(lease)
            else:
                lease.pin = pin
            return lease
        except BaseException:
            if lease is not None:
                lease.start_close()
            raise

    async def close(self):
        self.closed = True
        # An operation owner alone may initiate close after its actual provider
        # work settles. Shutdown never cuts off a retained request or iterator.
        closing = [lease.cleanup for lease in self.leases if lease.cleanup is not None]
        if closing:
            await asyncio.wait(closing, timeout=1)
        self._finish_close()
