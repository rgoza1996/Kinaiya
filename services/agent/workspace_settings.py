"""Protected owner workspace selection and settled native application.

The private database is lifetime locked, including after logical shutdown while
real operations or uncertain cleanup remain owned. No metadata read selects a
root, creates a directory, executes a command, or qualifies a sandbox. The
runtime wires the actual operation inventory, transport retirement and a purely
synchronous prepared-context installer through ``bind``.
"""

from __future__ import annotations

import asyncio
import fcntl
import inspect
import json
import os
import re
import secrets
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path

from packages.contracts.workspace_settings import (
    MAX_PATH_BYTES,
    MAX_READONLY_PATHS,
    MAX_RELATIVE_PATH_BYTES,
    MAX_WORKSPACE_METADATA_BYTES,
    MAX_WORKSPACE_REVISION,
    WorkspaceChange,
    WorkspacePin,
    WorkspaceSelection,
)
from services.settings.provider_catalog import _storage

MAX_RECEIPTS = 10_000
MAX_RECORD_BYTES = 65_536
_COUNTS = (
    "tasks",
    "workers",
    "processes",
    "retirements",
    "submit_preflights",
    "submit_operations",
    "pending_submits",
    "ecosystem_operations",
    "cleanup_pending",
    "cleanup_failed",
)
_TABLES = {
    "workspace_state": "CREATE TABLE workspace_state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, backend_id TEXT NOT NULL, active_instance TEXT NOT NULL, settings TEXT NOT NULL, root_identity TEXT NOT NULL)",
    "workspace_operations": "CREATE TABLE workspace_operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, request TEXT NOT NULL, receipt TEXT NOT NULL)",
}


class WorkspaceConflict(ValueError):
    pass


class WorkspaceUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Native workspace authority or application is unavailable")


def _encode(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _digest(value):
    import hashlib

    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _decode(value):
    if type(value) is not str or len(value.encode("utf-8")) > MAX_RECORD_BYTES:
        raise WorkspaceUnavailable

    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                raise WorkspaceUnavailable
            result[key] = child
        return result

    def invalid(_):
        raise WorkspaceUnavailable

    try:
        value = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)
        stack, nodes = [(value, 0)], 0
        while stack:
            child, depth = stack.pop()
            nodes += 1
            if depth > 12 or nodes > 4096:
                raise WorkspaceUnavailable
            if type(child) is dict:
                stack.extend((item, depth + 1) for item in child.values())
            elif type(child) is list:
                stack.extend((item, depth + 1) for item in child)
            elif type(child) is float:
                raise WorkspaceUnavailable
            elif type(child) is str:
                child.encode("utf-8")
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise WorkspaceUnavailable from None


def _hex(value, width):
    if type(value) is not str or re.fullmatch(rf"[a-f0-9]{{{width}}}", value) is None:
        raise WorkspaceUnavailable
    return value


def _canonical(path):
    value = Path(os.path.abspath(path))
    WorkspaceSelection(root=str(value), readonly_paths=[])
    if str(path) != str(value) or value.resolve() != value:
        raise ValueError("Workspace paths must not use aliases")
    return value


@dataclass(frozen=True)
class WorkspaceContext:
    """Native prepared identity. The owner retains its root descriptor."""

    root: Path
    owner_readonly_roots: tuple[Path, ...]
    readonly_roots: tuple[Path, ...]
    protected_roots: tuple[Path, ...]
    pin: WorkspacePin
    settings_json: str
    root_fd: int = field(repr=False, compare=False)

    def settings(self):
        return WorkspaceSelection.model_validate(_decode(self.settings_json))

    def current(self):
        try:
            path = _canonical(self.root)
            current, opened = path.stat(follow_symlinks=False), os.fstat(self.root_fd)
            return (
                stat.S_ISDIR(current.st_mode)
                and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
                and _digest({"root": str(path), "device": current.st_dev, "inode": current.st_ino})
                == self.pin.root_identity
            )
        except (OSError, ValueError, RuntimeError):
            return False


