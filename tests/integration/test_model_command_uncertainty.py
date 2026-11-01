"""Independent public-command recovery checks, frozen before #191 integration.

The command, model settings, and journal use real temporary SQLite. Faults wrap
the actual native change; providers, credentials, and ASGI are synthetic only.
"""

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from test_model_commands_authority import operation_key, request
from test_model_commands_authority import owner as owner

from packages.commands import Registry
from services.agent import model_settings
from services.agent.app import create_app
from services.common.auth import Auth
from services.session.store import LocalJournal

EXECUTE = "/v1/commands/execute"
PRIVATE_DIAGNOSTIC = "Synthetic-private-SQLite-diagnostic-do-not-publish"
AUTH = Auth({"owner": "o" * 32, "agent": "a" * 32, "decisions": "d" * 32})


def native_error(kind):
    # The new native type is intentionally resolved only when a test runs.
    # Collection remains possible before the separately authored implementation.
    return (
        model_settings.ModelSettingsUnavailable()
        if kind == "unavailable"
        else sqlite3.OperationalError(PRIVATE_DIAGNOSTIC)
    )


def command_row(owner, command):
    row = owner.commands.db.execute(
        "SELECT digest,result FROM commands WHERE session=? AND key=?",
        (command.session_id, command.idempotency_key),
    ).fetchone()
    assert row is not None
    assert row[0] == hashlib.sha256(command.model_dump_json().encode()).hexdigest()
    return row


def model_rows(settings):
    return (
        tuple(settings.db.execute("SELECT * FROM settings ORDER BY id")),
        tuple(settings.db.execute("SELECT * FROM operations ORDER BY key")),
    )


class NativeFailure:
    def __init__(self, settings, error, *, after_commit):
        self.change = settings.change
        self.error, self.after_commit = error, after_commit
        self.requests, self.receipts = [], []

    def __call__(self, body, *args, **kwargs):
        self.requests.append(body.model_dump(mode="json"))
        if self.after_commit:
            self.receipts.append(self.change(body, *args, **kwargs))
        raise self.error


@asynccontextmanager
async def command_http(owner):
    app = create_app(owner.runtime, AUTH, commands=owner.commands)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://synthetic-command.invalid",
        headers={"Authorization": "Bearer " + "o" * 32},
    ) as client:
        yield client


def assert_unconfirmed(response):
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert type(detail) is str and "unconfirmed" in detail.lower()
    assert PRIVATE_DIAGNOSTIC not in response.text
    assert "OperationalError" not in response.text


async def restart_models(owner, tmp_path, *, profiles):
    prior = owner.settings.snapshot()["write_authority"]
    await owner.settings.close()
    current = model_settings.AgentModelSettings(
        owner.providers[0],
        {"alternate": lambda effort: owner.providers[1]} if profiles else {},
        path=tmp_path / "models.sqlite",
    )
    owner.settings = owner.runtime.model_settings = current
    observed = current.snapshot()["write_authority"]
    assert observed["backend_id"] == prior["backend_id"]
    assert observed["instance_id"] != prior["instance_id"]
    assert observed["writable"] is True
    return current


def forbid_current_state(owner, monkeypatch, *, forbid_change=False):
    owner.commands.profiles.clear()
    forbidden = Mock(side_effect=AssertionError("Receipt recovery inspected current selection"))
    names = ["snapshot", "selection", "default_info", "resolve"]
    if forbid_change:
        names.append("change")
    for name in names:
        monkeypatch.setattr(owner.settings, name, forbidden)
    status = AsyncMock(side_effect=AssertionError("Receipt recovery inspected current work"))
    discovery = AsyncMock(side_effect=AssertionError("Receipt recovery ran skill discovery"))
    monkeypatch.setattr(owner.runtime, "status", status)
    monkeypatch.setattr(owner.commands, "refresh_skill_commands", discovery)

    class UnavailableCurrentProvider:
        @property
        def info(self):
            pytest.fail("Receipt recovery inspected the current provider identity")

    current_provider = UnavailableCurrentProvider()
    monkeypatch.setattr(owner.runtime, "provider", current_provider)
    return forbidden, status, discovery, current_provider


