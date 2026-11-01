"""Protected scoped native-operation rules and exact historical owner receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from packages.contracts.models import TERMINAL_STATES
from packages.contracts.tool_permissions import (
    MAX_PERMISSION_REVISION,
    NATIVE_PERMISSION_TOOLS,
    PermissionScope,
    ToolPermissionChange,
)
from services.agent.target_permission_store import TargetPermissionStore

MAX_RULES = 1024
MAX_RECEIPTS = 10000
MAX_SCOPE_BYTES = 2048
MAX_RECEIPT_BYTES = 8192


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
        if path is not None:
            path = Path(path).resolve()
            if path.is_relative_to(runtime.policy.workspace):
                raise ValueError("Permission settings must be outside the worker workspace")
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path is not None else ":memory:", timeout=0.5)
        if path is not None:
            path.chmod(0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS permission_state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS permission_rules (scope_id TEXT NOT NULL, tool TEXT NOT NULL, scope TEXT NOT NULL, mode TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(scope_id,tool))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS permission_operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS permission_target_rules (scope_id TEXT NOT NULL, target_id TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL, binding TEXT NOT NULL, mode TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(scope_id,target_id))"
        )
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO permission_state VALUES (1,0)")
        self._revision()

    @property
    def workspace_id(self):
        return _digest({"workspace": str(self.runtime.policy.workspace.resolve())})

    @staticmethod
    def scope_id(scope):
        return _digest(_scope_value(scope))

    def _revision(self):
        row = self.db.execute(
            "SELECT CASE WHEN typeof(revision)='integer' THEN revision END FROM permission_state WHERE id=1"
        ).fetchone()
        if row is None or type(row[0]) is not int or not 0 <= row[0] <= MAX_PERMISSION_REVISION:
            raise ValueError("Permission revision is unavailable")
        return row[0]

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