class AgentWorkspaceSettings:
    def __init__(
        self,
        host_workspace,
        *,
        path=None,
        protected_roots=(),
        control_roots=(),
        evaluation_roots=(),
        credential_roots=(),
        host_readonly_roots=(),
    ):
        self.instance = secrets.token_hex(16)
        self.backend_id = None
        self._closed = self._failed = self._db_closed = False
        self._lock_fd = None
        self._transition = None
        self._retirement = None
        self._apply_pending = None
        self._cleanup_failed = False
        self._runtime = self._operations = self._retire = self._apply = None
        self._active = None
        self._active_error = "Native workspace owner is not bound"
        self._contexts = []
        self.path = None if path is None else _canonical(path)
        groups = {
            "readonly": host_readonly_roots,
            "protected": protected_roots,
            "control": (*control_roots, *((self.path.parent,) if self.path else ())),
            "evaluation": evaluation_roots,
            "credentials": credential_roots,
        }
        self.guards = {
            name: tuple(sorted({_canonical(root) for root in roots}))
            for name, roots in groups.items()
        }
        if (
            sum(map(len, self.guards.values())) > 256
            or len(_encode(self._guard_metadata()).encode("utf-8")) > 65_536
        ):
            raise ValueError("Workspace host guards exceed their native bound")
        host = WorkspaceSelection(root=str(host_workspace), readonly_paths=[])
        if self.path is None:
            self.db, created = sqlite3.connect(":memory:", timeout=0.5), True
        else:
            self.db, created = _storage(self.path, host.root, report_created=True)
        try:
            if self.path is not None:
                self._lock_fd = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                opened, actual = os.fstat(self._lock_fd), self.path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or opened.st_nlink != 1
                    or opened.st_mode & 0o077
                    or (opened.st_dev, opened.st_ino) != (actual.st_dev, actual.st_ino)
                ):
                    raise WorkspaceUnavailable
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db.execute("PRAGMA busy_timeout=500")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            if created:
                if self.db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is not None:
                    raise WorkspaceUnavailable
                context = self._prepare(host, 0)
                self._contexts.append(context)
                for sql in _TABLES.values():
                    self.db.execute(sql)
                self.db.execute(
                    "INSERT INTO workspace_state VALUES (1,0,?,?,?,?)",
                    (
                        secrets.token_hex(16),
                        self.instance,
                        _encode(host.model_dump()),
                        context.pin.root_identity,
                    ),
                )
                self.db.execute("PRAGMA user_version=1")
            self._schema()
            self.backend_id = self._state()[1]
            self._validate_history()
            self.db.execute(
                "UPDATE workspace_state SET active_instance=? WHERE id=1", (self.instance,)
            )
            self.db.commit()
        except BaseException:
            self._failed = True
            self.db.close()
            self._db_closed = True
            self._release_descriptors()
            raise

    def _guard_metadata(self):
        return {name: [str(path) for path in roots] for name, roots in self.guards.items()}

    def _ensure(self):
        if self._closed or self._failed or self._db_closed:
            raise WorkspaceUnavailable

    def _schema(self):
        self._ensure()
        rows = self.db.execute(
            "SELECT type,CASE WHEN length(CAST(name AS BLOB))<=128 THEN name END,"
            "CASE WHEN length(CAST(tbl_name AS BLOB))<=128 THEN tbl_name END,"
            "CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN sql END FROM sqlite_schema LIMIT 4"
        ).fetchall()
        expected = {("table", name) for name in _TABLES} | {
            ("index", "sqlite_autoindex_workspace_operations_1")
        }
        if (
            self.db.execute("PRAGMA user_version").fetchone() != (1,)
            or {(row[0], row[1]) for row in rows} != expected
            or len(rows) != 3
        ):
            raise WorkspaceUnavailable
        for kind, name, table, sql in rows:
            if kind == "index":
                if table != "workspace_operations" or sql is not None:
                    raise WorkspaceUnavailable
            elif (
                table != name
                or type(sql) is not str
                or len(sql.encode("utf-8")) > 4096
                or re.sub(r"\s+", "", sql).lower() != re.sub(r"\s+", "", _TABLES[name]).lower()
            ):
                raise WorkspaceUnavailable

    def _state(self):
        self._schema()
        rows = self.db.execute(
            "SELECT id,CASE WHEN typeof(revision)='integer' THEN revision END,"
            "CASE WHEN typeof(backend_id)='text' AND length(CAST(backend_id AS BLOB))=32 THEN backend_id END,"
            "CASE WHEN typeof(active_instance)='text' AND length(CAST(active_instance AS BLOB))=32 THEN active_instance END,"
            "CASE WHEN typeof(settings)='text' AND length(CAST(settings AS BLOB))<=65536 THEN settings END,"
            "CASE WHEN typeof(root_identity)='text' AND length(CAST(root_identity AS BLOB))=64 THEN root_identity END "
            "FROM workspace_state LIMIT 2"
        ).fetchall()
        if len(rows) != 1:
            raise WorkspaceUnavailable
        identity, revision, backend, instance, settings, root = rows[0]
        if (
            type(identity) is not int
            or identity != 1
            or type(revision) is not int
            or not 0 <= revision <= MAX_WORKSPACE_REVISION
        ):
            raise WorkspaceUnavailable
        _hex(backend, 32)
        _hex(instance, 32)
        _hex(root, 64)
        if self.backend_id is not None and backend != self.backend_id:
            raise WorkspaceUnavailable
        return (
            revision,
            backend,
            instance,
            WorkspaceSelection.model_validate(_decode(settings)),
            root,
        )

    def _prepare(self, selection, revision, expected_identity=None):
        selection = WorkspaceSelection.model_validate(selection.model_dump())
        root = _canonical(selection.root)
        if root in {Path("/"), Path("/tmp"), Path("/run"), Path("/proc"), Path.home()}:
            raise ValueError("Choose a dedicated native worker directory")
        hard = tuple(
            sorted(
                {
                    path
                    for name, roots in self.guards.items()
                    if name != "readonly"
                    for path in roots
                }
            )
        )
        if any(root == path or root in path.parents or path in root.parents for path in hard):
            raise ValueError("Workspace root overlaps immutable native control authority")
        owner = []
        for relative in selection.readonly_paths:
            path = _canonical(root / relative)
            if not path.is_dir():
                raise ValueError("Read-only workspace folders must already exist")
            owner.append(path)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened, current = os.fstat(fd), root.stat(follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise WorkspaceUnavailable
            root_identity = _digest(
                {"root": str(root), "device": opened.st_dev, "inode": opened.st_ino}
            )
            if expected_identity is not None and root_identity != expected_identity:
                raise ValueError("Saved workspace root identity is unavailable")
            return WorkspaceContext(
                root=root,
                owner_readonly_roots=tuple(owner),
                readonly_roots=tuple(sorted(set(owner) | set(self.guards["readonly"]))),
                protected_roots=hard,
                settings_json=_encode(selection.model_dump()),
                root_fd=fd,
                pin=WorkspacePin(
                    schema_version="1.0",
                    # Preserve native path-scoped permission restrictions.
                    # Concrete directory identity is separately mandatory.
                    workspace_id=_digest({"workspace": str(root)}),
                    root_identity=root_identity,
                    settings_revision=revision,
                    source="host" if revision == 0 else "owner_saved",
                    shell_backend="bubblewrap",
                    mode="direct",
                ),
            )
        except BaseException:
            os.close(fd)
            raise

    def initial_context(self):
        self._ensure()
        revision, _, _, settings, identity = self._state()
        for context in self._contexts:
            if context.pin.settings_revision == revision and context.current():
                return context
        context = self._prepare(settings, revision, identity)
        self._contexts.append(context)
        return context

    def saved_selection(self):
        """Stored declaration only; this does not grant an execution context."""
        return self._state()[3]

    def bind(self, runtime, *, operation_state, retire, apply, prepare=None):
        self._ensure()
        if (
            self._runtime is not None
            or not all(callable(fn) for fn in (operation_state, retire, apply))
            or inspect.iscoroutinefunction(apply)
        ):
            raise ValueError("Workspace owner callbacks are not a synchronous native installer")
        self._runtime, self._operations, self._retire, self._apply = (
            runtime,
            operation_state,
            retire,
            apply,
        )
        self._prepare_apply = prepare
        try:
            context = self.initial_context()
        except (OSError, ValueError, RuntimeError):
            # Keep owner repair available. Every fresh runtime admission must
            # call active_context(), which continues to refuse this declaration.
            self._active_error = "Saved workspace directory or read-only folders are unavailable"
            return False
        if not self._matches_runtime(context):
            self._active_error = "Native workspace owners do not match saved authority"
            return False
        self._active, self._active_error = context, None
        return True

    def _matches_runtime(self, context):
        return (
            context.current()
            and self._runtime is not None
            and self._runtime.policy.workspace == context.root
            and self._runtime.workspace == context.root
            and set(self._runtime.policy.readonly_roots) == set(context.readonly_roots)
            and set(context.protected_roots).issubset(self._runtime.policy.protected_roots)
        )

    def active_context(self):
        """Fresh operation admission; never substitute a merely saved root."""
        self._ensure()
        state = self._state()
        active = self._active
        if (
            self._transition is not None
            or self._cleanup_failed
            or (self._apply_pending is not None and not self._apply_pending.done())
            or self._active_error is not None
            or active is None
            or not self._matches_runtime(active)
            or active.pin.settings_revision != state[0]
            or active.pin.root_identity != state[4]
            or active.settings() != state[3]
        ):
            raise WorkspaceUnavailable
        return active

    def authority_stamp(self):
        """Current-write freshness only; never recurse through owner tokens."""
        state = self._state()
        active = self._active
        available = (
            state[2] == self.instance
            and self._transition is None
            and not self._cleanup_failed
            and (self._apply_pending is None or self._apply_pending.done())
            and self._active_error is None
            and active is not None
            and self._matches_runtime(active)
            and active.pin.settings_revision == state[0]
            and active.pin.root_identity == state[4]
            and active.settings() == state[3]
        )
        return {
            "backend_id": state[1],
            "instance_id": self.instance,
            "active_instance": state[2],
            "saved_revision": state[0],
            "saved_root_identity": state[4],
            "available": available,
            "active_pin": active.pin.model_dump(mode="json") if available else None,
        }

    def _operation_state(self):
        if self._operations is None:
            return None
        value = self._operations()
        if (
            type(value) is not dict
            or set(value) != {"closed", *_COUNTS}
            or type(value["closed"]) is not bool
            or any(
                type(value[name]) is not int or not 0 <= value[name] <= 100_000 for name in _COUNTS
            )
        ):
            raise WorkspaceUnavailable
        return dict(value)

    def _token(self, state):
        runtime = self._runtime
        policy = None if runtime is None else runtime.policy
        execution = (
            None
            if policy is None
            else {
                "workspace": str(policy.workspace),
                "readonly": [str(path) for path in policy.readonly_roots],
                "protected": [str(path) for path in policy.protected_roots],
                "allow_write": policy.allow_write,
                "deny_write": policy.deny_write,
                "allow_shell": policy.allow_shell,
                "deny_shell": policy.deny_shell,
                "network": policy.network,
                "yolo": policy.yolo,
                "shell_working_directory": policy.shell_working_directory,
                "shell_timeout_s": policy.shell_timeout_s,
                "max_steps": runtime.max_steps,
                "tool_permissions": None
                if runtime.tool_permissions is None
                else runtime.tool_permissions.fingerprint(),
            }
        )
        return _digest(
            {
                "instance": self.instance,
                "state": [state[0], state[1], state[2], state[3].model_dump(), state[4]],
                "guards": self._guard_metadata(),
                "execution": execution,
            }
        )

    def snapshot(self):
        from services.agent.tools import bwrap_path

        state = self._state()
        desired_available, desired_error = True, None
        try:
            desired = self._prepare(state[3], state[0], state[4])
            os.close(desired.root_fd)
        except (OSError, ValueError, RuntimeError):
            desired_available, desired_error = (
                False,
                "Saved workspace directory or read-only folders are unavailable",
            )
        active = self._active
        active_error = self._active_error
        if active is not None and not self._matches_runtime(active):
            active, active_error = None, "Native active workspace identity is unavailable"
        operations = self._operation_state()
        result = {
            "schema_version": "1.0",
            "backend_id": state[1],
            "instance_id": self.instance,
            "revision": state[0],
            "state_token": self._token(state),
            "writable": state[2] == self.instance,
            "saved_settings": state[3].model_dump(),
            "saved_root_identity": state[4],
            "desired_available": desired_available,
            "desired_error": desired_error,
            "active": None
            if active is None
            else {"settings": active.settings().model_dump(), "pin": active.pin.model_dump()},
            "active_matches_saved": active is not None
            and active.pin.settings_revision == state[0]
            and active.pin.root_identity == state[4]
            and active.settings() == state[3],
            "active_error": active_error,
            "busy": operations is None
            or operations["closed"]
            or any(operations[name] for name in _COUNTS)
            or self._transition is not None
            or self._cleanup_failed
            or (self._apply_pending is not None and not self._apply_pending.done()),
            "operation_state": operations,
            "host_guards": self._guard_metadata(),
            "execution": {
                "shell_backend": "bubblewrap",
                "mode": "direct",
                "binary_detected": bwrap_path() is not None,
                "isolation_verified": False,
                "supported_backends": ["bubblewrap"],
                "worktree_supported": False,
            },
            "change_boundary": "settled_service",
            "limits": {
                "readonly_paths": MAX_READONLY_PATHS,
                "receipts": MAX_RECEIPTS,
                "path_bytes": MAX_PATH_BYTES,
                "relative_path_bytes": MAX_RELATIVE_PATH_BYTES,
            },
        }
        if len(_encode(result).encode("utf-8")) > MAX_WORKSPACE_METADATA_BYTES:
            raise WorkspaceUnavailable
        return result

    @staticmethod
    def _receipt(request, root_identity):
        return {
            "schema_version": "1.0",
            "status": "workspace_saved",
            "backend_id": request.expected_backend,
            "instance_id": request.expected_instance,
            "idempotency_key": request.idempotency_key,
            "expected_revision": request.expected_revision,
            "expected_state": request.expected_state,
            "revision": request.expected_revision + 1,
            "settings": request.settings.model_dump(),
            "root_identity": root_identity,
            "workspace_id": _digest({"workspace": request.settings.root}),
            "change_boundary": "settled_service",
        }

    def _recorded(self, key):
        state = self._state()
        row = self.db.execute(
            "SELECT CASE WHEN length(CAST(digest AS BLOB))=64 THEN digest END,"
            "CASE WHEN length(CAST(request AS BLOB))<=65536 THEN request END,"
            "CASE WHEN length(CAST(receipt AS BLOB))<=65536 THEN receipt END FROM workspace_operations WHERE key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        request = WorkspaceChange.model_validate(_decode(row[1]))
        receipt = _decode(row[2])
        identity = _hex(receipt.get("root_identity") if type(receipt) is dict else None, 64)
        if (
            request.idempotency_key != key
            or request.expected_backend != state[1]
            or row[0] != _digest(request.bounded_body())
            or type(receipt.get("expected_revision")) is not int
            or type(receipt.get("revision")) is not int
            or receipt != self._receipt(request, identity)
            or receipt["revision"] > state[0]
        ):
            raise WorkspaceUnavailable
        return request, receipt

    def _validate_history(self):
        state = self._state()
        rows = self.db.execute(
            "SELECT CASE WHEN length(CAST(key AS BLOB))<=128 THEN key END FROM workspace_operations LIMIT 10001"
        ).fetchall()
        if len(rows) > MAX_RECEIPTS or len(rows) != state[0]:
            raise WorkspaceUnavailable
        revisions, latest = set(), None
        for (key,) in rows:
            operation = self._recorded(key)
            if operation is None or operation[1]["revision"] in revisions:
                raise WorkspaceUnavailable
            receipt = operation[1]
            revisions.add(receipt["revision"])
            if latest is None or latest["revision"] < receipt["revision"]:
                latest = receipt
        if latest is not None and (
            latest["settings"] != state[3].model_dump() or latest["root_identity"] != state[4]
        ):
            raise WorkspaceUnavailable

    def receipt(self, key):
        if type(key) is not str or re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", key) is None:
            raise ValueError("Invalid workspace receipt identity")
        recorded = self._recorded(key)
        return None if recorded is None else recorded[1]

    def lookup(self, request):
        request = WorkspaceChange.model_validate(request)
        body, state = request.bounded_body(), self._state()
        if request.expected_backend != state[1]:
            raise WorkspaceConflict("Workspace request belongs to another backend")
        recorded = self._recorded(request.idempotency_key)
        if recorded is not None and recorded[0].bounded_body() != body:
            raise WorkspaceConflict("Workspace key belongs to another exact request")
        return {
            "schema_version": "1.0",
            "backend_id": state[1],
            "request": body,
            "status": "recorded" if recorded is not None else "unobserved",
            "receipt": None if recorded is None else recorded[1],
            "mutation_fenced": recorded is not None
            or state[2] != request.expected_instance
            or state[0] > request.expected_revision,
        }

    def _check_change(self, request):
        state = self._state()
        if (
            request.expected_backend != state[1]
            or request.expected_instance != self.instance
            or state[2] != self.instance
            or request.expected_revision != state[0]
            or request.expected_state != self._token(state)
        ):
            raise WorkspaceConflict("Workspace authority changed; inspect the exact receipt")
        operations = self._operation_state()
        if (
            operations is None
            or operations["closed"]
            or any(operations[name] for name in _COUNTS)
            or self._cleanup_failed
            or (self._apply_pending is not None and not self._apply_pending.done())
            or self._runtime is None
        ):
            raise WorkspaceConflict(
                "Finish or retire actual native operations before changing workspace"
            )
        if (
            self.db.execute("SELECT count(*) FROM workspace_operations").fetchone()[0]
            >= MAX_RECEIPTS
        ):
            raise ValueError("Workspace receipt capacity requires owner maintenance")

    async def change(self, request):
        request = WorkspaceChange.model_validate(request)
        body = request.bounded_body()
        recorded = self._recorded(request.idempotency_key)
        if recorded is not None:
            if recorded[0].bounded_body() != body:
                raise WorkspaceConflict("Workspace key belongs to another exact request")
            return recorded[1]
        if self._transition is not None:
            raise WorkspaceConflict("Another native workspace transition is still owned")
        self._check_change(request)
        context = self._prepare(request.settings, request.expected_revision + 1)
        self._contexts.append(context)
        # ADR0084 owns the retained relative shell cwd independently. It may
        # become unavailable in a new root; shell stays unavailable until its
        # own explicit Save repairs it. File tools need no shell directory.
        try:
            prepared = self._prepare_apply(context) if self._prepare_apply is not None else None
        except BaseException:
            self._discard_context(context)
            raise
        token = object()
        self._transition = token
        self._retirement = None
        committed = False
        try:
            self._retirement = asyncio.create_task(self._retire())
            try:
                async with asyncio.timeout(10):
                    await asyncio.shield(self._retirement)
            except BaseException:
                if (
                    self._retirement.done()
                    and not self._retirement.cancelled()
                    and self._retirement.exception() is not None
                ):
                    self._cleanup_failed = True
                raise WorkspaceUnavailable from None
            self._check_change(request)
            if not context.current():
                raise WorkspaceConflict("Prepared workspace identity changed")
            if prepared is not None and not prepared.current():
                raise WorkspaceConflict("Prepared native workspace owners changed")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._check_change(request)
                if prepared is not None and not prepared.current():
                    raise WorkspaceConflict("Prepared native workspace owners changed")
                receipt = self._receipt(request, context.pin.root_identity)
                self.db.execute(
                    "UPDATE workspace_state SET revision=?,settings=?,root_identity=? WHERE id=1",
                    (
                        receipt["revision"],
                        _encode(request.settings.model_dump()),
                        context.pin.root_identity,
                    ),
                )
                self.db.execute(
                    "INSERT INTO workspace_operations VALUES (?,?,?,?)",
                    (request.idempotency_key, _digest(body), _encode(body), _encode(receipt)),
                )
                self.db.commit()
                committed = True
            except BaseException:
                try:
                    self.db.rollback()
                    if self.db.in_transaction:
                        raise WorkspaceUnavailable
                except BaseException:
                    self._failed = True
                raise
            self._active, self._active_error = None, "Saved workspace application is unconfirmed"
            result = prepared.apply() if prepared is not None else self._apply(context)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                elif isinstance(result, asyncio.Future):
                    self._apply_pending = result

                    def applied(operation):
                        if not operation.cancelled():
                            operation.exception()
                        self.release_if_settled()

                    result.add_done_callback(applied)
                else:
                    self._cleanup_failed = True
                raise WorkspaceUnavailable
            if not self._matches_runtime(context):
                raise WorkspaceUnavailable
            self._active, self._active_error = context, None
            for previous in tuple(self._contexts):
                if previous is not context:
                    self._discard_context(previous)
            return receipt
        finally:
            retirement = self._retirement

            def settled(operation):
                if operation.cancelled() or operation.exception() is not None:
                    self._cleanup_failed = True
                if self._transition is token:
                    self._transition = None
                if not committed:
                    self._discard_context(context)
                self.release_if_settled()

            if retirement is not None and not retirement.done():
                retirement.add_done_callback(settled)
            elif retirement is not None:
                settled(retirement)
            else:
                self._transition = None
                if not committed:
                    self._discard_context(context)

    def _discard_context(self, context):
        if context in self._contexts:
            self._contexts.remove(context)
            os.close(context.root_fd)

    def _release_descriptors(self):
        for context in tuple(self._contexts):
            self._discard_context(context)
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def release_if_settled(self):
        """Runtime cleanup callbacks call this; a timeout never releases a lock."""
        if (
            not self._closed
            or self._transition is not None
            or self._cleanup_failed
            or (self._apply_pending is not None and not self._apply_pending.done())
        ):
            return False
        try:
            operations = self._operation_state()
            if operations is not None and any(operations[name] for name in _COUNTS):
                return False
            if not self._db_closed:
                self.db.close()
                self._db_closed = True
            self._release_descriptors()
            return True
        except BaseException:
            return False

    def close(self):
        self._closed = True
        return self.release_if_settled()
