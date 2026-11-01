"""Codex-authored native execution preference authority and SQLite regressions."""

import asyncio
import hashlib
import json
import re
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from core.policy import PolicyDenied, ToolPolicy
from packages.providers.text import ScriptedProvider
from services.agent import runtime_settings as native
from services.agent.options_api import router
from services.common.auth import Auth


class Retirement:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def retire_connections(self, *, actor):
        assert actor == "owner"
        self.calls += 1
        self.entered.set()
        await self.release.wait()


@pytest.fixture
def make_settings(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    owned = []

    def make(path=None, *, yolo=False):
        runtime = SimpleNamespace(
            policy=ToolPolicy(workspace, yolo=yolo),
            max_steps=12,
            busy=False,
            mcp_controller=Retirement(),
            mcp_host=None,
            integration_lock=asyncio.Lock(),
            provider=ScriptedProvider([]),
            model_settings=None,
        )
        runtime.runtime_settings_busy = lambda: runtime.busy
        runtime.tool_snapshot = lambda: SimpleNamespace(available_tools=[])
        settings = native.AgentRuntimeSettings(runtime, path=path)
        runtime.runtime_settings = settings
        owned.append(settings)
        return settings

    yield make
    for settings in reversed(owned):
        settings.close()


def legacy_change(controller, key="save", **overrides):
    observed = controller.snapshot()
    body = {
        "schema_version": "1.0",
        "expected_revision": observed["revision"],
        "expected_state": observed["state_token"],
        "idempotency_key": key,
        "settings": {**observed["settings"], "max_steps": 13, "file_writes": "deny"},
        "yolo": True,
        **overrides,
    }
    return native.RuntimeSettingsChange.model_validate(body)


def request_digest(request):
    return hashlib.sha256(
        json.dumps(
            request.model_dump(), sort_keys=True, allow_nan=False, ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


async def test_legacy_first_write_and_exact_replay_preserve_digest_and_receipt(make_settings):
    settings = make_settings()
    request = legacy_change(settings)
    receipt = await settings.change(request)
    assert receipt == {
        "schema_version": "1.0",
        "status": "settings_saved",
        "revision": 1,
        "idempotency_key": "save",
        "settings": request.settings.model_dump(),
        "requested_yolo": True,
        "service_instance": settings.instance,
        "change_boundary": "settled_service",
    }
    row = settings.db.execute("SELECT digest,receipt FROM operations").fetchone()
    assert row == (request_digest(request), json.dumps(receipt))
    assert "expected_backend" not in request.model_dump()
    assert "backend_id" not in receipt
    settings.runtime.busy = True
    settings.runtime.policy.yolo = False
    assert await settings.change(request) == receipt
    assert settings.runtime.mcp_controller.calls == 1
    assert settings.runtime.policy.yolo is False
    assert settings.db.execute("SELECT digest,receipt FROM operations").fetchone() == row


async def test_legacy_restart_restores_preferences_but_not_historical_yolo(make_settings, tmp_path):
    path = tmp_path / "execution.sqlite"
    first = make_settings(path)
    request = legacy_change(first)
    receipt = await first.change(request)
    first.close()
    restarted = make_settings(path, yolo=False)
    assert restarted.snapshot()["settings"] == request.settings.model_dump()
    assert restarted.snapshot()["yolo"] is False
    assert restarted.instance != first.instance
    assert await restarted.change(request) == receipt
    assert restarted.runtime.mcp_controller.calls == 0
    assert restarted.runtime.policy.yolo is False


async def test_legacy_unicode_directory_normalization_preserves_exact_canonical_digest(
    make_settings,
):
    settings = make_settings()
    directory = settings.runtime.policy.workspace / " café " / r"literal\name"
    directory.mkdir(parents=True)
    request = legacy_change(
        settings,
        settings={
            **settings.snapshot()["settings"],
            "working_directory": "./ café //literal\\name/.",
        },
    )
    assert request.settings.working_directory == " café /literal\\name"
    receipt = await settings.change(request)
    assert receipt["settings"]["working_directory"] == " café /literal\\name"
    assert settings.db.execute("SELECT digest FROM operations").fetchone()[0] == request_digest(
        request
    )


async def test_legacy_state_token_refuses_change_but_allows_actual_aba(make_settings):
    settings = make_settings()
    request = legacy_change(settings)
    settings.runtime.policy.yolo = True
    with pytest.raises(native.RuntimeSettingsConflict):
        await settings.change(request)
    settings.runtime.policy.yolo = False
    assert (await settings.change(request))["revision"] == 1


async def test_policy_is_rechecked_after_mcp_retirement_without_holding_sqlite_write_lock(
    make_settings, tmp_path
):
    path = tmp_path / "execution.sqlite"
    settings = make_settings(path)
    request = legacy_change(settings)
    retirement = settings.runtime.mcp_controller
    retirement.release.clear()
    pending = asyncio.create_task(settings.change(request))
    await asyncio.wait_for(retirement.entered.wait(), 1)
    with sqlite3.connect(path, timeout=0) as observer:
        observer.execute("BEGIN IMMEDIATE")
        observer.rollback()
    settings.runtime.policy.yolo = True
    retirement.release.set()
    with pytest.raises(native.RuntimeSettingsConflict):
        await pending
    assert settings.db.execute("SELECT revision FROM settings").fetchone()[0] == 0
    assert settings.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


async def test_new_controller_fences_an_older_open_legacy_writer(make_settings, tmp_path):
    path = tmp_path / "execution.sqlite"
    first = make_settings(path)
    request = legacy_change(first)
    current = make_settings(path)
    with pytest.raises(native.RuntimeSettingsConflict):
        await first.change(request)
    assert first.runtime.mcp_controller.calls == 0
    assert current.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


class FailedRollback:
    """Real DML stays uncommitted until the controller poisons this handle."""

    def __init__(self, db):
        self.db = db
        self.wrote = False

    def __getattr__(self, key):
        return getattr(self.db, key)

    def execute(self, sql, parameters=()):
        result = self.db.execute(sql, parameters)
        if sql.startswith("UPDATE settings SET revision="):
            self.wrote = True
        return result

    def commit(self):
        if self.wrote:
            raise RuntimeError("synthetic uncertain commit")
        self.db.commit()

    def rollback(self):
        if self.wrote:
            raise RuntimeError("synthetic failed rollback")
        self.db.rollback()


async def test_failed_rollback_cannot_publish_an_uncommitted_receipt(make_settings, tmp_path):
    path = tmp_path / "execution.sqlite"
    settings = make_settings(path)
    request = legacy_change(settings)
    settings.db = FailedRollback(settings.db)
    with pytest.raises(RuntimeError):
        await settings.change(request)
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        assert observer.execute("SELECT revision FROM settings").fetchone()[0] == 0
    with pytest.raises(RuntimeError):
        settings.receipt(request.idempotency_key)
    assert settings.db.wrote is True


def bound_change(controller, key="bound", **overrides):
    body = legacy_change(controller, key).model_dump()
    body.update(
        schema_version="1.1",
        expected_backend=controller.snapshot()["write_authority"]["backend_id"],
        expected_instance=controller.instance,
    )
    return native.BoundRuntimeSettingsChange.model_validate({**body, **overrides})


async def test_bound_receipt_proves_original_full_request_and_native_authority(make_settings):
    settings = make_settings()
    snapshot = settings.snapshot()
    assert snapshot["schema_version"] == "1.0"
    assert snapshot["write_authority"] == {
        "mutation_version": "1.1",
        "backend_id": settings.backend_id,
        "instance_id": settings.instance,
        "writable": True,
    }
    assert re.fullmatch(r"[a-f0-9]{32}", settings.backend_id)
    assert len(settings.instance) == 36
    request = bound_change(settings)
    assert settings.lookup(request) == {
        "schema_version": "1.1",
        "backend_id": settings.backend_id,
        "request": request.model_dump(),
        "status": "unobserved",
        "receipt": None,
        "mutation_fenced": False,
    }
    receipt = await settings.change(request)
    assert receipt["schema_version"] == "1.1"
    assert receipt["backend_id"] == request.expected_backend
    assert receipt["service_instance"] == request.expected_instance
    assert receipt["settings"] == request.settings.model_dump()
    proof = settings.lookup(request)
    assert proof["status"] == "recorded" and proof["mutation_fenced"] is True
    assert proof["request"] == request.model_dump() and proof["receipt"] == receipt
    assert settings.receipt(request.idempotency_key) == receipt
    assert settings.db.execute("SELECT digest FROM operations").fetchone()[0] == request_digest(
        request
    )
    settings.runtime.busy = True
    settings.runtime.policy.yolo = False
    assert await settings.change(request) == receipt
    assert settings.runtime.mcp_controller.calls == 1
    assert settings.runtime.policy.yolo is False


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_backend": "a" * 31},
        {"expected_backend": "A" * 32},
        {"expected_backend": "a" * 32 + "\n"},
        {"expected_instance": "a" * 32},
        {"expected_instance": "00000000-0000-0000-0000-000000000000\n"},
        {"expected_instance": None},
        {"expected_revision": True},
        {"expected_state": 1},
        {"yolo": 1},
        {"schema_version": "1.0"},
        {"unexpected": "field"},
    ],
)
def test_bound_request_has_strict_versioned_authority_fields(make_settings, mutation):
    body = bound_change(make_settings()).model_dump()
    with pytest.raises(ValidationError):
        native.BoundRuntimeSettingsChange.model_validate({**body, **mutation})
    with pytest.raises(ValidationError):
        native.RuntimeSettingsChange.model_validate(body)


async def test_foreign_backend_refuses_mutation_and_lookup_despite_equal_revision(
    make_settings, tmp_path
):
    original = make_settings(tmp_path / "original.sqlite")
    replacement = make_settings(tmp_path / "replacement.sqlite")
    request = bound_change(original)
    assert original.snapshot()["revision"] == replacement.snapshot()["revision"]
    assert original.backend_id != replacement.backend_id
    with pytest.raises(native.RuntimeSettingsConflict):
        await replacement.change(request)
    with pytest.raises(native.RuntimeSettingsConflict):
        replacement.lookup(request)
    assert replacement.runtime.mcp_controller.calls == 0


async def test_restart_retains_backend_and_exact_bound_receipt_but_fences_absent_original(
    make_settings, tmp_path
):
    path = tmp_path / "execution.sqlite"
    original = make_settings(path)
    absent = bound_change(original, "absent")
    legacy_absent = legacy_change(original, "legacy-absent")
    recorded = bound_change(original)
    receipt = await original.change(recorded)
    original.close()
    current = make_settings(path)
    assert current.backend_id == original.backend_id and current.instance != original.instance
    assert current.lookup(absent)["mutation_fenced"] is True
    assert current.lookup(legacy_absent)["mutation_fenced"] is False
    assert current.lookup(recorded)["receipt"] == receipt
    assert await current.change(recorded) == receipt
    assert current.runtime.policy.yolo is False
    assert current.runtime.mcp_controller.calls == 0
    with pytest.raises(native.RuntimeSettingsConflict):
        await current.change(absent)


async def test_retired_writer_can_read_proof_but_cannot_write_or_reactivate_on_close(
    make_settings, tmp_path
):
    path = tmp_path / "execution.sqlite"
    old = make_settings(path)
    recorded = bound_change(old)
    receipt = await old.change(recorded)
    absent = bound_change(old, "absent")
    current = make_settings(path)
    assert old.snapshot()["write_authority"]["writable"] is False
    assert old.lookup(recorded)["receipt"] == receipt
    assert await old.change(recorded) == receipt
    assert old.lookup(absent)["mutation_fenced"] is True
    with pytest.raises(native.RuntimeSettingsConflict):
        await old.change(absent)
    old.close()
    assert current.snapshot()["write_authority"]["writable"] is True
    assert (await current.change(bound_change(current, "current")))["revision"] == 2


async def test_lookup_requires_irreversible_fence_not_state_policy_busy_or_future_revision(
    make_settings,
):
    settings = make_settings()
    request = bound_change(settings)
    settings.runtime.policy.yolo = True
    settings.runtime.busy = True
    settings.runtime.policy.shell_working_directory = "missing-directory"
    assert settings.lookup(request)["mutation_fenced"] is False
    settings.runtime.policy.yolo = False
    settings.runtime.busy = False
    settings.runtime.policy.shell_working_directory = "."
    assert settings.lookup(request)["mutation_fenced"] is False
    future = request.model_copy(update={"expected_revision": 10})
    assert settings.lookup(future)["mutation_fenced"] is False
    await settings.change(bound_change(settings, "another"))
    assert settings.lookup(request)["mutation_fenced"] is True
    assert settings.lookup(future)["mutation_fenced"] is False


async def test_generation_is_rechecked_after_mcp_retirement(make_settings, tmp_path):
    path = tmp_path / "execution.sqlite"
    old = make_settings(path)
    request = bound_change(old)
    retirement = old.runtime.mcp_controller
    retirement.release.clear()
    pending = asyncio.create_task(old.change(request))
    await asyncio.wait_for(retirement.entered.wait(), 1)
    current = make_settings(path)
    retirement.release.set()
    with pytest.raises(native.RuntimeSettingsConflict):
        await pending
    assert current.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    assert old.runtime.policy.yolo is False


async def test_exact_receipt_created_during_retirement_precedes_new_eligibility(make_settings):
    settings = make_settings()
    request = bound_change(settings)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def retire(*, actor):
        nonlocal calls
        assert actor == "owner"
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()

    settings.runtime.mcp_controller.retire_connections = retire
    first = asyncio.create_task(settings.change(request))
    await asyncio.wait_for(entered.wait(), 1)
    receipt = await settings.change(request)
    settings.runtime.busy = True
    settings.runtime.policy.yolo = False
    release.set()
    assert await first == receipt
    assert settings.runtime.policy.yolo is False
    assert settings.snapshot()["revision"] == 1
    assert settings.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
    assert calls == 2


def legacy_database(path):
    preferences = native.ExecutionPreferences(
        file_writes="deny",
        shell_commands="ask",
        tool_network=False,
        max_steps=17,
        shell_timeout_s=120,
        working_directory=".",
    )
    request = native.RuntimeSettingsChange(
        schema_version="1.0",
        expected_revision=0,
        expected_state="a" * 64,
        idempotency_key="legacy-original",
        settings=preferences,
        yolo=True,
    )
    receipt = {
        "schema_version": "1.0",
        "status": "settings_saved",
        "revision": 1,
        "idempotency_key": request.idempotency_key,
        "settings": preferences.model_dump(),
        "requested_yolo": True,
        "service_instance": "00000000-0000-4000-8000-000000000000",
        "change_boundary": "settled_service",
    }
    raw = json.dumps(receipt, indent=3)
    # Match the owner-only file produced by the legacy native controller.
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), "
            "revision INTEGER NOT NULL, body TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL)"
        )
        db.execute("INSERT INTO settings VALUES (1,1,?)", (preferences.model_dump_json(),))
        db.execute(
            "INSERT INTO operations VALUES (?,?,?)",
            (request.idempotency_key, request_digest(request), raw),
        )
    return request, receipt, raw


