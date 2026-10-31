"""Actual adapter requests use native per-request tools, without cloud calls."""

import asyncio
import itertools
import json

import httpx
import pytest

from packages.contracts.agent_tools import agent_tool_scope, native_tool_snapshot
from packages.providers.text import MetaResponsesProvider, ProviderError


def response():
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
                            "text": json.dumps(
                                {
                                    "tool": "finish",
                                    "arguments": {
                                        "summary": "Synthetic proposal",
                                        "objective_revision": 1,
                                    },
                                }
                            ),
                        }
                    ],
                }
            ],
        },
    )


@pytest.mark.parametrize("flags", list(itertools.product((False, True), repeat=5)))
async def test_actual_meta_wire_uses_only_selected_native_operations(flags):
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
    observed = []

    def send(request):
        observed.append(json.loads(request.content))
        return response()

    provider = MetaResponsesProvider(
        api_key="synthetic-key",
        allow_cloud=True,
        structured_actions=True,
        transport=httpx.MockTransport(send),
    )
    try:
        with agent_tool_scope(snapshot):
            await provider.complete([{"role": "user", "content": "Synthetic request"}])
        assert len(observed) == 1
        body = observed[0]
        schema = body["text"]["format"]["schema"]
        assert schema["properties"]["tool"]["enum"] == list(snapshot.available_tools)
        hints = body["input"][0]["content"]
        fields = schema["properties"]["arguments"]["properties"]
        assert set(fields) == set(schema["properties"]["arguments"]["required"])
        for tool, field in (
            ("select_context", "max_chars"),
            ("skill_load", "skill_id"),
            ("plugin_call", "plugin_arguments_json"),
            ("mcp_call", "mcp_arguments_json"),
            ("publish_ui", "ui_spec_json"),
        ):
            assert (field in fields) == snapshot.supports(tool)
        for tool in ("plugin_call", "mcp_call", "publish_ui"):
            assert (tool in hints) == snapshot.supports(tool)
        assert "finish" in hints and "verification evidence" in hints
        assert "synthetic-key" not in json.dumps(body)
    finally:
        await provider.close()


async def test_concurrent_calls_on_one_adapter_keep_distinct_tool_schemas():
    snapshots = [native_tool_snapshot(), native_tool_snapshot(artifacts_available=True)]
    seen = {}
    entered = asyncio.Event()

    async def send(request):
        body = json.loads(request.content)
        label = body["input"][-1]["content"]
        seen[label] = body
        if len(seen) == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), timeout=1)
        return response()

    provider = MetaResponsesProvider(
        api_key="synthetic-key",
        allow_cloud=True,
        structured_actions=True,
        transport=httpx.MockTransport(send),
    )

    async def call(index):
        with agent_tool_scope(snapshots[index]):
            await provider.complete([{"role": "user", "content": f"Synthetic {index}"}])

    try:
        await asyncio.wait_for(asyncio.gather(call(0), call(1)), timeout=2)
        for index, snapshot in enumerate(snapshots):
            assert seen[f"Synthetic {index}"]["text"]["format"]["schema"]["properties"]["tool"][
                "enum"
            ] == list(snapshot.available_tools)
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "tool",
    [
        "select_context",
        "mcp_list",
        "mcp_call",
        "skills_list",
        "skill_load",
        "plugins_list",
        "plugin_call",
        "publish_ui",
    ],
)
async def test_unadvertised_response_only_reaches_native_name_refusal(tool):
    proposed = {"tool": tool, "arguments": {"unavailable_payload": "synthetic-private-marker"}}
    reply = response()
    body = json.loads(reply.content)
    body["output"][0]["content"][0]["text"] = json.dumps(proposed)
    provider = MetaResponsesProvider(
        api_key="synthetic-key",
        allow_cloud=True,
        structured_actions=True,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    try:
        with agent_tool_scope(native_tool_snapshot()):
            rejected = await provider.complete([{"role": "user", "content": "Synthetic request"}])
        assert json.loads(rejected) == {"tool": tool, "arguments": {}}
        assert "synthetic-private-marker" not in rejected
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "raw",
    [
        '{"tool":"publish_ui","tool":"publish_ui","arguments":{}}',
        '{"tool":"publish_ui","arguments":{"value":NaN}}',
        '{"tool":"publish_ui","arguments":{"value":1e999}}',
        '{"tool":"publish_ui","arguments":{"value":"\\ud800"}}',
        '{"tool":"publish_ui","arguments":{},"success":true}',
        json.dumps(
            {"tool": "publish_ui", "arguments": {"value": "界" * 110000}}, ensure_ascii=False
        ),
        '{"tool":"publish_ui","arguments":{',
        '[{"tool":"publish_ui","arguments":{}}]',
    ],
)
async def test_unadvertised_rejection_envelope_stays_closed_and_bounded(raw):
    reply = response()
    body = json.loads(reply.content)
    body["output"][0]["content"][0]["text"] = raw
    provider = MetaResponsesProvider(
        api_key="synthetic-key",
        allow_cloud=True,
        structured_actions=True,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    try:
        with agent_tool_scope(native_tool_snapshot()):
            with pytest.raises(ProviderError):
                await provider.complete([{"role": "user", "content": "Synthetic request"}])
    finally:
        await provider.close()
