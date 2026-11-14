"""Public fixtures exercise the complete managed-workspace shell path.

The provider is deterministic. Session storage, native workspace ownership,
runtime preferences, scoped permissions, Bubblewrap and process cleanup are
real. Public HTTPS is separately opted in; no model or private input is used.
"""

import json
import os
from contextlib import asynccontextmanager

import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.models import SessionRequest, TaskRequest, TaskState
from packages.providers.text import ScriptedProvider
from services.agent import tools
from services.agent.runtime import AgentRuntime
from services.agent.runtime_settings import AgentRuntimeSettings
from services.agent.tool_permissions import AgentToolPermissions
from services.agent.workspace_settings import AgentWorkspaceSettings
from services.session.store import LocalJournal, Store


@asynccontextmanager
async def managed_runtime(tmp_path, command, *, readonly="file", deny_write=False):
    workspace, control = tmp_path / "workspace", tmp_path / "control"
    workspace.mkdir(mode=0o700)
    control.mkdir(mode=0o700)
    reference = workspace / "public-reference.txt"
    reference.write_text("Public synthetic read-only reference.\n")
    roots = {"none": (), "file": (reference,), "workspace": (workspace,)}[readonly]
    store = Store(control / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "tool": "shell",
                    "arguments": {"command": command, "timeout_s": 30, "objective_revision": 1},
                }
            ),
            json.dumps(
                {
                    "tool": "finish",
                    "arguments": {"summary": "Public fixture complete.", "objective_revision": 1},
                }
            ),
        ]
    )
    policy = ToolPolicy(
        workspace,
        allow_shell=True,
        allow_write=True,
        deny_write=deny_write,
        network=True,
        shell_working_directory=".",
    )
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, policy, max_steps=2)
    try:
        owner = AgentWorkspaceSettings(
            workspace,
            path=control / "workspace.sqlite",
            control_roots=(control,),
            host_readonly_roots=roots,
        )
        runtime.workspace_settings = owner
        context = owner.initial_context()
        policy.install_workspace_context(
            context, actor="owner", available=lambda: owner.active_context() is context
        )
        assert owner.bind(
            runtime,
            operation_state=runtime.workspace_operation_state,
            retire=runtime.retire_workspace,
            apply=lambda value: runtime.prepare_workspace_apply(value).apply(),
            prepare=runtime.prepare_workspace_apply,
        )
        runtime.runtime_settings = AgentRuntimeSettings(runtime, path=control / "runtime.sqlite")
        runtime.tool_permissions = AgentToolPermissions(
            runtime, path=control / "permissions.sqlite"
        )
        assert context.pin.mode == "direct"
        assert runtime.runtime_settings.snapshot()["settings"]["working_directory"] == "."
        yield runtime, store, session, reference
    finally:
        try:
            await runtime.close()
            assert all(
                record.proc_slot.get("proc") is None
                and (record.worker is None or record.worker.done())
                for record in runtime._tasks.values()
            )
        finally:
            store.close()


@pytest.mark.parametrize("readonly", ["none", "file", "workspace"])
@pytest.mark.parametrize("deny_write", [False, True])
async def test_managed_root_fd_is_recognized_and_never_replaced_by_a_host_path(
    tmp_path, monkeypatch, readonly, deny_write
):
    monkeypatch.setattr(tools, "bwrap_path", lambda: "/usr/bin/bwrap")
    async with managed_runtime(tmp_path, "pwd", readonly=readonly, deny_write=deny_write) as env:
        runtime, _, _, _ = env
        policy = runtime.policy
        with pytest.raises(PolicyDenied, match="requires a pinned Bubblewrap mount"):
            tools.build_bwrap_args(policy, "pwd", True)
        with tools._readonly_bindings(policy) as (bindings, descriptors):
            root = [binding for binding in bindings if binding[2] == "/workspace"]
            assert len(root) == 1
            assert all(type(binding[2]) is str for binding in bindings)
            assert root[0][1] in descriptors
            argv, _ = tools.build_bwrap_args(policy, "pwd", True, readonly_bindings=bindings)
            position = argv.index("/workspace")
            expected_kind = "--ro-bind-fd" if deny_write or readonly == "workspace" else "--bind-fd"
            assert argv[position - 2 : position + 1] == [
                expected_kind,
                str(root[0][1]),
                "/workspace",
            ]
            assert str(policy.workspace) not in argv
            assert all(type(value) is str for value in argv)
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)


