"""Native advertisement and execution fences through the actual Meta adapter.

All HTTP inference responses are public synthetic MockTransport fixtures.
Configured selector metadata is used only to inspect advertisement, never to
claim that a generated extension is installed, approved or executable.
"""

import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jsonschema
import pytest

from core.policy import ToolPolicy
from packages.contracts.agent_tools import (
    NATIVE_TOOL_ORDER,
    current_agent_tools,
    native_tool_snapshot,
)
from packages.contracts.models import SessionRequest, Task, TaskRequest, TaskState
from packages.providers.text import MetaResponsesProvider
from services.agent import AgentRuntime, create_app
from services.agent.artifacts import ArtifactStore
from services.common.auth import Auth
from services.session.store import LocalJournal, Store

BASE_FIELDS = {
    "path",
    "content",
    "pattern",
    "command",
    "summary",
    "objective_revision",
    "max_bytes",
    "after",
    "limit",
    "timeout_s",
    "event_ids",
}


def wire_action(body, tool, *, validate=True, **arguments):
    schema = body["text"]["format"]["schema"]
    fields = schema["properties"]["arguments"]["properties"]
    action = {"tool": tool, "arguments": {**dict.fromkeys(fields), **arguments}}
    if validate:
        jsonschema.Draft202012Validator(schema).validate(action)
    return action


def publication():
    return {
        "title": "Public synthetic result",
        "summary": "Fictional fixture data only.",
        "components": [{"id": "summary", "type": "text", "data": {"text": "Synthetic 42"}}],
    }


def assert_advertisement(body, snapshot):
    schema = body["text"]["format"]["schema"]
    assert schema["properties"]["tool"]["enum"] == list(snapshot.available_tools)
    arguments = schema["properties"]["arguments"]
    fields = set(BASE_FIELDS)
    for tool, names in (
        ("select_context", {"max_chars"}),
        ("skill_load", {"skill_id", "resource"}),
        ("plugin_call", {"plugin_id", "plugin_arguments_json"}),
        ("mcp_call", {"server_id", "mcp_tool", "mcp_arguments_json"}),
        ("publish_ui", {"ui_spec_json"}),
        ("tool_catalog", {"kind", "name", "query", "server_id"}),
    ):
        if snapshot.supports(tool):
            fields.update(names)
    assert set(arguments["properties"]) == fields
    assert set(arguments["required"]) == fields
    assert arguments["additionalProperties"] is False
    hints = body["input"][0]["content"]
    for name in NATIVE_TOOL_ORDER:
        assert (f"- {name}:" in hints) == snapshot.supports(name)
    for name in ("publish_ui", "plugin_call", "mcp_call"):
        assert (f"For {name}," in hints) == snapshot.supports(name)
    user = body["input"][1]["content"]
    assert "Grants: write=False shell=False network=False" in user
    assert ("UI artifacts: configured;" in user) == snapshot.supports("publish_ui")
    assert ("Skills: configured;" in user) == snapshot.supports("skills_list")
    assert ("Compute plugins: configured;" in user) == snapshot.supports("plugins_list")


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = "Public synthetic fixture value: 42\n"
    (workspace / "facts.txt").write_text(content)
    journal = Store(tmp_path / "journal.sqlite")
    session = journal.create_session(SessionRequest(channel="agent", harness_version="seed-v1"))
    state = SimpleNamespace(calls=[], counts=[], replies=[], stores=[], content=content)

    async def respond(request):
        assert str(request.url) in {
            "https://synthetic.invalid/v1/responses",
            "https://synthetic.invalid/v1/responses/input_tokens",
        }
        body = json.loads(request.content)
        snapshot = current_agent_tools()
        assert snapshot is not None
        assert_advertisement(body, snapshot)
        if request.url.path == "/v1/responses/input_tokens":
            assert body["stream"] is False
            state.counts.append(SimpleNamespace(body=body, snapshot=snapshot))
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 123}
            )
        if state.counts:
            assert body == state.counts[-1].body
            assert snapshot is state.counts[-1].snapshot
        state.calls.append(SimpleNamespace(body=body, snapshot=snapshot))
        assert state.replies, "Unexpected synthetic inference request"
        action = await state.replies.pop(0)(body)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": json.dumps(action)}],
                    }
                ],
            },
        )

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-advertising-fixture",
        allow_cloud=True,
        structured_actions=True,
        reasoning_effort="minimal",
        context_tokens=32768,  # Verified synthetic count fixture only.
        transport=httpx.MockTransport(respond),
    )
    runtime = AgentRuntime(
        LocalJournal(journal, "agent"), provider, workspace, ToolPolicy(workspace)
    )
    tokens = {role: f"public-synthetic-{role}-token-0123456789" for role in ("owner", "decisions")}
    state.runtime, state.provider, state.journal = runtime, provider, journal
    state.session, state.workspace = session, workspace
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, Auth(tokens))),
        base_url="http://synthetic-owner",
        headers={"Authorization": "Bearer " + tokens["owner"]},
    ) as client:
        state.client = client
        try:
            yield state
        finally:
            attached = runtime.artifact_store
            await runtime.close()
            for store in state.stores:
                if store is not attached:
                    store.close()
            await provider.close()
            journal.close()


