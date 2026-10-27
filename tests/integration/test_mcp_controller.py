"""Durable MCP owner configuration controller tests (issue #44).

Covers first seed/restart authority, no execution during
construct/configure/inspect/enable/disable/remove, single-writer and
root/symlink/bound guards, authority/review/stale-revision rules,
disable/removal/re-enable lifecycles, idempotent identities with
conflict/pending recovery/reconciliation, cancellation without replay,
partial-apply fail-closed behavior, secret/arg hygiene in every output,
strict secret-ref format, and explicit real stdio plus loopback HTTP
test/reconnect. No paid inference, no external network; real-transport
cases skip explicitly when bubblewrap is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_ecosystem_mcp import SERVER_SRC, _sandbox_ok

from core.policy import ToolPolicy
from packages.ecosystem import mcp_control as controller_module
from packages.ecosystem.mcp_control import MCPControlError, MCPController
from packages.ecosystem.mcp_host import MCPConfig, MCPHost, MCPTransportError


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _make(
    tmp_path: Path,
    *,
    configs: list[Any] | None = None,
    policy: ToolPolicy | None = None,
    resolver=None,
    root_name: str = "mcp_root",
) -> MCPController:
    workspace = _workspace(tmp_path)
    return MCPController(
        tmp_path / root_name,
        workspace,
        policy or ToolPolicy(workspace, allow_shell=True),
        initial_configs=list(configs or []),
        secret_resolver=resolver,
    )


def _stdio_definition(workspace: Path, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "synth",
        "transport": "stdio",
        "command": "/usr/bin/python3",
        # NOTE: the bubblewrap sandbox mounts the policy workspace at
        # /workspace, so stdio args must use sandbox-relative paths.
        "args": ["/workspace/synth_server.py", "ok", "/workspace/calls.log"],
        "enabled": False,
        "allowed_tools": ["echo"],
        "timeout_seconds": 15.0,
    }
    base.update(over)
    return base


def _write_server(workspace: Path) -> None:
    (workspace / "synth_server.py").write_text(SERVER_SRC, encoding="utf-8")


def _db_rows(root: Path, table: str) -> list[tuple]:
    connection = sqlite3.connect(str(root / "mcp_control.sqlite3"))
    try:
        return connection.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        connection.close()


@pytest.fixture
def loopback_http():
    """Synthetic loopback Streamable-HTTP MCP server (test/reconnect only)."""
    state = {"calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            try:
                request = json.loads(self.rfile.read(length))
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            state["calls"].append(request.get("method"))
            if "id" not in request:
                self.send_response(202)
                self.end_headers()
                return
            method = request.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "synthetic-http", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {"name": "echo", "inputSchema": {"type": "object"}},
                        {"name": "hidden_tool", "inputSchema": {"type": "object"}},
                    ]
                }
            else:
                result = {"content": [{"type": "text", "text": "must-not-call"}]}
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/mcp", state
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_error_is_valueerror_and_close_idempotent(tmp_path) -> None:
    assert issubclass(MCPControlError, ValueError)
    controller = _make(tmp_path)
    assert isinstance(controller.host, MCPHost)

    async def exercise() -> None:
        await controller.close()
        await controller.close()
        with pytest.raises(MCPControlError):
            controller.snapshot()
        with pytest.raises(MCPControlError):
            await controller.control("reconcile", idempotency_key="k", reviewed=True)

    asyncio.run(exercise())


def test_first_seed_preserves_legacy_enabled_and_restart_is_authoritative(
    tmp_path,
) -> None:
    _workspace(tmp_path)
    legacy = MCPConfig(
        id="legacy",
        transport="stdio",
        command="/usr/bin/python3",
        args=["one"],
        enabled=True,
        allowed_tools=["echo"],
    )

    async def exercise() -> None:
        first = _make(tmp_path, configs=[legacy])
        try:
            snap = first.snapshot()
            assert snap["schema_version"] == "1.0"
            assert snap["revision"] == 0
            assert snap["blocked"] is False
            assert json.dumps(snap)  # snapshot is JSON-native
            assert len(snap["servers"]) == 1
            entry = snap["servers"][0]
            assert entry["id"] == "legacy"
            assert entry["enabled"] is True  # explicitly configured seed kept
            assert isinstance(entry["config_sha256"], str)
            assert len(entry["config_sha256"]) == 64
            assert "command" not in entry
            assert "args" not in entry
        finally:
            await first.close()
        # Later persisted state is authoritative: new seeds are ignored.
        replacement = MCPConfig(id="other", transport="stdio", command="/bin/true", enabled=True)
        second = _make(tmp_path, configs=[replacement])
        try:
            snap = second.snapshot()
            assert [item["id"] for item in snap["servers"]] == ["legacy"]
            assert snap["revision"] == 0
        finally:
            await second.close()

    asyncio.run(exercise())


def test_construction_and_config_ops_never_execute(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    _write_server(workspace)
    bogus = {
        "id": "bogus",
        "transport": "stdio",
        "command": "/nonexistent/binary-xyz",
        "enabled": False,
    }

    async def exercise() -> None:
        controller = _make(tmp_path, configs=[_stdio_definition(workspace)])
        try:
            # A bogus command is accepted: construction/configure never spawn.
            await controller.control(
                "configure",
                idempotency_key="cfg-bogus",
                config=bogus,
                expected_revision=0,
                reviewed=True,
            )
            await controller.control(
                "enable",
                idempotency_key="en-bogus",
                server_id="bogus",
                expected_revision=1,
                reviewed=True,
            )
            await controller.control(
                "disable",
                idempotency_key="dis-bogus",
                server_id="bogus",
                expected_revision=2,
            )
            controller.snapshot()
            assert not (workspace / "calls.log").exists()
            root = tmp_path / "mcp_root"
            names = {path.name for path in root.iterdir()}
            assert names <= {"controller.lock", "mcp_control.sqlite3"}
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_root_workspace_and_single_writer_guards(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    policy = ToolPolicy(workspace)
    with pytest.raises(MCPControlError):
        MCPController(workspace, workspace, policy, initial_configs=[])
    with pytest.raises(MCPControlError):
        MCPController(workspace / "nested", workspace, policy, initial_configs=[])
    with pytest.raises(MCPControlError):
        MCPController(tmp_path, tmp_path / "ws" / "deeper", policy, initial_configs=[])
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(MCPControlError):
        MCPController(tmp_path / "link" / "root", workspace, policy, initial_configs=[])

    async def exercise() -> None:
        first = _make(tmp_path)
        try:
            with pytest.raises(MCPControlError):
                _make(tmp_path)
        finally:
            await first.close()
        second = _make(tmp_path)
        await second.close()

    asyncio.run(exercise())


def test_files_are_owner_only(tmp_path) -> None:
    async def exercise() -> None:
        controller = _make(tmp_path)
        try:
            root = tmp_path / "mcp_root"
            assert stat.S_IMODE((root / "controller.lock").stat().st_mode) == 0o600
            assert stat.S_IMODE((root / "mcp_control.sqlite3").stat().st_mode) == 0o600
            assert stat.S_IMODE(root.stat().st_mode) == 0o700
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_seed_bounds_and_failed_seed_releases_lock(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    too_many = [
        MCPConfig(id=f"s{i:02d}", transport="stdio", command="/bin/true") for i in range(17)
    ]
    with pytest.raises(MCPControlError):
        _make(tmp_path, configs=too_many)
    oversize = _stdio_definition(workspace, id="big", command="x" * (300 * 1024))
    with pytest.raises(MCPControlError):
        _make(tmp_path, configs=[oversize])

    async def exercise() -> None:
        controller = _make(tmp_path)
        await controller.close()

    asyncio.run(exercise())


def test_configure_authority_review_and_revision(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    canary = "canary-config-leak-check"
    definition = _stdio_definition(workspace, args=[canary])

    async def exercise() -> None:
        controller = _make(tmp_path)
        try:
            with pytest.raises(MCPControlError):
                await controller.control("nope", idempotency_key="k")
            with pytest.raises(MCPControlError):
                await controller.control("configure", idempotency_key="k", actor="agent")
            with pytest.raises(MCPControlError):
                await controller.control("configure", idempotency_key="")
            with pytest.raises(MCPControlError):
                await controller.control(
                    "configure",
                    idempotency_key="k",
                    config=definition,
                    expected_revision=0,
                    reviewed="yes",  # type: ignore[arg-type]
                )
            with pytest.raises(MCPControlError):
                await controller.control(
                    "configure", idempotency_key="k", expected_revision=0, reviewed=True
                )
            with pytest.raises(MCPControlError):
                await controller.control(
                    "configure",
                    idempotency_key="k",
                    config="not-a-dict",  # type: ignore[arg-type]
                    expected_revision=0,
                    reviewed=True,
                )
            with pytest.raises(MCPControlError):
                # configure requires explicit review ...
                await controller.control(
                    "configure",
                    idempotency_key="k",
                    config=definition,
                    expected_revision=0,
                    reviewed=False,
                )
            enabled = dict(definition)
            enabled["enabled"] = True
            with pytest.raises(MCPControlError) as failure:
                # ... and a disabled definition.
                await controller.control(
                    "configure",
                    idempotency_key="k",
                    config=enabled,
                    expected_revision=0,
                    reviewed=True,
                )
            assert canary not in str(failure.value)
            mismatched = dict(definition)
            with pytest.raises(MCPControlError):
                await controller.control(
                    "configure",
                    idempotency_key="k",
                    server_id="other",
                    config=mismatched,
                    expected_revision=0,
                    reviewed=True,
                )
            result = await controller.control(
                "configure",
                idempotency_key="cfg-1",
                server_id="synth",
                config=definition,
                expected_revision=0,
                reviewed=True,
            )
            assert result == {"status": "configured", "server_id": "synth", "revision": 1}
            assert canary not in json.dumps(result)
            with pytest.raises(MCPControlError):
                # optimistic stale-write refusal
                await controller.control(
                    "configure",
                    idempotency_key="cfg-2",
                    config=_stdio_definition(workspace, allowed_tools=[]),
                    expected_revision=0,
                    reviewed=True,
                )
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_idempotent_replay_conflict_and_stored_errors(tmp_path) -> None:
    workspace = _workspace(tmp_path)

    async def exercise() -> None:
        controller = _make(tmp_path)
        try:
            first = await controller.control(
                "configure",
                idempotency_key="same-key",
                config=_stdio_definition(workspace),
                expected_revision=0,
                reviewed=True,
            )
            snap_before = controller.snapshot()
            repeat = await controller.control(
                "configure",
                idempotency_key="same-key",
                config=_stdio_definition(workspace),
                expected_revision=0,
                reviewed=True,
            )
            assert repeat == first
            assert controller.snapshot() == snap_before
            with pytest.raises(MCPControlError):
                # same identity, different payload: conflicting reuse refused
                await controller.control(
                    "configure",
                    idempotency_key="same-key",
                    config=_stdio_definition(workspace, allowed_tools=["other"]),
                    expected_revision=0,
                    reviewed=True,
                )

            calls = {"applies": 0}
            original = MCPHost.replace_configs

            async def flaky(self, *, actor, configs):
                calls["applies"] += 1
                raise RuntimeError("synthetic-apply-boom")

            controller_module.MCPHost.replace_configs = flaky  # type: ignore[method-assign]
            try:
                with pytest.raises(MCPControlError) as failure:
                    await controller.control(
                        "configure",
                        idempotency_key="boom-key",
                        config=_stdio_definition(workspace, id="second"),
                        expected_revision=1,
                        reviewed=True,
                    )
                assert "synthetic-apply-boom" not in str(failure.value)
                assert controller.snapshot()["blocked"] is True
                with pytest.raises(MCPControlError) as replayed:
                    await controller.control(
                        "configure",
                        idempotency_key="boom-key",
                        config=_stdio_definition(workspace, id="second"),
                        expected_revision=1,
                        reviewed=True,
                    )
                assert str(replayed.value) == str(failure.value)
                assert calls["applies"] == 1  # stored error, never re-executed
            finally:
                controller_module.MCPHost.replace_configs = original
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_enable_disable_remove_reenable_lifecycle(tmp_path) -> None:
    workspace = _workspace(tmp_path)

    async def exercise() -> None:
        controller = _make(tmp_path)
        try:
            await controller.control(
                "configure",
                idempotency_key="c1",
                config=_stdio_definition(workspace),
                expected_revision=0,
                reviewed=True,
            )
            with pytest.raises(MCPControlError):
                await controller.control(
                    "enable",
                    idempotency_key="e0",
                    server_id="synth",
                    expected_revision=1,
                    reviewed=False,
                )
            enabled = await controller.control(
                "enable",
                idempotency_key="e1",
                server_id="synth",
                expected_revision=1,
                reviewed=True,
            )
            assert enabled["revision"] == 2
            assert controller.snapshot()["servers"][0]["enabled"] is True
            await controller.control(
                "disable", idempotency_key="d1", server_id="synth", expected_revision=2
            )
            assert controller.snapshot()["servers"][0]["enabled"] is False
            removed = await controller.control(
                "remove", idempotency_key="r1", server_id="synth", expected_revision=3
            )
            assert removed == {"status": "removed", "server_id": "synth", "revision": 4}
            assert controller.snapshot()["servers"] == []
            with pytest.raises(MCPControlError):
                await controller.control(
                    "disable", idempotency_key="d2", server_id="synth", expected_revision=4
                )
            await controller.control(
                "configure",
                idempotency_key="c2",
                config=_stdio_definition(workspace),
                expected_revision=4,
                reviewed=True,
            )
            await controller.control(
                "enable",
                idempotency_key="e2",
                server_id="synth",
                expected_revision=5,
                reviewed=True,
            )
            assert controller.snapshot()["servers"][0]["enabled"] is True
            with pytest.raises(MCPControlError):
                await controller.control(
                    "remove",
                    idempotency_key="r2",
                    server_id="synth",
                    expected_revision=6,
                    actor="agent",
                )
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_update_never_inherits_enablement_or_grants(tmp_path) -> None:
    workspace = _workspace(tmp_path)

    async def exercise() -> None:
        seed = MCPConfig(
            id="synth",
            transport="stdio",
            command="/usr/bin/python3",
            enabled=True,
            allowed_tools=["echo"],
        )
        controller = _make(tmp_path, configs=[seed])
        try:
            before = controller.snapshot()["servers"][0]
            updated = _stdio_definition(workspace, allowed_tools=["other"])
            await controller.control(
                "configure",
                idempotency_key="upd",
                config=updated,
                expected_revision=0,
                reviewed=True,
            )
            entry = controller.snapshot()["servers"][0]
            assert entry["enabled"] is False  # updates are stored disabled
            assert entry["allowed_tools"] == ["other"]  # exact replacement
            assert entry["config_sha256"] != before["config_sha256"]
            await controller.control(
                "enable",
                idempotency_key="en",
                server_id="synth",
                expected_revision=1,
                reviewed=True,
            )
            assert controller.snapshot()["servers"][0]["allowed_tools"] == ["other"]
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_pending_crash_recovery_and_reviewed_reconcile(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    seed = MCPConfig(
        id="synth",
        transport="stdio",
        command="/usr/bin/python3",
        enabled=True,
        allowed_tools=["echo"],
    )

    async def seed_and_close() -> None:
        controller = _make(tmp_path, configs=[seed])
        await controller.close()

    asyncio.run(seed_and_close())
    # Simulate a crash between intent commit and effect completion.
    digest = controller_module._intent_digest(
        operation="configure",
        server_id=None,
        config_canonical=controller_module._canonical_descriptor(
            MCPConfig(
                id="late",
                transport="stdio",
                command="/bin/true",
                enabled=False,
            )
        ),
        expected_revision=0,
        reviewed=True,
        actor="owner",
    )
    connection = sqlite3.connect(str(tmp_path / "mcp_root" / "mcp_control.sqlite3"))
    try:
        connection.execute(
            "INSERT INTO intents(key, digest, status, result, updated_rev)"
            " VALUES(?, ?, 'pending', '{}', 0)",
            ("crash-key", digest),
        )
        connection.commit()
    finally:
        connection.close()

    async def recover() -> None:
        controller = _make(tmp_path)
        try:
            snap = controller.snapshot()
            assert snap["blocked"] is True
            # Fail closed: the model sees disabled/no-access configs.
            assert snap["servers"][0]["enabled"] is False
            with pytest.raises(MCPControlError):
                await controller.control(
                    "configure",
                    idempotency_key="late-key",
                    config=_stdio_definition(workspace, id="late"),
                    expected_revision=0,
                    reviewed=True,
                )
            with pytest.raises(MCPControlError) as interrupted:
                await controller.control(
                    "configure",
                    idempotency_key="crash-key",
                    config={
                        "id": "late",
                        "transport": "stdio",
                        "command": "/bin/true",
                        "enabled": False,
                    },
                    expected_revision=0,
                    reviewed=True,
                )
            assert "interrupted" in str(interrupted.value)
            assert controller.snapshot()["revision"] == 0  # never replayed
            with pytest.raises(MCPControlError):
                await controller.control("reconcile", idempotency_key="rec-0", expected_revision=0)
            result = await controller.control(
                "reconcile",
                idempotency_key="rec-1",
                expected_revision=0,
                reviewed=True,
            )
            assert result["status"] == "reconciled"
            snap = controller.snapshot()
            assert snap["blocked"] is False
            # Reviewed reconciliation restores the prior explicit enable.
            assert snap["servers"][0]["enabled"] is True
        finally:
            await controller.close()

    asyncio.run(recover())


def test_cancelled_test_never_silently_replays(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = {"discovers": 0}

    class HangingHost(MCPHost):
        async def discover(self, server_id: str):  # type: ignore[override]
            calls["discovers"] += 1
            entered.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait, 30.0)
            return []

    async def exercise() -> None:
        controller = _make(tmp_path, configs=[_stdio_definition(workspace)])
        original = controller_module.MCPHost
        controller_module.MCPHost = HangingHost  # type: ignore[assignment]
        try:
            task = asyncio.create_task(
                controller.control("test", idempotency_key="t1", server_id="synth")
            )
            await asyncio.get_running_loop().run_in_executor(None, entered.wait, 30.0)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            rows = _db_rows(tmp_path / "mcp_root", "intents")
            assert rows[0][2] == "interrupted"
            with pytest.raises(MCPControlError) as duplicate:
                await controller.control("test", idempotency_key="t1", server_id="synth")
            assert "interrupted" in str(duplicate.value)
            assert calls["discovers"] == 1  # cancelled outcome never replayed
            assert controller.snapshot()["blocked"] is False  # test is not a mutation
        finally:
            release.set()
            controller_module.MCPHost = original
            await controller.close()

    asyncio.run(exercise())


def test_apply_failure_fails_closed_until_reconcile(tmp_path) -> None:
    workspace = _workspace(tmp_path)

    async def exercise() -> None:
        controller = _make(tmp_path)
        try:
            await controller.control(
                "configure",
                idempotency_key="c1",
                config=_stdio_definition(workspace),
                expected_revision=0,
                reviewed=True,
            )
            original = MCPHost.replace_configs

            async def broken(self, *, actor, configs):
                raise RuntimeError("synthetic-apply-boom")

            MCPHost.replace_configs = broken  # type: ignore[method-assign]
            try:
                with pytest.raises(MCPControlError) as failure:
                    await controller.control(
                        "enable",
                        idempotency_key="e-boom",
                        server_id="synth",
                        expected_revision=1,
                        reviewed=True,
                    )
            finally:
                MCPHost.replace_configs = original
            assert "synthetic-apply-boom" not in str(failure.value)
            snap = controller.snapshot()
            assert snap["blocked"] is True
            assert snap["revision"] == 2  # durable write happened, apply did not
            with pytest.raises(MCPTransportError):
                # Live host is closed: no calls under mismatched config.
                await controller.host.discover("synth")
            with pytest.raises(MCPControlError):
                await controller.control("reconnect", idempotency_key="rc", server_id="synth")
            result = await controller.control(
                "reconcile",
                idempotency_key="rec",
                expected_revision=2,
                reviewed=True,
            )
            assert result == {"status": "reconciled", "revision": 2, "servers": 1}
            snap = controller.snapshot()
            assert snap["blocked"] is False
            assert snap["servers"][0]["enabled"] is True
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_no_secrets_or_args_in_outputs_history_or_errors(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    command_canary = "/opt/canary-bin-XYZ-123"
    arg_canary = "--canary-arg-ABC-456"
    inline_canary = "inline-canary-secret-777"
    secret_value = "canary-secret-value-999"
    definition = _stdio_definition(
        workspace,
        command=command_canary,
        args=[arg_canary],
        env_secret_refs={"SYNTH_TOKEN": "env:SYNTH_TOKEN"},
    )
    resolver = {"SYNTH_TOKEN": secret_value}.get

    async def exercise() -> None:
        controller = _make(tmp_path, resolver=resolver)
        try:
            result = await controller.control(
                "configure",
                idempotency_key="c1",
                config=definition,
                expected_revision=0,
                reviewed=True,
            )
            snap = controller.snapshot()
            dumped = json.dumps(snap)
            for canary in (
                command_canary,
                arg_canary,
                secret_value,
                inline_canary,
                "env:SYNTH_TOKEN",
            ):
                assert canary not in dumped, canary
                assert canary not in json.dumps(result), canary
            assert repr(controller).find(command_canary) == -1
            assert repr(controller).find(arg_canary) == -1
            bad = dict(definition)
            bad["id"] = "inline"
            bad["env_secret_refs"] = {"SYNTH_TOKEN": inline_canary}
            with pytest.raises(MCPControlError) as failure:
                await controller.control(
                    "configure",
                    idempotency_key="c-bad",
                    config=bad,
                    expected_revision=1,
                    reviewed=True,
                )
            assert inline_canary not in str(failure.value)
            assert secret_value not in str(failure.value)
            with pytest.raises(MCPControlError) as failed_test:
                await controller.control("test", idempotency_key="t-dead", server_id="synth")
            assert secret_value not in str(failed_test.value)
            assert command_canary not in str(failed_test.value)
            root = tmp_path / "mcp_root"
            # History and intent records must never carry raw descriptors
            # or secrets (the owner-only servers table is the durable
            # store, protected by mode 0600).
            for table in ("history", "intents"):
                for row in _db_rows(root, table):
                    blob = json.dumps(row, default=str)
                    assert secret_value not in blob, (table, row[0] if row else None)
                    assert command_canary not in blob, table
                    assert arg_canary not in blob, table
        finally:
            await controller.close()

    asyncio.run(exercise())
    # The failed test above attempted a real probe spawn; reap is handled
    # by the temporary host close. Nothing further to assert here.


def test_strict_secret_ref_format(tmp_path) -> None:
    workspace = _workspace(tmp_path)

    async def exercise() -> None:
        controller = _make(tmp_path)
        try:
            for index, ref in enumerate(
                ["rawvalue", "file:/x", "env:bad-name!", "ENV:X", "", "env: space"]
            ):
                bad = _stdio_definition(
                    workspace, id=f"ref{index}", env_secret_refs={"SYNTH_TOKEN": ref}
                )
                with pytest.raises(MCPControlError):
                    await controller.control(
                        "configure",
                        idempotency_key=f"ref-{index}",
                        config=bad,
                        expected_revision=0,
                        reviewed=True,
                    )
            good = _stdio_definition(
                workspace, id="good", env_secret_refs={"SYNTH_TOKEN": "env:OK_NAME_1"}
            )
            result = await controller.control(
                "configure",
                idempotency_key="ref-good",
                config=good,
                expected_revision=0,
                reviewed=True,
            )
            assert result["revision"] == 1
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_real_stdio_test_and_reconnect_never_call_tools(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    if not _sandbox_ok(workspace):
        pytest.skip("Real bubblewrap unavailable")
    _write_server(workspace)

    async def exercise() -> None:
        controller = _make(tmp_path)
        try:
            await controller.control(
                "configure",
                idempotency_key="c1",
                config=_stdio_definition(workspace),
                expected_revision=0,
                reviewed=True,
            )
            probed = await controller.control("test", idempotency_key="t1", server_id="synth")
            assert probed["status"] == "connected"
            assert probed["transport"] == "stdio"
            assert probed["tools"] == [
                {"name": "echo", "allowed": True},
                {"name": "env_probe", "allowed": False},
                {"name": "hidden_tool", "allowed": False},
            ]
            await controller.control(
                "enable",
                idempotency_key="e1",
                server_id="synth",
                expected_revision=1,
                reviewed=True,
            )
            reconnected = await controller.control(
                "reconnect", idempotency_key="r1", server_id="synth"
            )
            assert reconnected["status"] == "connected"
            assert reconnected["server_id"] == "synth"
            assert [tool["name"] for tool in reconnected["tools"]] == [
                "echo",
                "env_probe",
                "hidden_tool",
            ]
            log = workspace / "calls.log"
            if log.exists():
                rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
                assert [row for row in rows if row.get("event") == "call"] == []
            with pytest.raises(MCPControlError):
                await controller.control("test", idempotency_key="t-unknown", server_id="missing")
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_real_loopback_http_test(tmp_path, loopback_http) -> None:
    url, state = loopback_http
    workspace = _workspace(tmp_path)
    policy = ToolPolicy(workspace, network=True)

    async def exercise() -> None:
        controller = _make(
            tmp_path,
            policy=policy,
            resolver=lambda ref: "synthetic" if ref == "env:SYNTH_HDR" else None,
        )
        try:
            await controller.control(
                "configure",
                idempotency_key="c1",
                config={
                    "id": "web",
                    "transport": "http",
                    "url": url,
                    "enabled": False,
                    "allowed_tools": ["echo"],
                    "allow_remote": True,
                    "header_secret_refs": {"X-Synth": "env:SYNTH_HDR"},
                },
                expected_revision=0,
                reviewed=True,
            )
            probed = await controller.control("test", idempotency_key="t1", server_id="web")
            assert probed == {
                "status": "connected",
                "server_id": "web",
                "transport": "http",
                "tools": [
                    {"name": "echo", "allowed": True},
                    {"name": "hidden_tool", "allowed": False},
                ],
            }
            assert "tools/call" not in state["calls"]
            assert "initialize" in state["calls"]
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_reconnect_rules(tmp_path, loopback_http) -> None:
    url, _ = loopback_http
    workspace = _workspace(tmp_path)
    policy = ToolPolicy(workspace, network=True)

    async def exercise() -> None:
        controller = _make(tmp_path, policy=policy)
        try:
            empty = await controller.control("reconnect", idempotency_key="r0")
            assert empty == {"status": "connected", "servers": []}
            await controller.control(
                "configure",
                idempotency_key="c1",
                config={
                    "id": "web",
                    "transport": "http",
                    "url": url,
                    "enabled": False,
                    "allowed_tools": ["echo"],
                    "allow_remote": True,
                },
                expected_revision=0,
                reviewed=True,
            )
            with pytest.raises(MCPControlError) as disabled:
                await controller.control("reconnect", idempotency_key="r1", server_id="web")
            assert "disabled" in str(disabled.value)
            # A failed reconnect never silently enables the target.
            assert controller.snapshot()["servers"][0]["enabled"] is False
            with pytest.raises(MCPControlError):
                await controller.control("reconnect", idempotency_key="r2", server_id="missing")
            await controller.control(
                "enable",
                idempotency_key="e1",
                server_id="web",
                expected_revision=1,
                reviewed=True,
            )
            result = await controller.control("reconnect", idempotency_key="r3", server_id="web")
            assert result["status"] == "connected"
            multi = await controller.control("reconnect", idempotency_key="r4")
            assert [entry["server_id"] for entry in multi["servers"]] == ["web"]
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_control_history_is_bounded_without_evicting_request_identities(
    tmp_path, monkeypatch
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(controller_module, "MAX_CONTROL_INTENTS", 8)

    async def exercise() -> None:
        controller = _make(tmp_path, configs=[_stdio_definition(workspace)])
        try:
            first = None
            for index in range(7):
                result = await controller.control(
                    "disable",
                    idempotency_key=f"hist-{index}",
                    server_id="synth",
                    expected_revision=index,
                )
                if index == 0:
                    first = result
            with pytest.raises(MCPControlError, match="history is full"):
                await controller.control(
                    "disable", idempotency_key="overflow", server_id="synth", expected_revision=7
                )
            assert (
                await controller.control(
                    "disable", idempotency_key="hist-0", server_id="synth", expected_revision=0
                )
                == first
            )
            assert controller.snapshot()["revision"] == 7
            assert len(_db_rows(tmp_path / "mcp_root", "intents")) == 7
            assert len(_db_rows(tmp_path / "mcp_root", "history")) <= 2048
        finally:
            await controller.close()

    asyncio.run(exercise())


def test_snapshot_returns_deep_copies(tmp_path) -> None:
    workspace = _workspace(tmp_path)

    async def exercise() -> None:
        controller = _make(tmp_path, configs=[_stdio_definition(workspace)])
        try:
            first = controller.snapshot()
            first["revision"] = -1
            first["servers"][0]["allowed_tools"].append("injected")
            first["servers"][0]["config_sha256"] = "tampered"
            second = controller.snapshot()
            assert second["revision"] == 0
            assert second["servers"][0]["allowed_tools"] == ["echo"]
            assert second["servers"][0]["config_sha256"] not in (None, "tampered")
            again = await controller.control(
                "disable", idempotency_key="d", server_id="synth", expected_revision=0
            )
            assert again["revision"] == 1
            repeat = await controller.control(
                "disable", idempotency_key="d", server_id="synth", expected_revision=0
            )
            assert repeat == again
        finally:
            await controller.close()

    asyncio.run(exercise())
