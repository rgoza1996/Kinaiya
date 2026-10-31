"""Typed Meta requests through actual adapter/journal/store; public mocked HTTP."""

import asyncio
import itertools
import json
from types import SimpleNamespace

import httpx
import pytest
from jsonschema import Draft202012Validator

from core.policy import ToolPolicy
from packages.contracts.agent_tools import agent_tool_scope, native_tool_snapshot
from packages.contracts.models import SessionRequest, TaskRequest, TaskState
from packages.providers.meta_action_wire import encode_action
from packages.providers.prompt_budget import PromptCountUnavailable
from packages.providers.text import MetaResponsesProvider, ProviderError
from services.agent import AgentRuntime
from services.agent.artifacts import ArtifactStore
from services.session.store import LocalJournal, Store

MODEL = "muse-spark-1.3-contributor"
BASE = native_tool_snapshot().available_tools
UI = native_tool_snapshot(artifacts_available=True).available_tools
ALL = native_tool_snapshot(
    selector_available=True,
    artifacts_available=True,
    mcp_available=True,
    skills_available=True,
    plugins_available=True,
).available_tools


def response(value):
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": value if isinstance(value, str) else json.dumps(value),
                        }
                    ],
                }
            ],
        },
    )


def provider(transport, **kwargs):
    return MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-fixture-key",
        model=MODEL,
        allow_cloud=True,
        structured_actions=True,
        action_format="typed-v2",
        transport=httpx.MockTransport(transport),
        **kwargs,
    )


def finish():
    return {
        "action": {
            "tool": "finish",
            "arguments": {"summary": "Synthetic UI", "objective_revision": 1},
        }
    }


def publication():
    data = {
        "text": {"text": "Public synthetic explanation"},
        "status": {"text": "Simulated", "tone": None},
        "table": {
            "columns": ["N", "B", "I", "F", "S"],
            "rows": [[None, False, 0, 0.0, ""]],
            "selectable": False,
        },
        "chart": {"labels": ["Fictional"], "values": [0], "unit": "", "style": None},
        "diagram": {"nodes": [{"id": "n", "label": "", "detail": None}], "edges": []},
        "source": {
            "label": "Simulated",
            "url": "https://example.invalid/data",
            "freshness": "simulated",
            "observed_at": 0,
        },
        "number": {"label": "N", "value": 0, "min": 0, "max": 1.0, "step": None},
        "select": {"label": "Pick", "options": ["A", "B"], "value": "A"},
        "ask": {"label": "Ask", "message": "Explain this synthetic result"},
    }
    return {
        "action": {
            "tool": "publish_ui",
            "arguments": {
                "title": "Fictional results",
                "summary": "Simulated public fixture",
                "layout": None,
                "components": [
                    {"id": kind, "type": kind, "title": None, "data": value}
                    for kind, value in data.items()
                ],
            },
        }
    }


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    artifacts = ArtifactStore(tmp_path / "artifacts.sqlite")
    state = SimpleNamespace(
        replies=[], calls=[], workspace=workspace, store=store, session=session, artifacts=artifacts
    )

    def send(request):
        body = json.loads(request.content)
        state.calls.append(body)
        assert state.replies, "No undeclared inference allowed"
        item = state.replies.pop(0)
        return response(item(body) if callable(item) else item)

    state.provider = provider(send, max_tokens=8192, reasoning_effort="xhigh")
    state.agent = AgentRuntime(
        LocalJournal(store, "agent"),
        state.provider,
        workspace,
        ToolPolicy(workspace),
        artifact_store=artifacts,
    )
    yield state
    await state.agent.close()
    await state.provider.close()
    store.close()


def request(env):
    return TaskRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        objective="Public synthetic UI demonstration",
        content_scope="public_synthetic",
        idempotency_key="typed-ui-fixture",
    )


def page(env):
    return env.artifacts.page(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
    )["items"]