async def test_actual_legacy_migration_keeps_digest_raw_receipt_and_new_writes(
    make_settings, tmp_path
):
    path = tmp_path / "legacy.sqlite"
    request, receipt, raw = legacy_database(path)
    settings = make_settings(path)
    assert await settings.change(request) == receipt
    assert settings.lookup(request)["receipt"] == receipt
    assert settings.lookup(request)["request"] == request.model_dump()
    assert settings.db.execute("SELECT digest,receipt FROM operations").fetchone() == (
        request_digest(request),
        raw,
    )
    assert settings.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert settings.runtime.mcp_controller.calls == 0
    assert (await settings.change(legacy_change(settings, "new-legacy")))["revision"] == 2
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "corrupt",
    [
        "DROP TABLE runtime_write_authority",
        "DELETE FROM runtime_write_authority",
        "UPDATE runtime_write_authority SET backend_id='invalid'",
        "UPDATE runtime_write_authority SET active_instance='invalid'",
        "PRAGMA user_version=0",
        "PRAGMA user_version=2",
    ],
)
def test_migrated_corrupt_identity_is_not_reminted(make_settings, tmp_path, corrupt):
    path = tmp_path / "execution.sqlite"
    settings = make_settings(path)
    settings.close()
    with sqlite3.connect(path) as db:
        db.execute(corrupt)
    with pytest.raises(native.RuntimeSettingsUnavailable):
        make_settings(path)


