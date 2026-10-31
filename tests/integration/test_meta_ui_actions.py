"""Meta's real strict adapter and native UI runtime, synthetic HTTP only."""

import json
from types import SimpleNamespace

import httpx
import jsonschema
import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.models import SessionRequest, TaskRequest, TaskState
from packages.providers.text import MetaResponsesProvider, ProviderError
from services.agent import AgentRuntime
from services.agent.artifacts import ArtifactStore
from services.session.store import LocalJournal, Store


def spec():
    return {
        "title": "Synthetic UI 🌱",
        "summary": "Explicitly simulated local fixture.",
        "components": [{"id": "summary", "type": "text", "data": {"text": "Synthetic value: 42"}}],
    }


def response(action_text):
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": action_text}],
                }
            ],
        },
    )


def wire_action(body, tool, **arguments):
    schema = body["text"]["format"]["schema"]
    fields = schema["properties"]["arguments"]["properties"]
    result = {"tool": tool, "arguments": {**dict.fromkeys(fields), **arguments}}
    jsonschema.Draft202012Validator(schema).validate(result)
    return json.dumps(result)


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    journal = Store(tmp_path / "journal.sqlite")
    session = journal.create_session(SessionRequest(channel="converse"))
    artifacts = ArtifactStore(tmp_path / "artifacts.sqlite3")
    state = SimpleNamespace(calls=[], replies=[])

    def respond(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        state.calls.append(body)
        assert state.replies, "Unexpected model request"
        reply = state.replies.pop(0)
        return reply(body) if callable(reply) else response(reply)

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-fixture-key",
        allow_cloud=True,
        structured_actions=True,
        reasoning_effort="minimal",
        transport=httpx.MockTransport(respond),
    )
    runtime = AgentRuntime(
        LocalJournal(journal, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace),
        artifact_store=artifacts,
    )
    state.provider, state.runtime, state.artifacts = provider, runtime, artifacts
    state.journal, state.session, state.workspace = journal, session, workspace
    yield state
    await runtime.close()
    await provider.close()
    journal.close()


def request(env, *, content_scope="public_synthetic"):
    return TaskRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        objective="Publish the public synthetic UI fixture",
        content_scope=content_scope,
        idempotency_key="synthetic-meta-ui",
    )


@pytest.mark.parametrize("update", [False, True])
async def test_actual_meta_wire_publishes_finishes_and_round_trips_logical_history(env, update):
    published = spec()
    expected_specs = [published]
    env.replies.append(
        lambda body: response(
            wire_action(body, "publish_ui", ui_spec_json=json.dumps(published, ensure_ascii=False))
        )
    )
    if update:

        def replace(body):
            envelope = env.artifacts.page(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
            )["items"][0]
            patch = {
                "artifact_id": envelope["artifact_id"],
                "expected_revision": 1,
                "updates": [{"id": "summary", "data": {"text": "Verified synthetic value: 43"}}],
            }
            expected_specs.append(patch)
            return response(wire_action(body, "publish_ui", ui_spec_json=json.dumps(patch)))

        env.replies.append(replace)
    env.replies.append(
        lambda body: response(
            wire_action(body, "finish", summary="Published the synthetic UI", objective_revision=1)
        )
    )
    task = await env.runtime.submit(request(env))
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert [item.kind for item in final.evidence] == ["artifact"] * (2 if update else 1)
    assert env.artifacts.verify(final.evidence[-1], final)
    assert not list(env.workspace.iterdir())
    assert len(env.calls) == (3 if update else 2)
    schema = env.calls[0]["text"]["format"]["schema"]
    fields = schema["properties"]["arguments"]["properties"]
    assert fields["ui_spec_json"]["type"] == ["string", "null"]
    assert fields["ui_spec_json"]["maxLength"] == 48 * 1024
    assert schema["properties"]["arguments"]["additionalProperties"] is False
    assert set(schema["properties"]["arguments"]["required"]) == set(fields)
    assert "publish_ui" in schema["properties"]["tool"]["enum"]
    assert "arguments.ui_spec_json" in env.calls[0]["input"][0]["content"]
    prior = [
        json.loads(message["content"])
        for message in env.calls[-1]["input"]
        if message["role"] == "assistant"
    ]
    assert [set(item["arguments"]) for item in prior] == [{"ui_spec_json"}] * len(expected_specs)
    assert [json.loads(item["arguments"]["ui_spec_json"]) for item in prior] == expected_specs


