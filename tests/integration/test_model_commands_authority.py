"""Real command/native-store boundaries for bound model changes and exact recovery."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from core.policy import ToolPolicy
from packages.commands import Registry
from packages.contracts.models import CommandRequest, ProviderInfo, SessionRequest
from packages.providers.text import ScriptedProvider
from services.agent.model_settings import AgentModelSettings
from services.agent.runtime import AgentRuntime
from services.session.store import LocalJournal, Store


def provider(model):
    value = ScriptedProvider([])
    value.info = ProviderInfo(provider="synthetic-command", model=model, local=True)
    return value


@pytest.fixture
async def owner(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial, alternate = provider("initial"), provider("alternate")
    settings = AgentModelSettings(
        initial, {"alternate": lambda effort: alternate}, path=tmp_path / "models.sqlite"
    )
    runtime = AgentRuntime(LocalJournal(store, "agent"), initial, workspace, ToolPolicy(workspace))
    runtime.model_settings = settings
    commands = Registry(
        runtime,
        LocalJournal(store, "owner"),
        path=tmp_path / "commands.sqlite",
        profiles={"alternate": lambda: alternate},
    )
    try:
        yield SimpleNamespace(
            store=store,
            session=session,
            runtime=runtime,
            settings=settings,
            commands=commands,
            providers=(initial, alternate),
        )
    finally:
        commands.close()
        await runtime.close()
        store.close()


def request(owner, text="/model alternate", key="select-profile"):
    return CommandRequest(
        session_id=owner.session.session_id,
        thread_id=owner.session.thread_id,
        epoch=owner.session.epoch,
        input=text,
        idempotency_key=key,
    )


def operation_key(command):
    return (
        "command:"
        + hashlib.sha256((command.session_id + ":" + command.idempotency_key).encode()).hexdigest()
    )


async def test_new_model_command_uses_observed_native_authority(owner):
    command = request(owner)
    result = await owner.commands.execute(command)
    assert result["status"] == "ok"
    receipt = owner.settings.receipt(operation_key(command))
    assert receipt["schema_version"] == "1.1"
    authority = owner.settings.snapshot()["write_authority"]
    assert receipt["backend_id"] == authority["backend_id"]
    assert receipt["service_instance"] == authority["instance_id"]
    assert receipt["revision"] == 1
    assert receipt["binding"] == {"profile_id": "alternate", "reasoning": None}
    assert owner.runtime.provider is owner.providers[1]
    assert await owner.commands.execute(command) == result
    assert owner.settings.snapshot()["revision"] == 1
    assert all(not value.calls for value in owner.providers)


@pytest.mark.parametrize("authority_kind", ["missing", "unsupported", "retired"])
async def test_model_command_requires_supported_writable_authority(
    owner, monkeypatch, authority_kind
):
    snapshot = owner.settings.snapshot()
    if authority_kind == "missing":
        snapshot.pop("write_authority", None)
    else:
        snapshot["write_authority"] = {
            "mutation_version": "9.0" if authority_kind == "unsupported" else "1.1",
            "backend_id": "a" * 32,
            "instance_id": "b" * 32,
            "writable": authority_kind != "retired",
        }
    monkeypatch.setattr(owner.settings, "snapshot", lambda: snapshot)
    command = request(owner)
    result = await owner.commands.execute(command)
    assert result["status"] == "error"
    assert owner.settings.receipt(operation_key(command)) is None
    assert owner.runtime.provider is owner.providers[0]
    assert all(not value.calls for value in owner.providers)


def forbid_current_selection(owner, monkeypatch):
    owner.commands.profiles.clear()

    def unavailable(*args, **kwargs):
        pytest.fail("Exact model-command receipt consulted current model selection")

    async def unavailable_status():
        pytest.fail("Exact model-command receipt consulted current work")

    monkeypatch.setattr(owner.runtime, "status", unavailable_status)
    for name in ("snapshot", "selection", "default_info", "resolve"):
        monkeypatch.setattr(owner.settings, name, unavailable)


async def test_inner_model_receipt_recovers_before_current_checks(owner, monkeypatch):
    command = request(owner)
    # Native save succeeded, but no outer command result was recorded yet.
    await owner.commands._dispatch("model", ["alternate"], command)
    receipt = owner.settings.receipt(operation_key(command))
    active = owner.runtime.provider
    forbid_current_selection(owner, monkeypatch)
    result = await owner.commands.execute(command)
    assert result["status"] == "ok"
    assert result["result"] == {"model_settings_receipt": receipt, "recovered": True}
    assert owner.runtime.provider is active
    assert owner.settings.receipt(operation_key(command)) == receipt
    assert all(not value.calls for value in owner.providers)


async def test_legacy_inner_model_receipt_remains_exact(owner, monkeypatch):
    command = request(owner)
    body = {
        "schema_version": "1.0",
        "expected_revision": 0,
        "idempotency_key": operation_key(command),
        "harness_version": None,
        "profile_id": "alternate",
        "reasoning": None,
    }
    receipt = {
        "schema_version": "1.0",
        "idempotency_key": body["idempotency_key"],
        "revision": 1,
        "harness_version": None,
        "binding": {"profile_id": "alternate", "reasoning": None},
        "change_boundary": "new_tasks",
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with owner.settings.db:
        row = owner.settings.db.execute("SELECT body FROM settings WHERE id=1").fetchone()
        state = json.loads(row[0])
        state["default"] = receipt["binding"]
        owner.settings.db.execute(
            "UPDATE settings SET revision=1,body=? WHERE id=1", (json.dumps(state),)
        )
        owner.settings.db.execute(
            "INSERT INTO operations VALUES (?,?,?)",
            (body["idempotency_key"], digest, json.dumps(receipt)),
        )
    forbid_current_selection(owner, monkeypatch)
    result = await owner.commands.execute(command)
    assert result["status"] == "ok"
    assert result["result"] == {"model_settings_receipt": receipt, "recovered": True}
    assert owner.settings.receipt(operation_key(command)) == receipt
    assert owner.runtime.provider is owner.providers[0]


async def test_completed_command_replay_has_no_second_native_effect(owner, monkeypatch):
    command = request(owner)
    result = await owner.commands.execute(command)
    assert result["status"] == "ok"
    receipt = owner.settings.receipt(operation_key(command))
    forbid_current_selection(owner, monkeypatch)
    assert await owner.commands.execute(command) == result
    assert owner.settings.receipt(operation_key(command)) == receipt
    assert len(owner.store.read(kinds=["agent.command"])) == 1
