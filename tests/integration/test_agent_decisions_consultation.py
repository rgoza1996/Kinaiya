"""Optional Decisions consultation through the ordinary Agent tool loop."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from test_agent_advice_export import TOKENS
from test_agent_advice_operations import env as env
from test_agent_advice_operations import measured
from test_decisions_advice_client import connected
from test_skills_agent import activate, package

from core.policy import ToolPolicy
from packages.contracts.agent_tools import agent_tool_scope, native_tool_snapshot
from packages.contracts.client import JournalClient
from packages.contracts.decisions_consumers import DecisionsAgentPolicyChange
from packages.contracts.models import InterruptRequest, TaskRequest
from packages.contracts.native_tool_schemas import native_tool_definition, native_tool_schema
from packages.providers.meta_action_wire import TypedWireError, action_schema, decode_action
from packages.providers.text import ScriptedProvider
from services import agent, launcher
from services.agent.runtime import AgentRuntime
from services.agent.skill_service import SkillService
from services.decisions.agent_policy_settings import POLICY_STORE_NAME, AgentPolicySettings
from services.session.store import LocalJournal

OBJECTIVE = "Synthetic objective for the complete Agent history."
QUESTION = "  Ask about the original plan Ω.\n"
ASK = {
    "tool": "consult_model",
    "arguments": {"purpose": "decisions_advice", "prompt": QUESTION},
}
READ = {"tool": "read_file", "arguments": {"path": "source.txt"}}


@asynccontextmanager
async def agent_stack(env, monkeypatch, actions, *, hold=None, intercept=None, skill_service=None):
    journal = JournalClient(
        "http://session", TOKENS["decisions"], transport=httpx.ASGITransport(app=env.app)
    )
    env.owner.journal = journal
    wire, attempted, _ = measured(env, monkeypatch, hold=hold)
    workspace = env.catalog.workspace
    (workspace / "source.txt").write_text("Synthetic retained tool observation Ω\n")
    primary = ScriptedProvider([json.dumps(action, ensure_ascii=False) for action in actions])
    try:
        async with connected(env, intercept) as (client, transport):
            runtime = AgentRuntime(
                LocalJournal(env.store, "agent"),
                primary,
                workspace,
                ToolPolicy(workspace),
                max_steps=len(actions),
                decisions_advice=client,
                skill_service=skill_service,
            )
            try:
                task = await runtime.submit(
                    TaskRequest(
                        objective=OBJECTIVE,
                        session_id=env.session.session_id,
                        thread_id=env.session.thread_id,
                        epoch=env.session.epoch,
                        source_event_ids=[env.source.event_id],
                        content_scope="public_synthetic",
                        idempotency_key="agent-worker-advice",
                    )
                )
                yield runtime, task, primary, wire, attempted, transport
            finally:
                wire.release.set()
                wire.cancel_release.set()
                await runtime.close()
    finally:
        await journal.close()


async def test_agent_submits_exact_native_history_and_retains_untrusted_advice(env, monkeypatch):
    async with agent_stack(env, monkeypatch, [READ, ASK, READ]) as stack:
        runtime, task, primary, wire, attempted, transport = stack
        rec = runtime._get(task.task_id)
        await asyncio.wait_for(rec.worker, 5)
        assert transport.calls["requests"] == wire.predictions == 1
        assert len(primary.calls) == 3
        hint = next(
            line
            for line in primary.calls[0][0]["content"].splitlines()
            if line.startswith("- consult_model:")
        )
        assert '"decisions_advice"' in hint and "Available only when" not in hint
        generated = [request for request in attempted if request.purpose == "complete_generation"]
        assert len(generated) == 1
        assert generated[0].task == rec.native_task_identity
        assert generated[0].task.task_id == task.task_id
        assert generated[0].objective_revision == 1
        assert generated[0].source_revisions == {env.source.event_id: 1}
        qualified = [
            packet["parameter"]["history"]["messages"]
            for packet in wire.packets
            if packet.get("endpoint") == "applyPromptTemplate"
        ]
        assert qualified and all(messages == qualified[0] for messages in qualified)
        messages = [(message["role"], message["content"][0]["text"]) for message in qualified[0]]
        assert messages[0] == ("system", env.instructions)
        assert messages[1] == ("user", OBJECTIVE)
        assert messages[2] == ("assistant", json.dumps(READ, ensure_ascii=False))
        assert (
            messages[3][0] == "user" and "Synthetic retained tool observation Ω\n" in messages[3][1]
        )
        assert messages[4] == ("assistant", json.dumps(ASK, ensure_ascii=False))
        assert messages[5] == ("user", QUESTION)
        returned = next(
            item["content"]
            for item in primary.calls[-1]
            if "untrusted_model_output" in item["content"]
        )
        assert (
            json.loads(returned.removeprefix("[ok] "))["untrusted_model_output"] == "Public result"
        )
        evidence = [item for item in rec.task.evidence if item.kind == "external"]
        assert (
            len(evidence) == 1 and '"task_status_assessment": "not_assessed"' in evidence[0].output
        )
        assert "Public result" not in evidence[0].output
        assert not rec._cleanup_uncertain


async def test_optional_unavailable_decisions_leaves_the_agent_tool_loop_usable(env, monkeypatch):
    async def unavailable(asgi, request, route):
        if route == "runtime":
            return httpx.Response(503)
        return await asgi.handle_async_request(request)

    async with agent_stack(env, monkeypatch, [ASK, READ], intercept=unavailable) as stack:
        runtime, task, primary, wire, _, transport = stack
        rec = runtime._get(task.task_id)
        await asyncio.wait_for(rec.worker, 5)
        assert len(primary.calls) == 2
        assert "optional Decisions consultation is unavailable" in primary.calls[-1][-1]["content"]
        assert any(item.kind == "read" for item in rec.task.evidence)
        assert transport.calls["requests"] == wire.predictions == 0
        assert not rec._cleanup_uncertain


async def test_revocation_during_consultation_readiness_refuses_original_dispatch(
    env, monkeypatch, tmp_path
):
    skills = SkillService(tmp_path / "installed-skills", env.catalog.workspace)
    skill_id = await activate(skills, package(tmp_path / "synthetic-recipe"))

    async def revoke(asgi, request, route):
        if route == "runtime":
            await skills.mutate("disable", skill_id=skill_id, authority="owner")
        return await asgi.handle_async_request(request)

    load = {"tool": "skill_load", "arguments": {"skill_id": skill_id}}
    async with agent_stack(
        env, monkeypatch, [load, ASK], intercept=revoke, skill_service=skills
    ) as stack:
        runtime, task, primary, wire, _, transport = stack
        rec = runtime._get(task.task_id)
        await asyncio.wait_for(rec.worker, 5)
        assert len(primary.calls) == 2
        assert skill_id in rec.used_skills
        assert transport.calls["runtime"] == 1
        assert transport.calls["requests"] == 0
        assert wire.predictions == 0


async def test_interrupt_stops_the_original_consultation_before_the_worker_retires(
    env, monkeypatch
):
    async with agent_stack(env, monkeypatch, [ASK, READ], hold="model-reply") as stack:
        runtime, task, primary, wire, _, transport = stack
        rec = runtime._get(task.task_id)
        await asyncio.wait_for(wire.entered.wait(), 5)
        wire.hold_cancel_ack = True
        request = InterruptRequest(
            expected_objective_revision=1, idempotency_key="stop-advice", reason="Synthetic stop"
        )
        await runtime.interrupt(task.task_id, request)
        await asyncio.wait_for(wire.cancel_entered.wait(), 2)
        assert not rec.worker.done()
        assert runtime.workspace_operation_state()["cleanup_pending"] > 0
        wire.cancel_release.set()
        await asyncio.wait_for(rec.worker, 5)
        assert rec.task.state.value == "cancelled" and not rec._cleanup_uncertain
        assert len(primary.calls) == transport.calls["requests"] == transport.calls["cancel"] == 1
        assert all(socket.close_calls == socket.wait_calls == 1 for socket in wire.sockets)


def test_native_and_typed_provider_schemas_advertise_only_configured_decisions_advice():
    baseline = native_tool_snapshot(auxiliary_available=True)
    optional = native_tool_snapshot(decisions_advice=True)
    with agent_tool_scope(baseline):
        previous = action_schema(baseline.available_tools)
        with pytest.raises(TypedWireError):
            decode_action({"action": ASK}, baseline.available_tools)
    with agent_tool_scope(optional):
        schema = action_schema(optional.available_tools)
        assert decode_action({"action": ASK}, optional.available_tools) == ASK
    assert "decisions_advice" not in json.dumps(previous)
    assert "decisions_advice" in json.dumps(schema)
    assert (
        "decisions_advice"
        not in native_tool_schema("consult_model")["properties"]["purpose"]["enum"]
    )
    assert (
        "decisions_advice"
        in native_tool_schema("consult_model", decisions_advice=True)["properties"]["purpose"][
            "enum"
        ]
    )
    configured = native_tool_definition("consult_model", decisions_advice=True)
    assert "captured_auxiliary_model" not in configured["requirements"]
    assert "measured_complete_request" in configured["requirements"]
    assert "captured_auxiliary_model" in native_tool_definition("consult_model")["requirements"]


@pytest.mark.parametrize(
    "with_decisions,with_policy,decisions_host,stored,available",
    [
        (True, True, "127.0.0.1", None, True),
        (False, True, "127.0.0.1", None, False),
        (True, False, "127.0.0.1", None, False),
        (True, True, "localhost", None, False),
        (True, False, "127.0.0.1", "saved", True),
        (False, False, "127.0.0.1", "saved", False),
        (True, True, "127.0.0.1", "disabled", False),
        (True, True, "127.0.0.1", "corrupt", False),
    ],
)
def test_agent_launcher_keeps_decisions_optional_and_owns_the_client_lifespan(
    tmp_path, monkeypatch, with_decisions, with_policy, decisions_host, stored, available
):
    policy = {
        "catalog_binding": {
            "id": "synthetic-advisor",
            "generation": 1,
            "configuration_sha256": "a" * 64,
        },
        "instructions": "Synthetic advice policy.",
    }
    raw = {
        "session_url": "http://127.0.0.1:8760",
        "agent_url": "http://127.0.0.1:8761",
        "decisions_url": f"http://{decisions_host}:8763",
        "distribution": {"agent": True, "decisions": with_decisions},
        "decisions_agent_policy": policy if with_policy else None,
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(raw))
    data, workspace = tmp_path / "data", tmp_path / "workspace"
    store_path = data / POLICY_STORE_NAME
    if stored == "corrupt":
        data.mkdir(mode=0o700)
        store_path.write_text("Synthetic corrupt store")
        store_path.chmod(0o600)
    elif stored is not None:
        settings = AgentPolicySettings(store_path, workspace)
        try:
            settings.change(
                DecisionsAgentPolicyChange(
                    expected_backend=settings.backend,
                    expected_instance=settings.instance,
                    expected_revision=0,
                    idempotency_key="seed-synthetic-state",
                    selection={"source": stored, "policy": policy if stored == "saved" else None},
                ),
                check_policy=lambda _: None,
            )
        finally:
            settings.close()
    app_factory = agent.create_app
    captured = []

    def build(runtime, *args):
        async def recover():
            pass

        monkeypatch.setattr(runtime, "recover", recover)
        client = runtime.decisions_advice
        assert (client is not None) is available
        assert runtime.host_tool_snapshot().decisions_advice is available
        captured.append(client)
        return app_factory(runtime, *args)

    def listen(app, **kwargs):
        async def lifecycle():
            async with app.router.lifespan_context(app):
                if available:
                    assert not captured[0]._http.is_closed
            if available:
                assert captured[0]._http.is_closed

        asyncio.run(lifecycle())

    monkeypatch.setattr(agent, "create_app", build)
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda _: TOKENS)
    monkeypatch.setattr(launcher, "provider", lambda *args, **kwargs: ScriptedProvider([]))
    monkeypatch.setattr(uvicorn, "run", listen)
    launcher.serve(
        SimpleNamespace(
            config=config,
            data_dir=data,
            workspace=workspace,
            service="agent",
            port=None,
        )
    )
    assert len(captured) == 1
    assert config.read_text() == json.dumps(raw)
    if stored is None:
        assert not store_path.exists()
