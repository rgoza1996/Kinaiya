"""Protected TTS choice and exact per-operation ownership, independent of STT."""

import asyncio
import fcntl
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from packages.contracts.models import ProviderInfo
from packages.contracts.tts_models import (
    MAX_TTS_REVISION,
    TTSModelChange,
    TTSModelPin,
    TTSModelSelection,
)
from packages.providers.disclosure import scope_allowed
from packages.providers.text import ProviderCleanupError
from services.converse.model_settings import OPERATION_NAMES, _decode, _digest, _encode
from services.settings.provider_catalog import _storage
from services.settings.tts_factory import budget_available, inspect_tts_recipe, open_tts_recipe

BOUNDARY = "settled_converse_operations"
LEASE_LIMIT, RECEIPT_LIMIT = 64, 10000
_TABLES = {"tts_model_state", "tts_model_operations", "tts_model_admissions"}


@contextmanager
def _opening_store(path, workspace):
    if path is None:
        yield sqlite3.connect(":memory:"), True
        return
    if workspace is None:
        raise ValueError("Protected TTS settings require the worker workspace")
    path = Path(os.path.abspath(path))
    if path != path.resolve() or path.is_relative_to(Path(workspace).resolve()):
        raise ValueError("TTS storage must be outside the workspace without aliases")
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
                    raise RuntimeError("TTS startup ownership is busy") from None
                time.sleep(0.01)
        actual, pinned = path.parent.stat(), os.fstat(directory)
        if (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError("TTS storage directory identity changed")
        # Keep atomic file publication serialized through initialization; a
        # second starter must not win the lifetime lock on a creator's empty DB.
        yield _storage(path, workspace, report_created=True)
    finally:
        os.close(directory)


class TTSModelConflict(ValueError):
    pass


def _info(value):
    if value is None:
        return None
    info = ProviderInfo.model_validate(value.model_dump(), strict=True)
    TTSModelPin(
        source="host",
        backend_id="0" * 32,
        instance_id="0" * 32,
        settings_revision=0,
        catalog_revision=None,
        catalog_binding=None,
        provider=info.provider,
        model=info.model,
        local=info.local,
        profile=None,
        voice=None,
    )
    return info


class TTSModelLease:
    def __init__(self, owner, provider, selection, resource_revision=None):
        self.owner, self.provider, self.selection = owner, provider, selection
        self.target = owner.host_target if selection.source == "host" else provider
        self.resource_revision = resource_revision
        self.pin = None
        self.cleanup = None
        self.cleanup_failed = False
        self._native_cleanup_unknown = False
        self.publication_unknown = False
        self.settled = False
        self.admission_id = None
        self.admission_identity = None
        self.audio_exposed = False
        self.delivery_confirmed = False
        self.transport_cleanup = None

    def current(self):
        if (
            self.owner.closed
            or self not in self.owner.leases
            or self.pin is None
            or self.cleanup is not None
            or self.cleanup_failed
            or self.native_cleanup_unknown
        ):
            return False
        try:
            revision, selected = self.owner._read()
            if revision != self.pin.settings_revision or selected != self.selection:
                return False
            if self.selection.source == "host":
                return self.owner._host_available() and (
                    self.resource_revision is None
                    or self.owner._resource_revision() == self.resource_revision
                )
            info = _info(self.provider.info)
            return (
                (info.provider, info.model, info.local)
                == (self.pin.provider, self.pin.model, self.pin.local)
                and self.owner._resource_revision() == self.resource_revision
                and not getattr(self.provider, "_cleanup_failed", False)
            )
        except Exception:
            return False

    @property
    def playback_pending(self):
        return getattr(self, "audio_exposed", False) and not getattr(
            self, "delivery_confirmed", False
        )

    @property
    def cleanup_unknown(self):
        return self._native_cleanup_unknown or self.publication_unknown

    @property
    def native_cleanup_unknown(self):
        return self._native_cleanup_unknown

    def mark_unknown(self):
        self._native_cleanup_unknown = True

    def start_transport_close(self):
        if self.transport_cleanup is None:
            self.transport_cleanup = asyncio.create_task(self._close_transport())
            self.transport_cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self.transport_cleanup

    async def _close_transport(self):
        try:
            if self.selection.source == "catalog":
                await self.provider.close()
                self.provider.http.headers.clear()
            if getattr(self.target, "_cleanup_failed", False) or getattr(
                self.provider, "_cleanup_failed", False
            ):
                raise ProviderCleanupError("TTS transport cleanup is unconfirmed")
        except BaseException:
            self.cleanup_failed = True
            raise

    def start_close(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
            self.cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self.cleanup

    async def _close(self):
        try:
            if getattr(self.target, "_cleanup_failed", False) or getattr(
                self.provider, "_cleanup_failed", False
            ):
                self.cleanup_failed = True
            await asyncio.shield(self.start_transport_close())
            # Never close the shared host composition: it also owns STT.
            if self.cleanup_failed or self.cleanup_unknown or self.playback_pending:
                raise ProviderCleanupError("TTS operation cleanup is unconfirmed")
        except BaseException:
            self.cleanup_failed = True
            raise
        self.settled = True
        self.owner._released(self)


class TTSModelSettings:
    def __init__(
        self,
        runtime,
        catalog,
        host_provider=None,
        path=None,
        *,
        workspace=None,
        resource_state=None,
        host_info=None,
        host_profile=None,
        host_voice=None,
        budget_policy=None,
    ):
        self.runtime, self.catalog, self.host_provider = runtime, catalog, host_provider
        self.budget_policy = budget_policy
        self._host_identity_invalid = False
        try:
            self.host_target = getattr(host_provider, "tts", host_provider)
            self.host_method = getattr(host_provider, "synthesize", None)
            self.host_scoped = getattr(host_provider, "synthesize_scoped", None)
            declared = getattr(host_provider, "tts_info", getattr(host_provider, "info", None))
            self.host_declaration_present = declared is not None
            self.host_info = _info(declared if declared is not None else host_info)
            self.host_profile = TTSModelPin.display_identity(
                getattr(host_provider, "profile", host_profile)
            )
            self.host_voice = TTSModelPin.display_identity(
                getattr(self.host_target, "_voice", host_voice)
            )
        except (Exception, SystemExit):
            self.host_target, self.host_method = None, None
            self.host_declaration_present = False
            self.host_info = None
            self.host_profile = self.host_voice = None
            self._host_identity_invalid = True
        self.resource_state = resource_state
        self.instance_id = secrets.token_hex(16)
        self.backend_id = None
        self.leases = set()
        self._native_unsettled = set()
        self.closed = self._failed = self._db_closed = False
        self._owner_fd = None
        path = None if path is None else Path(os.path.abspath(path))
        try:
            with _opening_store(path, workspace) as (self.db, created):
                self._initialize_connection(path, created)
        except BaseException:
            if hasattr(self, "db"):
                # If close itself is unconfirmed, retain the actual lifetime
                # descriptor rather than admitting a replacement writer.
                self.db.close()
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None
            raise

    def _initialize_connection(self, path, created):
        if path is not None:
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
                raise ValueError("TTS storage identity changed")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db.execute("PRAGMA busy_timeout=500")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._initialize(created)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _schema(self):
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("TTS storage schema is unavailable")
        if (
            self.db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            != 3
        ):
            raise ValueError("TTS storage schema is unavailable")
        objects = set(
            self.db.execute(
                "SELECT CASE WHEN length(type)<=5 THEN type END, CASE WHEN length(name)<=32 THEN name END FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 4"
            )
        )
        if objects != {("table", name) for name in _TABLES}:
            raise ValueError("TTS storage schema is unavailable")
        columns = {
            "tts_model_state": [
                ("id", "INTEGER", 0, 1),
                ("revision", "INTEGER", 1, 0),
                ("selection", "TEXT", 1, 0),
                ("backend_id", "TEXT", 1, 0),
            ],
            "tts_model_operations": [
                ("key", "TEXT", 0, 1),
                ("digest", "TEXT", 1, 0),
                ("request", "TEXT", 1, 0),
                ("receipt", "TEXT", 1, 0),
            ],
            "tts_model_admissions": [("id", "TEXT", 0, 1), ("identity", "TEXT", 1, 0)],
        }
        for table, expected in columns.items():
            if self.db.execute("SELECT COUNT(*) FROM pragma_table_info(?)", (table,)).fetchone()[
                0
            ] != len(expected):
                raise ValueError("TTS storage schema is unavailable")
            actual = list(
                self.db.execute(
                    'SELECT CASE WHEN length(name)<=32 THEN name END, CASE WHEN length(type)<=7 THEN upper(type) END, "notnull", pk FROM pragma_table_info(?) LIMIT 5',
                    (table,),
                )
            )
            if actual != expected:
                raise ValueError("TTS storage schema is unavailable")

    def _initialize(self, created):
        marker = self.db.execute("PRAGMA user_version").fetchone()[0]
        objects = self.db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if created and marker == 0 and not objects:
            self.db.execute(
                "CREATE TABLE tts_model_state (id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,selection TEXT NOT NULL,backend_id TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE tts_model_operations (key TEXT PRIMARY KEY,digest TEXT NOT NULL,request TEXT NOT NULL,receipt TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE tts_model_admissions (id TEXT PRIMARY KEY,identity TEXT NOT NULL)"
            )
            self.db.execute(
                "INSERT INTO tts_model_state VALUES (1,0,?,?)",
                (_encode({"source": "host", "catalog_binding": None}), secrets.token_hex(16)),
            )
            self.db.execute("PRAGMA user_version=1")
        self._schema()
        revision, selection = self._read()
        self._native_unsettled = set(self._admissions())
        count = self.db.execute("SELECT COUNT(*) FROM tts_model_operations").fetchone()[0]
        if count != revision or count > RECEIPT_LIMIT:
            raise ValueError("TTS storage history is incomplete")
        revisions, latest = set(), None
        keys = self.db.execute(
            "SELECT CASE WHEN typeof(key)='text' AND length(CAST(key AS BLOB))<=128 THEN key END FROM tts_model_operations"
        )
        for (key,) in keys:
            saved = self._recorded(key)
            result_revision = saved[0]["revision"]
            if result_revision in revisions:
                raise ValueError("TTS storage history has duplicate revisions")
            revisions.add(result_revision)
            if result_revision == revision:
                latest = saved[0]["selection"]
        if (
            revisions != set(range(1, revision + 1))
            or (revision and latest != selection.model_dump())
            or (not revision and selection.source != "host")
        ):
            raise ValueError("TTS storage history is inconsistent")

    def _ensure_store(self):
        if self.closed or self._failed or self._db_closed:
            raise RuntimeError("TTS model settings are unavailable")

    def _read(self):
        self._ensure_store()
        self._schema()
        if self.db.execute("SELECT COUNT(*) FROM tts_model_state").fetchone()[0] != 1:
            raise ValueError("TTS model state is unavailable")
        row = self.db.execute(
            "SELECT CASE WHEN typeof(revision)='integer' THEN revision END, CASE WHEN typeof(selection)='text' AND length(CAST(selection AS BLOB))<=2048 THEN selection END, CASE WHEN typeof(backend_id)='text' AND length(backend_id)=32 THEN backend_id END FROM tts_model_state WHERE id=1"
        ).fetchone()
        if (
            row is None
            or type(row[0]) is not int
            or not 0 <= row[0] <= MAX_TTS_REVISION
            or type(row[2]) is not str
            or re.fullmatch(r"[a-f0-9]{32}", row[2]) is None
            or self.backend_id not in {None, row[2]}
        ):
            raise ValueError("TTS model state is unavailable")
        self.backend_id = row[2]
        return row[0], TTSModelSelection.model_validate(_decode(row[1], 2048))

    def _host_available(self):
        if (
            self._host_identity_invalid
            or self.host_provider is None
            or not callable(self.host_method)
        ):
            return False
        try:
            if (
                getattr(self.host_provider, "synthesize", None) != self.host_method
                or getattr(self.host_provider, "synthesize_scoped", None) != self.host_scoped
                or getattr(self.host_provider, "tts", self.host_provider) is not self.host_target
            ):
                return False
            if (
                getattr(self.host_provider, "profile", self.host_profile) != self.host_profile
                or getattr(self.host_target, "_voice", self.host_voice) != self.host_voice
            ):
                return False
            declared = getattr(
                self.host_provider, "tts_info", getattr(self.host_provider, "info", None)
            )
            return (declared is not None) == self.host_declaration_present and (
                declared is None or _info(declared) == self.host_info
            )
        except Exception:
            return False

    def _admissions(self):
        self._ensure_store()
        if self.db.execute("SELECT COUNT(*) FROM tts_model_admissions").fetchone()[0] > LEASE_LIMIT:
            raise ValueError("TTS admission history exceeds its bound")
        rows = self.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(id)=32 THEN id END, CASE WHEN typeof(identity)='text' AND length(CAST(identity AS BLOB))<=2048 THEN identity END FROM tts_model_admissions LIMIT 65"
        )
        result = {}
        revision, _ = self._read()
        for key, encoded in rows:
            if type(key) is not str or re.fullmatch(r"[a-f0-9]{32}", key) is None:
                raise ValueError("TTS native admission identity is unavailable")
            pin = TTSModelPin.model_validate(_decode(encoded, 2048))
            if (
                pin.backend_id != self.backend_id
                or pin.settings_revision > revision
                or _encode(pin.model_dump()) != encoded
            ):
                raise ValueError("TTS native admission provenance is unavailable")
            result[key] = encoded
        return result

    def unknown_admissions(self):
        observed = self._admissions()
        live = {
            lease.admission_id
            for lease in self.leases
            if not lease.cleanup_failed and not lease.cleanup_unknown
        }
        return len(set(observed) - live) + len(self._native_unsettled - set(observed))

    def begin_native(self, lease):
        if lease.admission_id is not None or lease not in self.leases or not lease.current():
            raise ValueError("TTS native invocation is stale or already admitted")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if (
                self.unknown_admissions()
                or len(self._admissions()) >= LEASE_LIMIT
                or not lease.current()
            ):
                raise TTSModelConflict("An unresolved native TTS admission must settle first")
            identity = _encode(lease.pin.model_dump())
            if len(identity.encode("utf-8")) > 2048:
                raise ValueError("TTS native admission metadata exceeds its bound")
            key = secrets.token_hex(16)
            lease.admission_id, lease.admission_identity = key, identity
            self._native_unsettled.add(key)
            self.db.execute("INSERT INTO tts_model_admissions VALUES (?,?)", (key, identity))
            self.db.commit()
        except BaseException:
            if lease.admission_id is not None:
                lease.mark_unknown()
            self._rollback()
            raise

    def finish_native(self, lease):
        if lease.admission_id is None:
            return
        if (
            lease.owner is not self
            or lease not in self.leases
            or lease.cleanup is not None
            or lease.cleanup_failed
            or lease.cleanup_unknown
            or lease.admission_id not in self._native_unsettled
        ):
            raise RuntimeError("TTS settlement does not own its original admission")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            observed = self._admissions()
            if observed.get(lease.admission_id) != lease.admission_identity:
                raise RuntimeError("Owned TTS admission settlement is unavailable")
            changed = self.db.execute(
                "DELETE FROM tts_model_admissions WHERE id=? AND identity=?",
                (lease.admission_id, lease.admission_identity),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Owned TTS admission settlement changed")
            self.db.commit()
            self._native_unsettled.discard(lease.admission_id)
        except BaseException:
            lease.mark_unknown()
            self._rollback()
            raise

    def _selected(self, selection, *, budget_ok=None):
        if selection.source == "host":
            if not self._host_available():
                raise ValueError("Host TTS is unavailable")
            return self.host_info
        if not (budget_available(self.budget_policy) if budget_ok is None else budget_ok):
            raise ValueError("A valid retained native speech budget is required")
        return inspect_tts_recipe(self.catalog, selection.catalog_binding).info

    def selected_info(self):
        return self._selected(self._read()[1])

    def selection(self):
        return self._read()[1]

    def _resource_revision(self):
        if self.resource_state is None:
            return None
        from services.resources.state import read_record

        state = read_record(self.resource_state)
        if state.profile == "local_only" or not state.desired_converse or state.phase != "ready":
            raise ValueError("Converse resource policy does not admit cloud TTS")
        return state.revision

    def state_token(self):
        revision, selection = self._read()
        return _digest(
            {
                "backend_id": self.backend_id,
                "instance_id": self.instance_id,
                "revision": revision,
                "selection": selection.model_dump(),
                "host_available": self._host_available(),
                "host_info": None if self.host_info is None else self.host_info.model_dump(),
            }
        )

    def _operations(self):
        observed = self.runtime.model_operations()
        if (
            type(observed) is not dict
            or set(observed) != set(OPERATION_NAMES)
            or any(
                type(count) is not int or not 0 <= count <= MAX_TTS_REVISION
                for count in observed.values()
            )
        ):
            raise RuntimeError("Converse operation ownership is unavailable")
        return {
            **observed,
            "leases": len(self.leases),
            "cleanup_pending": sum(
                not lease.cleanup_failed
                and (
                    (lease.cleanup is not None and not lease.cleanup.done())
                    or (lease.transport_cleanup is not None and not lease.transport_cleanup.done())
                )
                for lease in self.leases
            ),
            "cleanup_failed": sum(lease.cleanup_failed for lease in self.leases),
            "cleanup_unknown": self.unknown_admissions()
            + sum(lease.cleanup_unknown and lease.admission_id is None for lease in self.leases),
        }

    def snapshot(self):
        revision, selection = self._read()
        budget_ok = budget_available(self.budget_policy)
        try:
            info, available, error = self._selected(selection, budget_ok=budget_ok), True, None
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            info, available, error = (
                None,
                False,
                "Saved TTS selection is unavailable; choose an available source",
            )
        profiles, catalog_error = [], None
        try:
            if self.catalog is not None:
                for profile in self.catalog.snapshot()["profiles"]:
                    if profile["config"]["kind"] != "openrouter_tts":
                        continue
                    binding = {
                        name: profile[name] for name in ("id", "generation", "configuration_sha256")
                    }
                    selectable = True
                    try:
                        recipe = inspect_tts_recipe(self.catalog, binding)
                        if not budget_ok:
                            selectable = False
                    except (ValueError, RuntimeError, OSError, sqlite3.Error):
                        selectable = False
                    from services.settings.provider_factory import declaration_info

                    profiles.append(
                        {
                            "id": profile["id"],
                            "label": profile["label"],
                            "catalog_binding": binding,
                            "profile": "Kokoro 82M (OpenRouter)",
                            "voice": "af_heart",
                            "selectable": selectable,
                            "provider": recipe.info.model_dump()
                            if selectable
                            else declaration_info(profile["config"]).model_dump(),
                            "reason": "Kokoro speech interface; readiness is unknown"
                            if selectable
                            else "This generation or its retained native speech budget is unavailable",
                            "qualification": "not_checked",
                            "readiness": "unknown",
                        }
                    )
            else:
                catalog_error = "Provider catalog is unavailable; host TTS remains independent"
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            profiles, catalog_error = (
                [],
                "Provider catalog is unavailable; host TTS remains independent",
            )
        operations = self._operations()
        return {
            "schema_version": "1.0",
            "role": "tts",
            "backend_id": self.backend_id,
            "instance_id": self.instance_id,
            "revision": revision,
            "state_token": self.state_token(),
            "selection": selection.model_dump(),
            "selected_available": available,
            "selected_profile": self.host_profile
            if selection.source == "host"
            else "Kokoro 82M (OpenRouter)",
            "selected_voice": self.host_voice if selection.source == "host" else "af_heart",
            "budget": {
                "configured": budget_ok,
                "source": "host_configuration" if budget_ok else "unavailable",
                "reason": "Retained native character budget; estimates, not provider billing"
                if budget_ok
                else "A valid retained native speech budget is required; no allowance is created",
            },
            "selected_info": None if info is None else info.model_dump(),
            "selected_error": error,
            "host": {
                "provider": None if self.host_info is None else self.host_info.model_dump(),
                "selectable": self._host_available(),
                "profile": self.host_profile,
                "voice": self.host_voice,
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

    @staticmethod
    def _receipt(request):
        return {
            **request.model_dump(),
            "status": "tts_model_selection_saved",
            "role": "tts",
            "revision": request.expected_revision + 1,
            "change_boundary": BOUNDARY,
        }

    def _recorded(self, key):
        self._ensure_store()
        if type(key) is not str or re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", key) is None:
            raise ValueError("Stored TTS operation identity is invalid")
        row = self.db.execute(
            "SELECT CASE WHEN typeof(digest)='text' AND length(digest)=64 THEN digest END, CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=4096 THEN request END, CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=8192 THEN receipt END FROM tts_model_operations WHERE key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        original = TTSModelChange.model_validate(_decode(row[1], 4096))
        receipt = _decode(row[2], 8192)
        if (
            original.idempotency_key != key
            or original.expected_backend != self.backend_id
            or row[0] != _digest(original.model_dump())
            or type(receipt) is not dict
            or type(receipt.get("revision")) is not int
            or type(receipt.get("expected_revision")) is not int
            or _encode(receipt) != _encode(self._receipt(original))
            or receipt["revision"] > self._read()[0]
        ):
            raise RuntimeError("Stored TTS operation receipt is unavailable")
        return receipt, row[0]

    def _request(self, request):
        request = TTSModelChange.model_validate(request)
        self._read()
        if request.expected_backend != self.backend_id:
            raise TTSModelConflict(
                "TTS backend changed; original operation belongs to another authority"
            )
        return request

    def _prior(self, request):
        saved = self._recorded(request.idempotency_key)
        if saved is None:
            return None
        if saved[1] != _digest(request.model_dump()):
            raise TTSModelConflict("TTS operation key belongs to another original request")
        return saved[0]

    def lookup(self, request):
        request = self._request(request)
        self.db.execute("BEGIN")
        try:
            revision, _ = self._read()
            receipt = self._prior(request)
            result = {
                "schema_version": "1.0",
                "role": "tts",
                "status": "recorded" if receipt is not None else "unobserved",
                "request": request.model_dump(),
                "backend_id": self.backend_id,
                "instance_id": self.instance_id,
                "current_revision": revision,
                "mutation_fenced": request.expected_instance != self.instance_id
                or revision > request.expected_revision,
                "receipt": receipt,
            }
            self.db.commit()
            return result
        except BaseException:
            self._rollback()
            raise

    def _rollback(self):
        try:
            self.db.rollback()
            if self.db.in_transaction:
                raise RuntimeError("TTS rollback is unconfirmed")
        except BaseException:
            self.closed = self._failed = True
            try:
                self.db.close()
                self._db_closed = True
            finally:
                self._finish_close()
            raise RuntimeError("TTS storage is unavailable") from None

    def change(self, request):
        request = self._request(request)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self._prior(request)
            if prior is not None:
                self.db.commit()
                return prior
            revision, _ = self._read()
            if (
                request.expected_instance != self.instance_id
                or request.expected_revision != revision
                or request.expected_state != self.state_token()
            ):
                raise TTSModelConflict("TTS settings changed; refresh before saving")
            if any(self._operations().values()):
                raise TTSModelConflict(
                    "Converse operations and actual cleanup must settle before saving"
                )
            if revision >= RECEIPT_LIMIT:
                raise ValueError("TTS receipt capacity is exhausted")
            self._selected(request.selection)
            receipt = self._receipt(request)
            self.db.execute(
                "UPDATE tts_model_state SET revision=?,selection=? WHERE id=1",
                (revision + 1, _encode(request.selection.model_dump())),
            )
            self.db.execute(
                "INSERT INTO tts_model_operations VALUES (?,?,?,?)",
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
            self._rollback()
            raise

    def capture(self, content_scope):
        asyncio.get_running_loop()
        if (
            self.closed
            or getattr(self.runtime, "_tts_stopping", False)
            or self.unknown_admissions()
            or len(self.leases) >= LEASE_LIMIT
            or any(
                lease.cleanup is not None or lease.cleanup_failed or lease.cleanup_unknown
                for lease in self.leases
            )
        ):
            raise ValueError("TTS ownership or actual cleanup is busy")
        revision, selection = self._read()
        info = self._selected(selection)
        if info is not None and not scope_allowed(info, content_scope):
            raise ValueError("Audio disclosure is not permitted for the selected TTS model")
        recipe = None
        if selection.source == "host":
            provider = self.host_provider
            resource_revision = (
                self._resource_revision() if info is not None and info.local is False else None
            )
        else:
            resource_revision = self._resource_revision()
            provider, recipe = open_tts_recipe(
                self.catalog, selection.catalog_binding, self.budget_policy
            )
        lease = TTSModelLease(self, provider, selection, resource_revision)
        self.leases.add(lease)
        try:
            if recipe is not None and _info(provider.info) != info:
                raise ValueError("Captured TTS adapter identity changed")
            lease.pin = TTSModelPin(
                source=selection.source,
                backend_id=self.backend_id,
                instance_id=self.instance_id,
                settings_revision=revision,
                catalog_revision=None if recipe is None else recipe.catalog_revision,
                catalog_binding=selection.catalog_binding,
                provider=None if info is None else info.provider,
                model=None if info is None else info.model,
                local=None if info is None else info.local,
                profile=self.host_profile if recipe is None else "Kokoro 82M (OpenRouter)",
                voice=self.host_voice if recipe is None else "af_heart",
            )
            return lease
        except BaseException:
            lease.start_close()
            raise

    def _released(self, lease):
        self.leases.discard(lease)
        self._finish_close()

    def _finish_close(self):
        if self.closed and not self.leases and not self._native_unsettled:
            if not self._db_closed:
                self.db.close()
                self._db_closed = True
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None

    async def close(self):
        self.closed = True
        closing = [lease.cleanup for lease in self.leases if lease.cleanup is not None]
        if closing:
            await asyncio.wait(closing, timeout=1)
        self._finish_close()
