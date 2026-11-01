"""Codex-authored SQLite model preference identity and transaction checks."""

import asyncio
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from packages.contracts.models import ProviderInfo
from packages.providers.text import ScriptedProvider
from services.agent.model_settings import (
    AgentModelSettings,
    BoundModelSettingsChange,
    ModelSettingsChange,
    ModelSettingsConflict,
    ModelSettingsUnavailable,
)
from services.agent.options_api import router
from services.common.auth import Auth


def provider(name="host", effort=None):
    result = ScriptedProvider([])
    result.info = ProviderInfo(provider="synthetic", model=name, local=True)
    result.reasoning_effort = effort
    return result


@pytest.fixture
async def make_settings():
    owned = []

    def make(path=None, **kwargs):
        settings = AgentModelSettings(
            provider(),
            kwargs.pop("factories", {"other": lambda effort: provider("other", effort)}),
            path=path,
            reasoning_levels=kwargs.pop("reasoning_levels", {"other": ["low", "high"]}),
            **kwargs,
        )
        owned.append(settings)
        return settings

    yield make
    for settings in reversed(owned):
        await settings.close()


def change(settings, key="save", revision=None, **kwargs):
    return BoundModelSettingsChange(
        schema_version="1.1",
        expected_backend=settings.backend_id,
        expected_instance=settings.instance,
        expected_revision=settings.snapshot()["revision"] if revision is None else revision,
        idempotency_key=key,
        profile_id=kwargs.pop("profile_id", "other"),
        **kwargs,
    )


def canonical(request):
    return request.model_dump(
        exclude={"auxiliary_task"} if request.auxiliary_task is None else set()
    )


def fail_selection(*args, **kwargs):
    raise AssertionError("A historical receipt cannot select current providers or eligibility")


def legacy_database(path, purpose=None):
    request = ModelSettingsChange(
        schema_version="1.0",
        expected_revision=0,
        idempotency_key="legacy",
        profile_id="other",
        auxiliary_task=purpose,
    )
    receipt = {
        "schema_version": "1.0",
        "idempotency_key": "legacy",
        "revision": 1,
        "harness_version": None,
        "binding": {"profile_id": "other", "reasoning": None},
        "change_boundary": "new_tasks",
        **({"auxiliary_task": purpose} if purpose is not None else {}),
    }
    raw_receipt = json.dumps(receipt, indent=3)
    body = {
        "default": {
            "profile_id": "other" if purpose is None else "host-default",
            "reasoning": None,
        },
        "harnesses": {},
    }
    if purpose is not None:
        body.update(auxiliary={purpose: receipt["binding"]}, harness_auxiliary={})
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id=1), "
            "revision INTEGER NOT NULL, body TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL)"
        )
        db.execute("INSERT INTO settings VALUES (1, 1, ?)", (json.dumps(body),))
        db.execute(
            "INSERT INTO operations VALUES (?, ?, ?)",
            (
                "legacy",
                hashlib.sha256(json.dumps(canonical(request), sort_keys=True).encode()).hexdigest(),
                raw_receipt,
            ),
        )
    return request, receipt, raw_receipt


async def test_bound_receipt_and_canonical_digest_commit_once(make_settings):
    settings = make_settings()
    authority = settings.snapshot()["write_authority"]
    assert authority == {
        "mutation_version": "1.1",
        "backend_id": settings.backend_id,
        "instance_id": settings.instance,
        "writable": True,
    }
    request = change(settings, reasoning="high", harness_version="seed-v1")
    receipt, applied = settings.change_with_status(request)
    assert applied is True
    assert receipt == {
        "schema_version": "1.1",
        "idempotency_key": request.idempotency_key,
        "revision": 1,
        "harness_version": "seed-v1",
        "binding": {"profile_id": "other", "reasoning": "high"},
        "change_boundary": "new_tasks",
        "backend_id": request.expected_backend,
        "service_instance": request.expected_instance,
    }
    assert settings.snapshot()["schema_version"] == "1.0"
    assert settings.selection("seed-v1")[1].model == "other"
    assert settings.change_with_status(request) == (receipt, False)
    row = settings.db.execute("SELECT digest, receipt FROM operations").fetchone()
    assert (
        row[0]
        == hashlib.sha256(json.dumps(canonical(request), sort_keys=True).encode()).hexdigest()
    )
    assert "auxiliary_task" not in canonical(request)
    assert json.loads(row[1]) == receipt
    assert settings.snapshot()["revision"] == 1
    assert all(not candidate.calls for candidate in settings.instances.values())


