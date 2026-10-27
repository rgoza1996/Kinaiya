"""Durable owner MCP server configuration controller (issue #44).

The maintained-SDK :class:`MCPHost <packages.ecosystem.mcp_host.MCPHost>`
already brokers sandboxed stdio and bounded Streamable HTTP tools. This
module adds the durable owner setup/configuration/lifecycle plane on top
of it. The Agent task-admission gate lives outside (lead-owned): every
``control()`` call is wrapped by the Agent admission lock, so this
controller never needs to know about tasks.

Execution boundaries (hard rules, tested):

* Construction, ``configure``, ``snapshot``, ``enable``, ``disable`` and
  ``remove`` never start a new server or execute its code. Retiring an
  existing transport may perform protocol cleanup. Only explicit ``test`` /
  ``reconnect`` initialize/list tools; no operation ever calls a tool.
* The native host module is imported as-is and never weakened: stdio
  keeps the shell grant plus bubblewrap isolation, HTTP keeps the
  network grant plus per-server remote opt-in, and secrets resolve only
  through the supplied resolver. Failures are scrubbed.
* No OAuth, resources, or prompts are claimed or advertised.

Durability model:

* ``root`` is a protected directory disjoint from ``workspace`` in both
  directions (compared after symlink resolution, and no ancestor of
  either path may itself be a symlink). A no-follow, regular,
  owner-only, non-hardlinked lockfile is flocked (single writer) for the
  controller lifetime *before* the SQLite database is initialized.
* SQLite (mode 0600, ``synchronous=FULL``) stores bounded descriptors,
  one global monotone revision, applied-state tracking, intent records
  (idempotent results), and bounded control history.
* ``initial_configs`` seed the database only when no managed state
  exists; afterwards persisted state is authoritative and the argument
  is ignored. Seeding performs no network or process work. Explicitly
  configured legacy ``enabled`` descriptors are preserved on first
  seed, but the ``configure`` operation only ever stores disabled
  definitions -- enablement is a separate explicit, reviewed ``enable``
  step, and ``reconcile`` is the only path that restores prior explicit
  enables onto a fresh live host.

Limits (also enforced in code):

* at most 16 server identities;
* descriptor documents (canonical JSON per server, and the whole desired
  set) of at most 256 KiB;
* at most 2048 audit rows (oldest pruned) and 2048 retained intent records;
  identities are never evicted and one slot is reserved for recovery;
* idempotency keys are 1..128 chars without NUL/control characters;
* revisions are monotone integers; every bumping mutation consumes
  exactly ``revision + 1`` and requires the caller to present the exact
  current revision (optimistic stale-write refusal).

Failure model: the intent row is committed *before* effects run, so a
duplicate key either returns the exact stored bounded result or reports
a conflict -- never silently replays. A mutation whose live application
fails (or is cancelled) leaves the controller ``blocked`` with the live
host closed, so no call can run under unknown/mismatched config; only
an explicit reviewed owner ``reconcile`` applies the inspected durable
desired definitions (restoring prior explicit enables) without replaying
any tool request. Until reconciled, the live plane exposes
disabled/no-access configs.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.policy import ToolPolicy
from packages.ecosystem.mcp_host import MCPConfig, MCPError, MCPHost

SCHEMA_VERSION = "1.0"
MAX_SERVERS = 16
MAX_DESCRIPTOR_BYTES = 256 * 1024
MAX_HISTORY_ROWS = 2048
MAX_CONTROL_INTENTS = 2048
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_REVISION = 2147483647
MAX_STORED_RESULT_BYTES = 65536

_OPERATIONS = (
    "configure",
    "enable",
    "disable",
    "remove",
    "test",
    "reconnect",
    "reconcile",
)
# Mutations consume revision+1 and require the exact current revision.
_BUMPING_OPERATIONS = ("configure", "enable", "disable", "remove")

_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REF_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_DB_NAME = "mcp_control.sqlite3"
_LOCK_NAME = "controller.lock"


class MCPControlError(ValueError):
    """Safe, bounded control-plane failure. Never carries secrets or descriptors."""


class _CleanupUncertain(MCPControlError):
    pass


async def _settle_cleanup(awaitable) -> bool:
    """Finish transport reaping even if the caller is cancelled repeatedly."""
    worker = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            await asyncio.shield(worker)
            return cancelled
        except asyncio.CancelledError:
            if worker.cancelled():
                raise
            cancelled = True


def _fail(message: str) -> MCPControlError:
    return MCPControlError(message)


def _has_controls(text: str, *, allow_tab: bool = False) -> bool:
    for char in text:
        code = ord(char)
        if code == 0x09 and allow_tab:
            continue
        if code < 0x20 or code == 0x7F:
            return True
    return False


def _check_id_string(value: Any, *, what: str = "server identity") -> str:
    if not isinstance(value, str) or not _SERVER_ID_RE.fullmatch(value) or len(value) > 64:
        raise _fail(f"invalid {what}")
    return value


def _check_identity(key: Any) -> str:
    if not isinstance(key, str) or not key or len(key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise _fail("invalid request identity")
    if "\x00" in key or _has_controls(key):
        raise _fail("invalid request identity")
    return key


def _check_revision_shape(value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail("invalid configuration revision")
    if value < 0 or value > MAX_REVISION:
        raise _fail("invalid configuration revision")


def _require_exact_revision(value: int | None, *, required: bool, current: int) -> None:
    if value is None:
        if required:
            raise _fail("exact configuration revision is required")
        return
    if value != current:
        raise _fail(f"stale configuration revision (current {current})")


def _canonical_descriptor(config: MCPConfig) -> str:
    return json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _descriptor_digest(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_secret_refs(config: MCPConfig) -> None:
    """Every secret ref must be exactly ``env:VALID_ENV_NAME``.

    This is a format rule on the dedicated ref fields only: it rejects
    embedded/inline secret values without inventing content heuristics
    and without widening environment grants. Ordinary command-line data
    in ``args`` is left alone (only NUL/control bytes are rejected).
    """
    refs = {**config.env_secret_refs, **config.header_secret_refs}
    for ref in refs.values():
        if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
            raise _fail("secret reference uses an unsupported format")


def _check_descriptor_text(config: MCPConfig) -> None:
    """Reject NUL/control characters anywhere in a descriptor.

    Horizontal tabs are tolerated inside stdio ``args`` items only, since
    those carry ordinary command-line data.
    """

    def visit(node: Any) -> None:
        if isinstance(node, str):
            if "\x00" in node or _has_controls(node):
                raise _fail("descriptor contains unsupported characters")
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and ("\x00" in key or _has_controls(key)):
                    raise _fail("descriptor contains unsupported characters")
                visit(value)

    dumped = config.model_dump(mode="json")
    if isinstance(dumped.get("args"), list):
        for item in dumped["args"]:
            if isinstance(item, str):
                if "\x00" in item or _has_controls(item, allow_tab=True):
                    raise _fail("descriptor contains unsupported characters")
            else:
                visit(item)
    for key, value in dumped.items():
        if key == "args":
            continue
        if isinstance(key, str) and ("\x00" in key or _has_controls(key)):
            raise _fail("descriptor contains unsupported characters")
        visit(value)


def _parse_config(payload: Any) -> MCPConfig:
    """Validate an untrusted mapping into MCPConfig without echoing it."""
    if isinstance(payload, MCPConfig):
        candidate = payload
    elif isinstance(payload, dict):
        try:
            candidate = MCPConfig(**payload)
        except ValidationError:
            raise _fail("invalid server descriptor") from None
    else:
        raise _fail("invalid server descriptor")
    try:
        candidate = MCPConfig.model_validate(candidate.model_dump(mode="json"))
    except ValidationError:
        raise _fail("invalid server descriptor") from None
    _check_secret_refs(candidate)
    _check_descriptor_text(candidate)
    canonical = _canonical_descriptor(candidate)
    if len(canonical.encode("utf-8")) > MAX_DESCRIPTOR_BYTES:
        raise _fail("server descriptor exceeds size bounds")
    return candidate


def _intent_digest(
    *,
    operation: str,
    server_id: str | None,
    config_canonical: str | None,
    expected_revision: int | None,
    reviewed: bool,
    actor: str,
) -> str:
    canonical = json.dumps(
        {
            "actor": actor,
            "config": config_canonical,
            "expected_revision": expected_revision,
            "operation": operation,
            "reviewed": reviewed,
            "server_id": server_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reject_symlink_ancestors(path: Path) -> None:
    """Fail when the lexical path or any ancestor is a symlink.

    Runs on the unresolved path before any file is created, so a
    symlinked protected root cannot smuggle the store elsewhere. Missing
    trailing components are fine (they cannot be symlinks yet).
    """
    candidates = [path, *path.parents]
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                raise _fail("protected root must not traverse a symlink")
        except OSError:
            raise _fail("protected root is not accessible") from None


class MCPController:
    """Durable owner configuration plane over an :class:`MCPHost`."""

    def __init__(
        self,
        root: Path,
        workspace: Path,
        policy: ToolPolicy,
        *,
        initial_configs: list[MCPConfig],
        secret_resolver=None,
    ) -> None:
        if not isinstance(policy, ToolPolicy):
            raise _fail("configuration controller requires a ToolPolicy")
        if not isinstance(initial_configs, list):
            raise _fail("initial server definitions must be a list")
        for entry in initial_configs:
            if not isinstance(entry, (MCPConfig, dict)):
                raise _fail("initial server definitions must be mappings")

        raw_root = Path(root)
        raw_workspace = Path(workspace)
        _reject_symlink_ancestors(raw_root.absolute())
        _reject_symlink_ancestors(raw_workspace.absolute())
        resolved_root = raw_root.resolve()
        resolved_workspace = raw_workspace.resolve()
        if policy.workspace.resolve() != resolved_workspace:
            raise _fail("controller workspace must match its ToolPolicy")
        if resolved_root == resolved_workspace:
            raise _fail("protected root must be disjoint from the workspace")
        if resolved_root.is_relative_to(resolved_workspace):
            raise _fail("protected root must be disjoint from the workspace")
        if resolved_workspace.is_relative_to(resolved_root):
            raise _fail("protected root must be disjoint from the workspace")

        try:
            resolved_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError:
            raise _fail("protected root is not writable") from None
        try:
            os.chmod(resolved_root, 0o700)
        except OSError:
            raise _fail("protected root is not writable") from None
        if resolved_root.stat().st_uid != os.geteuid():
            raise _fail("protected root has an unexpected owner")

        lock_fd = self._take_lifetime_lock(resolved_root)
        try:
            db = self._open_database(resolved_root)
        except Exception:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            raise
        self._lock_fd = lock_fd
        self._db = db
        self._root = resolved_root
        self._workspace = resolved_workspace
        self._policy = policy
        self._resolver = secret_resolver
        self._lock = asyncio.Lock()
        self._closed = False
        self._runtime_blocked = False
        self._uncertain_hosts: list[MCPHost] = []
        self._definition_change = contextvars.ContextVar("mcp_definition_change", default=False)

        try:
            managed = self._db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if managed is None:
                self._seed(initial_configs)
            elif managed[0] != SCHEMA_VERSION:
                raise _fail("unsupported management store version")
            self._recover_pending_intents()
            blocked = self._meta("blocked") == "1"
            revision = int(self._meta("revision"))
            applied = int(self._meta("applied_revision"))
            if blocked or applied != revision:
                live = self._disabled_copy(self._desired_configs())
                self._host = MCPHost(
                    live,
                    policy,
                    secret_resolver,
                    access_guard=self._guard_access,
                    definition_guard=self._guard_definition_change,
                )
                if blocked or applied != revision:
                    self._set_meta("blocked", "1")
            else:
                self._host = MCPHost(
                    self._desired_configs(),
                    policy,
                    secret_resolver,
                    access_guard=self._guard_access,
                    definition_guard=self._guard_definition_change,
                )
        except Exception:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001 - constructor must not leak fds
                pass
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            raise

    def __repr__(self) -> str:
        try:
            revision = self._meta("revision")
        except Exception:  # noqa: BLE001 - repr never raises or leaks state
            revision = "?"
        return f"MCPController(root={str(getattr(self, '_root', '?'))!r}, revision={revision!r})"

    # -- construction helpers -------------------------------------------------

    @staticmethod
    def _take_lifetime_lock(resolved_root: Path) -> int:
        """Open (no-follow) and flock the single-writer lockfile for life."""
        path = resolved_root / _LOCK_NAME
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path, flags, 0o600)
        except OSError:
            raise _fail("management lockfile is not usable") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise _fail("management lockfile is not usable")
            if st.st_nlink != 1:
                raise _fail("management lockfile is not usable")
            if st.st_uid != os.geteuid():
                raise _fail("management lockfile has an unexpected owner")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise _fail("management plane is already held by a writer") from None
        except Exception as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            if isinstance(exc, MCPControlError):
                raise
            raise _fail("management lockfile is not usable") from None
        return fd

    @staticmethod
    def _open_database(resolved_root: Path) -> sqlite3.Connection:
        path = resolved_root / _DB_NAME
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError:
            raise _fail("management store is not usable") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise _fail("management store is not usable")
            if st.st_uid != os.geteuid():
                raise _fail("management store has an unexpected owner")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        old_umask = os.umask(0o077)
        try:
            db = sqlite3.connect(str(path), check_same_thread=True)
        finally:
            os.umask(old_umask)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS servers(
                    id TEXT PRIMARY KEY,
                    descriptor TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    rev INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intents(
                    key TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    updated_rev INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    op TEXT NOT NULL,
                    server_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                """
            )
            db.commit()
        except sqlite3.Error:
            db.close()
            raise _fail("management store is not usable") from None
        return db

    # -- durable state helpers --------------------------------------------------

    def _meta(self, key: str) -> str:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise _fail("management store is incomplete")
        return str(row[0])

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _seed(self, initial_configs: list[Any]) -> None:
        """First-boot seed only; never overwrites managed state.

        Preserves explicitly configured legacy ``enabled`` descriptors as
        given, but still applies the full strict validation (MCPConfig
        plus reference-only rules): an uninspectable seed is refused
        rather than executed later. Consumes no revision.
        """
        if len(initial_configs) > MAX_SERVERS:
            raise _fail("at most 16 MCP servers are supported")
        seen: set[str] = set()
        total = 0
        rows: list[tuple[str, str, str, int, int]] = []
        for entry in initial_configs:
            config = _parse_config(entry)
            if config.id in seen:
                raise _fail("duplicate server identity")
            seen.add(config.id)
            canonical = _canonical_descriptor(config)
            total += len(canonical.encode("utf-8"))
            if total > MAX_DESCRIPTOR_BYTES:
                raise _fail("server descriptors exceed size bounds")
            rows.append(
                (
                    config.id,
                    canonical,
                    _descriptor_digest(canonical),
                    1 if config.enabled else 0,
                    0,
                )
            )
        self._set_meta("schema_version", SCHEMA_VERSION)
        self._set_meta("revision", "0")
        self._set_meta("applied_revision", "0")
        self._set_meta("blocked", "0")
        self._db.executemany(
            "INSERT INTO servers(id, descriptor, sha256, enabled, rev) VALUES(?, ?, ?, ?, ?)",
            rows,
        )
        self._record_history("seed", "", 0, "ok", f"{len(rows)} server(s) seeded")
        self._db.commit()

    def _recover_pending_intents(self) -> None:
        """Mark crash/cancel-orphaned intents interrupted and fail closed.

        A pending outcome is never silently replayed: duplicates observe
        the stored interruption and must reconcile instead.
        """
        pending = self._db.execute("SELECT key FROM intents WHERE status='pending'").fetchall()
        if not pending:
            return
        payload = json.dumps(
            {"status": "interrupted", "detail": "outcome unknown; reconcile"},
            sort_keys=True,
            separators=(",", ":"),
        )
        self._db.execute(
            "UPDATE intents SET status='interrupted', result=? WHERE status='pending'",
            (payload,),
        )
        self._set_meta("blocked", "1")
        self._db.commit()

    def _desired_configs(self) -> list[MCPConfig]:
        sizes = self._db.execute("SELECT length(CAST(descriptor AS BLOB)) FROM servers").fetchall()
        if len(sizes) > MAX_SERVERS or sum(row[0] for row in sizes) > MAX_DESCRIPTOR_BYTES:
            raise _fail("stored server definitions exceed bounds")
        rows = self._db.execute("SELECT id,descriptor,sha256 FROM servers ORDER BY id").fetchall()
        configs = []
        for server_id, descriptor, digest in rows:
            try:
                config = _parse_config(json.loads(descriptor))
            except (ValueError, RecursionError):
                raise _fail("stored server definition is invalid") from None
            if (
                config.id != server_id
                or _descriptor_digest(_canonical_descriptor(config)) != digest
            ):
                raise _fail("stored server definition digest does not match")
            configs.append(config)
        return configs

    def _desired_map(self) -> dict[str, MCPConfig]:
        return {config.id: config for config in self._desired_configs()}

    def _sha_map(self) -> dict[str, str]:
        return {
            row[0]: row[1] for row in self._db.execute("SELECT id, sha256 FROM servers").fetchall()
        }

    @staticmethod
    def _disabled_copy(configs: list[MCPConfig]) -> list[MCPConfig]:
        return [config.model_copy(update={"enabled": False}) for config in configs]

    def _record_history(
        self,
        op: str,
        server_id: str,
        revision: int,
        status: str,
        detail: str,
    ) -> None:
        # History stores only bounded safe metadata: operation names from
        # the fixed set, pattern-checked server ids, ints, and fixed
        # safe strings. Raw descriptors and secrets never reach it.
        self._db.execute(
            "INSERT INTO history(op, server_id, revision, status, detail) VALUES(?, ?, ?, ?, ?)",
            (op, server_id, revision, status, detail[:300]),
        )
        self._db.execute(
            "DELETE FROM history WHERE seq NOT IN "
            f"(SELECT seq FROM history ORDER BY seq DESC LIMIT {MAX_HISTORY_ROWS})"
        )

    @property
    def blocked(self) -> bool:
        if self._closed or self._runtime_blocked:
            return True
        try:
            return self._meta("blocked") == "1" or self._meta("applied_revision") != self._meta(
                "revision"
            )
        except (sqlite3.Error, MCPControlError):
            return True

    def _guard_access(self) -> None:
        if self.blocked:
            raise _fail("MCP management requires reconciliation")

    def _guard_definition_change(self) -> None:
        if not self._definition_change.get():
            raise _fail("managed MCP definitions must change through the owner controller")

    async def _apply_descriptors(self, configs):
        token = self._definition_change.set(True)
        try:
            await self._host.replace_configs(actor="owner", configs=configs)
        finally:
            self._definition_change.reset(token)

    async def retire_connections(self, *, actor: str) -> None:
        """Trusted owner permission change; retire transports without changing definitions.

        Revocation remains usable during Agent work. The host serializes
        transport cleanup; descriptor changes still require normal admission.
        """
        if actor != "owner" or self._closed:
            raise _fail("transport retirement requires a live owner controller")
        try:
            cancelled = await _settle_cleanup(self._host.reset_connections(actor="owner"))
        except Exception:
            self._runtime_blocked = True
            try:
                self._set_meta("blocked", "1")
                self._db.commit()
            except sqlite3.Error:
                pass
            await self._close_host_best_effort()
            raise _fail("transport retirement is unconfirmed; reconcile") from None
        if cancelled:
            raise asyncio.CancelledError

    # -- read-only inspect ------------------------------------------------------

    @property
    def host(self) -> MCPHost:
        """Trusted broker handle; descriptor replacement is controller-guarded."""
        return self._host

    def snapshot(self) -> dict[str, Any]:
        """Bounded safe metadata: host projection plus descriptor digests.

        Never carries raw commands/args, secret values, ref values, or
        config dumps. The caller receives deep copies.
        """
        if self._closed:
            raise _fail("management plane is closed")
        revision = int(self._meta("revision"))
        blocked = self.blocked
        sha_by_id = self._sha_map()
        servers = []
        for entry in self._host.list_servers():
            # Host projection is preserved verbatim except the stdio
            # endpoint: the native sanitized endpoint embeds the raw
            # command, and this plane never publishes raw commands/args.
            # HTTP endpoints stay as the host sanitizes them (scheme +
            # host + path only, no userinfo/query/fragment).
            endpoint = entry["endpoint"]
            if entry["transport"] == "stdio":
                endpoint = "stdio:[redacted]"
            servers.append(
                {
                    "id": entry["id"],
                    "transport": entry["transport"],
                    "enabled": entry["enabled"],
                    "allowed_tools": list(entry["allowed_tools"]),
                    "endpoint": endpoint,
                    "timeout_seconds": entry["timeout_seconds"],
                    "allow_remote": entry["allow_remote"],
                    "content_scope": entry["content_scope"],
                    "credentialed": entry["credentialed"],
                    "secret_refs": list(entry["secret_refs"]),
                    "config_sha256": sha_by_id.get(entry["id"]),
                }
            )
        servers.sort(key=lambda item: item["id"])
        return copy.deepcopy(
            {
                "schema_version": SCHEMA_VERSION,
                "revision": revision,
                "blocked": blocked,
                "servers": servers,
            }
        )

    # -- control plane ----------------------------------------------------------

    async def control(
        self,
        operation: str,
        *,
        idempotency_key: str,
        server_id: str | None = None,
        config: dict | None = None,
        expected_revision: int | None = None,
        reviewed: bool = False,
        actor: str = "owner",
    ) -> dict[str, Any]:
        """Reserve a stable request identity before state-dependent planning."""
        if operation not in _OPERATIONS or actor != "owner":
            raise _fail("control requires a supported owner operation")
        key = _check_identity(idempotency_key)
        if server_id is not None:
            server_id = _check_id_string(server_id)
        _check_revision_shape(expected_revision)
        if type(reviewed) is not bool:
            raise _fail("review flag must be a boolean")
        if config is not None and operation not in {"configure", "test"}:
            raise _fail("unexpected server descriptor")
        if operation == "reconcile" and server_id is not None:
            raise _fail("unexpected server identity")
        canonical = _canonical_descriptor(_parse_config(config)) if config is not None else None
        digest = _intent_digest(
            operation=operation,
            server_id=server_id,
            config_canonical=canonical,
            expected_revision=expected_revision,
            reviewed=reviewed,
            actor=actor,
        )
        async with self._lock:
            if self._closed:
                raise _fail("management plane is closed")
            row = self._db.execute(
                "SELECT digest,status,result FROM intents WHERE key=?", (key,)
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise _fail("request identity already used")
                if row[1] == "completed":
                    stored = json.loads(row[2])
                    if stored.get("status") == "error":
                        raise _fail(str(stored.get("detail", "control failed")))
                    return copy.deepcopy(stored)
                raise _fail(
                    "request was interrupted; reconcile"
                    if row[1] == "interrupted"
                    else "request is already in progress"
                )
            revision = int(self._meta("revision"))
            _require_exact_revision(
                expected_revision, required=operation in _BUMPING_OPERATIONS, current=revision
            )
            if self.blocked and operation != "reconcile":
                raise _fail("management plane is blocked; reconcile")
            # Never evict identities and accidentally permit old effects to replay.
            # One slot remains available for explicit recovery after a failed last mutation.
            limit = MAX_CONTROL_INTENTS if operation == "reconcile" else MAX_CONTROL_INTENTS - 1
            if self._db.execute("SELECT count(*) FROM intents").fetchone()[0] >= limit:
                raise _fail("management request history is full; preserve/export the store")
            plan = self._plan(operation, server_id=server_id, config=config, reviewed=reviewed)
            reserved = False
            try:
                self._db.execute(
                    "INSERT INTO intents VALUES (?,?,'pending','{}',?)", (key, digest, revision)
                )
                self._db.commit()
                reserved = True
                if plan.mutates_durable:
                    self._runtime_blocked = True
                    assert plan.new_desired is not None
                    self._write_desired(plan.new_desired)
                    revision += 1
                    self._set_meta("revision", str(revision))
                    self._set_meta("blocked", "1")
                    self._db.commit()
                result = await self._effects(operation, plan, revision)
                stored = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
                if len(stored.encode()) > MAX_STORED_RESULT_BYTES:
                    raise _fail("control result exceeds bounds")
                self._db.execute(
                    "UPDATE intents SET status='completed',result=?,updated_rev=? WHERE key=?",
                    (stored, revision, key),
                )
                self._record_history(
                    operation, plan.server_label, revision, "ok", plan.history_detail
                )
                self._db.commit()
                if plan.mutates_durable or operation == "reconcile":
                    self._runtime_blocked = False
                return copy.deepcopy(result)
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                safe_failure = isinstance(exc, (MCPControlError, MCPError))
                must_block = (
                    plan.mutates_durable
                    or operation == "reconcile"
                    or isinstance(exc, _CleanupUncertain)
                    or not (safe_failure or cancelled)
                )
                message = (
                    str(exc)[:300]
                    if isinstance(exc, MCPControlError)
                    else self._safe_host_message(operation)
                )
                try:
                    self._db.rollback()
                    if cancelled and reserved:
                        self._db.execute(
                            "UPDATE intents SET status='interrupted',result=? WHERE key=?",
                            ('{"status":"error","detail":"request was interrupted"}', key),
                        )
                        if must_block:
                            self._set_meta("blocked", "1")
                        self._db.commit()
                    elif reserved:
                        self._fail_intent(
                            key, operation, plan, revision, message, fail_closed=must_block
                        )
                    elif must_block:
                        self._set_meta("blocked", "1")
                        self._db.commit()
                except Exception:
                    must_block = True
                if must_block:
                    self._runtime_blocked = True
                    await self._close_host_best_effort()
                if cancelled:
                    raise
                raise _fail(message) from None

    def _fail_intent(
        self,
        key: str,
        operation: str,
        plan: _Plan,
        revision: int,
        message: str,
        *,
        fail_closed: bool,
    ) -> None:
        self._db.execute(
            "UPDATE intents SET status='completed', result=?, updated_rev=? WHERE key=?",
            (
                json.dumps(
                    {"status": "error", "detail": message[:300]},
                    separators=(",", ":"),
                ),
                revision,
                key,
            ),
        )
        self._record_history(operation, plan.server_label, revision, "error", message)
        if fail_closed:
            self._set_meta("blocked", "1")
        try:
            self._db.commit()
        except sqlite3.Error:
            raise _fail("management store is not usable") from None

    async def _close_host_best_effort(self) -> None:
        """Close live transports without letting cleanup mask the cause."""
        try:
            await _settle_cleanup(self._host.close())
        except Exception:  # noqa: BLE001 - guard remains closed even if cleanup fails
            pass

    # -- planning ---------------------------------------------------------------

    def _plan(
        self,
        operation: str,
        *,
        server_id: str | None,
        config: dict | None,
        reviewed: bool,
    ) -> _Plan:
        """Validate request shape against durable state. No effects here."""
        desired = self._desired_map()
        if operation == "configure":
            if config is None:
                raise _fail("configuration requires a server definition")
            candidate = _parse_config(config)
            if server_id is not None and candidate.id != server_id:
                raise _fail("server identity does not match the definition")
            if not reviewed:
                raise _fail("configuration requires explicit owner review")
            if candidate.enabled:
                raise _fail("new definitions must be disabled when configured")
            # Updates never inherit enablement or hidden grants: the
            # replacement is stored exactly as reviewed (disabled), with
            # exactly the reviewed allowlist/scope/refs.
            merged = dict(desired)
            merged[candidate.id] = candidate
            if len(merged) > MAX_SERVERS:
                raise _fail("at most 16 MCP servers are supported")
            self._check_total_size(merged)
            return _Plan(
                server_label=candidate.id,
                config_canonical=_canonical_descriptor(candidate),
                mutates_durable=True,
                new_desired=sorted(merged.values(), key=lambda item: item.id),
                history_detail="definition stored disabled",
            )
        if operation in ("enable", "disable", "remove"):
            if server_id is None:
                raise _fail("server identity is required")
            current = desired.get(server_id)
            if current is None:
                raise _fail("unknown server identity")
            if operation == "enable":
                if not reviewed:
                    raise _fail("enable requires explicit owner review")
                updated = current.model_copy(update={"enabled": True})
                detail = "server enabled"
            elif operation == "disable":
                updated = current.model_copy(update={"enabled": False})
                detail = "server disabled"
            else:
                merged = {key: value for key, value in desired.items() if key != server_id}
                self._check_total_size(merged)
                return _Plan(
                    server_label=server_id,
                    config_canonical=None,
                    mutates_durable=True,
                    new_desired=sorted(merged.values(), key=lambda item: item.id),
                    history_detail="server removed",
                )
            merged = dict(desired)
            merged[server_id] = updated
            self._check_total_size(merged)
            return _Plan(
                server_label=server_id,
                config_canonical=_canonical_descriptor(updated),
                mutates_durable=True,
                new_desired=sorted(merged.values(), key=lambda item: item.id),
                history_detail=detail,
            )
        if operation == "test":
            if config is None and server_id is None:
                raise _fail("test requires a server identity or definition")
            if config is not None:
                candidate = _parse_config(config)
                if server_id is not None and candidate.id != server_id:
                    raise _fail("server identity does not match the definition")
                captured = candidate
            else:
                assert server_id is not None
                captured = desired.get(server_id)
                if captured is None:
                    raise _fail("unknown server identity")
            return _Plan(
                server_label=captured.id,
                config_canonical=_canonical_descriptor(captured),
                mutates_durable=False,
                new_desired=None,
                history_detail="connection tested",
                test_capture=captured,
            )
        if operation == "reconnect":
            if server_id is not None and server_id not in desired:
                raise _fail("unknown server identity")
            return _Plan(
                server_label=server_id or "",
                config_canonical=None,
                mutates_durable=False,
                new_desired=None,
                history_detail="connections retired and rediscovered",
            )
        if operation == "reconcile":
            if not reviewed:
                raise _fail("reconcile requires explicit owner review")
            return _Plan(
                server_label="",
                config_canonical=None,
                mutates_durable=False,
                new_desired=None,
                history_detail="durable definitions applied",
            )
        raise _fail("unknown control operation")

    @staticmethod
    def _check_total_size(desired: dict[str, MCPConfig]) -> None:
        total = sum(
            len(_canonical_descriptor(config).encode("utf-8")) for config in desired.values()
        )
        if total > MAX_DESCRIPTOR_BYTES:
            raise _fail("server descriptors exceed size bounds")

    def _write_desired(self, configs: list[MCPConfig]) -> None:
        """Persist the full desired set inside one transaction."""
        self._db.execute("DELETE FROM servers")
        self._db.executemany(
            "INSERT INTO servers(id, descriptor, sha256, enabled, rev) VALUES(?, ?, ?, ?, ?)",
            [
                (
                    config.id,
                    _canonical_descriptor(config),
                    _descriptor_digest(_canonical_descriptor(config)),
                    1 if config.enabled else 0,
                    0,
                )
                for config in configs
            ],
        )

    # -- effects ------------------------------------------------------------------

    @staticmethod
    def _safe_host_message(operation: str) -> str:
        if operation in ("test", "reconnect"):
            return "connection attempt failed"
        return "management plane failed to apply the change"

    async def _effects(self, operation: str, plan: _Plan, revision: int) -> dict[str, Any]:
        if operation in ("configure", "enable", "disable", "remove"):
            assert plan.new_desired is not None
            await self._apply_descriptors(plan.new_desired)
            self._set_meta("applied_revision", str(revision))
            self._set_meta("blocked", "0")
            status = {
                "configure": "configured",
                "enable": "enabled",
                "disable": "disabled",
                "remove": "removed",
            }[operation]
            return {
                "status": status,
                "server_id": plan.server_label,
                "revision": revision,
            }
        if operation == "test":
            assert plan.test_capture is not None
            return await self._effect_test(plan.test_capture)
        if operation == "reconnect":
            return await self._effect_reconnect(plan.server_label or None)
        if operation == "reconcile":
            for temporary in list(self._uncertain_hosts):
                await self._close_temporary(temporary)
            if await _settle_cleanup(self._host.close()):
                raise asyncio.CancelledError
            desired = self._desired_configs()
            await self._apply_descriptors(desired)
            self._set_meta("applied_revision", str(revision))
            self._set_meta("blocked", "0")
            return {
                "status": "reconciled",
                "revision": revision,
                "servers": len(desired),
            }
        raise _fail("unknown control operation")

    async def _effect_test(self, captured: MCPConfig) -> dict[str, Any]:
        """Connect one captured config via a temporary host, then reap it.

        The probe copy is enabled only for this test. Only bounded
        ``{name, allowed}`` metadata is returned -- never full schemas or
        tool output -- and no tool is ever called.
        """
        probe = captured.model_copy(update={"enabled": True})
        allowlist = set(probe.allowed_tools)
        temporary = MCPHost([probe], self._policy, self._resolver)
        self._uncertain_hosts.append(temporary)
        try:
            try:
                listed = await temporary.discover(probe.id)
            except Exception as exc:  # noqa: BLE001 - probe failure is honest
                raise _fail("connection attempt failed") from exc
            tools = []
            for item in listed:
                name = item.get("name") if isinstance(item, dict) else None
                if not isinstance(name, str) or not name:
                    continue
                tools.append({"name": name, "allowed": name in allowlist})
                if len(tools) >= 128:
                    break
            tools.sort(key=lambda item: item["name"])
            return {
                "status": "connected",
                "server_id": probe.id,
                "transport": probe.transport,
                "tools": tools,
            }
        finally:
            await self._close_temporary(temporary)

    async def _close_temporary(self, temporary: MCPHost) -> None:
        try:
            cancelled = await _settle_cleanup(temporary.close())
        except asyncio.CancelledError:
            raise _CleanupUncertain("connection cleanup incomplete; reconcile") from None
        except Exception:
            raise _CleanupUncertain("connection cleanup incomplete; reconcile") from None
        self._uncertain_hosts.remove(temporary)
        if cancelled:
            raise asyncio.CancelledError

    async def _effect_reconnect(self, server_id: str | None) -> dict[str, Any]:
        """Retire live connections, then discover (never call, never replay).

        A disabled target is an explicit error: reconnect never silently
        enables a server.
        """
        try:
            await self._host.reset_connections(actor="owner")
        except Exception as exc:  # noqa: BLE001 - reconnect failure is honest
            raise _fail("connection attempt failed") from exc
        live = {entry["id"]: entry for entry in self._host.list_servers()}
        if server_id is not None:
            entry = live.get(server_id)
            if entry is None:
                raise _fail("unknown server identity")
            if not entry["enabled"]:
                raise _fail("server is disabled")
            return await self._discover_live(server_id, str(entry["transport"]))
        results = []
        for entry in sorted(live.values(), key=lambda item: item["id"]):
            if not entry["enabled"]:
                continue
            results.append(await self._discover_live(entry["id"], str(entry["transport"])))
        return {"status": "connected", "servers": results}

    async def _discover_live(self, server_id: str, transport: str) -> dict[str, Any]:
        desired = self._desired_map()
        current = desired.get(server_id)
        allowlist = set(current.allowed_tools) if current is not None else set()
        try:
            listed = await self._host.discover(server_id)
        except Exception as exc:  # noqa: BLE001 - reconnect failure is honest
            raise _fail("connection attempt failed") from exc
        tools = []
        for item in listed:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name:
                continue
            tools.append({"name": name, "allowed": name in allowlist})
            if len(tools) >= 128:
                break
        tools.sort(key=lambda item: item["name"])
        return {
            "status": "connected",
            "server_id": server_id,
            "transport": transport,
            "tools": tools,
        }

    # -- shutdown -------------------------------------------------------------------

    async def close(self) -> None:
        """Release ownership only after every retained transport is reaped."""
        async with self._lock:
            if self._closed:
                return
            self._runtime_blocked = True
            cancelled = False
            for temporary in list(self._uncertain_hosts):
                try:
                    await self._close_temporary(temporary)
                except asyncio.CancelledError:
                    cancelled = True
            cancelled = await _settle_cleanup(self._host.close()) or cancelled
            self._closed = True
            self._db.close()
            os.close(self._lock_fd)
            if cancelled:
                raise asyncio.CancelledError


class _Plan:
    """Validated intent for one control call. Built before any effect runs."""

    __slots__ = (
        "server_label",
        "config_canonical",
        "mutates_durable",
        "new_desired",
        "history_detail",
        "test_capture",
    )

    def __init__(
        self,
        *,
        server_label: str,
        config_canonical: str | None,
        mutates_durable: bool,
        new_desired: list[MCPConfig] | None,
        history_detail: str,
        test_capture: MCPConfig | None = None,
    ) -> None:
        self.server_label = server_label
        self.config_canonical = config_canonical
        self.mutates_durable = mutates_durable
        self.new_desired = new_desired
        self.history_detail = history_detail
        self.test_capture = test_capture