def assert_no_current_reads(owner, guards):
    forbidden, status, discovery, current_provider = guards
    forbidden.assert_not_called()
    status.assert_not_called()
    discovery.assert_not_called()
    assert owner.runtime.provider is current_provider
    assert all(not provider.calls for provider in owner.providers)


@pytest.mark.parametrize("kind", ["unavailable", "sqlite"])
async def test_public_http_native_commit_uncertainty_is_fixed_503_and_keeps_intent(
    owner, monkeypatch, kind
):
    command = request(owner)
    before = model_rows(owner.settings)
    failure = NativeFailure(owner.settings, native_error(kind), after_commit=True)
    monkeypatch.setattr(owner.settings, "change", failure)
    async with command_http(owner) as client:
        response = await client.post(EXECUTE, json=command.model_dump(mode="json"))
    assert len(failure.requests) == len(failure.receipts) == 1
    saved = failure.receipts[0]
    assert saved["schema_version"] == "1.1" and saved["revision"] == 1
    assert saved["idempotency_key"] == operation_key(command)
    assert saved["backend_id"] == failure.requests[0]["expected_backend"]
    assert saved["service_instance"] == failure.requests[0]["expected_instance"]
    assert owner.settings.receipt(operation_key(command)) == saved
    assert model_rows(owner.settings) != before
    assert command_row(owner, command)[1] is None
    assert owner.store.read(kinds=["agent.command"]) == []
    assert_unconfirmed(response)


@pytest.mark.parametrize("kind", ["unavailable", "sqlite"])
async def test_public_execute_recovers_committed_inner_receipt_from_real_null_intent(
    owner, tmp_path, monkeypatch, kind
):
    command = request(owner)
    failure = NativeFailure(owner.settings, native_error(kind), after_commit=True)
    monkeypatch.setattr(owner.settings, "change", failure)
    with pytest.raises(type(failure.error)):
        await owner.commands.execute(command)
    assert len(failure.requests) == len(failure.receipts) == 1
    original = failure.receipts[0]
    assert command_row(owner, command)[1] is None
    assert owner.settings.receipt(operation_key(command)) == original
    assert owner.store.read(kinds=["agent.command"]) == []

    # A real native restart changes writer generation and removes the profile.
    # Recovery must use the old exact receipt, not a newly built bound request.
    current = await restart_models(owner, tmp_path, profiles=False)
    before = model_rows(current)
    guards = forbid_current_state(owner, monkeypatch)
    result = await owner.commands.execute(command)
    assert result["status"] == "ok"
    assert result["result"] == {"model_settings_receipt": original, "recovered": True}
    assert json.loads(command_row(owner, command)[1]) == result
    assert model_rows(current) == before
    assert current.receipt(operation_key(command)) == original
    assert len(owner.store.read(kinds=["agent.command"])) == 1
    assert_no_current_reads(owner, guards)


@pytest.mark.parametrize("kind", ["unavailable", "sqlite"])
async def test_uncommitted_null_intent_remains_503_after_generation_changes_without_new_write(
    owner, tmp_path, monkeypatch, kind
):
    command = request(owner)
    before = model_rows(owner.settings)
    failure = NativeFailure(owner.settings, native_error(kind), after_commit=False)
    monkeypatch.setattr(owner.settings, "change", failure)
    with pytest.raises(type(failure.error)):
        await owner.commands.execute(command)
    assert len(failure.requests) == 1 and failure.receipts == []
    assert model_rows(owner.settings) == before
    assert owner.settings.receipt(operation_key(command)) is None
    original_row = command_row(owner, command)
    assert original_row[1] is None

    current = await restart_models(owner, tmp_path, profiles=True)
    assert (
        current.snapshot()["write_authority"]["instance_id"]
        != failure.requests[0]["expected_instance"]
    )
    guards = forbid_current_state(owner, monkeypatch, forbid_change=True)
    async with command_http(owner) as client:
        for _ in range(2):
            response = await client.post(EXECUTE, json=command.model_dump(mode="json"))
            assert command_row(owner, command) == original_row
            assert model_rows(current) == before
            assert current.receipt(operation_key(command)) is None
            assert_unconfirmed(response)
    assert owner.store.read(kinds=["agent.command"]) == []
    assert_no_current_reads(owner, guards)