@pytest.mark.parametrize(
    "mutation",
    [
        {"expected_backend": "a" * 31},
        {"expected_backend": "A" * 32},
        {"expected_backend": "a" * 32 + "\n"},
        {"expected_instance": None},
        {"expected_instance": 1},
        {"expected_revision": True},
        {"schema_version": "1.0"},
        {"unexpected": "field"},
    ],
)
async def test_bound_wire_is_strict(make_settings, mutation):
    request = change(make_settings()).model_dump()
    with pytest.raises(ValidationError):
        BoundModelSettingsChange(**{**request, **mutation})
    with pytest.raises(ValidationError):
        ModelSettingsChange(**request)


async def test_new_unbound_change_refuses_without_selection_or_receipt(make_settings, monkeypatch):
    settings = make_settings()
    monkeypatch.setattr(settings, "resolve", fail_selection)
    request = ModelSettingsChange(
        schema_version="1.0", expected_revision=0, idempotency_key="unbound", profile_id="other"
    )
    with pytest.raises(ModelSettingsConflict):
        settings.change(request)
    assert settings.db.execute("SELECT revision FROM settings").fetchone()[0] == 0
    assert settings.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


@pytest.mark.parametrize("purpose", [None, "analysis"])
async def test_first_migration_preserves_legacy_request_and_receipt_bytes(
    tmp_path, make_settings, monkeypatch, purpose
):
    path = tmp_path / "legacy.sqlite"
    request, receipt, raw = legacy_database(path, purpose)
    settings = make_settings(path, factories={}, reasoning_levels={})
    monkeypatch.setattr(settings, "resolve", fail_selection)
    monkeypatch.setattr(settings, "resolve_auxiliary", fail_selection)
    assert settings.change(request, validate_harness=fail_selection) == receipt
    recorded, actual = settings.recorded_change("legacy")
    assert type(recorded) is ModelSettingsChange and canonical(recorded) == canonical(request)
    assert actual == receipt
    assert settings.db.execute("SELECT receipt FROM operations").fetchone()[0] == raw
    assert settings.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "expected_backend" not in canonical(recorded)
    assert "expected_instance" not in canonical(recorded)


async def test_replacement_backend_cannot_accept_or_settle_another_backend(
    tmp_path, make_settings, monkeypatch
):
    original = make_settings(tmp_path / "original.sqlite")
    replacement = make_settings(tmp_path / "replacement.sqlite")
    request = change(original)
    monkeypatch.setattr(replacement, "resolve", fail_selection)
    assert original.backend_id != replacement.backend_id
    with pytest.raises(ModelSettingsConflict):
        replacement.change(request)
    with pytest.raises(ModelSettingsConflict):
        replacement.lookup(request)
    assert replacement.snapshot()["revision"] == 0
    assert replacement.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


async def test_restart_keeps_receipt_identity_and_retires_old_first_application(
    tmp_path, make_settings, monkeypatch
):
    path = tmp_path / "settings.sqlite"
    old = make_settings(path)
    request = change(old, harness_version="seed-v1", reasoning="high", auxiliary_task="analysis")
    receipt = old.change(request)
    unfinished = change(old, "unfinished")
    current = make_settings(path)
    assert current.backend_id == old.backend_id and current.instance != old.instance
    assert old.snapshot()["write_authority"]["writable"] is False
    assert current.snapshot()["write_authority"]["writable"] is True
    monkeypatch.setattr(current, "resolve", fail_selection)
    monkeypatch.setattr(current, "resolve_auxiliary", fail_selection)
    assert current.change(request, validate_harness=fail_selection) == receipt
    assert old.change(request, validate_harness=fail_selection) == receipt
    with pytest.raises(ModelSettingsConflict):
        current.change(unfinished)
    with pytest.raises(ModelSettingsConflict):
        old.change(unfinished)
    assert current.lookup(request) == {
        "schema_version": "1.1",
        "backend_id": old.backend_id,
        "request": canonical(request),
        "status": "recorded",
        "receipt": receipt,
        "mutation_fenced": True,
    }
    absence = current.lookup(unfinished)
    assert absence["status"] == "unobserved" and absence["mutation_fenced"] is True
    await old.close()
    assert (
        current.db.execute("SELECT active_instance FROM model_write_authority").fetchone()[0]
        == current.instance
    )


