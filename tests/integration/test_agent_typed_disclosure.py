"""Native Agent/command disclosure gates; providers below are synthetic spies."""

import asyncio
import hashlib
import json

import httpx
import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.commands import Registry
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    CommandRequest,
    InterruptRequest,
    ProviderInfo,
    SessionRequest,
    SteerRequest,
    TaskRequest,
)
from packages.providers.completion_budget import PreparedComplete
from services.agent import AgentRuntime, create_app
from services.agent.skill_service import SkillService
from services.common.auth import Auth
from services.session.store import LocalJournal, Store


class Spy:
    def __init__(self, *, local=False, contributor=True, responses=None):
        self.info = ProviderInfo(
            provider="meta" if contributor and not local else "fixture",
            model="muse-spark-1.3-contributor" if contributor else "ordinary-cloud",
            local=local,
        )
        self.calls = []
        self.responses = list(responses or [])

    async def prepare_complete(self, messages):
        """Explicit local test double; no tokenizer or inference claim."""
        return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

    async def complete(self, messages):
        self.calls.append(messages)
        item = (
            self.responses.pop(0)
            if self.responses
            else {
                "tool": "finish",
                "arguments": {"objective_revision": 1},
            }
        )
        if callable(item):
            item = await item()
        return json.dumps(item)


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "public.txt").write_text("public fixture")
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    provider = Spy()
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace))
    yield tmp_path, store, session, provider, runtime
    await runtime.close()
    store.close()


def request(session, *, scope="local_only", key="task", sources=None, revision=1):
    return TaskRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Read public.txt",
        content_scope=scope,
        idempotency_key=key,
        source_event_ids=sources or [],
        source_revision=revision,
    )


def user(store, session, *, scope="local_only", revision=1, text="Read public.txt"):
    payload = {"text": text, "final": True, "revision": revision}
    if scope is not None:
        payload["content_scope"] = scope
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload=payload,
            idempotency_key="user",
        ),
        "owner",
    )


def correction(store, session, source, revision, scope, text="Read public.txt"):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.revision",
            payload={
                "target_event_id": source.event_id,
                "text": text,
                "revision": revision,
                "content_scope": scope,
            },
            idempotency_key=f"correction:{revision}",
        ),
        "owner",
    )


@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized"])
async def test_contributor_direct_request_denied_before_acceptance_or_inference(env, scope):
    _, store, session, provider, runtime = env
    with pytest.raises(PolicyDenied, match="disclosure"):
        await runtime.submit(request(session, scope=scope))
    assert provider.calls == []
    assert await runtime.status() == []
    assert store.read(kinds=["agent.command"]) == []


@pytest.mark.parametrize(
    "local,contributor,scope",
    [
        (True, True, "local_only"),
        (True, False, "private_cloud_authorized"),
        (False, True, "public_synthetic"),
        (False, False, "private_cloud_authorized"),
    ],
)
async def test_permitted_target_runs_and_all_native_task_records_retain_scope(
    env, local, contributor, scope
):
    _, store, session, _, runtime = env
    provider = runtime.provider = Spy(
        local=local,
        contributor=contributor,
        responses=[
            {"tool": "read_file", "arguments": {"path": "public.txt"}},
            {"tool": "finish", "arguments": {"objective_revision": 1}},
        ],
    )
    task = await runtime.submit(request(session, scope=scope))
    result = await runtime.wait(task.task_id, timeout=2)
    assert result.state == "succeeded" and result.content_scope == scope
    assert len(provider.calls) == 2
    entries = store.read(task_id=task.task_id)
    assert entries and all(e.payload["content_scope"] == scope for e in entries)
    assert entries[-1].payload["task"]["content_scope"] == scope


@pytest.mark.parametrize("scope", [None, "local_only", "private_cloud_authorized"])
@pytest.mark.parametrize("actor", ["owner", "decisions"])
async def test_public_request_cannot_widen_finalized_source_scope(env, scope, actor):
    _, store, session, provider, runtime = env
    source = user(store, session, scope=scope)
    with pytest.raises(PolicyDenied, match="disclosure"):
        await runtime.submit(
            request(session, scope="public_synthetic", sources=[source.event_id]), actor=actor
        )
    assert provider.calls == []


