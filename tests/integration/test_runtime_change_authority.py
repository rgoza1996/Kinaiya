"""Independent #213 API/SQLite oracles with synthetic policy and MCP fixtures.

Frozen before production integration. No provider, worker, subprocess, external
service, user configuration, or existing test fixture is invoked or changed.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from core.policy import ToolPolicy
from services.agent.options_api import router
from services.agent.runtime_settings import AgentRuntimeSettings
from services.common.auth import Auth

WRITE = "/v1/agent/options/runtime"
LOOKUP = "/v1/agent/options/runtime-changes/lookup"
AUTH = Auth({"owner": "o" * 32, "agent": "a" * 32})


@pytest.fixture
def make_env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("nested", r"literal\segment", " room café "):
        (workspace / name).mkdir()
    controllers = []

    def create(name="runtime.sqlite", *, retirement="controller", yolo=False):
        retire = AsyncMock()
        runtime = SimpleNamespace(
            policy=ToolPolicy(workspace, yolo=yolo),
            max_steps=12,
            integration_lock=asyncio.Lock(),
            runtime_settings_busy=Mock(return_value=False),
            mcp_controller=SimpleNamespace(retire_connections=retire)
            if retirement == "controller"
            else None,
            mcp_host=SimpleNamespace(reset_connections=retire) if retirement == "host" else None,
        )
        settings = AgentRuntimeSettings(runtime, path=tmp_path / name)
        controllers.append(settings)
        runtime.runtime_settings = settings
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600
        return SimpleNamespace(
            runtime=runtime,
            settings=settings,
            retire=retire,
            path=tmp_path / name,
            workspace=workspace,
        )

    yield create
    for controller in reversed(controllers):
        controller.close()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def canonical(body):
    result = {**body, "settings": dict(body["settings"])}
    result["settings"]["working_directory"] = Path(
        result["settings"]["working_directory"]
    ).as_posix()
    return result


def preferences(directory="."):
    return {
        "file_writes": "deny",
        "shell_commands": "allow",
        "tool_network": False,
        "max_steps": 19,
        "shell_timeout_s": 15,
        "working_directory": directory,
    }


def authority(env):
    snapshot = env.settings.snapshot()
    value = snapshot["write_authority"]
    assert snapshot["schema_version"] == "1.0"
    assert set(value) == {"mutation_version", "backend_id", "instance_id", "writable"}
    assert value["mutation_version"] == "1.1"
    assert type(value["backend_id"]) is str and re.fullmatch(r"[a-f0-9]{32}", value["backend_id"])
    assert value["instance_id"] == snapshot["instance_id"] == str(uuid.UUID(value["instance_id"]))
    assert len(value["instance_id"]) == 36 and type(value["writable"]) is bool
    return value


def request(env, key, *, bound=False, settings=None, yolo=False, revision=None):
    snapshot = env.settings.snapshot()
    body = {
        "schema_version": "1.1" if bound else "1.0",
        "expected_revision": snapshot["revision"] if revision is None else revision,
        "expected_state": snapshot["state_token"],
        "idempotency_key": key,
        "settings": settings or preferences(),
        "yolo": yolo,
    }
    if bound:
        observed = authority(env)
        body.update(
            expected_backend=observed["backend_id"], expected_instance=observed["instance_id"]
        )
    return body


def receipt(body, instance):
    value = canonical(body)
    result = {
        "schema_version": body["schema_version"],
        "status": "settings_saved",
        "revision": body["expected_revision"] + 1,
        "idempotency_key": body["idempotency_key"],
        "settings": value["settings"],
        "requested_yolo": body["yolo"],
        "service_instance": instance,
        "change_boundary": "settled_service",
    }
    if body["schema_version"] == "1.1":
        result["backend_id"] = body["expected_backend"]
        assert instance == body["expected_instance"]
    return result


def rows(connection):
    return {
        "marker": connection.execute("PRAGMA user_version").fetchone()[0],
        "tables": {
            name: tuple(connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid'))
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        },
    }


def policy_values(env):
    policy = env.runtime.policy
    return (
        policy.allow_write,
        policy.deny_write,
        policy.allow_shell,
        policy.deny_shell,
        policy.network,
        policy.yolo,
        policy.shell_working_directory,
        policy.shell_timeout_s,
        env.runtime.max_steps,
    )


@asynccontextmanager
async def api(env):
    app = FastAPI()
    app.include_router(router(env.runtime, AUTH))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://synthetic-runtime.invalid",
        headers={"Authorization": "Bearer " + "o" * 32},
    ) as client:
        yield client


async def saved(client, body, instance):
    response = await client.post(WRITE, json=body)
    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"
    assert response.json() == receipt(body, instance)
    return response.json()


async def observed(client, body, backend, *, recorded=None, fenced=False):
    response = await client.post(LOOKUP, json=body)
    assert response.status_code == 200, response.text
    assert response.headers.get("cache-control") == "no-store"
    assert response.json() == {
        "schema_version": "1.1",
        "backend_id": backend,
        "request": canonical(body),
        "status": "recorded" if recorded is not None else "unobserved",
        "receipt": recorded,
        "mutation_fenced": recorded is not None or fenced,
    }


def forbid_reapply(env, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Historical proof consulted current policy"))
    for name in ("_current", "_state", "_check", "_apply"):
        monkeypatch.setattr(env.settings, name, forbidden)
    monkeypatch.setattr(env.runtime, "runtime_settings_busy", forbidden)
    env.retire.side_effect = AssertionError("Historical proof retired MCP again")
    return forbidden


@pytest.mark.parametrize("directory", [".", "./nested//.", r"literal\segment", " room café /."])
async def test_legacy_first_write_preserves_normalized_digest_receipt_and_precise_modes(
    make_env, directory
):
    env = make_env()
    initial = env.settings.snapshot()
    policy = env.runtime.policy
    assert initial["state_token"] == digest(
        {
            "instance": initial["instance_id"],
            "settings": initial["settings"],
            "yolo": False,
            "workspace": str(policy.workspace),
            "protected": [str(path) for path in policy.protected_roots],
            "readonly": [str(path) for path in policy.readonly_roots],
        }
    )
    body = request(env, "legacy-first", settings=preferences(directory), yolo=True)
    async with api(env) as client:
        result = await saved(client, body, initial["instance_id"])
        after = rows(env.settings.db)
        await saved(client, body, initial["instance_id"])
        assert rows(env.settings.db) == after
    stored = env.settings.db.execute("SELECT key,digest,receipt FROM operations").fetchall()
    assert len(stored) == 1 and stored[0][:2] == (body["idempotency_key"], digest(canonical(body)))
    assert json.loads(stored[0][2]) == result
    assert policy.deny_write and not policy.allow_write and not policy.effective_write
    assert policy.allow_shell and policy.effective_shell and policy.yolo
    assert policy.shell_working_directory == canonical(body)["settings"]["working_directory"]
    env.retire.assert_awaited_once_with(actor="owner")


async def test_legacy_restart_receipt_never_restores_old_yolo_or_retires_mcp(make_env, monkeypatch):
    first = make_env(retirement="host")
    instance = first.settings.snapshot()["instance_id"]
    body = request(first, "legacy-restart", yolo=True)
    async with api(first) as client:
        original = await saved(client, body, instance)
    first.settings.close()
    current = make_env(retirement="host")
    assert current.settings.snapshot()["instance_id"] != instance
    assert current.runtime.policy.yolo is False and current.runtime.policy.deny_write
    before = rows(current.settings.db), policy_values(current)
    forbidden = forbid_reapply(current, monkeypatch)
    async with api(current) as client:
        await saved(client, body, instance)
        response = await client.get("/v1/agent/options/runtime-changes/legacy-restart")
        assert response.status_code == 200 and response.json() == original
    assert (rows(current.settings.db), policy_values(current)) == before
    forbidden.assert_not_called()
    current.retire.assert_not_awaited()


async def test_legacy_same_key_different_complete_body_refuses_without_policy_change(make_env):
    env = make_env()
    body = request(env, "legacy-conflict")
    async with api(env) as client:
        await saved(client, body, env.settings.instance)
        before = rows(env.settings.db), policy_values(env)
        response = await client.post(WRITE, json={**body, "yolo": True})
        assert response.status_code == 409, response.text
    assert (rows(env.settings.db), policy_values(env)) == before


async def test_bound_exact_receipt_and_full_request_lookup_do_not_reapply(make_env, monkeypatch):
    env = make_env()
    body = request(env, "bound-exact", bound=True)
    async with api(env) as client:
        original = await saved(client, body, body["expected_instance"])
        before = rows(env.settings.db), policy_values(env)
        forbidden = forbid_reapply(env, monkeypatch)
        await saved(client, body, body["expected_instance"])
        await observed(client, body, body["expected_backend"], recorded=original)
        response = await client.post(LOOKUP, json={**body, "yolo": True})
        assert response.status_code == 409, response.text
    assert (rows(env.settings.db), policy_values(env)) == before
    forbidden.assert_not_called()


async def test_bound_foreign_runtime_database_cannot_apply_or_prove_absence(make_env):
    first, second = make_env("first.sqlite"), make_env("second.sqlite")
    body = request(first, "original-backend", bound=True)
    assert first.settings.snapshot()["revision"] == second.settings.snapshot()["revision"] == 0
    assert first.settings.snapshot()["settings"] == second.settings.snapshot()["settings"]
    assert authority(first)["backend_id"] != authority(second)["backend_id"]
    before = rows(second.settings.db), policy_values(second)
    async with api(second) as client:
        for route in (WRITE, LOOKUP):
            response = await client.post(route, json=body)
            assert response.status_code == 409, response.text
            assert (rows(second.settings.db), policy_values(second)) == before
        own = request(second, "second-own", bound=True)
        await saved(client, own, own["expected_instance"])


async def test_bound_same_database_restart_preserves_original_receipt_identity(
    make_env, monkeypatch
):
    first = make_env()
    body = request(first, "bound-restart", bound=True, yolo=True)
    async with api(first) as client:
        original = await saved(client, body, body["expected_instance"])
    first.settings.close()
    current = make_env()
    active = authority(current)
    assert active["backend_id"] == body["expected_backend"]
    assert active["instance_id"] != body["expected_instance"] and active["writable"]
    assert current.runtime.policy.yolo is False
    before = rows(current.settings.db), policy_values(current)
    forbidden = forbid_reapply(current, monkeypatch)
    async with api(current) as client:
        await saved(client, body, body["expected_instance"])
        await observed(client, body, active["backend_id"], recorded=original)
    assert (rows(current.settings.db), policy_values(current)) == before
    forbidden.assert_not_called()


async def test_uncommitted_bound_original_instance_is_permanently_fenced_after_restart(make_env):
    first = make_env()
    body = request(first, "bound-uncommitted", bound=True)
    first.settings.close()
    current = make_env()
    assert authority(current)["backend_id"] == body["expected_backend"]
    before = rows(current.settings.db), policy_values(current)
    async with api(current) as client:
        await observed(client, body, body["expected_backend"], fenced=True)
        response = await client.post(WRITE, json=body)
        assert response.status_code == 409, response.text
    assert (rows(current.settings.db), policy_values(current)) == before


async def test_older_live_controller_cannot_apply_even_a_matching_legacy_state_token(make_env):
    old = make_env()
    body = request(old, "legacy-retired")
    current = make_env()
    assert old.settings.snapshot()["state_token"] == body["expected_state"]
    before = rows(current.settings.db), policy_values(old)
    async with api(old) as client:
        response = await client.post(WRITE, json=body)
    assert response.status_code == 409, {
        "status": response.status_code,
        "rows_unchanged": rows(current.settings.db) == before[0],
    }
    assert (rows(current.settings.db), policy_values(old)) == before
    old.settings.close()
    assert authority(current)["writable"] is True
    async with api(current) as client:
        own = request(current, "current-legacy")
        await saved(client, own, current.settings.instance)


async def test_writer_generation_is_rechecked_after_actual_mcp_retirement_await(make_env):
    old = make_env()
    body = request(old, "retired-during-await")
    created = []

    async def new_writer(*, actor):
        assert actor == "owner"
        created.append(make_env())

    old.retire.side_effect = new_writer
    before_policy = policy_values(old)
    async with api(old) as client:
        response = await client.post(WRITE, json=body)
    assert len(created) == 1
    assert response.status_code == 409, response.text
    assert created[0].settings.db.execute("SELECT revision FROM settings").fetchone()[0] == 0
    assert created[0].settings.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    assert policy_values(old) == before_policy


@pytest.mark.parametrize("mutable_state", ["yolo", "path", "busy"])
async def test_unobserved_mutable_state_and_aba_are_never_permanent_fences(make_env, mutable_state):
    env = make_env()
    body = request(env, "state-aba", bound=True)
    original_state = body["expected_state"]
    if mutable_state == "yolo":
        env.runtime.policy.yolo = True
    elif mutable_state == "path":
        env.runtime.policy.shell_working_directory = "missing-directory"
    else:
        env.runtime.runtime_settings_busy.return_value = True
    before = rows(env.settings.db)
    async with api(env) as client:
        await observed(client, body, body["expected_backend"])
        assert rows(env.settings.db) == before
        response = await client.post(WRITE, json=body)
        assert response.status_code == 409, response.text
        env.runtime.policy.yolo = False
        env.runtime.policy.shell_working_directory = "."
        env.runtime.runtime_settings_busy.return_value = False
        assert env.settings.snapshot()["state_token"] == original_state
        await observed(client, body, body["expected_backend"])
        await saved(client, body, body["expected_instance"])


async def test_future_revision_does_not_fence_unobserved_request(make_env):
    env = make_env()
    unchanged = env.settings.snapshot()["settings"]
    future = request(env, "future", bound=True, settings=unchanged, revision=2)
    old = request(env, "past", bound=True, settings=unchanged)
    async with api(env) as client:
        await observed(client, future, future["expected_backend"])
        for index in range(2):
            body = request(env, f"advance-{index}", bound=True, settings=unchanged)
            await saved(client, body, body["expected_instance"])
            await observed(client, future, future["expected_backend"])
        await observed(client, old, old["expected_backend"], fenced=True)
        await saved(client, future, future["expected_instance"])


async def test_legacy_full_request_lookup_does_not_invent_authority_for_absence(make_env):
    first = make_env()
    legacy = request(first, "legacy-recorded", yolo=True)
    absent = request(first, "legacy-absent")
    async with api(first) as client:
        original = await saved(client, legacy, first.settings.instance)
    first.settings.close()
    current = make_env()
    backend = authority(current)["backend_id"]
    async with api(current) as client:
        await observed(client, legacy, backend, recorded=original)
        await observed(client, absent, backend)


@pytest.mark.parametrize(
    "damage",
    [
        "missing-backend",
        "missing-instance",
        "wrong-uuid",
        "bool-revision",
        "float-revision",
        "string-revision",
        "bool-state",
        "extra",
    ],
)
async def test_bound_wire_rejects_authority_omission_and_coercion(make_env, damage):
    env = make_env()
    body = {
        **request(env, "strict-wire"),
        "schema_version": "1.1",
        "expected_backend": "b" * 32,
        "expected_instance": "11111111-2222-4333-8444-555555555555",
    }
    if damage == "missing-backend":
        body.pop("expected_backend")
    elif damage == "missing-instance":
        body.pop("expected_instance")
    elif damage == "wrong-uuid":
        body["expected_instance"] = "a" * 32
    elif damage == "bool-state":
        body["expected_state"] = True
    elif damage == "extra":
        body["permission_grant"] = "all"
    else:
        body["expected_revision"] = {
            "bool-revision": True,
            "float-revision": 0.0,
            "string-revision": "0",
        }[damage]
    before = rows(env.settings.db), policy_values(env)
    async with api(env) as client:
        for route in (WRITE, LOOKUP):
            response = await client.post(route, json=body)
            assert response.status_code == 422, response.text
    assert (rows(env.settings.db), policy_values(env)) == before


@pytest.mark.parametrize("route", [WRITE, LOOKUP])
async def test_runtime_mutation_and_full_request_lookup_are_owner_only(make_env, route):
    env = make_env()
    body = request(env, "owner-only")
    before = rows(env.settings.db)
    async with api(env) as client:
        for credential, code in (("", 401), ("Bearer " + "a" * 32, 403)):
            response = await client.post(route, json=body, headers={"Authorization": credential})
            assert response.status_code == code, response.text
    assert rows(env.settings.db) == before


@pytest.mark.parametrize("damage", ["backend", "missing-row"])
async def test_migrated_authority_damage_never_mints_new_runtime_identity(make_env, damage):
    env = make_env()
    published = authority(env)["backend_id"]
    env.settings.close()
    with sqlite3.connect(env.path) as connection:
        if damage == "backend":
            connection.execute("UPDATE runtime_write_authority SET backend_id=?", (published[:-1],))
        else:
            connection.execute("DELETE FROM runtime_write_authority")
    with sqlite3.connect(env.path) as connection:
        before = rows(connection)
    with pytest.raises((RuntimeError, ValueError, sqlite3.Error)):
        make_env()
    with sqlite3.connect(env.path) as connection:
        assert rows(connection) == before


@pytest.mark.parametrize("damage", ["different-key", "bool-revision", "duplicate", "deep", "json"])
async def test_corrupt_legacy_receipt_refuses_fixed_unknown_instead_of_success(make_env, damage):
    env = make_env()
    body = request(env, "corrupt-receipt")
    async with api(env) as client:
        original = await saved(client, body, env.settings.instance)
    altered = dict(original)
    if damage == "different-key":
        altered["idempotency_key"] = "different-operation"
    elif damage == "bool-revision":
        altered["revision"] = True
    value = json.dumps(altered)
    if damage == "duplicate":
        value = '{"schema_version":"bad",' + value[1:]
    elif damage == "deep":
        value = "[" * 1500 + "]" * 1500
    elif damage == "json":
        value = "not-json"
    with env.settings.db:
        env.settings.db.execute("UPDATE operations SET receipt=?", (value,))
    before = rows(env.settings.db), policy_values(env)
    async with api(env) as client:
        for route in ("/v1/agent/options/runtime-changes/corrupt-receipt", WRITE):
            response = (
                await client.post(route, json=body) if route == WRITE else await client.get(route)
            )
            assert response.status_code == 503, response.text
            assert (
                "unconfirmed" in response.json()["detail"].lower()
                or "unavailable" in response.json()["detail"].lower()
            )
    assert (rows(env.settings.db), policy_values(env)) == before


class SqlFault:
    """Raise only around real SQL writes/fetches/commits; prove the fault fired."""

    def __init__(self, connection, phase, *, bad_rollback=False):
        self.connection, self.phase = connection, phase
        self.bad_rollback = bad_rollback
        self.fired = self.closed = self.rollback_attempted = False
        self.mutated = False
        self.at_fault = None

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        normalized = " ".join(sql.lower().split())
        if self.phase == "begin" and normalized.startswith("begin"):
            self.fired = True
            raise RuntimeError("Synthetic private begin fault")
        result = self.connection.execute(sql, *args)
        target = (
            "settings"
            if normalized.startswith("update settings ")
            else "operations"
            if normalized.startswith("insert into operations")
            else None
        )
        if target is not None:
            self.mutated = True
        receipt_read = (
            normalized.startswith("select ")
            and "receipt" in normalized
            and "from operations" in normalized
        )
        if self.phase == target or (self.phase == "receipt" and receipt_read):
            self.fired = True
            self.at_fault = rows(self.connection)
            raise RuntimeError("Synthetic private SQL adapter fault")
        return result

    def commit(self):
        if self.mutated and self.phase in {"commit-before", "commit-after"}:
            self.fired = True
            self.at_fault = rows(self.connection)
            assert self.connection.in_transaction
            if self.phase == "commit-after":
                self.connection.commit()
            raise RuntimeError("Synthetic private commit acknowledgment fault")
        return self.connection.commit()

    def rollback(self):
        self.rollback_attempted = True
        if self.bad_rollback:
            raise RuntimeError("Synthetic private rollback fault")
        return self.connection.rollback()

    def close(self):
        self.closed = True
        return self.connection.close()


@pytest.mark.parametrize(
    "phase", ["begin", "settings", "operations", "commit-before", "commit-after"]
)
async def test_actual_storage_failure_is_unknown_with_clean_state_or_exact_committed_receipt(
    make_env, monkeypatch, phase
):
    env = make_env()
    body = request(env, "storage-fault", yolo=True)
    connection = env.settings.db
    before, before_policy = rows(connection), policy_values(env)
    fault = SqlFault(connection, phase)
    env.settings.db = fault
    async with api(env) as client:
        response = await client.post(WRITE, json=body)
    assert fault.fired and response.status_code == 503, response.text
    if phase != "begin":
        assert fault.mutated and fault.at_fault != before
    assert policy_values(env) == before_policy
    assert not connection.in_transaction
    if phase == "commit-after":
        original = receipt(body, env.settings.instance)
        assert (
            json.loads(connection.execute("SELECT receipt FROM operations").fetchone()[0])
            == original
        )
        env.settings.db = connection
        forbidden = forbid_reapply(env, monkeypatch)
        async with api(env) as client:
            await saved(client, body, env.settings.instance)
        forbidden.assert_not_called()
    else:
        assert rows(connection) == before
        with sqlite3.connect(env.path) as independent:
            assert rows(independent) == before
    env.settings.db = connection


async def test_rollback_failure_closes_poisoned_connection_instead_of_publishing_dirty_receipt(
    make_env,
):
    env = make_env()
    body = request(env, "dirty-rollback")
    connection = env.settings.db
    before = rows(connection), policy_values(env)
    fault = SqlFault(connection, "operations", bad_rollback=True)
    env.settings.db = fault
    async with api(env) as client:
        response = await client.post(WRITE, json=body)
        assert fault.fired and fault.at_fault != before[0] and fault.rollback_attempted
        assert response.status_code == 503, response.text
        assert fault.closed
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        response = await client.get("/v1/agent/options/runtime-changes/dirty-rollback")
        assert response.status_code == 503, response.text
    with sqlite3.connect(env.path) as independent:
        assert rows(independent) == before[0]
    assert policy_values(env) == before[1]


async def test_actual_receipt_read_runtime_error_is_storage_unknown_not_raw_500(make_env):
    env = make_env()
    body = request(env, "read-fault")
    async with api(env) as client:
        await saved(client, body, env.settings.instance)
    connection = env.settings.db
    before = rows(connection)
    fault = SqlFault(connection, "receipt")
    env.settings.db = fault
    async with api(env) as client:
        response = await client.get("/v1/agent/options/runtime-changes/read-fault")
    assert fault.fired and response.status_code == 503, response.text
    assert "Synthetic private" not in response.text
    assert rows(connection) == before
    env.settings.db = connection
