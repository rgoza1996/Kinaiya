"""#473: settled pure-format refusals, real native runtime, synthetic HTTP only."""

import asyncio
import inspect
import json
from types import SimpleNamespace

import httpx
import jsonschema
import pytest

from core.policy import ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    InterruptRequest,
    SessionRequest,
    SteerRequest,
    TaskRequest,
    TaskState,
)
from packages.providers.completion_budget import PreparedComplete
from packages.providers.prompt_budget import request_digest
from packages.providers.text import (
    ActionDecodeError,
    MetaResponsesProvider,
    SettledPublicationRejection,
)
from services.agent import AgentRuntime
from services.agent.artifacts import ArtifactStore
from services.session.store import LocalJournal, Store

MARKER = "PRIVATE_REJECTED_MODEL_VALUE_473"
MALFORMED = '{"' + MARKER + '":'


def publication():
    return {
        "title": "Public synthetic publication",
        "summary": "Synthetic fixture only",
        "components": [{"id": "text", "type": "text", "data": {"text": "Synthetic 42"}}],
    }


def wire(body, tool, **arguments):
    schema = body["text"]["format"]["schema"]
    fields = schema["properties"]["arguments"]["properties"]
    value = {"tool": tool, "arguments": {**dict.fromkeys(fields), **arguments}}
    jsonschema.Draft202012Validator(schema).validate(value)
    return json.dumps(value)


def response_bytes(body, encoded=MALFORMED, *, status="completed"):
    value = {
        "output": [
            {
                "type": "message",
                "phase": "final_answer",
                "content": [
                    {"type": "output_text", "text": wire(body, "publish_ui", ui_spec_json=encoded)}
                ],
            }
        ],
    }
    if status is not None:
        value["status"] = status
    return json.dumps(value).encode()


def rejected(body):
    return httpx.Response(200, content=response_bytes(body))


def published(body):
    return httpx.Response(200, content=response_bytes(body, json.dumps(publication())))


def finished(revision=1):
    def reply(body):
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": wire(
                                    body,
                                    "finish",
                                    summary="Verified synthetic publication",
                                    objective_revision=revision,
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    return reply


class HeldResponse(httpx.AsyncByteStream):
    """Own the original EOF/close while native controls change independently."""

    def __init__(self, raw, *, fail_close=False):
        self.raw, self.fail_close = raw, fail_close
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.closed, self.close_count = False, 0

    async def __aiter__(self):
        yield self.raw

    async def aclose(self):
        self.close_count += 1
        self.closing.set()
        await self.release.wait()
        if self.fail_close:
            raise RuntimeError(MARKER)
        self.closed = True


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    artifacts = ArtifactStore(tmp_path / "artifacts.sqlite")
    state = SimpleNamespace(
        calls=[], counts=[], replies=[], streams=[], stream_ready=asyncio.Event()
    )

    async def send(request):
        body = json.loads(request.content)
        if request.url.path.endswith("input_tokens"):
            state.counts.append(body)
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": 123}
            )
        assert request.url.path == "/v1/responses"
        assert body == state.counts[-1]
        state.calls.append(body)
        assert state.replies, "No undeclared inference is permitted"
        reply = state.replies.pop(0)
        value = reply(body) if callable(reply) else reply
        return await value if inspect.isawaitable(value) else value

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-fixture-key",
        allow_cloud=True,
        structured_actions=True,
        reasoning_effort="minimal",
        context_tokens=32768,
        transport=httpx.MockTransport(send),
    )
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace),
        artifact_store=artifacts,
        max_steps=12,
    )
    state.store, state.session, state.workspace = store, session, workspace
    state.provider, state.runtime, state.artifacts = provider, runtime, artifacts

    async def start(*, source=None):
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Publish the public synthetic fixture",
                content_scope="public_synthetic",
                source_event_ids=[] if source is None else [source.event_id],
                idempotency_key="synthetic-publication-recovery-473",
            )
        )
        state.task_id = task.task_id
        return task

    state.start = start
    yield state
    for stream in state.streams:
        stream.release.set()
    try:
        await runtime.close()
    finally:
        await provider.close()
        artifacts.close()
        store.close()


