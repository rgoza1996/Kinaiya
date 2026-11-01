"""Independent #191 regressions: real SQLite and ASGI, synthetic owner/providers.

These Codex-authored oracles precede the implementation. No external service,
provider inference, live settings, or existing test fixture is used or changed.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from packages.contracts.models import ProviderInfo
from packages.providers.text import ScriptedProvider
from services.agent.model_settings import AgentModelSettings, ModelSettingsChange
from services.agent.options_api import router
from services.common.auth import Auth

WRITE = "/v1/agent/options/models"
LOOKUP = "/v1/agent/options/model-changes/lookup"
OWNER = {"Authorization": "Bearer " + "o" * 32}
AUTH = Auth({"owner": "o" * 32, "agent": "a" * 32})


@pytest.fixture
async def settings_factory():
    controllers, providers = [], []

    def provider(model, effort=None):
        instance = ScriptedProvider([])
        instance.info = ProviderInfo(provider="synthetic-authority", model=model, local=True)
        instance.reasoning_effort = effort
        providers.append(instance)
        return instance

    def create(path, *, profiles=True):
        settings = AgentModelSettings(
            provider("original"),
            {"fast": lambda effort: provider("fast", effort)} if profiles else {},
            path=path,
            reasoning_levels={"fast": ["low", "high"]} if profiles else {},
            auxiliary_factories={"fast": lambda effort: provider("fast", effort)}
            if profiles
            else {},
            auxiliary_host_factory=lambda effort: provider("original", effort),
        )
        controllers.append(settings)
        return settings

    yield create
    for settings in reversed(controllers):
        await settings.close()
    assert all(instance.calls == [] for instance in providers)


def authority(settings):
    snapshot = settings.snapshot()
    assert snapshot["schema_version"] == "1.0"
    result = snapshot["write_authority"]
    assert set(result) == {"mutation_version", "backend_id", "instance_id", "writable"}
    assert result["mutation_version"] == "1.1"
    for key in ("backend_id", "instance_id"):
        assert type(result[key]) is str and re.fullmatch(r"[0-9a-f]{32}", result[key])
    assert type(result["writable"]) is bool
    return result


def bound(settings, key, *, revision=None, harness=None, purpose=None, profile="fast"):
    observed = authority(settings)
    return {
        "schema_version": "1.1",
        "expected_backend": observed["backend_id"],
        "expected_instance": observed["instance_id"],
        "expected_revision": settings.snapshot()["revision"] if revision is None else revision,
        "idempotency_key": key,
        "harness_version": harness,
        "profile_id": profile,
        "reasoning": None,
        **({"auxiliary_task": purpose} if purpose is not None else {}),
    }


def canonical(body):
    result = {
        "schema_version": body["schema_version"],
        "expected_revision": body["expected_revision"],
        "idempotency_key": body["idempotency_key"],
        "harness_version": body.get("harness_version"),
        "profile_id": body.get("profile_id"),
        "reasoning": body.get("reasoning"),
    }
    if body["schema_version"] == "1.1":
        result.update(
            expected_backend=body["expected_backend"], expected_instance=body["expected_instance"]
        )
    if body.get("auxiliary_task") is not None:
        result["auxiliary_task"] = body["auxiliary_task"]
    return result


def receipt(body):
    result = {
        "schema_version": body["schema_version"],
        "idempotency_key": body["idempotency_key"],
        "revision": body["expected_revision"] + 1,
        "harness_version": body.get("harness_version"),
        "binding": None
        if body.get("profile_id") is None
        else {"profile_id": body["profile_id"], "reasoning": body.get("reasoning")},
        "change_boundary": "new_tasks",
    }
    if body["schema_version"] == "1.1":
        result.update(
            backend_id=body["expected_backend"], service_instance=body["expected_instance"]
        )
    if body.get("auxiliary_task") is not None:
        result["auxiliary_task"] = body["auxiliary_task"]
    return result


def lookup_result(body, *, recorded=None, fenced=False):
    return {
        "schema_version": "1.1",
        "backend_id": body["expected_backend"],
        "request": canonical(body),
        "status": "recorded" if recorded is not None else "unobserved",
        "receipt": recorded,
        "mutation_fenced": recorded is not None or fenced,
    }


def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def rows(connection):
    names = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return {
        "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "tables": {
            name: tuple(connection.execute(f"SELECT * FROM {quoted(name)} ORDER BY rowid"))
            for (name,) in names
        },
    }


def legacy_database(path, *, purpose=None, explicit_null=False):
    body = {
        "schema_version": "1.0",
        "expected_revision": 0,
        "idempotency_key": "historical-save",
        "harness_version": "seed-v1",
        "profile_id": "fast",
        "reasoning": None,
        **({"auxiliary_task": purpose} if purpose is not None or explicit_null else {}),
    }
    state = {"default": {"profile_id": "host-default", "reasoning": None}, "harnesses": {}}
    binding = {"profile_id": "fast", "reasoning": None}
    if purpose is None:
        state["harnesses"]["seed-v1"] = binding
    else:
        state.update(auxiliary={}, harness_auxiliary={"seed-v1": {purpose: binding}})
    saved = receipt(body)
    digest = hashlib.sha256(json.dumps(canonical(body), sort_keys=True).encode()).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), "
            "revision INTEGER NOT NULL, body TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, "
            "receipt TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO settings VALUES (1,1,?)", (json.dumps(state),))
        connection.execute(
            "INSERT INTO operations VALUES (?,?,?)",
            (body["idempotency_key"], digest, json.dumps(saved)),
        )
    return body, saved


@asynccontextmanager
async def api(settings, *, runtime=None):
    runtime = runtime or SimpleNamespace(
        model_settings=settings,
        integration_lock=asyncio.Lock(),
        provider=object(),
        session_harness=Mock(return_value="seed-v1"),
    )
    app = FastAPI()
    app.include_router(router(runtime, AUTH))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://synthetic-authority.invalid",
        headers=OWNER,
    ) as client:
        yield client, runtime


async def post_saved(client, body):
    response = await client.post(WRITE, json=body)
    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"
    assert response.json() == receipt(body)
    return response.json()


async def checked_lookup(client, body, *, recorded=None, fenced=False):
    response = await client.post(LOOKUP, json=body)
    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"
    assert response.json() == lookup_result(body, recorded=recorded, fenced=fenced)


async def test_authority_is_durable_lineage_and_per_start_generation(tmp_path, settings_factory):
    path = tmp_path / "models.sqlite"
    first = settings_factory(path)
    original = authority(first)
    assert original["writable"] is True
    before = rows(first.db)
    await first.close()
    restarted = settings_factory(path)
    current = authority(restarted)
    assert current["backend_id"] == original["backend_id"]
    assert current["instance_id"] != original["instance_id"] and current["writable"] is True
    assert rows(restarted.db)["tables"]["settings"] == before["tables"]["settings"]
    replacement = authority(settings_factory(tmp_path / "different.sqlite"))
    assert replacement["backend_id"] != current["backend_id"]


async def test_simultaneous_startup_keeps_one_backend_and_one_active_writer(
    tmp_path, settings_factory
):
    path = tmp_path / "concurrent.sqlite"
    starting, registered, observed = (Barrier(2, timeout=10) for _ in range(3))

    def start():
        provider = ScriptedProvider([])
        provider.info = ProviderInfo(provider="synthetic-authority", model="original", local=True)
        controller = None
        try:
            starting.wait()
            controller = AgentModelSettings(provider, {}, path=path)
            registered.wait()
            snapshot = authority(controller)
            observed.wait()
            return snapshot
        finally:
            if controller is not None:
                asyncio.run(controller.close())
            assert provider.calls == []

    with ThreadPoolExecutor(max_workers=2) as workers:
        snapshots = list(workers.map(lambda _: start(), range(2)))
    assert snapshots[0]["backend_id"] == snapshots[1]["backend_id"]
    assert snapshots[0]["instance_id"] != snapshots[1]["instance_id"]
    assert sum(snapshot["writable"] for snapshot in snapshots) == 1
    assert authority(settings_factory(path))["backend_id"] == snapshots[0]["backend_id"]


async def test_replacement_database_refuses_original_mutation_and_lookup(
    tmp_path, settings_factory
):
    first = settings_factory(tmp_path / "original.sqlite")
    second = settings_factory(tmp_path / "replacement.sqlite")
    body = bound(first, "original-backend-save")
    assert first.snapshot()["revision"] == second.snapshot()["revision"] == 0
    assert first.snapshot()["profiles"] == second.snapshot()["profiles"]
    before = rows(second.db)
    async with api(second) as (client, _):
        for route in (WRITE, LOOKUP):
            response = await client.post(route, json=body)
            assert response.status_code == 409, response.text
            assert rows(second.db) == before
        await post_saved(client, bound(second, "replacement-own-save"))
    async with api(first) as (client, _):
        await post_saved(client, body)


@pytest.mark.parametrize(
    "purpose,harness", [(None, None), (None, "seed-v1"), ("compression", None)]
)
async def test_bound_exact_request_applies_once_and_digest_keeps_canonical_fields(
    tmp_path, settings_factory, purpose, harness
):
    settings = settings_factory(tmp_path / "models.sqlite")
    body = bound(settings, "exact-canonical", purpose=purpose, harness=harness)
    async with api(settings) as (client, _):
        saved = await post_saved(client, body)
        after = rows(settings.db)
        await post_saved(client, body)
        await checked_lookup(client, body, recorded=saved)
        assert rows(settings.db) == after
        conflict = {**body, "profile_id": "host-default"}
        for route in (WRITE, LOOKUP):
            response = await client.post(route, json=conflict)
            assert response.status_code == 409, response.text
            assert rows(settings.db) == after
    stored = settings.db.execute("SELECT key,digest,receipt FROM operations").fetchall()
    expected_digest = hashlib.sha256(
        json.dumps(canonical(body), sort_keys=True).encode()
    ).hexdigest()
    assert len(stored) == 1 and stored[0][:2] == (body["idempotency_key"], expected_digest)
    assert json.loads(stored[0][2]) == saved


async def test_restart_exact_replay_and_lookup_do_not_select_current_provider_or_harness(
    tmp_path, settings_factory, monkeypatch
):
    path = tmp_path / "models.sqlite"
    first = settings_factory(path)
    body = bound(first, "before-restart", harness="seed-v1")
    async with api(first) as (client, _):
        saved = await post_saved(client, body)
    await first.close()
    current = settings_factory(path, profiles=False)
    assert authority(current)["backend_id"] == body["expected_backend"]
    assert authority(current)["instance_id"] != body["expected_instance"]
    before = rows(current.db)
    forbidden = Mock(side_effect=AssertionError("Historical replay selected current configuration"))
    monkeypatch.setattr(current, "selection", forbidden)
    monkeypatch.setattr(current, "snapshot", forbidden)
    original_provider = object()
    runtime = SimpleNamespace(
        model_settings=current,
        integration_lock=asyncio.Lock(),
        provider=original_provider,
        session_harness=forbidden,
    )
    async with api(current, runtime=runtime) as (client, _):
        await post_saved(client, body)
        await checked_lookup(client, body, recorded=saved)
    forbidden.assert_not_called()
    assert runtime.provider is original_provider and rows(current.db) == before


async def test_restart_unobserved_old_request_is_permanently_fenced(tmp_path, settings_factory):
    path = tmp_path / "models.sqlite"
    first = settings_factory(path)
    body = bound(first, "never-observed")
    await first.close()
    current = settings_factory(path)
    assert authority(current)["backend_id"] == body["expected_backend"]
    before = rows(current.db)
    async with api(current) as (client, _):
        await checked_lookup(client, body, fenced=True)
        response = await client.post(WRITE, json=body)
        assert response.status_code == 409, response.text
        assert rows(current.db) == before
        await post_saved(client, bound(current, "current-generation"))


async def test_new_controller_retires_old_writer_and_old_close_does_not_clear_new_writer(
    tmp_path, settings_factory
):
    path = tmp_path / "models.sqlite"
    old = settings_factory(path)
    committed = bound(old, "old-committed")
    async with api(old) as (client, _):
        saved = await post_saved(client, committed)
    pending = bound(old, "old-uncommitted")
    current = settings_factory(path)
    assert authority(old)["backend_id"] == authority(current)["backend_id"]
    assert authority(old)["writable"] is False
    assert authority(current)["writable"] is True
    before = rows(current.db)
    async with api(old) as (client, _):
        await post_saved(client, committed)
        await checked_lookup(client, committed, recorded=saved)
        await checked_lookup(client, pending, fenced=True)
        response = await client.post(WRITE, json=pending)
        assert response.status_code == 409, response.text
    assert rows(current.db) == before
    await old.close()
    assert authority(current)["writable"] is True
    async with api(current) as (client, _):
        await post_saved(client, bound(current, "new-writer"))


async def test_lookup_fences_only_past_revision_never_a_future_revision(tmp_path, settings_factory):
    settings = settings_factory(tmp_path / "models.sqlite")
    present = bound(settings, "present-not-sent")
    future = bound(settings, "future-not-sent", revision=2)
    before = rows(settings.db)
    async with api(settings) as (client, _):
        await checked_lookup(client, present)
        await checked_lookup(client, future)
        assert rows(settings.db) == before
        response = await client.post(WRITE, json=future)
        assert response.status_code == 409, response.text
        assert rows(settings.db) == before
        await post_saved(client, bound(settings, "advance-one"))
        await checked_lookup(client, present, fenced=True)
        await checked_lookup(client, future)
        await post_saved(client, bound(settings, "advance-two"))
        await checked_lookup(client, future)
        saved = await post_saved(client, future)
        await checked_lookup(client, future, recorded=saved)


@pytest.mark.parametrize(
    "purpose,explicit_null", [(None, False), (None, True), ("compression", False)]
)
async def test_legacy_migration_replays_exact_original_digest_and_receipt_only(
    tmp_path, settings_factory, purpose, explicit_null
):
    path = tmp_path / "legacy.sqlite"
    body, saved = legacy_database(path, purpose=purpose, explicit_null=explicit_null)
    with sqlite3.connect(path) as connection:
        before = rows(connection)
    settings = settings_factory(path)
    observed = authority(settings)
    assert observed["writable"] is True and settings.snapshot()["revision"] == 1
    assert rows(settings.db)["tables"]["settings"] == before["tables"]["settings"]
    assert rows(settings.db)["tables"]["operations"] == before["tables"]["operations"]
    async with api(settings) as (client, _):
        await post_saved(client, body)
        response = await client.get("/v1/agent/options/model-changes/historical-save")
        assert response.status_code == 200 and response.json() == saved
        assert response.headers.get("cache-control") == "no-store"
    assert rows(settings.db)["tables"]["operations"] == before["tables"]["operations"]


@pytest.mark.parametrize(
    "purpose,explicit_null", [(None, False), (None, True), ("compression", False)]
)
async def test_legacy_exact_native_receipt_is_a_preserved_positive_control(
    tmp_path, settings_factory, purpose, explicit_null
):
    path = tmp_path / "legacy.sqlite"
    body, saved = legacy_database(path, purpose=purpose, explicit_null=explicit_null)
    settings = settings_factory(path)
    before = rows(settings.db)
    assert settings.change(ModelSettingsChange.model_validate(body)) == saved
    assert settings.receipt(body["idempotency_key"]) == saved
    assert rows(settings.db) == before


@pytest.mark.parametrize("purpose,harness", [(None, None), (None, "seed-v1"), ("analysis", None)])
async def test_new_unbound_legacy_operation_cannot_mutate(
    tmp_path, settings_factory, purpose, harness
):
    settings = settings_factory(tmp_path / "models.sqlite")
    body = {
        "schema_version": "1.0",
        "expected_revision": 0,
        "idempotency_key": "new-unbound-save",
        "harness_version": harness,
        "profile_id": "fast",
        "reasoning": None,
        **({"auxiliary_task": purpose} if purpose is not None else {}),
    }
    before = rows(settings.db)
    async with api(settings) as (client, _):
        response = await client.post(WRITE, json=body)
    assert response.status_code == 409, {
        "status": response.status_code,
        "rows_unchanged": rows(settings.db) == before,
    }
    assert rows(settings.db) == before


async def test_legacy_api_replay_precedes_current_provider_harness_and_assignment(
    tmp_path, settings_factory, monkeypatch
):
    path = tmp_path / "legacy.sqlite"
    body, saved = legacy_database(path)
    settings = settings_factory(path, profiles=False)
    before = rows(settings.db)
    forbidden = Mock(side_effect=AssertionError("Exact legacy receipt must precede selection"))
    monkeypatch.setattr(settings, "selection", forbidden)
    monkeypatch.setattr(settings, "snapshot", forbidden)
    original_provider = object()
    runtime = SimpleNamespace(
        model_settings=settings,
        integration_lock=asyncio.Lock(),
        provider=original_provider,
        session_harness=forbidden,
    )
    async with api(settings, runtime=runtime) as (client, _):
        response = await client.post(WRITE, json=body)
    assert response.status_code == 200 and response.json() == saved, response.text
    assert response.headers.get("cache-control") == "no-store"
    forbidden.assert_not_called()
    assert runtime.provider is original_provider and rows(settings.db) == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing-backend",
        "missing-instance",
        "null-backend",
        "upper-instance",
        "bool-revision",
        "float-revision",
        "string-revision",
        "extra",
    ],
)
async def test_bound_wire_refuses_missing_authority_and_coercion_without_changes(
    tmp_path, settings_factory, damage
):
    settings = settings_factory(tmp_path / "models.sqlite")
    body = {
        "schema_version": "1.1",
        "expected_backend": "b" * 32,
        "expected_instance": "c" * 32,
        "expected_revision": 0,
        "idempotency_key": "strict-wire",
        "profile_id": "host-default",
    }
    if damage == "missing-backend":
        body.pop("expected_backend")
    elif damage == "missing-instance":
        body.pop("expected_instance")
    elif damage == "null-backend":
        body["expected_backend"] = None
    elif damage == "upper-instance":
        body["expected_instance"] = "C" * 32
    elif damage == "extra":
        body["unbound_override"] = True
    else:
        body["expected_revision"] = {
            "bool-revision": True,
            "float-revision": 0.0,
            "string-revision": "0",
        }[damage]
    before = rows(settings.db)
    async with api(settings) as (client, _):
        for route in (WRITE, LOOKUP):
            response = await client.post(route, json=body)
            assert response.status_code == 422, response.text
    assert rows(settings.db) == before


@pytest.mark.parametrize("route", [WRITE, LOOKUP])
async def test_model_mutation_and_lookup_remain_owner_only(tmp_path, settings_factory, route):
    settings = settings_factory(tmp_path / "models.sqlite")
    body = {
        "schema_version": "1.1",
        "expected_backend": "b" * 32,
        "expected_instance": "c" * 32,
        "expected_revision": 0,
        "idempotency_key": "auth-boundary",
        "profile_id": "host-default",
    }
    before = rows(settings.db)
    async with api(settings) as (client, _):
        for credential, expected in (("", 401), ("Bearer " + "a" * 32, 403)):
            response = await client.post(route, json=body, headers={"Authorization": credential})
            assert response.status_code == expected, response.text
    assert rows(settings.db) == before


def authority_storage_cell(connection, identifier):
    """Locate the real metadata cell by its published value, without a schema oracle."""
    matches = []
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted(name)})")]
        for row in connection.execute(f"SELECT rowid,* FROM {quoted(name)}"):
            for column, value in zip(columns, row[1:], strict=True):
                if type(value) is str and identifier in value:
                    matches.append((name, column, row[0], value))
    assert len(matches) == 1, "Published backend must have one durable metadata source"
    return matches[0]


@pytest.mark.parametrize("damage", ["malformed-backend", "missing-migrated-row"])
async def test_migrated_identity_damage_refuses_without_silent_remint(
    tmp_path, settings_factory, damage
):
    path = tmp_path / "models.sqlite"
    settings = settings_factory(path)
    observed = authority(settings)
    await settings.close()
    with sqlite3.connect(path) as connection:
        name, column, rowid, value = authority_storage_cell(connection, observed["backend_id"])
        if damage == "malformed-backend":
            connection.execute(
                f"UPDATE {quoted(name)} SET {quoted(column)}=? WHERE rowid=?",
                (value.replace(observed["backend_id"], "f" * 31), rowid),
            )
        else:
            connection.execute(f"DELETE FROM {quoted(name)} WHERE rowid=?", (rowid,))
    with sqlite3.connect(path) as connection:
        before = rows(connection)
    with pytest.raises((ValueError, RuntimeError, sqlite3.Error)):
        settings_factory(path)
    with sqlite3.connect(path) as connection:
        assert rows(connection) == before


@pytest.mark.parametrize("damage", ["invalid-json", "different-key", "boolean-revision"])
async def test_corrupt_legacy_receipt_is_unavailable_never_a_success_or_absence(
    tmp_path, settings_factory, damage
):
    path = tmp_path / "legacy.sqlite"
    body, saved = legacy_database(path)
    settings = settings_factory(path)
    corrupted = dict(saved)
    if damage == "different-key":
        corrupted["idempotency_key"] = "not-the-request"
    elif damage == "boolean-revision":
        corrupted["revision"] = True
    value = "not-json" if damage == "invalid-json" else json.dumps(corrupted)
    with settings.db:
        settings.db.execute("UPDATE operations SET receipt=?", (value,))
    before = rows(settings.db)
    async with api(settings) as (client, _):
        for route, payload in (
            (WRITE, body),
            ("/v1/agent/options/model-changes/historical-save", None),
        ):
            response = (
                await client.get(route)
                if payload is None
                else await client.post(route, json=payload)
            )
            assert response.status_code == 503, response.text
    assert rows(settings.db) == before


class SqlFailure:
    """Inject only around actual SQLite DML/commit; retain dirty-row fault evidence."""

    def __init__(self, connection, phase, *, rollback_failure=False):
        self.connection, self.phase = connection, phase
        self.rollback_failure = rollback_failure
        self.fired = self.rollback_attempted = self.closed = False
        self.changed_rows = None
        self.rows_at_failure = None

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("commit"):
            return self.commit()
        if normalized.startswith("rollback"):
            return self.rollback()
        if self.phase == "begin" and normalized.startswith("begin"):
            self.fired = True
            raise sqlite3.OperationalError("Synthetic begin failure")
        result = self.connection.execute(sql, *args)
        target = (
            "settings"
            if normalized.startswith("update settings ")
            else "operations"
            if normalized.startswith("insert into operations")
            else None
        )
        if self.phase == target:
            self.fired = True
            self.changed_rows = result.rowcount
            self.rows_at_failure = rows(self.connection)
            assert self.connection.in_transaction and result.rowcount == 1
            raise RuntimeError("Synthetic failure after real model-settings DML")
        return result

    def commit(self):
        if self.phase == "commit":
            self.fired = True
            self.rows_at_failure = rows(self.connection)
            assert self.connection.in_transaction
            assert len(self.rows_at_failure["tables"]["operations"]) == 1
            assert self.rows_at_failure["tables"]["settings"][0][1] == 1
            raise RuntimeError("Synthetic commit failure before actual commit")
        return self.connection.commit()

    def rollback(self):
        self.rollback_attempted = True
        if self.rollback_failure:
            raise RuntimeError("Synthetic rollback failure")
        return self.connection.rollback()

    def close(self):
        self.closed = True
        return self.connection.close()


@pytest.mark.parametrize("phase", ["begin", "settings", "operations", "commit"])
async def test_failed_native_transaction_never_leaves_partial_state_or_dirty_reuse(
    tmp_path, settings_factory, phase
):
    path = tmp_path / "models.sqlite"
    settings = settings_factory(path)
    body = bound(settings, "transaction-fault")
    connection = settings.db
    before = rows(connection)
    failure = SqlFailure(connection, phase)
    settings.db = failure
    async with api(settings) as (client, _):
        response = await client.post(WRITE, json=body)
        assert failure.fired, "Valid native application must reach the requested SQL fault"
        assert response.status_code == 503, response.text
        if phase != "begin":
            assert failure.rows_at_failure != before and failure.rollback_attempted
        if not failure.closed:
            assert not connection.in_transaction and rows(connection) == before
        with sqlite3.connect(path) as independent:
            assert rows(independent) == before
    if not failure.closed:
        settings.db = connection
        async with api(settings) as (client, _):
            await checked_lookup(client, body)
            await post_saved(client, body)


async def test_rollback_failure_closes_unsafe_connection_and_preserves_durable_rows(
    tmp_path, settings_factory
):
    path = tmp_path / "models.sqlite"
    settings = settings_factory(path)
    body = bound(settings, "rollback-fault")
    connection = settings.db
    before = rows(connection)
    failure = SqlFailure(connection, "operations", rollback_failure=True)
    settings.db = failure
    async with api(settings) as (client, _):
        response = await client.post(WRITE, json=body)
        assert failure.fired and failure.changed_rows == 1 and failure.rows_at_failure != before
        assert response.status_code == 503, response.text
        assert failure.rollback_attempted and failure.closed
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with sqlite3.connect(path) as independent:
            assert rows(independent) == before
        response = await client.post(LOOKUP, json=body)
        assert response.status_code == 503, response.text
