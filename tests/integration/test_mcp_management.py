"""Management authority and task-admission races without paid inference."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from core.policy import ToolPolicy
from packages.commands import Registry
from packages.contracts.models import (
    CommandRequest,
    InterruptRequest,
    MCPControl,
    SessionRequest,
    TaskRequest,
)
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.agent.mcp_controls import MCPManagementBusy, apply_mcp_control
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


class ManagedFixture:
    """Labeled controller fixture; actual transports are tested separately."""

    def __init__(self):
        self.host = self
        self.calls = []
        self.retired = 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()

    def list_servers(self):
        return []

    def snapshot(self):
        return {"schema_version": "1.0", "revision": 0, "blocked": False, "servers": []}

    async def control(self, operation, **fields):
        self.calls.append((operation, fields))
        self.entered.set()
        await self.release.wait()
        return {"status": "connected", "server_id": fields.get("server_id")}

    async def close(self):
        pass

    async def retire_connections(self, *, actor):
        assert actor == "owner"
        self.retired += 1


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "public.txt").write_text("synthetic native evidence")
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    provider = ScriptedProvider([])
    controller = ManagedFixture()
    journal = LocalJournal(store, "agent")
    runtime = AgentRuntime(
        journal, provider, workspace, ToolPolicy(workspace), mcp_controller=controller
    )
    registry = Registry(runtime, LocalJournal(store, "owner"))
    yield SimpleNamespace(
        runtime=runtime,
        controller=controller,
        provider=provider,
        store=store,
        session=session,
        journal=journal,
        commands=registry,
    )
    controller.release.set()
    registry.close()
    await runtime.close()
    store.close()


def control():
    return MCPControl(
        operation="test", server_id="synthetic", expected_revision=0, idempotency_key="test"
    )


def task_request(env):
    s = env.session
    return TaskRequest(
        session_id=s.session_id,
        thread_id=s.thread_id,
        epoch=s.epoch,
        objective="Read public.txt",
        idempotency_key="task",
    )


def command(env, text, key):
    s = env.session
    return CommandRequest(
        session_id=s.session_id,
        thread_id=s.thread_id,
        epoch=s.epoch,
        input=text,
        idempotency_key=key,
    )


async def test_management_requires_owner_and_never_enters_inference(env):
    tokens = {
        role: role + "-synthetic-token-0123456789"
        for role in (
            "owner",
            "agent",
            "converse",
            "decisions",
            "resources",
        )
    }
    app = create_app(env.runtime, Auth(tokens))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for role in ("agent", "converse", "decisions", "resources"):
            headers = {"Authorization": "Bearer " + tokens[role]}
            assert (await client.get("/v1/mcp/management", headers=headers)).status_code == 403
            assert (
                await client.post("/v1/mcp/control", headers=headers, json=control().model_dump())
            ).status_code == 403
        headers = {"Authorization": "Bearer " + tokens["owner"]}
        response = await client.get("/v1/mcp/management", headers=headers)
        assert response.json()["configured"] is True
        response = await client.post(
            "/v1/mcp/control", headers=headers, json=control().model_dump()
        )
        assert response.status_code == 200 and response.json()["status"] == "connected"
    assert env.provider.calls == []


async def test_mcp_command_is_agent_only_and_a_runtime_control(env):
    definition = next(row for row in env.commands.definitions()["commands"] if row["name"] == "mcp")
    assert definition["execution_type"] == "runtime_control"
    assert definition["surfaces"] == ["agent-chat", "agent-cli"]
    response = await env.commands.execute(command(env, "/mcp list", "list"))
    assert response["status"] == "ok" and env.provider.calls == []
    other = env.store.create_session(SessionRequest(channel="converse"))
    request = command(env, "/mcp list", "converse").model_copy(
        update={
            "session_id": other.session_id,
            "thread_id": other.thread_id,
        }
    )
    with pytest.raises(ValueError, match="Agent session"):
        await env.commands.execute(request)


async def test_active_task_prevents_definition_replacement(env):
    entered = asyncio.Event()

    async def hung(messages):
        entered.set()
        await asyncio.Event().wait()

    env.provider.complete = hung
    await env.runtime.submit(task_request(env))
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(MCPManagementBusy):
        await apply_mcp_control(env.runtime, control())
    assert env.controller.calls == []


async def test_task_admission_cannot_race_management_idle_check(env):
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.journal.session

    async def delayed(session_id):
        entered.set()
        await release.wait()
        return await original(session_id)

    env.journal.session = delayed
    submission = asyncio.create_task(env.runtime.submit(task_request(env)))
    await asyncio.wait_for(entered.wait(), 1)
    management = asyncio.create_task(apply_mcp_control(env.runtime, control()))
    await asyncio.sleep(0)
    assert not management.done() and not env.controller.calls
    release.set()
    await submission
    with pytest.raises(MCPManagementBusy):
        await management


async def test_management_reservation_precedes_new_task_admission(env):
    env.controller.release.clear()
    management = asyncio.create_task(apply_mcp_control(env.runtime, control()))
    await asyncio.wait_for(env.controller.entered.wait(), 1)
    env.provider.responses.extend(
        [
            '{"tool":"read_file","arguments":{"path":"public.txt"}}',
            '{"tool":"finish","arguments":{"objective_revision":1,"summary":"Read file"}}',
        ]
    )
    submission = asyncio.create_task(env.runtime.submit(task_request(env)))
    await asyncio.sleep(0)
    assert not submission.done() and env.provider.calls == []
    env.controller.release.set()
    await management
    accepted = await submission
    assert (await env.runtime.wait(accepted.task_id)).state == "succeeded"


async def test_slow_mcp_command_does_not_block_status_or_stop(env):
    env.controller.release.clear()
    managing = asyncio.create_task(
        env.commands.execute(command(env, "/mcp test synthetic", "slow"))
    )
    await asyncio.wait_for(env.controller.entered.wait(), 1)
    try:
        status = await asyncio.wait_for(env.commands.execute(command(env, "/status", "status")), 1)
        assert status["status"] == "ok" and status["result"] == []
        stopped = await asyncio.wait_for(env.commands.execute(command(env, "/stop", "stop")), 1)
        assert stopped["status"] == "error" and "No matching" in stopped["error"]
        duplicate = await env.commands.execute(command(env, "/mcp test synthetic", "slow"))
        assert duplicate["status"] == "error" and len(env.controller.calls) == 1
    finally:
        env.controller.release.set()
        await managing


async def test_owner_can_stop_recovered_paused_work_before_managing_mcp(env):
    entered = asyncio.Event()

    async def hung(messages):
        entered.set()
        await asyncio.Event().wait()

    env.provider.complete = hung
    accepted = await env.runtime.submit(task_request(env))
    await asyncio.wait_for(entered.wait(), 1)
    await env.runtime.close()
    # Native recovery creates no worker and never repeats a model/tool call.
    env.runtime = AgentRuntime(
        env.journal,
        ScriptedProvider([]),
        env.runtime.workspace,
        env.runtime.policy,
        mcp_controller=env.controller,
    )
    await env.runtime.recover()
    with pytest.raises(MCPManagementBusy):
        await apply_mcp_control(env.runtime, control())
    await env.runtime.interrupt(
        accepted.task_id,
        InterruptRequest(
            expected_objective_revision=1,
            idempotency_key="stop-recovered",
        ),
    )
    result = await env.runtime.wait(accepted.task_id, timeout=0.1)
    assert result.state == "cancelled"
    assert "uncertain" in result.summary
    assert (await apply_mcp_control(env.runtime, control()))["status"] == "connected"


async def test_repeated_stop_retains_worker_until_cleanup_finishes(env, monkeypatch):
    import services.agent.runtime as module

    entered, cleaning, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await released.wait()
            raise

    monkeypatch.setattr(module, "run_mcp_list", blocked)
    env.provider.responses.append('{"tool":"mcp_list","arguments":{}}')
    accepted = await env.runtime.submit(task_request(env))
    await asyncio.wait_for(entered.wait(), 1)
    await env.runtime.interrupt(
        accepted.task_id,
        InterruptRequest(
            expected_objective_revision=1,
            idempotency_key="first-stop",
        ),
    )
    await asyncio.wait_for(cleaning.wait(), 1)
    worker = env.runtime._get(accepted.task_id).worker
    try:
        await env.runtime.interrupt(
            accepted.task_id,
            InterruptRequest(
                expected_objective_revision=1,
                idempotency_key="second-stop",
            ),
        )
        assert worker is not None and not worker.done() and worker.cancelling() == 1
        with pytest.raises(MCPManagementBusy):
            await apply_mcp_control(env.runtime, control())
    finally:
        released.set()
    assert (await env.runtime.wait(accepted.task_id)).state == "cancelled"


async def test_yolo_expansion_waits_for_idle_but_revocation_remains_available(env):
    entered = asyncio.Event()

    async def hung(messages):
        entered.set()
        await asyncio.Event().wait()

    env.provider.complete = hung
    await env.runtime.submit(task_request(env))
    await asyncio.wait_for(entered.wait(), 1)
    expanded = await env.commands.execute(command(env, "/yolo on", "expand"))
    assert expanded["status"] == "error" and not env.runtime.policy.yolo
    env.runtime.policy.set_permissions(actor="owner", yolo=True)
    revoked = await asyncio.wait_for(env.commands.execute(command(env, "/yolo off", "revoke")), 1)
    assert revoked["status"] == "ok" and not env.runtime.policy.yolo
    assert env.controller.retired == 1 and env.controller.calls == []


async def test_recovery_cannot_land_between_management_idle_check_and_apply(env):
    entered, released = asyncio.Event(), asyncio.Event()
    original = env.journal.read

    async def delayed(**kwargs):
        entered.set()
        await released.wait()
        return await original(**kwargs)

    env.journal.read = delayed
    recovering = asyncio.create_task(env.runtime.recover())
    await asyncio.wait_for(entered.wait(), 1)
    managing = asyncio.create_task(apply_mcp_control(env.runtime, control()))
    await asyncio.sleep(0)
    assert not managing.done() and not env.controller.calls
    released.set()
    await recovering
    assert (await managing)["status"] == "connected"


def test_reviewed_flag_and_revision_do_not_coerce_untrusted_values():
    for fields in ({"reviewed": "true"}, {"expected_revision": True}):
        with pytest.raises(ValueError):
            MCPControl(operation="enable", server_id="synth", idempotency_key="x", **fields)