@pytest.mark.parametrize("update", [False, True])
async def test_typed_actual_adapter_publishes_all_nine_types_and_explicitly_finishes(env, update):
    def admit(body):
        value = publication()
        Draft202012Validator(body["text"]["format"]["schema"]).validate(value)
        return value

    env.replies.append(admit)
    if update:

        def patch(body):
            previous = page(env)[0]
            value = {
                "action": {
                    "tool": "publish_ui",
                    "arguments": {
                        "artifact_id": previous["artifact_id"],
                        "expected_revision": 1,
                        "updates": [
                            {"id": c["id"], "data": c["data"]}
                            for c in publication()["action"]["arguments"]["components"]
                        ],
                    },
                }
            }
            value["action"]["arguments"]["updates"][0]["data"]["text"] = (
                "Updated synthetic explanation"
            )
            Draft202012Validator(body["text"]["format"]["schema"]).validate(value)
            return value

        env.replies.append(patch)
    env.replies.append(finish())
    initial = await env.agent.submit(request(env))
    final = await env.agent.wait(initial.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert len(env.calls) == (3 if update else 2)
    assert len(final.evidence) == (2 if update else 1)
    assert env.artifacts.verify(final.evidence[-1], final)
    stored = page(env)[0]
    assert stored["revision"] == (2 if update else 1)
    kinds = {c["type"]: c["data"] for c in stored["components"]}
    row = kinds["table"]["rows"][0]
    assert row == [None, False, 0, 0.0, ""]
    assert [type(v) for v in row] == [type(None), bool, int, float, str]
    assert kinds["table"]["selectable"] is False
    assert kinds["source"]["observed_at"] == 0 and type(kinds["source"]["observed_at"]) is int
    assert kinds["chart"]["unit"] == "" and kinds["diagram"]["nodes"][0]["label"] == ""
    assert not list(env.workspace.iterdir())
    assert not any(m["role"] == "assistant" for m in env.calls[-1]["input"])
    receipts = [
        json.loads(m["content"])
        for m in env.calls[-1]["input"]
        if m["role"] == "user" and m["content"].startswith("{")
    ]
    assert [r["revision"] for r in receipts] == ([1, 2] if update else [1])
    assert receipts[-1]["content_sha256"] == final.evidence[-1].sha256
    assert all(r["payload_omitted"] and len(json.dumps(r).encode()) <= 4096 for r in receipts)


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize(
    "invalid",
    ["mixed", "missing_nullable", "string_ui", "trailing", "duplicate", "nonfinite", "unicode"],
)
async def test_bad_typed_proposal_stays_failed_without_consuming_queued_finish(
    env, previous, invalid
):
    if previous:
        env.replies.append(publication())
    value = publication()
    args = value["action"]["arguments"]
    reason = "schema"
    if invalid == "mixed":
        args["objective_revision"] = 1
    elif invalid == "missing_nullable":
        del args["layout"]
    elif invalid == "string_ui":
        value["action"]["arguments"] = json.dumps(args) + " public trailing prose"
    elif invalid == "trailing":
        value = json.dumps(value) + " public trailing prose"
        reason = "json_syntax"
    elif invalid == "duplicate":
        value = '{"action":{},"action":{}}'
        reason = "duplicate_key"
    elif invalid == "nonfinite":
        args["components"][6]["data"]["value"] = float("inf")
        reason = "non_finite"
    elif invalid == "unicode":
        args["title"] = "\ud800"
        reason = "invalid_unicode"
    env.replies.extend([value, finish()])
    initial = await env.agent.submit(request(env))
    final = await env.agent.wait(initial.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert len(env.calls) == 1 + previous and env.replies == [finish()]
    assert len(page(env)) == int(previous)
    if previous:
        assert env.artifacts.verify(final.evidence[-1], final)
    outcomes = env.store.read(session_id=env.session.session_id, kinds=["agent.outcome"])
    assert len(outcomes) == 1 and outcomes[0].payload["state"] == "failed"
    assert outcomes[0].payload["provider_diagnostic"] == {"stage": "typed_wire", "reason": reason}
    assert "public trailing prose" not in final.error


async def test_typed_logical_ui_size_guard_remains_terminal(env):
    value = publication()
    value["action"]["arguments"]["components"] = [
        {"id": f"text{i}", "type": "text", "title": None, "data": {"text": "x" * 8000}}
        for i in range(8)
    ]
    env.replies.extend([value, finish()])
    task = await env.agent.submit(request(env))
    final = await env.agent.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and len(env.calls) == 1
    assert not page(env)
    outcome = env.store.read(session_id=env.session.session_id, kinds=["agent.outcome"])[0]
    assert outcome.payload["provider_diagnostic"] == {
        "stage": "ui_arguments",
        "reason": "size_limit",
    }


async def test_typed_shape_does_not_replace_native_relationship_validation(env):
    value = publication()
    value["action"]["arguments"]["components"][4]["data"]["edges"] = [
        {"from": "n", "to": "absent", "label": None}
    ]
    env.replies.extend([value, finish()])
    task = await env.agent.submit(request(env))
    final = await env.agent.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and len(env.calls) == 2
    assert not page(env)
    assert (
        "provider_diagnostic"
        not in env.store.read(session_id=env.session.session_id, kinds=["agent.outcome"])[0].payload
    )


@pytest.mark.parametrize("flags", list(itertools.product((False, True), repeat=5)))
async def test_actual_typed_requests_share_native_selection_and_gate_hints(flags):
    snapshot = native_tool_snapshot(
        **dict(
            zip(
                (
                    "selector_available",
                    "mcp_available",
                    "skills_available",
                    "plugins_available",
                    "artifacts_available",
                ),
                flags,
                strict=True,
            )
        )
    )
    calls = []

    def send(req):
        body = json.loads(req.content)
        calls.append(body)
        Draft202012Validator(body["text"]["format"]["schema"]).validate(finish())
        return response(finish())

    p = provider(send)
    try:
        with agent_tool_scope(snapshot):
            result = await p.complete([{"role": "user", "content": "Public synthetic fixture"}])
        assert json.loads(result)["tool"] == "finish" and len(calls) == 1
        hints = calls[0]["input"][0]["content"]
        for tool in ("publish_ui", "mcp_call", "plugin_call"):
            assert (tool in hints) == snapshot.supports(tool)
        assert "synthetic-fixture-key" not in json.dumps(calls[0])
    finally:
        await p.close()


@pytest.mark.parametrize("tool", [name for name in ALL if name not in BASE])
async def test_unavailable_typed_argument_payload_never_reaches_gateway_decoder(monkeypatch, tool):
    import packages.providers.text as text

    def trap(_):
        raise AssertionError("Unavailable argument decoder invoked")

    monkeypatch.setattr(text, "_gateway_action_object", trap)
    p = provider(
        lambda _: response(
            {
                "action": {
                    "tool": tool,
                    "arguments": {"mcp_arguments_json": "not JSON", "poison": [False, None]},
                }
            }
        )
    )
    try:
        with agent_tool_scope(native_tool_snapshot()):
            result = await p.complete([{"role": "user", "content": "Public synthetic fixture"}])
        assert json.loads(result) == {"tool": tool, "arguments": {}}
    finally:
        await p.close()


@pytest.mark.parametrize("tool", ["mcp_call", "plugin_call"])
@pytest.mark.parametrize("extra", [False, True])
async def test_historical_gateway_arguments_are_never_repaired_by_dropping_keys(tool, extra):
    args = {
        "server_id": "demo",
        "tool": "calculate",
        "arguments": {"nullable": None, "enabled": False},
    }
    if tool == "plugin_call":
        args = {"plugin_id": "demo", "arguments": {"nullable": None, "enabled": False}}
    if extra:
        args["unexpected"] = "synthetic marker"
    original = json.dumps({"tool": tool, "arguments": args})
    p = provider(lambda _: response(finish()))
    try:
        body = p.body([{"role": "assistant", "content": original}], False)
        observed = body["input"][-1]["content"]
        if extra:
            assert observed == original
        else:
            prior = json.loads(observed)["action"]["arguments"]
            key = "mcp_arguments_json" if tool == "mcp_call" else "plugin_arguments_json"
            assert json.loads(prior[key]) == {"nullable": None, "enabled": False}
    finally:
        await p.close()


@pytest.mark.parametrize("tool", ["mcp_call", "plugin_call"])
async def test_existing_gateway_json_text_conversion_stays_native(tool):
    args = {"server_id": "demo", "mcp_tool": "calculate", "mcp_arguments_json": '{"value":NaN}'}
    if tool == "plugin_call":
        args = {"plugin_id": "demo", "plugin_arguments_json": '{"value":NaN}'}
    p = provider(lambda _: response(encode_action({"tool": tool, "arguments": args})))
    try:
        result = json.loads(
            await p.complete([{"role": "user", "content": "Public synthetic fixture"}])
        )
        assert (
            result["arguments"]["arguments"]["value"] != result["arguments"]["arguments"]["value"]
        )
    finally:
        await p.close()


async def test_format_and_schema_are_bound_to_exact_count_and_generation():
    calls = []

    def send(req):
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        if req.url.path.endswith("input_tokens"):
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 100}
            )
        assert body["stream"] is True
        events = [
            {"type": "response.output_text.delta", "delta": json.dumps(finish())},
            {"type": "response.completed"},
        ]
        return httpx.Response(
            200, content="".join("data: " + json.dumps(e) + "\n\n" for e in events)
        )

    p = provider(send, context_tokens=8192, max_tokens=128, reasoning_effort="minimal")
    messages = [{"role": "user", "content": "Public synthetic fixture"}]

    async def consume():
        return "".join([part async for part in p.stream(messages)])

    try:
        await p.measure_prompt(messages)
        p.action_format = "flat-v1"
        with pytest.raises(PromptCountUnavailable):
            await consume()
        assert len(calls) == 1
        p.action_format = "typed-v2"
        with agent_tool_scope(native_tool_snapshot()):
            await p.measure_prompt(messages)
        with agent_tool_scope(native_tool_snapshot(artifacts_available=True)):
            with pytest.raises(PromptCountUnavailable):
                await consume()
        assert len(calls) == 2
        await p.measure_prompt(messages)
        assert await consume() == json.dumps(finish())
        assert len(calls) == 4
        assert calls[-2][1] == calls[-1][1]
    finally:
        await p.close()


