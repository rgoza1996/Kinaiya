"""Lead fault injection beyond the controller's real-transport tests."""

import asyncio
import os
import sqlite3

import pytest
from test_mcp_controller import _make, _stdio_definition, _workspace

from core.policy import ToolPolicy
from packages.ecosystem import mcp_control as module
from packages.ecosystem.mcp_control import MCPControlError, MCPController
from packages.ecosystem.mcp_host import MCPHost, MCPTransportError


async def test_remove_and_enable_replays_do_not_depend_on_current_configuration(tmp_path):
    workspace = _workspace(tmp_path)
    controller = _make(tmp_path, configs=[_stdio_definition(workspace)])
    try:
        first = await controller.control(
            "enable",
            server_id="synth",
            expected_revision=0,
            idempotency_key="enable",
            reviewed=True,
        )
        await controller.control(
            "disable", server_id="synth", expected_revision=1, idempotency_key="disable"
        )
        assert (
            await controller.control(
                "enable",
                server_id="synth",
                expected_revision=0,
                idempotency_key="enable",
                reviewed=True,
            )
            == first
        )
        assert controller.snapshot()["servers"][0]["enabled"] is False
        removed = await controller.control(
            "remove", server_id="synth", expected_revision=2, idempotency_key="remove"
        )
        assert (
            await controller.control(
                "remove", server_id="synth", expected_revision=2, idempotency_key="remove"
            )
            == removed
        )
        assert controller.snapshot()["revision"] == 3
    finally:
        await controller.close()


@pytest.mark.parametrize("phase", ["desired", "history"])
async def test_storage_fault_revokes_host_access_until_reviewed_reconciliation(
    tmp_path, monkeypatch, phase
):
    workspace = _workspace(tmp_path)
    controller = _make(tmp_path, configs=[_stdio_definition(workspace, enabled=True)])
    name = "_write_desired" if phase == "desired" else "_record_history"
    original = getattr(controller, name)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic persistence failure")

    monkeypatch.setattr(controller, name, fail)
    try:
        with pytest.raises(MCPControlError):
            await controller.control(
                "configure",
                config=_stdio_definition(workspace, id="second"),
                expected_revision=0,
                idempotency_key="fault",
                reviewed=True,
            )
        assert controller.snapshot()["blocked"] is True
        assert all(not entry["enabled"] for entry in controller.host.list_servers())
        with pytest.raises((MCPControlError, MCPTransportError), match="reconciliation|closed"):
            await controller.host.discover("synth")
        monkeypatch.setattr(controller, name, original)
        revision = controller.snapshot()["revision"]
        result = await controller.control(
            "reconcile",
            expected_revision=revision,
            idempotency_key="reviewed-recovery",
            reviewed=True,
        )
        assert result["status"] == "reconciled" and not controller.blocked
    finally:
        monkeypatch.setattr(controller, name, original)
        await controller.close()


