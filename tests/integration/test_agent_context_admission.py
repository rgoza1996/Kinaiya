"""Issue179: actual native reads/history to the real adapter, mocked HTTP only."""

import json
from types import SimpleNamespace

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.models import SessionRequest, SteerRequest, TaskRequest, TaskState
from packages.providers.meta_action_wire import encode_action
from packages.providers.text import CompatibleTextProvider, MetaResponsesProvider
from services.agent import AgentRuntime
from services.session.store import LocalJournal, Store


def generated(body, action):
    if body["text"]["format"]["name"].endswith("v2"):
        action = encode_action(action)
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(action)}],
                }
            ],
        },
    )


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    state = SimpleNamespace(
        store=store,
        session=session,
        workspace=workspace,
        agents=[],
        providers=[],
        counts=[],
        calls=[],
    )

    async def start(actions, *, action_format="typed-v2", count=None, context_tokens=32768):
        remaining = list(actions)

        async def send(request):
            body = json.loads(request.content)
            assert body["stream"] is False
            assert body["max_output_tokens"] == 8192
            if request.url.path == "/v1/responses/input_tokens":
                state.counts.append(body)
                tokens = await count(body) if count is not None else 123
                return httpx.Response(
                    200, json={"object": "response.input_tokens", "input_tokens": tokens}
                )
            assert request.url.path == "/v1/responses"
            assert state.counts[-1] == body
            state.calls.append(body)
            assert remaining, "No undeclared generation allowed"
            action = remaining.pop(0)
            if callable(action):
                action = await action(body)
            return generated(body, action)

        provider = MetaResponsesProvider(
            base_url="https://synthetic.invalid/v1",
            api_key="public-synthetic-test-key",
            allow_cloud=True,
            structured_actions=True,
            action_format=action_format,
            context_tokens=context_tokens,
            max_tokens=8192,
            reasoning_effort="xhigh",
            transport=httpx.MockTransport(send),
        )
        agent = AgentRuntime(
            LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace), max_steps=12
        )
        state.providers.append(provider)
        state.agents.append(agent)
        state.agent, state.provider = agent, provider
        task = await agent.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Inspect the complete public synthetic fixtures",
                content_scope="public_synthetic",
                idempotency_key="synthetic-complete-context",
            )
        )
        state.task_id = task.task_id
        return await agent.wait(task.task_id, timeout=5)

    state.start = start
    yield state
    for agent in state.agents:
        await agent.close()
    for provider in state.providers:
        await provider.close()
    store.close()


def read(path):
    return {"tool": "read_file", "arguments": {"path": path}}


def finish(revision=1):
    return {"tool": "finish", "arguments": {"summary": "Inspected", "objective_revision": revision}}


@pytest.mark.parametrize("action_format", ["flat-v1", "typed-v2"])
async def test_native_files_reach_exact_measured_request_in_full_beyond_eight_messages(
    env, action_format
):
    contents = [
        "Public synthetic prefix\n" + "x" * 4000 + "\nCOMPLETE_ASCII_TAIL",
        json.dumps(
            {"head": "synthetic", "body": "🌱" * 2500, "tail": "COMPLETE_JSON_TAIL"},
            ensure_ascii=False,
        ),
        "日本語の公開テスト\n" * 800 + "COMPLETE_UNICODE_TAIL",
        "Fourth synthetic fixture",
        "Fifth synthetic fixture",
    ]
    for index, content in enumerate(contents):
        (env.workspace / f"fixture{index}.txt").write_text(content, encoding="utf-8")
    task = await env.start(
        [*[read(f"fixture{i}.txt") for i in range(5)], finish()], action_format=action_format
    )
    assert task.state == TaskState.SUCCEEDED, task.error
    assert len(env.calls) == len(env.counts) == 6
    final_input = env.calls[-1]["input"]
    assert len(final_input) == 12  # Protected base plus five whole action/result exchanges.
    for index, content in enumerate(contents):
        action, observed = final_input[2 + 2 * index : 4 + 2 * index]
        assert action["role"] == "assistant"
        logical = json.loads(action["content"])
        if action_format == "typed-v2":
            logical = logical["action"]
        assert logical["tool"] == "read_file"
        assert observed == {"role": "user", "content": "[ok] " + content}
    assert (
        json.loads(final_input[5]["content"].removeprefix("[ok] "))["tail"] == "COMPLETE_JSON_TAIL"
    )
    assert len(task.evidence) == 5
    assert all(e.sha256 and e.output == "" for e in task.evidence)