@pytest.mark.parametrize(
    "schema",
    [
        "CREATE TABLE unrelated (value TEXT)",
        "CREATE VIEW settings AS SELECT 1 AS id,0 AS revision,'{}' AS body",
        "CREATE TABLE settings (id INTEGER PRIMARY KEY,revision INTEGER,body BLOB)",
    ],
)
def test_partially_initialized_or_foreign_store_is_not_a_fresh_database(
    make_settings, tmp_path, schema
):
    path = tmp_path / "partial.sqlite"
    # Exercise schema refusal after passing the independent file-mode guard.
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.execute(schema)
    with pytest.raises(native.RuntimeSettingsUnavailable):
        make_settings(path)


class StorageFault:
    def __init__(self, db, stage, error_type):
        self.db, self.stage, self.error_type = db, stage, error_type
        self.wrote, self.failed = False, False

    def __getattr__(self, key):
        return getattr(self.db, key)

    def fail(self):
        self.failed = True
        raise self.error_type("synthetic storage uncertainty")

    def execute(self, sql, parameters=()):
        result = self.db.execute(sql, parameters)
        if sql.startswith("UPDATE settings SET revision="):
            self.wrote = True
            if self.stage == "settings" and not self.failed:
                self.fail()
        if (
            sql.startswith("INSERT INTO operations")
            and self.stage == "operations"
            and not self.failed
        ):
            self.fail()
        return result

    def commit(self):
        if self.wrote and not self.failed and self.stage in {"commit", "after-commit"}:
            if self.stage == "after-commit":
                self.db.commit()
            self.fail()
        self.db.commit()