async def read_action(body):
    return wire_action(body, "read_file", path="facts.txt")


async def finish_action(body):
    return wire_action(
        body, "finish", summary="Read the public synthetic fixture", objective_revision=1
    )


async def submit_and_wait(env):
    request = TaskRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        objective="Read facts.txt and report its public synthetic value.",
        content_scope="public_synthetic",
        idempotency_key="synthetic-tool-advertising",
    )
    response = await env.client.post("/v1/agent/tasks", json=request.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    task = Task.model_validate(response.json())
    return await env.runtime.wait(task.task_id, timeout=3)


async def assert_read_only_inspection(env, task, *, generic_snapshot=None):
    before = await env.runtime.status(task.task_id)
    entries = env.journal.read(session_id=env.session.session_id)
    count = len(env.calls)
    task_snapshot = env.runtime.tool_snapshot(task)
    for params, snapshot in (
        ({}, generic_snapshot or env.runtime.tool_snapshot()),
        ({"task_id": task.task_id}, task_snapshot),
    ):
        response = await env.client.get("/v1/agents/capabilities", params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["tools"] == list(snapshot.available_tools)
        assert result["tool_catalog"] == {
            "schema_version": "1.0",
            "scope": "native_gateways",
            "task_id": params.get("task_id"),
            "task_state": task.state.value if params else None,
            "items": [asdict(descriptor) for descriptor in snapshot.descriptors],
        }
        expected_harness = (
            task.harness_version
            if params
            else env.runtime.selector_registry.active
            if env.runtime.selector_registry is not None
            else "seed-v1"
        )
        assert result["harness"] == expected_harness
    assert (
        await env.client.get("/v1/agents/capabilities", params={"task_id": "missing"})
    ).status_code == 404
    assert (
        await env.client.get("/v1/agents/capabilities", headers={"Authorization": ""})
    ).status_code == 401
    assert await env.runtime.status(task.task_id) == before
    assert env.journal.read(session_id=env.session.session_id) == entries
    assert len(env.calls) == count


async def test_actual_read_finish_wire_prompt_snapshot_and_api_agree(env):
    async def inspect_then_read(body):
        (task,) = await env.runtime.status()
        await assert_read_only_inspection(env, task)
        return await read_action(body)

    env.replies.extend([inspect_then_read, finish_action])
    final = await submit_and_wait(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert len(env.calls) == 2
    assert [call.body for call in env.counts] == [call.body for call in env.calls]
    assert all(call.snapshot == native_tool_snapshot(catalog_available=True) for call in env.calls)
    assert [evidence.kind for evidence in final.evidence] == ["read"]
    assert final.evidence[0].sha256 == hashlib.sha256(env.content.encode()).hexdigest()
    assert (env.workspace / "facts.txt").read_text() == env.content
    outcomes = env.journal.read(session_id=env.session.session_id, kinds=["agent.outcome"])
    assert len(outcomes) == 1 and outcomes[0].payload["state"] == "succeeded"
    assert Task.model_validate(outcomes[0].payload["task"]) == final
    await assert_read_only_inspection(env, final)
    assert current_agent_tools() is None


async def test_seed_task_keeps_its_tool_set_after_selector_metadata_is_configured(env):
    active = "v-" + "a" * 24

    async def configure_metadata_then_read(body):
        (task,) = await env.runtime.status()
        assert task.harness_version == "seed-v1"
        # Metadata-only fake: no generated code, evaluation, promotion or execution.
        env.runtime.selector_registry = SimpleNamespace(active=active)
        assert env.runtime.tool_snapshot().supports("select_context")
        assert not env.runtime.tool_snapshot(task).supports("select_context")
        await assert_read_only_inspection(
            env,
            task,
            generic_snapshot=native_tool_snapshot(selector_available=True, catalog_available=True),
        )
        generic = (await env.client.get("/v1/agents/capabilities")).json()
        assert generic["harness"] == active
        return await read_action(body)

    env.replies.extend([configure_metadata_then_read, finish_action])
    final = await submit_and_wait(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert final.harness_version == "seed-v1"
    assert len(env.calls) == 2
    assert all(not call.snapshot.supports("select_context") for call in env.calls)
    await assert_read_only_inspection(
        env,
        final,
        generic_snapshot=native_tool_snapshot(selector_available=True, catalog_available=True),
    )


@pytest.mark.parametrize("change", ["unconfigured", "appears", "disappears"])
async def test_unavailable_publication_never_executes_or_loses_required_effect_failure(
    env, monkeypatch, tmp_path, change
):
    native_publish = AsyncMock(
        side_effect=AssertionError("Unavailable native publication executed")
    )
    monkeypatch.setattr(env.runtime, "_publish_ui", native_publish)
    store = None
    if change != "unconfigured":
        store = ArtifactStore(tmp_path / "synthetic-artifacts.sqlite3")
        env.stores.append(store)
    if change == "disappears":
        env.runtime.artifact_store = store

    async def propose_unavailable_publication(body):
        advertised = env.calls[-1].snapshot
        assert advertised.supports("publish_ui") == (change == "disappears")
        if change == "appears":
            env.runtime.artifact_store = store
        elif change == "disappears":
            env.runtime.artifact_store = None
        (task,) = await env.runtime.status()
        current = env.runtime.tool_snapshot(task)
        assert current.supports("publish_ui") == (change == "appears")
        assert current_agent_tools() is advertised
        await assert_read_only_inspection(env, task)
        return wire_action(
            body,
            "publish_ui",
            validate=change == "disappears",
            ui_spec_json=json.dumps(publication()),
        )

    async def read_after_rejection(body):
        expected = native_tool_snapshot(
            artifacts_available=change == "appears", catalog_available=True
        )
        assert env.calls[-1].snapshot == expected
        observations = [
            message["content"] for message in body["input"] if message["role"] == "user"
        ]
        assert any(
            "publish_ui" in item and "unavailable for this task" in item for item in observations
        )
        native_publish.assert_not_awaited()
        return await read_action(body)

    env.replies.extend([propose_unavailable_publication, read_after_rejection, finish_action])
    final = await submit_and_wait(env)
    assert final.state == TaskState.FAILED
    if change == "unconfigured":
        assert len(env.calls) == 3, final.error
        assert not env.replies
        assert "required effect failed" in final.error
        assert [evidence.kind for evidence in final.evidence] == ["read"]
        assert final.evidence[0].sha256 == hashlib.sha256(env.content.encode()).hexdigest()
    else:
        # Counted admission pins the service identity and advertisement. A
        # mid-request change now stops this task before admitting its action.
        assert len(env.calls) == len(env.counts) == 1
        assert len(env.replies) == 2
        assert final.error == "provider error: Agent tool metadata disclosure changed"
        assert final.evidence == []
    native_publish.assert_not_awaited()
    if store is not None:
        assert (
            store.page(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
            )["items"]
            == []
        )
    entries = env.journal.read(session_id=env.session.session_id)
    assert not any(entry.kind == "artifact.updated" for entry in entries)
    rejected = [
        entry
        for entry in entries
        if entry.kind == "agent.tool" and entry.payload.get("tool") == "publish_ui"
    ]
    assert all(
        entry.payload["ok"] is False and "evidence" not in entry.payload for entry in rejected
    )
    if change == "unconfigured":
        assert len(rejected) == 1
        assert rejected[0].origin == "agent-supervisor"
        assert rejected[0].payload["reason"] == "not_advertised"
        assert rejected[0].payload["effect_executed"] is False
    else:
        assert rejected == []  # No model action reached native tool admission.
    assert not any(evidence.kind == "artifact" for evidence in final.evidence)
    outcomes = [entry for entry in entries if entry.kind == "agent.outcome"]
    assert len(outcomes) == 1 and outcomes[0].payload["state"] == "failed"
    assert current_agent_tools() is None