async def test_old_controller_cannot_use_new_instances_authority(tmp_path, make_settings):
    path = tmp_path / "settings.sqlite"
    old = make_settings(path)
    current = make_settings(path)
    request = change(current)
    with pytest.raises(ModelSettingsConflict):
        old.change(request)
    assert current.change(request)["service_instance"] == current.instance


async def test_unobserved_future_or_equal_revision_is_not_fenced(make_settings):
    settings = make_settings()
    future = change(settings, "future", revision=2)
    for revision in range(3):
        observation = settings.lookup(future)
        assert observation["status"] == "unobserved"
        assert observation["receipt"] is None
        assert observation["mutation_fenced"] is False
        settings.change(change(settings, f"advance-{revision}"))
    assert settings.lookup(future)["mutation_fenced"] is True
    with pytest.raises(ModelSettingsConflict):
        settings.change(future)


@pytest.mark.parametrize(
    "damage", ["drop", "empty", "backend", "active", "schema", "marker-zero", "marker-unknown"]
)
async def test_migrated_metadata_damage_never_mints_replacement(tmp_path, make_settings, damage):
    path = tmp_path / "settings.sqlite"
    first = make_settings(path)
    await first.close()
    with sqlite3.connect(path) as db:
        if damage == "drop":
            db.execute("DROP TABLE model_write_authority")
        elif damage == "empty":
            db.execute("DELETE FROM model_write_authority")
        elif damage == "backend":
            db.execute("UPDATE model_write_authority SET backend_id='INVALID'")
        elif damage == "active":
            db.execute("UPDATE model_write_authority SET active_instance='INVALID'")
        elif damage == "schema":
            db.execute("ALTER TABLE model_write_authority RENAME TO old_authority")
            db.execute(
                "CREATE TABLE model_write_authority (id INTEGER, backend_id TEXT, active_instance TEXT)"
            )
            db.execute("INSERT INTO model_write_authority SELECT * FROM old_authority")
            db.execute("DROP TABLE old_authority")
        else:
            db.execute(f"PRAGMA user_version={0 if damage == 'marker-zero' else 2}")
    before = path.read_bytes()
    with pytest.raises(ModelSettingsUnavailable):
        make_settings(path)
    assert path.read_bytes() == before


async def test_partial_legacy_schema_is_not_a_first_migration(tmp_path, make_settings):
    path = tmp_path / "partial.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, revision INTEGER, body TEXT)")
    with pytest.raises(ModelSettingsUnavailable):
        make_settings(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("settings",)
        ]