@pytest.mark.parametrize("middle", ["local_only", "private_cloud_authorized", [], None])
async def test_later_public_correction_cannot_erase_intervening_native_scope(env, middle):
    _, store, session, provider, runtime = env
    source = user(store, session, scope="public_synthetic")
    correction(store, session, source, 2, middle)
    correction(store, session, source, 3, "public_synthetic")
    with pytest.raises(PolicyDenied, match="disclosure"):
        await runtime.submit(
            request(session, scope="public_synthetic", sources=[source.event_id], revision=3),
            actor="decisions",
        )
    assert provider.calls == []


@pytest.mark.parametrize("revision", [2.0, True])
async def test_unprocessable_native_revision_never_restores_cloud_permission(env, revision):
    _, store, session, provider, runtime = env
    source = user(
        store, session, scope="public_synthetic", revision=True if revision is True else 1
    )
    correction(store, session, source, 2 if revision is True else revision, "public_synthetic")
    with pytest.raises(PolicyDenied, match="disclosure"):
        await runtime.submit(
            request(session, scope="public_synthetic", sources=[source.event_id], revision=2),
            actor="decisions",
        )
    assert provider.calls == []


async def test_authentic_public_finalized_revision_can_route_and_keeps_its_scope(env):
    _, store, session, provider, runtime = env
    source = user(store, session, scope="public_synthetic")
    correction(store, session, source, 2, "public_synthetic")
    task = await runtime.submit(
        request(session, scope="public_synthetic", sources=[source.event_id], revision=2),
        actor="decisions",
    )
    result = await runtime.wait(task.task_id, timeout=2)
    assert result.content_scope == "public_synthetic" and provider.calls
    assert result.source_event_ids == [source.event_id]


async def test_public_correction_cannot_regrant_original_local_source(env):
    _, store, session, provider, runtime = env
    source = user(store, session)
    correction(store, session, source, 2, "public_synthetic")
    with pytest.raises(PolicyDenied, match="disclosure"):
        await runtime.submit(
            request(session, scope="public_synthetic", sources=[source.event_id], revision=2),
            actor="decisions",
        )
    assert provider.calls == []


async def test_unscoped_intermediate_revision_keeps_local_floor_after_public_revision(env):
    _, store, session, provider, runtime = env
    source = user(store, session, scope="public_synthetic")
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.revision",
            payload={
                "target_event_id": source.event_id,
                "text": "Unscoped correction",
                "revision": 2,
            },
            idempotency_key="unscoped-revision",
        ),
        "owner",
    )
    correction(store, session, source, 3, "public_synthetic")
    with pytest.raises(PolicyDenied, match="disclosure"):
        await runtime.submit(
            request(session, scope="public_synthetic", sources=[source.event_id], revision=3),
            actor="decisions",
        )
    assert provider.calls == []


async def test_exhausted_source_revision_page_fails_closed_without_inference(env):
    _, store, session, provider, runtime = env
    source = user(store, session, scope="public_synthetic")
    for revision in range(2, 202):
        correction(store, session, source, revision, "public_synthetic")
    with pytest.raises(PolicyDenied, match="disclosure"):
        await runtime.submit(
            request(session, scope="public_synthetic", sources=[source.event_id], revision=201),
            actor="decisions",
        )
    assert provider.calls == []


async def test_transcript_tool_does_not_export_a_relabelled_local_source(env):
    _, store, session, provider, runtime = env
    source = user(store, session, text="PRIVATE ORIGINAL")
    correction(store, session, source, 2, "public_synthetic", text="PRIVATE CORRECTION")
    provider.responses.extend(
        [
            {"tool": "read_transcript", "arguments": {"event_ids": [source.event_id]}},
            {"tool": "finish", "arguments": {"objective_revision": 1}},
        ]
    )
    task = await runtime.submit(request(session, scope="public_synthetic"))
    result = await runtime.wait(task.task_id, timeout=2)
    assert result.state == "failed"
    assert "PRIVATE ORIGINAL" not in json.dumps(provider.calls)
    assert "PRIVATE CORRECTION" not in json.dumps(provider.calls)


async def test_denied_scope_is_an_authenticated_http_refusal_with_zero_calls(env):
    _, _, session, provider, runtime = env
    tokens = {role: role + "-synthetic-token-0123456789abcdef" for role in ("owner", "agent")}
    app = create_app(runtime, Auth(tokens))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        result = await c.post(
            "/v1/agent/tasks",
            json=request(session).model_dump(),
            headers={"Authorization": "Bearer " + tokens["owner"]},
        )
    assert result.status_code == 403 and provider.calls == []