@pytest.mark.parametrize("kind", ["unavailable", "sqlite"])
async def test_unresolved_outer_key_rejects_different_input_before_any_recovery(
    owner, tmp_path, monkeypatch, kind
):
    command = request(owner)
    failure = NativeFailure(owner.settings, native_error(kind), after_commit=False)
    monkeypatch.setattr(owner.settings, "change", failure)
    with pytest.raises(type(failure.error)):
        await owner.commands.execute(command)
    original_row = command_row(owner, command)
    assert original_row[1] is None
    current = await restart_models(owner, tmp_path, profiles=True)
    before = model_rows(current)
    guards = forbid_current_state(owner, monkeypatch, forbid_change=True)
    conflict = request(owner, text="/model host-default")
    async with command_http(owner) as client:
        response = await client.post(EXECUTE, json=conflict.model_dump(mode="json"))
    assert response.status_code == 400, response.text
    assert command_row(owner, command) == original_row
    assert model_rows(current) == before
    assert current.receipt(operation_key(command)) is None
    assert_no_current_reads(owner, guards)


async def test_committed_native_receipt_precedes_skill_discovery_with_a_fresh_outer_registry(
    owner, tmp_path, monkeypatch
):
    command = request(owner)
    failure = NativeFailure(owner.settings, native_error("unavailable"), after_commit=True)
    monkeypatch.setattr(owner.settings, "change", failure)
    with pytest.raises(type(failure.error)):
        await owner.commands.execute(command)
    assert len(failure.receipts) == 1 and command_row(owner, command)[1] is None
    saved = failure.receipts[0]
    current = await restart_models(owner, tmp_path, profiles=False)
    before = model_rows(current)
    owner.commands.close()
    owner.commands = Registry(
        owner.runtime,
        LocalJournal(owner.store, "owner"),
        path=tmp_path / "separate-command-registry.sqlite",
    )
    assert owner.commands.db.execute("SELECT * FROM commands").fetchall() == []
    guards = forbid_current_state(owner, monkeypatch)
    result = await owner.commands.execute(command)
    assert result["status"] == "ok"
    assert result["result"] == {"model_settings_receipt": saved, "recovered": True}
    assert json.loads(command_row(owner, command)[1]) == result
    assert model_rows(current) == before
    assert len(owner.store.read(kinds=["agent.command"])) == 1
    assert_no_current_reads(owner, guards)


async def test_non_model_null_intent_keeps_existing_interrupted_dispatch_behavior(
    owner, monkeypatch
):
    command = request(owner, text="/status")
    dispatch = AsyncMock(side_effect=RuntimeError("Synthetic interrupted ordinary command"))
    monkeypatch.setattr(owner.commands, "_dispatch", dispatch)
    with pytest.raises(RuntimeError, match="Synthetic interrupted ordinary command"):
        await owner.commands.execute(command)
    original_row = command_row(owner, command)
    assert original_row[1] is None
    result = await owner.commands.execute(command)
    assert result == {
        "schema_version": "1.0",
        "status": "error",
        "command": None,
        "error": "Interrupted dispatch; inspect state before a new request",
    }
    assert command_row(owner, command) == original_row
    dispatch.assert_awaited_once()
    assert owner.store.read(kinds=["agent.command"]) == []