async def test_concurrent_first_start_keeps_one_backend_and_one_active_generation(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    started = threading.Barrier(2, timeout=5)

    def boot():
        settings = AgentModelSettings(provider(), {}, path=path)
        try:
            started.wait()
            return settings.snapshot()["write_authority"]
        finally:
            asyncio.run(settings.close())

    first, second = await asyncio.gather(asyncio.to_thread(boot), asyncio.to_thread(boot))
    assert first["backend_id"] == second["backend_id"]
    assert first["instance_id"] != second["instance_id"]
    assert sum([first["writable"], second["writable"]]) == 1
    with sqlite3.connect(path) as db:
        backend, active = db.execute(
            "SELECT backend_id, active_instance FROM model_write_authority"
        ).fetchone()
        assert backend == first["backend_id"] and active in {
            first["instance_id"],
            second["instance_id"],
        }


class TransactionFault:
    def __init__(self, db, stage, *, rollback_fails=False):
        self.db, self.stage = db, stage
        self.rollback_fails = rollback_fails
        self.fired = False
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        match = self.stage == "begin" and sql.startswith("BEGIN IMMEDIATE")
        match = match or self.stage == "settings" and sql.startswith("UPDATE settings")
        match = match or self.stage == "operations" and sql.startswith("INSERT INTO operations")
        if match and not self.fired:
            self.fired = True
            if self.stage != "begin":
                self.db.execute(sql, *args)
            raise sqlite3.OperationalError("synthetic private storage fault")
        return self.db.execute(sql, *args)

    def commit(self):
        if self.stage in {"commit-before", "commit-after"} and not self.fired:
            self.fired = True
            if self.stage == "commit-after":
                self.db.commit()
            raise sqlite3.OperationalError("synthetic private commit fault")
        return self.db.commit()

    def rollback(self):
        if self.rollback_fails:
            raise sqlite3.OperationalError("synthetic rollback fault")
        return self.db.rollback()

    def close(self):
        self.closed = True
        return self.db.close()


@pytest.mark.parametrize("stage", ["begin", "settings", "operations", "commit-before"])
async def test_clean_rollback_preserves_explicit_retry_capacity(make_settings, stage):
    settings = make_settings()
    request = change(settings)
    settings.db = TransactionFault(settings.db, stage)
    with pytest.raises(sqlite3.Error):
        settings.change(request)
    assert not settings.db.in_transaction
    assert settings.snapshot()["revision"] == 0
    assert settings.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    assert settings.change(request)["revision"] == 1


async def test_commit_then_error_reconciles_the_exact_receipt_without_selection(make_settings):
    settings = make_settings()
    request = change(settings, harness_version="seed-v1")
    settings.db = TransactionFault(settings.db, "commit-after")
    with pytest.raises(sqlite3.Error):
        settings.change(request)
    assert settings.lookup(request)["status"] == "recorded"
    receipt, applied = settings.change_with_status(request, validate_harness=fail_selection)
    assert applied is False
    assert receipt["revision"] == 1 and settings.snapshot()["revision"] == 1
    assert settings.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 1


async def test_failed_rollback_poison_closes_and_never_reuses_connection(tmp_path, make_settings):
    path = tmp_path / "rollback.sqlite"
    settings = make_settings(path)
    request = change(settings)
    fault = TransactionFault(settings.db, "operations", rollback_fails=True)
    settings.db = fault
    with pytest.raises(ModelSettingsUnavailable):
        settings.change(request)
    assert fault.closed and settings._failed
    for operation in [
        lambda: settings.change(request),
        lambda: settings.lookup(request),
        settings.snapshot,
    ]:
        with pytest.raises(ModelSettingsUnavailable):
            operation()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT revision FROM settings").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "damage", ["extra-field", "backend", "revision", "digest", "json", "state"]
)
async def test_corrupt_record_or_state_is_unavailable_not_absence(make_settings, damage):
    settings = make_settings()
    request = change(settings)
    receipt = settings.change(request)
    if damage == "state":
        settings.db.execute("UPDATE settings SET revision=0")
    elif damage == "digest":
        settings.db.execute("UPDATE operations SET digest=?", ("0" * 64,))
    elif damage == "json":
        settings.db.execute("UPDATE operations SET receipt='not-json'")
    else:
        altered = dict(receipt)
        if damage == "extra-field":
            altered["unrecognized"] = True
        elif damage == "backend":
            altered["backend_id"] = "0" * 32
        else:
            altered["revision"] = True
        settings.db.execute("UPDATE operations SET receipt=?", (json.dumps(altered),))
    settings.db.commit()
    with pytest.raises(ModelSettingsUnavailable):
        settings.lookup(request)
    with pytest.raises(ModelSettingsUnavailable):
        settings.change(request)


async def test_api_replay_never_assigns_provider_and_all_owner_results_are_no_store(
    tmp_path, make_settings, monkeypatch
):
    path = tmp_path / "api.sqlite"
    old = make_settings(path)
    request = change(old, harness_version="seed-v1")
    receipt = old.change(request)
    current = make_settings(path)
    monkeypatch.setattr(current, "selection", fail_selection)
    runtime = SimpleNamespace(
        model_settings=current,
        integration_lock=asyncio.Lock(),
        session_harness=fail_selection,
        provider=object(),
    )
    original_provider = runtime.provider
    app = FastAPI()
    app.include_router(router(runtime, Auth({"owner": "o" * 32, "agent": "a" * 32})))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
    ) as client:
        for headers, expected in [
            ({}, 401),
            ({"Authorization": "Bearer " + "a" * 32}, 403),
            ({"Authorization": "Bearer " + "o" * 32}, 200),
        ]:
            response = await client.post(
                "/v1/agent/options/models", json=canonical(request), headers=headers
            )
            assert response.status_code == expected
            assert response.headers["Cache-Control"] == "no-store"
            if expected == 200:
                assert response.json() == receipt
        client.headers["Authorization"] = "Bearer " + "o" * 32
        observed = await client.post(
            "/v1/agent/options/model-changes/lookup", json=canonical(request)
        )
        assert observed.status_code == 200 and observed.json()["receipt"] == receipt
        malformed = await client.post("/v1/agent/options/models", json={"schema_version": "1.1"})
        assert malformed.status_code == 422 and malformed.headers["Cache-Control"] == "no-store"
        current.db = TransactionFault(current.db, "begin")
        unavailable = await client.post("/v1/agent/options/models", json=canonical(request))
        assert unavailable.status_code == 503
        assert "private" not in unavailable.text
        assert unavailable.headers["Cache-Control"] == "no-store"
    assert runtime.provider is original_provider