async def test_source_revocation_during_inference_prevents_another_call_or_effect(env):
    _, store, session, _, runtime = env
    entered, release = asyncio.Event(), asyncio.Event()

    async def first():
        entered.set()
        await release.wait()
        return {"tool": "write_file", "arguments": {"path": "leak.txt", "content": "no"}}

    provider = runtime.provider = Spy(responses=[first])
    source = user(store, session, scope="public_synthetic")
    task = await runtime.submit(
        request(session, scope="public_synthetic", sources=[source.event_id]), actor="decisions"
    )
    await asyncio.wait_for(entered.wait(), 1)
    correction(store, session, source, 2, "local_only")
    release.set()
    result = await runtime.wait(task.task_id, timeout=2)
    assert result.state == "failed" and result.content_scope == "local_only"
    assert len(provider.calls) == 1
    assert not (runtime.workspace / "leak.txt").exists()
    assert store.read(task_id=task.task_id)[-1].payload["content_scope"] == "local_only"


async def test_local_task_and_steer_cannot_be_promoted_by_changing_provider(env):
    _, _, session, _, runtime = env
    entered, release = asyncio.Event(), asyncio.Event()

    async def first():
        entered.set()
        await release.wait()
        return {"tool": "read_file", "arguments": {"path": "public.txt"}}

    local = runtime.provider = Spy(local=True, responses=[first])
    task = await runtime.submit(request(session))
    await asyncio.wait_for(entered.wait(), 1)
    updated = await runtime.steer(
        task.task_id,
        SteerRequest(
            instruction="Continue the local task",
            content_scope="public_synthetic",
            expected_objective_revision=1,
            idempotency_key="steer",
        ),
    )
    assert updated.content_scope == "local_only"
    cloud = runtime.provider = Spy()
    release.set()
    result = await runtime.wait(task.task_id, timeout=2)
    assert result.state == "failed" and result.content_scope == "local_only"
    assert len(local.calls) == 1 and cloud.calls == []


async def test_source_less_local_steer_denied_for_active_public_contributor_task(env):
    _, _, session, _, runtime = env
    entered = asyncio.Event()

    async def block():
        entered.set()
        await asyncio.Event().wait()

    provider = runtime.provider = Spy(responses=[block])
    task = await runtime.submit(request(session, scope="public_synthetic"))
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(PolicyDenied, match="Steering disclosure"):
        await runtime.steer(
            task.task_id,
            SteerRequest(
                instruction="Private new constraint",
                expected_objective_revision=1,
                idempotency_key="steer",
            ),
        )
    assert len(provider.calls) == 1
    assert "Private new constraint" not in (await runtime.status(task.task_id)).objective
    await runtime.interrupt(
        task.task_id, InterruptRequest(expected_objective_revision=1, idempotency_key="stop")
    )


async def test_approval_continuation_rechecks_provider_but_owner_denial_still_works(env):
    _, _, session, _, runtime = env
    runtime.provider = Spy(
        local=True,
        responses=[{"tool": "write_file", "arguments": {"path": "new.txt", "content": "x"}}],
    )
    task = await runtime.submit(request(session))
    for _ in range(100):
        approvals = (await runtime.pending_approvals()).approvals
        if approvals:
            break
        await asyncio.sleep(0)
    assert len(approvals) == 1
    cloud = runtime.provider = Spy()
    approval = approvals[0]
    resolution = ApprovalResolution(
        approved=True,
        action_digest=approval.action_digest,
        objective_revision=approval.objective_revision,
    )
    with pytest.raises(PolicyDenied, match="disclosure"):
        await runtime.resolve_approval(approval.approval_id, resolution)
    await runtime.resolve_approval(
        approval.approval_id, resolution.model_copy(update={"approved": False})
    )
    assert (await runtime.wait(task.task_id)).state == "failed"
    assert cloud.calls == [] and not (runtime.workspace / "new.txt").exists()


async def test_historical_unscoped_recovery_remains_local_and_controls_need_no_export(env):
    _, store, session, provider, runtime = env
    task_id = "historical"
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.command",
            task_refs=[task_id],
            payload={
                "op": "submit",
                "task_id": task_id,
                "session_id": session.session_id,
                "thread_id": session.thread_id,
                "epoch": session.epoch,
                "objective": "old private request",
                "idempotency_key": "old",
            },
            idempotency_key="agent:historical:agent.command:1",
        ),
        "agent",
    )
    recovered = await runtime.recover()
    assert recovered[0].content_scope == "local_only" and recovered[0].state == "paused"
    registry = Registry(runtime)
    try:
        result = await registry.execute(
            CommandRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                input="/status",
                idempotency_key="status",
            )
        )
        assert result["result"][0]["content_scope"] == "local_only"
        await registry.execute(
            CommandRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                input="/stop historical",
                idempotency_key="stop",
            )
        )
        assert (await runtime.status(task_id)).state == "cancelled"
        assert provider.calls == []
    finally:
        registry.close()