def entries(env):
    return env.store.read(session_id=env.session.session_id, limit=200)


def notices(env):
    return [
        entry
        for entry in entries(env)
        if entry.kind == "agent.tool" and entry.payload.get("reason") == "invalid_proposal_format"
    ]


def assert_redacted(env):
    assert MARKER not in json.dumps([entry.model_dump(mode="json") for entry in entries(env)])
    assert MARKER not in json.dumps(env.runtime._tasks[env.task_id].history)


def held_reply(env, *, fail_close=False):
    def reply(body):
        stream = HeldResponse(response_bytes(body), fail_close=fail_close)
        env.streams.append(stream)
        env.stream_ready.set()
        return httpx.Response(200, stream=stream)

    return reply


@pytest.mark.parametrize("prior_publication", [False, True])
async def test_settled_json_syntax_has_one_native_notice_then_new_counted_publication(
    env, prior_publication
):
    if prior_publication:
        env.replies.append(published)
    env.replies.extend([rejected, published, finished()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert len(env.calls) == len(env.counts) == 3 + int(prior_publication)
    assert all(count == generation for count, generation in zip(env.counts, env.calls, strict=True))
    assert len(final.evidence) == 1 + int(prior_publication)
    assert all(env.artifacts.verify(evidence, final) for evidence in final.evidence)
    (notice,) = notices(env)
    payload = notice.payload
    assert notice.origin == "agent-supervisor"
    assert payload["effect_executed"] is False and payload["ok"] is False
    assert "evidence" not in payload
    assert payload["provider_diagnostic"] == {"stage": "ui_arguments", "reason": "json_syntax"}
    assert payload["request_sha256"] == request_digest(env.calls[int(prior_publication)])
    assert len(payload["generation_id"]) == 32
    assert payload["objective_revision"] == 1 and payload["task_id"] == task.task_id
    assert len(json.dumps(payload).encode()) <= 1024
    record = env.runtime._tasks[task.task_id]
    assert record.ui_format_recovery_revision == 1
    assert record.ui_format_pending_revision is None
    assert env.calls[-2] != env.calls[int(prior_publication)]
    assert_redacted(env)


async def test_no_notice_or_next_generation_until_original_http_close_settles(env):
    env.replies.extend([held_reply(env), published, finished()])
    task = await env.start()
    await asyncio.wait_for(env.stream_ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    assert not stream.closed and len(env.calls) == 1 and notices(env) == []
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert stream.closed and stream.close_count == 1
    assert len(env.calls) == 3 and len(notices(env)) == 1
    assert_redacted(env)


async def test_second_malformed_proposal_is_terminal_and_preserves_prior_artifact(env):
    env.replies.extend([published, rejected, rejected, published, finished()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert (
        final.error
        == "provider error: UI publication arguments require a bounded UTF-8 JSON object"
    )
    assert len(env.calls) == 3 and len(env.replies) == 2 and len(notices(env)) == 1
    assert len(final.evidence) == 1 and env.artifacts.verify(final.evidence[0], final)
    assert env.runtime._tasks[task.task_id].ui_format_pending_revision == 1
    assert_redacted(env)


@pytest.mark.parametrize("unrelated_read", [False, True])
async def test_finish_cannot_hide_rejected_publication_even_with_old_artifact_or_new_read(
    env, unrelated_read
):
    env.replies.extend([published, rejected])
    if unrelated_read:
        (env.workspace / "public.txt").write_text("Synthetic unrelated read")

        def read(body):
            value = json.loads(response_bytes(body))
            value["output"][0]["content"][0]["text"] = wire(body, "read_file", path="public.txt")
            return httpx.Response(200, json=value)

        env.replies.append(read)
    env.replies.append(finished())
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert final.error == "finish while a rejected UI publication remains unresolved"
    assert len(env.calls) == 3 + int(unrelated_read)
    assert len(final.evidence) == 1 + int(unrelated_read)
    assert env.artifacts.verify(final.evidence[0], final)
    assert env.runtime._tasks[task.task_id].ui_format_pending_revision == 1
    assert_redacted(env)


async def test_failed_native_publication_cannot_clear_pending_format_fence(env):
    invalid = {**publication(), "components": [{"id": "bad", "type": "unknown", "data": {}}]}
    env.replies.extend(
        [
            published,
            rejected,
            lambda body: httpx.Response(200, content=response_bytes(body, json.dumps(invalid))),
            finished(),
        ]
    )
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert final.error == "finish while a rejected UI publication remains unresolved"
    assert len(env.calls) == 4 and len(final.evidence) == 1
    assert env.artifacts.verify(final.evidence[0], final)
    assert env.runtime._tasks[task.task_id].ui_format_pending_revision == 1
    assert len(notices(env)) == 1
    assert_redacted(env)


async def test_recovery_does_not_increase_native_step_budget(env):
    env.runtime.max_steps = 1
    env.replies.extend([rejected, published])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert final.error == "step budget exhausted after 1 steps"
    assert len(env.calls) == 1 and len(env.replies) == 1 and len(notices(env)) == 1
    assert final.evidence == []
    assert_redacted(env)


@pytest.mark.parametrize("status", [None, "queued", "incomplete", "failed"])
async def test_not_completed_response_never_authorizes_recovery(env, status):
    env.replies.extend(
        [lambda body: httpx.Response(200, content=response_bytes(body, status=status)), finished()]
    )
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert len(env.calls) == 1 and len(env.replies) == 1
    assert notices(env) == [] and final.evidence == []
    assert_redacted(env)


@pytest.mark.parametrize("damage", ["request", "stage", "reason", "subclass"])
async def test_wrong_or_mutated_settled_proof_is_terminal(env, monkeypatch, damage):
    original = env.provider._complete_request

    async def corrupt(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except SettledPublicationRejection as error:
            if damage == "request":
                error._request_sha256 = "0" * 64
            elif damage == "stage":
                error._stage = MARKER
            elif damage == "reason":
                error._reason = MARKER
            else:

                class UnprovenSubclass(SettledPublicationRejection):
                    def validate_request(self, _):
                        pass

                raise UnprovenSubclass("0" * 64) from None
            raise

    monkeypatch.setattr(env.provider, "_complete_request", corrupt)
    env.replies.extend([rejected, published])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert len(env.calls) == 1 and len(env.replies) == 1
    assert notices(env) == [] and final.evidence == []
    assert_redacted(env)


async def test_unmeasured_fixture_cannot_supply_settled_proof(env, monkeypatch):
    async def generate():
        raise SettledPublicationRejection("0" * 64)

    async def prepare(_):
        return PreparedComplete(None, generate, fixture=True)

    monkeypatch.setattr(env.provider, "prepare_complete", prepare)
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert env.calls == [] and env.counts == [] and notices(env) == []


async def test_provider_subclass_cannot_supply_native_recovery_proof(env):
    class UnprovenProvider(MetaResponsesProvider):
        pass

    env.provider.__class__ = UnprovenProvider
    env.replies.extend([rejected, published])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert len(env.calls) == 1 and len(env.replies) == 1
    assert notices(env) == [] and final.evidence == []


async def test_generic_unproven_decode_error_stays_terminal(env):
    def fail(_):
        raise ActionDecodeError("ui_arguments", "json_syntax")

    env.replies.extend([fail, published])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert len(env.calls) == 1 and len(env.replies) == 1
    assert notices(env) == [] and final.evidence == []


async def test_uncertain_http_cleanup_dominates_format_rejection(env):
    env.replies.extend([held_reply(env, fail_close=True), published])
    task = await env.start()
    await asyncio.wait_for(env.stream_ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED
    assert final.error == "provider error: HTTP response cleanup is unconfirmed"
    assert env.runtime._tasks[task.task_id]._cleanup_uncertain is True
    assert stream.close_count == 1 and not stream.closed
    assert len(env.calls) == 1 and notices(env) == [] and final.evidence == []
    assert_redacted(env)


async def test_native_cancel_during_original_close_wins_without_recovery(env):
    env.replies.extend([held_reply(env), published])
    task = await env.start()
    await asyncio.wait_for(env.stream_ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    interrupted = await env.runtime.interrupt(
        task.task_id,
        InterruptRequest(
            expected_objective_revision=1,
            idempotency_key="native-cancel-at-close-473",
        ),
    )
    assert interrupted.state == TaskState.CANCEL_REQUESTED
    assert not stream.closed and len(env.calls) == 1
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.CANCELLED
    assert stream.closed and stream.close_count == 1
    assert len(env.calls) == 1 and notices(env) == [] and final.evidence == []
    assert_redacted(env)


async def test_native_steer_during_original_close_discards_old_rejection(env):
    env.replies.extend([held_reply(env), published, finished(2)])
    task = await env.start()
    await asyncio.wait_for(env.stream_ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    await env.runtime.steer(
        task.task_id,
        SteerRequest(
            instruction="Keep the synthetic publication compact",
            content_scope="public_synthetic",
            expected_objective_revision=1,
            idempotency_key="native-steer-at-close-473",
        ),
    )
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert final.objective_revision == 2 and notices(env) == []
    assert len(env.calls) == 3 and stream.closed and stream.close_count == 1
    assert all(evidence.objective_revision == 2 for evidence in final.evidence)
    assert_redacted(env)


@pytest.mark.parametrize(
    "mutation", ["source_scope", "source_revision", "permissions", "session_reset"]
)
async def test_native_source_and_authority_changes_at_close_prevent_recovery(env, mutation):
    source = env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.user",
            payload={
                "text": "Publish the public synthetic fixture",
                "final": True,
                "revision": 1,
                "content_scope": "public_synthetic",
            },
            idempotency_key="source-473",
        ),
        "owner",
    )
    env.replies.extend([held_reply(env), published])
    task = await env.start(source=source)
    await asyncio.wait_for(env.stream_ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    if mutation.startswith("source"):
        env.store.append(
            AppendRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                kind="conversation.revision",
                payload={
                    "target_event_id": source.event_id,
                    "text": "Updated public synthetic source",
                    "revision": 2,
                    "content_scope": "local_only"
                    if mutation == "source_scope"
                    else "public_synthetic",
                },
                idempotency_key="revise-source-473",
            ),
            "owner",
        )
    elif mutation == "permissions":
        env.runtime.policy.set_permissions(actor="owner", allow_write=True)
    else:
        env.store.reset(env.session.session_id)
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    if mutation == "session_reset":
        assert final.state == TaskState.PAUSED and "unconfirmed" in final.error
        rec = env.runtime._get(task.task_id)
        assert rec.completion_pending and rec.completion_snapshot.state == TaskState.FAILED
        assert rec.completion_snapshot.error
        assert env.store.read(task_id=task.task_id, kinds=["agent.outcome"]) == []
    else:
        assert final.state == TaskState.FAILED
    assert len(env.calls) == 1 and notices(env) == [] and final.evidence == []
    assert stream.closed and stream.close_count == 1
    assert_redacted(env)


async def test_recovery_limit_is_one_per_native_revision_not_a_step_limit_override(env):
    async def steer(body):
        await env.runtime.steer(
            env.task_id,
            SteerRequest(
                instruction="Publish the synthetic fixture under revision two",
                content_scope="public_synthetic",
                expected_objective_revision=1,
                idempotency_key="steer-after-notice-473",
            ),
        )
        return rejected(body)

    env.replies.extend([rejected, steer, rejected, published, finished(2)])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert final.objective_revision == 2 and len(env.calls) == len(env.counts) == 5
    assert [entry.payload["objective_revision"] for entry in notices(env)] == [1, 2]
    assert env.runtime.max_steps == 12
    assert env.runtime._tasks[task.task_id].ui_format_pending_revision is None
    assert all(evidence.objective_revision == 2 for evidence in final.evidence)
    assert_redacted(env)
