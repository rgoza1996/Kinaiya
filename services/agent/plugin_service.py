"""Trusted owner controls and model-scoped access to isolated compute plugins.

Candidates never select their evaluator, disclosure scope or activation authority.
The optional host is imported only when plugins are explicitly configured.
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path

from core.plugins import PluginError, PluginRegistry
from core.plugins.storage import read_bytes

SCOPES = {"local_only", "public_synthetic", "private_cloud_authorized"}
PLUGIN_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _atomic(path, value):
    fd, name = tempfile.mkstemp(prefix=".scope-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(_json(value))
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


async def _reap(worker):
    """Repeated cancellation cannot release admission before the child exits."""
    while True:
        try:
            await asyncio.shield(worker)
            return
        except asyncio.CancelledError:
            if worker.cancelled():
                raise
        except Exception:
            return


class PluginService:
    def __init__(self, root: Path, workspace: Path, suites: dict[str, Path]):
        self.root = root.absolute()
        self.workspace = workspace.resolve()
        if self.root.is_relative_to(self.workspace) or self.workspace.is_relative_to(self.root):
            raise ValueError("Plugin control root must be disjoint from the worker workspace")
        for plugin_id, path in suites.items():
            if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
                raise ValueError("Invalid configured plugin id")
            if path.resolve().is_relative_to(self.workspace):
                raise ValueError("Plugin acceptance suites must be outside the worker workspace")
        self.registry = PluginRegistry(self.root, suites)
        self.scope_file = self.root / "model-scopes.json"
        self.scope_pending = self.root / "scope-change.pending"
        self._pins = {}
        self._lock = asyncio.Lock()
        self._execution = asyncio.Semaphore(1)
        path = self.root / "owner-controls.sqlite"
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                    raise PluginError("Unsafe plugin control database")
            finally:
                os.close(fd)
            self.controls = sqlite3.connect(path)
            self.controls.execute("PRAGMA synchronous=FULL")
            self.controls.execute(
                "CREATE TABLE IF NOT EXISTS controls "
                "(key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT)"
            )
            self.controls.commit()
        except BaseException:
            if hasattr(self, "controls"):
                self.controls.close()
            self.registry.close()
            raise

    async def owner_control(self, key, payload, operation):
        """Persist intent before effects; uncertain operations never replay."""
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        # No awaits between lookup and reservation. Other controls (notably
        # disable) stay responsive while an evaluation or computation runs.
        row = self.controls.execute(
            "SELECT digest,result FROM controls WHERE key=?", (key,)
        ).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError("Plugin control idempotency key conflicts with earlier input")
            if row[1] is None:
                raise ValueError("Plugin control outcome is uncertain; inspect before retrying")
            saved = json.loads(row[1])
        else:
            if self.controls.execute("SELECT count(*) FROM controls").fetchone()[0] >= 2048:
                raise ValueError("Plugin control history bound reached")
            with self.controls:
                self.controls.execute("INSERT INTO controls VALUES (?,?,NULL)", (key, digest))
            try:
                saved = {"ok": True, "result": await operation()}
            except Exception as exc:
                # An exception may follow a durable pointer write. Retain the
                # outcome and require inspection rather than repeating effects.
                saved = {"ok": False, "error": str(exc)[:1000]}
            with self.controls:
                self.controls.execute(
                    "UPDATE controls SET result=? WHERE key=?", (_json(saved), key)
                )
        if not saved["ok"]:
            raise ValueError(saved["error"])
        return saved["result"]

    def inspect(self, version_id):
        row = self.registry.inspect(version_id)
        manifest = row.get("manifest")
        if not isinstance(manifest, dict) or not PLUGIN_ID.fullmatch(manifest.get("plugin_id", "")):
            raise PluginError("Plugin metadata is incomplete")
        return {
            "version_id": version_id,
            "source_sha256": row["source_sha256"],
            "manifest": manifest,
            "content_scope": self._scopes().get(version_id, "local_only"),
        }

    def _scopes(self):
        if not self.scope_file.exists():
            return {}
        data = read_bytes(self.scope_file, 65_536)
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or len(value) > 128
            or any(not isinstance(k, str) or v not in SCOPES for k, v in value.items())
        ):
            raise PluginError("Plugin disclosure configuration is invalid")
        return value

    def _check_disclosure(self, version_id, provider_info):
        if type(provider_info.local) is not bool:
            raise PluginError("Verified provider metadata is required")
        if provider_info.local:
            return
        scope = self._scopes().get(version_id, "local_only")
        if self.scope_pending.exists():
            raise PluginError("Plugin disclosure change requires owner reconciliation")
        contributor = (
            provider_info.provider == "meta" or "contributor" in provider_info.model.lower()
        )
        if scope == "public_synthetic" or (not contributor and scope == "private_cloud_authorized"):
            return
        raise PluginError("Plugin is unavailable under this provider disclosure policy")

    def list_owner(self, after=0, limit=8):
        if (
            type(after) is not int
            or not 0 <= after <= 128
            or type(limit) is not int
            or not 1 <= limit <= 8
        ):
            raise ValueError("Plugin paging requires after 0..128 and limit 1..8")
        rows = self.registry.versions()
        selected = [self.inspect(row["version_id"]) for row in rows[after : after + limit]]
        return {
            "plugins": selected,
            "next_after": after + limit if after + limit < len(rows) else None,
        }

    async def catalog(self, provider_info, *, plugin_id=None, after=0):
        if type(after) is not int or not 0 <= after <= 128:
            raise ValueError("Plugin cursor must be an integer in 0..128")
        rows = self.registry.versions()
        allowed = []
        for row in rows:
            item = self.inspect(row["version_id"])
            identity = item["manifest"]["plugin_id"]
            if self.registry.active(identity) != item["version_id"]:
                continue
            try:
                self._check_disclosure(item["version_id"], provider_info)
            except PluginError:
                continue
            allowed.append(item)
        allowed.sort(key=lambda row: row["manifest"]["plugin_id"])
        if plugin_id is not None:
            matches = [row for row in allowed if row["manifest"]["plugin_id"] == plugin_id]
            if not matches:
                raise PluginError("Plugin is unavailable under this provider disclosure policy")
            result = matches[0]
            if len(_json(result)) > 12_000:
                raise PluginError("Plugin schema exceeds the model context profile")
            return result
        selected = allowed[after : after + 4]
        return {
            "plugins": [
                {
                    "plugin_id": row["manifest"]["plugin_id"],
                    "version_id": row["version_id"],
                    "description": row["manifest"].get("description", "")[:80],
                }
                for row in selected
            ],
            "next_after": after + 4 if after + 4 < len(allowed) else None,
        }

    async def pin_task(self, task, plugin_id, provider_info):
        if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id):
            raise ValueError("Invalid plugin id")
        async with self._lock:
            pins = self._pins.setdefault(task.task_id, {})
            if plugin_id in pins:
                version_id = pins[plugin_id]
                self._check_disclosure(version_id, provider_info)
                return version_id, False
            if len(task.plugin_versions) >= 16 and plugin_id not in task.plugin_versions:
                raise PluginError("A task can pin at most 16 plugins")
            version_id = task.plugin_versions.get(plugin_id) or self.registry.active(plugin_id)
            if version_id is None:
                raise PluginError("No approved active plugin for this operation")
            self._check_disclosure(version_id, provider_info)
            item = self.inspect(version_id)
            if item["manifest"]["plugin_id"] != plugin_id:
                raise PluginError("Task plugin pin does not match its identity")
            self.registry.pin(version_id, task_id=task.task_id)
            pins[plugin_id] = version_id
            changed = plugin_id not in task.plugin_versions
            task.plugin_versions[plugin_id] = version_id
            return version_id, changed

    async def validate_task(self, task, provider_info):
        for plugin_id in task.plugin_versions:
            await self.pin_task(task, plugin_id, provider_info)
            self.inspect(task.plugin_versions[plugin_id])

    async def call_task(self, task, plugin_id, arguments, provider_info):
        version_id, _ = await self.pin_task(task, plugin_id, provider_info)
        result = await self.run(version_id, arguments)
        self._check_disclosure(version_id, provider_info)
        if len(_json(result)) > 11_500:
            raise PluginError("Plugin output exceeds the model context profile")
        return result

    async def run(self, version_id, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("Plugin arguments must be an object")
        cancel = threading.Event()
        async with self._execution:
            worker = asyncio.create_task(
                asyncio.to_thread(self.registry.run, version_id, arguments, cancel_event=cancel)
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel.set()
                # Reap the isolated process before cancellation is acknowledged.
                await _reap(worker)
                raise

    async def evaluate(self, version_id):
        cancel = threading.Event()
        async with self._execution:
            worker = asyncio.create_task(
                asyncio.to_thread(self.registry.evaluate, version_id, cancel_event=cancel)
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancel.set()
                await _reap(worker)
                raise

    async def set_scope(self, version_id, scope, *, reviewed=False):
        if scope not in SCOPES or reviewed is not True:
            raise ValueError("Scope changes require an explicit reviewed owner choice")
        async with self._lock:
            self.inspect(version_id)
            scopes = self._scopes()
            scopes[version_id] = scope
            _atomic(self.scope_pending, {"version_id": version_id})
            _atomic(self.scope_file, scopes)
            self.scope_pending.unlink()
        return {"version_id": version_id, "content_scope": scope}

    async def release_task(self, task_id):
        async with self._lock:
            for version_id in self._pins.pop(task_id, {}).values():
                self.registry.unpin(version_id, task_id=task_id)

    async def recover_pins(self, tasks):
        async with self._lock:
            self.registry.reconcile_task_pins(tasks)
            self._pins = {task_id: dict(versions) for task_id, versions in tasks.items()}

    async def close(self):
        for task_id in list(self._pins):
            await self.release_task(task_id)
        self.registry.close()
        self.controls.close()
