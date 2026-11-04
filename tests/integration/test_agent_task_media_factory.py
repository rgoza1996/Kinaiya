"""Real native receipts and owner files; no model, decoder, audio, or API runs."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from core.policy import ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    TaskRequest,
    TaskState,
    WindowDecision,
)
from packages.providers.text import ScriptedProvider
from services.agent.model_settings import AgentModelSettings
from services.agent.runtime import AgentRuntime
from services.agent.task_media_factory import TaskMediaFactory
from services.session.store import LocalJournal, Store

VIDEO = b"\x00\x00\x00\x18ftypmp42synthetic owner source"
SHA = hashlib.sha256(VIDEO).hexdigest()
AUDIO_SHA = "a" * 64
OBJECTIVE = "Create a new task-specific harness for the synthetic clip.mp4 fixture."


def write_plan(env, **changes):
    plan = {
        "schema_version": "task_media_owner.v1",
        "plan_id": "synthetic-owner-plan",
        "session_id": env.session.session_id,
        "thread_id": env.session.thread_id,
        "epoch": env.session.epoch,
        "utterance_id": "synthetic-voice-intent",
        "audio_sha256": AUDIO_SHA,
        "source_path": "clip.mp4",
        "source_sha256": SHA,
        "source_bytes": len(VIDEO),
        "content_scope": "private_cloud_authorized",
        "targets": [
            {"operation": "sam_hands", "model": "sam-3.1", "prompt": "hands"},
            {
                "operation": "transcribe_video",
                "model": "muse-spark-1.3-contributor",
                "reasoning": "xhigh",
                "prompt": "Describe only this synthetic clip.",
            },
        ],
        "primary_result_target": {
            "provider": "meta",
            "model": "muse-spark-1.3-contributor",
            "local": False,
        },
        "required_operations": ["sam_hands", "transcribe_video"],
        **changes,
    }
    env.plan_path.write_text(json.dumps(plan))
    env.plan_path.chmod(0o600)
    return plan


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "clip.mp4").write_bytes(VIDEO)
    owner_dir = tmp_path / "owner"
    owner_dir.mkdir(mode=0o700)
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    window = SpeechWindow(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        utterance_id="synthetic-voice-intent",
        window_index=0,
        revision=1,
        text=OBJECTIVE,
        start_ms=0,
        end_ms=2000,
        user_speaking=False,
        capture_ended=True,
        audio_sha256=AUDIO_SHA,
        content_scope="public_synthetic",
    )
    source = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.window",
            payload=window.model_dump(mode="json"),
            idempotency_key="synthetic-voice-source",
        ),
        "converse",
    )
    decision = WindowDecision(
        decision_id="synthetic-native-decision",
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=1,
        conversation="WAIT_FOR_NEXT_WINDOW",
        agent_route="QUEUE_UNTIL_TASK_DONE",
    )
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="routing.window_decided",
            payload=decision.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key=f"window-decision:{source.event_id}",
        ),
        "decisions",
    )
    primary = ScriptedProvider([])
    primary.info = ProviderInfo(provider="meta", model="muse-spark-1.3-contributor", local=False)
    primary.reasoning_effort = "xhigh"
    runtime = AgentRuntime(LocalJournal(store, "agent"), primary, workspace, ToolPolicy(workspace))
    runtime.model_settings = AgentModelSettings(primary, {}, path=tmp_path / "models.sqlite")
    calls, started, release = [], asyncio.Event(), asyncio.Event()

    async def worker(rec):
        calls.append(rec)
        rec.task.state = TaskState.RUNNING
        started.set()
        await release.wait()

    async def media_provider(*_):
        pytest.fail("Enrollment must not call a provider")

    runtime._run = worker
    result = SimpleNamespace(
        runtime=runtime,
        session=session,
        source=source,
        decision=decision,
        store=store,
        primary=primary,
        workspace=workspace,
        plan_path=owner_dir / "plan.json",
        calls=calls,
        started=started,
        release=release,
    )
    result.factory = TaskMediaFactory(plan_path=result.plan_path, provider=media_provider)
    runtime.task_harness_broker_factory = result.factory
    runtime.task_preworker_hook = result.factory.enroll
    write_plan(result)
    try:
        yield result
    finally:
        release.set()
        await runtime.close()
        store.close()


def request(env, **changes):
    key = (
        "window:queue_until_task_done:"
        + hashlib.sha256(f"{env.decision.decision_id}:QUEUE_UNTIL_TASK_DONE".encode()).hexdigest()[
            :32
        ]
    )
    return TaskRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        objective=OBJECTIVE,
        source_event_ids=[env.source.event_id],
        source_revision=1,
        content_scope="public_synthetic",
        idempotency_key=key,
        **changes,
    )


async def test_native_enrollment_precedes_worker_and_captures_exact_owner_grant(env):
    factory, runtime = env.factory, env.runtime
    assert not factory.available(runtime)
    accepted = await runtime.submit(request(env), actor="decisions")
    await asyncio.wait_for(env.started.wait(), 1)
    rec = env.calls[0]
    assert accepted.task_id == rec.native_task_identity.task_id
    assert factory.available(runtime, rec.task)
    broker = factory(runtime, rec, lambda: None)
    assert broker.grant.binding.task_id == accepted.task_id
    assert broker.grant.source_sha256 == SHA
    assert broker.grant.content_scope == "private_cloud_authorized"
    assert broker.required_operations == {"sam_hands", "transcribe_video"}
    assert broker.grant.targets[1].prompt == "Describe only this synthetic clip."
    assert factory.describe(runtime, rec.task) == {
        "source_path": "clip.mp4",
        "source_sha256": SHA,
        "source_bytes": len(VIDEO),
        "allowed_operations": ["sam_hands", "transcribe_video"],
        "required_operations": ["sam_hands", "transcribe_video"],
    }
    assert await broker.close()
    assert env.primary.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"audio_sha256": "b" * 64},
        {"utterance_id": "different-voice-intent"},
        {"session_id": "another-session"},
        {"epoch": 2},
        {"required_operations": ["sam_hands", "sam_hands"]},
    ],
)
async def test_owner_plan_mismatch_refuses_worker_but_retains_committed_task(env, changes):
    write_plan(env, **changes)
    refused = await env.runtime.submit(request(env), actor="decisions")
    assert refused.state == TaskState.FAILED
    assert env.calls == [] and env.primary.calls == []
    rec = env.runtime._tasks[refused.task_id]
    assert rec.submit_confirmed and rec.native_task_identity is not None
    entries = env.store.read(task_id=refused.task_id)
    assert any(
        entry.kind == "agent.command" and entry.payload.get("op") == "submit" for entry in entries
    )
    assert any(
        entry.kind == "agent.outcome" and entry.payload.get("state") == "failed"
        for entry in entries
    )
    again = await env.runtime.submit(request(env), actor="decisions")
    assert again.task_id == refused.task_id and again.state == TaskState.FAILED


@pytest.mark.parametrize("change", ["permissions", "symlink", "duplicate", "workspace"])
async def test_generated_or_unprotected_plan_cannot_authorize_enrollment(env, change):
    if change == "permissions":
        env.plan_path.chmod(0o644)
    elif change == "symlink":
        alternate = env.plan_path.with_name("alternate.json")
        env.plan_path.rename(alternate)
        env.plan_path.symlink_to(alternate)
    elif change == "duplicate":
        raw = env.plan_path.read_text()
        env.plan_path.write_text('{"plan_id":"forged",' + raw[1:])
    else:
        alternate = env.workspace / "plan.json"
        alternate.write_bytes(env.plan_path.read_bytes())
        alternate.chmod(0o600)
        env.factory.plan_path = alternate
    refused = await env.runtime.submit(request(env), actor="decisions")
    assert refused.state == TaskState.FAILED and env.calls == []


@pytest.mark.parametrize("change", ["plan", "model", "effort", "revision", "factory", "source"])
async def test_advertisement_and_broker_recheck_current_native_authority(env, change):
    await env.runtime.submit(request(env), actor="decisions")
    await asyncio.wait_for(env.started.wait(), 1)
    rec = env.calls[0]
    assert env.factory.available(env.runtime, rec.task)
    if change == "plan":
        write_plan(env, source_sha256="b" * 64)
    elif change == "model":
        env.primary.info = env.primary.info.model_copy(update={"model": "different-model"})
    elif change == "effort":
        env.primary.reasoning_effort = "high"
    elif change == "revision":
        rec.task.objective_revision += 1
    elif change == "factory":
        env.runtime.task_harness_broker_factory = None
    else:
        rec.source_revisions[env.source.event_id] = 2
    assert not env.factory.available(env.runtime, rec.task)
    with pytest.raises(RuntimeError, match="enrollment is unavailable"):
        env.factory(env.runtime, rec, lambda: None)


async def test_native_hook_without_configuration_preserves_legacy_submit(env):
    env.runtime.task_preworker_hook = None
    env.runtime.task_harness_broker_factory = None
    env.plan_path.unlink()
    accepted = await env.runtime.submit(request(env), actor="decisions")
    await asyncio.wait_for(env.started.wait(), 1)
    assert accepted.state == TaskState.ACCEPTED and len(env.calls) == 1


async def test_owner_submit_cannot_substitute_for_native_decisions_route(env):
    refused = await env.runtime.submit(request(env), actor="owner")
    assert refused.state == TaskState.FAILED and env.calls == []


async def test_decisions_actor_without_matching_native_route_key_cannot_enroll(env):
    forged = request(env).model_copy(update={"idempotency_key": "different-native-route"})
    refused = await env.runtime.submit(forged, actor="decisions")
    assert refused.state == TaskState.FAILED and env.calls == []


async def test_owner_plan_cannot_attach_to_second_task_in_designated_session(env):
    env.runtime.task_preworker_hook = None
    earlier = request(env).model_copy(update={"idempotency_key": "earlier-native-task"})
    await env.runtime.submit(earlier, actor="owner")
    await asyncio.wait_for(env.started.wait(), 1)
    env.runtime.task_preworker_hook = env.factory.enroll
    refused = await env.runtime.submit(request(env), actor="decisions")
    assert refused.state == TaskState.FAILED and len(env.calls) == 1


async def test_missing_or_changed_workspace_source_refuses_before_first_inference(env):
    (env.workspace / "clip.mp4").write_bytes(VIDEO[:-1] + b"!")
    refused = await env.runtime.submit(request(env), actor="decisions")
    assert refused.state == TaskState.FAILED and env.calls == [] and env.primary.calls == []


async def test_unsettled_enrollment_cannot_launch_after_caller_cancellation(env):
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed(runtime, rec):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        settled.set()
        return True

    env.runtime.task_preworker_hook = delayed
    operation = asyncio.create_task(env.runtime.submit(request(env), actor="decisions"))
    await entered.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert env.calls == [] and env.runtime._submit_operations
    release.set()
    await asyncio.wait_for(settled.wait(), 1)
    # The retained original must settle without granting a second launch.
    await asyncio.gather(*tuple(env.runtime._submit_operations), return_exceptions=True)
    assert env.calls == []
    rec = next(iter(env.runtime._tasks.values()))
    assert rec.submit_confirmed and rec.native_task_identity is not None
    again = await env.runtime.submit(request(env), actor="decisions")
    assert again.task_id == rec.task.task_id and env.calls == []