async def test_exact_count_evicts_oldest_whole_pairs_and_retains_native_evidence(env):
    for index in range(5):
        (env.workspace / f"fixture{index}.txt").write_text(f"Synthetic complete result {index}")

    async def count(body):
        # Synthetic tokenizer fixture: full request above six messages overflows.
        return 25000 if len(body["input"]) > 6 else 123

    task = await env.start([*[read(f"fixture{i}.txt") for i in range(5)], finish()], count=count)
    assert task.state == TaskState.SUCCEEDED, task.error
    assert len(env.counts) > len(env.calls) == 6
    for body in env.calls:
        assert 2 <= len(body["input"]) <= 6
        history = body["input"][2:]
        assert len(history) % 2 == 0
        assert [message["role"] for message in history] == ["assistant", "user"] * (
            len(history) // 2
        )
    last = env.calls[-1]["input"]
    assert last[3]["content"] == "[ok] Synthetic complete result 3"
    assert last[5]["content"] == "[ok] Synthetic complete result 4"
    assert len(env.agent._tasks[task.task_id].history) == 10
    assert len(task.evidence) == 5


@pytest.mark.parametrize("tokens", [None, 40000])
async def test_unconfigured_context_or_protected_overflow_never_generates(env, tokens):
    async def count(_body):
        return tokens

    task = await env.start(
        [read("unused")], count=count, context_tokens=None if tokens is None else 32768
    )
    assert task.state == TaskState.FAILED
    assert len(env.counts) == (0 if tokens is None else 1)
    assert env.calls == []
    assert task.evidence == []


@pytest.mark.parametrize("mutation", ["history", "grants", "provider", "session_reset"])
async def test_authority_change_during_token_count_blocks_generation(env, mutation):
    async def count(_body):
        if mutation == "history":
            env.agent._tasks[env.task_id].history.append(
                {"role": "user", "content": "New native context"}
            )
        elif mutation == "grants":
            env.agent.policy.set_permissions(actor="owner", allow_write=True)
        elif mutation == "provider":
            env.provider.info = env.provider.info.model_copy(
                update={"model": "changed-native-model"}
            )
        else:
            env.store.reset(env.session.session_id)
        return 123

    task = await env.start([read("unused")], count=count)
    assert task.state == TaskState.FAILED
    assert len(env.counts) == 1
    assert env.calls == []
    assert task.evidence == []


async def test_steering_during_count_discards_stale_request_then_counts_new_revision(env):
    (env.workspace / "current.txt").write_text("Public synthetic current revision")

    async def count(_body):
        if len(env.counts) == 1:
            await env.agent.steer(
                env.task_id,
                SteerRequest(
                    instruction="Read current.txt before finishing",
                    expected_objective_revision=1,
                    idempotency_key="synthetic-count-steer",
                    content_scope="public_synthetic",
                ),
            )
        return 123

    task = await env.start([read("current.txt"), finish(2)], count=count)
    assert task.state == TaskState.SUCCEEDED, task.error
    assert task.objective_revision == 2
    assert len(env.counts) == 3 and len(env.calls) == 2
    assert "Objective revision 1:" in env.counts[0]["input"][1]["content"]
    assert all("Objective revision 2:" in body["input"][1]["content"] for body in env.calls)
    assert all(e.objective_revision == 2 for e in task.evidence)


async def test_unsupported_complete_measurement_fails_before_any_http(env):
    async def send(_request):
        pytest.fail("Unsupported provider must not generate")

    provider = CompatibleTextProvider(
        "http://127.0.0.1:9876/v1", "synthetic", transport=httpx.MockTransport(send)
    )
    agent = AgentRuntime(
        LocalJournal(env.store, "agent"), provider, env.workspace, ToolPolicy(env.workspace)
    )
    env.agents.append(agent)
    env.providers.append(provider)
    task = await agent.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Synthetic unsupported adapter",
            content_scope="public_synthetic",
            idempotency_key="unsupported",
        )
    )
    final = await agent.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert "cannot measure complete requests" in final.error
    assert final.evidence == []