INVALID_SPECS = [
    "",
    "[]",
    "null",
    "true",
    "3",
    '"text"',
    "{bad",
    '{"title":"one","title":"two","summary":"Synthetic","components":[]}',
    '{"title":"T","summary":"S","components":[{"id":"a","type":"text","data":{"text":"a","text":"b"}}]}',
    '{"title":"T","summary":"S","components":[],"data":NaN}',
    '{"title":"T","summary":"S","components":[1e999]}',
    json.dumps({**spec(), "title": "\ud800"}),
    json.dumps({**spec(), "session_id": "model-metadata-is-forbidden"}),
    json.dumps({**spec(), "artifact_id": "mixed-shapes"}),
    json.dumps(
        {"artifact_id": "native-id", "expected_revision": 1, "updates": [], "title": "extra"}
    ),
    json.dumps({**spec(), "summary": "🌱" * 13000}, ensure_ascii=False),
    json.dumps(spec()) + " " * (48 * 1024),
    None,
    {},
]


@pytest.mark.parametrize("encoded", INVALID_SPECS)
async def test_meta_ui_decoder_rejects_invalid_object_text_without_native_publication(env, encoded):
    env.replies.append(json.dumps({"tool": "publish_ui", "arguments": {"ui_spec_json": encoded}}))
    with pytest.raises(ProviderError, match="UI publication"):
        await env.provider.complete([{"role": "user", "content": "Public synthetic UI fixture"}])
    assert len(env.calls) == 1
    assert (
        env.artifacts.page(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
        )["items"]
        == []
    )


@pytest.mark.parametrize(
    "extra", [{"path": "unrelated.txt"}, {"undeclared": None}, {"summary": "extra"}]
)
async def test_meta_ui_wrapper_rejects_unrelated_or_undeclared_fields(env, extra):
    env.replies.append(
        json.dumps(
            {
                "tool": "publish_ui",
                "arguments": {
                    "ui_spec_json": json.dumps(spec()),
                    **extra,
                },
            }
        )
    )
    with pytest.raises(ProviderError, match="UI publication"):
        await env.provider.complete([])


async def test_meta_ui_rejects_duplicate_wire_fields_and_native_top_level_metadata(env):
    encoded = json.dumps(json.dumps(spec()))
    env.replies.extend(
        [
            '{"tool":"publish_ui","arguments":{"ui_spec_json":'
            + encoded
            + ',"ui_spec_json":'
            + encoded
            + "}}",
            json.dumps(
                {
                    "tool": "publish_ui",
                    "arguments": {"ui_spec_json": json.dumps(spec())},
                    "schema_version": "1.0",
                }
            ),
        ]
    )
    for _ in range(2):
        with pytest.raises(ProviderError, match="UI publication"):
            await env.provider.complete([])


async def test_meta_ui_spec_is_not_a_wire_argument_for_other_tools(env):
    env.replies.append(
        json.dumps(
            {
                "tool": "write_file",
                "arguments": {
                    "path": "unrelated.txt",
                    "content": "synthetic",
                    "ui_spec_json": json.dumps(spec()),
                },
            }
        )
    )
    with pytest.raises(ProviderError, match="only valid for publish_ui"):
        await env.provider.complete([])


async def test_meta_ui_json_bound_is_utf8_bytes_and_accepts_exact_limit(env):
    value = {
        **spec(),
        "components": [
            {"id": f"t{index}", "type": "text", "data": {"text": "a" * 7000}} for index in range(6)
        ]
        + [{"id": "last", "type": "text", "data": {"text": ""}}],
    }
    empty = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    value["components"][-1]["data"]["text"] = "a" * (48 * 1024 - len(empty.encode("utf-8")))
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) == 48 * 1024
    env.replies.append(json.dumps({"tool": "publish_ui", "arguments": {"ui_spec_json": encoded}}))
    decoded = json.loads(await env.provider.complete([]))
    assert decoded == {"tool": "publish_ui", "arguments": value}
    env.replies.append(
        json.dumps({"tool": "publish_ui", "arguments": {"ui_spec_json": encoded + " "}})
    )
    with pytest.raises(ProviderError, match="bounded UTF-8 JSON object"):
        await env.provider.complete([])


async def test_meta_ui_invalid_response_utf8_is_rejected(env):
    env.replies.append(lambda _: httpx.Response(200, content=b"\xff"))
    with pytest.raises(ProviderError, match="invalid response JSON"):
        await env.provider.complete([])


async def test_meta_ui_never_bypasses_native_cloud_disclosure(env):
    with pytest.raises(PolicyDenied, match="disclosure"):
        await env.runtime.submit(request(env, content_scope="local_only"))
    assert not env.calls
