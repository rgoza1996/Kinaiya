"""Issue453: redacted native outcomes through actual prepared Meta HTTP requests."""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.models import SessionRequest, SteerRequest, TaskRequest, TaskState
from packages.providers.meta_action_wire import encode_action
from packages.providers.prompt_budget import request_digest
from packages.providers.response_diagnostics import MAX_DIAGNOSTIC_BYTES, meta_failure_diagnostic
from packages.providers.text import MetaResponseError, MetaResponsesProvider
from services.agent import AgentRuntime
from services.agent.artifacts import ArtifactStore
from services.session.store import LocalJournal, Store

MARKER = "SYNTHETIC_SECRET_AGENT453"


def generated(action):
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(encode_action(action))}],
                }
            ],
        },
    )


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    artifacts = ArtifactStore(tmp_path / "artifacts.sqlite3")
    state = SimpleNamespace(
        calls=[], counts=[], replies=[], store=store, session=session, workspace=workspace
    )

    async def send(request):
        body = json.loads(request.content)
        if request.url.path.endswith("input_tokens"):
            state.counts.append(body)
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 23})
        assert request.url.path == "/v1/responses"
        assert state.counts[-1] == body
        state.calls.append(body)
        assert state.replies, "No undeclared retry or generation"
        reply = state.replies.pop(0)
        return await reply(body) if callable(reply) else reply

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-key",
        allow_cloud=True,
        context_tokens=32768,
        structured_actions=True,
        action_format="typed-v2",
        transport=httpx.MockTransport(send),
    )
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace),
        artifact_store=artifacts,
        max_steps=4,
    )
    state.provider, state.runtime, state.artifacts = provider, runtime, artifacts

    async def submit():
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Inspect the public synthetic fixture453",
                content_scope="public_synthetic",
                idempotency_key="synthetic-meta-response453",
            )
        )
        state.task_id = task.task_id
        return task

    state.submit = submit
    yield state
    await runtime.close()
    await provider.close()
    store.close()


def entries(env):
    return env.store.read(session_id=env.session.session_id, limit=200)


def outcomes(env):
    return [entry for entry in entries(env) if entry.kind == "agent.outcome"]


@pytest.mark.parametrize("status,reason", [("failed", None), ("incomplete", "max_output_tokens")])
async def test_native_terminal_diagnostic_is_task_generation_and_exact_request_bound(
    env, status, reason
):
    response = {
        "status": status,
        "error": {"code": "server_error", "message": MARKER},
        "usage": {"input_tokens": 23, "output_tokens": 7, "total_tokens": 30, "debug": MARKER},
        "id": MARKER,
    }
    if reason:
        response["incomplete_details"] = {"reason": reason, "private": MARKER}
    env.replies.append(httpx.Response(200, json=response))
    env.replies.append(
        generated({"tool": "finish", "arguments": {"summary": "trap", "objective_revision": 1}})
    )
    task = await env.submit()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert len(env.calls) == len(env.counts) == 1
    assert len(env.replies) == 1, "No retry, output repair, or queued finish"
    outcome = outcomes(env)
    assert len(outcome) == 1 and outcome[0].origin == "agent-supervisor"
    diagnostic = outcome[0].payload["provider_response_diagnostic"]
    assert diagnostic["task_id"] == final.task_id
    assert diagnostic["objective_revision"] == final.objective_revision == 1
    assert UUID(diagnostic["generation_id"]).hex == diagnostic["generation_id"]
    observed = list(env.runtime.context_observations._rows)
    assert observed[-1].operation_id == diagnostic["generation_id"]
    assert diagnostic["request_sha256"] == request_digest(env.calls[0])
    assert diagnostic["request_sha256"] == observed[-1].measurement.request_sha256
    assert diagnostic["status"] == status
    assert diagnostic["reason"] == (reason if reason else "not_applicable")
    assert diagnostic["error_code"] == "server_error"
    assert diagnostic["usage"] == {"input_tokens": 23, "output_tokens": 7, "total_tokens": 30}
    assert len(json.dumps(diagnostic).encode()) <= MAX_DIAGNOSTIC_BYTES
    assert "provider_diagnostic" not in outcome[0].payload
    assert MARKER not in json.dumps([entry.model_dump(mode="json") for entry in entries(env)])
    assert final.evidence == [] and not list(env.workspace.iterdir())