async def test_uncertain_commit_acknowledgment_never_reexecutes_configuration(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    controller = _make(tmp_path)
    native = controller._db

    class CommitFault:
        count = 0

        def __getattr__(self, name):
            return getattr(native, name)

        def commit(self):
            self.count += 1
            native.commit()
            if self.count == 3:
                raise sqlite3.OperationalError("synthetic lost commit acknowledgment")

    controller._db = CommitFault()
    original = controller.host.replace_configs
    applies = 0

    async def replace(**kwargs):
        nonlocal applies
        applies += 1
        await original(**kwargs)

    monkeypatch.setattr(controller.host, "replace_configs", replace)
    args = dict(
        config=_stdio_definition(workspace),
        expected_revision=0,
        idempotency_key="commit-fault",
        reviewed=True,
    )
    try:
        with pytest.raises(MCPControlError):
            await controller.control("configure", **args)
        with pytest.raises(MCPControlError):
            await controller.control("configure", **args)
        assert applies == 1 and controller.blocked
    finally:
        await controller.close()


async def test_failed_probe_cleanup_cannot_report_connected_or_release_unknown_host(
    tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path)
    controller = _make(tmp_path, configs=[_stdio_definition(workspace, enabled=True)])
    may_close = False

    class CleanupFault(MCPHost):
        async def discover(self, server_id):
            return []

        async def close(self):
            if not may_close:
                raise RuntimeError("synthetic cleanup failure")
            await super().close()

    monkeypatch.setattr(module, "MCPHost", CleanupFault)
    try:
        with pytest.raises(MCPControlError, match="cleanup incomplete"):
            await controller.control(
                "test", server_id="synth", expected_revision=0, idempotency_key="probe"
            )
        assert controller.blocked and len(controller._uncertain_hosts) == 1
        with pytest.raises((MCPControlError, MCPTransportError), match="reconciliation|closed"):
            await controller.host.discover("synth")
        may_close = True
        await controller.control(
            "reconcile", expected_revision=0, reviewed=True, idempotency_key="reap-and-reconcile"
        )
        assert not controller._uncertain_hosts and not controller.blocked
    finally:
        may_close = True
        await controller.close()


async def test_repeated_cancellation_waits_for_probe_reaping(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    controller = _make(tmp_path, configs=[_stdio_definition(workspace)])
    entered, closing, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowCleanup(MCPHost):
        async def discover(self, server_id):
            entered.set()
            await asyncio.Event().wait()

        async def close(self):
            closing.set()
            await released.wait()
            await super().close()

    monkeypatch.setattr(module, "MCPHost", SlowCleanup)
    task = asyncio.create_task(
        controller.control("test", server_id="synth", idempotency_key="cancelled-probe")
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not controller._uncertain_hosts
        await controller.close()


async def test_broker_guard_denies_new_calls_even_if_transport_close_fails(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    controller = _make(tmp_path, configs=[_stdio_definition(workspace, enabled=True)])
    original_close, original_history = controller.host.close, controller._record_history

    async def cannot_close():
        raise RuntimeError("synthetic incomplete transport cleanup")

    def cannot_record(*args):
        raise sqlite3.OperationalError("synthetic failed final acknowledgment")

    monkeypatch.setattr(controller.host, "close", cannot_close)
    monkeypatch.setattr(controller, "_record_history", cannot_record)
    try:
        with pytest.raises(MCPControlError):
            await controller.control(
                "configure",
                config=_stdio_definition(workspace, id="second"),
                expected_revision=0,
                reviewed=True,
                idempotency_key="unknown",
            )
        assert not controller.host._closed and controller.blocked
        with pytest.raises(MCPControlError, match="reconciliation"):
            await controller.host.discover("synth")
        assert controller.host._actors == {}
    finally:
        monkeypatch.setattr(controller.host, "close", original_close)
        monkeypatch.setattr(controller, "_record_history", original_history)
        await controller.close()


def test_policy_workspace_mismatch_is_rejected_before_protected_storage_creation(tmp_path):
    actual = tmp_path / "actual-grant"
    reported = tmp_path / "reported-workspace"
    actual.mkdir()
    reported.mkdir()
    root = actual / "must-not-be-created"
    with pytest.raises(MCPControlError, match="ToolPolicy"):
        MCPController(root, reported, ToolPolicy(actual), initial_configs=[])
    assert not root.exists()


def test_hardlinked_database_is_rejected_without_mutating_original(tmp_path):
    workspace = _workspace(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    original = tmp_path / "original"
    original.write_bytes(b"synthetic owner file")
    os.link(original, root / "mcp_control.sqlite3")
    with pytest.raises(MCPControlError):
        MCPController(root, workspace, ToolPolicy(workspace), initial_configs=[])
    assert original.read_bytes() == b"synthetic owner file"


async def test_descriptor_digest_is_verified_before_reopening(tmp_path):
    workspace = _workspace(tmp_path)
    controller = _make(tmp_path, configs=[_stdio_definition(workspace)])
    await controller.close()
    with sqlite3.connect(tmp_path / "mcp_root/mcp_control.sqlite3") as db:
        db.execute("UPDATE servers SET sha256='not-the-native-digest'")
    with pytest.raises(MCPControlError, match="digest"):
        _make(tmp_path)


async def test_managed_definition_replacement_cannot_bypass_controller(tmp_path):
    workspace = _workspace(tmp_path)
    controller = _make(tmp_path, configs=[_stdio_definition(workspace)])
    before = controller.snapshot()
    try:
        with pytest.raises(MCPControlError, match="owner controller"):
            await controller.host.replace_configs(actor="owner", configs=[])
        assert controller.snapshot() == before
        await controller.retire_connections(actor="owner")
        assert controller.snapshot() == before  # retirement changes no descriptors/revision
    finally:
        await controller.close()
