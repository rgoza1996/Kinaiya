"""Protected next-launch settings for the independent Agent advice policy."""

import fcntl
import os
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from packages.contracts.agent_advice import encoded
from packages.contracts.agent_control import agent_control_json
from packages.contracts.decisions_consumers import (
    MAX_POLICY_CHANGE_BYTES,
    MAX_POLICY_CHANGES,
    DecisionsAgentPolicyChange,
    DecisionsAgentPolicyLookup,
    DecisionsAgentPolicyReceipt,
    DecisionsAgentPolicySelection,
)

POLICY_STORE_NAME = "decisions-agent-policy.sqlite"
HOST_POLICY = DecisionsAgentPolicySelection(source="host")
_STATE = (
    "CREATE TABLE policy_state (id INTEGER PRIMARY KEY CHECK(id=1), "
    "backend_id TEXT NOT NULL, revision INTEGER NOT NULL, selection TEXT NOT NULL)"
)
_OPERATIONS = (
    "CREATE TABLE policy_operations (revision INTEGER PRIMARY KEY CHECK(revision>0), "
    "key TEXT NOT NULL UNIQUE, receipt TEXT NOT NULL)"
)
_SCHEMA = {
    ("table", "policy_state", "policy_state", _STATE),
    ("table", "policy_operations", "policy_operations", _OPERATIONS),
    ("index", "sqlite_autoindex_policy_operations_1", "policy_operations", None),
}


class AgentPolicySettingsUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Agent advice policy settings are unavailable")


class AgentPolicySettingsConflict(ValueError):
    def __init__(self):
        super().__init__("Agent advice policy revision or original request changed")


def _identity(path, fd):
    retained = os.fstat(fd)
    actual = path.stat(follow_symlinks=False)
    parent = path.parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
        or (actual.st_dev, actual.st_ino) != (retained.st_dev, retained.st_ino)
        or not stat.S_ISREG(actual.st_mode)
        or actual.st_uid != os.geteuid()
        or actual.st_nlink != 1
        or actual.st_mode & 0o077
        or actual.st_size > 64 * 1024 * 1024
    ):
        raise AgentPolicySettingsUnavailable


def _state(db):
    if db.execute("PRAGMA user_version").fetchone() != (1,):
        raise AgentPolicySettingsUnavailable
    schema = db.execute(
        "SELECT type,name,tbl_name,CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN sql END "
        "FROM sqlite_schema LIMIT 4"
    ).fetchall()
    if len(schema) != 3 or set(schema) != _SCHEMA:
        raise AgentPolicySettingsUnavailable
    rows = db.execute(
        "SELECT id,CASE WHEN length(CAST(backend_id AS BLOB))=32 THEN backend_id END,"
        "revision,CASE WHEN length(CAST(selection AS BLOB))<=? THEN selection END "
        "FROM policy_state LIMIT 2",
        (MAX_POLICY_CHANGE_BYTES,),
    ).fetchall()
    if len(rows) != 1:
        raise AgentPolicySettingsUnavailable
    identity, backend, revision, selection = rows[0]
    if (
        identity != 1
        or type(backend) is not str
        or re.fullmatch(r"[a-f0-9]{32}", backend) is None
        or type(revision) is not int
        or not 0 <= revision <= MAX_POLICY_CHANGES
        or type(selection) is not str
    ):
        raise AgentPolicySettingsUnavailable
    selected = _parse(DecisionsAgentPolicySelection, selection, MAX_POLICY_CHANGE_BYTES)
    count, latest = db.execute(
        "SELECT count(*),coalesce(max(revision),0) FROM policy_operations"
    ).fetchone()
    if count != revision or latest != revision:
        raise AgentPolicySettingsUnavailable
    if revision == 0:
        if selected != HOST_POLICY:
            raise AgentPolicySettingsUnavailable
    else:
        head = _receipt(db, "revision", revision, backend)
        if (
            head is None
            or head.selection_source != selected.source
            or head.policy_sha256 != _policy_digest(selected)
        ):
            raise AgentPolicySettingsUnavailable
    return backend, revision, selected


def _policy_digest(selection):
    return selection.policy.fingerprint() if selection.policy is not None else None


def _parse(model, raw, maximum=4096):
    if type(raw) is not str:
        raise AgentPolicySettingsUnavailable
    try:
        return model.model_validate(agent_control_json(raw.encode(), maximum=maximum))
    except (ValueError, TypeError, RecursionError):
        raise AgentPolicySettingsUnavailable from None


def _receipt(db, column, value, backend):
    # The column is chosen by the native caller, while all input stays bound.
    sql = (
        "SELECT revision,key,CASE WHEN length(CAST(receipt AS BLOB))<=2048 THEN receipt END "
        "FROM policy_operations WHERE "
    )
    row = db.execute(sql + ("revision=?" if column == "revision" else "key=?"), (value,)).fetchone()
    if row is None:
        return None
    revision, key, raw = row
    receipt = _parse(DecisionsAgentPolicyReceipt, raw)
    if (receipt.backend_id, receipt.revision, receipt.idempotency_key) != (backend, revision, key):
        raise AgentPolicySettingsUnavailable
    return receipt


