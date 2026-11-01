"""Additive exact-target rules sharing legacy native CAS and receipt ownership."""

import sqlite3

from packages.contracts.tool_permissions import (
    MAX_PERMISSION_REVISION,
    PermissionScope,
    ToolPermissionTargetChange,
)
from services.agent.permission_targets import registry, target_id


class TargetPermissionStore:
    def _rule_count(self):
        return self.db.execute(
            "SELECT (SELECT count(*) FROM permission_rules)+(SELECT count(*) FROM permission_target_rules)"
        ).fetchone()[0]

    def _target_rule(self, scope, target):
        from services.agent.tool_permissions import _decode, _digest

        sid, tid = self.scope_id(scope), target_id(target)
        row = self.db.execute(
            "SELECT CASE WHEN length(CAST(scope AS BLOB))<=2048 THEN scope END,"
            "CASE WHEN length(CAST(target AS BLOB))<=512 THEN target END,"
            "CASE WHEN length(CAST(binding AS BLOB))<=512 THEN binding END,"
            "CASE WHEN length(CAST(mode AS BLOB))<=5 THEN mode END,"
            "CASE WHEN typeof(revision)='integer' THEN revision END "
            "FROM permission_target_rules WHERE scope_id=? AND target_id=?",
            (sid, tid),
        ).fetchone()
        if row is None:
            return None
        value = {
            "schema_version": "1.1",
            "expected_revision": 0,
            "expected_state": "0" * 64,
            "idempotency_key": "stored",
            "scope": _decode(row[0], 2048),
            "target": _decode(row[1], 512),
            "binding": _decode(row[2], 512),
            "mode": row[3],
        }
        checked = ToolPermissionTargetChange.model_validate(value)
        if (
            checked.scope != scope
            or checked.target.model_dump() != target
            or checked.mode is None
            or type(row[4]) is not int
            or not 1 <= row[4] <= self._revision()
        ):
            raise ValueError("Permission target rule is unavailable")
        return {
            "mode": checked.mode,
            "binding": checked.binding.model_dump(),
            "revision": row[4],
            "rule_id": _digest({"scope_id": sid, "target": target}),
        }

    def _resolve_target(self, scope, target, inventory=None):
        from packages.contracts.tool_permissions import NATIVE_PERMISSION_TOOLS

        inventory = registry(self.runtime, target) if inventory is None else inventory
        tid = target_id(target)
        item = inventory.get(tid)
        exact = self._target_rule(scope, target)
        winner, source = None, None
        for candidate in self._lineage(scope):
            winner = self._target_rule(candidate, target)
            if winner is not None:
                source = candidate
                break
        if item is None:
            item = {
                "target": target,
                "binding": None,
                "available": False,
                "editable": True,
                "rule_api": "target",
                "host_mode": "deny",
                "hard_denied": True,
                "reason": "Saved target is no longer configured",
            }
        result = {
            **item,
            "target_id": tid,
            "stored_binding": winner["binding"] if winner else None,
            "binding_state": "current",
            "override": exact["mode"] if exact else None,
            "effective": item["host_mode"],
            "source": "host",
            "source_scope_id": None,
            "rule_id": None,
            "rule_revision": None,
        }
        if target["kind"] == "native" and target["name"] in NATIVE_PERMISSION_TOOLS:
            result.update(self._resolve(scope, target["name"]))
            result.pop("tool")
            return result
        if winner:
            result.update(
                effective=winner["mode"],
                source=source.kind,
                source_scope_id=self.scope_id(source),
                rule_id=winner["rule_id"],
                rule_revision=winner["revision"],
            )
        binding = item["binding"]
        if binding is None or (target["kind"] == "mcp" and binding["tool_sha256"] is None):
            result["binding_state"] = "unavailable"
        elif winner and winner["binding"] != binding:
            result["binding_state"] = "changed"
        if item["hard_denied"] or not item["available"] or result["binding_state"] != "current":
            result["effective"] = "deny"
        return result

    def resolve_target(self, task, target):
        scope = (
            self._scope_for_task(task)
            if task is not None
            else PermissionScope(kind="workspace", workspace_id=self.workspace_id)
        )
        return self._resolve_target(scope, target)

    def _saved_targets(self):
        from services.agent.tool_permissions import _decode

        rows = self.db.execute(
            "SELECT DISTINCT target_id,CASE WHEN length(CAST(target AS BLOB))<=512 THEN target END "
            "FROM permission_target_rules LIMIT 1025"
        ).fetchall()
        if len(rows) > 1024:
            raise ValueError("Saved permission target bound exceeded")
        result = {}
        for identity, encoded in rows:
            value = _decode(encoded, 512)
            if target_id(value) != identity:
                raise ValueError("Saved target identity is invalid")
            result[identity] = value
        return result

    async def select_targets(
        self, kind="default", session_id=None, task_id=None, scope_id=None, after=None, limit=20
    ):
        snapshot = await self.select(kind, session_id, task_id, scope_id)
        return await self.target_snapshot(
            PermissionScope.model_validate(snapshot["scope"]), after, limit
        )

    async def target_snapshot(self, scope, after=None, limit=20):
        if (
            type(limit) is not int
            or not 1 <= limit <= 20
            or (
                after is not None
                and (
                    type(after) is not str
                    or len(after) != 64
                    or any(c not in "0123456789abcdef" for c in after)
                )
            )
        ):
            raise ValueError("Invalid permission target page")
        snapshot = await self.snapshot(scope)
        inventory = registry(self.runtime)
        targets = {key: value["target"] for key, value in inventory.items()}
        targets.update(self._saved_targets())
        identities = sorted(key for key in targets if key > (after or ""))
        selected = identities[:limit]
        snapshot.pop("tools")
        return {
            **snapshot,
            "schema_version": "1.1",
            "targets": [self._resolve_target(scope, targets[key], inventory) for key in selected],
            "next_after": selected[-1] if len(identities) > limit else None,
        }

    def _target_receipt_value(self, value, key):
        expected = {
            "schema_version",
            "status",
            "idempotency_key",
            "scope",
            "scope_id",
            "target",
            "binding",
            "mode",
            "expected_revision",
            "expected_state",
            "revision",
            "change_boundary",
        }
        if set(value) != expected:
            raise ValueError("Permission target receipt fields are invalid")
        request = ToolPermissionTargetChange.model_validate(
            {
                key: child
                for key, child in value.items()
                if key not in {"status", "scope_id", "revision", "change_boundary"}
            }
        )
        if (
            value["status"] != "permission_target_saved"
            or request.idempotency_key != key
            or value["scope_id"] != self.scope_id(request.scope)
            or type(value["revision"]) is not int
            or value["revision"] != request.expected_revision + 1
            or value["change_boundary"] != "next_action_admission"
        ):
            raise ValueError("Permission target receipt binding is invalid")
        return value

    def _prior_target(self, request, digest):
        from services.agent.tool_permissions import MAX_RECEIPT_BYTES, ToolPermissionConflict

        row = self.db.execute(
            "SELECT CASE WHEN length(CAST(digest AS BLOB))=64 THEN digest END,"
            "CASE WHEN length(CAST(receipt AS BLOB))<=? THEN receipt END FROM permission_operations WHERE key=?",
            (MAX_RECEIPT_BYTES, request.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise ToolPermissionConflict("Permission key belongs to another exact change")
        value = self._receipt_value(row[1], request.idempotency_key)
        original = {
            key: child
            for key, child in value.items()
            if key not in {"status", "scope_id", "revision", "change_boundary"}
        }
        if original != self._request_value(request) or value["revision"] > self._revision():
            raise ValueError("Permission target receipt does not match its exact request")
        return value

    async def change_target(self, request):
        from services.agent.tool_permissions import (
            MAX_RECEIPTS,
            MAX_RULES,
            ToolPermissionConflict,
            _digest,
            _encode,
            _scope_value,
        )

        request = ToolPermissionTargetChange.model_validate(request)
        value = self._request_value(request)
        digest = _digest(value)
        prior = self._prior_target(request, digest)
        if prior is not None:
            return prior
        target, binding = request.target.model_dump(), request.binding.model_dump()
        if target["kind"] == "native" and target["name"] != "search":
            raise ValueError("This native operation does not use the target change protocol")
        sid, tid = self.scope_id(request.scope), target_id(target)
        saved_reset = request.mode is None and self._target_rule(request.scope, target) is not None
        if not saved_reset and not await self._live(request.scope):
            raise ToolPermissionConflict("Permission scope is retired or unavailable")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self._prior_target(request, digest)
            if prior is not None:
                self.db.rollback()
                return prior
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
                    "DELETE FROM permission_target_rules WHERE scope_id=? AND target_id=?",
                    (sid, tid),
                )
            else:
                item = registry(self.runtime, target).get(tid)
                if (
                    item is None
                    or item["rule_api"] != "target"
                    or item["binding"] != binding
                    or (request.mode != "deny" and not item["available"])
                ):
                    raise ToolPermissionConflict(
                        "Target binding is unavailable; inspect current metadata"
                    )
                exists = self._target_rule(request.scope, target)
                if exists is None and self._rule_count() >= MAX_RULES:
                    raise ValueError("Permission rule capacity requires owner reset")
                self.db.execute(
                    "INSERT INTO permission_target_rules VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(scope_id,target_id) DO UPDATE SET scope=excluded.scope,target=excluded.target,"
                    "binding=excluded.binding,mode=excluded.mode,revision=excluded.revision",
                    (
                        sid,
                        tid,
                        _encode(_scope_value(request.scope)),
                        _encode(target),
                        _encode(binding),
                        request.mode,
                        revision + 1,
                    ),
                )
            result = {
                **value,
                "status": "permission_target_saved",
                "scope_id": sid,
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