@pytest.mark.parametrize("stage", ["settings", "operations", "commit", "after-commit"])
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, RuntimeError, ValueError])
async def test_real_sqlite_transaction_faults_are_unknown_and_reconcile_exactly(
    make_settings, tmp_path, stage, error_type
):
    path = tmp_path / "execution.sqlite"
    settings = make_settings(path)
    request = bound_change(settings)
    settings.db = StorageFault(settings.db, stage, error_type)
    with pytest.raises(native.RuntimeSettingsUnavailable):
        await settings.change(request)
    assert settings.db.wrote is True and settings.db.failed is True
    assert settings.runtime.policy.yolo is False and settings.runtime.max_steps == 12
    with sqlite3.connect(path) as observer:
        count = observer.execute("SELECT count(*) FROM operations").fetchone()[0]
        revision = observer.execute("SELECT revision FROM settings").fetchone()[0]
    assert count == revision == (1 if stage == "after-commit" else 0)
    proof = settings.lookup(request)
    assert proof["status"] == ("recorded" if stage == "after-commit" else "unobserved")
    assert proof["mutation_fenced"] is (stage == "after-commit")
    if stage == "after-commit":
        assert await settings.change(request) == proof["receipt"]
        assert settings.runtime.policy.yolo is False
        assert settings.runtime.mcp_controller.calls == 1