async def test_mutated_format_after_http_dispatch_cannot_reinterpret_received_wire():
    p = None

    def send(_):
        p.action_format = "flat-v1"
        return response(finish())

    p = provider(send)
    try:
        assert (
            json.loads(await p.complete([{"role": "user", "content": "Public synthetic fixture"}]))
            == finish()["action"]
        )
    finally:
        await p.close()


async def test_concurrent_typed_snapshots_do_not_share_schema():
    entered = asyncio.Event()
    bodies = {}

    async def send(req):
        body = json.loads(req.content)
        label = body["input"][-1]["content"]
        bodies[label] = body
        if len(bodies) == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), 1)
        return response(finish())

    p = provider(send)

    async def call(snapshot, label):
        with agent_tool_scope(snapshot):
            return await p.complete([{"role": "user", "content": label}])

    try:
        await asyncio.gather(
            call(native_tool_snapshot(), "base"),
            call(native_tool_snapshot(artifacts_available=True), "ui"),
        )
        assert "publish_ui" not in json.dumps(bodies["base"]["text"])
        assert "publish_ui" in json.dumps(bodies["ui"]["text"])
    finally:
        await p.close()


@pytest.mark.parametrize("action_format", [None, True, "typed-v3", []])
def test_invalid_wire_profile_never_opens_transport(action_format):
    with pytest.raises(ProviderError, match="Unknown Meta action wire format"):
        MetaResponsesProvider(api_key="synthetic", allow_cloud=True, action_format=action_format)
