"""Ordinary Session's durable, original-owner media storage authority.

Recording storage and proof-backed evidence anchors are implemented here.
Runtime declarations never enable capture, decoder start, providers or effects.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field

from packages.contracts import media as m
from packages.contracts.models import AppendRequest, JournalEntry, Session
from services.common.media_wire import IngressBody
from services.session.media_clients import (
    MediaGrantKeyStore,
    MediaOwnerEventHub,
    MediaSessionError,
    NativeMediaPrincipal,
    private_header,
    refuse,
)
from services.session.media_spool import MediaSpool

_MAX = m.MAX_MEDIA_INTEGER
_METADATA = 1_073_741_824
# media_spool._record_bytes returns one fixed4096-byte catalog block;
# reserve adds one block, append adds one >QQQ32s32s ledger record (88bytes),
# and seal/reader/close/delete rewrite the SAME catalog slot. Unknown outcomes
# retain this full anticipated increment; no snapshot refund is made.
_SPOOL_GROWTH = 4096
# Lifetime native bookkeeping for 16 pending/unknown and 64 recent originals,
# each with at most two ordinary-JSON Session snapshots plus fixed overhead.
_SESSION_CHANGE_METADATA = 80 * (2 * m.MAX_MEDIA_CONTROL_BYTES + 4096)
_LEASE = 30.0
_KINDS = {
    "source": "SourceAdmission",
    "upload_ack": "UploadAck",
    "seal": "Seal",
    "task_proof": "TaskProof",
    "anchor": "AnalyzeTaskAnchor",
    "status": "Status",
    "delete": "DeleteReceipt",
    "child_owner": "ChildOwner",
    "cleanup": "WorkerCleanup",
}
_REQUESTS = {
    "admit": "Admit",
    "upload": "UploadPut",
    "seal": "SealRequest",
    "task_proof": "TaskProofRequest",
    "anchor": "AnchorAdmission",
    "control": "Control",
    "delete": "DeleteRequest",
    "child_owner_append": "ChildOwnerAppend",
    "cleanup_append": "WorkerCleanupAppend",
    "status_append": "StatusAppend",
}
_RESULTS = {
    "admit": "source",
    "upload": "upload_ack",
    "seal": "seal",
    "task_proof": "task_proof",
    "anchor": "anchor",
    "control": "status",
    "delete": "delete",
    "child_owner_append": "child_owner",
    "cleanup_append": "cleanup",
    "status_append": "status",
}
_DDL = (
    f"CREATE TABLE media_meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL CHECK(version=1),backend_id TEXT NOT NULL,owner_realm_id TEXT NOT NULL,app_instance TEXT NOT NULL,grant_key_id TEXT NOT NULL,grant_key_sha256 TEXT NOT NULL,poisoned INTEGER NOT NULL CHECK(poisoned IN(0,1)),spool_metadata_origin INTEGER NOT NULL CHECK(spool_metadata_origin BETWEEN 0 AND {_METADATA}))",
    f"CREATE TABLE media_owner_auth(id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND {_MAX}),auth_instance TEXT NOT NULL,credential_fingerprint TEXT NOT NULL,app_instance TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN('current','fenced','unknown')))",
    f"CREATE TABLE media_clients(client_instance TEXT PRIMARY KEY,owner_realm_id TEXT NOT NULL,auth_generation INTEGER NOT NULL CHECK(auth_generation BETWEEN 1 AND {_MAX}),app_instance TEXT NOT NULL,session_id TEXT NOT NULL,thread_id TEXT NOT NULL,epoch INTEGER NOT NULL CHECK(epoch BETWEEN 1 AND {_MAX}),original_key TEXT NOT NULL UNIQUE,nonce_sha256 TEXT NOT NULL UNIQUE,grant_sha256 TEXT NOT NULL,connection_instance TEXT UNIQUE,connection_state TEXT NOT NULL CHECK(connection_state IN('unbound','connected','fenced','settled','unknown')),source_id TEXT NOT NULL UNIQUE,lease_generation INTEGER NOT NULL CHECK(lease_generation BETWEEN 1 AND {_MAX}),display_expires_at_ms INTEGER NOT NULL CHECK(display_expires_at_ms BETWEEN 0 AND {_MAX}))",
    f"CREATE TABLE media_operations(key TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN('admit','upload','seal','task_proof','anchor','control','delete','child_owner_append','cleanup_append','status_append')),request_sha256 TEXT NOT NULL,canonical_request BLOB NOT NULL,owner_realm_id TEXT NOT NULL,client_instance TEXT,source_id TEXT,original_operation_id TEXT NOT NULL UNIQUE,original_app_instance TEXT NOT NULL,candidate_receipt_id TEXT NOT NULL UNIQUE,phase TEXT NOT NULL CHECK(phase IN('prepared','effect_pending','committed','refused','unknown')),result_receipt_id TEXT,error_code TEXT,original_fence BLOB,charged_metadata_bytes INTEGER NOT NULL CHECK(charged_metadata_bytes BETWEEN 0 AND {_METADATA}))",
    "CREATE TABLE media_receipts(receipt_id TEXT PRIMARY KEY,kind TEXT NOT NULL,key TEXT NOT NULL UNIQUE,request_sha256 TEXT NOT NULL,sha256 TEXT NOT NULL,canonical_receipt BLOB NOT NULL,owner_realm_id TEXT NOT NULL,session_id TEXT NOT NULL,thread_id TEXT NOT NULL,epoch INTEGER NOT NULL,original_role TEXT NOT NULL,original_instance TEXT NOT NULL,original_operation_id TEXT NOT NULL)",
    "CREATE TABLE media_catalog(kind TEXT NOT NULL,sha256 TEXT NOT NULL,canonical_value BLOB NOT NULL,registration_owner TEXT NOT NULL,PRIMARY KEY(kind,sha256))",
    f"CREATE TABLE media_reservations(reservation_id TEXT PRIMARY KEY,source_id TEXT NOT NULL UNIQUE,owner_realm_id TEXT NOT NULL,policy_sha256 TEXT NOT NULL,spool_bytes INTEGER NOT NULL CHECK(spool_bytes BETWEEN 1 AND {_MAX}),metadata_bytes INTEGER NOT NULL CHECK(metadata_bytes BETWEEN 0 AND {_METADATA}),child_slots INTEGER NOT NULL CHECK(child_slots BETWEEN 0 AND 16),state TEXT NOT NULL CHECK(state IN('held','unknown','released')))",
    "CREATE TABLE media_sources(source_id TEXT PRIMARY KEY,admission_receipt_id TEXT NOT NULL UNIQUE,owner_realm_id TEXT NOT NULL,client_instance TEXT NOT NULL,session_id TEXT NOT NULL,thread_id TEXT NOT NULL,epoch INTEGER NOT NULL,anchor_receipt_id TEXT,profile_sha256 TEXT NOT NULL,policy_sha256 TEXT NOT NULL,reservation_id TEXT NOT NULL UNIQUE,current_fence BLOB NOT NULL,equivalence_id TEXT,desired TEXT NOT NULL CHECK(desired IN('running','stopped','deleted')),local_actual TEXT NOT NULL CHECK(local_actual IN('reserved','uploading','sealed','ready','running','fenced','closing','settled','unknown','deleted')),raw_state TEXT NOT NULL CHECK(raw_state IN('buffered','retained','expired','delete_pending','deleted')),current_status_receipt_id TEXT,logical_deleted INTEGER NOT NULL CHECK(logical_deleted IN(0,1)))",
    "CREATE TABLE media_dependencies(dependency_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,original_operation_id TEXT NOT NULL,kind TEXT NOT NULL,original_instance TEXT NOT NULL,child_owner_receipt_id TEXT,state TEXT NOT NULL CHECK(state IN('promised','active','settled','unknown')),cleanup_receipt_id TEXT,publication_state TEXT NOT NULL CHECK(publication_state IN('pending','settled','unknown')))",
    "CREATE TABLE media_objective_anchors(owner_objective_sha256 TEXT PRIMARY KEY,owner_objective BLOB NOT NULL,owner_realm_id TEXT NOT NULL,session_id TEXT NOT NULL,thread_id TEXT NOT NULL,epoch INTEGER NOT NULL,task_submit_event_id TEXT NOT NULL,task_id TEXT NOT NULL,initial_objective_revision INTEGER NOT NULL,task_proof_receipt_id TEXT NOT NULL,anchor_receipt_id TEXT NOT NULL UNIQUE)",
    "CREATE TABLE media_equivalences(owner_objective_sha256 TEXT NOT NULL,source_sha256 TEXT NOT NULL,source_bytes INTEGER NOT NULL,equivalence_id TEXT NOT NULL UNIQUE,original_seal_receipt_id TEXT NOT NULL,PRIMARY KEY(owner_objective_sha256,source_sha256,source_bytes))",
    f'CREATE TABLE media_uploads(source_id TEXT NOT NULL,"index" INTEGER NOT NULL CHECK("index" BETWEEN 0 AND {_MAX}),key TEXT NOT NULL,offset INTEGER NOT NULL CHECK(offset BETWEEN 0 AND {_MAX}),body_bytes INTEGER NOT NULL CHECK(body_bytes BETWEEN 1 AND 4194304),body_sha256 TEXT NOT NULL,next_offset INTEGER NOT NULL CHECK(next_offset BETWEEN 1 AND {_MAX}),ack_receipt_id TEXT NOT NULL,PRIMARY KEY(source_id,"index"),UNIQUE(source_id,key))',
)


def native_ms():
    value = time.time_ns() // 1_000_000
    if not 0 <= value <= _MAX:
        refuse()
    return value


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _fresh(value, kind):
    if type(value) is not kind:
        refuse("bad_packet")
    return m.media_json(m.media_bytes(value), kind)


def _parsed_media_bytes(value):
    # Only a fresh, unexposed media_json result (or its unchanged child) may
    # enter here. Parsing already ran complete semantic/digest validation;
    # retain full actual-graph/canonical bounds without reconstructing it again.
    try:
        return m._canonical(value)
    except m.MediaContractError:
        raise
    except m.ValidationError as error:
        raise m._normalize_error(error) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise m.MediaContractError() from None


def _parsed_media_digest(short, value):
    # Same freshness rule as above; never replace public typed-input validation.
    # Keep the codec's registered domain and omission of its one root self hash.
    try:
        if type(short) is not str or m._BY_SHORT.get(short) is not type(value):
            raise m.MediaContractError()
        return m._hash(short, value.__dict__, m._SELF.get(type(value)))
    except m.MediaContractError:
        raise
    except m.ValidationError as error:
        raise m._normalize_error(error) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise m.MediaContractError() from None


def _ref(backend, receipt):
    return m.MediaNativeRef(backend_id=backend, receipt_id=receipt.id, sha256=receipt.sha256)


@dataclass(slots=True)
class OriginalOperation:
    key: str
    digest: str
    kind: str
    original_id: str
    candidate_id: str
    session_id: str
    private_binding: str = ""
    source_id: str | None = None
    client_instance: str | None = None
    task: asyncio.Task | None = None
    assigned: asyncio.Event = field(default_factory=asyncio.Event)
    fenced: threading.Event = field(default_factory=threading.Event)
    result: object = None
    effect_result: object = None
    unknown: bool = False


@dataclass(frozen=True, slots=True)
class _CleanupCheckpoint:
    task: asyncio.Task
    upload: object
    connection: object


@dataclass(frozen=True, slots=True)
class _MediaCleanupAuthority:
    request: object
    principal: NativeMediaPrincipal
    original: OriginalOperation
    fence: object


@dataclass(frozen=True, slots=True)
class _SessionCleanupAuthority:
    change: _SessionChange
    dispatch: _SessionChangeDispatch
    session: bytes
    reference: object
    fence: object


@dataclass(slots=True)
class _CleanupProbe:
    checkpoint: _CleanupCheckpoint
    authority: _MediaCleanupAuthority | _SessionCleanupAuthority
    loop: asyncio.AbstractEventLoop
    marker: object = field(default_factory=object)
    assigned: asyncio.Event = field(default_factory=asyncio.Event)
    placeholder: asyncio.Future | None = None
    coroutine: object = None
    task: asyncio.Task | None = None
    entered_task: asyncio.Task | None = None
    completed_task: asyncio.Task | None = None
    outcome: tuple | None = None
    setup_complete: bool = False
    no_dispatch: bool = False
    retired: bool = False
    unknown: bool = False


@dataclass(slots=True)
class _CleanupContinuation:
    checkpoint: _CleanupCheckpoint
    authority: _MediaCleanupAuthority | _SessionCleanupAuthority
    loop: asyncio.AbstractEventLoop
    assigned: asyncio.Event = field(default_factory=asyncio.Event)
    placeholder: asyncio.Future | None = None
    coroutine: object = None
    task: asyncio.Task | None = None
    entered_task: asyncio.Task | None = None
    completed_task: asyncio.Task | None = None
    completion_task: asyncio.Task | None = None
    unknown: bool = False

    @property
    def original(self):
        authority = self.authority
        return authority.original if type(authority) is _MediaCleanupAuthority else authority.change


@dataclass(slots=True)
class _Source:
    admission: object
    upload: object
    expires: float
    original_operation_id: str
    fenced: threading.Event = field(default_factory=threading.Event)
    reason: str = "none"
    connection: object = None
    sealed_result: tuple | None = None
    delete_task: asyncio.Future | None = None
    children: dict = field(default_factory=dict)
    reference: object = None
    original: OriginalOperation | None = None
    current_fence: object = None
    expiry_task: asyncio.Task | None = None
    created_at: float = 0.0
    last_upload_at: float = 0.0
    sealed_at: float | None = None
    cleanup_checkpoint: _CleanupCheckpoint | None = None
    cleanup_probe: _CleanupProbe | None = None
    cleanup_continuation: _CleanupContinuation | None = None


@dataclass(slots=True)
class _CloseOperation:
    loop: asyncio.AbstractEventLoop
    deadline: float
    task: asyncio.Task | None = None
    setup_error: BaseException | None = None
    phases: dict = field(default_factory=dict)
    originals: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    retained: set = field(default_factory=set)
    unknown: bool = False
    sealed: bool = False


@dataclass(slots=True, eq=False)
class _SessionChangeDispatch:
    loop: asyncio.AbstractEventLoop
    ordinal: int
    previous: int | None
    marker: object = field(default_factory=object)
    assigned: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    coroutine: object = None
    entered_task: asyncio.Task | None = None
    completed_task: asyncio.Task | None = None
    worker_assigned: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None
    worker_entered_task: asyncio.Task | None = None
    worker_completed_task: asyncio.Task | None = None
    worker_result: tuple | None = None
    worker_coroutine: object = None
    setup_error: BaseException | None = None
    outcome: str = "pending"
    error_code: str | None = None
    no_dispatch: bool = False
    unknown: bool = False
    setup_complete: bool = False
    retired: bool = False


@dataclass(slots=True, eq=False)
class _SessionChange:
    session_id: str
    reason: str
    state: str = "pending"
    original: bytes | None = None
    result: bytes | None = None
    error_code: str | None = None
    running: bool = False
    fence_committed: bool = False
    first_dispatch: _SessionChangeDispatch | None = None
    dispatch: _SessionChangeDispatch | None = None
    completed_checks: int = 0


@dataclass(frozen=True, slots=True)
class _SessionChangeView:
    _session: bytes
    state: str
    _result: bytes | None
    error_code: str | None
    replayed: bool

    @property
    def session(self):
        return Session.model_validate_json(self._session)

    @property
    def result(self):
        return None if self._result is None else Session.model_validate_json(self._result)


class MediaSessionOwner:
    def __init__(
        self,
        store,
        control,
        spool,
        *,
        auth,
        selected_limits: m.MediaLimits,
        selected_profile: m.MediaDecoderProfile,
        grant_key_store: MediaGrantKeyStore,
        clock=time.monotonic,
        wall_clock_ms=native_ms,
    ):
        if type(spool) is not MediaSpool or type(grant_key_store) is not MediaGrantKeyStore:
            refuse("bad_packet")
        self._store, self._control, self.spool, self.auth = store, control, spool, auth
        self.limits = _fresh(selected_limits, m.MediaLimits)
        self.profile = _fresh(selected_profile, m.MediaDecoderProfile)
        self._limits_pin, self._profile_pin = (
            m.media_bytes(self.limits),
            m.media_bytes(self.profile),
        )
        self.policy_sha256 = m.media_digest("Limits", self.limits)
        if m.media_bytes(self.profile.limits) != self._limits_pin:
            refuse("conflict")
        self.keys = grant_key_store
        self.clock, self.wall_clock_ms = clock, wall_clock_ms
        self.backend_id = control.backend()  # Public transaction, only at construction.
        # Native page geometry bounds SQLite row/index split/overflow slack.
        # It is never caller-selected or inferred from a spool snapshot. A
        # changed database geometry fails the persisted-charge history checks.
        with store.lock:
            page_bytes = store.db.execute("PRAGMA page_size").fetchone()[0]
        if (
            type(page_bytes) is not int
            or not 512 <= page_bytes <= 65_536
            or page_bytes & (page_bytes - 1)
        ):
            refuse()
        self._base_charge = 64 * page_bytes + _SESSION_CHANGE_METADATA
        self._unit_charge = 2 * 65_536 + 64 * page_bytes + _SPOOL_GROWTH
        self._intake_charge = 16 * page_bytes + _SPOOL_GROWTH
        self._charges = {
            kind: (
                5 * self._unit_charge
                if kind == "admit"
                else 0
                if kind in {"child_owner_append", "cleanup_append"}
                else self._unit_charge
            )
            for kind in _REQUESTS
        }
        self.app_instance = secrets.token_hex(16)
        self._principal_issuer = object()
        self._auth_identity = None
        self._auth_generation = None
        self._bound_control = None
        self._poisoned = self._closed = self._closing = False
        self._initialized = False
        self._mutex = threading.RLock()
        self._local = threading.local()
        self._sources = {}
        self._operations = OrderedDict()
        self._native_jobs = {}
        self._async_lock = asyncio.Lock()
        self._pending_bytes = 0
        self._intakes = {}
        self._loop = None
        self._retirement_requests = {}
        self._retirement_unknown = False
        self._close_operation = None
        self._session_changes = {}
        self._session_change_recent = OrderedDict()
        self._session_change_jobs = {}
        # This is the ONLY spool accounting snapshot, before Store.lock. Every
        # subsequent possible catalog/ledger effect has an original precharge.
        self._spool_metadata_snapshot = spool.metadata_bytes
        self._spool_reserved_snapshot = spool.reserved_bytes
        self._spool_metadata_origin = self._spool_metadata_snapshot
        self._metadata_hold = self._base_charge + self._spool_metadata_origin
        self.event_hub = MediaOwnerEventHub(self)
        self._initialize()
        self._initialized = True

    @property
    def store(self):
        return self._store

    def belongs_to_store(self, store) -> bool:
        return self._store is store

    def _sql(self, statement, args=()):
        try:
            return self.store.db.execute(statement, args)
        except Exception:
            self._poisoned = True
            refuse()

    @contextmanager
    def _boundary(self):
        with self.store.lock:
            connection = self.store.db
            if self._poisoned or connection.in_transaction:
                refuse()
            token = object()
            self._local.token = token
            metadata_before = self._metadata_hold
            try:
                connection.execute("BEGIN IMMEDIATE")
                if self._initialized:
                    self._audit(token)
                yield token
                if self._initialized:
                    self._audit(token)
                try:
                    connection.commit()
                except BaseException:
                    self._poisoned = True  # Commit may already have taken effect.
                    raise
            except BaseException as error:
                try:
                    connection.rollback()
                except BaseException:
                    self._poisoned = True
                if not self._poisoned:
                    self._metadata_hold = metadata_before  # Positive no-effect rollback.
                if isinstance(error, m.MediaContractError):
                    self._poisoned = True
                    refuse()
                raise
            finally:
                self._local.token = None

    def _locked(self, token):
        if token is not getattr(self._local, "token", None) or token is None:
            refuse()
        if not self.store.db.in_transaction:
            refuse()

    def _initialize(self):
        if self._metadata_hold > _METADATA:
            refuse("capacity")
        with self._boundary() as token:
            names = {
                row[0]
                for row in self._sql(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND substr(name,1,6)='media_'"
                )
            }
            expected = {statement.split("(", 1)[0].split()[-1] for statement in _DDL}
            if names and names != expected:
                refuse()
            if not names:
                if self._spool_reserved_snapshot:
                    refuse()  # Unknown original bytes cannot acquire a fresh SQL authority.
                for statement in _DDL:
                    self._sql(statement)
                self.owner_realm_id = secrets.token_hex(16)
                self._sql(
                    "INSERT INTO media_meta VALUES(1,1,?,?,?,?,?,0,?)",
                    (
                        self.backend_id,
                        self.owner_realm_id,
                        self.app_instance,
                        self.keys.key_id,
                        self.keys.key_sha256,
                        self._spool_metadata_origin,
                    ),
                )
            else:
                # Exact DDL/constraints are part of this private version1 schema.
                for statement in _DDL:
                    name = statement.split("(", 1)[0].split()[-1]
                    row = self._sql(
                        "SELECT sql FROM sqlite_schema WHERE name=?", (name,)
                    ).fetchone()
                    if row is None or row[0] != statement:
                        refuse()
                rows = self._sql("SELECT * FROM media_meta").fetchall()
                if len(rows) != 1:
                    refuse()
                row = rows[0]
                if (
                    row["id"] != 1
                    or row["version"] != 1
                    or row["poisoned"] != 0
                    or row["backend_id"] != self.backend_id
                    or row["grant_key_id"] != self.keys.key_id
                    or row["grant_key_sha256"] != self.keys.key_sha256
                ):
                    refuse()
                self.owner_realm_id = row["owner_realm_id"]
                self._spool_metadata_origin = row["spool_metadata_origin"]
                total = self._sql(
                    "SELECT COALESCE(SUM(charged_metadata_bytes),0) FROM media_operations"
                ).fetchone()[0]
                total += self._intake_metadata() + self._catalog_metadata()
                possible_spool = (
                    self._sql(
                        "SELECT COUNT(*) FROM media_operations WHERE kind IN('admit','upload')"
                    ).fetchone()[0]
                    * _SPOOL_GROWTH
                )
                if self._spool_metadata_snapshot > self._spool_metadata_origin + possible_spool:
                    refuse()
                held = self._sql(
                    "SELECT COALESCE(SUM(spool_bytes),0) FROM media_reservations WHERE state!='released'"
                ).fetchone()[0]
                if self._spool_reserved_snapshot > held:
                    refuse()
                self._metadata_hold = self._base_charge + self._spool_metadata_origin + total
                self._audit(token)
                self._sql(
                    "UPDATE media_operations SET phase='unknown' WHERE phase IN('prepared','effect_pending')"
                )
                self._sql(
                    "UPDATE media_clients SET connection_state='unknown' WHERE connection_state!='settled'"
                )
                self._sql(
                    "UPDATE media_dependencies SET state='unknown',publication_state='unknown' WHERE state!='settled' OR publication_state!='settled'"
                )
                self._sql("UPDATE media_reservations SET state='unknown' WHERE state!='released'")
                self._sql(
                    "UPDATE media_sources SET desired='stopped',local_actual='unknown' WHERE logical_deleted=0"
                )
                self._sql("UPDATE media_owner_auth SET state='fenced'")
                self._sql("UPDATE media_meta SET app_instance=? WHERE id=1", (self.app_instance,))
            for kind, digest, raw in (
                ("limits", self.policy_sha256, self._limits_pin),
                ("decoder_profile", self.profile.profile_sha256, self._profile_pin),
            ):
                old = self._sql(
                    "SELECT canonical_value FROM media_catalog WHERE kind=? AND sha256=?",
                    (kind, digest),
                ).fetchone()
                if old is not None and bytes(old[0]) != raw:
                    refuse()
                if old is None:
                    if self._metadata_hold + self._unit_charge > _METADATA:
                        refuse("capacity")
                    self._metadata_hold += self._unit_charge
                self._sql(
                    "INSERT OR IGNORE INTO media_catalog VALUES(?,?,?,?)",
                    (kind, digest, raw, self.app_instance),
                )

    def bind_app(self, control, auth):
        if self._bound_control is not None:
            if control is self._bound_control and auth is self.auth:
                return
            refuse("conflict")
        if auth is not self.auth or control.store is not self.store or not control.instance:
            refuse()
        with self._boundary():
            row = control.metadata()
            if row["backend_id"] != self.backend_id or row["instance_id"] != control.instance:
                refuse()
            self._control = self._bound_control = control
        self.register_native_auth(auth)

    def register_native_auth(self, auth):
        if auth is not self.auth or self._bound_control is None:
            refuse()
        identity = auth.media_identity()
        if self._auth_identity is identity:
            return identity
        if self._auth_identity is not None:
            refuse("conflict")
        fingerprint = self.keys.authenticate_digest(
            b"kinaiya.media.auth-fingerprint.v1\0", identity.credential_fingerprint
        )
        with self._boundary():
            previous = self._sql("SELECT * FROM media_owner_auth WHERE id=1").fetchone()
            generation = 1 if previous is None else previous["generation"] + 1
            if generation > _MAX:
                refuse("bound")
            self._sql(
                "UPDATE media_clients SET connection_state='fenced' WHERE connection_state NOT IN('settled','unknown')"
            )
            self._sql(
                "UPDATE media_sources SET desired='stopped',local_actual='fenced' WHERE logical_deleted=0 AND local_actual!='unknown'"
            )
            self._sql(
                "INSERT OR REPLACE INTO media_owner_auth VALUES(1,?,?,?,?, 'current')",
                (
                    generation,
                    identity.instance_id,
                    fingerprint,
                    self.app_instance,
                ),
            )
            self._auth_generation, self._auth_identity = generation, identity
        return identity

    def _authority(self):
        if (
            self._poisoned
            or self._closed
            or self._closing
            or self._bound_control is None
            or self._auth_identity is None
        ):
            refuse()
        try:
            if self.auth.media_identity() is not self._auth_identity:
                refuse()
            row = self._control.metadata()
            if row["instance_id"] != self._control.instance or row["backend_id"] != self.backend_id:
                refuse()
        except Exception:
            self._fence_all_memory("replacement")
            refuse()
        row = self._sql("SELECT * FROM media_owner_auth WHERE id=1").fetchone()
        if (
            row is None
            or row["generation"] != self._auth_generation
            or row["app_instance"] != self.app_instance
            or row["state"] != "current"
            or row["auth_instance"] != self._auth_identity.instance_id
            or row["credential_fingerprint"]
            != self.keys.authenticate_digest(
                b"kinaiya.media.auth-fingerprint.v1\0", self._auth_identity.credential_fingerprint
            )
        ):
            refuse()

    def _principal_memory(self, principal, roles):
        if (
            self._poisoned
            or self._closed
            or self._closing
            or self._bound_control is None
            or self._auth_identity is None
        ):
            refuse()
        if self.auth.media_identity() is not self._auth_identity:
            self._fence_all_memory("replacement")
            refuse()
        if (
            type(principal) is not NativeMediaPrincipal
            or principal.issuer is not self._principal_issuer
            or principal.auth_identity is not self._auth_identity
            or principal.auth_generation != self._auth_generation
            or principal.app_instance != self.app_instance
            or principal.role not in roles
        ):
            refuse("denied")

    def _principal(self, principal, roles):
        self._principal_memory(principal, roles)
        self._authority()

    async def authenticate(self, request, roles, *, capability=False):
        if self._closing:
            refuse()
        # Run the actual configured Auth dependency, not a supplied role string.
        authorization = request.headers.get("authorization", "")
        if len(authorization) > 8192:
            refuse("bad_packet")
        role = await self.auth.require(*roles)(request, authorization)
        try:
            identity = self.auth.media_identity()
        except Exception:
            refuse()
        if identity is not self._auth_identity:
            refuse()
        grant = private_header(
            request.headers, "X-Kinaiya-Media-Client-Capability", required=capability
        )
        return NativeMediaPrincipal(
            self._principal_issuer, identity, self._auth_generation, self.app_instance, role, grant
        )

    def authenticate_websocket(self, ws, token, capability):
        if self._closing:
            refuse()
        origin = ws.headers.get("origin")
        if origin and origin not in self.auth.allowed_origins:
            refuse("denied")
        if self.auth.authenticate(token) != "owner":
            refuse("denied")
        identity = self.auth.media_identity()
        if identity is not self._auth_identity:
            refuse()
        return NativeMediaPrincipal(
            self._principal_issuer,
            identity,
            self._auth_generation,
            self.app_instance,
            "owner",
            capability,
        )

    @contextmanager
    def _transaction(self, principal, roles):
        self._principal_memory(principal, roles)
        with self._boundary() as token:
            self._principal(principal, roles)
            yield token
            self._principal(principal, roles)

    def _audit(self, token):
        try:
            self._validate_history(token)
        except BaseException as error:
            self._poisoned = True
            if not isinstance(error, Exception):
                raise
            refuse()

    def _validate_history(self, token):
        self._locked(token)
        meta = self._sql("SELECT * FROM media_meta WHERE id=1").fetchone()
        if (
            meta is None
            or meta["backend_id"] != self.backend_id
            or meta["poisoned"] != 0
            or meta["owner_realm_id"] != self.owner_realm_id
            or meta["grant_key_id"] != self.keys.key_id
            or meta["grant_key_sha256"] != self.keys.key_sha256
        ):
            refuse()
        total = self._sql(
            "SELECT COALESCE(SUM(charged_metadata_bytes),0) FROM media_operations"
        ).fetchone()[0]
        total += self._intake_metadata() + self._catalog_metadata()
        if (
            type(total) is not int
            or total < 0
            or self._base_charge + self._spool_metadata_origin + total > self._metadata_hold
            or self._metadata_hold > _METADATA
        ):
            refuse()
        for table, column, maximum in (
            ("media_receipts", "canonical_receipt", 65_536),
            ("media_operations", "canonical_request", 262_144),
            ("media_catalog", "canonical_value", 65_536),
            ("media_objective_anchors", "owner_objective", 65_536),
            ("media_sources", "current_fence", 65_536),
        ):
            # Refuse oversized corrupt storage before fetching its full bytes
            # into Python. Native count/page bounds remain independent.
            size, count = self._sql(
                f"SELECT COALESCE(MAX(length({column})),0),COUNT(*) FROM {table}"
            ).fetchone()
            if size > maximum or count > _METADATA // 128:
                refuse()
        after = ""
        while True:
            rows = self._sql(
                "SELECT * FROM media_receipts WHERE receipt_id>? ORDER BY receipt_id LIMIT 64",
                (after,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                after = row["receipt_id"]
                self._read_receipt(row)
        after = ""
        while True:
            rows = self._sql(
                "SELECT * FROM media_operations WHERE key>? ORDER BY key LIMIT 64", (after,)
            ).fetchall()
            if not rows:
                break
            for row in rows:
                after = row["key"]
                if (
                    row["kind"] not in _REQUESTS
                    or row["charged_metadata_bytes"] != self._charges[row["kind"]]
                ):
                    refuse()
                for identifier in (
                    row["original_operation_id"],
                    row["candidate_receipt_id"],
                    row["original_app_instance"],
                ):
                    if (
                        type(identifier) is not str
                        or len(identifier) != 32
                        or any(char not in "0123456789abcdef" for char in identifier)
                    ):
                        refuse()
                kind = getattr(m, "Media" + _REQUESTS[row["kind"]])
                request = m.media_json(bytes(row["canonical_request"]), kind)
                if (
                    _parsed_media_bytes(request) != bytes(row["canonical_request"])
                    or request.key != row["key"]
                    or _parsed_media_digest(_REQUESTS[row["kind"]], request)
                    != row["request_sha256"]
                    or row["owner_realm_id"] != self.owner_realm_id
                ):
                    refuse()
                if row["phase"] == "committed":
                    result = self._sql(
                        "SELECT * FROM media_receipts WHERE receipt_id=?",
                        (row["result_receipt_id"],),
                    ).fetchone()
                    if (
                        result is None
                        or result["key"] != row["key"]
                        or result["request_sha256"] != row["request_sha256"]
                        or result["original_operation_id"] != row["original_operation_id"]
                        or result["kind"] != _RESULTS[row["kind"]]
                    ):
                        refuse()
                    allowed = {
                        "seal": {"converse"},
                        "task_proof": {"owner", "agent"},
                        "child_owner_append": {"session"},
                        "cleanup_append": {"session"},
                        "status_append": {"session"},
                    }.get(row["kind"], {"owner"})
                    if (
                        result["original_role"] not in allowed
                        or result["original_instance"] != row["original_app_instance"]
                    ):
                        refuse()
                    parsed = self._read_receipt(result)
                    if row["kind"] in {"admit", "task_proof", "anchor"}:
                        session = parsed.body.session
                    elif row["kind"] in {"control", "delete", "status_append"}:
                        session = request.fence.session
                    else:
                        session = self._lookup_ref(token, request.source, "source").body.session
                    if (result["session_id"], result["thread_id"], result["epoch"]) != (
                        session.session_id,
                        session.thread_id,
                        session.epoch,
                    ):
                        refuse()
                    self._validate_result_binding(token, row, request, parsed)
        self._validate_links(token)

    def _validate_result_binding(self, token, operation, request, receipt):
        self._locked(token)
        body, kind = receipt.body, operation["kind"]
        original_fence = getattr(request, "fence", None)
        expected_fence = None if original_fence is None else _parsed_media_bytes(original_fence)
        if (
            None if operation["original_fence"] is None else bytes(operation["original_fence"])
        ) != expected_fence or receipt.id != operation["candidate_receipt_id"]:
            refuse()
        if kind == "admit":
            if (
                body.admission_id != operation["original_operation_id"]
                or body.source_id != operation["source_id"]
                or body.client_instance != operation["client_instance"]
                or body.source_equivalence_id is not None
                or body.source_revision != 1
                or body.capture_instance is not None
                or body.kind != "recording"
                or (
                    body.session,
                    body.kind,
                    body.purpose,
                    body.anchor,
                    body.replaces_source,
                    body.scope,
                    body.decoder_profile_sha256,
                    body.raw_retain_until_ms,
                )
                != (
                    request.session,
                    request.kind,
                    request.purpose,
                    request.anchor,
                    request.replaces_source,
                    request.scope,
                    request.decoder_profile_sha256,
                    request.raw_retain_until_ms,
                )
                or body.fence
                != m.MediaFence(
                    session=body.session, source_revision=1, generation=1, lease_generation=1
                )
                or body.reservation.metadata_bytes != self._charges["admit"]
                or body.reservation.child_slots != 1
            ):
                refuse()
            limits = self._sql(
                "SELECT canonical_value FROM media_catalog WHERE kind='limits' AND sha256=?",
                (body.reservation.policy_sha256,),
            ).fetchone()
            if limits is None:
                refuse()
            policy = m.media_json(bytes(limits[0]), m.MediaLimits)
            expected = (
                policy.source.file_bytes
                if request.declared_bytes is None
                else request.declared_bytes
            )
            if (
                body.reservation.owner_spool_bytes != expected
                or body.reservation.global_spool_bytes != expected
            ):
                refuse()
        elif kind == "upload":
            if any(
                getattr(body, name) != getattr(request, name)
                for name in ("source", "index", "offset", "body_bytes", "body_sha256")
            ):
                refuse()
        elif kind == "seal":
            source = self._lookup_ref(token, request.source, "source").body
            if (
                body.source != request.source
                or body.fence != request.fence
                or body.bytes != request.final_bytes
                or body.source_sha256 != request.source_sha256
                or body.reservation_id != source.reservation.reservation_id
                or body.decoder_profile_sha256 != source.decoder_profile_sha256
            ):
                refuse()
        elif kind == "task_proof":
            if (
                body.session != request.session
                or body.original_admission_id != request.original_admission_id
            ):
                refuse()
            original = self._sql(
                "SELECT * FROM entries WHERE event_id=?", (body.original_admission_id,)
            ).fetchone()
            if original is not None:
                entry = self.store._agent_submit_check_row(original)
                if (
                    entry.payload["task_id"] != body.task_id
                    or entry.payload["objective_revision"] != body.objective_revision
                    or hashlib.sha256(original["request"].encode("utf-8")).hexdigest()
                    != body.request_sha256
                ):
                    refuse()
        elif kind == "anchor":
            if request.submit is not None or (
                body.owner_objective,
                body.scope,
                body.task_admission,
            ) != (request.owner_objective, request.scope, request.task_admission):
                refuse()
            proof = self._lookup_ref(token, body.task_admission, "task_proof").body
            if (
                body.session,
                body.task_id,
                body.initial_objective_revision,
                body.owner_objective,
            ) != (proof.session, proof.task_id, proof.objective_revision, proof.owner_objective):
                refuse()
        elif kind in {"control", "delete"}:
            if (
                body.source != request.source
                or body.fence.session != request.fence.session
                or body.fence.source_revision != request.fence.source_revision
                or body.fence.generation < request.fence.generation
                or body.fence.lease_generation < request.fence.lease_generation
            ):
                refuse()
        elif kind in {"child_owner_append", "cleanup_append", "status_append"}:
            if m.media_bytes(body) != m.media_bytes(request.value):
                refuse()
        else:
            refuse()

    def _validate_links(self, token):
        self._locked(token)
        for row in self._sql(
            "SELECT r.*,o.kind AS operation_kind,o.phase,o.result_receipt_id FROM media_receipts r LEFT JOIN media_operations o ON o.key=r.key"
        ):
            if (
                row["operation_kind"] not in _RESULTS
                or row["phase"] != "committed"
                or row["result_receipt_id"] != row["receipt_id"]
                or _RESULTS[row["operation_kind"]] != row["kind"]
            ):
                refuse()
        if self._sql(
            "SELECT 1 FROM media_operations WHERE kind IN('child_owner_append','cleanup_append') GROUP BY source_id,kind HAVING COUNT(*)>2 LIMIT 1"
        ).fetchone():
            refuse()
        for row in self._sql("SELECT * FROM media_catalog"):
            short = {"limits": "Limits", "decoder_profile": "DecoderProfile"}.get(row["kind"])
            if short is None:
                refuse()
            value = m.media_json(bytes(row["canonical_value"]), getattr(m, "Media" + short))
            if (
                _parsed_media_bytes(value) != bytes(row["canonical_value"])
                or _parsed_media_digest(short, value) != row["sha256"]
            ):
                refuse()
        for row in self._sql("SELECT * FROM media_clients"):
            operation = self._sql(
                "SELECT * FROM media_operations WHERE key=?", (row["original_key"],)
            ).fetchone()
            if (
                operation is None
                or operation["kind"] != "admit"
                or operation["source_id"] != row["source_id"]
                or operation["client_instance"] != row["client_instance"]
                or operation["owner_realm_id"] != row["owner_realm_id"]
                or row["owner_realm_id"] != self.owner_realm_id
                or row["app_instance"] != operation["original_app_instance"]
            ):
                refuse()
            admitted = m.media_json(bytes(operation["canonical_request"]), m.MediaAdmit)
            if (row["session_id"], row["thread_id"], row["epoch"]) != (
                admitted.session.session_id,
                admitted.session.thread_id,
                admitted.session.epoch,
            ):
                refuse()
            if (
                type(row["auth_generation"]) is not int
                or not 1 <= row["auth_generation"] <= _MAX
                or type(row["nonce_sha256"]) is not str
                or len(row["nonce_sha256"]) != 64
                or type(row["grant_sha256"]) is not str
                or len(row["grant_sha256"]) != 64
            ):
                refuse()
            if operation["phase"] == "committed":
                if not secrets.compare_digest(
                    hashlib.sha256(self._grant(row).encode("ascii")).hexdigest(),
                    row["grant_sha256"],
                ):
                    refuse()
        for _row in self._sql(
            "SELECT r.* FROM media_reservations r LEFT JOIN media_operations o ON o.source_id=r.source_id AND o.kind='admit' WHERE o.key IS NULL OR o.owner_realm_id!=r.owner_realm_id"
        ):
            refuse()
        if self._sql(
            "SELECT 1 FROM media_dependencies d LEFT JOIN media_clients c ON c.source_id=d.source_id WHERE c.source_id IS NULL LIMIT 1"
        ).fetchone():
            refuse()
        if self._sql(
            "SELECT 1 FROM media_uploads u LEFT JOIN media_sources s ON s.source_id=u.source_id WHERE s.source_id IS NULL LIMIT 1"
        ).fetchone():
            refuse()
        for row in self._sql("SELECT * FROM media_objective_anchors"):
            original = self._sql(
                "SELECT * FROM media_receipts WHERE receipt_id=?", (row["anchor_receipt_id"],)
            ).fetchone()
            proof_row = self._sql(
                "SELECT * FROM media_receipts WHERE receipt_id=?", (row["task_proof_receipt_id"],)
            ).fetchone()
            if (
                original is None
                or original["kind"] != "anchor"
                or proof_row is None
                or proof_row["kind"] != "task_proof"
            ):
                refuse()
            anchor, proof = self._read_receipt(original).body, self._read_receipt(proof_row)
            pin = m.media_bytes(anchor.owner_objective)
            if (
                pin != bytes(row["owner_objective"])
                or hashlib.sha256(pin).hexdigest() != row["owner_objective_sha256"]
                or row["owner_realm_id"] != self.owner_realm_id
                or (
                    row["session_id"],
                    row["thread_id"],
                    row["epoch"],
                    row["task_id"],
                    row["initial_objective_revision"],
                    row["task_submit_event_id"],
                )
                != (
                    anchor.session.session_id,
                    anchor.session.thread_id,
                    anchor.session.epoch,
                    anchor.task_id,
                    anchor.initial_objective_revision,
                    proof.body.original_admission_id,
                )
                or anchor.task_admission != _ref(self.backend_id, proof)
            ):
                refuse()
        for row in self._sql("SELECT * FROM media_equivalences"):
            seal_row = self._sql(
                "SELECT * FROM media_receipts WHERE receipt_id=?",
                (row["original_seal_receipt_id"],),
            ).fetchone()
            if seal_row is None or seal_row["kind"] != "seal":
                refuse()
            seal = self._read_receipt(seal_row).body
            admission = self._lookup_ref(token, seal.source, "source").body
            anchor = self._lookup_ref(token, admission.anchor, "anchor").body
            if (
                row["source_sha256"],
                row["source_bytes"],
                row["equivalence_id"],
                row["owner_objective_sha256"],
            ) != (
                seal.source_sha256,
                seal.bytes,
                seal.source_equivalence_id,
                hashlib.sha256(m.media_bytes(anchor.owner_objective)).hexdigest(),
            ):
                refuse()
        for row in self._sql("SELECT * FROM media_sources"):
            original = self._sql(
                "SELECT * FROM media_receipts WHERE receipt_id=?", (row["admission_receipt_id"],)
            ).fetchone()
            if original is None or original["kind"] != "source":
                refuse()
            receipt = self._read_receipt(original)
            source = receipt.body
            if (
                row["source_id"],
                row["client_instance"],
                row["session_id"],
                row["thread_id"],
                row["epoch"],
                row["profile_sha256"],
                row["policy_sha256"],
                row["reservation_id"],
                row["anchor_receipt_id"],
            ) != (
                source.source_id,
                source.client_instance,
                source.session.session_id,
                source.session.thread_id,
                source.session.epoch,
                source.decoder_profile_sha256,
                source.reservation.policy_sha256,
                source.reservation.reservation_id,
                None if source.anchor is None else source.anchor.receipt_id,
            ):
                refuse()
            client = self._client(source.source_id)
            if client["client_instance"] != source.client_instance:
                refuse()
            fence = m.media_json(bytes(row["current_fence"]), m.MediaFence)
            if (
                m.media_bytes(fence) != bytes(row["current_fence"])
                or fence.session != source.session
                or fence.source_revision != source.source_revision
                or fence.generation < source.fence.generation
                or fence.lease_generation < source.fence.lease_generation
            ):
                refuse()
            reservation = self._sql(
                "SELECT * FROM media_reservations WHERE source_id=?", (source.source_id,)
            ).fetchone()
            if (
                reservation is None
                or reservation["reservation_id"] != source.reservation.reservation_id
                or reservation["owner_realm_id"] != self.owner_realm_id
                or reservation["policy_sha256"] != source.reservation.policy_sha256
                or reservation["spool_bytes"] != source.reservation.owner_spool_bytes
                or reservation["spool_bytes"] != source.reservation.global_spool_bytes
            ):
                refuse()
            if row["raw_state"] == "deleted" and (
                not row["logical_deleted"] or reservation["state"] != "released"
            ):
                refuse()
            if row["current_status_receipt_id"] is not None:
                status_row = self._sql(
                    "SELECT * FROM media_receipts WHERE receipt_id=?",
                    (row["current_status_receipt_id"],),
                ).fetchone()
                if (
                    status_row is None
                    or status_row["kind"] != "status"
                    or self._read_receipt(status_row).body.source != _ref(self.backend_id, receipt)
                ):
                    refuse()
            expected_index, expected_offset = 0, 0
            for part in self._sql(
                'SELECT * FROM media_uploads WHERE source_id=? ORDER BY "index"',
                (source.source_id,),
            ):
                ack_row = self._sql(
                    "SELECT * FROM media_receipts WHERE receipt_id=?", (part["ack_receipt_id"],)
                ).fetchone()
                if ack_row is None or ack_row["kind"] != "upload_ack":
                    refuse()
                ack = self._read_receipt(ack_row)
                if (part["index"], part["offset"]) != (expected_index, expected_offset):
                    refuse()
                if (
                    ack.key,
                    ack.body.source,
                    ack.body.index,
                    ack.body.offset,
                    ack.body.body_bytes,
                    ack.body.body_sha256,
                    ack.body.next_offset,
                ) != (
                    part["key"],
                    _ref(self.backend_id, receipt),
                    part["index"],
                    part["offset"],
                    part["body_bytes"],
                    part["body_sha256"],
                    part["next_offset"],
                ):
                    refuse()
                expected_index += 1
                expected_offset = part["next_offset"]
            parent = self._sql(
                "SELECT * FROM media_operations WHERE key=?", (receipt.key,)
            ).fetchone()
            for dependency in self._sql(
                "SELECT * FROM media_dependencies WHERE source_id=?", (source.source_id,)
            ):
                if dependency["kind"] not in {"upload", "decoder", "intake", "owner_channel"}:
                    refuse()
                child_row = self._sql(
                    "SELECT * FROM media_receipts WHERE receipt_id=?",
                    (dependency["child_owner_receipt_id"],),
                ).fetchone()
                if child_row is None or child_row["kind"] != "child_owner":
                    refuse()
                child = self._read_receipt(child_row)
                if (
                    child.body.source != _ref(self.backend_id, receipt)
                    or child.body.backend_id != self.backend_id
                ):
                    refuse()
                if dependency["kind"] in {"upload", "decoder"}:
                    binding = {
                        "source": json.loads(m.media_bytes(child.body.source)),
                        "fence": json.loads(m.media_bytes(source.fence)),
                        "parent_request_sha256": parent["request_sha256"],
                        "kind": dependency["kind"],
                        "instance": dependency["original_instance"],
                        "operation_id": parent["original_operation_id"],
                        "backend_id": self.backend_id,
                        "consumer": None,
                    }
                    if (
                        child.body.kind != dependency["kind"]
                        or child.body.instance != dependency["original_instance"]
                        or child.body.operation_id != parent["original_operation_id"]
                        or child.body.peer.kind != "native_task"
                        or child.body.peer.backend_instance != parent["original_app_instance"]
                        or child.body.peer.task_id != dependency["dependency_id"]
                        or child.body.peer.request_sha256 != parent["request_sha256"]
                        or child.body.binding_sha256
                        != hashlib.sha256(
                            b"kinaiya.media.child-binding.v1\0" + _canonical(binding)
                        ).hexdigest()
                    ):
                        refuse()
                    if dependency["state"] == "settled":
                        cleanup_row = self._sql(
                            "SELECT * FROM media_receipts WHERE receipt_id=?",
                            (dependency["cleanup_receipt_id"],),
                        ).fetchone()
                        if cleanup_row is None or cleanup_row["kind"] != "cleanup":
                            refuse()
                        cleanup = self._read_receipt(cleanup_row).body
                        if (
                            cleanup.original_owner != _ref(self.backend_id, child)
                            or cleanup.state != "settled"
                            or cleanup.operation_id != child.body.operation_id
                            or cleanup.binding_sha256 != child.body.binding_sha256
                            or cleanup.child_instance != child.body.instance
                            or dependency["publication_state"] != "settled"
                        ):
                            refuse()
            counts = dict(
                self._sql(
                    "SELECT kind,COUNT(*) FROM media_dependencies WHERE source_id=? AND kind IN('upload','decoder') GROUP BY kind",
                    (source.source_id,),
                ).fetchall()
            )
            if counts != {"upload": 1, "decoder": 1}:
                refuse()
            if row["equivalence_id"] is not None:
                verified = self._sql(
                    "SELECT 1 FROM media_operations o JOIN media_receipts r ON r.receipt_id=o.result_receipt_id WHERE o.kind='seal' AND o.source_id=? AND r.kind='seal' AND json_extract(CAST(r.canonical_receipt AS TEXT),'$.body.source_equivalence_id')=?",
                    (source.source_id, row["equivalence_id"]),
                ).fetchone()
                if verified is None:
                    refuse()

    def _read_receipt(self, row):
        if row["kind"] not in _KINDS or row["owner_realm_id"] != self.owner_realm_id:
            refuse()
        kind = getattr(m, "Media" + _KINDS[row["kind"]])
        receipt = m.media_json(bytes(row["canonical_receipt"]), m.MediaReceipt[kind])
        # The fresh parse already rebuilt every child and checked its digest.
        # Re-encode its complete actual graph without another reconstruction.
        canonical = _parsed_media_bytes(receipt)
        if (
            canonical != bytes(row["canonical_receipt"])
            or receipt.id != row["receipt_id"]
            or receipt.key != row["key"]
            or receipt.request_sha256 != row["request_sha256"]
            or receipt.sha256 != row["sha256"]
        ):
            refuse()
        return receipt

    def _lookup_ref(self, token, reference, kind):
        self._locked(token)
        reference = _fresh(reference, m.MediaNativeRef)
        if reference.backend_id != self.backend_id:
            refuse("unknown_ref")
        row = self._sql(
            "SELECT * FROM media_receipts WHERE receipt_id=?", (reference.receipt_id,)
        ).fetchone()
        if row is None or row["kind"] != kind or row["sha256"] != reference.sha256:
            refuse("unknown_ref")
        return self._read_receipt(row)

    def _session_admission(self, session_id):
        with self._mutex:
            if session_id in self._session_changes:
                refuse("conflict")

    @staticmethod
    def _session_change_arguments(session_id, reason):
        if (
            type(session_id) is not str
            or not 1 <= len(session_id) <= 128
            or type(reason) is not str
            or reason not in {"reset", "deleted"}
        ):
            refuse("bad_packet")
        try:
            if len(session_id.encode("utf-8")) > 128:
                refuse("bad_packet")
        except UnicodeError:
            refuse("bad_packet")

    def begin_session_change(self, session_id, reason):
        # Negative enrollment must precede Store.lock. It observes no SQL or
        # OS state and cannot itself establish that the requested change ran.
        self._session_change_arguments(session_id, reason)
        if self._closing or self._closed or self._poisoned:
            refuse()
        with self._mutex:
            if self._closing or self._closed or self._poisoned:
                refuse()
            change = self._session_changes.get(session_id)
            if change is not None:
                if change.reason != reason:
                    refuse("conflict")
            else:
                if len(self._session_changes) >= 16:
                    refuse("capacity")
                change = _SessionChange(session_id, reason)
                self._session_changes[session_id] = change
            for source in self._sources.values():
                if source.admission.session.session_id == session_id:
                    source.fenced.set()
                    source.reason = reason
                    if source.original is not None:
                        source.original.fenced.set()
            for original in self._operations.values():
                if original.session_id == session_id:
                    original.fenced.set()
            return change

    def validate_session_change(self, change, session_id, reason):
        self._session_change_arguments(session_id, reason)
        if self._closing or self._closed or self._poisoned:
            refuse()
        with self._mutex:
            if self._closing or self._closed or self._poisoned:
                refuse()
            self._session_change_registered(change)
            if (change.session_id, change.reason) != (session_id, reason):
                refuse("conflict")
            if change.state == "unknown":
                refuse()
            if change.state == "refused":
                refuse(change.error_code)
            return change

    def _session_change_registered(self, change):
        # A foreign/reconstructed token never enrolls itself on observation.
        if type(change) is not _SessionChange:
            refuse("bad_packet")
        if self._session_changes.get(change.session_id) is change:
            return
        if self._session_change_recent.get(id(change)) is not change:
            refuse("stale")

    def _session_change_finish(self, change, state, *, result=None, error_code=None):
        with self._mutex:
            self._session_change_registered(change)
            if change.state == "unknown" and state != "unknown":
                refuse()
            change.state, change.result, change.error_code = state, result, error_code
            if state in {"applied", "refused"}:
                if self._session_changes.get(change.session_id) is change:
                    del self._session_changes[change.session_id]
                self._session_change_recent[id(change)] = change
                for key, previous in tuple(self._session_change_recent.items()):
                    if len(self._session_change_recent) <= 64:
                        break
                    if (
                        previous.state in {"applied", "refused"}
                        and not previous.running
                        and self._session_dispatch_joined(previous.dispatch)
                        and (previous.dispatch is None or previous.dispatch.retired)
                    ):
                        del self._session_change_recent[key]

    def _session_change_read(self, session_id):
        row = self._sql("SELECT value FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        raw = row[0]
        # Legacy Session has unbounded display strings. Refuse such a snapshot
        # before UTF-8 copying/model reconstruction or retaining it in a token.
        if type(raw) is not str:
            self._poisoned = True
            refuse()
        if len(raw) > m.MAX_MEDIA_CONTROL_BYTES:
            refuse("bound")
        try:
            if len(raw.encode("utf-8")) > m.MAX_MEDIA_CONTROL_BYTES:
                refuse("bound")
            session = Session.model_validate_json(raw)
            if session.session_id != session_id:
                self._poisoned = True
                refuse()
            m.MediaSession(
                session_id=session.session_id, thread_id=session.thread_id, epoch=session.epoch
            )
            encoded = _canonical(session.model_dump(mode="json"))
        except MediaSessionError:
            raise
        except m.MediaContractError as error:
            refuse(error.code)
        except Exception:
            self._poisoned = True
            refuse()
        if len(encoded) > m.MAX_MEDIA_CONTROL_BYTES:
            refuse("bound")
        return encoded

    @staticmethod
    def _session_change_identity(raw):
        session = Session.model_validate_json(raw)
        return session.session_id, session.thread_id, session.epoch

    def _session_change_fences(self, change):
        if (
            self._sql(
                "SELECT 1 FROM media_clients WHERE session_id=? AND connection_state NOT IN('fenced','unknown','settled') LIMIT 1",
                (change.session_id,),
            ).fetchone()
            or self._sql(
                "SELECT 1 FROM media_sources WHERE session_id=? AND desired='running' LIMIT 1",
                (change.session_id,),
            ).fetchone()
        ):
            refuse("conflict")

    @contextmanager
    def session_change_transaction(self, change, phase):
        if (
            type(phase) is not str
            or phase not in {"reset", "delete_fence", "delete_finalize"}
            or type(change) is not _SessionChange
            or (phase == "reset") != (change.reason == "reset")
        ):
            refuse("bad_packet")
        if self._closing or self._closed or self._poisoned:
            refuse()
        with self.store.lock:
            # Never join/commit/roll back a caller's already-open transaction.
            if self._closing or self._closed or self._poisoned or self.store.db.in_transaction:
                refuse()
            with self._mutex:
                self._session_change_registered(change)
                if change.running:
                    refuse("conflict")
                if change.state == "unknown":
                    refuse()
                if change.state == "refused":
                    refuse(change.error_code)
                if (
                    change.state == "pending"
                    and phase == "delete_finalize"
                    and not change.fence_committed
                ):
                    refuse("conflict")
                change.running = True
            committed = False
            replay = change.state == "applied"
            phase_replay = replay or phase == "delete_fence" and change.fence_committed
            try:
                with self._boundary():
                    self._authority()
                    if change.original is None:
                        change.original = self._session_change_read(change.session_id)
                        if change.original is None:
                            refuse("unknown_ref")
                    original_id = self._session_change_identity(change.original)
                    if not replay:
                        current = self._session_change_read(change.session_id)
                        if current is None or self._session_change_identity(current) != original_id:
                            refuse("stale")
                        if phase == "reset" and original_id[2] >= _MAX:
                            refuse("bound")
                    before = self.store.db.total_changes
                    yield _SessionChangeView(
                        change.original,
                        change.state,
                        change.result,
                        change.error_code,
                        phase_replay,
                    )
                    self._authority()
                    if phase_replay:
                        # Even a mistaken caller cannot commit a second DML
                        # operation while observing a completed original token.
                        if self.store.db.total_changes != before:
                            refuse("conflict")
                    if not replay:
                        self._session_change_fences(change)
                        current = self._session_change_read(change.session_id)
                        if phase == "reset":
                            if current is None or self._session_change_identity(current) != (
                                *original_id[:2],
                                original_id[2] + 1,
                            ):
                                refuse("conflict")
                        elif phase == "delete_fence":
                            if (
                                current is None
                                or self._session_change_identity(current) != original_id
                            ):
                                refuse("conflict")
                        elif current is not None:
                            refuse("conflict")
                committed = True
                if not replay:
                    # Retain Store.lock through the native postcommit read.
                    # No caller flag, requested phase or rowcount proves this.
                    observed = self._session_change_read(change.session_id)
                    if observed != current:
                        self._poisoned = True
                        refuse()
                    if phase == "delete_fence":
                        change.fence_committed = True
                    else:
                        self._session_change_finish(change, "applied", result=observed)
            except BaseException as error:
                if committed or self._poisoned or not isinstance(error, Exception):
                    self._poisoned = True
                    self._session_change_finish(change, "unknown", error_code="unavailable")
                    if isinstance(error, Exception):
                        raise MediaSessionError("unavailable") from None
                elif not replay:
                    code = error.code if isinstance(error, MediaSessionError) else "unavailable"
                    if not (
                        change.reason == "deleted" and change.fence_committed and code == "conflict"
                    ):
                        self._session_change_finish(change, "refused", error_code=code)
                    if not isinstance(error, MediaSessionError):
                        refuse(code)
                raise
            finally:
                with self._mutex:
                    change.running = False

    @staticmethod
    def _session_dispatch_joined(dispatch):
        if dispatch is None:
            return True
        if dispatch.unknown:
            return False
        if dispatch.no_dispatch and dispatch.coroutine is None:
            return dispatch.outcome == "refused"
        task = dispatch.task
        return (
            type(task) is asyncio.Task
            and task.get_loop() is dispatch.loop
            and task.get_coro() is dispatch.coroutine
            and dispatch.entered_task is task
            and dispatch.completed_task is task
            and task.done()
            and not task.cancelled()
            and task.exception() is None
            and task.result() == dispatch.outcome
            and dispatch.outcome in {"applied", "refused", "conflict"}
        )

    def _session_dispatch_unknown(self, change, dispatch, error=None):
        with self._mutex:
            self._poisoned = True
            dispatch.unknown = True
            dispatch.outcome, dispatch.error_code = "unknown", "unavailable"
            if error is not None:
                dispatch.setup_error = error
            self._session_change_finish(change, "unknown", error_code="unavailable")
            # Setup may have taken effects even without returning a Task. Its
            # exact precharged slot/original remains reachable, never retried.
            self._native_jobs.setdefault(dispatch.marker, dispatch.task)
            self._session_change_jobs[dispatch.marker] = (change, dispatch)

    def _session_dispatch_done(self, change, dispatch, task):
        with self._mutex:
            if not dispatch.setup_complete:
                return
            if task is not dispatch.task or not self._session_dispatch_joined(dispatch):
                self._session_dispatch_unknown(change, dispatch)
                return
            # Release only after the actual original Task facade returned and
            # positively completed, including an eager factory's setup window.
            self._native_jobs.pop(dispatch.marker, None)
            self._session_change_jobs.pop(dispatch.marker, None)
            dispatch.retired = True

    def _session_change_store(self, change):
        # No caller factory, role string or boolean selects native mutation.
        try:
            function = self.store.reset if change.reason == "reset" else self.store.delete
            function(change.session_id, _media_change=change)
        except BaseException as error:
            if not isinstance(error, Exception):
                return "unknown", "unavailable"
            return "error", error.code if isinstance(error, MediaSessionError) else "unavailable"
        return "returned", None

    async def _session_change_worker(self, change, dispatch):
        await dispatch.worker_assigned.wait()
        current = asyncio.current_task()
        if (
            current is not dispatch.worker
            or not self._actual_task(dispatch.worker, dispatch.loop, dispatch.worker_coroutine)
            or dispatch.unknown
        ):
            self._session_dispatch_unknown(change, dispatch)
            refuse()
        dispatch.worker_entered_task = current
        result = await asyncio.to_thread(self._session_change_store, change)
        dispatch.worker_result = result
        dispatch.worker_completed_task = current
        return result

    def _session_store_worker_joined(self, dispatch):
        worker = dispatch.worker
        return (
            self._actual_task(worker, dispatch.loop, dispatch.worker_coroutine)
            and dispatch.worker_entered_task is worker
            and dispatch.worker_completed_task is worker
            and worker.done()
            and not worker.cancelled()
            and worker.exception() is None
            and worker.result() == dispatch.worker_result
        )

    async def _run_session_change_native(self, change, dispatch):
        with self._mutex:
            if self._session_change_jobs.get(dispatch.marker) != (change, dispatch):
                refuse()
            if dispatch.unknown:
                refuse()
            if self._closing or self._closed or self._poisoned:
                # This exact native branch precedes worker coroutine creation.
                dispatch.no_dispatch = True
                refuse()
        dispatch.worker_coroutine = self._session_change_worker(change, dispatch)
        try:
            dispatch.worker = asyncio.create_task(dispatch.worker_coroutine)
            if (
                not self._actual_task(dispatch.worker, dispatch.loop, dispatch.worker_coroutine)
                or dispatch.worker.done()
            ):
                refuse()
            dispatch.worker_assigned.set()
        except BaseException as error:
            self._session_dispatch_unknown(change, dispatch, error)
            raise
        interrupted = False
        while not dispatch.worker.done():
            try:
                await asyncio.shield(dispatch.worker)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            # An actual controller cancellation is distinct from an observer
            # leaving asyncio.wait; the original worker has still been joined.
            raise asyncio.CancelledError
        if not self._session_store_worker_joined(dispatch):
            self._session_dispatch_unknown(change, dispatch)
            refuse()
        return dispatch.worker_result

    async def _session_change_controller(self, change, dispatch):
        try:
            await dispatch.assigned.wait()
            current = asyncio.current_task()
            if (
                current is not dispatch.task
                or not self._actual_task(dispatch.task, dispatch.loop, dispatch.coroutine)
                or not dispatch.setup_complete
                or dispatch.unknown
            ):
                self._session_dispatch_unknown(change, dispatch)
                refuse()
            dispatch.entered_task = current
            outcome, code = await self._run_session_change_native(change, dispatch)
            if (
                outcome == "error"
                and code == "conflict"
                and change.reason == "deleted"
                and change.state == "pending"
                and change.fence_committed
                and not change.running
            ):
                # Only this explicit native async dispatch can reconcile a
                # known reader checkpoint. Direct Store checks stay checks.
                await self._session_reader_reconciliation(change, dispatch)
            with self._mutex:
                if dispatch.unknown:
                    pass
                elif outcome == "returned" and change.state == "applied" and not change.running:
                    dispatch.outcome = "applied"
                elif outcome == "error" and change.state == "refused" and not change.running:
                    dispatch.outcome, dispatch.error_code = "refused", change.error_code
                elif (
                    outcome == "error"
                    and change.reason == "deleted"
                    and change.state == "pending"
                    and change.fence_committed
                    and not change.running
                    and code == "conflict"
                ):
                    dispatch.outcome, dispatch.error_code = "conflict", "conflict"
                else:
                    self._session_dispatch_unknown(change, dispatch)
        except MediaSessionError as error:
            with self._mutex:
                if dispatch.unknown:
                    pass
                elif dispatch.no_dispatch:
                    if change.state == "applied" and not change.running:
                        dispatch.outcome = "applied"
                    elif change.state == "refused" and not change.running:
                        dispatch.outcome, dispatch.error_code = "refused", change.error_code
                    elif change.state == "pending" and not change.running:
                        self._session_change_finish(change, "refused", error_code=error.code)
                        dispatch.outcome, dispatch.error_code = "refused", error.code
                    else:
                        self._session_dispatch_unknown(change, dispatch)
                else:
                    self._session_dispatch_unknown(change, dispatch)
        except BaseException as error:
            self._session_dispatch_unknown(change, dispatch, error)
        # Keep only a fixed native outcome in the Task, not a mutable Session
        # alias or an exception traceback retaining each completed SQL check.
        dispatch.completed_task = asyncio.current_task()
        return dispatch.outcome

    async def apply_session_change(self, change):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0  # Observation only; never a physical cancellation.
        if type(change) is not _SessionChange:
            refuse("bad_packet")
        fresh = complete = False
        with self._mutex:
            if self._closing or self._closed or self._poisoned:
                refuse()
            self._session_change_registered(change)
            dispatch = change.dispatch
            if change.state == "unknown" or dispatch is not None and dispatch.unknown:
                refuse()
            if change.state == "refused":
                refuse(change.error_code)
            if change.state == "applied" and self._session_dispatch_joined(dispatch):
                result = change.result
                complete = True
            elif dispatch is None or (self._session_dispatch_joined(dispatch) and dispatch.retired):
                if change.running:
                    refuse("conflict")
                if dispatch is not None and not (
                    dispatch.outcome == "conflict"
                    and change.reason == "deleted"
                    and change.fence_committed
                    and change.state == "pending"
                ):
                    refuse()
                if self._loop is not None and self._loop is not loop:
                    refuse("conflict")
                if change.completed_checks >= _MAX - 1:
                    refuse("bound")
                completed = change.completed_checks + (dispatch is not None)
                dispatch = _SessionChangeDispatch(
                    loop,
                    completed + 1,
                    None if dispatch is None else dispatch.ordinal,
                )
                change.completed_checks = completed
                change.dispatch = dispatch
                if change.first_dispatch is None:
                    change.first_dispatch = dispatch
                # The SAME existing native16-job cap covers controller setup,
                # queued work and its sole thread; never double-reserve a job.
                if len(self._native_jobs) >= 16:
                    dispatch.no_dispatch = True
                    dispatch.outcome, dispatch.error_code = "refused", "capacity"
                    dispatch.setup_complete = dispatch.retired = True
                    self._session_change_finish(change, "refused", error_code="capacity")
                    refuse("capacity")
                self._loop = loop
                self._native_jobs[dispatch.marker] = None
                self._session_change_jobs[dispatch.marker] = (change, dispatch)
                fresh = True
            elif dispatch.loop is not loop:
                refuse("conflict")
            elif dispatch.task is None or not dispatch.task.done():
                # Additional pending observers refuse rather than accumulating
                # unbounded waiters or scheduling a second native Store call.
                refuse("conflict")
        if complete:
            return self._session_change_observed_result(result, loop, deadline)
        if fresh:
            task = None
            try:
                dispatch.coroutine = self._session_change_controller(change, dispatch)
                task = asyncio.create_task(dispatch.coroutine)
                if not self._actual_task(task, dispatch.loop, dispatch.coroutine) or task.done():
                    refuse()
            except BaseException as error:
                self._session_dispatch_unknown(change, dispatch, error)
                if not isinstance(error, Exception):
                    raise
                refuse()
            finally:
                with self._mutex:
                    if task is not None:
                        dispatch.task = task
                        if dispatch.marker in self._native_jobs:
                            self._native_jobs[dispatch.marker] = task
            try:
                task.add_done_callback(
                    lambda done: self._session_dispatch_done(change, dispatch, done)
                )
            except BaseException as error:
                self._session_dispatch_unknown(change, dispatch, error)
                if not isinstance(error, Exception):
                    raise
                refuse()
            with self._mutex:
                dispatch.setup_complete = True
            dispatch.assigned.set()
            if task.done():
                self._session_dispatch_done(change, dispatch, task)
        if dispatch.task is None:
            refuse("conflict")  # Same original is still in setup; no second runner.
        if not dispatch.task.done():
            _, pending = await asyncio.wait(
                {dispatch.task}, timeout=max(0.0, deadline - loop.time())
            )
            if pending or loop.time() >= deadline:
                refuse()  # Original gate/task/slot stay owned after this observation.
        with self._mutex:
            self._session_change_registered(change)
            if not self._session_dispatch_joined(dispatch):
                self._session_dispatch_unknown(change, dispatch)
                refuse()
            if dispatch.outcome != "applied":
                refuse(dispatch.error_code)
            if change.state != "applied":
                refuse()
            result = change.result
        return self._session_change_observed_result(result, loop, deadline)

    @staticmethod
    def _session_change_observed_result(raw, loop, deadline):
        result = None if raw is None else Session.model_validate_json(raw)
        if loop.time() >= deadline:
            refuse()
        return result

    def _session(self, value):
        self._session_admission(value.session_id)
        session = self.store.session(value.session_id)
        if (session.thread_id, session.epoch) != (value.thread_id, value.epoch):
            refuse("stale")
        return session

    def _prepare(self, token, request, kind, *, source_id=None, client_instance=None):
        self._locked(token)
        raw = m.media_bytes(request)
        digest = m.media_digest(_REQUESTS[kind], request)
        old = self._sql("SELECT * FROM media_operations WHERE key=?", (request.key,)).fetchone()
        if old is not None:
            if (
                old["kind"] != kind
                or old["request_sha256"] != digest
                or bytes(old["canonical_request"]) != raw
            ):
                refuse("conflict")
            return old, False
        charge = self._charges[kind]
        with self._mutex:
            if self._metadata_hold + charge > _METADATA:
                refuse("capacity")
            # Includes bounded SQL/receipt/index/dep overhead AND anticipated
            # <=4KiB catalog/ledger growth; no snapshot is a reservation.
            self._metadata_hold += charge
        operation = secrets.token_hex(16)
        candidate = secrets.token_hex(16)
        fence = getattr(request, "fence", None)
        self._sql(
            "INSERT INTO media_operations VALUES(?,?,?,?,?,?,?,?,?,?,'prepared',NULL,NULL,?,?)",
            (
                request.key,
                kind,
                digest,
                raw,
                self.owner_realm_id,
                client_instance,
                source_id,
                operation,
                self.app_instance,
                candidate,
                None if fence is None else m.media_bytes(fence),
                charge,
            ),
        )
        return self._sql(
            "SELECT * FROM media_operations WHERE key=?", (request.key,)
        ).fetchone(), True

    def _catalog_metadata(self):
        return self._sql("SELECT COUNT(*) FROM media_catalog").fetchone()[0] * self._unit_charge

    def _intake_metadata(self):
        total = 0
        cursor = self._sql(
            "SELECT r.source_id,r.metadata_bytes,COUNT(d.dependency_id) AS intakes FROM media_reservations r LEFT JOIN media_dependencies d ON d.source_id=r.source_id AND d.kind IN('intake','owner_channel') GROUP BY r.source_id,r.metadata_bytes"
        )
        for row in cursor:
            extra = row["metadata_bytes"] - self._charges["admit"]
            if extra != row["intakes"] * self._intake_charge or extra < 0:
                refuse()
            total += extra
        return total

    def _complete(self, token, operation, body, kind, session, role):
        self._locked(token)
        fields = {
            "v": 1,
            "id": operation["candidate_receipt_id"],
            "key": operation["key"],
            "request_sha256": operation["request_sha256"],
            "body": body,
        }
        fields["sha256"] = m.media_digest("R<" + _KINDS[kind] + ">", fields)
        receipt = m.MediaReceipt[type(body)](**fields)
        raw = m.media_bytes(receipt)  # Complete root caps before any receipt DML.
        self._sql(
            "INSERT INTO media_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt.id,
                kind,
                receipt.key,
                receipt.request_sha256,
                receipt.sha256,
                raw,
                self.owner_realm_id,
                session.session_id,
                session.thread_id,
                session.epoch,
                role,
                self.app_instance,
                operation["original_operation_id"],
            ),
        )
        self._sql(
            "UPDATE media_operations SET phase='committed',result_receipt_id=? WHERE key=?",
            (receipt.id, operation["key"]),
        )
        return receipt

    def _existing(self, operation):
        if operation["phase"] != "committed":
            refuse()
        row = self._sql(
            "SELECT * FROM media_receipts WHERE receipt_id=?", (operation["result_receipt_id"],)
        ).fetchone()
        if row is None:
            refuse()
        return self._read_receipt(row)

    async def run_native(self, function, *args, **kwargs):
        # Once the final logical barrier is sealed, only the two exact original
        # close phases below can acquire native jobs. No caller flag bypasses it.
        with self._mutex:
            if self._close_operation is not None and self._close_operation.sealed:
                refuse()
        return await self._run_native_original(function, *args, **kwargs)

    async def _run_native_original(self, function, *args, **kwargs):
        """Retain actual thread work through caller cancellation and late results."""
        marker = object()
        with self._mutex:
            loop = asyncio.get_running_loop()
            if self._loop is not None and self._loop is not loop:
                refuse("conflict")
            self._loop = loop
            if len(self._native_jobs) >= 16:
                refuse("capacity")
            self._native_jobs[marker] = None
        task = None
        interrupted = False
        try:
            task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            with self._mutex:
                self._native_jobs[marker] = task
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    interrupted = True
            result = task.result()
            if interrupted:
                raise asyncio.CancelledError
            return result
        finally:
            if task is None or task.done():
                with self._mutex:
                    self._native_jobs.pop(marker, None)

    async def _execute(self, request, kind, work, principal, nonce=b""):
        raw = m.media_bytes(request)
        digest = m.media_digest(_REQUESTS[kind], request)
        session_id = (
            request.session.session_id if kind == "admit" else request.fence.session.session_id
        )
        if (
            type(principal) is not NativeMediaPrincipal
            or principal.issuer is not self._principal_issuer
            or principal.auth_identity is not self.auth.media_identity()
        ):
            refuse("denied")
        binding = self.keys.authenticate_digest(
            b"kinaiya.media.original-waiter.v1\0",
            _canonical(
                {
                    "auth": principal.auth_identity.instance_id,
                    "generation": principal.auth_generation,
                    "app": principal.app_instance,
                    "role": principal.role,
                    "capability_sha256": None
                    if principal.client_capability is None
                    else hashlib.sha256(principal.client_capability.encode("ascii")).hexdigest(),
                    "nonce_sha256": hashlib.sha256(nonce).hexdigest(),
                }
            ),
        )
        candidate = OriginalOperation(
            request.key,
            digest,
            kind,
            secrets.token_hex(16),
            secrets.token_hex(16),
            session_id,
            binding,
        )
        async with self._async_lock:
            with self._mutex:
                original = self._operations.get(request.key)
                fresh = original is None
                if not fresh:
                    if (
                        original.kind != kind
                        or original.digest != digest
                        or original.private_binding != binding
                        or original.session_id != session_id
                    ):
                        refuse("conflict")
                else:
                    if self._poisoned:
                        refuse("unavailable")
                    if session_id in self._session_changes:
                        refuse("conflict")
                    active = sum(
                        item.task is None or not item.task.done()
                        for item in self._operations.values()
                    )
                    if self._closing or active >= 16:
                        refuse("capacity" if not self._closing else "unavailable")
                    original = candidate
                    self._operations[request.key] = original
            if fresh:

                async def original_work():
                    try:
                        value = m.media_json(raw, getattr(m, "Media" + _REQUESTS[kind]))
                        original.result = await work(value, original)
                        return original.result
                    except BaseException as error:
                        if not isinstance(error, Exception):
                            original.unknown = True
                        raise

                try:
                    original.task = asyncio.create_task(original_work())
                finally:
                    original.assigned.set()
        await original.assigned.wait()
        if original.task is None:
            refuse()
        try:
            return await asyncio.shield(original.task)
        finally:
            # Bounded positive recent cache; unresolved original owners never evict.
            with self._mutex:
                positives = [
                    key
                    for key, item in self._operations.items()
                    if item.task is not None and item.task.done() and not item.unknown
                ]
                for key in positives[:-64]:
                    self._operations.pop(key, None)

    def _capture_operation(self, original, row):
        with self._mutex:
            original.original_id = row["original_operation_id"]
            original.candidate_id = row["candidate_receipt_id"]
            original.source_id = row["source_id"]
            original.client_instance = row["client_instance"]

    def _original_row(self, token, original):
        self._locked(token)
        if self._operations.get(original.key) is not original or original.effect_result is None:
            refuse()
        row = self._sql("SELECT * FROM media_operations WHERE key=?", (original.key,)).fetchone()
        meta = self._sql("SELECT app_instance FROM media_meta WHERE id=1").fetchone()
        if (
            row is None
            or row["original_operation_id"] != original.original_id
            or row["candidate_receipt_id"] != original.candidate_id
            or row["request_sha256"] != original.digest
            or row["kind"] != original.kind
            or row["original_app_instance"] != self.app_instance
            or meta is None
            or meta[0] != self.app_instance
        ):
            refuse()
        return row

    def _nonce_binding(self, row):
        return {
            "backend_id": self.backend_id,
            "owner_realm_id": row["owner_realm_id"],
            "owner_auth_generation": row["auth_generation"],
            "app_instance": row["app_instance"],
            "session": {
                "session_id": row["session_id"],
                "thread_id": row["thread_id"],
                "epoch": row["epoch"],
            },
            "key": row["original_key"],
            "request_sha256": self._sql(
                "SELECT request_sha256 FROM media_operations WHERE key=?", (row["original_key"],)
            ).fetchone()[0],
            "source_id": row["source_id"],
            "admission_id": self._sql(
                "SELECT original_operation_id FROM media_operations WHERE key=?",
                (row["original_key"],),
            ).fetchone()[0],
            "client_instance": row["client_instance"],
            "nonce_sha256": row["nonce_sha256"],
            "key_id": self.keys.key_id,
        }

    def _grant(self, row):
        return self.keys.authenticate_digest(
            b"kinaiya.media.client-grant.v1\0", _canonical(self._nonce_binding(row))
        )

    def _client(self, source_id):
        row = self._sql("SELECT * FROM media_clients WHERE source_id=?", (source_id,)).fetchone()
        if row is None or row["owner_realm_id"] != self.owner_realm_id:
            refuse("unknown_ref")
        return row

    def _capability(self, principal, client):
        value = principal.client_capability
        if (
            type(value) is not str
            or len(value) != 64
            or not secrets.compare_digest(
                hashlib.sha256(value.encode("ascii")).hexdigest(), client["grant_sha256"]
            )
            or not secrets.compare_digest(value, self._grant(client))
            or client["auth_generation"] != self._auth_generation
            or client["app_instance"] != self.app_instance
        ):
            refuse("denied")

    def _memory_source(self, source_id, *, connected=True):
        with self._mutex:
            source = self._sources.get(source_id)
            if source is None:
                refuse()
            if source.admission.session.session_id in self._session_changes:
                refuse("conflict")
            if self.clock() >= source.expires:
                source.fenced.set()
                source.reason = "lease_expired"
            if source.fenced.is_set():
                refuse("stale")
            if connected and (
                source.connection is None
                or source.connection.fenced
                or source.connection.unknown
                or source.connection.joined
            ):
                refuse("stale")
            return source

    def _source_guard(
        self, token, reference, fence, principal, *, connected=True, revocation=False
    ):
        receipt = self._lookup_ref(token, reference, "source")
        row = self._sql(
            "SELECT * FROM media_sources WHERE source_id=?", (receipt.body.source_id,)
        ).fetchone()
        if row is None or row["admission_receipt_id"] != receipt.id:
            refuse()
        current = m.media_json(bytes(row["current_fence"]), m.MediaFence)
        if current != fence or current.session != receipt.body.session:
            refuse("stale")
        if row["owner_realm_id"] != self.owner_realm_id:
            refuse("denied")
        if revocation:
            self._principal(principal, {"owner"})
            return row, self._sources.get(row["source_id"])
        self._session(current.session)
        self._capability(principal, self._client(row["source_id"]))
        if row["logical_deleted"] or row["raw_state"] in {"expired", "deleted"}:
            refuse("expired")
        source = self._sources.get(row["source_id"])
        if source is not None and self._raw_expired(source):
            source.fenced.set()
            source.reason = "expired"
            refuse("expired")
        source = self._memory_source(row["source_id"], connected=connected)
        if current != source.admission.fence:
            refuse("stale")
        if (
            row["profile_sha256"] != self.profile.profile_sha256
            or row["policy_sha256"] != self.policy_sha256
        ):
            refuse("stale")
        if connected:
            client = self._client(row["source_id"])
            if (
                client["connection_state"] != "connected"
                or client["connection_instance"] != source.connection.instance
            ):
                refuse("stale")
        self._anchor_guard(
            token, source.admission.anchor, source.admission.session, source.admission.scope
        )
        return row, source

    def _anchor_guard(self, token, reference, session, scope):
        if reference is None:
            refuse("unsupported")
        receipt = self._lookup_ref(token, reference, "anchor")
        body = receipt.body
        if (
            body.session != session
            or body.scope != scope
            or body.allowed_operation != "evidence_only"
        ):
            refuse("denied")
        proof = self._lookup_ref(token, body.task_admission, "task_proof")
        initial, revision, actual_scope, state, objective = self._task_truth(
            token, proof.body.original_admission_id, session
        )
        if (
            proof.body.task_id != initial.payload["task_id"]
            or body.task_id != proof.body.task_id
            or proof.body.owner_objective != objective
            or body.owner_objective != objective
            or revision != body.initial_objective_revision
            or actual_scope != scope
            or state in {"succeeded", "failed", "cancelled", "cancel_requested"}
        ):
            refuse("stale")
        saved = self._sql(
            "SELECT anchor_receipt_id FROM media_objective_anchors WHERE owner_objective_sha256=?",
            (hashlib.sha256(m.media_bytes(objective)).hexdigest(),),
        ).fetchone()
        if saved is None or saved[0] != receipt.id:
            refuse()
        return receipt

    def _admit_prepare(self, request, principal, nonce, original):
        with self._transaction(principal, {"owner"}) as token:
            nonce_sha = hashlib.sha256(nonce).hexdigest()
            old = self._sql("SELECT * FROM media_operations WHERE key=?", (request.key,)).fetchone()
            if old is not None:
                operation, _ = self._prepare(token, request, "admit")
                client = self._client(operation["source_id"])
                if client["nonce_sha256"] != nonce_sha:
                    refuse("conflict")
                return self._existing(operation), None
            if self._closing or original.fenced.is_set():
                refuse()
            self._session(request.session)
            if request.kind != "recording" or request.purpose not in {"observe", "analyze"}:
                refuse("unavailable")
            if request.declared_bytes == 0:
                refuse("unsupported")
            if request.decoder_profile_sha256 != self.profile.profile_sha256:
                refuse("stale")
            self._anchor_guard(token, request.anchor, request.session, request.scope)
            reserved = (
                self.limits.source.file_bytes
                if request.declared_bytes is None
                else request.declared_bytes
            )
            if reserved > self.limits.source.file_bytes:
                refuse("bound")
            now_ms = self.wall_clock_ms()
            if request.raw_retain_until_ms is not None and request.raw_retain_until_ms <= now_ms:
                refuse("stale")
            if self._sql(
                "SELECT 1 FROM media_clients WHERE nonce_sha256=?", (nonce_sha,)
            ).fetchone():
                refuse("conflict")
            holds = self._sql(
                "SELECT COALESCE(SUM(spool_bytes),0),COALESCE(SUM(child_slots),0),COUNT(*) FROM media_reservations WHERE state!='released'"
            ).fetchone()
            unsealed = self._sql(
                "SELECT COUNT(*) FROM media_reservations r LEFT JOIN media_sources s ON s.source_id=r.source_id WHERE r.state!='released' AND (s.source_id IS NULL OR s.local_actual NOT IN('sealed','ready','running'))"
            ).fetchone()[0]
            if (
                holds[0] + reserved
                > min(self.limits.source.owner_spool_bytes, self.limits.source.global_spool_bytes)
                or holds[1] >= self.limits.decode.children_global
                or holds[2] >= self.limits.source.operations_global
                or unsealed >= self.limits.source.unsealed_uploads_per_owner
            ):
                refuse("capacity")
            if request.replaces_source is not None:
                replaced = self._lookup_ref(token, request.replaces_source, "source")
                if (
                    replaced.body.session != request.session
                    or replaced.body.anchor != request.anchor
                    or replaced.body.scope != request.scope
                ):
                    refuse("denied")
            if (
                self._sql(
                    "SELECT COUNT(*) FROM media_dependencies WHERE state!='settled'"
                ).fetchone()[0]
                + 2
                > 16
            ):
                refuse("capacity")
            source_id, client_id, reservation_id = (secrets.token_hex(16) for _ in range(3))
            operation, _ = self._prepare(
                token, request, "admit", source_id=source_id, client_instance=client_id
            )
            self._capture_operation(original, operation)
            deadline = self.clock() + _LEASE
            display = now_ms + 30_000
            if display > _MAX:
                refuse("bound")
            self._sql(
                "INSERT INTO media_clients VALUES(?,?,?,?,?,?,?,?,?,?,NULL,'unbound',?,1,?)",
                (
                    client_id,
                    self.owner_realm_id,
                    self._auth_generation,
                    self.app_instance,
                    request.session.session_id,
                    request.session.thread_id,
                    request.session.epoch,
                    request.key,
                    nonce_sha,
                    "pending",
                    source_id,
                    display,
                ),
            )
            client = self._client(source_id)
            grant = self._grant(client)
            self._sql(
                "UPDATE media_clients SET grant_sha256=? WHERE source_id=?",
                (hashlib.sha256(grant.encode("ascii")).hexdigest(), source_id),
            )
            self._sql(
                "INSERT INTO media_reservations VALUES(?,?,?,?,?,?,1,'held')",
                (
                    reservation_id,
                    source_id,
                    self.owner_realm_id,
                    self.policy_sha256,
                    reserved,
                    self._charges["admit"],
                ),
            )
            for kind in ("upload", "decoder"):
                self._sql(
                    "INSERT INTO media_dependencies VALUES(?,?,?,?,?,NULL,'promised',NULL,'pending')",
                    (
                        secrets.token_hex(16),
                        source_id,
                        original.original_id,
                        kind,
                        secrets.token_hex(16),
                    ),
                )
            self._sql(
                "UPDATE media_operations SET phase='effect_pending' WHERE key=?", (request.key,)
            )
            return operation, (source_id, reserved, reservation_id, client_id, deadline, display)

    async def admit(self, request, principal, bootstrap_nonce):
        request = _fresh(request, m.MediaAdmit)
        if type(bootstrap_nonce) is not bytes or len(bootstrap_nonce) != 32:
            refuse("bad_packet")

        async def work(value, original):
            row, prepared = await self.run_native(
                self._admit_prepare, value, principal, bootstrap_nonce, original
            )
            if prepared is None:
                return row
            source_id, reserved, reservation, client, deadline, display = prepared
            original.unknown = True  # Before original OS reservation may take effects.
            upload = await self.run_native(self.spool.reserve, source_id, reserved)
            original.effect_result = upload
            body = m.MediaSourceAdmission(
                v=1,
                admission_id=original.original_id,
                source_id=source_id,
                source_equivalence_id=None,
                source_revision=1,
                session=value.session,
                kind=value.kind,
                purpose=value.purpose,
                anchor=value.anchor,
                replaces_source=value.replaces_source,
                capture_instance=None,
                client_instance=client,
                scope=value.scope,
                reservation=m.MediaReservation(
                    reservation_id=reservation,
                    owner_spool_bytes=reserved,
                    global_spool_bytes=reserved,
                    metadata_bytes=self._charges["admit"],
                    child_slots=1,
                    policy_sha256=self.policy_sha256,
                ),
                decoder_profile_sha256=value.decoder_profile_sha256,
                fence=m.MediaFence(
                    session=value.session, source_revision=1, generation=1, lease_generation=1
                ),
                lease_expires_at_ms=display,
                raw_retain_until_ms=value.raw_retain_until_ms,
            )
            state = _Source(
                _fresh(body, m.MediaSourceAdmission), upload, deadline, original.original_id
            )
            state.original = original
            state.current_fence = body.fence
            state.created_at = state.last_upload_at = deadline - _LEASE
            with self._mutex:
                self._sources[source_id] = state
                if original.fenced.is_set() or self._closing:
                    state.fenced.set()
            result = await self.run_native(self._admit_finish, value, principal, original, body)
            original.unknown = False
            state.expiry_task = asyncio.create_task(self._watch_source(state))
            if state.fenced.is_set() or self.clock() >= state.expires:
                state.fenced.set()
                self._schedule_cleanup(state)
                refuse("stale")
            return result

        return await self._execute(request, "admit", work, principal, bootstrap_nonce)

    def _admit_finish(self, request, principal, original, body):
        with self._boundary() as token:
            operation = self._original_row(token, original)
            source = self._sources[body.source_id]
            current = True
            try:
                self._principal(principal, {"owner"})
                self._session(body.session)
                self._anchor_guard(token, body.anchor, body.session, body.scope)
                self._memory_source(body.source_id, connected=False)
            except MediaSessionError:
                current = False
                source.fenced.set()
            receipt = self._complete(token, operation, body, "source", body.session, "owner")
            source.reference = _ref(self.backend_id, receipt)
            self._sql(
                "INSERT INTO media_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0)",
                (
                    body.source_id,
                    receipt.id,
                    self.owner_realm_id,
                    body.client_instance,
                    body.session.session_id,
                    body.session.thread_id,
                    body.session.epoch,
                    body.anchor.receipt_id,
                    self.profile.profile_sha256,
                    self.policy_sha256,
                    body.reservation.reservation_id,
                    m.media_bytes(body.fence),
                    None,
                    "running" if current else "stopped",
                    "reserved" if current else "fenced",
                    "retained" if body.raw_retain_until_ms is not None else "buffered",
                ),
            )
            self._sql(
                "UPDATE media_dependencies SET state='active',publication_state='settled' WHERE source_id=? AND kind='upload'",
                (body.source_id,),
            )
            for dependency in self._sql(
                "SELECT * FROM media_dependencies WHERE source_id=? AND kind IN('upload','decoder')",
                (body.source_id,),
            ).fetchall():
                child = self._create_child_owner(token, receipt, operation, dependency)
                source.children[dependency["kind"]] = child
            if not current:
                self._sql(
                    "UPDATE media_clients SET connection_state='fenced' WHERE source_id=?",
                    (body.source_id,),
                )
            if request.replaces_source is not None:
                previous = self._lookup_ref(token, request.replaces_source, "source")
                self._fence_source_locked(token, previous.body.source_id, "replacement")
            return receipt

    def _native_append(self, token, request, kind, result_kind, session, source_id):
        self._locked(token)
        parent = self._sql(
            "SELECT * FROM media_operations WHERE original_operation_id=? AND kind='admit' AND source_id=?",
            (request.value.operation_id, source_id),
        ).fetchone()
        if (
            parent is None
            or parent["phase"] != "committed"
            or parent["charged_metadata_bytes"] != self._charges["admit"]
        ):
            refuse()
        existing_count = self._sql(
            "SELECT COUNT(*) FROM media_operations WHERE kind=? AND source_id=?", (kind, source_id)
        ).fetchone()[0]
        old = self._sql("SELECT 1 FROM media_operations WHERE key=?", (request.key,)).fetchone()
        if old is None and existing_count >= 2:
            refuse("capacity")
        operation, fresh = self._prepare(
            token, request, kind, source_id=source_id, client_instance=parent["client_instance"]
        )
        if not fresh:
            return self._existing(operation)
        return self._complete(token, operation, request.value, result_kind, session, "session")

    def _create_child_owner(self, token, source_receipt, operation, dependency):
        reference = _ref(self.backend_id, source_receipt)
        source = source_receipt.body
        binding = {
            "source": json.loads(m.media_bytes(reference)),
            "fence": json.loads(m.media_bytes(source.fence)),
            "parent_request_sha256": operation["request_sha256"],
            "kind": dependency["kind"],
            "instance": dependency["original_instance"],
            "operation_id": operation["original_operation_id"],
            "backend_id": self.backend_id,
            "consumer": None,
        }
        body = m.MediaChildOwner(
            v=1,
            kind=dependency["kind"],
            instance=dependency["original_instance"],
            operation_id=operation["original_operation_id"],
            source=reference,
            consumer=None,
            binding_sha256=hashlib.sha256(
                b"kinaiya.media.child-binding.v1\0" + _canonical(binding)
            ).hexdigest(),
            backend_id=self.backend_id,
            peer={
                "kind": "native_task",
                "backend_instance": self.app_instance,
                "task_id": dependency["dependency_id"],
                "request_sha256": operation["request_sha256"],
            },
        )
        request = m.MediaChildOwnerAppend(
            v=1,
            key="media:child-owner:" + dependency["dependency_id"],
            consumer=None,
            source=reference,
            fence=source.fence,
            value=body,
        )
        receipt = self._native_append(
            token, request, "child_owner_append", "child_owner", source.session, source.source_id
        )
        self._sql(
            "UPDATE media_dependencies SET child_owner_receipt_id=? WHERE dependency_id=?",
            (receipt.id, dependency["dependency_id"]),
        )
        return receipt

    def _upload_begin(self, header, principal, intake_id):
        with self._transaction(principal, {"owner"}) as token:
            row, source = self._source_guard(token, header.source, header.fence, principal)
            if source.upload.sealed:
                old = self._sql(
                    'SELECT * FROM media_uploads WHERE source_id=? AND "index"=?',
                    (row["source_id"], header.index),
                ).fetchone()
                if old is None or (
                    old["offset"],
                    old["body_bytes"],
                    old["body_sha256"],
                ) != (header.offset, header.body_bytes, header.body_sha256):
                    refuse("conflict")
            if header.body_bytes > min(
                self.limits.source.upload_chunk_bytes, self.limits.wire.body_bytes
            ):
                refuse("bound")
            if header.offset + header.body_bytes > source.admission.reservation.owner_spool_bytes:
                refuse("bound")
            prior = self._sql(
                'SELECT * FROM media_uploads WHERE source_id=? AND "index"=?',
                (row["source_id"], header.index),
            ).fetchone()
            if prior is not None:
                if (prior["offset"], prior["body_bytes"], prior["body_sha256"]) != (
                    header.offset,
                    header.body_bytes,
                    header.body_sha256,
                ):
                    refuse("conflict")
            else:
                tail = self._sql(
                    'SELECT "index",next_offset FROM media_uploads WHERE source_id=? ORDER BY "index" DESC LIMIT 1',
                    (row["source_id"],),
                ).fetchone()
                if (header.index, header.offset) != (
                    (0, 0) if tail is None else (tail[0] + 1, tail[1])
                ):
                    refuse("conflict")
            operation, _ = self._prepare(
                token,
                header,
                "upload",
                source_id=row["source_id"],
                client_instance=row["client_instance"],
            )
            if operation["phase"] not in {"prepared", "committed"}:
                # Only an actual retained SAME-app original may reconcile pending work.
                original = self._operations.get(header.key)
                if original is None or original.original_id != operation["original_operation_id"]:
                    refuse()
            if (
                self._sql(
                    "SELECT COUNT(*) FROM media_dependencies WHERE state!='settled'"
                ).fetchone()[0]
                >= 16
            ):
                refuse("capacity")
            with self._mutex:
                if self._metadata_hold + self._intake_charge > _METADATA:
                    refuse("capacity")
                self._metadata_hold += self._intake_charge
            child = source.children.get("upload")
            if child is None:
                refuse()
            self._sql(
                "INSERT INTO media_dependencies VALUES(?,?,?,?,?,?,'active',NULL,'pending')",
                (
                    intake_id,
                    row["source_id"],
                    operation["original_operation_id"],
                    "intake",
                    intake_id,
                    child.id,
                ),
            )
            self._sql(
                "UPDATE media_reservations SET metadata_bytes=metadata_bytes+? WHERE source_id=?",
                (self._intake_charge, row["source_id"]),
            )
            return row["source_id"]

    def _upload_prepare(self, header, principal, original):
        with self._transaction(principal, {"owner"}) as token:
            row, source = self._source_guard(token, header.source, header.fence, principal)
            operation, fresh = self._prepare(
                token,
                header,
                "upload",
                source_id=row["source_id"],
                client_instance=row["client_instance"],
            )
            self._capture_operation(original, operation)
            if not fresh and operation["phase"] == "committed":
                return self._existing(operation), source
            if operation["original_app_instance"] != self.app_instance or operation[
                "phase"
            ] not in {"prepared", "effect_pending"}:
                refuse()
            # Validate the full all-index ledger before the single original write.
            old = self._sql(
                'SELECT * FROM media_uploads WHERE source_id=? AND "index"=?',
                (row["source_id"], header.index),
            ).fetchone()
            if old is not None:
                if (old["offset"], old["body_bytes"], old["body_sha256"], old["next_offset"]) != (
                    header.offset,
                    header.body_bytes,
                    header.body_sha256,
                    header.offset + header.body_bytes,
                ):
                    refuse("conflict")
            else:
                previous = self._sql(
                    'SELECT "index",next_offset FROM media_uploads WHERE source_id=? ORDER BY "index" DESC LIMIT 1',
                    (row["source_id"],),
                ).fetchone()
                expected = (0, 0) if previous is None else (previous[0] + 1, previous[1])
                if (header.index, header.offset) != expected:
                    refuse("conflict")
            self._sql(
                "UPDATE media_operations SET phase='effect_pending' WHERE key=?", (header.key,)
            )
            return operation, source

    async def upload(self, body, principal):
        if type(body) is not IngressBody or type(body.header) is not m.MediaUploadPut:
            refuse("bad_packet")
        header = body.header
        intake_id = secrets.token_hex(16)
        with self._mutex:
            source_id = next(
                (key for key, value in self._sources.items() if value.reference == header.source),
                None,
            )
            source_intakes = [
                item for item in self._intakes.values() if item["source_id"] == source_id
            ]
            if (
                self._closing
                or len(self._intakes) >= 16
                or len(source_intakes) >= self.limits.wire.consumer_packets
                or sum(item["body_bytes"] for item in source_intakes) + header.body_bytes
                > self.limits.wire.consumer_bytes
                or self._pending_bytes + header.body_bytes > self.limits.wire.total_pending_bytes
            ):
                refuse("capacity")
            self._intakes[intake_id] = {
                "task": asyncio.current_task(),
                "source_id": source_id,
                "body_bytes": header.body_bytes,
                "unknown": True,
            }
            self._pending_bytes += header.body_bytes
        registered = False
        try:
            source_id = await self.run_native(self._upload_begin, header, principal, intake_id)
            self._intakes[intake_id]["source_id"] = source_id
            registered = True
            data = bytearray()
            async for chunk in body.iter_chunks():
                self._memory_source(source_id)
                if len(data) + len(chunk) > min(
                    header.body_bytes, self.limits.source.upload_chunk_bytes
                ):
                    refuse("bound")
                data.extend(chunk)
            if not body.verified or len(data) != header.body_bytes:
                refuse("bad_packet")
            payload = bytes(data)

            async def work(value, original):
                operation, source = await self.run_native(
                    self._upload_prepare, value, principal, original
                )
                if not hasattr(operation, "body"):
                    original.unknown = True
                    self._memory_source(source_id)
                    progress = await self.run_native(
                        source.upload.append, value.index, value.offset, payload, value.body_sha256
                    )
                    original.effect_result = progress
                    source.last_upload_at = self.clock()
                    receipt = await self.run_native(self._upload_finish, value, original, progress)
                    original.unknown = False
                    return receipt
                return operation

            result = await self._execute(header, "upload", work, principal)
            # EVERY HTTP delivery, including exact key replay, verifies its actual
            # body and the original on-disk duplicate. No cached-body bypass.
            source = self._memory_source(source_id)
            await self.run_native(
                source.upload.append, header.index, header.offset, payload, header.body_sha256
            )
            await self.run_native(self._upload_ack_guard, header, principal, intake_id)
            self._intakes[intake_id]["unknown"] = False
            return result
        except BaseException:
            if registered:
                source = self._sources.get(self._intakes[intake_id]["source_id"])
                if source is not None:
                    source.fenced.set()
                    source.reason = "publication_unknown"
            raise
        finally:
            with self._mutex:
                intake = self._intakes[intake_id]
                if not registered or not intake["unknown"]:
                    self._intakes.pop(intake_id, None)
                    self._pending_bytes -= header.body_bytes

    def _upload_finish(self, header, original, progress):
        if (
            progress.index,
            progress.offset,
            progress.body_bytes,
            progress.body_sha256,
            progress.next_offset,
        ) != (
            header.index,
            header.offset,
            header.body_bytes,
            header.body_sha256,
            header.offset + header.body_bytes,
        ):
            refuse()
        with self._boundary() as token:
            operation = self._original_row(token, original)
            source = self._lookup_ref(token, header.source, "source").body
            result = self._complete(
                token,
                operation,
                m.MediaUploadAck(
                    v=1,
                    source=header.source,
                    index=header.index,
                    offset=header.offset,
                    body_bytes=header.body_bytes,
                    body_sha256=header.body_sha256,
                    next_offset=progress.next_offset,
                ),
                "upload_ack",
                source.session,
                "owner",
            )
            existing = self._sql(
                'SELECT * FROM media_uploads WHERE source_id=? AND "index"=?',
                (source.source_id, header.index),
            ).fetchone()
            if existing is None:
                self._sql(
                    "INSERT INTO media_uploads VALUES(?,?,?,?,?,?,?,?)",
                    (
                        source.source_id,
                        header.index,
                        header.key,
                        header.offset,
                        header.body_bytes,
                        header.body_sha256,
                        progress.next_offset,
                        result.id,
                    ),
                )
            elif (existing["offset"], existing["body_bytes"], existing["body_sha256"]) != (
                header.offset,
                header.body_bytes,
                header.body_sha256,
            ):
                refuse()
            if not self._sources[source.source_id].fenced.is_set():
                self._sql(
                    "UPDATE media_sources SET local_actual='uploading' WHERE source_id=?",
                    (source.source_id,),
                )
            return result

    def _upload_ack_guard(self, header, principal, intake_id):
        with self._transaction(principal, {"owner"}) as token:
            self._source_guard(token, header.source, header.fence, principal)
            self._sql(
                "UPDATE media_dependencies SET state='settled',publication_state='settled' WHERE dependency_id=? AND original_instance=?",
                (intake_id, intake_id),
            )

    def _seal_prepare(self, request, principal, original):
        with self._transaction(principal, {"converse"}) as token:
            row, source = self._source_guard(token, request.source, request.fence, principal)
            admission = self._sql(
                "SELECT canonical_request FROM media_operations WHERE original_operation_id=? AND kind='admit'",
                (source.original_operation_id,),
            ).fetchone()
            if admission is None:
                refuse()
            admitted = m.media_json(bytes(admission[0]), m.MediaAdmit)
            if (
                admitted.declared_bytes is not None
                and request.final_bytes != admitted.declared_bytes
                or admitted.declared_sha256 is not None
                and request.source_sha256 != admitted.declared_sha256
            ):
                refuse("conflict")
            if request.final_bytes > source.admission.reservation.owner_spool_bytes:
                refuse("bound")
            last = self._sql(
                'SELECT next_offset FROM media_uploads WHERE source_id=? ORDER BY "index" DESC LIMIT 1',
                (row["source_id"],),
            ).fetchone()
            if last is None or last[0] != request.final_bytes:
                refuse("conflict")
            operation, _ = self._prepare(
                token,
                request,
                "seal",
                source_id=row["source_id"],
                client_instance=row["client_instance"],
            )
            self._capture_operation(original, operation)
            if operation["phase"] == "committed":
                return self._existing(operation), source
            if (
                operation["phase"] not in {"prepared", "effect_pending"}
                or operation["original_app_instance"] != self.app_instance
            ):
                refuse()
            self._sql(
                "UPDATE media_operations SET phase='effect_pending' WHERE key=?", (request.key,)
            )
            return operation, source

    async def seal(self, request, principal):
        request = _fresh(request, m.MediaSealRequest)

        async def work(value, original):
            operation, source = await self.run_native(
                self._seal_prepare, value, principal, original
            )
            if hasattr(operation, "body"):
                return operation
            original.unknown = True
            self._memory_source(source.admission.source_id)
            await self.run_native(source.upload.seal, value.final_bytes, value.source_sha256)
            # This exact returned call/arguments is retained; .sealed is no proof.
            original.effect_result = (value.final_bytes, value.source_sha256)
            source.sealed_result = original.effect_result
            source.sealed_at = self.clock()
            result = await self.run_native(self._seal_finish, value, original)
            original.unknown = False
            self._memory_source(source.admission.source_id)
            return result

        result = await self._execute(request, "seal", work, principal)
        await self.run_native(self._seal_ack_guard, request, principal)
        return result

    def _seal_ack_guard(self, request, principal):
        with self._transaction(principal, {"converse"}) as token:
            self._source_guard(token, request.source, request.fence, principal)

    def _seal_finish(self, request, original):
        if original.effect_result != (request.final_bytes, request.source_sha256):
            refuse()
        with self._boundary() as token:
            operation = self._original_row(token, original)
            admission = self._lookup_ref(token, request.source, "source").body
            anchor = self._lookup_ref(token, admission.anchor, "anchor").body
            objective_sha = hashlib.sha256(m.media_bytes(anchor.owner_objective)).hexdigest()
            old = self._sql(
                "SELECT * FROM media_equivalences WHERE owner_objective_sha256=? AND source_sha256=? AND source_bytes=?",
                (objective_sha, request.source_sha256, request.final_bytes),
            ).fetchone()
            equivalence = secrets.token_hex(16) if old is None else old["equivalence_id"]
            body = m.MediaSeal(
                v=1,
                source=request.source,
                source_equivalence_id=equivalence,
                fence=request.fence,
                reservation_id=admission.reservation.reservation_id,
                bytes=request.final_bytes,
                source_sha256=request.source_sha256,
                decoder_profile_sha256=admission.decoder_profile_sha256,
                state="sealed",
            )
            receipt = self._complete(token, operation, body, "seal", admission.session, "converse")
            if old is None:
                self._sql(
                    "INSERT INTO media_equivalences VALUES(?,?,?,?,?)",
                    (
                        objective_sha,
                        request.source_sha256,
                        request.final_bytes,
                        equivalence,
                        receipt.id,
                    ),
                )
            state = self._sources[admission.source_id]
            self._sql(
                "UPDATE media_sources SET equivalence_id=?,local_actual=? WHERE source_id=?",
                (equivalence, "fenced" if state.fenced.is_set() else "sealed", admission.source_id),
            )
            return receipt

    def _owner_entry(self, row):
        if row is None or row["writer"] != "owner":
            refuse("denied")
        request_data = self.store._agent_submit_bytes(row["request"])
        value_data = self.store._agent_submit_bytes(row["value"])
        if (
            type(request_data) is not dict
            or set(request_data) != set(AppendRequest.model_fields)
            or type(value_data) is not dict
            or set(value_data) != set(JournalEntry.model_fields)
            or request_data != {name: value_data[name] for name in AppendRequest.model_fields}
        ):
            refuse()
        event = JournalEntry.model_validate_json(row["value"], strict=True)
        if (
            event.origin != "authenticated-user"
            or any(
                row[name] != getattr(event, name)
                for name in ("event_id", "sequence", "session_id", "thread_id", "epoch", "kind")
            )
            or row["idem"] != event.idempotency_key
        ):
            refuse()
        return event

    def _owner_event(self, event_id, session):
        event = self._owner_entry(
            self._sql("SELECT * FROM entries WHERE event_id=?", (event_id,)).fetchone()
        )
        if (
            event.kind != "conversation.user"
            or event.payload.get("final") is not True
            or event.session_id != session.session_id
            or event.thread_id != session.thread_id
            or event.epoch != session.epoch
            or type(event.payload.get("text")) is not str
        ):
            refuse("denied")
        after = event.sequence
        while True:
            rows = self._sql(
                "SELECT * FROM entries WHERE session_id=? AND kind='conversation.revision' AND sequence>? AND json_extract(value,'$.payload.target_event_id')=? ORDER BY sequence LIMIT 64",
                (session.session_id, after, event_id),
            ).fetchall()
            if not rows:
                break
            for revision in rows:
                updated = self._owner_entry(revision)
                after = updated.sequence
                if updated.thread_id != session.thread_id or updated.epoch != session.epoch:
                    refuse("denied")
        return self.store.resolve(event_id)

    def _task_truth(self, token, event_id, session):
        self._locked(token)
        self._session(session)
        row = self._sql("SELECT * FROM entries WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            refuse("unknown_ref")
        initial = self.store._agent_submit_check_row(row)
        initial = self.store._agent_submit_winner(
            event_id, session.session_id, initial.payload["idempotency_key"]
        )
        if (
            initial.session_id != session.session_id
            or initial.thread_id != session.thread_id
            or initial.epoch != session.epoch
            or initial.payload.get("actor") != "owner"
            or len(initial.payload["source_event_ids"]) != 1
        ):
            refuse("denied")
        objective = self._owner_event(initial.payload["source_event_ids"][0], session)
        source_revision = objective.payload.get("revision", 1)
        if (
            type(source_revision) is not int
            or source_revision != initial.payload["source_revision"]
            or objective.payload.get("text") != initial.payload["objective"]
            or objective.payload.get("content_scope", "local_only")
            != initial.payload.get("content_scope", "local_only")
        ):
            refuse("stale")
        revision, scope, state, _ = self._control._history(initial)
        if revision != initial.payload["objective_revision"]:
            refuse("stale")
        return (
            initial,
            revision,
            scope,
            state,
            m.MediaOwnerTextObjective(kind="owner_text", event_ref=objective.event_id),
        )

    def task_proof(self, request, principal):
        request = _fresh(request, m.MediaTaskProofRequest)
        with self._transaction(principal, {"owner", "agent"}) as token:
            operation, fresh = self._prepare(token, request, "task_proof")
            if not fresh:
                return self._existing(operation)
            initial, revision, _, _, objective = self._task_truth(
                token, request.original_admission_id, request.session
            )
            raw = (
                self._sql("SELECT request FROM entries WHERE event_id=?", (initial.event_id,))
                .fetchone()[0]
                .encode("utf-8")
            )
            body = m.MediaTaskProof(
                v=1,
                session=request.session,
                task_id=initial.payload["task_id"],
                objective_revision=revision,
                owner_objective=objective,
                request_sha256=hashlib.sha256(raw).hexdigest(),
                original_admission_id=initial.event_id,
                state="accepted",
            )
            result = self._complete(
                token, operation, body, "task_proof", request.session, principal.role
            )
            # Full native task/source history at final publication, unchanged helper.
            self._task_truth(token, request.original_admission_id, request.session)
            return result

    def anchor(self, request, principal):
        request = _fresh(request, m.MediaAnchorAdmission)
        with self._transaction(principal, {"owner"}) as token:
            if request.submit is not None:
                refuse("unavailable")
            operation, fresh = self._prepare(token, request, "anchor")
            if not fresh:
                return self._existing(operation)
            proof = self._lookup_ref(token, request.task_admission, "task_proof")
            body = proof.body
            if body.state != "accepted" or body.owner_objective != request.owner_objective:
                refuse("denied")
            initial, revision, scope, state, objective = self._task_truth(
                token, body.original_admission_id, body.session
            )
            if (
                objective != request.owner_objective
                or scope != request.scope
                or body.objective_revision != revision
                or body.task_id != initial.payload["task_id"]
                or state in {"succeeded", "failed", "cancelled", "cancel_requested"}
            ):
                refuse("stale")
            objective_pin = m.media_bytes(objective)
            objective_sha = hashlib.sha256(objective_pin).hexdigest()
            if self._sql(
                "SELECT 1 FROM media_objective_anchors WHERE owner_objective_sha256=?",
                (objective_sha,),
            ).fetchone():
                refuse("conflict")
            anchor = m.MediaAnalyzeTaskAnchor(
                v=1,
                owner_objective=objective,
                session=body.session,
                task_id=body.task_id,
                initial_objective_revision=revision,
                task_admission=request.task_admission,
                scope=scope,
                allowed_operation="evidence_only",
            )
            result = self._complete(token, operation, anchor, "anchor", body.session, "owner")
            self._sql(
                "INSERT INTO media_objective_anchors VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    objective_sha,
                    objective_pin,
                    self.owner_realm_id,
                    body.session.session_id,
                    body.session.thread_id,
                    body.session.epoch,
                    initial.event_id,
                    body.task_id,
                    revision,
                    proof.id,
                    result.id,
                ),
            )
            self._task_truth(token, body.original_admission_id, body.session)
            return result

    def _status(self, token, row):
        self._locked(token)
        self._read_receipt(
            self._sql(
                "SELECT * FROM media_receipts WHERE receipt_id=?", (row["admission_receipt_id"],)
            ).fetchone()
        )
        reservation = self._sql(
            "SELECT * FROM media_reservations WHERE reservation_id=?", (row["reservation_id"],)
        ).fetchone()
        if reservation is None:
            refuse()
        children = []
        for dependency in self._sql(
            "SELECT * FROM media_dependencies WHERE source_id=? AND kind IN('upload','decoder') ORDER BY dependency_id",
            (row["source_id"],),
        ).fetchall():
            owner = self._sql(
                "SELECT * FROM media_receipts WHERE receipt_id=?",
                (dependency["child_owner_receipt_id"],),
            ).fetchone()
            if owner is None:
                refuse()
            receipt = self._read_receipt(owner)
            cleanup = None
            outcome = None
            if dependency["cleanup_receipt_id"] is not None:
                cleanup_row = self._sql(
                    "SELECT * FROM media_receipts WHERE receipt_id=?",
                    (dependency["cleanup_receipt_id"],),
                ).fetchone()
                if cleanup_row is None:
                    refuse()
                cleanup_receipt = self._read_receipt(cleanup_row)
                cleanup = _ref(self.backend_id, cleanup_receipt)
                outcome = cleanup_receipt.body.outcome
            child_state = {
                "promised": "reserved",
                "active": "running",
                "settled": "settled",
                "unknown": "unknown",
            }[dependency["state"]]
            if row["desired"] != "running" and child_state not in {"settled", "unknown"}:
                child_state = "closing"
            children.append(
                m.MediaChild(
                    kind=dependency["kind"],
                    instance=receipt.body.instance,
                    operation_id=receipt.body.operation_id,
                    owner=_ref(self.backend_id, receipt),
                    binding_sha256=receipt.body.binding_sha256,
                    state=child_state,
                    outcome=outcome,
                    cleanup=cleanup,
                )
            )
        source = self._sources.get(row["source_id"])
        reason = (
            source.reason
            if source is not None
            else ("deleted" if row["raw_state"] == "deleted" else "cleanup_unknown")
        )
        released = reservation["state"] == "released"
        return m.MediaStatus(
            v=1,
            source=m.MediaNativeRef(
                backend_id=self.backend_id,
                receipt_id=row["admission_receipt_id"],
                sha256=self._sql(
                    "SELECT sha256 FROM media_receipts WHERE receipt_id=?",
                    (row["admission_receipt_id"],),
                ).fetchone()[0],
            ),
            fence=m.media_json(bytes(row["current_fence"]), m.MediaFence),
            desired=row["desired"],
            local_actual=row["local_actual"],
            children=children,
            remote_state="none",
            remote_liability_head=None,
            reason=reason,
            reservation=m.MediaReservation(
                reservation_id=reservation["reservation_id"],
                owner_spool_bytes=0 if released else reservation["spool_bytes"],
                global_spool_bytes=0 if released else reservation["spool_bytes"],
                metadata_bytes=reservation["metadata_bytes"],
                child_slots=0 if released else reservation["child_slots"],
                policy_sha256=reservation["policy_sha256"],
            ),
            raw_state=row["raw_state"],
        )

    def _fence_reference_memory(self, reference, fence, reason, principal):
        self._principal_memory(principal, {"owner"})
        with self._mutex:
            for source in self._sources.values():
                if source.reference == reference and source.current_fence == fence:
                    source.fenced.set()
                    source.reason = reason
                    for original in self._operations.values():
                        if original.source_id == source.admission.source_id:
                            original.fenced.set()
                    return source
        return None

    async def control(self, request, principal):
        request = _fresh(request, m.MediaControl)
        if request.command in {"start", "resume"}:
            refuse("unavailable")
        reason = {"stop": "owner_stop", "reset": "reset", "seek": "superseded"}.get(request.command)
        if reason is not None:
            self._fence_reference_memory(request.source, request.fence, reason, principal)

        def write(original):
            renewal = None
            previous_expiry = None
            with self._transaction(principal, {"owner"}) as token:
                operation, fresh = self._prepare(token, request, "control")
                if not fresh:
                    return self._existing(operation)
                if request.command == "heartbeat":
                    row, source = self._source_guard(
                        token, request.source, request.fence, principal
                    )
                    previous_expiry = source.expires
                    renewal = self.clock() + _LEASE
                    display = self.wall_clock_ms() + 30_000
                    if display > _MAX:
                        refuse("bound")
                    self._sql(
                        "UPDATE media_clients SET display_expires_at_ms=? WHERE source_id=?",
                        (display, row["source_id"]),
                    )
                else:
                    row, source = self._source_guard(
                        token, request.source, request.fence, principal, revocation=True
                    )
                    self._fence_source_locked(token, row["source_id"], reason)
                    if request.command in {"reset", "seek"}:
                        current = m.media_json(
                            bytes(
                                self._sql(
                                    "SELECT current_fence FROM media_sources WHERE source_id=?",
                                    (row["source_id"],),
                                ).fetchone()[0]
                            ),
                            m.MediaFence,
                        )
                        if current.generation == _MAX:
                            refuse("bound")
                        current = m.MediaFence(
                            session=current.session,
                            source_revision=current.source_revision,
                            generation=current.generation + 1,
                            lease_generation=current.lease_generation,
                        )
                        self._sql(
                            "UPDATE media_sources SET current_fence=? WHERE source_id=?",
                            (m.media_bytes(current), row["source_id"]),
                        )
                        if source is not None:
                            source.current_fence = current
                self._sql(
                    "UPDATE media_operations SET source_id=?,client_instance=? WHERE key=?",
                    (row["source_id"], row["client_instance"], request.key),
                )
                operation = self._sql(
                    "SELECT * FROM media_operations WHERE key=?", (request.key,)
                ).fetchone()
                self._capture_operation(original, operation)
                row = self._sql(
                    "SELECT * FROM media_sources WHERE source_id=?", (row["source_id"],)
                ).fetchone()
                if renewal is not None:
                    self._source_guard(token, request.source, request.fence, principal)
                receipt = self._complete(
                    token,
                    operation,
                    self._status(token, row),
                    "status",
                    request.fence.session,
                    "owner",
                )
                self._sql(
                    "UPDATE media_sources SET current_status_receipt_id=? WHERE source_id=?",
                    (receipt.id, row["source_id"]),
                )
            if renewal is not None:
                with self._mutex:
                    if source.fenced.is_set() or self.clock() >= previous_expiry:
                        source.fenced.set()
                        source.reason = "lease_expired"
                        refuse("stale")
                    source.expires = renewal
            return receipt

        async def work(value, original):
            result = await self.run_native(write, original)
            if reason is not None:
                source = next(
                    (item for item in self._sources.values() if item.reference == request.source),
                    None,
                )
                if source is not None:
                    self._schedule_cleanup(source)
            return result

        return await self._execute(request, "control", work, principal)

    def _raw_expired(self, source):
        now = self.clock()
        retain = source.admission.raw_retain_until_ms
        return (
            (retain is not None and self.wall_clock_ms() >= retain)
            or now - source.created_at >= 86_400
            or (source.sealed_at is None and now - source.last_upload_at >= 1800)
            or (
                source.sealed_at is not None
                and now - source.sealed_at
                >= min(1_800_000, self.limits.source.sealed_unstarted_ms) / 1000
            )
        )

    async def _watch_source(self, source):
        # Registered on the exact original Source before scheduling; this is a
        # negative expiry observer, never a lease renewal or decoder dispatch.
        while not self._closing:
            if self._raw_expired(source):
                source.fenced.set()
                source.reason = "expired"

                def expire():
                    with self._boundary() as token:
                        self._fence_source_locked(token, source.admission.source_id, "expired")
                        self._sql(
                            "UPDATE media_sources SET raw_state='expired' WHERE source_id=? AND raw_state!='deleted'",
                            (source.admission.source_id,),
                        )

                await self.run_native(expire)
                await asyncio.shield(self._schedule_cleanup(source))
                return
            if self.clock() >= source.expires:
                source.fenced.set()
                source.reason = "lease_expired"

                def fence():
                    with self._boundary() as token:
                        self._fence_source_locked(
                            token, source.admission.source_id, "lease_expired"
                        )

                await self.run_native(fence)
                await asyncio.shield(self._schedule_cleanup(source))
                if source.admission.raw_retain_until_ms is None:
                    return
            await asyncio.sleep(1.0)

    def _schedule_cleanup(self, source):
        # Closing observes the same original cleanup, including a false/unknown
        # result; late callbacks cannot start a replacement physical attempt.
        if self._closing and source.delete_task is not None:
            return source.delete_task
        if self._close_operation is not None and self._close_operation.sealed:
            refuse()
        if source.delete_task is None or source.delete_task.done():
            source.delete_task = asyncio.create_task(self._cleanup_source(source))

            def observed(done):
                if not done.cancelled() and done.exception() is None and done.result() is True:
                    with self._mutex:
                        for key, original in tuple(self._retirement_requests.items()):
                            if original.source_id == source.admission.source_id:
                                self._retirement_requests.pop(key, None)

            source.delete_task.add_done_callback(observed)
        return source.delete_task

    def current_status(self, source_id, source_receipt_id, principal):
        if (
            type(source_id) is not str
            or type(source_receipt_id) is not str
            or len(source_id) != 32
            or len(source_receipt_id) != 32
            or any(char not in "0123456789abcdef" for char in source_id + source_receipt_id)
        ):
            refuse("bad_packet")
        with self._transaction(principal, {"owner"}) as token:
            row = self._sql(
                "SELECT * FROM media_sources WHERE source_id=?", (source_id,)
            ).fetchone()
            if row is None or row["admission_receipt_id"] != source_receipt_id:
                refuse("unknown_ref")
            source = self._sources.get(source_id)
            if source is not None:
                if self.clock() >= source.expires:
                    source.fenced.set()
                    source.reason = "lease_expired"
                if source.fenced.is_set():
                    self._fence_source_locked(token, source_id, source.reason)
                    row = self._sql(
                        "SELECT * FROM media_sources WHERE source_id=?", (source_id,)
                    ).fetchone()
            value = self._status(token, row)
            if row["current_status_receipt_id"] is not None:
                old = self._sql(
                    "SELECT * FROM media_receipts WHERE receipt_id=?",
                    (row["current_status_receipt_id"],),
                ).fetchone()
                if old is None or old["kind"] != "status":
                    refuse()
                receipt = self._read_receipt(old)
                if m.media_bytes(receipt.body) == m.media_bytes(value):
                    return receipt
            request = m.MediaStatusAppend(
                v=1,
                key="media:status:" + secrets.token_hex(16),
                consumer=None,
                source=value.source,
                fence=value.fence,
                value=value,
            )
            operation, _ = self._prepare(
                token,
                request,
                "status_append",
                source_id=source_id,
                client_instance=row["client_instance"],
            )
            receipt = self._complete(
                token, operation, value, "status", value.fence.session, "session"
            )
            self._sql(
                "UPDATE media_sources SET current_status_receipt_id=? WHERE source_id=?",
                (receipt.id, source_id),
            )
            return receipt

    def _store_cleanup(self, token, source, kind, outcome):
        dependency = self._sql(
            "SELECT * FROM media_dependencies WHERE source_id=? AND kind=?",
            (source.admission.source_id, kind),
        ).fetchone()
        if dependency is None or source.children.get(kind) is None:
            refuse()
        owner = source.children[kind]
        if dependency["child_owner_receipt_id"] != owner.id:
            refuse()
        body = m.MediaWorkerCleanup(
            v=1,
            kind="native_owner",
            operation_id=source.original_operation_id,
            child_instance=owner.body.instance,
            binding_sha256=owner.body.binding_sha256,
            original_owner=_ref(self.backend_id, owner),
            state="settled",
            outcome=outcome,
            proof="local_join",
            publication="settled",
            scope="native_task",
        )
        request = m.MediaWorkerCleanupAppend(
            v=1,
            key="media:cleanup:" + dependency["dependency_id"],
            consumer=None,
            source=source.reference,
            fence=source.admission.fence,
            value=body,
        )
        receipt = self._native_append(
            token,
            request,
            "cleanup_append",
            "cleanup",
            source.admission.session,
            source.admission.source_id,
        )
        self._sql(
            "UPDATE media_dependencies SET state='settled',cleanup_receipt_id=?,publication_state='settled' WHERE dependency_id=?",
            (receipt.id, dependency["dependency_id"]),
        )

    @staticmethod
    def _actual_task(task, loop, coroutine):
        return (
            type(task) is asyncio.Task and task.get_loop() is loop and task.get_coro() is coroutine
        )

    @staticmethod
    def _cleanup_probe_joined(source):
        probe = source.cleanup_probe
        if probe is None:
            return True
        if probe.unknown:
            return False
        if probe.no_dispatch and probe.coroutine is None:
            return probe.retired
        task = probe.task
        return (
            MediaSessionOwner._actual_task(task, probe.loop, probe.coroutine)
            and probe.entered_task is task
            and probe.completed_task is task
            and probe.setup_complete
            and probe.retired
            and task.done()
            and not task.cancelled()
            and task.exception() is None
            and task.result() == probe.outcome
        )

    @staticmethod
    def _cleanup_continuation_joined(source):
        continuation = source.cleanup_continuation
        return continuation is None or (
            not continuation.unknown
            and MediaSessionOwner._actual_task(
                continuation.task, continuation.loop, continuation.coroutine
            )
            and continuation.entered_task is continuation.task
            and continuation.completed_task is continuation.task
            and continuation.task.done()
            and not continuation.task.cancelled()
            and continuation.task.exception() is None
        )

    def _cleanup_checkpoint_matches(self, source, checkpoint):
        return (
            not self._closing
            and not (self._close_operation is not None and self._close_operation.sealed)
            and self._sources.get(source.admission.source_id) is source
            and type(checkpoint) is _CleanupCheckpoint
            and source.cleanup_checkpoint is checkpoint
            and source.cleanup_continuation is None
            and (
                source.delete_task is checkpoint.task
                or source.cleanup_probe is not None
                and not source.cleanup_probe.unknown
                and source.cleanup_probe.checkpoint is checkpoint
                and source.delete_task is source.cleanup_probe.placeholder
            )
            and source.upload is checkpoint.upload
            and source.connection is checkpoint.connection
            and (
                source.connection is None
                or source.connection.joined
                and not source.connection.unknown
            )
            and checkpoint.task.done()
            and not checkpoint.task.cancelled()
            and checkpoint.task.exception() is None
            and checkpoint.task.result() is False
            and not any(
                item.unknown
                for item in self._operations.values()
                if item.source_id == source.admission.source_id
            )
            and not any(
                item["source_id"] == source.admission.source_id for item in self._intakes.values()
            )
        )

    def _cleanup_reconcile_guard(self, request, principal, original, source, checkpoint, fence):
        with self._transaction(principal, {"owner"}) as token:
            operation = self._sql(
                "SELECT * FROM media_operations WHERE key=?", (request.key,)
            ).fetchone()
            if (
                operation is None
                or self._operations.get(original.key) is not original
                or operation["kind"] != "delete"
                or operation["phase"] != "prepared"
                or operation["original_operation_id"] != original.original_id
                or operation["candidate_receipt_id"] != original.candidate_id
                or operation["request_sha256"] != original.digest
                or bytes(operation["canonical_request"]) != m.media_bytes(request)
            ):
                refuse()
            _, selected = self._source_guard(
                token, request.source, fence, principal, revocation=True
            )
            with self._mutex:
                if (
                    selected is not source
                    or source.current_fence != fence
                    or not self._cleanup_checkpoint_matches(source, checkpoint)
                ):
                    refuse("conflict")

    def _session_cleanup_memory(self, authority):
        change, dispatch = authority.change, authority.dispatch
        self._session_change_registered(change)
        if (
            self._closing
            or self._closed
            or self._poisoned
            or self._session_changes.get(change.session_id) is not change
            or change.reason != "deleted"
            or change.state != "pending"
            or not change.fence_committed
            or change.running
            or change.original != authority.session
            or change.dispatch is not dispatch
            or self._session_change_jobs.get(dispatch.marker) != (change, dispatch)
            or dispatch.unknown
            or not dispatch.setup_complete
            or not self._actual_task(dispatch.task, dispatch.loop, dispatch.coroutine)
            or dispatch.entered_task is not dispatch.task
            or dispatch.task.done()
            or not self._session_store_worker_joined(dispatch)
            or dispatch.worker_result != ("error", "conflict")
        ):
            refuse("conflict")

    def _session_cleanup_guard(self, source, checkpoint, authority):
        with self._boundary() as token:
            self._authority()
            with self._mutex:
                self._session_cleanup_memory(authority)
            current = self._session_change_read(authority.change.session_id)
            expected = self._session_change_identity(authority.session)
            if current is None or self._session_change_identity(current) != expected:
                refuse("stale")
            self._session_change_fences(authority.change)
            receipt = self._lookup_ref(token, authority.reference, "source")
            row = self._sql(
                "SELECT * FROM media_sources WHERE source_id=?", (receipt.body.source_id,)
            ).fetchone()
            if (
                row is None
                or row["admission_receipt_id"] != receipt.id
                or row["owner_realm_id"] != self.owner_realm_id
                or (row["session_id"], row["thread_id"], row["epoch"]) != expected
                or row["desired"] == "running"
                or m.media_json(bytes(row["current_fence"]), m.MediaFence) != authority.fence
                or source.reference != authority.reference
            ):
                refuse("conflict")
            with self._mutex:
                self._session_cleanup_memory(authority)
                if source.current_fence != authority.fence or not self._cleanup_checkpoint_matches(
                    source, checkpoint
                ):
                    refuse("conflict")
            self._authority()

    def _cleanup_authority_guard(self, source, checkpoint, authority):
        if type(authority) is _MediaCleanupAuthority:
            self._cleanup_reconcile_guard(
                authority.request,
                authority.principal,
                authority.original,
                source,
                checkpoint,
                authority.fence,
            )
        elif type(authority) is _SessionCleanupAuthority:
            self._session_cleanup_guard(source, checkpoint, authority)
        else:
            refuse()

    def _cleanup_authority_unknown(self, authority, error=None):
        if type(authority) is _MediaCleanupAuthority:
            authority.original.unknown = True
        elif type(authority) is _SessionCleanupAuthority:
            self._session_dispatch_unknown(authority.change, authority.dispatch, error)
        else:
            self._poisoned = True

    def _reader_probe_sync(self, source, probe):
        try:
            self._cleanup_authority_guard(source, probe.checkpoint, probe.authority)
        except Exception as error:
            if self._poisoned:
                raise
            # Only a positively rolled-back guard before close can be a known
            # no-effect refusal. An exception's code never proves capacity.
            return "refused", error.code if isinstance(error, MediaSessionError) else "unavailable"
        closed = source.upload.close()
        if type(closed) is not bool:
            refuse()
        if closed:
            self._cleanup_authority_guard(source, probe.checkpoint, probe.authority)
        return "returned", closed

    async def _reader_probe_worker(self, source, probe):
        await probe.assigned.wait()
        current = asyncio.current_task()
        if (
            current is not probe.task
            or not self._actual_task(probe.task, probe.loop, probe.coroutine)
            or not probe.setup_complete
            or probe.unknown
            or source.cleanup_probe is not probe
        ):
            probe.unknown = True
            self._cleanup_authority_unknown(probe.authority)
            refuse()
        probe.entered_task = current
        try:
            probe.outcome = await asyncio.to_thread(self._reader_probe_sync, source, probe)
            probe.completed_task = current
            return probe.outcome
        except BaseException as error:
            probe.unknown = True
            self._cleanup_authority_unknown(probe.authority, error)
            raise

    def _reader_probe_done(self, source, probe, task):
        with self._mutex:
            if not probe.setup_complete:
                return
            if (
                task is not probe.task
                or not self._actual_task(task, probe.loop, probe.coroutine)
                or probe.entered_task is not task
                or probe.completed_task is not task
                or not task.done()
                or task.cancelled()
                or task.exception() is not None
                or task.result() != probe.outcome
                or probe.unknown
            ):
                probe.unknown = True
                self._cleanup_authority_unknown(probe.authority)
                return
            self._native_jobs.pop(probe.marker, None)
            probe.retired = True
            if source.delete_task is probe.placeholder:
                source.delete_task = probe.checkpoint.task
            if not probe.placeholder.done():
                probe.placeholder.set_result(False)  # A close probe is not settlement.

    async def _probe_reader_cleanup(self, source, checkpoint, authority):
        loop = asyncio.get_running_loop()
        probe = _CleanupProbe(checkpoint, authority, loop)
        probe.placeholder = loop.create_future()
        with self._mutex:
            if (
                source.current_fence != authority.fence
                or not self._cleanup_checkpoint_matches(source, checkpoint)
                or not self._cleanup_probe_joined(source)
                or self._loop is not None
                and self._loop is not loop
            ):
                return None  # No enrollment/effect; the existing claim stays pending.
            source.cleanup_probe = probe
            if len(self._native_jobs) >= 16:
                # This native branch precedes coroutine creation or OS work.
                probe.no_dispatch = probe.setup_complete = probe.retired = True
                return None
            self._loop = loop
            self._native_jobs[probe.marker] = None
            # Preserve this exact checkpoint while the explicit native probe
            # owns the slot; unchanged generic scheduling sees a pending owner.
            source.delete_task = probe.placeholder
        try:
            probe.coroutine = self._reader_probe_worker(source, probe)
            probe.task = asyncio.create_task(probe.coroutine)
            with self._mutex:
                self._native_jobs[probe.marker] = probe.task
            if not self._actual_task(probe.task, loop, probe.coroutine) or probe.task.done():
                refuse()
            probe.task.add_done_callback(lambda done: self._reader_probe_done(source, probe, done))
            probe.setup_complete = True
            probe.assigned.set()
        except BaseException as error:
            probe.unknown = True
            self._cleanup_authority_unknown(authority, error)
            raise
        # Observation never cancels the retained native worker. A cancelled
        # controller still leaves its probe/slot/Source claims reachable.
        await asyncio.wait({probe.task})
        self._reader_probe_done(source, probe, probe.task)
        if not self._cleanup_probe_joined(source):
            refuse()
        outcome, value = probe.outcome
        if outcome == "refused":
            return None
        if outcome != "returned" or type(value) is not bool:
            probe.unknown = True
            self._cleanup_authority_unknown(authority)
            refuse()
        if type(authority) is _MediaCleanupAuthority:
            authority.original.effect_result = value
        return value

    async def _session_reader_reconciliation(self, change, dispatch):
        # A synchronous Store.delete remains an attempt/check. This hook is
        # owned only by an explicit, retained async apply dispatch after its
        # SAME original Store worker returned a known pending-delete conflict.
        with self._mutex:
            sources = tuple(
                source
                for source in self._sources.values()
                if source.admission.session.session_id == change.session_id
            )
        for source in sources:
            with self._mutex:
                checkpoint = source.cleanup_checkpoint
                if not self._cleanup_checkpoint_matches(source, checkpoint):
                    continue
                if not self._cleanup_probe_joined(source):
                    if source.cleanup_probe.unknown:
                        self._cleanup_authority_unknown(source.cleanup_probe.authority)
                        refuse()
                    continue
                authority = _SessionCleanupAuthority(
                    change, dispatch, change.original, source.reference, source.current_fence
                )
                self._session_cleanup_memory(authority)
            closed = await self._probe_reader_cleanup(source, checkpoint, authority)
            if closed is True:
                self._resume_reader_cleanup(source, checkpoint, authority)

    async def _continue_cleanup(self, source, continuation):
        await continuation.assigned.wait()
        current = asyncio.current_task()
        if (
            current is not continuation.task
            or not self._actual_task(continuation.task, continuation.loop, continuation.coroutine)
            or continuation.unknown
            or source.cleanup_continuation is not continuation
        ):
            continuation.unknown = True
            self._cleanup_authority_unknown(continuation.authority)
            return False
        continuation.entered_task = current
        try:
            result = await self._cleanup_source(source, continuation)
            if continuation.unknown:
                self._cleanup_authority_unknown(continuation.authority)
            continuation.completed_task = current
            return result
        except BaseException as error:
            continuation.unknown = True
            self._cleanup_authority_unknown(continuation.authority, error)
            raise

    def _resume_reader_cleanup(self, source, checkpoint, authority):
        if type(authority) is _MediaCleanupAuthority:
            self._principal_memory(authority.principal, {"owner"})
        continuation = _CleanupContinuation(checkpoint, authority, asyncio.get_running_loop())
        continuation.placeholder = continuation.loop.create_future()
        with self._mutex:
            if type(authority) is _SessionCleanupAuthority:
                try:
                    self._session_cleanup_memory(authority)
                except MediaSessionError:
                    if self._poisoned:
                        raise
                    return False
            probe = source.cleanup_probe
            if (
                source.current_fence != authority.fence
                or not self._cleanup_checkpoint_matches(source, checkpoint)
                or not self._cleanup_probe_joined(source)
                or probe is None
                or probe.authority is not authority
                or probe.checkpoint is not checkpoint
                or probe.outcome != ("returned", True)
            ):
                return False
            source.cleanup_continuation = continuation
            # The original placeholder prevents generic scheduling during
            # setup. A lost/foreign facade never opens the physical-work gate.
            source.delete_task = continuation.placeholder
        try:
            continuation.coroutine = self._continue_cleanup(source, continuation)
            continuation.task = asyncio.create_task(continuation.coroutine)
            with self._mutex:
                source.delete_task = continuation.task
            if (
                not self._actual_task(continuation.task, continuation.loop, continuation.coroutine)
                or continuation.task.done()
            ):
                refuse()

            def observed(done):
                if done is not continuation.task or not self._cleanup_continuation_joined(source):
                    continuation.unknown = True
                    self._cleanup_authority_unknown(authority)
                    positive = False
                else:
                    positive = done.result() is True
                if not continuation.placeholder.done():
                    continuation.placeholder.set_result(positive)
                if positive and not continuation.unknown:
                    with self._mutex:
                        for key, item in tuple(self._retirement_requests.items()):
                            if item.source_id == source.admission.source_id:
                                self._retirement_requests.pop(key, None)

            continuation.task.add_done_callback(observed)
        except BaseException as error:
            continuation.unknown = True
            source.cleanup_checkpoint = None
            self._cleanup_authority_unknown(authority, error)
            raise
        continuation.assigned.set()
        return True

    async def _cleanup_source(self, source, continuation=None):
        source.fenced.set()
        source.cleanup_checkpoint = None
        try:
            if (
                not self._cleanup_probe_joined(source)
                or source.cleanup_continuation is not None
                and source.cleanup_continuation.unknown
            ):
                return False
            connection = source.connection
            if connection is not None:
                if connection.unknown:
                    if continuation is not None:
                        continuation.unknown = True
                    return False
                retirement = connection.retirement_task
                if retirement is None:
                    # The hub enrolls its one original before awaiting it.
                    retired = await self.event_hub.retire(connection.instance)
                    retirement = connection.retirement_task
                else:
                    if (
                        type(retirement) is not asyncio.Task
                        or retirement.get_loop() is not asyncio.get_running_loop()
                    ):
                        return False
                    retired = await asyncio.shield(retirement)
                # Physical joins precede the original native publication. A
                # joined flag cannot replace that same retirement Task's result.
                if (
                    retired is not True
                    or source.connection is not connection
                    or connection.retirement_task is not retirement
                    or type(retirement) is not asyncio.Task
                    or retirement.get_loop() is not asyncio.get_running_loop()
                    or not retirement.done()
                    or retirement.cancelled()
                    or retirement.exception() is not None
                    or retirement.result() is not True
                    or not connection.joined
                    or connection.unknown
                ):
                    return False
            originals = [
                item.task
                for item in tuple(self._operations.values())
                if item.source_id == source.admission.source_id
                and item.task is not None
                and item.task is not asyncio.current_task()
            ]
            if originals:
                await asyncio.gather(
                    *(asyncio.shield(item) for item in originals), return_exceptions=True
                )
            if any(
                item["source_id"] == source.admission.source_id for item in self._intakes.values()
            ):
                return False
            # No decoder entry point is installed in this increment. The SAME
            # registered promised native task is fenced before any dispatch;
            # there is no queued/late spawn or source-child FD to reconstruct.
            if any(
                item.unknown
                for item in self._operations.values()
                if item.source_id == source.admission.source_id
            ):
                if continuation is not None:
                    continuation.unknown = True
                return False
            closed = await self.run_native(source.upload.close)
            if closed is not True:
                if continuation is not None:
                    continuation.unknown = True
                source.cleanup_checkpoint = _CleanupCheckpoint(
                    asyncio.current_task(), source.upload, source.connection
                )
                return False

            def finish():
                with self._boundary() as token:
                    if (
                        not self._cleanup_probe_joined(source)
                        or source.cleanup_continuation is not None
                        and source.cleanup_continuation.unknown
                    ):
                        return False
                    pending = self._sql(
                        "SELECT 1 FROM media_dependencies WHERE source_id=? AND kind NOT IN('upload','decoder') AND (state!='settled' OR publication_state!='settled') LIMIT 1",
                        (source.admission.source_id,),
                    ).fetchone()
                    if pending:
                        return False
                    self._store_cleanup(token, source, "decoder", "not_started")
                    self._store_cleanup(token, source, "upload", "cancelled")
                    self._sql(
                        "UPDATE media_reservations SET child_slots=0 WHERE source_id=?",
                        (source.admission.source_id,),
                    )
                    self._sql(
                        "UPDATE media_sources SET local_actual='settled' WHERE source_id=? AND logical_deleted=0",
                        (source.admission.source_id,),
                    )
                    return True

            settled = await self.run_native(finish)
            if settled is not True:
                return False
            if (
                source.admission.raw_retain_until_ms is None
                or self._raw_expired(source)
                or source.reason == "deleted"
            ):
                if await self.run_native(source.upload.delete) is not True:
                    if continuation is not None:
                        continuation.unknown = True
                    return False

                def deleted():
                    with self._boundary():
                        if (
                            not self._cleanup_probe_joined(source)
                            or source.cleanup_continuation is not None
                            and source.cleanup_continuation.unknown
                        ):
                            return False
                        pending = self._sql(
                            "SELECT 1 FROM media_dependencies WHERE source_id=? AND (state!='settled' OR publication_state!='settled') LIMIT 1",
                            (source.admission.source_id,),
                        ).fetchone()
                        if pending is not None:
                            return False
                        self._sql(
                            "UPDATE media_sources SET logical_deleted=1,desired='deleted',local_actual='deleted',raw_state='deleted' WHERE source_id=?",
                            (source.admission.source_id,),
                        )
                        self._sql(
                            "UPDATE media_reservations SET state='released',child_slots=0 WHERE source_id=?",
                            (source.admission.source_id,),
                        )
                        return True

                positive = await self.run_native(deleted)
                if positive is True:
                    with self._mutex:
                        if source.cleanup_continuation is not None:
                            source.cleanup_continuation.completion_task = asyncio.current_task()
                        retired = [
                            key
                            for key, item in self._sources.items()
                            if item.upload.deleted
                            and self._cleanup_probe_joined(item)
                            and (
                                item.cleanup_continuation is None
                                or self._cleanup_continuation_joined(item)
                                and self._close_result(item.cleanup_continuation.completion_task)
                            )
                        ]
                        for key in retired[:-64]:
                            self._sources.pop(key, None)
                return positive
            return True
        except BaseException as error:
            if continuation is not None:
                continuation.unknown = True
            if not isinstance(error, Exception):
                raise
            return False

    async def delete(self, request, principal):
        request = _fresh(request, m.MediaDeleteRequest)
        self._fence_reference_memory(request.source, request.fence, "deleted", principal)

        def prepare(original):
            with self._transaction(principal, {"owner"}) as token:
                operation, fresh = self._prepare(token, request, "delete")
                if not fresh:
                    return self._existing(operation), None, None
                row, source = self._source_guard(
                    token, request.source, request.fence, principal, revocation=True
                )
                self._sql(
                    "UPDATE media_operations SET source_id=?,client_instance=? WHERE key=?",
                    (row["source_id"], row["client_instance"], request.key),
                )
                operation = self._sql(
                    "SELECT * FROM media_operations WHERE key=?", (request.key,)
                ).fetchone()
                self._capture_operation(original, operation)
                self._fence_source_locked(token, row["source_id"], "deleted")
                self._sql(
                    "UPDATE media_sources SET logical_deleted=1,desired='deleted',raw_state='delete_pending',local_actual='closing' WHERE source_id=?",
                    (row["source_id"],),
                )
                fence = m.media_json(
                    bytes(
                        self._sql(
                            "SELECT current_fence FROM media_sources WHERE source_id=?",
                            (row["source_id"],),
                        ).fetchone()[0]
                    ),
                    m.MediaFence,
                )
                return operation, source, fence

        async def work(value, original):
            operation, source, fence = await self.run_native(prepare, original)
            if source is None and hasattr(operation, "body"):
                return operation
            # A delete result is a snapshot. Never block on unknown physical
            # claims, and never derive success from an absent SQL child row.
            positive = False
            if source is not None:
                prior = source.delete_task
                if prior is not None and prior.done():
                    positive = prior.result() is True and self._cleanup_continuation_joined(source)
                    checkpoint = source.cleanup_checkpoint
                    with self._mutex:
                        reconcile = self._cleanup_checkpoint_matches(source, checkpoint)
                    if not positive and reconcile:
                        authority = _MediaCleanupAuthority(value, principal, original, fence)
                        closed = await self._probe_reader_cleanup(source, checkpoint, authority)
                        if closed is True:
                            self._resume_reader_cleanup(source, checkpoint, authority)
                elif prior is None:
                    # Observe cleanup separately; this receipt stays pending.
                    self._schedule_cleanup(source)
            if positive:
                original.unknown = True
                positive = await self.run_native(source.upload.delete)
                original.effect_result = positive
                original.unknown = positive is not True
            result = await self.run_native(self._delete_finish, value, original, positive)
            return result

        return await self._execute(request, "delete", work, principal)

    def _delete_finish(self, request, original, positive):
        with self._boundary() as token:
            operation = self._sql(
                "SELECT * FROM media_operations WHERE key=?", (original.key,)
            ).fetchone()
            if operation is None or operation["original_operation_id"] != original.original_id:
                refuse()
            source_receipt = self._lookup_ref(token, request.source, "source")
            source_id = source_receipt.body.source_id
            dependencies = self._sql(
                "SELECT 1 FROM media_dependencies WHERE source_id=? AND (state!='settled' OR publication_state!='settled') LIMIT 1",
                (source_id,),
            ).fetchone()
            source = self._sources.get(source_id)
            released = (
                positive is True
                and original.effect_result is True
                and dependencies is None
                and source is not None
                and self._cleanup_probe_joined(source)
                and self._cleanup_continuation_joined(source)
            )
            if released:
                self._sql(
                    "UPDATE media_sources SET local_actual='deleted',raw_state='deleted' WHERE source_id=?",
                    (source_id,),
                )
                self._sql(
                    "UPDATE media_reservations SET state='released',child_slots=0 WHERE source_id=?",
                    (source_id,),
                )
            body = m.MediaDeleteReceipt(
                v=1,
                source=request.source,
                fence=m.media_json(
                    bytes(
                        self._sql(
                            "SELECT current_fence FROM media_sources WHERE source_id=?",
                            (source_id,),
                        ).fetchone()[0]
                    ),
                    m.MediaFence,
                ),
                state="local_deleted" if released else "local_delete_pending",
                local_children_settled=dependencies is None,
                local_spool_unlinked=released,
                local_processing_reservation_released=released,
                remote_state="none",
                remote_liability_head=None,
            )
            return self._complete(
                token, operation, body, "delete", source_receipt.body.session, "owner"
            )

    def lookup(self, request, principal):
        request = _fresh(request, m.MediaLookup)
        if request.kind not in _KINDS:
            refuse("unsupported")
        with self._transaction(principal, {"owner", "converse"}) as token:
            if request.ref.backend_id != self.backend_id:
                refuse("unknown_ref")
            row = self._sql(
                "SELECT * FROM media_receipts WHERE receipt_id=?", (request.ref.receipt_id,)
            ).fetchone()
            if row is None:
                return m.MediaFound[getattr(m, "Media" + _KINDS[request.kind])](
                    v=1, found=False, value=None
                )
            receipt = self._lookup_ref(token, request.ref, request.kind)
            self._lookup_scope(token, receipt, request.kind, principal)
            return m.MediaFound[type(receipt.body)](v=1, found=True, value=receipt)

    def _lookup_scope(self, token, receipt, kind, principal):
        self._locked(token)
        row = self._sql("SELECT * FROM media_receipts WHERE receipt_id=?", (receipt.id,)).fetchone()
        if row is None or row["owner_realm_id"] != self.owner_realm_id:
            refuse("denied")
        if principal.role == "converse":
            if kind == "source":
                source_id = receipt.body.source_id
            elif hasattr(receipt.body, "source"):
                source_id = self._lookup_ref(token, receipt.body.source, "source").body.source_id
            else:
                refuse("denied")
            self._capability(principal, self._client(source_id))

    def lookup_key(self, request, principal):
        request = _fresh(request, m.MediaKeyLookup)
        if request.kind not in _KINDS:
            refuse("unsupported")
        with self._transaction(principal, {"owner", "converse"}) as token:
            row = self._sql("SELECT * FROM media_receipts WHERE key=?", (request.key,)).fetchone()
            kind = getattr(m, "Media" + _KINDS[request.kind])
            if row is None:
                return m.MediaFound[kind](v=1, found=False, value=None)
            if row["kind"] != request.kind or row["request_sha256"] != request.request_sha256:
                refuse("conflict")
            receipt = self._read_receipt(row)
            self._lookup_scope(token, receipt, request.kind, principal)
            return m.MediaFound[kind](v=1, found=True, value=receipt)

    def fence_session_locked(self, session, reason):
        # Root's Store hook owns the SAME actual SQLite transaction. This method
        # does not begin/commit it, wait for a worker, or invoke spool/OS work.
        if not self.store.db.in_transaction or not self.store.lock._is_owned():
            refuse()
        self._authority()
        if self._closing:  # Authority reads may have overlapped the close fence.
            refuse()
        with self._mutex:
            if self._closing:
                refuse()
            sources = tuple(self._sources.values())
            candidates = tuple(self._operations.values())
            originals = []
            for source in sources:
                if source.admission.session.session_id == session.session_id:
                    source.fenced.set()
                    source.reason = reason
                    if source.original is not None:
                        originals.append(source.original)
            for original in originals:
                original.fenced.set()
            # Request-derived Session identity exists before any SQL prepare
            # or Source publication, including the seal original.
            for item in candidates:
                if item.session_id == session.session_id:
                    if not any(item is old for old in originals):
                        originals.append(item)
                    item.fenced.set()
        # The external Store owns this transaction; grant only the exact private
        # token while running the same native fence helper, with no OS work.
        previous = getattr(self._local, "token", None)
        token = object()
        self._local.token = token
        try:
            self._audit(token)
            for row in self._sql(
                "SELECT source_id FROM media_sources WHERE session_id=?", (session.session_id,)
            ).fetchall():
                self._fence_source_locked(token, row["source_id"], reason)
        finally:
            self._local.token = previous
        self._sql(
            "UPDATE media_clients SET connection_state='fenced' WHERE session_id=? AND connection_state NOT IN('unknown','settled')",
            (session.session_id,),
        )
        previous = getattr(self._local, "token", None)
        token = object()
        self._local.token = token
        try:
            self._audit(token)
        finally:
            self._local.token = previous
        return tuple(originals)

    def request_session_retirements(self, operations):
        if type(operations) is not tuple or self.store.lock._is_owned():
            refuse()
        with self._mutex:
            for original in operations:
                if type(original) is not OriginalOperation:
                    refuse()
                source = self._sources.get(original.source_id)
                if self._operations.get(original.key) is not original and (
                    source is None or source.original is not original
                ):
                    refuse()
                original.fenced.set()
                self._retirement_requests[id(original)] = original
            loop = self._loop
            if loop is None or not loop.is_running():
                self._retirement_unknown = bool(operations)
                return
            try:
                loop.call_soon_threadsafe(self._dispatch_retirements)
            except BaseException as error:
                self._retirement_unknown = True
                if not isinstance(error, Exception):
                    raise

    def _retirement_cleanup_task(self, source):
        # Session retirement keeps the exact positively joined reader cleanup;
        # fencing an already deleted source does not start its physical work again.
        with self._mutex:
            continuation = source.cleanup_continuation
            if (
                continuation is None
                or source.delete_task is not continuation.task
                or continuation.completion_task is not continuation.task
                or source.upload is not continuation.checkpoint.upload
                or source.connection is not continuation.checkpoint.connection
                or not self._cleanup_probe_joined(source)
                or not self._cleanup_continuation_joined(source)
                or not self._close_result(continuation.task)
                or not source.upload.deleted
                or source.connection is not None
                and (source.connection.unknown or not source.connection.joined)
                or any(
                    item.source_id == source.admission.source_id
                    and (item.unknown or item.task is None or not item.task.done())
                    for item in self._operations.values()
                )
                or any(
                    item["source_id"] == source.admission.source_id
                    for item in self._intakes.values()
                )
            ):
                return None
            return continuation.task

    def _dispatch_retirements(self):
        for original in tuple(self._retirement_requests.values()):
            source = self._sources.get(original.source_id)
            if source is not None:
                source.fenced.set()
                task = self._retirement_cleanup_task(source)
                if task is None:
                    task = self._schedule_cleanup(source)

                def observed(done, operation=original):
                    if not done.cancelled() and done.exception() is None and done.result() is True:
                        with self._mutex:
                            self._retirement_requests.pop(id(operation), None)

                task.add_done_callback(observed)
            else:
                self._retirement_unknown = True

    def require_delete_settled_locked(self, session):
        if not self.store.db.in_transaction or not self.store.lock._is_owned():
            refuse()
        self._authority()
        previous = getattr(self._local, "token", None)
        token = object()
        self._local.token = token
        try:
            self._audit(token)
        finally:
            self._local.token = previous
        # Read-only final assertion: root already committed its explicit fence.
        with self._mutex:
            if any(
                item.session_id == session.session_id
                and (item.unknown or item.task is None or not item.task.done())
                for item in self._operations.values()
            ):
                refuse("conflict")
            for source in self._sources.values():
                if source.admission.session.session_id != session.session_id:
                    continue
                if (
                    not source.upload.deleted
                    or not self._cleanup_probe_joined(source)
                    or not self._cleanup_continuation_joined(source)
                    or source.delete_task is None
                    or not source.delete_task.done()
                    or source.delete_task.cancelled()
                    or source.delete_task.exception() is not None
                    or source.delete_task.result() is not True
                    or source.connection is not None
                    and (source.connection.unknown or not source.connection.joined)
                ):
                    refuse("conflict")
                if any(
                    item["source_id"] == source.admission.source_id
                    for item in self._intakes.values()
                ):
                    refuse("conflict")
                if any(
                    item.source_id == source.admission.source_id
                    and (item.unknown or item.task is None or not item.task.done())
                    for item in self._operations.values()
                ):
                    refuse("conflict")
        # Include preparations which have not yet published SourceAdmission.
        if self._sql(
            "SELECT 1 FROM media_clients c LEFT JOIN media_sources s ON s.source_id=c.source_id LEFT JOIN media_reservations r ON r.source_id=c.source_id WHERE c.session_id=? AND (s.source_id IS NULL OR r.reservation_id IS NULL OR s.raw_state!='deleted' OR r.state!='released') LIMIT 1",
            (session.session_id,),
        ).fetchone():
            refuse("conflict")
        if self._sql(
            "SELECT 1 FROM media_sources s JOIN media_reservations r ON r.source_id=s.source_id WHERE s.session_id=? AND (s.raw_state!='deleted' OR r.state!='released' OR EXISTS(SELECT 1 FROM media_dependencies d WHERE d.source_id=s.source_id AND (d.state!='settled' OR d.publication_state!='settled'))) LIMIT 1",
            (session.session_id,),
        ).fetchone():
            refuse("conflict")

    def store_close_confirmed(self) -> bool:
        # Cached native task outcome only; no SQL/OS or caller proof flag.
        operation = self._close_operation
        return (
            self._closed is True
            and operation is not None
            and not operation.unknown
            and self._close_result(operation.task)
        )

    @staticmethod
    def _close_result(task):
        if task is None or not task.done() or task.cancelled():
            return False
        try:
            return task.result() is True
        except BaseException:
            return False

    def _close_phase(self, operation, name, factory):
        if name in operation.phases:
            return operation.phases[name]
        if operation.unknown or operation.loop.time() >= operation.deadline:
            return None
        operation.phases[name] = None  # Enrollment precedes eager task creation.
        try:
            task = asyncio.create_task(factory())
        except BaseException:
            operation.unknown = True
            raise
        operation.phases[name] = task
        operation.retained.add(task)
        return task

    async def _close_wait(self, operation, tasks):
        tasks = set(tasks)
        operation.retained.update(tasks)
        if operation.unknown or operation.loop.time() >= operation.deadline:
            return False
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=max(0.0, operation.deadline - operation.loop.time())
            )
            if pending:
                return False
        if operation.loop.time() >= operation.deadline:
            return False
        for task in tasks:
            if task.cancelled():
                operation.unknown = True
                return False
            error = task.exception()
            if error is not None and not isinstance(error, Exception):
                operation.unknown = True
                return False
        return True

    def _close_capture(self, operation):
        with self._mutex:
            for source_id, source in self._sources.items():
                known = operation.sources.get(source_id)
                if known is not None and known is not source:
                    operation.unknown = True
                operation.sources[source_id] = source
                if source.original is not None:
                    operation.originals[id(source.original)] = source.original
            for original in self._operations.values():
                operation.originals[id(original)] = original
            return tuple(operation.sources.values()), tuple(operation.originals.values())

    def _close_barrier(self, operation):
        # No await between this complete original-owner check and sealing the
        # public job gate. Removals are permitted only because the retained
        # original objects/tasks below still have their positive proof.
        with self._mutex:
            if self._intakes or self._native_jobs or operation.unknown:
                return False
            if any(
                change.running
                or change.state == "unknown"
                or not self._session_dispatch_joined(change.dispatch)
                for change in (
                    *self._session_changes.values(),
                    *self._session_change_recent.values(),
                )
            ):
                return False
            if any(
                operation.sources.get(key) is not source for key, source in self._sources.items()
            ):
                return False
            if any(
                operation.originals.get(id(original)) is not original
                for original in self._operations.values()
            ):
                return False
            for original in operation.originals.values():
                if (
                    original.unknown
                    or original.task is None
                    or not original.task.done()
                    or original.task.cancelled()
                ):
                    return False
                original.task.exception()  # A joined refusal is not a live OS owner.
            for source in operation.sources.values():
                if (
                    not source.fenced.is_set()
                    or not self._cleanup_probe_joined(source)
                    or not self._cleanup_continuation_joined(source)
                    or not self._close_result(source.delete_task)
                ):
                    return False
                if source.expiry_task is not None and (
                    not source.expiry_task.done()
                    or source.expiry_task.cancelled()
                    or source.expiry_task.exception() is not None
                ):
                    return False
            return self._close_result(operation.phases.get("hub"))

    async def _close_native_phase(self, operation, name):
        if (
            operation is not self._close_operation
            or not operation.sealed
            or operation.unknown
            or name not in {"spool", "key"}
            or operation.loop is not asyncio.get_running_loop()
            or operation.loop.time() >= operation.deadline
        ):
            return False
        function = self.spool.close if name == "spool" else self.keys.close
        return await self._run_native_original(function)

    async def _close_original(self, operation):
        try:
            hub = self._close_phase(operation, "hub", self.event_hub.close)
            if hub is None:
                return False
            sources, originals = self._close_capture(operation)
            for source in sources:
                self._schedule_cleanup(source)
            if any(original.task is None for original in originals):
                operation.unknown = True
                return False
            if not await self._close_wait(operation, {hub, *(item.task for item in originals)}):
                return False
            if not self._close_result(hub):
                return False

            # An admitted reserve can publish its exact Source while the first
            # wait is pending. Capture those late originals before physical
            # spool/key closure, and join their own cleanup/publication owners.
            sources, originals = self._close_capture(operation)
            if any(original.task is None for original in originals):
                operation.unknown = True
                return False
            cleanups = {self._schedule_cleanup(source) for source in sources}
            observers = {source.expiry_task for source in sources if source.expiry_task is not None}
            if not await self._close_wait(
                operation, {*cleanups, *observers, *(item.task for item in originals)}
            ):
                return False
            if (
                any(original.unknown for original in originals)
                or any(not self._close_result(task) for task in cleanups)
                or any(task.exception() is not None for task in observers)
            ):
                return False

            # Native workers are retained independently of their observers.
            # A finished worker may still have a scheduled wrapper-finally that
            # removes its exact registry entry; yield under the same deadline.
            while True:
                with self._mutex:
                    jobs = tuple(self._native_jobs.values())
                if not jobs:
                    break
                if not await self._close_wait(
                    operation, {task for task in jobs if task is not None}
                ):
                    return False
                await asyncio.sleep(0)

            with self._mutex:
                if (
                    not self._close_barrier(operation)
                    or operation.loop.time() >= operation.deadline
                ):
                    return False
                operation.sealed = True

            spool = self._close_phase(
                operation, "spool", lambda: self._close_native_phase(operation, "spool")
            )
            if spool is None or not await self._close_wait(operation, {spool}):
                return False
            if not self._close_result(spool) or not self._close_barrier(operation):
                return False
            key = self._close_phase(
                operation, "key", lambda: self._close_native_phase(operation, "key")
            )
            if key is None or not await self._close_wait(operation, {key}):
                return False
            with self._mutex:
                if (
                    not self._close_result(key)
                    or not self._close_barrier(operation)
                    or operation.loop.time() >= operation.deadline
                ):
                    return False
                self._closed = True
            return True
        except BaseException as error:
            operation.unknown = True
            if not isinstance(error, Exception):
                raise
            return False

    async def close(self):
        # Charge even the synchronous fence work to the original observation
        # window. Every native fence still precedes this method's first await.
        loop = asyncio.get_running_loop()
        began = loop.time()
        self._closing = True
        with self._mutex:
            operation = self._close_operation
            fresh = operation is None
            if fresh:
                operation = _CloseOperation(loop, began + 5.0)
                self._close_operation = operation
            self._fence_all_memory("disconnect")
            if operation.loop is not loop:
                return False
            if fresh and self._loop is not None and self._loop is not loop:
                operation.unknown = True
                return False
        if fresh:
            # The original record already exists. An eager factory may run
            # arbitrary coroutine phases before returning: never hold the
            # memory mutex across that native scheduling/physical-work chain.
            task = None
            try:
                task = asyncio.create_task(self._close_original(operation))
            except BaseException as error:
                operation.setup_error = error
                operation.unknown = True
                raise
            finally:
                with self._mutex:
                    if task is not None:
                        operation.task = task
                        operation.retained.add(task)
        # A concurrent observer during setup sees this same pending original;
        # it does not create another runner or another deadline.
        if operation.task is None:
            return False
        if operation.task.done():
            return not operation.unknown and self._close_result(operation.task)
        # asyncio.wait cancels neither the retained close task nor its physical
        # phase futures when this observing caller is cancelled or times out.
        remaining = operation.deadline - loop.time()
        if remaining <= 0:
            return False
        _, pending = await asyncio.wait({operation.task}, timeout=remaining)
        return not pending and not operation.unknown and self._close_result(operation.task)

    def admission_capability(self, receipt, principal, bootstrap_nonce):
        with self._transaction(principal, {"owner"}) as token:
            receipt = self._lookup_ref(token, _ref(self.backend_id, receipt), "source")
            client = self._client(receipt.body.source_id)
            if client["nonce_sha256"] != hashlib.sha256(bootstrap_nonce).hexdigest():
                refuse("conflict")
            if (
                client["auth_generation"] != self._auth_generation
                or client["app_instance"] != self.app_instance
            ):
                return None
            try:
                grant = self._grant(client)
                # The exact original bootstrap nonce authenticates recovery.
                # Reuse every source-currentness check with the native-derived
                # grant; recovery cannot skip anchor, retention or fence guards.
                recovery = NativeMediaPrincipal(
                    principal.issuer,
                    principal.auth_identity,
                    principal.auth_generation,
                    principal.app_instance,
                    principal.role,
                    grant,
                )
                self._source_guard(
                    token,
                    _ref(self.backend_id, receipt),
                    receipt.body.fence,
                    recovery,
                    connected=False,
                )
            except MediaSessionError:
                return None
            if client["connection_state"] not in {"unbound", "connected"}:
                return None
            return grant

    def associate_connection(self, connection, session_id):
        principal = connection.principal
        with self._transaction(principal, {"owner"}) as token:
            receipt = self._lookup_ref(token, connection.source, "source")
            if session_id != receipt.body.session.session_id:
                refuse("denied")
            client = self._client(receipt.body.source_id)
            self._capability(principal, client)
            source = self._memory_source(receipt.body.source_id, connected=False)
            self._session(receipt.body.session)
            self._anchor_guard(
                token, source.admission.anchor, source.admission.session, source.admission.scope
            )
            if client["connection_state"] != "unbound" or source.connection is not None:
                refuse("conflict")
            if (
                self._sql(
                    "SELECT COUNT(*) FROM media_dependencies WHERE state!='settled'"
                ).fetchone()[0]
                >= 16
            ):
                refuse("capacity")
            with self._mutex:
                if self._metadata_hold + self._intake_charge > _METADATA:
                    refuse("capacity")
                self._metadata_hold += self._intake_charge
            self._sql(
                "INSERT INTO media_dependencies VALUES(?,?,?,?,?,?,'active',NULL,'pending')",
                (
                    connection.instance,
                    source.admission.source_id,
                    source.original_operation_id,
                    "owner_channel",
                    connection.instance,
                    source.children["upload"].id,
                ),
            )
            self._sql(
                "UPDATE media_reservations SET metadata_bytes=metadata_bytes+? WHERE source_id=?",
                (self._intake_charge, source.admission.source_id),
            )
            connection.source_id = receipt.body.source_id
            connection.client_instance = client["client_instance"]
            connection.session_id = session_id
            with self._mutex:
                source.connection = connection
            self._sql(
                "UPDATE media_clients SET connection_instance=?,connection_state='connected' WHERE source_id=?",
                (connection.instance, source.admission.source_id),
            )
            return {
                "v": 1,
                "kind": "media.client_associated",
                "source": json.loads(m.media_bytes(connection.source)),
                "client_instance": client["client_instance"],
                "connection_instance": connection.instance,
                "fence": json.loads(m.media_bytes(source.admission.fence)),
            }

    def connection_remaining(self, connection):
        source = self._memory_source(connection.source_id)
        if source.connection is not connection:
            refuse("stale")
        if self.auth.media_identity() is not connection.principal.auth_identity:
            self.fence_connection_memory(connection, "replacement")
            refuse("stale")
        return source.expires - self.clock()

    def fence_connection_memory(self, connection, reason):
        with self._mutex:
            source_id = connection.source_id
            source = self._sources.get(source_id) if source_id is not None else None
            if source is None or source.connection is not connection:
                return False
            if not source.fenced.is_set():
                source.reason = reason
            source.fenced.set()
            for operation in self._operations.values():
                if operation.source_id == source_id:
                    operation.fenced.set()
            return True

    def fence_connection_locked(self, connection, reason):
        with self._boundary() as token:
            row = self._sql(
                "SELECT connection_instance FROM media_clients WHERE source_id=?",
                (connection.source_id,),
            ).fetchone()
            if row is not None and row[0] == connection.instance:
                self._fence_source_locked(token, connection.source_id, reason)

    def retire_connection_locked(self, connection):
        if not connection.joined or connection.unknown:
            refuse()
        with self._boundary():
            self._sql(
                "UPDATE media_clients SET connection_state='settled' WHERE source_id=? AND connection_instance=? AND connection_state='fenced'",
                (connection.source_id, connection.instance),
            )
            self._sql(
                "UPDATE media_dependencies SET state='settled',publication_state='settled' WHERE dependency_id=? AND kind='owner_channel' AND original_instance=?",
                (connection.instance, connection.instance),
            )

    def _fence_all_memory(self, reason):
        with self._mutex:
            for source in self._sources.values():
                source.fenced.set()
                source.reason = reason
            for operation in self._operations.values():
                operation.fenced.set()

    def _fence_source_locked(self, token, source_id, reason):
        self._locked(token)
        with self._mutex:
            source = self._sources.get(source_id)
            if source is not None:
                source.fenced.set()
                source.reason = reason
        row = self._sql(
            "SELECT current_fence,desired FROM media_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is not None:
            if row["desired"] != "running":
                return
            fence = m.media_json(bytes(row[0]), m.MediaFence)
            if fence.lease_generation >= _MAX:
                refuse("bound")
            fence = m.MediaFence(
                session=fence.session,
                source_revision=fence.source_revision,
                generation=fence.generation,
                lease_generation=fence.lease_generation + 1,
            )
            if source is not None:
                source.current_fence = fence
            self._sql(
                "UPDATE media_sources SET desired='stopped',local_actual='fenced',current_fence=? WHERE source_id=? AND logical_deleted=0",
                (m.media_bytes(fence), source_id),
            )
            self._sql(
                "UPDATE media_clients SET connection_state='fenced',lease_generation=? WHERE source_id=? AND connection_state!='unknown'",
                (fence.lease_generation, source_id),
            )
