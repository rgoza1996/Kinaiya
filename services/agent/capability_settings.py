"""Protected native availability and intent-before-effect extension activation."""

import asyncio
import fcntl
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from packages.contracts.agent_tools import NATIVE_TOOL_ORDER
from packages.contracts.capability_settings import (
    MAX_REVISION,
    CapabilityChange,
    ToolAvailabilityAction,
    action_section,
)
from services.agent.capability_inventory import digest, encode, inventory
from services.settings.provider_catalog import _storage

RECEIPT_LIMIT = 10000
BOUNDARY = "native_owner_admission"


class CapabilityConflict(ValueError):
    pass


class CapabilityUnconfirmed(RuntimeError):
    pass


def decode(raw, maximum=16384):
    if type(raw) is not str or len(raw.encode("utf-8")) > maximum:
        raise ValueError("Capability metadata exceeds its bound")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate capability field")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
    )


@contextmanager
def _opening(path, workspace):
    if path is None:
        yield sqlite3.connect(":memory:"), True, None
        return
    path = Path(os.path.abspath(path))
    if (
        workspace is None
        or path != path.resolve()
        or path.is_relative_to(Path(workspace).resolve())
    ):
        raise ValueError("Capability storage must be protected outside the workspace")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CapabilityUnconfirmed("Capability startup is busy") from None
                time.sleep(0.01)
        actual, opened = path.parent.stat(), os.fstat(directory)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Capability parent identity changed")
        db, created = _storage(path, workspace, report_created=True)
        yield db, created, path
    finally:
        os.close(directory)


