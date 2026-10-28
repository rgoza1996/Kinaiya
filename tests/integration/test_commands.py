"""Controls remain available with hung inference and durable transport replay."""

import asyncio

import httpx
import pytest

from core.policy import ToolPolicy
from packages.commands import Registry, parse
from packages.contracts.models import CommandRequest, ProviderInfo, SessionRequest, TaskRequest
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime, create_app
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


@pytest.mark.parametrize(
    "text",
    [r"\/stop", "/tmp/file.txt", '"/stop"', "```\n/stop\n```", "/stop\nnow", "discuss /stop"],
)
def test_literal_input_is_not_a_control(text):
    assert parse(text) is None


def test_quoting_and_unicode_arguments():
    assert parse('/new "A quiet café"') == ("new", ["A quiet café"])


async def test_registry_distinguishes_inference_from_owner_controls(env):
    _, _, runtime, registry = env
    definitions = {row["name"]: row for row in registry.definitions()["commands"]}
    assert definitions["skill"]["execution_type"] == "skill_invocation"
    for name in ("status", "stop", "permissions", "model", "skills", "resources"):
        assert definitions[name]["execution_type"] == "runtime_control"
    assert runtime.provider.calls == []


@pytest.mark.parametrize(
    "suffix,task", [("--gpu-bytes 1073741824", None), ("target --gpu-bytes 1073741824", "target")]
)
async def test_gpu_handoff_is_explicit_owner_control_without_inference(env, suffix, task):
    _, session, runtime, registry = env
    seen = []

    class Client:
        async def action(self, payload):
            seen.append(payload)
            return {"ok": True}

        async def close(self):
            pass

    runtime.resource_client = Client()
    result = await registry.execute(request(session, "/resources handoff 268435456 " + suffix))
    assert result["status"] == "ok" and runtime.provider.calls == []
    assert seen[0]["required_gpu_bytes"] == 1073741824
    assert seen[0]["required_bytes"] == 268435456
    assert seen[0].get("task_id") == task


@pytest.mark.parametrize(
    "suffix", ["--gpu-bytes", "--gpu-bytes 1 --gpu-bytes 2", "--gpu-bytes 1 misplaced-task"]
)
async def test_malformed_gpu_handoff_never_contacts_controller(env, suffix):
    _, session, runtime, registry = env

    class Client:
        async def action(self, payload):
            pytest.fail("malformed handoff reached controller")

        async def close(self):
            pass

    runtime.resource_client = Client()
    result = await registry.execute(request(session, "/resources handoff 268435456 " + suffix))
    assert result["status"] == "error" and runtime.provider.calls == []


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = ScriptedProvider([])
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace))
    registry = Registry(runtime, LocalJournal(store, "owner"), path=tmp_path / "commands.sqlite")
    yield store, session, runtime, registry
    registry.close()
    await runtime.close()
    store.close()


def request(session, text, key="k"):
    return CommandRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        input=text,
        idempotency_key=key,
    )


async def test_unknown_control_never_reaches_model_and_is_journaled(env):
    store, s, r, commands = env
    result = await commands.execute(request(s, "/stpo"))
    assert result["status"] == "error" and "stop" in result["suggestions"]
    assert r.provider.calls == []
    assert store.read(kinds=["agent.command"])[0].origin == "agent-supervisor"


async def test_durable_duplicate_new_session_and_conflict(env, tmp_path):
    store, s, r, commands = env
    body = request(s, '/new "New study"')
    first = await commands.execute(body)
    other = Registry(r, LocalJournal(store, "owner"), path=tmp_path / "commands.sqlite")
    try:
        assert await other.execute(body) == first
        assert len(store.sessions()) == 2
        with pytest.raises(ValueError, match="conflicts"):
            await other.execute(request(s, "/yolo on"))
    finally:
        other.close()


async def test_owner_authority_and_agent_surface_are_required(env):
    store, s, r, commands = env
    with pytest.raises(PermissionError):
        await commands.execute(request(s, "/yolo on"), actor="decisions")
    converse = store.create_session(SessionRequest(channel="converse"))
    with pytest.raises(ValueError, match="Agent session"):
        await commands.execute(request(converse, "/stop"))
    assert not r.policy.yolo


async def test_session_selection_and_clear_preserve_truth(env):
    store, s, r, commands = env
    result = await commands.execute(request(s, "/resume " + s.session_id))
    assert result["result"]["session_id"] == s.session_id
    clear = await commands.execute(request(s, "/clear", "clear"))
    assert clear["result"] == {"clear_view": True, "journal_preserved": True}
    assert store.read(kinds=["agent.command"]) and store.session(s.session_id)


async def test_owner_permission_change_never_lifts_workspace_boundary(env, tmp_path):
    store, s, r, commands = env
    assert (await commands.execute(request(s, "/yolo on")))["result"]["yolo"]
    from core.policy import PolicyDenied

    with pytest.raises(PolicyDenied):
        r.policy.resolve_path(tmp_path / "secret")
    assert not (await commands.execute(request(s, "/yolo off", "off")))["result"]["yolo"]


async def test_status_stop_do_not_wait_for_hung_provider(env):
    store, s, r, commands = env
    entered = asyncio.Event()

    class Hung:
        info = ProviderInfo(provider="fixture", model="hung", local=True)

        async def complete(self, messages):
            entered.set()
            await asyncio.Event().wait()

    r.provider = Hung()
    task = await r.submit(
        TaskRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=1,
            objective="Synthetic read",
            idempotency_key="task",
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    status = await asyncio.wait_for(commands.execute(request(s, "/status")), 1)
    assert status["result"][0]["state"] == "running"
    stopped = await asyncio.wait_for(commands.execute(request(s, "/stop", "stop")), 1)
    assert stopped["result"]["state"] == "cancel_requested"
    assert (await r.wait(task.task_id)).state == "cancelled"


async def test_command_api_requires_owner_token(env):
    store, s, r, commands = env
    tokens = {
        role: role + "-test-token-0123456789abcdef"
        for role in ("owner", "agent", "converse", "decisions", "resources")
    }
    app = create_app(r, Auth(tokens), commands)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for role in ("converse", "decisions"):
            response = await client.post(
                "/v1/commands/execute",
                json=request(s, "/yolo on").model_dump(),
                headers={"Authorization": "Bearer " + tokens[role]},
            )
            assert response.status_code == 403
        response = await client.get(
            "/v1/commands", headers={"Authorization": "Bearer " + tokens["owner"]}
        )
        assert response.status_code == 200
        assert all(
            d["surfaces"] == ["agent-chat", "agent-cli"] for d in response.json()["commands"]
        )
