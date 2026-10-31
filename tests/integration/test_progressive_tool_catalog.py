"""Progressive discovery through native runtime/API and real Meta adapters.

All inference responses and broker payloads are synthetic. No paid provider,
installed skill execution, plugin evaluation or MCP transport is used.
"""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from core.policy import ToolPolicy
from harnesses.seed import build_messages
from packages.contracts.agent_tools import (
    LEGACY_NATIVE_TOOL_ORDER,
    NATIVE_TOOL_ORDER,
    agent_tool_scope,
    native_tool_snapshot,
)
from packages.contracts.models import SessionRequest, TaskRequest, TaskState
from packages.contracts.tool_catalog import CatalogError, encode_catalog
from packages.ecosystem.mcp_host import MCPConfig, MCPHost
from packages.providers.meta_action_wire import encode_action
from packages.providers.text import (
    ActionDecodeError,
    CompatibleTextProvider,
    MetaResponsesProvider,
    ProviderRefusal,
)
from services.agent import AgentRuntime, create_app
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "public.txt").write_text("Public synthetic value 42")
    store = Store(tmp_path / "journal.sqlite3")
    session = store.create_session(SessionRequest(channel="agent", harness_version="seed-v1"))
    state = SimpleNamespace(replies=[], requests=[], counts=[], mode="flat-v1")

    def send(request):
        body = json.loads(request.content)
        if request.url.path == "/v1/responses/input_tokens":
            assert body["stream"] is False
            state.counts.append(body)
            return httpx.Response(
                200,
                json={
                    "object": "response.input_tokens",
                    "input_tokens": state.count_input_tokens(body)
                    if hasattr(state, "count_input_tokens")
                    else 123,
                },
            )
        assert request.url.path == "/v1/responses"
        if state.counts:
            assert body == state.counts[-1]
        state.requests.append(body)
        if getattr(state, "on_send", None) is not None:
            state.on_send(body, len(state.requests))
        assert state.replies, "Unexpected inference request"
        tool, arguments = state.replies.pop(0)
        if state.mode == "typed-v2":
            wire = encode_action({"tool": tool, "arguments": arguments})
        else:
            fields = body["text"]["format"]["schema"]["properties"]["arguments"]["properties"]
            wire = {"tool": tool, "arguments": {**dict.fromkeys(fields), **arguments}}
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": json.dumps(wire)}],
                    }
                ],
            },
        )

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-fixture-key",
        allow_cloud=True,
        structured_actions=True,
        context_tokens=32768,  # Verified synthetic count fixture only.
        transport=httpx.MockTransport(send),
    )
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace))
    token = "public-synthetic-owner-token-0123456789"
    auth = Auth(
        {
            role: token if role == "owner" else "public-synthetic-" + role + "-0123456789"
            for role in ("owner", "decisions")
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, auth)),
        base_url="http://fixture",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        state.runtime, state.provider, state.store = runtime, provider, store
        state.client, state.session = client, session
        try:
            yield state
        finally:
            await runtime.close()
            await provider.close()
            store.close()


async def submit(env):
    task = await env.runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Inspect tools then read public.txt.",
            content_scope="public_synthetic",
            idempotency_key="public-catalog-fixture",
        )
    )
    return await env.runtime.wait(task.task_id, timeout=3)


async def test_api_pages_and_selected_native_permissions_are_read_only(env):
    before = env.store.read(session_id=env.session.session_id)
    names, after = [], 0
    while True:
        response = await env.client.get(
            "/v1/agents/tool-catalog", params={"kind": "native", "after": after}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["inspection_only"] and body["untrusted_metadata"]
        assert len(body["items"]) <= 4 and len(encode_catalog(body)) <= 16384
        names.extend(row["name"] for row in body["items"])
        if body["next_after"] is None:
            break
        assert body["next_after"] > after
        after = body["next_after"]
    assert len(names) == len(set(names)) == len(NATIVE_TOOL_ORDER)
    assert set(names) == set(NATIVE_TOOL_ORDER)
    selected = (
        await env.client.get(
            "/v1/agents/tool-catalog", params={"kind": "native", "name": "write_file"}
        )
    ).json()["item"]
    assert selected["availability"] == "configured"
    assert selected["invocation"] == {"tool": "write_file"}
    assert selected["permissions"]["approval"] == "per_action"
    assert selected["permissions"]["readiness"] == "not_checked"
    assert selected["input_schema"]["properties"]["content"]["type"] == "string"
    missing = (
        await env.client.get(
            "/v1/agents/tool-catalog", params={"kind": "native", "name": "publish_ui"}
        )
    ).json()["item"]
    assert missing["availability"] == "not_configured" and "invocation" not in missing
    assert missing["input_schema"] and not env.runtime.tool_snapshot().supports("publish_ui")
    assert env.store.read(session_id=env.session.session_id) == before
    assert await env.runtime.status() == [] and env.requests == []
    assert env.runtime.policy._approvals == {}


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "owner"},
        {"name": "shell"},
        {"server_id": "test"},
        {"kind": "mcp", "name": "test"},
        {"kind": "native", "name": "shell", "after": "1"},
        {"after": "true"},
        {"after": "1.0"},
        {"after": "1000001"},
        {"content_scope": "public_synthetic"},
        {"invocation": "user"},
        {"name": "x" * 513},
        [("kind", "native"), ("kind", "mcp")],
    ],
)
async def test_api_invalid_queries_use_constant_errors(env, params):
    response = await env.client.get("/v1/agents/tool-catalog", params=params)
    assert response.status_code == 400
    assert response.json() == {"detail": "Tool catalog failed: invalid_query"}
    assert not env.requests