async def run_public_command(tmp_path, command, *, readonly="file"):
    try:
        await tools.run_shell(ToolPolicy(tmp_path, allow_shell=True), "true", 5, 1)
    except tools.ToolFailure as error:
        pytest.skip(f"Basic OS sandbox unavailable: {error}")
    async with managed_runtime(tmp_path, command, readonly=readonly) as env:
        runtime, store, session, reference = env
        original = reference.read_bytes()
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Run the fixed public diagnostic command.",
                content_scope="public_synthetic",
                idempotency_key="managed-public-command",
            )
        )
        final = await runtime.wait(task.task_id, timeout=40)
        # #580: preserve the actual public fixture failure behind the evaluator's
        # refusal. This diagnostic runs only after the unchanged assertion fails;
        # it contains no environment, credentials or full conversation history.
        assert final.state == TaskState.SUCCEEDED, {
            "task_error": final.error,
            "commands": [
                {"exit_code": item.exit_code, "output_tail": item.output[-1000:]}
                for item in final.evidence
                if item.kind == "command"
            ],
            "last_observation": [
                message["content"][-1000:]
                for message in runtime._tasks[task.task_id].history
                if message.get("role") == "user" and isinstance(message.get("content"), str)
            ][-1:],
        }
        evidence = [item for item in final.evidence if item.kind == "command"]
        assert len(evidence) == 1 and evidence[0].exit_code == 0
        events = store.read(task_id=task.task_id, kinds=["agent.tool"])
        assert len(events) == 1 and events[0].payload["tool"] == "shell"
        assert events[0].payload["ok"] is True
        assert events[0].payload["permission_admission"]["kind"] == "host"
        assert reference.read_bytes() == original
        return evidence[0], runtime.workspace


@pytest.mark.sandbox
@pytest.mark.parametrize("readonly", ["none", "file", "workspace"])
async def test_actual_runtime_settings_permissions_and_pinned_workspace_execute_pwd(
    tmp_path, readonly
):
    evidence, _ = await run_public_command(tmp_path, "pwd", readonly=readonly)
    assert "/workspace" in evidence.output


@pytest.mark.sandbox
@pytest.mark.parametrize("readonly,writable", [("none", True), ("workspace", False)])
async def test_actual_managed_workspace_mount_preserves_write_boundary(
    tmp_path, readonly, writable
):
    command = "touch /workspace/public-write-probe && pwd"
    if not writable:
        command = "! " + command
    evidence, workspace = await run_public_command(tmp_path, command, readonly=readonly)
    assert "/workspace" in evidence.output
    assert (workspace / "public-write-probe").exists() is writable


@pytest.mark.sandbox
@pytest.mark.live
async def test_opt_in_public_https_through_managed_runtime(tmp_path):
    if os.environ.get("KINAIYA_PUBLIC_HTTPS_TEST") != "1":
        pytest.skip("Set KINAIYA_PUBLIC_HTTPS_TEST=1 for the explicit public Python.org GET")
    command = (
        "curl --fail --silent --show-error --proto '=https' --proto-redir '=https' "
        "--max-time 15 --max-filesize 65536 --output /workspace/public-robots.txt "
        "https://www.python.org/robots.txt"
    )
    _, workspace = await run_public_command(tmp_path, command)
    assert 0 < (workspace / "public-robots.txt").stat().st_size <= 65536
