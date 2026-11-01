"""Codex-authored combined execution-save/catalog-close regressions.

Real runtime/settings/SQLite objects exercise the shared idle boundary. Only
adapter close and MCP retirement are synthetic; no model requests are issued.
"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from core.policy import ToolPolicy
from packages.providers.text import ProviderCleanupError, ScriptedProvider
from services.agent import AgentRuntime
from services.agent.model_settings import AgentModelSettings
from services.agent.runtime_settings import (
    AgentRuntimeSettings,
    BoundRuntimeSettingsChange,
    RuntimeSettingsConflict,
)
from services.agent.task_model_lease import TaskModelLease
from services.session.store import LocalJournal, Store


class SyntheticMCP:
    def __init__(self):
        self.host = self
        self.retired = 0

    async def retire_connections(self, *, actor):
        assert actor == "owner"
        self.retired += 1

    async def close(self):
        pass


class GatedCloseAdapter:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.close_calls = 0
        self.closed = False

    async def close(self):
        self.close_calls += 1
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("synthetic adapter close failed")
        self.closed = True


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    journal_store = Store(tmp_path / "journal.sqlite")
    provider = ScriptedProvider([])
    mcp = SyntheticMCP()
    runtime = AgentRuntime(
        LocalJournal(journal_store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace),
        mcp_controller=mcp,
    )
    model_settings = AgentModelSettings(provider, {}, path=tmp_path / "models.sqlite")
    runtime.model_settings = model_settings
    execution_path = tmp_path / "execution.sqlite"
    settings = AgentRuntimeSettings(runtime, path=execution_path)
    runtime.runtime_settings = settings
    leases = []

    async def closing_lease(*, fail=False):
        adapter = GatedCloseAdapter(fail=fail)
        # Exercise the actual lease and owner registry without constructing a
        # catalog provider, resolving credentials, or starting task inference.
        lease = TaskModelLease(model_settings.catalog_leases.discard)
        lease.own(adapter)
        model_settings.catalog_leases.add(lease)
        leases.append((lease, adapter))
        lease.start_close()
        await asyncio.wait_for(adapter.entered.wait(), 1)
        return lease, adapter

    yield SimpleNamespace(
        runtime=runtime,
        models=model_settings,
        settings=settings,
        execution_path=execution_path,
        provider=provider,
        mcp=mcp,
        closing_lease=closing_lease,
    )
    for _lease, adapter in leases:
        adapter.release.set()
    if leases:
        await asyncio.gather(*(lease.close() for lease, _adapter in leases), return_exceptions=True)
    try:
        await runtime.close()
    finally:
        journal_store.close()


def request(env, key, *, steps=23, yolo=False):
    snapshot = env.settings.snapshot()
    authority = snapshot["write_authority"]
    return BoundRuntimeSettingsChange(
        schema_version="1.1",
        expected_backend=authority["backend_id"],
        expected_instance=authority["instance_id"],
        expected_revision=snapshot["revision"],
        expected_state=snapshot["state_token"],
        idempotency_key=key,
        settings={**snapshot["settings"], "max_steps": steps, "file_writes": "deny"},
        yolo=yolo,
    )


def durable_counts(env):
    with sqlite3.connect(env.execution_path) as observer:
        return (
            observer.execute("SELECT revision FROM settings WHERE id=1").fetchone()[0],
            observer.execute("SELECT count(*) FROM operations").fetchone()[0],
        )


@pytest.mark.parametrize("failed", [False, True], ids=["pending-close", "failed-close"])
async def test_catalog_cleanup_blocks_new_save_but_not_exact_historical_receipt(env, failed):
    historical = request(env, "historical", steps=17, yolo=True)
    receipt = await env.settings.change(historical)
    env.runtime.policy.set_permissions(actor="owner", yolo=False)
    current = request(env, "unobserved-current", steps=29)
    before = env.settings.snapshot()
    lease, adapter = await env.closing_lease(fail=failed)
    if failed:
        adapter.release.set()
        with pytest.raises(ProviderCleanupError):
            await lease.close()
        assert lease.failed is True and lease.cleanup.done()
    else:
        assert not lease.cleanup.done()
    assert not env.runtime._tasks
    assert env.models.catalog_cleanup_busy is True
    assert env.runtime.runtime_settings_busy() is True
    assert lease in env.models.catalog_leases and lease.settled is False

    with pytest.raises(RuntimeSettingsConflict):
        await env.settings.change(current)
    assert env.settings.receipt(current.idempotency_key) is None
    proof = env.settings.lookup(current)
    assert proof["request"] == current.model_dump()
    assert proof["status"] == "unobserved" and proof["receipt"] is None
    assert proof["mutation_fenced"] is False
    assert env.settings.snapshot()["state_token"] == current.expected_state

    assert await env.settings.change(historical) == receipt
    assert env.settings.lookup(historical)["receipt"] == receipt
    assert env.mcp.retired == 1
    assert env.runtime.policy.yolo is False
    assert env.runtime.max_steps == before["settings"]["max_steps"] == 17
    assert env.settings.snapshot()["settings"] == before["settings"]
    assert durable_counts(env) == (1, 1)
    assert adapter.close_calls == 1 and env.provider.calls == []


async def test_actual_catalog_close_settlement_allows_the_same_unobserved_request_after_aba(env):
    original = request(env, "same-original", steps=31)
    original_body = original.model_dump()
    assert env.runtime.runtime_settings_busy() is False
    lease, adapter = await env.closing_lease()
    assert env.models.catalog_cleanup_busy is True
    assert env.runtime.runtime_settings_busy() is True
    with pytest.raises(RuntimeSettingsConflict):
        await env.settings.change(original)
    assert durable_counts(env) == (0, 0) and env.mcp.retired == 0
    assert env.settings.lookup(original)["mutation_fenced"] is False

    adapter.release.set()
    await asyncio.wait_for(lease.close(), 1)
    assert lease.settled is True and lease.failed is False
    assert lease not in env.models.catalog_leases and lease.adapters == []
    assert adapter.closed is True and adapter.close_calls == 1
    assert env.models.catalog_cleanup_busy is False
    assert env.runtime.runtime_settings_busy() is False
    assert env.settings.snapshot()["state_token"] == original.expected_state
    assert env.settings.lookup(original)["mutation_fenced"] is False
    assert original.model_dump() == original_body

    receipt = await env.settings.change(original)
    assert receipt["idempotency_key"] == original.idempotency_key
    assert receipt["settings"] == original_body["settings"]
    assert env.runtime.max_steps == 31
    assert durable_counts(env) == (1, 1) and env.mcp.retired == 1
    assert env.settings.lookup(original)["receipt"] == receipt
    assert env.provider.calls == []
