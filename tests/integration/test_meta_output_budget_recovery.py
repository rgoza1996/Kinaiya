"""#484: actual prepared Meta/native admission, public synthetic HTTP only."""

import asyncio
import inspect
import json
from types import SimpleNamespace

import httpx
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
from packages.providers.meta_action_wire import encode_action
from packages.providers.prompt_budget import request_digest
from packages.providers.response_diagnostics import meta_failure_diagnostic
from packages.providers.text import (
    MetaResponseError,
    MetaResponsesProvider,
    SettledOutputBudgetExhaustion,
)
from services.agent import AgentRuntime
from services.resources.providers import ResourceBoundProvider
from services.resources.state import ResourceRecord
from services.session.store import LocalJournal, Store

MARKER = "SYNTHETIC_INCOMPLETE_OUTPUT_484"


def generated(tool="write_file", **arguments):
    if not arguments:
        arguments = {"path": "result.txt", "content": "Verified public result"}
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
                            "text": json.dumps(
                                encode_action({"tool": tool, "arguments": arguments})
                            ),
                        }
                    ],
                }
            ],
        },
    )


def finished(revision=1):
    return generated("finish", summary="Public synthetic result", objective_revision=revision)


def exhausted_bytes(body, **changes):
    return json.dumps(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 23, "output_tokens": body["max_output_tokens"]},
            # Even a syntactically valid partial tool proposal must never execute.
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                encode_action(
                                    {
                                        "tool": "write_file",
                                        "arguments": {
                                            "path": "partial-trap.txt",
                                            "content": MARKER,
                                        },
                                    }
                                )
                            ),
                        }
                    ],
                }
            ],
            **changes,
        }
    ).encode()


def exhausted(body):
    return httpx.Response(200, content=exhausted_bytes(body))


class HeldClose(httpx.AsyncByteStream):
    def __init__(self, raw, *, fail=False):
        self.raw, self.fail = raw, fail
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.closed, self.closes = False, 0

    async def __aiter__(self):
        yield self.raw

    async def aclose(self):
        self.closes += 1
        self.closing.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError(MARKER)
        self.closed = True


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    state = SimpleNamespace(
        calls=[],
        counts=[],
        admissions=[],
        replies=[],
        streams=[],
        ready=asyncio.Event(),
        later_count=23,
    )

    async def send(request):
        body = json.loads(request.content)
        if request.url.path.endswith("input_tokens"):
            state.counts.append(body)
            tokens = 23 if len(state.counts) == 1 else state.later_count
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": tokens}
            )
        assert request.url.path == "/v1/responses" and state.counts[-1] == body
        state.calls.append(body)
        assert state.replies, "No undeclared generation is permitted"
        reply = state.replies.pop(0)
        result = reply(body) if callable(reply) else reply
        return await result if inspect.isawaitable(result) else result

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="public-synthetic-key",
        allow_cloud=True,
        context_tokens=8192,
        max_tokens=128,
        structured_actions=True,
        action_format="typed-v2",
        reasoning_effort="xhigh",
        transport=httpx.MockTransport(send),
    )
    policy = ToolPolicy(workspace)
    policy.set_permissions(actor="owner", allow_write=True)
    journal = LocalJournal(store, "agent")
    original_admit = journal.agent_export_admission

    async def admit(request):
        state.admissions.append(request.model_dump(mode="json"))
        return await original_admit(request)

    journal.agent_export_admission = admit
    runtime = AgentRuntime(journal, provider, workspace, policy, max_steps=12)
    state.runtime, state.provider, state.store, state.session, state.workspace = (
        runtime,
        provider,
        store,
        session,
        workspace,
    )

    async def start(source=None):
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Write and verify a public synthetic result",
                content_scope="public_synthetic",
                source_event_ids=[] if source is None else [source.event_id],
                idempotency_key="output-484",
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
        store.close()


def entries(env):
    return env.store.read(session_id=env.session.session_id, limit=200)


def notices(env):
    return [
        entry
        for entry in entries(env)
        if entry.kind == "agent.state"
        and entry.payload.get("reason") == "settled_output_budget_exhausted"
    ]