@pytest.mark.parametrize(
    "bad_json",
    [
        '{"schema_version":"1.1","schema_version":"1.1"}',
        '{"revision":NaN}',
        '{"revision":1e999}',
        "[" * 20_000 + "0" + "]" * 20_000,
        '"not-a-receipt"',
    ],
)
async def test_corrupt_stored_receipt_never_becomes_positive_or_absent_proof(
    make_settings, bad_json
):
    settings = make_settings()
    request = bound_change(settings)
    await settings.change(request)
    settings.db.execute("UPDATE operations SET receipt=?", (bad_json,))
    settings.db.commit()
    for observe in (
        lambda: settings.receipt(request.idempotency_key),
        lambda: settings.lookup(request),
    ):
        with pytest.raises(native.RuntimeSettingsUnavailable):
            observe()
    with pytest.raises(native.RuntimeSettingsUnavailable):
        await settings.change(request)


async def test_full_request_digest_and_receipt_fields_both_must_match(make_settings):
    settings = make_settings()
    request = bound_change(settings)
    await settings.change(request)
    different = request.model_copy(update={"expected_state": "b" * 64})
    with pytest.raises(native.RuntimeSettingsConflict):
        settings.lookup(different)
    with pytest.raises(native.RuntimeSettingsConflict):
        await settings.change(different)
    receipt = settings.receipt(request.idempotency_key)
    receipt["requested_yolo"] = False
    settings.db.execute("UPDATE operations SET receipt=?", (json.dumps(receipt),))
    settings.db.commit()
    with pytest.raises(native.RuntimeSettingsUnavailable):
        settings.lookup(request)