class CapabilitySettings:
    def __init__(self, runtime, path=None, *, workspace=None):
        self.runtime, self.instance_id = runtime, secrets.token_hex(16)
        self.backend_id = None
        self.closed, self._failed = False, False
        self._owner_fd = None
        self._path = None
        self.operations = set()
        self.actual_operations = set()
        self.native_workers = set()
        try:
            with _opening(path, workspace) as (self.db, created, actual_path):
                if actual_path is not None:
                    self._path = actual_path
                    self._owner_fd = os.open(
                        actual_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                    )
                    fd, actual = os.fstat(self._owner_fd), actual_path.stat(follow_symlinks=False)
                    if (
                        not stat.S_ISREG(fd.st_mode)
                        or fd.st_uid != os.geteuid()
                        or fd.st_nlink != 1
                        or fd.st_mode & 0o077
                        or (fd.st_dev, fd.st_ino) != (actual.st_dev, actual.st_ino)
                    ):
                        raise ValueError("Capability storage identity changed")
                    fcntl.flock(self._owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.db.execute("PRAGMA synchronous=FULL")
                self.db.execute("PRAGMA busy_timeout=500")
                with self._transaction(initial=True):
                    objects = self.db.execute(
                        "SELECT CASE WHEN length(type)<=5 THEN type END,CASE WHEN length(name)<=64 THEN name END FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 3"
                    ).fetchall()
                    version = self.db.execute("PRAGMA user_version").fetchone()[0]
                    if created and not objects and version == 0:
                        self.db.execute(
                            "CREATE TABLE capability_state (id INTEGER PRIMARY KEY CHECK(id=1), backend TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL)"
                        )
                        self.db.execute(
                            "CREATE TABLE capability_operations (key TEXT PRIMARY KEY, revision INTEGER NOT NULL UNIQUE, request TEXT NOT NULL, receipt TEXT)"
                        )
                        self.db.execute(
                            "INSERT INTO capability_state VALUES (1,?,0,?)",
                            (secrets.token_hex(16), encode({"disabled": []})),
                        )
                        self.db.execute("PRAGMA user_version=1")
                    self._schema()
                    self._read()
                    rows = self.db.execute(
                        "SELECT CASE WHEN typeof(key)='text' AND length(key)<=128 THEN key END,CASE WHEN typeof(revision)='integer' THEN revision END,CASE WHEN typeof(request)='text' AND length(request)<=16384 THEN request END,CASE WHEN receipt IS NULL OR (typeof(receipt)='text' AND length(receipt)<=16384) THEN receipt ELSE '' END FROM capability_operations ORDER BY revision LIMIT 10001"
                    ).fetchall()
                    if len(rows) > RECEIPT_LIMIT:
                        raise ValueError("Capability history exceeds its bound")
                    tools = []
                    for expected, row in enumerate(rows, 1):
                        request, receipt = self._validate_record(row, expected)
                        if request.action.operation == "set_tools":
                            if receipt is None:
                                raise ValueError("Atomic capability mask receipt is missing")
                            tools.append(request.action)
                    if self._read()[1] != tuple(tools[-1].disabled if tools else ()):
                        raise ValueError("Capability mask does not match its history")
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None
            raise

    @contextmanager
    def _transaction(self, *, initial=False):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if not initial:
                self._schema()
            yield
            self.db.commit()
        except BaseException:
            try:
                self.db.rollback()
            except BaseException:
                self._failed = True
            raise

    def _schema(self):
        if self.closed or self._failed or self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise CapabilityUnconfirmed("Capability authority is unavailable")
        if self._path is not None:
            actual, opened = self._path.stat(follow_symlinks=False), os.fstat(self._owner_fd)
            if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
                raise CapabilityUnconfirmed("Capability file identity changed")
        objects = self.db.execute(
            "SELECT CASE WHEN length(type)<=5 THEN type END,CASE WHEN length(name)<=64 THEN name END FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 3"
        ).fetchall()
        if set(objects) != {("table", "capability_state"), ("table", "capability_operations")}:
            raise ValueError("Capability schema is unavailable")
        columns = {
            "capability_state": [
                ("id", "INTEGER", 0, 1),
                ("backend", "TEXT", 1, 0),
                ("revision", "INTEGER", 1, 0),
                ("body", "TEXT", 1, 0),
            ],
            "capability_operations": [
                ("key", "TEXT", 0, 1),
                ("revision", "INTEGER", 1, 0),
                ("request", "TEXT", 1, 0),
                ("receipt", "TEXT", 0, 0),
            ],
        }
        for table, expected in columns.items():
            actual = [
                (row[1], row[2], row[3], row[5])
                for row in self.db.execute(f"PRAGMA table_info({table})")
            ]
            if actual != expected:
                raise ValueError("Capability columns are unavailable")

    def _read(self):
        self._schema()
        rows = self.db.execute(
            "SELECT id, CASE WHEN typeof(backend)='text' AND length(backend)=32 THEN backend END, CASE WHEN typeof(revision)='integer' THEN revision END, CASE WHEN typeof(body)='text' AND length(body)<=2048 THEN body END FROM capability_state LIMIT 2"
        ).fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise ValueError("Capability singleton is unavailable")
        _, backend, revision, raw = rows[0]
        if (
            type(backend) is not str
            or len(backend) != 32
            or any(c not in "0123456789abcdef" for c in backend)
            or type(revision) is not int
            or not 0 <= revision <= MAX_REVISION
        ):
            raise ValueError("Capability authority is malformed")
        body = decode(raw, 2048)
        if type(body) is not dict or set(body) != {"disabled"}:
            raise ValueError("Capability mask is malformed")
        action = ToolAvailabilityAction(operation="set_tools", disabled=body["disabled"])
        count, maximum = self.db.execute(
            "SELECT count(*),max(revision) FROM capability_operations"
        ).fetchone()
        if count != revision or count > RECEIPT_LIMIT or (maximum or 0) != revision:
            raise ValueError("Capability history is incomplete")
        if self.backend_id is not None and self.backend_id != backend:
            raise ValueError("Capability backend changed")
        self.backend_id = backend
        return revision, tuple(action.disabled)

    def _validate_record(self, row, expected=None):
        key, revision, raw, receipt = row
        if (
            type(revision) is not int
            or not 1 <= revision <= MAX_REVISION
            or (expected is not None and revision != expected)
        ):
            raise ValueError("Capability receipt revision is malformed")
        request = CapabilityChange.model_validate(decode(raw))
        if (
            request.idempotency_key != key
            or request.expected_revision + 1 != revision
            or request.expected_backend != self.backend_id
        ):
            raise ValueError("Capability receipt identity is malformed")
        if receipt is None:
            return request, None
        value = decode(receipt)
        if type(value) is not dict or set(value) != set(request.model_dump()) | {
            "status",
            "revision",
            "result",
            "change_boundary",
        }:
            raise ValueError("Capability receipt shape is malformed")
        if (
            type(value["revision"]) is not int
            or value["revision"] != revision
            or value["status"] != "capability_change_recorded"
            or value["change_boundary"] != BOUNDARY
        ):
            raise ValueError("Capability receipt is malformed")
        if encode({key: value[key] for key in request.model_dump()}) != encode(
            request.model_dump(mode="json")
        ):
            raise ValueError("Capability receipt request changed")
        result = value["result"]
        self._validate_result(request, result)
        return request, value

    @staticmethod
    def _validate_result(request, result):
        action = request.action
        if type(result) is not dict or result.get("operation") != action.operation:
            raise ValueError("Capability result is malformed")
        if action.operation == "set_tools":
            if encode(result) != encode({"operation": "set_tools", "disabled": action.disabled}):
                raise ValueError("Capability mask receipt differs")
        elif action.operation.endswith("skill"):
            expected = {
                "operation": action.operation,
                "skill_id": action.skill_id,
                "digest": action.digest,
                "enabled": action.operation == "enable_skill",
            }
            if encode(result) != encode(expected):
                raise ValueError("Capability skill receipt differs")
        elif action.operation == "evaluate_plugin":
            if (
                set(result) != {"operation", "version_id", "report_id", "passed", "cases"}
                or result["version_id"] != action.version_id
                or type(result["passed"]) is not bool
                or type(result["cases"]) is not int
                or not 1 <= result["cases"] <= 64
                or type(result["report_id"]) is not str
                or re.fullmatch(r"rep-[A-Za-z0-9-]{1,60}", result["report_id"]) is None
            ):
                raise ValueError("Capability evaluation receipt differs")
        elif encode(result) != encode(
            {
                "operation": action.operation,
                "plugin_id": action.plugin_id,
                "version_id": action.version_id,
                "enabled": action.operation != "disable_plugin",
            }
        ):
            raise ValueError("Capability activation receipt differs")

    def _record(self, request):
        row = self.db.execute(
            "SELECT key,revision,CASE WHEN typeof(request)='text' AND length(request)<=16384 THEN request END,CASE WHEN receipt IS NULL OR (typeof(receipt)='text' AND length(receipt)<=16384) THEN receipt ELSE '' END FROM capability_operations WHERE key=?",
            (request.idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        original, receipt = self._validate_record(row)
        if encode(original.model_dump(mode="json")) != encode(request.model_dump(mode="json")):
            raise CapabilityConflict("Original capability identity conflicts")
        return row[1], receipt

    def pending(self):
        self._read()
        rows = self.db.execute(
            "SELECT CASE WHEN typeof(request)='text' AND length(request)<=16384 THEN request END FROM capability_operations WHERE receipt IS NULL ORDER BY revision LIMIT 10001"
        ).fetchall()
        if len(rows) > RECEIPT_LIMIT:
            raise ValueError("Capability pending history exceeds its bound")
        return [CapabilityChange.model_validate(decode(row[0])) for row in rows]

    def mask(self):
        revision, requested = self._read()
        sections = {action_section(request.action) for request in self.pending()}
        disabled = set(requested)
        if "skills" in sections:
            disabled.update(("skills_list", "skill_load"))
        if "plugins" in sections:
            disabled.update(("plugins_list", "plugin_call"))
        return revision, tuple(name for name in NATIVE_TOOL_ORDER if name in disabled)

    async def _state(self, section):
        revision, disabled = self._read()
        state, rows = await inventory(self.runtime, section, disabled)
        pending = [
            {"idempotency_key": request.idempotency_key, "operation": request.action.operation}
            for request in self.pending()
            if action_section(request.action) == section
        ]
        pending.sort(key=lambda item: item["idempotency_key"])
        native = {
            "backend": self.backend_id,
            "instance": self.instance_id,
            "revision": revision,
            "disabled": disabled,
            "section": section,
            "host_state": state,
            "rows": rows,
            "pending": pending,
        }
        if len(encode(native)) > 8 * 1024 * 1024:
            raise ValueError("Capability inventory exceeds its bound")
        return revision, state, rows, pending, digest(native)

    def _busy(self):
        runtime = self.runtime
        return runtime.runtime_settings_busy() or any(
            bool(getattr(runtime, name, ()))
            for name in (
                "_submit_preflights",
                "_pending_submits",
                "_submit_operations",
                "_workspace_operations",
                "_workspace_workers",
            )
        )

    async def snapshot(self, section, *, after=0, limit=8):
        if (
            section not in {"tools", "skills", "plugins"}
            or type(after) is not int
            or not 0 <= after <= 1000000
            or type(limit) is not int
            or not 1 <= limit <= 8
        ):
            raise ValueError("Invalid capability inventory selection")
        async with self.runtime.integration_lock:
            revision, state, rows, pending, token = await self._state(section)
            result = {
                "schema_version": "1.0",
                "backend_id": self.backend_id,
                "instance_id": self.instance_id,
                "revision": revision,
                "state_token": token,
                "section": section,
                "host_state": state,
                "disabled": list(self._read()[1]),
                "writable": not self.closed,
                "busy": self._busy(),
                "items": [],
                "next_after": None,
                "pending_count": len(pending),
                "pending_operations": pending,
                "change_boundary": BOUNDARY,
                "limits": {"page": 8, "receipts": RECEIPT_LIMIT},
            }
            if len(encode(result).encode("ascii")) > 65536:
                raise ValueError("Capability pending metadata exceeds the response bound")
            for row in rows[after : after + limit]:
                result["items"].append(row)
                end = after + len(result["items"])
                result["next_after"] = end if end < len(rows) else None
                if len(encode(result).encode("ascii")) > 65536:
                    result["items"].pop()
                    if not result["items"]:
                        raise ValueError("One installed metadata row exceeds the response bound")
                    result["next_after"] = after + len(result["items"])
                    break
            return result

    def lookup(self, original):
        request = CapabilityChange.model_validate(original.model_dump(mode="json"))
        revision, _ = self._read()
        if request.expected_backend != self.backend_id:
            raise CapabilityConflict("Foreign capability backend")
        record = self._record(request)
        receipt = None if record is None else record[1]
        return {
            "schema_version": "1.0",
            "request": request.model_dump(mode="json"),
            "backend_id": self.backend_id,
            "instance_id": self.instance_id,
            "current_revision": revision,
            "status": "unobserved"
            if record is None
            else "recorded"
            if receipt is not None
            else "pending",
            "mutation_fenced": record is None
            and (
                request.expected_instance != self.instance_id
                or revision > request.expected_revision
            ),
            "receipt": receipt,
        }

    async def change(self, original):
        request = CapabilityChange.model_validate(original.model_dump(mode="json"))
        async with self.runtime.integration_lock:
            self._read()
            if request.expected_backend != self.backend_id:
                raise CapabilityConflict("Foreign capability backend")
            record = self._record(request)
            if record is not None:
                if record[1] is None:
                    raise CapabilityUnconfirmed("Original activation outcome remains unconfirmed")
                return record[1]
            section = action_section(request.action)
            revision, state, rows, pending, token = await self._state(section)
            if (
                request.expected_instance != self.instance_id
                or request.expected_revision != revision
                or request.expected_state != token
            ):
                raise CapabilityConflict("Capability state changed")
            revoking = request.action.operation in {"disable_skill", "disable_plugin"}
            section_pending = [
                old for old in self.pending() if action_section(old.action) == section
            ]
            blocks = bool(pending) and not (
                request.action.operation == "disable_plugin"
                and all(old.action.operation == "evaluate_plugin" for old in section_pending)
            )
            if self.runtime._closed or (not revoking and self._busy()) or blocks:
                raise ValueError("Capability activation requires settled native ownership")
            if section != "tools" and state != "configured":
                raise ValueError("Capability owner is not configured")
            self._validate_action(request.action, rows)
            result = None
            if section == "tools":
                result = {"operation": "set_tools", "disabled": request.action.disabled}
            receipt = self._receipt(request, result) if result is not None else None
            with self._transaction():
                actual, _ = self._read()
                if actual != revision:
                    raise CapabilityConflict("Capability authority changed")
                if revision >= RECEIPT_LIMIT:
                    raise ValueError("Capability receipt bound reached")
                self.db.execute(
                    "UPDATE capability_state SET revision=?, body=? WHERE id=1",
                    (
                        revision + 1,
                        encode({"disabled": request.action.disabled})
                        if section == "tools"
                        else encode({"disabled": list(self._read()[1])}),
                    ),
                )
                self.db.execute(
                    "INSERT INTO capability_operations VALUES (?,?,?,?)",
                    (
                        request.idempotency_key,
                        revision + 1,
                        encode(request.model_dump(mode="json")),
                        None if receipt is None else encode(receipt),
                    ),
                )
            if receipt is not None:
                return receipt
            # Reserve the exact intent before starting any native effect. The
            # independently retained task survives caller cancellation.
            operation = asyncio.create_task(
                self.runtime.owned_workspace_operation(
                    lambda: self._perform(request),
                    require_root=request.action.operation == "evaluate_plugin",
                    retain=self._retain_actual,
                )
            )
            self.operations.add(operation)
            operation.add_done_callback(self.operations.discard)
            operation.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return await asyncio.shield(operation)

    def _retain_actual(self, operation):
        self.actual_operations.add(operation)
        operation.add_done_callback(self.actual_operations.discard)

    @staticmethod
    def _receipt(request, result):
        return {
            **request.model_dump(mode="json"),
            "status": "capability_change_recorded",
            "revision": request.expected_revision + 1,
            "result": result,
            "change_boundary": BOUNDARY,
        }

    def _validate_action(self, action, rows):
        if action.operation == "set_tools":
            return
        if action.operation.endswith("skill"):
            row = next(
                (
                    row
                    for row in rows
                    if row["skill_id"] == action.skill_id and row["digest"] == action.digest
                ),
                None,
            )
            if row is None or not row["installed"]:
                raise CapabilityConflict("Installed skill identity changed")
            if action.operation == "enable_skill" and row["status"] in {
                "unsupported",
                "needs_setup",
                "needs_adapter",
            }:
                raise ValueError("Installed skill requires native setup or an unsupported adapter")
            return
        row = next(
            (
                row
                for row in rows
                if row["plugin_id"] == action.plugin_id
                and row["version_id"] == action.version_id
                and row["source_sha256"] == action.source_sha256
                and row["manifest_sha256"] == action.manifest_sha256
            ),
            None,
        )
        if row is None:
            raise CapabilityConflict("Installed plugin identity changed")
        if action.operation in {"promote_plugin", "rollback_plugin"} and not action.reviewed:
            raise ValueError("Plugin activation requires explicit owner review")
        if action.operation != "promote_plugin" and action.report_id is not None:
            raise ValueError("Only promotion accepts an exact report")
        if action.operation == "promote_plugin" and not any(
            report["report_id"] == action.report_id
            and report["passed"]
            and report["current_binding"]
            for report in row["reports"]
        ):
            raise ValueError("Promotion requires a current passing native report")
        if action.operation == "rollback_plugin" and not row["rollback_eligible"]:
            raise ValueError("No exact approved rollback target")
        if action.operation == "evaluate_plugin" and not row["suite_configured"]:
            raise ValueError("No owner suite configured")

    async def _perform(self, request):
        action = request.action
        try:
            if action.operation.endswith("skill"):
                result = await self.runtime.skill_service.activate_installed_exact(
                    action.operation, action.skill_id, action.digest
                )
            else:
                service = self.runtime.plugin_service
                if action.operation == "evaluate_plugin":
                    result = await service.evaluate(action.version_id)
                    result = {
                        key: result[key] for key in ("version_id", "report_id", "passed", "cases")
                    }
                else:

                    def apply():
                        registry = service.registry
                        with registry._thread:
                            row = registry.inspect(action.version_id)
                            if (
                                row["plugin_id"] != action.plugin_id
                                or row["source_sha256"] != action.source_sha256
                                or digest(row["manifest"]) != action.manifest_sha256
                            ):
                                raise CapabilityConflict("Captured plugin identity changed")
                            if action.operation == "promote_plugin":
                                selected = registry.promote(
                                    action.version_id,
                                    action.report_id,
                                    actor="owner",
                                    reviewed=True,
                                )
                            elif action.operation == "rollback_plugin":
                                entry = registry._read_active_doc()["plugins"].get(
                                    action.plugin_id, {}
                                )
                                if (
                                    not entry.get("history")
                                    or entry["history"][-1] != action.version_id
                                ):
                                    raise CapabilityConflict("Rollback target changed")
                                selected = registry.rollback(
                                    action.plugin_id, actor="owner", reviewed=True
                                )
                            else:
                                registry.disable(action.plugin_id, actor="owner")
                                selected = action.version_id
                            return {
                                "plugin_id": action.plugin_id,
                                "version_id": selected,
                                "enabled": action.operation != "disable_plugin",
                            }

                    worker = asyncio.create_task(asyncio.to_thread(apply))
                    self.native_workers.add(worker)
                    worker.add_done_callback(self.native_workers.discard)
                    worker.add_done_callback(
                        lambda task: task.exception() if not task.cancelled() else None
                    )
                    try:
                        result = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        # Pointer/audit writes may still be in a real thread.
                        # Repeated cancellation never frees that owner early.
                        while not worker.done():
                            try:
                                await asyncio.shield(worker)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        raise
            receipt = self._receipt(request, {"operation": action.operation, **result})
            self._validate_result(request, receipt["result"])
            with self._transaction():
                record = self._record(request)
                if record is None:
                    raise CapabilityUnconfirmed("Original activation reservation is unavailable")
                self.db.execute(
                    "UPDATE capability_operations SET receipt=? WHERE key=? AND receipt IS NULL",
                    (encode(receipt), request.idempotency_key),
                )
            return receipt
        except BaseException:
            # Native errors can follow a durable pointer/report write. Preserve
            # the original pending record; state equality is not a receipt.
            raise CapabilityUnconfirmed(
                "Activation outcome is unconfirmed; inspect the original request"
            ) from None

    async def close(self):
        if self.closed:
            return
        # No host/controller or SQLite close underneath real plugin workers.
        while self.operations or self.actual_operations or self.native_workers:
            await asyncio.shield(
                asyncio.gather(
                    *tuple(self.operations | self.actual_operations | self.native_workers),
                    return_exceptions=True,
                )
            )
        self.closed = True
        self.db.close()
        if self._owner_fd is not None:
            os.close(self._owner_fd)
            self._owner_fd = None