async def test_api_requires_actor_and_real_task(env):
    response = await env.client.get(
        "/v1/agents/tool-catalog", headers={"Authorization": "Bearer invalid"}
    )
    assert response.status_code in {401, 403}
    response = await env.client.get("/v1/agents/tool-catalog", params={"task_id": "unknown"})
    assert response.status_code == 404 and response.json() == {"detail": "Task not found"}


@pytest.mark.parametrize("mode", ["flat-v1", "typed-v2"])
async def test_catalog_schema_survives_model_history_and_terminal_api_without_effects(env, mode):
    env.mode = env.provider.action_format = mode
    env.replies.extend(
        [
            ("tool_catalog", {"kind": "native", "name": "publish_ui"}),
            ("read_file", {"path": "public.txt"}),
            (
                "finish",
                {"objective_revision": 1, "summary": "Inspected tools and read synthetic value 42"},
            ),
        ]
    )
    final = await submit(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    receipt = final.evidence[0]
    assert receipt.kind == "inspection" and receipt.path is None
    assert "inspection only" in receipt.summary
    assert json.loads(receipt.output) == {"inspection_only": True, "metadata_omitted": True}
    second = env.requests[1]["input"]
    (catalog_text,) = [
        message["content"] for message in second if message["content"].startswith('{"catalogs":')
    ]
    catalog = json.loads(catalog_text)
    assert catalog["history_kind"] == "tool_catalog_result"
    assert catalog["tool"] == "tool_catalog" and catalog["ok"] is True
    assert "Inspection complete" in catalog["next_action"]
    assert catalog["item"]["name"] == "publish_ui"
    assert "invocation" not in catalog["item"]
    assert len(catalog_text) > 2000 and len(catalog_text) <= 32768
    assert "truncated" not in catalog_text
    actions = [
        json.loads(message["content"]) for message in second if message["role"] == "assistant"
    ]
    assert any((action.get("action", action))["tool"] == "tool_catalog" for action in actions)
    before = final.model_dump(mode="json")
    response = await env.client.get(
        "/v1/agents/tool-catalog",
        params={
            "kind": "native",
            "name": "read_file",
            "task_id": final.task_id,
        },
    )
    assert response.status_code == 200
    assert response.json()["task_state"] == "succeeded"
    assert (await env.runtime.status(final.task_id)).model_dump(mode="json") == before
    assert len(env.requests) == 3 and len(list(env.runtime.workspace.iterdir())) == 1
    assert env.counts == env.requests


@pytest.mark.parametrize("failure", ["write_file", "publish_ui", "plugin_call"])
async def test_catalog_cannot_clear_failed_effect_or_plugin_taint(env, failure):
    # The unavailable-action fence is genuine runtime behavior. The following
    # inspection must not turn the required failed effect into successful work.
    if failure == "write_file":
        args = {"path": "../outside.txt", "content": "Synthetic"}
        env.runtime.policy.set_permissions(actor="owner", allow_write=True)
    elif failure == "publish_ui":
        args = {"ui_spec_json": "{}"}
    else:
        args = {"plugin_id": "missing", "plugin_arguments_json": "{}"}
    env.replies.extend(
        [
            ("read_file", {"path": "public.txt"}),
            (failure, args),
            ("tool_catalog", {"kind": "native", "name": "read_file"}),
            ("finish", {"objective_revision": 1, "summary": "Do not accept this claim"}),
        ]
    )
    final = await submit(env)
    assert final.state == TaskState.FAILED
    assert "failed" in final.error
    assert [evidence.kind for evidence in final.evidence] == ["read", "inspection"]


async def test_real_mcp_uncached_inventory_never_initializes_transport(env, monkeypatch):
    host = MCPHost(
        [
            MCPConfig(
                id="public",
                command="should-never-run",
                allowed_tools=["same"],
                content_scope="public_synthetic",
            )
        ],
        policy=env.runtime.policy,
    )
    monkeypatch.setattr(host, "_actor_for", lambda _: pytest.fail("Inspection started an actor"))
    monkeypatch.setattr(
        host, "discover", AsyncMock(side_effect=AssertionError("Discovery executed"))
    )
    env.runtime.mcp_host = host
    try:
        listed = await env.runtime.inspect_tools({"kind": "mcp"})
        (row,) = listed["items"]
        assert row["name"] == "same" and row["schema_state"] == "not_discovered"
        selected = await env.runtime.inspect_tools(
            {"kind": "mcp", "server_id": "public", "name": "same"}
        )
        assert selected["item"]["schema_state"] == "not_discovered"
        assert "input_schema" not in selected["item"]
        assert selected["item"]["provenance"]["server_version"] is None
        assert "endpoint" not in encode_catalog(selected) and "secret_refs" not in encode_catalog(
            selected
        )
        host.discover.assert_not_awaited()
        assert host._actors == {}
    finally:
        env.runtime.mcp_host = None
        await host.close()


async def test_broker_failure_is_closed_and_cancellation_propagates(env):
    env.runtime.skill_service = SimpleNamespace(
        tool_metadata=AsyncMock(side_effect=ValueError("SYNTHETIC_PRIVATE"))
    )
    body = await env.runtime.inspect_tools({"kind": "skill"})
    assert body["catalogs"] == [{"kind": "skill", "state": "metadata_unavailable"}]
    assert body["items"] == [] and "SYNTHETIC_PRIVATE" not in encode_catalog(body)
    env.runtime.skill_service.tool_metadata.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await env.runtime.inspect_tools({"kind": "skill"})
    env.runtime.skill_service = None


async def test_provider_change_during_metadata_await_discards_projection(env):
    async def changed(*args, **kwargs):
        env.runtime.provider.info = env.runtime.provider.info.model_copy(
            update={"model": "changed"}
        )
        return []

    original = copy.deepcopy(env.provider.info)
    env.runtime.skill_service = SimpleNamespace(tool_metadata=changed)
    try:
        with pytest.raises(CatalogError, match="invalid catalog metadata"):
            await env.runtime.inspect_tools({"kind": "skill"})
    finally:
        env.provider.info = original
        env.runtime.skill_service = None


@pytest.mark.parametrize("mode", ["flat-v1", "typed-v2"])
async def test_bare_provider_keeps_legacy_advertisement_and_catalog_is_opt_in(env, mode):
    env.provider.action_format = mode
    body = env.provider.body([{"role": "user", "content": "Synthetic"}], False)
    assert "tool_catalog" not in json.dumps(body["text"]["format"]["schema"])
    assert "tool_catalog" not in LEGACY_NATIVE_TOOL_ORDER
    with agent_tool_scope(native_tool_snapshot(catalog_available=True)):
        body = env.provider.body([{"role": "user", "content": "Synthetic"}], False)
    assert "tool_catalog" in json.dumps(body["text"]["format"]["schema"])


def test_catalog_history_is_whole_or_rejected_never_clipped():
    with pytest.raises(ValueError, match="catalog exceeds"):
        build_messages(
            "Synthetic",
            1,
            [],
            [{"role": "user", "content": "x" * 32769, "context_kind": "tool_catalog"}],
            "none",
        )


async def test_flat_catalog_refuses_non_catalog_non_null_fields(env):
    env.replies.append(("tool_catalog", {"kind": "native", "path": "SYNTHETIC_PRIVATE"}))
    with agent_tool_scope(native_tool_snapshot(catalog_available=True)):
        with pytest.raises(ActionDecodeError) as error:
            await env.provider.complete([{"role": "user", "content": "Synthetic"}])
    assert error.value.diagnostic == {"stage": "catalog_wire", "reason": "schema"}
    assert "SYNTHETIC_PRIVATE" not in str(error.value)


async def test_catalog_inspection_alone_cannot_complete_work(env):
    env.replies.extend(
        [
            ("tool_catalog", {"kind": "native", "name": "read_file"}),
            ("finish", {"objective_revision": 1, "summary": "This did not read public.txt"}),
        ]
    )
    final = await submit(env)
    assert final.state == TaskState.FAILED and "no current-revision evidence" in final.error
    assert [evidence.kind for evidence in final.evidence] == ["inspection"]


def skill_row(*, long=False):
    identity = "clawhub:" + "a" * 128 + "/" + "b" * 128 if long else "local/withdrawn_skill"
    return {
        "id": identity,
        "name": "Synthetic",
        "description": "😀" * 80,
        "version": "1.0",
        "digest": "a" * 64,
        "source_kind": "local",
        "source_revision": None,
        "active_version": {"version": "1.0", "digest": "a" * 64},
        "task_pinned_version": None,
        "integrity": "registered_only",
        "model_invocable": True,
        "user_invocable": True,
    }


def fake_skill(row):
    service = SimpleNamespace(public=True, validates=0)

    async def metadata(*args, **kwargs):
        return [copy.deepcopy(row)] if service.public else []

    def current(*args, **kwargs):
        return service.public

    async def validate(*args, **kwargs):
        service.validates += 1

    service.tool_metadata, service.tool_metadata_current = metadata, current
    service.validate_task, service.release_task = validate, AsyncMock()
    return service


async def test_max_existing_skill_identity_and_escaped_description_fit_inventory(env):
    service = fake_skill(skill_row(long=True))
    env.runtime.skill_service = service
    try:
        body = await env.runtime.inspect_tools({"kind": "skill"})
        (row,) = body["items"]
        assert len(row["name"]) == 265 and len(encode_catalog(row)) <= 2048
        assert row["description"] == "😀" * 80
        selected = await env.runtime.inspect_tools({"kind": "skill", "name": row["name"]})
        assert selected["item"]["input_schema"]["properties"]["skill_id"]["const"] == row["name"]
        assert selected["item"]["provenance"]["task_pinned_version"] is None
    finally:
        env.runtime.skill_service = None


async def test_later_broker_await_cannot_disclose_revoked_earlier_metadata(env):
    service = fake_skill(skill_row())
    env.runtime.skill_service = service

    async def plugins(*args, **kwargs):
        service.public = False
        return []

    env.runtime.plugin_service = SimpleNamespace(tool_metadata=plugins)
    try:
        with pytest.raises(CatalogError):
            await env.runtime.inspect_tools({})
    finally:
        env.runtime.skill_service = env.runtime.plugin_service = None


@pytest.mark.parametrize("boundary", ["epoch", "journal", "next_turn"])
async def test_withdrawn_unpinned_catalog_never_reaches_later_provider(env, monkeypatch, boundary):
    service = fake_skill(skill_row())
    env.runtime.skill_service = service
    checked = False
    native_epoch, native_append = env.runtime._epoch_error, env.runtime._append

    async def epoch(rec):
        nonlocal checked
        result = await native_epoch(rec)
        if (
            boundary == "epoch"
            and not checked
            and any(source.get("context_kind") == "tool_catalog" for source in rec.history) is False
        ):
            # The metadata method arms this flag only after returning its row.
            if getattr(service, "observed", False):
                service.public, checked = False, True
        return result

    original_metadata = service.tool_metadata

    async def metadata(*args, **kwargs):
        result = await original_metadata(*args, **kwargs)
        service.observed = True
        return result

    async def append(rec, kind, payload, **kwargs):
        result = await native_append(rec, kind, payload, **kwargs)
        if kind == "agent.tool" and payload.get("tool") == "tool_catalog":
            service.catalog_receipted = True
            if boundary == "journal":
                service.public = False
        return result

    async def validate(*args, **kwargs):
        service.validates += 1
        if boundary == "next_turn" and getattr(service, "catalog_receipted", False):
            service.public = False

    service.tool_metadata, service.validate_task = metadata, validate
    monkeypatch.setattr(env.runtime, "_epoch_error", epoch)
    monkeypatch.setattr(env.runtime, "_append", append)
    env.replies.extend(
        [
            ("tool_catalog", {"kind": "skill", "name": "local/withdrawn_skill"}),
            ("read_file", {"path": "public.txt"}),
            ("finish", {"objective_revision": 1, "summary": "Read synthetic public value 42"}),
        ]
    )
    try:
        final = await submit(env)
        if boundary == "next_turn":
            assert final.state == TaskState.FAILED and final.content_scope == "local_only"
            assert final.error in {
                "runtime error: Tool catalog disclosure changed; task stopped",
                "provider error: Agent tool metadata disclosure changed",
            }
            assert len(env.requests) == 1
        else:
            assert final.state == TaskState.SUCCEEDED, final.error
            assert len(env.requests) == 3
        for request in env.requests[1:]:
            assert "withdrawn_skill" not in json.dumps(request)
        for entry in env.store.read(session_id=env.session.session_id, kinds=["agent.tool"]):
            if entry.payload.get("tool") == "tool_catalog" and entry.payload.get("ok"):
                assert "withdrawn_skill" not in json.dumps(entry.payload)
    finally:
        env.runtime.skill_service = None


async def test_derived_metadata_cannot_survive_catalog_revocation(env):
    row = skill_row()
    marker = "SYNTHETIC_WITHDRAWN_CATALOG"
    row["description"] = marker
    service = fake_skill(row)
    env.runtime.skill_service = service

    def revoke(body, number):
        if number == 2:
            assert marker in json.dumps(body)
            service.public = False

    env.on_send = revoke
    env.replies.extend(
        [
            ("tool_catalog", {"kind": "skill", "name": row["id"]}),
            ("read_file", {"path": marker + ".txt"}),
            ("finish", {"objective_revision": 1, "summary": "Must never run"}),
        ]
    )
    try:
        final = await submit(env)
        assert final.state == TaskState.FAILED and final.content_scope == "local_only"
        assert final.error in {
            "runtime error: Tool catalog disclosure changed; task stopped",
            "provider error: Agent tool metadata disclosure changed",
        }
        assert len(env.requests) == 2
        assert [evidence.kind for evidence in final.evidence] == ["inspection"]
        rec = env.runtime._get(final.task_id)
        assert not any(marker + ".txt" in turn["content"] for turn in rec.history)
    finally:
        env.runtime.skill_service = None


async def test_catalog_guard_survives_prompt_history_window(env):
    row = skill_row()
    service = fake_skill(row)
    env.runtime.skill_service = service

    def revoke(body, number):
        if number == 6:
            assert not any(
                '"inspection_only":true' in message["content"] for message in body["input"]
            )
            service.public = False

    def counted_tokens(body):
        # Force real measured eviction immediately before generation six,
        # instead of relying on the removed eight-message history suffix.
        has_catalog = any(
            '"inspection_only":true' in message["content"] for message in body["input"]
        )
        if len(env.requests) == 5 and has_catalog:
            return env.provider.context_tokens - env.provider.max_tokens + 1
        return 123

    env.count_input_tokens = counted_tokens
    env.on_send = revoke
    env.replies.append(("tool_catalog", {"kind": "skill", "name": row["id"]}))
    env.replies.extend(("read_file", {"path": "public.txt"}) for _ in range(5))
    env.replies.append(("finish", {"objective_revision": 1, "summary": "Must never run"}))
    try:
        final = await submit(env)
        assert final.state == TaskState.FAILED and final.content_scope == "local_only"
        assert final.error in {
            "runtime error: Tool catalog disclosure changed; task stopped",
            "provider error: Agent tool metadata disclosure changed",
        }
        assert len(env.requests) == 6
        assert len(env.counts) == 7  # Six generations plus one rejected full-history count.
        assert any('"inspection_only":true' in m["content"] for m in env.counts[-2]["input"])
        assert not any('"inspection_only":true' in m["content"] for m in env.counts[-1]["input"])
        assert env.runtime._get(final.task_id).catalog_guards
    finally:
        env.runtime.skill_service = None


@pytest.mark.parametrize("provider_kind", ["meta", "compatible"])
async def test_provider_export_guard_rechecks_after_capture_await(monkeypatch, provider_kind):
    from packages.providers import decision_capture

    allowed, calls = True, []

    async def capture(*args, **kwargs):
        nonlocal allowed
        allowed = False

    def send(request):
        calls.append(request)
        pytest.fail("Withdrawn metadata was exported")

    monkeypatch.setattr(decision_capture, "capture_request", capture)
    cls = MetaResponsesProvider if provider_kind == "meta" else CompatibleTextProvider
    provider = cls(
        model="muse-spark-1.3-contributor",
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-key",
        allow_cloud=True,
        transport=httpx.MockTransport(send),
    )
    try:
        with agent_tool_scope(
            native_tool_snapshot(catalog_available=True), export_guard=lambda: allowed
        ):
            with pytest.raises(ProviderRefusal, match="metadata disclosure changed"):
                await provider.complete([{"role": "user", "content": "Synthetic catalog metadata"}])
        assert calls == []
    finally:
        await provider.close()