def http_client(settings):
    app = FastAPI()
    tokens = {role: role + "-synthetic-01234567890123456789" for role in ("owner", "agent")}
    app.include_router(router(settings.runtime, Auth(tokens)))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://synthetic",
        headers={"Authorization": "Bearer " + tokens["owner"]},
    )


@pytest.mark.parametrize("version", ["1.0", "1.1"])
async def test_owner_http_routes_accept_both_versions_and_full_request_proof(
    make_settings, version
):
    settings = make_settings()
    request = legacy_change(settings) if version == "1.0" else bound_change(settings)
    async with http_client(settings) as client:
        response = await client.post("/v1/agent/options/runtime", json=request.model_dump())
        assert response.status_code == 200
        proof = await client.post(
            "/v1/agent/options/runtime-changes/lookup", json=request.model_dump()
        )
        assert proof.status_code == 200 and proof.json()["receipt"] == response.json()
        assert proof.json()["request"] == request.model_dump()
        assert proof.json()["mutation_fenced"] is True
        observed = await client.get("/v1/agent/options")
        assert observed.status_code == 200
        assert observed.json()["runtime_settings"]["write_authority"]["writable"] is True
        for result in (response, proof, observed):
            assert result.headers["cache-control"] == "no-store"


async def test_runtime_http_auth_malformed_conflict_and_path_refusal_stay_distinct(make_settings):
    settings = make_settings()
    request = bound_change(settings).model_dump()
    async with http_client(settings) as client:
        for path in ("/v1/agent/options/runtime", "/v1/agent/options/runtime-changes/lookup"):
            for headers, status in (
                ({"Authorization": ""}, 401),
                ({"Authorization": "Bearer agent-synthetic-01234567890123456789"}, 403),
            ):
                response = await client.post(path, json=request, headers=headers)
                assert response.status_code == status
                assert response.headers["cache-control"] == "no-store"
        malformed = await client.post(
            "/v1/agent/options/runtime", json={**request, "expected_instance": None}
        )
        assert malformed.status_code == 422 and malformed.headers["cache-control"] == "no-store"
        conflict = await client.post(
            "/v1/agent/options/runtime", json={**request, "expected_backend": "0" * 32}
        )
        assert conflict.status_code == 409
        invalid = await client.post(
            "/v1/agent/options/runtime",
            json={**request, "settings": {**request["settings"], "working_directory": "missing"}},
        )
        assert invalid.status_code == 400
    assert settings.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


async def test_runtime_http_storage_failure_is_fixed_503_not_user_error(make_settings):
    settings = make_settings()
    request = bound_change(settings)
    settings.db = StorageFault(settings.db, "operations", RuntimeError)
    async with http_client(settings) as client:
        response = await client.post("/v1/agent/options/runtime", json=request.model_dump())
        assert response.status_code == 503
        assert response.json() == {
            "detail": "Execution settings outcome is unconfirmed; inspect the exact receipt"
        }
        assert response.headers["cache-control"] == "no-store"
    assert settings.receipt(request.idempotency_key) is None


@pytest.mark.parametrize(
    "error_type", [ValueError, RuntimeError, sqlite3.OperationalError, PolicyDenied]
)
async def test_retirement_failure_is_unconfirmed_not_invalid_user_input(make_settings, error_type):
    settings = make_settings()
    request = bound_change(settings)

    async def failed_retirement(*, actor):
        raise error_type("synthetic retirement failure")

    settings.runtime.mcp_controller.retire_connections = failed_retirement
    async with http_client(settings) as client:
        response = await client.post("/v1/agent/options/runtime", json=request.model_dump())
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Execution settings outcome is unconfirmed; inspect the exact receipt"
    }
    assert settings.receipt(request.idempotency_key) is None
    assert settings.runtime.policy.yolo is False