async def test_prior_verified_artifact_survives_without_upgrading_failure_to_success(env):
    env.replies.extend(
        [
            generated(
                {
                    "tool": "publish_ui",
                    "arguments": {
                        "title": "Synthetic fixture453",
                        "summary": "Public synthetic",
                        "components": [{"id": "result", "type": "text", "data": {"text": "42"}}],
                    },
                }
            ),
            httpx.Response(200, json={"status": "failed", "error": {"message": MARKER}}),
            generated(
                {"tool": "finish", "arguments": {"summary": "trap", "objective_revision": 1}}
            ),
        ]
    )
    task = await env.submit()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and final.summary == ""
    assert final.error == "provider error: Meta response failed"
    assert len(final.evidence) == 1 and env.artifacts.verify(final.evidence[0], final)
    assert len(env.calls) == 2 and len(env.replies) == 1
    diagnostic = outcomes(env)[0].payload["provider_response_diagnostic"]
    assert diagnostic["request_sha256"] == request_digest(env.calls[-1])
    assert diagnostic["request_sha256"] != request_digest(env.calls[0])


@pytest.mark.parametrize("mutation", ["diagnostic", "request"])
async def test_invalid_optional_diagnostic_cannot_block_terminal_native_failure(env, mutation):
    async def invalid(body):
        error = MetaResponseError(
            meta_failure_diagnostic({"status": "failed"}, request_digest(body))
        )
        if mutation == "diagnostic":
            error._diagnostic["status"] = MARKER
        else:
            error._diagnostic["request_sha256"] = "0" * 64
        raise error

    env.replies.append(invalid)
    task = await env.submit()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert len(env.calls) == 1
    assert len(outcomes(env)) == 1
    assert "provider_response_diagnostic" not in outcomes(env)[0].payload
    assert MARKER not in json.dumps([entry.model_dump(mode="json") for entry in entries(env)])


class Body(httpx.AsyncByteStream):
    def __init__(self, *, held=False, fail=False):
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.closed, self.fail, self.confirmed = 0, fail, False
        if not held:
            self.release.set()

    async def __aiter__(self):
        yield b'{"status":"failed","error":{"code":"server_error"}}'

    async def aclose(self):
        self.closed += 1
        self.closing.set()
        await self.release.wait()
        if self.fail:
            raise OSError(MARKER)
        self.confirmed = True


async def test_original_cleanup_failure_dominates_status_diagnostics(env):
    body = Body(fail=True)
    env.replies.append(httpx.Response(200, stream=body))
    task = await env.submit()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert "cleanup" in final.error.lower()
    assert len(env.calls) == body.closed == 1
    assert not body.confirmed
    assert env.runtime._tasks[task.task_id]._cleanup_uncertain
    assert "provider_response_diagnostic" not in outcomes(env)[0].payload
    assert MARKER not in final.error


async def test_cancellation_retains_original_close_and_never_becomes_status_failure(env):
    body = Body(held=True)
    env.replies.append(httpx.Response(200, stream=body))
    task = await env.submit()
    try:
        async with asyncio.timeout(3):
            await body.closing.wait()
            worker = env.runtime._tasks[task.task_id].worker
            worker.cancel()
            await asyncio.sleep(0)
            worker.cancel()
            await asyncio.sleep(0)
            assert not worker.done() and not body.confirmed
            body.release.set()
            final = await env.runtime.wait(task.task_id, timeout=2)
        assert final.state == TaskState.CANCELLED
        assert body.confirmed and body.closed == len(env.calls) == 1
        assert all("provider_response_diagnostic" not in entry.payload for entry in outcomes(env))
    finally:
        body.release.set()


@pytest.mark.parametrize("mutation", ["disclosure", "session_reset"])
async def test_changed_authority_cannot_retain_stale_response_diagnostic(env, mutation):
    async def changed(_body):
        rec = env.runtime._tasks[env.task_id]
        if mutation == "disclosure":
            rec.task.content_scope = "local_only"
        else:
            env.store.reset(env.session.session_id)
        return httpx.Response(200, json={"status": "failed"})

    env.replies.append(changed)
    task = await env.submit()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert len(env.calls) == 1
    assert all("provider_response_diagnostic" not in entry.payload for entry in outcomes(env))


async def test_authenticated_steer_discards_old_status_before_new_revision_generation(env):
    (env.workspace / "current.txt").write_text("Public synthetic453")

    async def steer(_body):
        await env.runtime.steer(
            env.task_id,
            SteerRequest(
                instruction="Read current.txt before finishing",
                expected_objective_revision=1,
                idempotency_key="synthetic-response-steer453",
                content_scope="public_synthetic",
            ),
        )
        return httpx.Response(200, json={"status": "failed"})

    env.replies.extend(
        [
            steer,
            generated({"tool": "read_file", "arguments": {"path": "current.txt"}}),
            generated(
                {
                    "tool": "finish",
                    "arguments": {"summary": "Inspected synthetic453", "objective_revision": 2},
                }
            ),
        ]
    )
    task = await env.submit()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert final.objective_revision == 2 and len(env.calls) == 3
    assert len(outcomes(env)) == 1 and outcomes(env)[0].payload["state"] == "succeeded"
    assert "provider_response_diagnostic" not in outcomes(env)[0].payload