def _connect(path, workspace, *, writable):
    if path != path.resolve() or path.is_relative_to(Path(workspace).resolve()):
        raise AgentPolicySettingsUnavailable
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC
    db = fd = None
    try:
        try:
            directory = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
        except FileNotFoundError:
            if writable:
                raise
            return None
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise AgentPolicySettingsUnavailable
            # Serialize first-writer publication for this owner. A reader of
            # an existing but uninitialized store must refuse the optional route.
            if writable:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            created = False
            if writable:
                try:
                    fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                    created = True
                except FileExistsError:
                    pass
            if fd is None:
                try:
                    fd = os.open(path.name, flags, dir_fd=directory)
                except FileNotFoundError:
                    if writable:
                        raise
                    return None
            _identity(path, fd)
            if writable:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            mode = "rw" if writable else "ro"
            db = sqlite3.connect(f"file:/proc/self/fd/{fd}?mode={mode}", uri=True, timeout=0.5)
            db.execute("PRAGMA trusted_schema=OFF")
            if writable:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                if created:
                    db.execute(_STATE)
                    db.execute(_OPERATIONS)
                    db.execute(
                        "INSERT INTO policy_state VALUES (1,?,0,?)",
                        (secrets.token_hex(16), encoded(HOST_POLICY.model_dump()).decode()),
                    )
                    db.execute("PRAGMA user_version=1")
                startup = _state(db)
                _identity(path, fd)
                db.commit()
            else:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                startup = _state(db)
                _identity(path, fd)
            return db, fd, startup
        finally:
            os.close(directory)
    except BaseException:
        try:
            if db is not None:
                db.close()
        finally:
            if fd is not None:
                os.close(fd)
        raise


def read_agent_policy_selection(path, workspace):
    """The Agent launcher can read the next selection without creating a store."""
    path = Path(os.path.abspath(path))
    try:
        opened = _connect(path, workspace, writable=False)
    except (OSError, ValueError, sqlite3.Error):
        raise AgentPolicySettingsUnavailable from None
    if opened is None:
        return HOST_POLICY
    db, fd, (_, _, selection) = opened
    try:
        return selection
    finally:
        try:
            db.close()
        finally:
            os.close(fd)


class AgentPolicySettings:
    """One Decisions writer, with revision CAS and exact historical receipts."""

    def __init__(self, path, workspace):
        self.path = Path(os.path.abspath(path))
        self.instance = secrets.token_hex(16)
        self.closed = self.failed = False
        try:
            self.db, self.fd, (self.backend, _, self.startup_selection) = _connect(
                self.path, workspace, writable=True
            )
        except (OSError, ValueError, sqlite3.Error):
            raise AgentPolicySettingsUnavailable from None

    @contextmanager
    def _transaction(self, *, write=False):
        if self.closed or self.failed:
            raise AgentPolicySettingsUnavailable
        try:
            _identity(self.path, self.fd)
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            state = _state(self.db)
            if state[0] != self.backend:
                raise AgentPolicySettingsUnavailable
            yield state[1:]
            _identity(self.path, self.fd)
            self.db.commit()
        except (OSError, sqlite3.Error, AgentPolicySettingsUnavailable):
            self.failed = True
            raise AgentPolicySettingsUnavailable from None
        finally:
            if self.db.in_transaction:
                self.db.rollback()

    def inspect(self):
        with self._transaction() as state:
            return state

    def change(self, original, *, check_policy):
        original = DecisionsAgentPolicyChange.model_validate(original)
        digest = original.fingerprint()
        with self._transaction(write=True) as (revision, _):
            prior = self._lookup(original, digest)
            if prior is not None:
                return prior
            if (
                original.expected_instance != self.instance
                or original.expected_revision != revision
                or revision >= MAX_POLICY_CHANGES
            ):
                raise AgentPolicySettingsConflict
            if original.selection.policy is not None:
                check_policy(original.selection.policy)
            receipt = DecisionsAgentPolicyReceipt(
                backend_id=self.backend,
                instance_id=self.instance,
                revision=revision + 1,
                idempotency_key=original.idempotency_key,
                request_sha256=digest,
                selection_source=original.selection.source,
                policy_sha256=_policy_digest(original.selection),
            )
            self.db.execute(
                "INSERT INTO policy_operations VALUES (?,?,?)",
                (receipt.revision, receipt.idempotency_key, encoded(receipt.model_dump()).decode()),
            )
            self.db.execute(
                "UPDATE policy_state SET revision=?,selection=? WHERE id=1",
                (receipt.revision, encoded(original.selection.model_dump()).decode()),
            )
            return receipt

    def _lookup(self, original, digest):
        if original.expected_backend != self.backend:
            raise AgentPolicySettingsConflict
        prior = _receipt(self.db, "key", original.idempotency_key, self.backend)
        if prior is not None:
            if prior.request_sha256 != digest:
                raise AgentPolicySettingsConflict
            if (
                prior.instance_id != original.expected_instance
                or prior.revision != original.expected_revision + 1
                or prior.selection_source != original.selection.source
                or prior.policy_sha256 != _policy_digest(original.selection)
            ):
                raise AgentPolicySettingsUnavailable
        return prior

    def lookup(self, original):
        original = DecisionsAgentPolicyChange.model_validate(original)
        digest = original.fingerprint()
        with self._transaction() as (revision, _):
            prior = self._lookup(original, digest)
            return DecisionsAgentPolicyLookup(
                backend_id=self.backend,
                instance_id=self.instance,
                current_revision=revision,
                request_sha256=digest,
                receipt=prior,
                mutation_fenced=prior is None
                and (
                    original.expected_instance != self.instance
                    or original.expected_revision < revision
                    or revision == MAX_POLICY_CHANGES
                ),
            )

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.db.close()
        finally:
            os.close(self.fd)