async def test_local_profile_control_does_not_export_or_resume_old_work(env):
    _, _, session, provider, runtime = env
    registry = Registry(runtime, profiles={"cloud": lambda: provider})
    try:
        result = await registry.execute(
            CommandRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                input="/model cloud",
                idempotency_key="model",
            )
        )
        assert result["status"] == "ok" and provider.calls == []
        with pytest.raises(PolicyDenied, match="disclosure"):
            await runtime.submit(request(session))
    finally:
        registry.close()


async def test_pre_scope_submit_idempotency_replays_locally_without_reexecution(env):
    _, store, session, _, runtime = env
    provider = runtime.provider = Spy(local=True)
    body = request(session)
    old_payload = {
        key: getattr(body, key)
        for key in (
            "session_id",
            "thread_id",
            "epoch",
            "objective",
            "source_event_ids",
            "source_revision",
        )
    }
    digest = hashlib.sha256(
        json.dumps(old_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.command",
            task_refs=["old"],
            idempotency_key="old-submit",
            payload={
                **old_payload,
                "task_id": "old",
                "op": "submit",
                "idempotency_key": body.idempotency_key,
                "payload_digest": digest,
            },
        ),
        "agent",
    )
    await runtime.recover()
    task = await runtime.submit(body)
    assert task.task_id == "old" and task.state == "paused"
    assert task.content_scope == "local_only" and provider.calls == []


async def test_submit_grant_is_part_of_idempotency_and_cannot_relabel_an_old_task(env):
    _, _, session, _, runtime = env
    runtime.provider = Spy(local=True)
    body = request(session)
    task = await runtime.submit(body)
    await runtime.wait(task.task_id, timeout=2)
    from services.agent.runtime import IdemConflict

    with pytest.raises(IdemConflict):
        await runtime.submit(body.model_copy(update={"content_scope": "public_synthetic"}))
    assert (await runtime.status(task.task_id)).content_scope == "local_only"


async def test_historical_control_replay_needs_no_new_export_grant(env):
    _, _, session, provider, runtime = env
    registry = Registry(runtime)
    body = CommandRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        input="/status",
        idempotency_key="historical-control",
    )
    old_digest = hashlib.sha256(
        body.model_dump_json(exclude={"content_scope"}).encode()
    ).hexdigest()
    old_result = {"status": "ok", "command": "status", "result": []}
    registry.db.execute(
        "INSERT INTO commands VALUES (?,?,?,?)",
        (session.session_id, body.idempotency_key, old_digest, json.dumps(old_result)),
    )
    registry.db.commit()
    try:
        assert await registry.execute(body) == old_result
        with pytest.raises(ValueError, match="conflicts"):
            await registry.execute(body.model_copy(update={"content_scope": "public_synthetic"}))
        assert provider.calls == []
    finally:
        registry.close()


@pytest.mark.parametrize("command", ["/skill {skill} public work", "/recipe public work"])
@pytest.mark.parametrize("scope,allowed", [("local_only", False), ("public_synthetic", True)])
async def test_skill_created_task_inherits_owner_command_scope(env, command, scope, allowed):
    root, _, session, provider, runtime = env
    source = root / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: recipe\ndescription: public fixture\nbare-invocable: true\n---\nRead public.txt.\n"
    )
    service = SkillService(root / "skills", runtime.workspace)
    runtime.skill_service = service
    installed = await service.mutate(
        "install", source=str(source), content_scope="public_synthetic", authority="owner"
    )
    await service.mutate("enable", skill_id=installed["id"], authority="owner")
    registry = Registry(runtime)
    body = CommandRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        input=command.format(skill=installed["id"]),
        content_scope=scope,
        idempotency_key="skill-command",
    )
    try:
        if not allowed:
            with pytest.raises(PolicyDenied, match="disclosure"):
                await registry.execute(body)
            assert provider.calls == [] and await runtime.status() == []
        else:
            result = await registry.execute(body)
            assert result["status"] == "ok"
            task = await runtime.wait(result["result"]["accepted_task_id"], timeout=2)
            assert task.content_scope == "public_synthetic" and provider.calls
    finally:
        registry.close()