def redacted(env):
    assert MARKER not in json.dumps([entry.model_dump(mode="json") for entry in entries(env)])
    assert MARKER not in json.dumps(env.runtime._tasks[env.task_id].history)
    assert not (env.workspace / "partial-trap.txt").exists()


def held(env, *, fail=False):
    def reply(body):
        stream = HeldClose(exhausted_bytes(body), fail=fail)
        env.streams.append(stream)
        env.ready.set()
        return httpx.Response(200, stream=stream)

    return reply


@pytest.mark.parametrize("wrapped", [False, True])
async def test_one_settled_generation_gets_fresh_count_admission_then_real_action(
    env, tmp_path, wrapped
):
    if wrapped:
        path = tmp_path / "resources.json"
        path.write_text(
            ResourceRecord(profile="configured_hybrid", phase="ready").model_dump_json()
        )
        path.chmod(0o600)
        env.runtime.provider = ResourceBoundProvider(env.provider, path, "agent")
    env.replies.extend([exhausted, generated(), finished()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert (env.workspace / "result.txt").read_text() == "Verified public result"
    assert len(env.calls) == len(env.counts) == 3 and len(env.admissions) == 6
    assert all(count == call for count, call in zip(env.counts, env.calls, strict=True))
    assert [row["purpose"] for row in env.admissions] == [
        "complete_count",
        "complete_generation",
    ] * 3
    assert len({row["request_nonce"] for row in env.admissions}) == 6
    assert env.calls[1] != env.calls[0]
    assert all(
        body["max_output_tokens"] == 128 and body["reasoning"]["effort"] == "xhigh"
        for body in env.calls
    )
    (notice,) = notices(env)
    diagnostic = notice.payload["provider_response_diagnostic"]
    assert diagnostic["request_sha256"] == request_digest(env.calls[0])
    assert diagnostic["task_id"] == task.task_id and diagnostic["objective_revision"] == 1
    assert diagnostic["usage"] == {"input_tokens": 23, "output_tokens": 128}
    assert notice.payload["effect_executed"] is False and notice.payload["recovery_attempt"] == 1
    assert "evidence" not in notice.payload and len(final.evidence) == 1
    assert env.runtime._tasks[task.task_id].output_budget_recovery_revision == 1
    redacted(env)


async def test_second_exhaustion_is_terminal_not_a_step_or_output_limit_increase(env):
    env.replies.extend([exhausted, exhausted, generated()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and "output token budget exhausted" in final.error
    assert len(env.calls) == len(env.counts) == 2 and len(env.replies) == 1
    assert len(notices(env)) == 1 and final.evidence == [] and env.runtime.max_steps == 12
    outcome = [entry for entry in entries(env) if entry.kind == "agent.outcome"][-1]
    assert outcome.payload["provider_response_diagnostic"]["request_sha256"] == request_digest(
        env.calls[1]
    )
    redacted(env)


async def test_original_close_must_settle_before_notice_or_next_count(env):
    env.replies.extend([held(env), generated(), finished()])
    task = await env.start()
    await asyncio.wait_for(env.ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    assert not stream.closed and len(env.calls) == len(env.counts) == 1 and notices(env) == []
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert stream.closed and stream.closes == 1 and len(notices(env)) == 1
    redacted(env)


async def test_failed_close_is_terminal_with_no_recovery(env):
    env.replies.extend([held(env, fail=True), generated()])
    task = await env.start()
    await asyncio.wait_for(env.ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and "cleanup is unconfirmed" in final.error
    assert env.runtime._tasks[task.task_id]._cleanup_uncertain is True
    assert stream.closes == 1 and not stream.closed and len(env.calls) == len(env.counts) == 1
    assert notices(env) == [] and len(env.replies) == 1
    redacted(env)


@pytest.mark.parametrize(
    "control", ["cancel", "steer", "reset", "source", "permissions", "resources"]
)
async def test_owner_source_and_resource_changes_during_close_cannot_recover_old_request(
    env, tmp_path, control
):
    source = env.store.append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.user",
            payload={
                "text": "Public synthetic request",
                "final": True,
                "revision": 1,
                "content_scope": "public_synthetic",
            },
            idempotency_key="source-484",
        ),
        "owner",
    )
    path = tmp_path / "resources.json"
    if control == "resources":
        path.write_text(
            ResourceRecord(profile="configured_hybrid", phase="ready").model_dump_json()
        )
        path.chmod(0o600)
        env.runtime.provider = ResourceBoundProvider(env.provider, path, "agent")
    env.replies.extend([held(env), generated(), finished(2)])
    task = await env.start(source)
    await asyncio.wait_for(env.ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    if control == "cancel":
        await env.runtime.interrupt(
            task.task_id,
            InterruptRequest(expected_objective_revision=1, idempotency_key="cancel-484"),
        )
    elif control == "steer":
        await env.runtime.steer(
            task.task_id,
            SteerRequest(
                instruction="Keep the public result compact",
                content_scope="public_synthetic",
                expected_objective_revision=1,
                idempotency_key="steer-484",
            ),
        )
    elif control == "reset":
        env.store.reset(env.session.session_id)
    elif control == "source":
        env.store.append(
            AppendRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=env.session.epoch,
                kind="conversation.revision",
                payload={
                    "target_event_id": source.event_id,
                    "text": "Private source now",
                    "revision": 2,
                    "content_scope": "local_only",
                },
                idempotency_key="source-revision-484",
            ),
            "owner",
        )
    elif control == "permissions":
        env.runtime.policy.set_permissions(actor="owner", allow_write=False)
    else:
        path.write_text(
            ResourceRecord(revision=1, profile="local_only", phase="ready").model_dump_json()
        )
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert notices(env) == [] and stream.closed and stream.closes == 1
    if control == "steer":
        assert final.state == TaskState.SUCCEEDED and final.objective_revision == 2
        assert len(env.calls) == 3
    else:
        assert final.state == (TaskState.CANCELLED if control == "cancel" else TaskState.FAILED)
        assert len(env.calls) == len(env.counts) == 1
    redacted(env)


@pytest.mark.parametrize(
    "damage", ["status", "reason", "usage", "error", "duplicate", "nonfinite", "http"]
)
async def test_unproven_response_metadata_stays_terminal(env, damage):
    def reply(body):
        changes = {
            "status": {"status": "failed"},
            "reason": {"incomplete_details": {"reason": "content_filter"}},
            "usage": {"usage": {"input_tokens": 23, "output_tokens": 127}},
            "error": {"error": {"code": "server_error"}},
        }
        raw = exhausted_bytes(body, **changes.get(damage, {}))
        if damage == "duplicate":
            raw = b'{"status":"incomplete",' + raw[1:]
        elif damage == "nonfinite":
            raw = b'{"ignored":1e999,' + raw[1:]
        return httpx.Response(503 if damage == "http" else 200, content=raw)

    env.replies.extend([reply, generated()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and len(env.calls) == len(env.counts) == 1
    assert notices(env) == [] and len(env.replies) == 1 and final.evidence == []
    redacted(env)


@pytest.mark.parametrize("damage", ["digest", "seal", "owner", "consumed", "subclass"])
async def test_mutated_or_foreign_proof_is_not_recovery_authority(env, monkeypatch, damage):
    original = env.provider._complete_request

    async def corrupt(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except SettledOutputBudgetExhaustion as error:
            if damage == "digest":
                error._diagnostic["usage"]["input_tokens"] += 1
            elif damage == "seal":
                error._proof = object()
            elif damage == "owner":
                error._attempt.owner = object()
            elif damage == "consumed":
                error._attempt.consumed = True
            else:

                class Foreign(SettledOutputBudgetExhaustion):
                    pass

                error.__class__ = Foreign
            raise

    monkeypatch.setattr(env.provider, "_complete_request", corrupt)
    env.replies.extend([exhausted, generated()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and len(env.calls) == len(env.counts) == 1
    assert notices(env) == [] and len(env.replies) == 1
    redacted(env)


async def test_same_task_old_settlement_cannot_replay_as_a_new_prepared_attempt(env, monkeypatch):
    original = env.provider._complete_request
    prior = None

    async def replay(*args, **kwargs):
        nonlocal prior
        if prior is not None:
            raise prior
        try:
            return await original(*args, **kwargs)
        except SettledOutputBudgetExhaustion as error:
            prior = error
            raise

    monkeypatch.setattr(env.provider, "_complete_request", replay)
    env.replies.extend([exhausted, generated()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and len(env.calls) == 1 and len(env.counts) == 2
    assert len(notices(env)) == 1 and len(env.replies) == 1
    redacted(env)


@pytest.mark.parametrize("kind", ["generic_error", "provider_subclass", "transport"])
async def test_unsealed_errors_and_foreign_provider_never_recover(env, kind):
    if kind == "provider_subclass":

        class Foreign(MetaResponsesProvider):
            pass

        env.provider.__class__ = Foreign
        reply = exhausted
    else:

        def reply(body):
            if kind == "transport":
                raise httpx.ReadTimeout(MARKER)
            raise MetaResponseError(
                meta_failure_diagnostic(json.loads(exhausted_bytes(body)), request_digest(body))
            )

    env.replies.extend([reply, generated()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and len(env.calls) == len(env.counts) == 1
    assert notices(env) == [] and len(env.replies) == 1
    redacted(env)


@pytest.mark.parametrize("fence", ["required_work", "publication", "failed_effect"])
async def test_recovery_cannot_clear_existing_finish_fences(env, fence):
    def fail_with_fence(body):
        record = env.runtime._tasks[env.task_id]
        if fence == "required_work":
            record.task_media_completion_check = lambda: False
        elif fence == "publication":
            record.ui_format_pending_revision = 1
        else:
            record.failed_effect = record.failed_work_effect = True
        return exhausted(body)

    env.replies.extend([generated(), fail_with_fence, finished()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and final.summary == ""
    assert len(env.calls) == 3 and len(notices(env)) == 1 and len(final.evidence) == 1
    assert "finish" in final.error
    redacted(env)


async def test_recovery_does_not_override_owner_step_limit(env):
    env.runtime.max_steps = 1
    env.replies.extend([exhausted, generated()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and final.error == "step budget exhausted after 1 steps"
    assert len(env.calls) == 1 and len(notices(env)) == 1 and len(env.replies) == 1
    redacted(env)


async def test_fresh_count_must_still_fit_without_context_or_output_enlargement(env):
    env.later_count = 8192
    env.replies.extend([exhausted, generated()])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and len(env.calls) == 1 and len(env.counts) >= 2
    assert len(notices(env)) == 1 and len(env.replies) == 1 and final.evidence == []
    assert env.provider.context_tokens == 8192 and env.provider.max_tokens == 128
    redacted(env)


async def test_expired_total_deadline_never_becomes_a_settled_recovery(env):
    env.provider._request_timeout_s = 1
    env.replies.extend([held(env), generated()])
    task = await env.start()
    await asyncio.wait_for(env.ready.wait(), 2)
    stream = env.streams[0]
    await asyncio.wait_for(stream.closing.wait(), 2)
    await asyncio.sleep(1.1)
    assert notices(env) == [] and len(env.calls) == len(env.counts) == 1
    stream.release.set()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.FAILED and "total request timeout" in final.error
    assert stream.closed and stream.closes == 1 and notices(env) == [] and len(env.replies) == 1
    redacted(env)


async def test_one_recovery_allowance_is_per_revision_under_authentic_native_steer(env):
    async def revised(body):
        await env.runtime.steer(
            env.task_id,
            SteerRequest(
                instruction="Keep the public result compact",
                content_scope="public_synthetic",
                expected_objective_revision=1,
                idempotency_key="revision-two-484",
            ),
        )
        return exhausted(body)

    env.replies.extend([exhausted, revised, exhausted, generated(), finished(2)])
    task = await env.start()
    final = await env.runtime.wait(task.task_id, timeout=5)
    assert final.state == TaskState.SUCCEEDED and final.objective_revision == 2, final.error
    assert len(env.calls) == len(env.counts) == 5
    assert [
        entry.payload["provider_response_diagnostic"]["objective_revision"]
        for entry in notices(env)
    ] == [1, 2]
    assert all(item.objective_revision == 2 for item in final.evidence)
    assert env.runtime.max_steps == 12
    redacted(env)


def test_public_diagnostic_alone_cannot_construct_a_native_settlement():
    diagnostic = meta_failure_diagnostic(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 1, "output_tokens": 128},
        },
        "a" * 64,
    )
    with pytest.raises(ValueError, match="Unproven Meta output-budget settlement"):
        SettledOutputBudgetExhaustion(diagnostic, object(), lambda: True)
