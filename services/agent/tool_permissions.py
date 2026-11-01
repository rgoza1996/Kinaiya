"""Protected scoped native-operation rules and exact historical owner receipts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid

from packages.contracts.models import TERMINAL_STATES
from packages.contracts.tool_permissions import (
    MAX_PERMISSION_REVISION,
    NATIVE_PERMISSION_TOOLS,
    PermissionScope,
    ToolPermissionChange,
    ToolPermissionTargetChange,
)
from services.agent.target_permission_store import TargetPermissionStore
from services.settings.provider_catalog import _storage

MAX_RULES = 1024
MAX_RECEIPTS = 10000
MAX_SCOPE_BYTES = 2048
MAX_RECEIPT_BYTES = 8192
STORAGE_VERSION = 2
_LEGACY_TABLES = {
    "permission_state": "CREATE TABLE permission_state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)",
    "permission_rules": "CREATE TABLE permission_rules (scope_id TEXT NOT NULL, tool TEXT NOT NULL, scope TEXT NOT NULL, mode TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(scope_id,tool))",
    "permission_operations": "CREATE TABLE permission_operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL)",
}
_TABLES = {
    **_LEGACY_TABLES,
    "permission_target_rules": "CREATE TABLE permission_target_rules (scope_id TEXT NOT NULL, target_id TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL, binding TEXT NOT NULL, mode TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(scope_id,target_id))",
}
_LEGACY_INDEXES = {
    "sqlite_autoindex_permission_rules_1": "permission_rules",
    "sqlite_autoindex_permission_operations_1": "permission_operations",
}
_INDEXES = {
    **_LEGACY_INDEXES,
    "sqlite_autoindex_permission_target_rules_1": "permission_target_rules",
}


class ToolPermissionConflict(ValueError):
    pass


def _encode(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _digest(value):
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _decode(value, maximum):
    if type(value) is not str or len(value.encode("utf-8")) > maximum:
        raise ValueError("Permission metadata exceeds its native bound")

    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                raise ValueError("Duplicate permission metadata key")
            result[key] = child
        return result

    def invalid(_):
        raise ValueError("Invalid permission metadata number")

    try:
        decoded = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)
        pending, count = [(decoded, 0)], 0
        while pending:
            child, depth = pending.pop()
            count += 1
            if depth > 8 or count > 256:
                raise ValueError("Permission metadata exceeds its structural bound")
            if isinstance(child, dict):
                pending.extend((item, depth + 1) for item in child.values())
            elif isinstance(child, list):
                pending.extend((item, depth + 1) for item in child)
            elif type(child) is float:
                raise ValueError("Permission metadata requires exact integers")
            elif isinstance(child, str):
                child.encode("utf-8")
        return decoded
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("Permission metadata is unavailable") from exc


def _scope_value(scope):
    return scope.model_dump(exclude_none=True)


class AgentToolPermissions(TargetPermissionStore):
    def __init__(self, runtime, path=None):
        self.runtime = runtime
        self.instance = str(uuid.uuid4())
        self._closed = False
        if path is None:
            self.db, created = sqlite3.connect(":memory:", timeout=0.5), True
        else:
            self.db, created = _storage(path, runtime.policy.workspace, report_created=True)
        try:
            self.db.execute("PRAGMA busy_timeout=500")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            if created:
                if (
                    self.db.execute("PRAGMA user_version").fetchone() != (0,)
                    or self.db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is not None
                ):
                    raise ValueError("New permission storage is not empty")
                for statement in _TABLES.values():
                    self.db.execute(statement)
                self.db.execute("INSERT INTO permission_state VALUES (1,0)")
                self.db.execute(f"PRAGMA user_version={STORAGE_VERSION}")
            elif self.db.execute("PRAGMA user_version").fetchone() in {(0,), (1,)}:
                # Upgrade only complete original authority. In particular, the
                # unversioned pre-release four-table layout is not a legacy DB.
                self._schema(legacy=True)
                self._validate_storage(legacy=True)
                self.db.execute(_TABLES["permission_target_rules"])
                self.db.execute(f"PRAGMA user_version={STORAGE_VERSION}")
            self._schema()
            self._validate_storage()
            self.db.commit()
            self._revision()
        except BaseException:
            self._closed = True
            self.db.close()
            raise

    def _schema(self, *, legacy=False):
        if self._closed:
            raise ValueError("Permission storage is closed")
        version = self.db.execute("PRAGMA user_version").fetchone()
        permitted = {(0,), (1,)} if legacy else {(STORAGE_VERSION,)}
        if version not in permitted:
            raise ValueError("Permission storage version is unavailable")
        rows = self.db.execute(
            "SELECT type, CASE WHEN length(CAST(name AS BLOB))<=128 THEN name END,"
            "CASE WHEN length(CAST(tbl_name AS BLOB))<=128 THEN tbl_name END,"
            "CASE WHEN length(CAST(sql AS BLOB))<=4096 THEN sql END "
            "FROM sqlite_schema LIMIT 8"
        ).fetchall()
        tables = _LEGACY_TABLES if legacy else _TABLES
        indexes = _LEGACY_INDEXES if legacy else _INDEXES
        expected = {("table", name) for name in tables} | {("index", name) for name in indexes}
        if len(rows) != len(expected) or {(row[0], row[1]) for row in rows} != expected:
            raise ValueError("Permission storage schema is unavailable")
        for kind, name, table, sql in rows:
            if kind == "index":
                if table != indexes[name] or sql is not None:
                    raise ValueError("Permission storage index is unavailable")
            elif (
                table != name
                or type(sql) is not str
                or re.sub(r"\s+", "", sql)
                .lower()
                .replace("createtableifnotexists", "createtable", 1)
                != re.sub(r"\s+", "", tables[name]).lower()
            ):
                raise ValueError("Permission storage table is unavailable")

    def _stored_revision(self):
        rows = self.db.execute(
            "SELECT id, CASE WHEN typeof(revision)='integer' THEN revision END "
            "FROM permission_state LIMIT 2"
        ).fetchall()
        if (
            len(rows) != 1
            or rows[0][0] != 1
            or type(rows[0][1]) is not int
            or not 0 <= rows[0][1] <= MAX_PERMISSION_REVISION
        ):
            raise ValueError("Permission revision is unavailable")
        return rows[0][1]

    def _validate_storage(self, *, legacy=False):
        # Every native mutation retains one receipt and increments exactly once;
        # checking their full bounded history detects incomplete legacy stores.
        revision = self._stored_revision()
        rules = {}
        for identity, tool, encoded, mode, rule_revision in self.db.execute(
            "SELECT CASE WHEN typeof(scope_id)='text' AND length(CAST(scope_id AS BLOB))=64 THEN scope_id END,"
            "CASE WHEN typeof(tool)='text' AND length(CAST(tool AS BLOB))<=10 THEN tool END,"
            f"CASE WHEN typeof(scope)='text' AND length(CAST(scope AS BLOB))<={MAX_SCOPE_BYTES} THEN scope END,"
            "CASE WHEN typeof(mode)='text' AND length(CAST(mode AS BLOB))<=5 THEN mode END,"
            "CASE WHEN typeof(revision)='integer' THEN revision END "
            f"FROM permission_rules LIMIT {MAX_RULES + 1}"
        ):
            scope = PermissionScope.model_validate(_decode(encoded, MAX_SCOPE_BYTES))
            if (
                len(rules) >= MAX_RULES
                or identity != self.scope_id(scope)
                or tool not in NATIVE_PERMISSION_TOOLS
                or mode not in {"allow", "ask", "deny"}
                or type(rule_revision) is not int
                or not 1 <= rule_revision <= revision
            ):
                raise ValueError("Stored permission rule is unavailable")
            rules[("1.0", identity, tool)] = (_scope_value(scope), mode, rule_revision)
        if not legacy:
            rules.update(self._stored_target_rules(revision, len(rules)))
        operations, revisions, latest = 0, set(), {}
        for key, digest, encoded in self.db.execute(
            "SELECT CASE WHEN typeof(key)='text' AND length(CAST(key AS BLOB))<=128 THEN key END,"
            "CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 THEN digest END,"
            f"CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<={MAX_RECEIPT_BYTES} THEN receipt END "
            f"FROM permission_operations LIMIT {MAX_RECEIPTS + 1}"
        ):
            operations += 1
            receipt = self._receipt_value(encoded, key)
            version = receipt["schema_version"]
            if legacy and version != "1.0":
                raise ValueError("Legacy permission history contains newer authority")
            request = (
                ToolPermissionChange if version == "1.0" else ToolPermissionTargetChange
            ).model_validate(
                {
                    field: child
                    for field, child in receipt.items()
                    if field not in {"status", "scope_id", "revision", "change_boundary"}
                }
            )
            operation_revision = receipt["revision"]
            if (
                operations > MAX_RECEIPTS
                or operation_revision > revision
                or operation_revision in revisions
                or digest != _digest(self._request_value(request))
            ):
                raise ValueError("Stored permission operation is unavailable")
            revisions.add(operation_revision)
            if version == "1.0":
                identity = (version, receipt["scope_id"], receipt["tool"])
                value = (receipt["scope"], receipt["mode"], operation_revision)
            else:
                from services.agent.permission_targets import target_id

                identity = (version, receipt["scope_id"], target_id(receipt["target"]))
                value = (
                    receipt["scope"],
                    receipt["target"],
                    receipt["binding"],
                    receipt["mode"],
                    operation_revision,
                )
            current = latest.get(identity)
            if current is None or current[-1] < operation_revision:
                latest[identity] = value
        if operations != revision or rules != {
            identity: value for identity, value in latest.items() if value[-2] is not None
        }:
            raise ValueError("Permission storage history is incomplete")

    @property
    def workspace_id(self):
        return _digest({"workspace": str(self.runtime.policy.workspace.resolve())})

    @staticmethod
    def scope_id(scope):
        return _digest(_scope_value(scope))

    def _revision(self):
        self._schema()
        return self._stored_revision()

    def _host(self, tool):
        policy = self.runtime.policy
        # The policy's action methods may consult this resolver. Host ceilings
        # must therefore use the underlying owner configuration directly.
        if tool in {"read_file", "search"}:
            return "allow", False
        denied = policy.deny_write if tool == "write_file" else policy.deny_shell
        allowed = policy.allow_write if tool == "write_file" else policy.allow_shell
        if any(type(value) is not bool for value in (denied, allowed, policy.yolo)):
            raise ValueError("Native host permission configuration is unavailable")
        mode = "allow" if allowed or policy.yolo else "ask"
        return ("deny" if denied else mode), denied

    def fingerprint(self, task=None):
        return _digest(
            {
                "instance": self.instance,
                "revision": self._revision(),
                "workspace_id": self.workspace_id,
                "host": {tool: self._host(tool) for tool in NATIVE_PERMISSION_TOOLS},
                "network": self.runtime.policy.network,
                "yolo": self.runtime.policy.yolo,
                "shell_directory": self.runtime.policy.shell_working_directory,
                "shell_timeout_s": self.runtime.policy.shell_timeout_s,
                "protected": [str(path) for path in self.runtime.policy.protected_roots],
                "readonly": [str(path) for path in self.runtime.policy.readonly_roots],
            }
        )

    def _scope_for_task(self, task):
        return PermissionScope(
            kind="task",
            workspace_id=self.workspace_id,
            session_id=task.session_id,
            thread_id=task.thread_id,
            epoch=task.epoch,
            task_id=task.task_id,
            objective_revision=task.objective_revision,
        )

    def _lineage(self, scope):
        value = _scope_value(scope)
        result = [scope]
        if scope.kind == "task":
            result.append(
                PermissionScope(
                    **{
                        key: child
                        for key, child in value.items()
                        if key not in {"kind", "task_id", "objective_revision"}
                    },
                    kind="session",
                )
            )
        if scope.kind in {"task", "session"}:
            result.append(PermissionScope(kind="workspace", workspace_id=scope.workspace_id))
        if scope.kind != "default":
            result.append(PermissionScope(kind="default"))
        return result

    def _rule(self, scope, tool):
        self._revision()
        identity = self.scope_id(scope)
        row = self.db.execute(
            "SELECT CASE WHEN length(CAST(scope AS BLOB))<=? THEN scope END,"
            "CASE WHEN length(CAST(mode AS BLOB))<=5 THEN mode END,"
            "CASE WHEN typeof(revision)='integer' THEN revision END "
            "FROM permission_rules WHERE scope_id=? AND tool=?",
            (MAX_SCOPE_BYTES, identity, tool),
        ).fetchone()
        if row is None:
            return None
        stored = PermissionScope.model_validate(_decode(row[0], MAX_SCOPE_BYTES))
        if (
            stored != scope
            or row[1] not in {"allow", "ask", "deny"}
            or type(row[2]) is not int
            or not 1 <= row[2] <= self._revision()
        ):
            raise ValueError("Permission rule is unavailable")
        return {
            "mode": row[1],
            "revision": row[2],
            "rule_id": _digest({"scope_id": identity, "tool": tool}),
        }

    def _resolve(self, scope, tool):
        if tool not in NATIVE_PERMISSION_TOOLS:
            raise ValueError("Unsupported native permission target")
        host, denied = self._host(tool)
        override = self._rule(scope, tool)
        result = {
            "tool": tool,
            "override": override["mode"] if override else None,
            "effective": host,
            "source": "host",
            "source_scope_id": None,
            "rule_id": None,
            "rule_revision": None,
            "hard_denied": denied,
            "host_mode": host,
        }
        if not denied:
            for source in self._lineage(scope):
                rule = self._rule(source, tool)
                if rule is not None:
                    result.update(
                        effective=rule["mode"],
                        source=source.kind,
                        source_scope_id=self.scope_id(source),
                        rule_id=rule["rule_id"],
                        rule_revision=rule["revision"],
                    )
                    break
        return result

    def resolve(self, task, tool):
        if tool == "search":
            return self.resolve_target(task, {"kind": "native", "name": tool})
        scope = (
            self._scope_for_task(task)
            if task is not None
            else PermissionScope(kind="workspace", workspace_id=self.workspace_id)
        )
        return self._resolve(scope, tool)

    def _native_task(self, scope):
        rec = self.runtime._tasks.get(scope.task_id)
        if (
            rec is None
            or rec.recovered
            or rec.closing
            or rec.cancel_requested
            or rec.completion_pending
            or rec.task.state in TERMINAL_STATES
            or not rec.submit_confirmed
        ):
            return None
        return rec if self._scope_for_task(rec.task) == scope else None

    async def _live(self, scope):
        if self._closed or self.runtime._closed:
            return False
        if scope.kind == "default":
            return True
        if scope.workspace_id != self.workspace_id:
            return False
        if scope.kind == "workspace":
            return True
        rec = self._native_task(scope) if scope.kind == "task" else None
        if scope.kind == "task" and rec is None:
            return False
        try:
            session = await self.runtime.journal.session(scope.session_id)
        except KeyError:
            return False
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                return False
            raise RuntimeError("Native permission scope authority is unavailable") from None
        if (
            session.channel not in {"agent", "converse"}
            or scope.workspace_id != self.workspace_id
            or (session.session_id, session.thread_id, session.epoch)
            != (scope.session_id, scope.thread_id, scope.epoch)
        ):
            return False
        return scope.kind != "task" or self._native_task(scope) is rec

    async def select(self, kind="default", session_id=None, task_id=None, scope_id=None):
        for identity in (session_id, task_id):
            if identity is not None and (
                type(identity) is not str or not 1 <= len(identity) <= 128 or "\0" in identity
            ):
                raise ValueError("Invalid native permission target identity")
        if scope_id is not None:
            if kind != "default" or session_id is not None or task_id is not None:
                raise ValueError("Saved permission scope cannot be combined with a target")
            scope = self._saved_scope(scope_id)
        elif kind == "default":
            if session_id is not None or task_id is not None:
                raise ValueError("Default permission scope has no target")
            scope = PermissionScope(kind="default")
        elif kind == "workspace":
            if session_id is not None or task_id is not None:
                raise ValueError("Workspace permission scope has no target")
            scope = PermissionScope(kind="workspace", workspace_id=self.workspace_id)
        elif kind == "task":
            rec = self.runtime._tasks.get(task_id)
            if rec is None or session_id is not None:
                raise ValueError("Native task permission scope is unavailable")
            scope = self._scope_for_task(rec.task)
        elif kind == "session":
            if task_id is not None or session_id is None:
                raise ValueError("Native session permission scope is unavailable")
            try:
                session = await self.runtime.journal.session(session_id)
            except KeyError:
                raise ValueError("Native session permission scope is unavailable") from None
            except Exception as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                    raise ValueError("Native session permission scope is unavailable") from None
                raise RuntimeError("Native session permission authority is unavailable") from None
            if session.channel not in {"agent", "converse"}:
                raise ValueError("Permission session is unavailable")
            scope = PermissionScope(
                kind="session",
                workspace_id=self.workspace_id,
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
            )
        else:
            raise ValueError("Unsupported permission scope")
        return await self.snapshot(scope)

    async def snapshot(self, scope):
        scope = PermissionScope.model_validate(scope)
        live = await self._live(scope)
        return {
            "schema_version": "1.0",
            "revision": self._revision(),
            "state_token": self.fingerprint(),
            "instance_id": self.instance,
            "scope": _scope_value(scope),
            "scope_id": self.scope_id(scope),
            "scope_live": live,
            "change_boundary": "next_action_admission",
            "tools": [self._resolve(scope, tool) for tool in NATIVE_PERMISSION_TOOLS],
            "limits": {"rules": MAX_RULES, "receipts": MAX_RECEIPTS},
        }

    def _saved_scope(self, identity):
        self._revision()
        if (
            type(identity) is not str
            or len(identity) != 64
            or any(char not in "0123456789abcdef" for char in identity)
        ):
            raise ValueError("Invalid saved permission scope")
        row = self.db.execute(
            "SELECT CASE WHEN length(CAST(scope AS BLOB))<=? THEN scope END FROM (SELECT scope_id,scope FROM permission_rules UNION ALL SELECT scope_id,scope FROM permission_target_rules) WHERE scope_id=? LIMIT 1",
            (MAX_SCOPE_BYTES, identity),
        ).fetchone()
        if row is None:
            raise ValueError("Saved permission scope is unavailable")
        scope = PermissionScope.model_validate(_decode(row[0], MAX_SCOPE_BYTES))
        if self.scope_id(scope) != identity:
            raise ValueError("Saved permission scope identity is invalid")
        return scope

    async def page_saved(self, after=None, limit=20):
        self._revision()
        if (
            type(limit) is not int
            or not 1 <= limit <= 20
            or (
                after is not None
                and (
                    type(after) is not str
                    or len(after) != 64
                    or any(char not in "0123456789abcdef" for char in after)
                )
            )
        ):
            raise ValueError("Invalid saved permission page")
        rows = self.db.execute(
            "SELECT DISTINCT CASE WHEN length(CAST(scope_id AS BLOB))=64 THEN scope_id END "
            "FROM (SELECT scope_id FROM permission_rules UNION ALL SELECT scope_id FROM permission_target_rules) WHERE scope_id>? ORDER BY scope_id LIMIT ?",
            (after or "", limit + 1),
        ).fetchall()
        items = [
            {"scope_id": row[0], "scope": _scope_value(self._saved_scope(row[0]))}
            for row in rows[:limit]
        ]
        return {
            "schema_version": "1.0",
            "scopes": items,
            "next_after": items[-1]["scope_id"] if len(rows) > limit else None,
        }

    def _request_value(self, request):
        return {**request.model_dump(), "scope": _scope_value(request.scope)}

    def receipt(self, key):
        self._revision()
        if (
            type(key) is not str
            or not 1 <= len(key) <= 128
            or any(
                char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:._-"
                for char in key
            )
        ):
            raise ValueError("Invalid permission receipt key")
        row = self.db.execute(
            "SELECT CASE WHEN length(CAST(receipt AS BLOB))<=? THEN receipt END FROM permission_operations WHERE key=?",
            (MAX_RECEIPT_BYTES, key),
        ).fetchone()
        if row is None:
            return None
        value = self._receipt_value(row[0], key)
        if value["revision"] > self._revision():
            raise ValueError("Permission receipt is ahead of native state")
        return value

    def _receipt_value(self, encoded, key):
        value = _decode(encoded, MAX_RECEIPT_BYTES)
        if type(value) is dict and value.get("schema_version") == "1.1":
            return self._target_receipt_value(value, key)
        expected = {
            "schema_version",
            "status",
            "idempotency_key",
            "scope",
            "scope_id",
            "tool",
            "mode",
            "expected_revision",
            "expected_state",
            "revision",
            "change_boundary",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("Permission receipt is invalid")
        scope = PermissionScope.model_validate(value["scope"])
        if (
            value["schema_version"] != "1.0"
            or value["status"] != "permission_saved"
            or value["idempotency_key"] != key
            or value["scope_id"] != self.scope_id(scope)
            or value["tool"] not in NATIVE_PERMISSION_TOOLS
            or value["mode"] not in (None, "allow", "ask", "deny")
            or type(value["expected_revision"]) is not int
            or type(value["expected_state"]) is not str
            or len(value["expected_state"]) != 64
            or any(char not in "0123456789abcdef" for char in value["expected_state"])
            or type(value["revision"]) is not int
            or not 0 <= value["expected_revision"] < MAX_PERMISSION_REVISION
            or value["revision"] != value["expected_revision"] + 1
            or value["change_boundary"] != "next_action_admission"
        ):
            raise ValueError("Permission receipt binding is invalid")
        return value

    def _prior(self, request, digest):
        self._revision()
        row = self.db.execute(
            "SELECT CASE WHEN length(CAST(digest AS BLOB))=64 THEN digest END,"
            "CASE WHEN length(CAST(receipt AS BLOB))<=? THEN receipt END "
            "FROM permission_operations WHERE key=?",
            (MAX_RECEIPT_BYTES, request.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise ToolPermissionConflict("Permission request key belongs to a different change")
        value = self._receipt_value(row[1], request.idempotency_key)
        if (
            value["scope"] != _scope_value(request.scope)
            or value["tool"] != request.tool
            or value["mode"] != request.mode
            or value["expected_revision"] != request.expected_revision
            or value["expected_state"] != request.expected_state
            or value["revision"] > self._revision()
        ):
            raise ValueError("Permission receipt does not match its original change")
        return value

    async def change(self, request):
        request = ToolPermissionChange.model_validate(request)
        digest = _digest(self._request_value(request))
        previous = self._prior(request, digest)
        if previous is not None:
            return previous
        identity = self.scope_id(request.scope)
        saved_reset = (
            request.mode is None
            and self.db.execute(
                "SELECT 1 FROM permission_rules WHERE scope_id=? LIMIT 1", (identity,)
            ).fetchone()
            is not None
        )
        live = True if saved_reset else await self._live(request.scope)
        if not live:
            raise ToolPermissionConflict("Permission scope is retired or unavailable")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self._prior(request, digest)
            if previous is not None:
                self.db.rollback()
                return previous
            if (
                self._closed
                or self.runtime._closed
                or (
                    request.scope.kind == "task"
                    and not saved_reset
                    and self._native_task(request.scope) is None
                )
            ):
                raise ToolPermissionConflict("Native permission scope changed before saving")
            revision = self._revision()
            if (
                revision != request.expected_revision
                or self.fingerprint() != request.expected_state
            ):
                raise ToolPermissionConflict("Permission settings changed; refresh before saving")
            if revision >= MAX_PERMISSION_REVISION:
                raise ValueError("Permission revision space is exhausted")
            if (
                self.db.execute("SELECT count(*) FROM permission_operations").fetchone()[0]
                >= MAX_RECEIPTS
            ):
                raise ValueError("Permission receipt capacity requires owner maintenance")
            if request.mode is None:
                self.db.execute(
                    "DELETE FROM permission_rules WHERE scope_id=? AND tool=?",
                    (identity, request.tool),
                )
            else:
                exists = self.db.execute(
                    "SELECT 1 FROM permission_rules WHERE scope_id=? AND tool=?",
                    (identity, request.tool),
                ).fetchone()
                if exists is None and self._rule_count() >= MAX_RULES:
                    raise ValueError("Permission rule capacity requires owner reset")
                self.db.execute(
                    "INSERT INTO permission_rules VALUES (?,?,?,?,?) ON CONFLICT(scope_id,tool) DO UPDATE SET scope=excluded.scope,mode=excluded.mode,revision=excluded.revision",
                    (
                        identity,
                        request.tool,
                        _encode(_scope_value(request.scope)),
                        request.mode,
                        revision + 1,
                    ),
                )
            result = {
                "schema_version": "1.0",
                "status": "permission_saved",
                "idempotency_key": request.idempotency_key,
                "scope": _scope_value(request.scope),
                "scope_id": identity,
                "tool": request.tool,
                "mode": request.mode,
                "expected_revision": revision,
                "expected_state": request.expected_state,
                "revision": revision + 1,
                "change_boundary": "next_action_admission",
            }
            encoded = _encode(result)
            self._receipt_value(encoded, request.idempotency_key)
            self.db.execute("UPDATE permission_state SET revision=? WHERE id=1", (revision + 1,))
            self.db.execute(
                "INSERT INTO permission_operations VALUES (?,?,?)",
                (request.idempotency_key, digest, encoded),
            )
            self.db.commit()
            return result
        except BaseException:
            try:
                self.db.rollback()
            except sqlite3.Error:
                self._closed = True
                self.db.close()
            raise

    def close(self):
        self._closed = True
        self.db.close()
