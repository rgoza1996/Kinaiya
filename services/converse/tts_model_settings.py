"""Protected TTS choice and exact per-operation ownership, independent of STT."""

import asyncio
import fcntl
import hashlib
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
    TTS_SETTLEMENT_PROTOCOL,
    TTSAdmissionReconcile,
    TTSAdmissionResult,
    TTSModelChange,
    TTSModelPin,
    TTSModelSelection,
    TTSWorkerBinding,
    TTSWorkerProof,
)
from packages.providers.disclosure import scope_allowed
from packages.providers.text import ProviderCleanupError
from services.converse.model_settings import OPERATION_NAMES, _decode, _digest, _encode
from services.settings.provider_catalog import _storage
from services.settings.tts_factory import budget_available, inspect_tts_recipe, open_tts_recipe

BOUNDARY = "settled_converse_operations"
LEASE_LIMIT, RECEIPT_LIMIT = 64, 10000
_TABLES_V1 = {"tts_model_state", "tts_model_operations", "tts_model_admissions"}
_TABLES = {
    "tts_model_state",
    "tts_model_operations",
    "tts_model_admissions",
    "tts_model_settlements",
}


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
        self.cleanup_actual_failed = False
        self._native_cleanup_unknown = False
        self.publication_unknown = False
        self.settled = False
        self.admission_id = None
        self.admission_identity = None
        self.admission_binding = None
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
            self.cleanup_actual_failed = True
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
                self.cleanup_actual_failed = True
            await asyncio.shield(self.start_transport_close())
            # Never close the shared host composition: it also owns STT.
            if (
                self.cleanup_failed
                or self.cleanup_actual_failed
                or self.cleanup_unknown
                or self.playback_pending
            ):
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
        self._reconciling = {}
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

    def _schema_v1(self):
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
        if objects != {("table", name) for name in _TABLES_V1}:
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

    def _schema(self):
        if self.db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("TTS storage schema is unavailable")
        if (
            self.db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            != 4
        ):
            raise ValueError("TTS storage schema is unavailable")
        objects = set(
            self.db.execute(
                "SELECT CASE WHEN length(type)<=5 THEN type END, CASE WHEN length(name)<=32 THEN name END FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 5"
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
            "tts_model_admissions": [
                ("id", "TEXT", 0, 1),
                ("identity", "TEXT", 1, 0),
                ("binding", "TEXT", 0, 0),
            ],
            "tts_model_settlements": [
                ("id", "TEXT", 0, 1),
                ("identity", "TEXT", 1, 0),
                ("binding", "TEXT", 1, 0),
                ("digest", "TEXT", 1, 0),
                ("request", "TEXT", 1, 0),
                ("receipt", "TEXT", 1, 0),
            ],
        }
        for table, expected in columns.items():
            if self.db.execute("SELECT COUNT(*) FROM pragma_table_info(?)", (table,)).fetchone()[
                0
            ] != len(expected):
                raise ValueError("TTS storage schema is unavailable")
            actual = list(
                self.db.execute(
                    'SELECT CASE WHEN length(name)<=32 THEN name END, CASE WHEN length(type)<=7 THEN upper(type) END, "notnull", pk FROM pragma_table_info(?) LIMIT 7',
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
                "CREATE TABLE tts_model_admissions (id TEXT PRIMARY KEY,identity TEXT NOT NULL,binding TEXT)"
            )
            self.db.execute(
                "CREATE TABLE tts_model_settlements (id TEXT PRIMARY KEY,identity TEXT NOT NULL,binding TEXT NOT NULL,digest TEXT NOT NULL,request TEXT NOT NULL,receipt TEXT NOT NULL)"
            )
            self.db.execute(
                "INSERT INTO tts_model_state VALUES (1,0,?,?)",
                (_encode({"source": "host", "catalog_binding": None}), secrets.token_hex(16)),
            )
            self.db.execute("PRAGMA user_version=2")
        elif not created and marker == 1 and objects == 3:
            self._schema_v1()
            self.db.execute("ALTER TABLE tts_model_admissions ADD COLUMN binding TEXT")
            self.db.execute(
                "CREATE TABLE tts_model_settlements (id TEXT PRIMARY KEY,identity TEXT NOT NULL,binding TEXT NOT NULL,digest TEXT NOT NULL,request TEXT NOT NULL,receipt TEXT NOT NULL)"
            )
            self.db.execute("PRAGMA user_version=2")
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
        if (
            self.db.execute("SELECT COUNT(*) FROM tts_model_settlements").fetchone()[0]
            > RECEIPT_LIMIT
        ):
            raise ValueError("TTS settlement history exceeds its bound")
        settlement_ids = self.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(id)=32 THEN id END FROM tts_model_settlements"
        )
        for (admission_id,) in settlement_ids:
            self._settled(admission_id)

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

    def _admission_records(self):
        self._ensure_store()
        if self.db.execute("SELECT COUNT(*) FROM tts_model_admissions").fetchone()[0] > LEASE_LIMIT:
            raise ValueError("TTS admission history exceeds its bound")
        rows = self.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(id)=32 THEN id END, CASE WHEN typeof(identity)='text' AND length(CAST(identity AS BLOB))<=2048 THEN identity END, CASE WHEN binding IS NULL THEN NULL WHEN typeof(binding)='text' AND length(CAST(binding AS BLOB))<=4096 THEN binding ELSE 0 END FROM tts_model_admissions LIMIT 65"
        )
        result = {}
        revision, _ = self._read()
        for key, encoded, raw_binding in rows:
            if (
                type(key) is not str
                or re.fullmatch(r"[a-f0-9]{32}", key) is None
                or raw_binding == 0
            ):
                raise ValueError("TTS native admission identity is unavailable")
            pin = TTSModelPin.model_validate(_decode(encoded, 2048))
            if (
                pin.backend_id != self.backend_id
                or pin.settings_revision > revision
                or _encode(pin.model_dump()) != encoded
            ):
                raise ValueError("TTS native admission provenance is unavailable")
            binding = None
            if raw_binding is not None:
                binding = TTSWorkerBinding.model_validate(_decode(raw_binding, 4096))
                if (
                    pin.source != "host"
                    or (pin.profile is not None and binding.profile != pin.profile)
                    or _encode(binding.model_dump(mode="json")) != raw_binding
                ):
                    raise ValueError("TTS native admission binding is unavailable")
            result[key] = (encoded, binding)
        return result

    def _admissions(self):
        return {key: encoded for key, (encoded, _) in self._admission_records().items()}

    def unknown_admissions(self):
        observed = self._admissions()
        live = {
            lease.admission_id
            for lease in self.leases
            if not lease.cleanup_failed and not lease.cleanup_unknown
        }
        return len(set(observed) - live) + len(self._native_unsettled - set(observed))

    def begin_native(self, lease, *, binding=None):
        if lease.admission_id is not None or lease not in self.leases or not lease.current():
            raise ValueError("TTS native invocation is stale or already admitted")
        encoded_binding = None
        if binding is not None:
            binding = TTSWorkerBinding.model_validate(
                binding.model_dump(mode="json")
                if isinstance(binding, TTSWorkerBinding)
                else binding,
                strict=True,
            )
            if (
                lease.pin.source != "host"
                or (lease.pin.profile is not None and binding.profile != lease.pin.profile)
                or binding.audio_exposed
            ):
                raise ValueError("TTS native admission binding does not match captured lease")
            encoded_binding = _encode(binding.model_dump(mode="json"))
            if len(encoded_binding.encode("utf-8")) > 4096:
                raise ValueError("TTS native admission binding exceeds its bound")
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
            lease.admission_id = key
            lease.admission_identity = identity
            lease.admission_binding = encoded_binding
            self._native_unsettled.add(key)
            self.db.execute(
                "INSERT INTO tts_model_admissions VALUES (?,?,?)",
                (key, identity, encoded_binding),
            )
            self.db.commit()
        except BaseException:
            if lease.admission_id is not None:
                lease.mark_unknown()
            self._rollback()
            raise

    def mark_binding_audio_exposed(self, lease):
        if lease.owner is not self or lease.admission_id is None or lease.admission_binding is None:
            return
        binding = TTSWorkerBinding.model_validate(_decode(lease.admission_binding, 4096))
        if binding.audio_exposed:
            return
        updated = binding.model_copy(update={"audio_exposed": True})
        encoded_binding = _encode(updated.model_dump(mode="json"))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            records = self._admission_records()
            record = records.get(lease.admission_id)
            if (
                record is None
                or record[0] != lease.admission_identity
                or record[1] is None
                or _encode(record[1].model_dump(mode="json")) != lease.admission_binding
            ):
                raise RuntimeError("Owned TTS admission binding is unavailable")
            changed = self.db.execute(
                "UPDATE tts_model_admissions SET binding=? WHERE id=? AND identity=? AND binding=?",
                (
                    encoded_binding,
                    lease.admission_id,
                    lease.admission_identity,
                    lease.admission_binding,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Owned TTS admission binding changed")
            self.db.commit()
            lease.admission_binding = encoded_binding
        except BaseException:
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
            records = self._admission_records()
            record = records.get(lease.admission_id)
            if (
                record is None
                or record[0] != lease.admission_identity
                or (None if record[1] is None else _encode(record[1].model_dump(mode="json")))
                != lease.admission_binding
            ):
                raise RuntimeError("Owned TTS admission settlement is unavailable")
            changed = self.db.execute(
                "DELETE FROM tts_model_admissions WHERE id=? AND identity=? AND binding IS ?",
                (lease.admission_id, lease.admission_identity, lease.admission_binding),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Owned TTS admission settlement changed")
            self.db.commit()
            self._native_unsettled.discard(lease.admission_id)
        except BaseException:
            lease.mark_unknown()
            self._rollback()
            raise

    @staticmethod
    def _pin_sha256(identity: str) -> str:
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _native_tts_client(self):
        from packages.providers.meta_speech import CloudSTTLocalTTS

        provider = self.host_provider
        target = (
            getattr(provider, "tts", provider)
            if isinstance(provider, CloudSTTLocalTTS)
            else getattr(provider, "tts", provider)
        )
        if callable(getattr(target, "prepare_native_tts", None)) and callable(
            getattr(target, "lookup_native_tts", None)
        ):
            return target
        return None

    def admission_inventory(self):
        self._read()
        records = self._admission_records()
        live = {
            lease.admission_id
            for lease in self.leases
            if not lease.cleanup_failed and not lease.cleanup_unknown
        }
        client = self._native_tts_client()
        admissions = []
        for admission_id, (identity, binding) in sorted(records.items()):
            active = admission_id in live
            supported = (
                binding is not None
                and client is not None
                and getattr(client, "_native_stt_origin", None) == binding.origin
                and getattr(client, "profile", None) == binding.profile
            )
            admissions.append(
                {
                    "admission_id": admission_id,
                    "pin_sha256": self._pin_sha256(identity),
                    "protocol": None if binding is None else binding.protocol,
                    "worker_instance": None if binding is None else binding.worker_instance,
                    "operation_id": None if binding is None else binding.operation_id,
                    "tts_operation_id": None if binding is None else binding.tts_operation_id,
                    "audio_exposed": False if binding is None else binding.audio_exposed,
                    "active": active,
                    "reconcilable": supported and not active,
                }
            )
        return {
            "schema_version": "1.0",
            "role": "tts",
            "backend_id": self.backend_id,
            "instance_id": self.instance_id,
            "admissions": admissions,
            "limit": LEASE_LIMIT,
        }

    def _settled(self, admission_id):
        self._ensure_store()
        if type(admission_id) is not str or re.fullmatch(r"[a-f0-9]{32}", admission_id) is None:
            raise ValueError("Stored TTS settlement admission identity is invalid")
        row = self.db.execute(
            "SELECT CASE WHEN typeof(identity)='text' AND length(CAST(identity AS BLOB))<=2048 THEN identity END, CASE WHEN typeof(binding)='text' AND length(CAST(binding AS BLOB))<=4096 THEN binding END, CASE WHEN typeof(digest)='text' AND length(digest)=64 THEN digest END, CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=4096 THEN request END, CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=4096 THEN receipt END FROM tts_model_settlements WHERE id=?",
            (admission_id,),
        ).fetchone()
        if row is None:
            return None
        if any(item is None for item in row):
            raise RuntimeError("Stored TTS settlement receipt is unavailable")
        pin = TTSModelPin.model_validate(_decode(row[0], 2048))
        binding = TTSWorkerBinding.model_validate(_decode(row[1], 4096))
        original = TTSAdmissionReconcile.model_validate(_decode(row[3], 4096))
        receipt = TTSAdmissionResult.model_validate(_decode(row[4], 4096))
        revision, _ = self._read()
        pin_sha256 = self._pin_sha256(row[0])
        expected_receipt = TTSAdmissionResult(
            backend_id=self.backend_id,
            admission_id=admission_id,
            pin_sha256=pin_sha256,
            worker_instance=binding.worker_instance,
            operation_id=binding.operation_id,
            worker_outcome=receipt.worker_outcome,
            state="retired",
            replayed=False,
        )
        if (
            pin.source != "host"
            or pin.backend_id != self.backend_id
            or pin.settings_revision > revision
            or _encode(pin.model_dump()) != row[0]
            or (pin.profile is not None and binding.profile != pin.profile)
            or _encode(binding.model_dump(mode="json")) != row[1]
            or original.admission_id != admission_id
            or original.expected_backend_id != self.backend_id
            or original.pin_sha256 != pin_sha256
            or row[2] != _digest(original.model_dump(mode="json"))
            or _encode(original.model_dump(mode="json")) != row[3]
            or _encode(receipt.model_dump(mode="json"))
            != _encode(expected_receipt.model_dump(mode="json"))
        ):
            raise RuntimeError("Stored TTS settlement receipt is unavailable")
        if (
            self.db.execute(
                "SELECT 1 FROM tts_model_admissions WHERE id=?", (admission_id,)
            ).fetchone()
            is not None
        ):
            raise ValueError("A TTS admission cannot be both retained and retired")
        return original, receipt.model_dump(mode="json")

    async def _journal_confirms_delivery(self, binding: TTSWorkerBinding) -> bool:
        journal = getattr(self.runtime, "journal", None)
        if journal is None:
            return False
        try:
            read_fn = getattr(journal, "recent", None) or getattr(journal, "read", None)
            if not callable(read_fn):
                return False
            entries = await read_fn(session_id=binding.session_id, limit=100)
        except Exception:
            return False
        for entry in entries:
            if (entry.session_id, entry.thread_id, entry.epoch) != (
                binding.session_id,
                binding.thread_id,
                binding.epoch,
            ):
                continue
            if entry.kind != "delivery.updated" or entry.origin != "converse":
                continue
            payload = entry.payload or {}
            if (
                payload.get("event_id") == binding.source_event_id
                and payload.get("generation_epoch") == binding.generation_epoch
                and payload.get("tts_operation_id") == binding.tts_operation_id
                and payload.get("status") in {"played", "interrupted", "delivery_unknown"}
            ):
                return True
            if (
                payload.get("control") == "generation_stop"
                and payload.get("status") == "interrupted"
                and isinstance(payload.get("next_generation_epoch"), int)
                and payload["next_generation_epoch"] > binding.generation_epoch
            ):
                return True
        return False

    @staticmethod
    def _validate_worker_proof(proof, binding: TTSWorkerBinding) -> TTSWorkerProof:
        from packages.providers.speech import SpeechClient

        try:
            if type(proof) is dict:
                if set(proof) != set(TTSWorkerProof.model_fields):
                    raise ValueError("TTS worker proof is missing required fields")
                validated = TTSWorkerProof.model_validate(proof, strict=True)
            elif type(proof) is TTSWorkerProof:
                validated = TTSWorkerProof.model_validate(
                    proof.model_dump(mode="json"), strict=True
                )
            else:
                raise ValueError("TTS worker proof has an unsupported shape")
        except Exception as exc:
            raise TTSModelConflict(
                "TTS worker proof does not confirm positive settlement for the admitted operation"
            ) from exc
        expected_request_sha256 = SpeechClient._tts_request_sha256(
            binding.text_sha256, binding.profile
        )
        if (
            validated.protocol != TTS_SETTLEMENT_PROTOCOL
            or validated.protocol != binding.protocol
            or validated.worker_instance != binding.worker_instance
            or validated.operation_id != binding.operation_id
            or validated.text_sha256 != binding.text_sha256
            or validated.profile != binding.profile
            or validated.request_sha256 != binding.request_sha256
            or validated.request_sha256 != expected_request_sha256
            or validated.settled is not True
            or validated.state != "settled"
            or validated.outcome not in {"not_dispatched", "completed", "cancelled", "failed"}
        ):
            raise TTSModelConflict(
                "TTS worker proof does not confirm positive settlement for the admitted operation"
            )
        return validated

    async def _check_settlement_fences(self, admission_id: str, binding: TTSWorkerBinding, client):
        if getattr(client, "_cleanup_failed", False) or getattr(client, "_native_closes", set()):
            raise TTSModelConflict(
                "Local TTS client transport cleanup is unconfirmed; admission cannot settle"
            )
        native_bound = getattr(client, "_native_bound", None)
        if callable(native_bound) and not native_bound():
            raise TTSModelConflict(
                "Local TTS client transport binding changed; admission cannot settle"
            )
        if (
            getattr(client, "_native_stt_origin", binding.origin) != binding.origin
            or getattr(client, "profile", binding.profile) != binding.profile
        ):
            raise TTSModelConflict(
                "Configured host TTS provider does not match the recorded admission binding"
            )
        speech_groups = getattr(self.runtime, "_speech", {})
        for group in speech_groups.values():
            for op in group:
                if (op.event_id, op.generation) == (
                    binding.source_event_id,
                    binding.generation_epoch,
                ):
                    if (
                        not op.cancelled
                        or op.cleanup is None
                        or not op.cleanup.done()
                        or op.cleanup_failed
                    ):
                        raise TTSModelConflict(
                            "Original speech operation still owns active or failed local cleanup"
                        )
        matching_leases = [lease for lease in self.leases if lease.admission_id == admission_id]
        turns = set(getattr(self.runtime, "_tts_turns", ())) | set(
            getattr(self.runtime, "_tts_history", {}).values()
        )
        matching_turns = [
            turn
            for turn in turns
            if turn.lease.admission_id == admission_id
            or turn.operation_id == binding.tts_operation_id
        ]
        effective_audio_exposed = (
            binding.audio_exposed
            or any(lease.audio_exposed for lease in matching_leases)
            or any(turn.lease.audio_exposed for turn in matching_turns)
        )
        for lease in matching_leases:
            if lease.cleanup_actual_failed:
                raise TTSModelConflict(
                    "Local TTS transport cleanup failed; admission cannot settle"
                )
            if (
                lease.transport_cleanup is None
                or not lease.transport_cleanup.done()
                or lease.transport_cleanup.cancelled()
                or lease.transport_cleanup.exception() is not None
            ):
                raise TTSModelConflict(
                    "Local TTS transport cleanup must complete before settlement"
                )
            if lease.publication_unknown or lease.playback_pending:
                raise TTSModelConflict(
                    "TTS publication and playback fencing must settle before admission recovery"
                )
            if effective_audio_exposed and not lease.delivery_confirmed:
                raise TTSModelConflict(
                    "Exposed interrupted TTS audio requires confirmed terminal delivery before settlement"
                )
        for turn in matching_turns:
            if (
                not turn.request_done.is_set()
                or turn.producer_cleanup is None
                or not turn.producer_cleanup.done()
                or turn.lease.cleanup_actual_failed
                or turn.lease.publication_unknown
                or turn.lease.playback_pending
                or (effective_audio_exposed and not turn.lease.delivery_confirmed)
            ):
                raise TTSModelConflict(
                    "Local TTS turn cleanup and delivery obligations must settle first"
                )
            pending_pubs = [task for task in turn.publications if not task.done()]
            if pending_pubs:
                raise TTSModelConflict("TTS journal publications are still in flight")
            admitted = turn.admission_publication
            if admitted is not None and (admitted.cancelled() or admitted.exception() is not None):
                raise TTSModelConflict("TTS admission publication is unconfirmed")
        if effective_audio_exposed and not matching_leases and not matching_turns:
            if not await self._journal_confirms_delivery(binding):
                raise TTSModelConflict(
                    "Exposed interrupted TTS audio requires confirmed terminal delivery before settlement"
                )
        return matching_leases, matching_turns, effective_audio_exposed

    async def reconcile_admission(self, request):
        request = TTSAdmissionReconcile.model_validate(
            request.model_dump(mode="json")
            if isinstance(request, TTSAdmissionReconcile)
            else request,
            strict=True,
        )
        self._read()
        if request.expected_backend_id != self.backend_id:
            raise TTSModelConflict(
                "TTS backend changed; original admission belongs to another authority"
            )
        prior = self._settled(request.admission_id)
        if prior is not None:
            if prior[0] != request:
                raise TTSModelConflict(
                    "TTS admission settlement already recorded with different request parameters"
                )
            return {**prior[1], "replayed": True}
        running = self._reconciling.get(request.admission_id)
        if running is not None:
            if running[0] != request:
                raise TTSModelConflict("TTS admission reconciliation identity was reused")
            return await asyncio.shield(running[1])
        if len(self._reconciling) >= 8:
            raise RuntimeError("TTS reconciliation capacity is retained")
        task = asyncio.create_task(self._reconcile_admission(request))
        self._reconciling[request.admission_id] = (request, task)

        def done(completed):
            if not completed.cancelled():
                completed.exception()
            self._reconciling.pop(request.admission_id, None)
            self._finish_close()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def _reconcile_admission(self, request: TTSAdmissionReconcile):
        records = self._admission_records()
        record = records.get(request.admission_id)
        if record is None:
            prior = self._settled(request.admission_id)
            if prior is not None:
                if prior[0] != request:
                    raise TTSModelConflict(
                        "TTS admission settlement already recorded with different request parameters"
                    )
                return {**prior[1], "replayed": True}
            raise TTSModelConflict("Unknown TTS admission was not found")
        identity, stored_binding = record
        pin_sha256 = self._pin_sha256(identity)
        if request.pin_sha256 != pin_sha256:
            raise TTSModelConflict("TTS admission pin digest does not match the recorded admission")
        if stored_binding is None:
            raise TTSModelConflict(
                "TTS admission does not have a verifiable worker operation binding"
            )
        client = self._native_tts_client()
        if client is None:
            raise TTSModelConflict(
                "Configured host TTS provider does not support native settlement lookup"
            )
        await self._check_settlement_fences(request.admission_id, stored_binding, client)
        try:
            raw_proof = await client.lookup_native_tts(stored_binding)
        except Exception as exc:
            raise TTSModelConflict("Native TTS worker settlement lookup failed") from exc
        proof = self._validate_worker_proof(raw_proof, stored_binding)
        post_record = self._admission_records().get(request.admission_id)
        if post_record is None or post_record[0] != identity or post_record[1] is None:
            raise TTSModelConflict("Owned TTS admission settlement changed")
        current_binding = post_record[1]
        if current_binding.model_copy(
            update={"audio_exposed": stored_binding.audio_exposed}
        ) != stored_binding or (stored_binding.audio_exposed and not current_binding.audio_exposed):
            raise TTSModelConflict("Owned TTS admission binding changed")
        proof = self._validate_worker_proof(proof, current_binding)
        expected_binding_json = _encode(current_binding.model_dump(mode="json"))
        (
            matching_leases,
            matching_turns,
            effective_audio_exposed,
        ) = await self._check_settlement_fences(request.admission_id, current_binding, client)
        normalized_binding = current_binding.model_copy(
            update={"audio_exposed": effective_audio_exposed}
        )
        encoded_binding = _encode(normalized_binding.model_dump(mode="json"))
        stored_receipt = TTSAdmissionResult(
            backend_id=self.backend_id,
            admission_id=request.admission_id,
            pin_sha256=pin_sha256,
            worker_instance=current_binding.worker_instance,
            operation_id=current_binding.operation_id,
            worker_outcome=proof.outcome,
            state="retired",
            replayed=False,
        ).model_dump(mode="json")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._read()
            current_record = self._admission_records().get(request.admission_id)
            if (
                current_record is None
                or current_record[0] != identity
                or current_record[1] is None
                or _encode(current_record[1].model_dump(mode="json")) != expected_binding_json
            ):
                raise TTSModelConflict("Owned TTS admission settlement changed")
            count = self.db.execute("SELECT COUNT(*) FROM tts_model_settlements").fetchone()[0]
            if count > RECEIPT_LIMIT:
                raise ValueError("TTS settlement history exceeds its bound")
            if count == RECEIPT_LIMIT:
                self.db.execute(
                    "DELETE FROM tts_model_settlements WHERE rowid=(SELECT MIN(rowid) FROM tts_model_settlements)"
                )
            changed = self.db.execute(
                "DELETE FROM tts_model_admissions WHERE id=? AND identity=? AND binding=?",
                (request.admission_id, identity, expected_binding_json),
            ).rowcount
            if changed != 1:
                raise TTSModelConflict("Owned TTS admission settlement changed")
            self.db.execute(
                "INSERT INTO tts_model_settlements VALUES (?,?,?,?,?,?)",
                (
                    request.admission_id,
                    identity,
                    encoded_binding,
                    _digest(request.model_dump(mode="json")),
                    _encode(request.model_dump(mode="json")),
                    _encode(stored_receipt),
                ),
            )
            self.db.commit()
        except BaseException:
            self._rollback()
            raise
        self._native_unsettled.discard(request.admission_id)
        for lease in matching_leases:
            lease._native_cleanup_unknown = False
            lease.admission_binding = encoded_binding
            if (
                not lease.cleanup_actual_failed
                and not lease.cleanup_unknown
                and not lease.playback_pending
                and lease.transport_cleanup is not None
                and lease.transport_cleanup.done()
            ):
                lease.cleanup_failed = False
                lease.settled = True
                self._released(lease)
        for turn in matching_turns:
            if turn.lease.settled:
                self.runtime._tts_turns.discard(turn)
        return stored_receipt

    async def reconcile_settled_turns(self):
        if self.closed or self._failed or self._db_closed:
            return
        try:
            records = self._admission_records()
        except Exception:
            return
        live = {
            lease.admission_id
            for lease in self.leases
            if not lease.cleanup_failed and not lease.cleanup_unknown
        }
        for admission_id, (identity, binding) in list(records.items()):
            if binding is None or admission_id in live:
                continue
            try:
                await self.reconcile_admission(
                    TTSAdmissionReconcile(
                        expected_backend_id=self.backend_id,
                        admission_id=admission_id,
                        pin_sha256=self._pin_sha256(identity),
                    )
                )
            except (TTSModelConflict, ValueError, RuntimeError):
                pass

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
            "cleanup_pending": len(self._reconciling)
            + sum(
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
        if self.closed and not self.leases and not self._native_unsettled and not self._reconciling:
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