class FetchFault:
    def __init__(self, cursor, error_type):
        self.cursor, self.error_type = cursor, error_type

    def fetchone(self):
        self.cursor.fetchone()
        raise self.error_type("synthetic receipt read failure")


class ReceiptReadFault:
    def __init__(self, db, error_type):
        self.db, self.error_type = db, error_type

    def __getattr__(self, key):
        return getattr(self.db, key)

    def execute(self, sql, parameters=()):
        cursor = self.db.execute(sql, parameters)
        if sql.startswith("SELECT digest,receipt FROM operations"):
            return FetchFault(cursor, self.error_type)
        return cursor


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, sqlite3.OperationalError])
async def test_actual_receipt_fetch_failure_is_fixed_unknown_for_every_http_observation(
    make_settings, error_type
):
    settings = make_settings()
    request = bound_change(settings)
    await settings.change(request)
    settings.db = ReceiptReadFault(settings.db, error_type)
    async with http_client(settings) as client:
        responses = [
            await client.get("/v1/agent/options/runtime-changes/" + request.idempotency_key),
            await client.post(
                "/v1/agent/options/runtime-changes/lookup", json=request.model_dump()
            ),
            await client.post("/v1/agent/options/runtime", json=request.model_dump()),
        ]
    for response in responses:
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert "synthetic" not in response.text


@pytest.mark.parametrize(
    "corrupt",
    [
        '{"max_steps":12,"max_steps":12}',
        '{"max_steps":Infinity}',
        "[" * 20_000 + "0" + "]" * 20_000,
        '"not-settings"',
    ],
)
async def test_corrupt_persisted_settings_are_unknown_not_absence(make_settings, corrupt):
    settings = make_settings()
    request = bound_change(settings)
    settings.db.execute("UPDATE settings SET body=?", (corrupt,))
    settings.db.commit()
    with pytest.raises(native.RuntimeSettingsUnavailable):
        settings.snapshot()
    with pytest.raises(native.RuntimeSettingsUnavailable):
        settings.lookup(request)
    with pytest.raises(native.RuntimeSettingsUnavailable):
        await settings.change(request)


async def test_cancelled_mcp_retirement_never_applies_or_records_settings(make_settings):
    settings = make_settings()
    request = bound_change(settings)
    retirement = settings.runtime.mcp_controller
    retirement.release.clear()
    pending = asyncio.create_task(settings.change(request))
    await asyncio.wait_for(retirement.entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert settings.lookup(request)["mutation_fenced"] is False
    assert settings.receipt(request.idempotency_key) is None
    assert settings.runtime.policy.yolo is False


def test_unopenable_sqlite_path_is_native_storage_unavailability(make_settings, tmp_path):
    directory = tmp_path / "not-a-database-file"
    directory.mkdir()
    with pytest.raises(native.RuntimeSettingsUnavailable):
        make_settings(directory)


async def test_caller_mutation_during_retirement_cannot_rewrite_frozen_native_request(
    make_settings,
):
    settings = make_settings()
    request = bound_change(settings)
    original = request.model_dump()
    retirement = settings.runtime.mcp_controller
    retirement.release.clear()
    pending = asyncio.create_task(settings.change(request))
    await asyncio.wait_for(retirement.entered.wait(), 1)
    request.settings.max_steps = 999
    request.idempotency_key = "mutated-later"
    request.yolo = False
    retirement.release.set()
    receipt = await pending
    assert receipt["idempotency_key"] == original["idempotency_key"]
    assert receipt["settings"] == original["settings"]
    assert receipt["requested_yolo"] is original["yolo"]
    assert settings.lookup(original)["receipt"] == receipt
    assert settings.receipt("mutated-later") is None


async def test_non_utf8_stored_receipt_is_unknown_instead_of_http_serialization_failure(
    make_settings,
):
    settings = make_settings()
    request = bound_change(settings)
    receipt = await settings.change(request)
    receipt["settings"]["working_directory"] = "\ud800"
    settings.db.execute("UPDATE operations SET receipt=?", (json.dumps(receipt),))
    settings.db.commit()
    async with http_client(settings) as client:
        response = await client.get("/v1/agent/options/runtime-changes/" + request.idempotency_key)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
