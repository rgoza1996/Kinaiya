"""Real native receipts and owner files; no model, decoder, audio, or API runs."""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.policy import PolicyDenied, ToolPolicy
from core.task_harness import json_bytes
from packages.contracts.agent_media import MediaProviderResult
from packages.contracts.interfaces import AgentControlConflict
from packages.contracts.models import (
    AppendRequest,
    InterruptRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    SteerRequest,
    TaskRequest,
    TaskState,
    WindowDecision,
)
from packages.providers.text import ScriptedProvider
from services.agent.artifacts import ArtifactStore
from services.agent.media_assets import NativeMediaAssets
from services.agent.model_settings import AgentModelSettings
from services.agent.runtime import AgentRuntime
from services.agent.task_media_completion import MediaCompletionError
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
        if getattr(result, "media_assets", None) is not None:
            result.media_assets.close()
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


PROGRAM = """def run(payload):
    state = payload["state"] or {"next": 0}
    observed = payload["observation"]
    if observed is not None:
        if observed["operation"] == "transcribe_video":
            state["transcript"] = observed["text"]
        else:
            state["asset_id"] = observed["data"]["media_asset"]["asset_id"]
    operations = payload["input"]["operations"]
    if state["next"] < len(operations):
        operation = operations[state["next"]]
        state["next"] += 1
        return {"op": "consult_media", "request": {
            "operation": operation, "source_path": "clip.mp4",
            "source_sha256": payload["input"]["media_sha256"]}, "state": state}
    return {"op": "finish", "result": state}
"""
PROGRAM_SHA = hashlib.sha256(PROGRAM.encode()).hexdigest()
TRANSCRIPT = "First synthetic sentence.\nSecond synthetic sentence."


async def _prepare_completion(env, required_operations=None):
    if required_operations is not None:
        write_plan(env, required_operations=required_operations)
    env.runtime.policy.allow_write = True
    env.media_assets = NativeMediaAssets(env.plan_path.parent / "media.sqlite")
    env.runtime.artifact_store = ArtifactStore(
        env.plan_path.parent / "artifacts.sqlite", media_assets=env.media_assets
    )
    env.media_calls = []

    async def provider(prepared, current):
        current()
        operation = prepared.request.operation
        env.media_calls.append(operation)
        data = {}
        if operation == "sam_hands":
            env.asset = env.media_assets.register(
                binding=prepared.grant.binding.model_dump(mode="json"),
                content_scope=prepared.grant.content_scope,
                source_bytes=VIDEO,
                source_sha256=SHA,
                rendered_mp4=VIDEO + b"synthetic-highlight",
                overlay_sha256="b" * 64,
                width=16,
                height=16,
                duration_seconds=1.0,
                fps_num=1,
                fps_den=1,
                frame_count=1,
            )
            data = {"media_asset": env.asset}
        return MediaProviderResult(
            model=prepared.target.model,
            provider_request_sha256=hashlib.sha256(operation.encode()).hexdigest(),
            provider_response_sha256=hashlib.sha256((operation + "response").encode()).hexdigest(),
            text=TRANSCRIPT if operation == "transcribe_video" else "Synthetic hands observation",
            data=data,
        )

    env.factory.provider = provider
    task = await env.runtime.submit(request(env), actor="decisions")
    await asyncio.wait_for(env.started.wait(), 1)
    env.rec = env.runtime._tasks[task.task_id]
    return env


@pytest.fixture
async def completion_env(env):
    return await _prepare_completion(env)


async def _effect(env, tool, arguments):
    await env.runtime._execute(env.rec, tool, arguments, "")


async def _program(env, operations=None):
    await _effect(
        env,
        "write_file",
        {
            "path": "new-asl-task.py",
            "content": PROGRAM,
            "objective_revision": 1,
        },
    )
    await _effect(
        env,
        "run_task_harness",
        {
            "source_path": "new-asl-task.py",
            "source_sha256": PROGRAM_SHA,
            "input": {
                "media_sha256": SHA,
                "operations": operations or ["transcribe_video", "sam_hands"],
            },
        },
    )


async def _publish(env, *, text=TRANSCRIPT, asset=True, asset_id=None):
    components = []
    if asset:
        components.append(
            {
                "id": "hands",
                "type": "media",
                "data": {
                    "asset_id": asset_id or env.asset["asset_id"],
                },
            }
        )
    if text is not None:
        components.append({"id": "transcript", "type": "text", "data": {"text": text}})
    await _effect(
        env,
        "publish_ui",
        {
            "title": "Synthetic native media result",
            "summary": "Fixture only",
            "components": components,
        },
    )


async def _finish_media(env):
    return await env.runtime._finish(
        env.rec,
        {
            "objective_revision": 1,
            "summary": "Synthetic workflow complete",
        },
        "",
    )


async def test_enrolled_completion_requires_actual_both_operations_and_same_artifact(
    completion_env,
):
    env = completion_env
    # An earlier public placeholder remains valid alongside the private result.
    await _publish(env, text="Preparing the synthetic task", asset=False)
    await _program(env)
    assert env.media_calls == ["transcribe_video", "sam_hands"]
    await _publish(env, text="Transcript:\n" + TRANSCRIPT + "\nEnd of transcript")
    assert env.factory.check_completion(env.runtime, env.rec) is True
    await _finish_media(env)
    assert env.rec.task.state == TaskState.SUCCEEDED
    assert env.rec.task.content_scope == "private_cloud_authorized"
    assert env.rec.task_media_completion_check is not None


@pytest.mark.parametrize("raced_path", ["translation.txt", "solution.py"])
async def test_executable_owner_mode_requires_native_program_and_publishes_its_actual_output(
    env, monkeypatch, raced_path
):
    from core.supervision import command_evidence
    from services.agent import task_program as native

    write_plan(env, completion_mode="executable-v1")
    env.runtime.policy.allow_shell = True
    env = await _prepare_completion(env)
    assert env.runtime.tool_snapshot(env.rec.task).supports("run_task_program")
    await _program(env)
    # Real media consultations alone retain the original native obligation but
    # cannot satisfy this explicitly different owner execution requirement.
    await _publish(env)
    with pytest.raises(MediaCompletionError):
        env.factory.check_completion(env.runtime, env.rec)
    code = "# Public synthetic Agent-authored executable task\nprint('fixture')\n"
    code_sha = hashlib.sha256(code.encode()).hexdigest()
    await _effect(
        env, "write_file", {"path": "solution.py", "content": code, "objective_revision": 1}
    )
    actual = "Actual synthetic executable result, distinct from the video-model observation."

    async def execute(policy, fd, interpreter, argv, timeout, revision, **kwargs):
        assert policy is env.runtime.policy and interpreter == "python3"
        assert argv == ["/kinaiya-input", "/workspace/translation.txt"]
        assert kwargs["source_sha256"] == code_sha and kwargs["input_sha256"] == SHA
        (env.workspace / "translation.txt").write_text(actual)
        return "fixture", command_evidence("fixture", 0, "fixture", revision, "fixture exit")

    monkeypatch.setattr(native, "run_program", execute)
    await _effect(
        env,
        "run_task_program",
        {
            "interpreter": "python3",
            "source_path": "solution.py",
            "source_sha256": code_sha,
            "entrypoint_path": "solution.py",
            "entrypoint_sha256": code_sha,
            "input_path": "clip.mp4",
            "input_sha256": SHA,
            "output_path": "translation.txt",
            "argv": ["{input}", "{output}"],
            "timeout_s": 120,
        },
    )
    assert env.rec.last_tool_ok is True and env.rec.task_program_proof is not None
    # Publishing the old model text still cannot replace the actual program result.
    with pytest.raises(MediaCompletionError):
        env.factory.check_completion(env.runtime, env.rec)
    await _publish(env, text=actual)
    assert env.factory.check_completion(env.runtime, env.rec) is True
    original_get = env.runtime.artifact_store.get

    def race(*args, **kwargs):
        artifact = original_get(*args, **kwargs)
        (env.workspace / raced_path).write_text("changed during native publication validation")
        return artifact

    monkeypatch.setattr(env.runtime.artifact_store, "get", race)
    with pytest.raises(MediaCompletionError):
        env.factory.check_completion(env.runtime, env.rec)


async def test_legacy_owner_mode_never_advertises_executable_route(completion_env):
    env = completion_env
    assert not env.runtime.tool_snapshot(env.rec.task).supports("run_task_program")
    assert env.factory._enrollment.plan.completion_mode == "media-v1"


async def test_executable_enrollment_installs_private_floor_before_any_shell_or_program(env):
    write_plan(env, completion_mode="executable-v1")
    env = await _prepare_completion(env)
    assert env.rec.task_program_executions == [] and env.media_calls == []
    assert env.rec.task_harness_publication_scope == "private_cloud_authorized"
    assert env.rec.task_program_export_check() is True
    assert env.runtime._publication_task(env.rec).content_scope == "private_cloud_authorized"
    (env.workspace / "clip.mp4").write_bytes(VIDEO[:-1] + b"!")
    assert env.runtime._catalog_export_current(env.rec, []) is False
    assert env.rec.task.content_scope == "local_only"


@pytest.mark.parametrize("required", ["sam_hands", "transcribe_video"])
async def test_other_owner_plans_keep_their_declared_required_subset(env, required):
    env = await _prepare_completion(env, [required])
    await _program(env, [required])
    await _publish(
        env,
        text=TRANSCRIPT if required == "transcribe_video" else None,
        asset=required == "sam_hands",
    )
    await _finish_media(env)
    assert env.rec.task.state == TaskState.SUCCEEDED
    assert env.media_calls == [required]


@pytest.mark.parametrize("bypass", ["old_selector_read", "new_source_only", "text_only_ui"])
async def test_enrolled_finish_cannot_skip_the_native_workflow(completion_env, bypass):
    env = completion_env
    if bypass == "old_selector_read":
        (env.workspace / "old-selector.py").write_text("def select(payload): return []\n")
        await _effect(env, "read_file", {"path": "old-selector.py"})
    elif bypass == "new_source_only":
        await _effect(
            env,
            "write_file",
            {
                "path": "new-asl-task.py",
                "content": PROGRAM,
                "objective_revision": 1,
            },
        )
    else:
        await _publish(env, text="A claimed result without media operations", asset=False)
    await _finish_media(env)
    assert env.rec.task.state == TaskState.FAILED
    assert "enrolled native media workflow" in env.rec.task.error
    assert env.media_calls == []


@pytest.mark.parametrize("missing", ["sam_hands", "transcribe_video"])
async def test_one_required_operation_cannot_establish_completion(completion_env, missing):
    env = completion_env
    await _program(
        env, [operation for operation in ("sam_hands", "transcribe_video") if operation != missing]
    )
    assert env.rec.failed_task_harness
    with pytest.raises(MediaCompletionError):
        env.factory.check_completion(env.runtime, env.rec)
    await _finish_media(env)
    assert env.rec.task.state == TaskState.FAILED


@pytest.mark.parametrize(
    "damage",
    [
        "missing_execution",
        "missing_proof",
        "unsettled",
        "wrong_revision",
        "boolean_revision",
        "missing_result",
        "wrong_broker",
        "source_changed",
        "video_changed",
        "missing_source_write",
        "missing_extension",
        "old_selector_contract",
        "owner_plan_changed",
        "factory_removed",
        "cleanup_uncertain",
    ],
)
async def test_native_completion_refuses_missing_stale_or_substituted_execution(
    completion_env, damage
):
    env = completion_env
    await _program(env)
    await _publish(env)
    execution = env.rec.task_harness_execution
    proof = execution["completion_proof"]
    if damage == "missing_execution":
        env.rec.task_harness_execution = None
    elif damage == "missing_proof":
        execution.pop("completion_proof")
    elif damage == "unsettled":
        execution["settled"] = False
    elif damage == "wrong_revision":
        execution["revision"] = 2
    elif damage == "boolean_revision":
        execution["revision"] = True
    elif damage == "missing_result":
        execution["result"] = None
    elif damage == "wrong_broker":
        execution["broker"] = object()
    elif damage == "source_changed":
        (env.workspace / "new-asl-task.py").write_text(PROGRAM + "\n")
    elif damage == "video_changed":
        (env.workspace / "clip.mp4").write_bytes(VIDEO + b"changed")
    elif damage == "missing_source_write":
        env.rec.task.evidence = [e for e in env.rec.task.evidence if e.kind != "file"]
    elif damage == "missing_extension":
        env.rec.task.evidence = [e for e in env.rec.task.evidence if e.kind != "extension"]
    elif damage == "old_selector_contract":
        value = json.loads(execution["result"][0])
        value["contract"] = "context_selector.v1"
        output = json_bytes(value).decode()
        execution["result"] = (output, execution["result"][1])
        execution["completion_proof"] = replace(
            proof, result_sha256=hashlib.sha256(output.encode()).hexdigest()
        )
    elif damage == "owner_plan_changed":
        write_plan(env, plan_id="changed-owner-plan")
    elif damage == "factory_removed":
        env.runtime.task_harness_broker_factory = None
    elif damage == "cleanup_uncertain":
        env.rec._cleanup_uncertain = True
    with pytest.raises(MediaCompletionError):
        env.factory.check_completion(env.runtime, env.rec)
    assert len(env.media_calls) == 2
    # A replacement advertisement cannot remove the captured native obligation.
    assert env.rec.task_media_completion_check is not None
    if damage in {"owner_plan_changed", "factory_removed"}:
        # Direct _finish preserves the existing disclosure refusal. The outer
        # real _run catches this exception and records terminal failure.
        with pytest.raises(PolicyDenied, match="^Tool catalog disclosure changed; task stopped$"):
            await _finish_media(env)
        assert env.rec.task.state != TaskState.SUCCEEDED
    else:
        await _finish_media(env)
        assert env.rec.task.state == TaskState.FAILED
    if damage == "unsettled":
        assert execution["settled"] is False


@pytest.mark.parametrize(
    "damage",
    [
        "model",
        "source_sha256",
        "content_scope",
        "provider_response_sha256",
        "task_id",
        "objective_revision",
        "status",
        "text",
        "omit_operation",
    ],
)
async def test_completion_uses_immutable_original_broker_observations(completion_env, damage):
    env = completion_env
    await _program(env)
    await _publish(env)
    execution = env.rec.task_harness_execution
    proof = execution["completion_proof"]
    observations = list(proof.observations)
    value = json.loads(observations[0])
    if damage == "omit_operation":
        observations.pop(0)
    else:
        if damage in {"task_id", "objective_revision"}:
            value["binding"][damage] = "other-task" if damage == "task_id" else 2
        else:
            value[damage] = {
                "model": "other-model",
                "source_sha256": "f" * 64,
                "content_scope": "public_synthetic",
                "provider_response_sha256": "f" * 64,
                "status": "unknown",
                "text": "Fabricated replacement transcript",
            }[damage]
        value["result_sha256"] = hashlib.sha256(
            json_bytes({key: item for key, item in value.items() if key != "result_sha256"})
        ).hexdigest()
        observations[0] = json_bytes(value)
    execution["completion_proof"] = replace(proof, observations=tuple(observations))
    with pytest.raises(MediaCompletionError):
        env.factory.check_completion(env.runtime, env.rec)
    assert len(env.media_calls) == 2


@pytest.mark.parametrize(
    "publication",
    [
        "text_only",
        "media_only",
        "wrong_text",
        "separate",
        "wrong_asset",
        "corrupt_bytes",
        "wrong_scope",
        "wrong_task",
        "wrong_revision",
    ],
)
async def test_completion_requires_actual_media_and_full_transcript_together(
    completion_env, publication
):
    env = completion_env
    await _program(env)
    if publication == "text_only":
        await _publish(env, asset=False)
    elif publication == "media_only":
        await _publish(env, text=None)
    elif publication == "wrong_text":
        await _publish(env, text="A replacement summary")
    elif publication == "separate":
        await _publish(env, text=None)
        await _publish(env, asset=False)
    elif publication == "wrong_asset":
        other = env.media_assets.register(
            binding=env.asset["binding"],
            content_scope=env.asset["content_scope"],
            source_bytes=VIDEO,
            source_sha256=SHA,
            rendered_mp4=VIDEO + b"other-highlight",
            overlay_sha256="c" * 64,
            width=16,
            height=16,
            duration_seconds=1.0,
            fps_num=1,
            fps_den=1,
            frame_count=1,
        )
        await _publish(env, asset_id=other["asset_id"])
    elif publication == "corrupt_bytes":
        await _publish(env)
        env.media_assets._db.execute("UPDATE media_assets SET highlighted=?", (VIDEO + b"corrupt",))
    else:
        await _publish(env)
        column, value = {
            "wrong_scope": ("content_scope", "public_synthetic"),
            "wrong_task": ("task_id", "other-task"),
            "wrong_revision": ("objective_revision", 2),
        }[publication]
        env.runtime.artifact_store._conn.execute("UPDATE artifacts SET " + column + "=?", (value,))
    with pytest.raises(MediaCompletionError):
        env.factory.check_completion(env.runtime, env.rec)
    await _finish_media(env)
    assert env.rec.task.state == TaskState.FAILED


async def test_cancel_during_native_completion_validation_wins(completion_env):
    env = completion_env
    await _program(env)
    await _publish(env)
    original = env.rec.task_media_completion_check

    def cancelled():
        env.rec.cancel_requested = True
        return original()

    env.rec.task_media_completion_check = cancelled
    await _finish_media(env)
    assert env.rec.task.state == TaskState.CANCELLED


async def test_removing_factory_cannot_bypass_enrolled_finish(completion_env):
    env = completion_env
    # A generic public read can otherwise satisfy ordinary finish. No private
    # result/catalog guard exists yet; the captured owner obligation must act.
    (env.workspace / "public-fixture.txt").write_text("Synthetic native read fixture")
    await _effect(env, "read_file", {"path": "public-fixture.txt"})
    env.runtime.task_harness_broker_factory = None
    assert env.rec.task_media_completion_check is not None
    with pytest.raises(MediaCompletionError):
        env.rec.task_media_completion_check()
    await _finish_media(env)
    assert env.rec.task.state == TaskState.FAILED
    assert "enrolled native media workflow" in env.rec.task.error


@pytest.mark.parametrize("action", ["cancel", "steer"])
async def test_native_owner_change_during_final_await_wins(completion_env, action):
    env = completion_env
    if action == "cancel":
        await _program(env)
        await _publish(env)
    else:
        # Public owner steering remains admissible before private observations.
        # It must win over the enrolled workflow still being incomplete.
        await _effect(
            env,
            "write_file",
            {"path": "new-asl-task.py", "content": PROGRAM, "objective_revision": 1},
        )
        await _publish(env, text="Preparing the synthetic task", asset=False)
    entered, release = asyncio.Event(), asyncio.Event()
    verify = env.runtime._verify_finish_files

    async def held(rec):
        entered.set()
        await release.wait()
        return await verify(rec)

    env.runtime._verify_finish_files = held
    finishing = asyncio.create_task(_finish_media(env))
    await asyncio.wait_for(entered.wait(), 1)
    if action == "cancel":
        await env.runtime.interrupt(
            env.rec.task.task_id,
            InterruptRequest(
                expected_objective_revision=1,
                idempotency_key="synthetic-cancel-at-finish",
            ),
            actor="owner",
        )
    else:
        await env.runtime.steer(
            env.rec.task.task_id,
            SteerRequest(
                instruction="Revised synthetic task",
                content_scope="public_synthetic",
                expected_objective_revision=1,
                idempotency_key="synthetic-steer-at-finish",
            ),
            actor="owner",
        )
    release.set()
    await asyncio.wait_for(finishing, 2)
    assert env.rec.task.state != TaskState.SUCCEEDED
    if action == "cancel":
        assert env.rec.task.state == TaskState.CANCELLED
    else:
        assert env.rec.task.objective_revision == 2
    assert len(env.media_calls) == (2 if action == "cancel" else 0)


async def test_post_private_owner_steer_refusal_cannot_report_success(completion_env):
    env = completion_env
    await _program(env)
    await _publish(env)
    # The media result grant does not authorize a generic private primary
    # steering request or permit narrowing protected private journal history.
    with pytest.raises(AgentControlConflict):
        await env.runtime.steer(
            env.rec.task.task_id,
            SteerRequest(
                instruction="Revised synthetic task",
                content_scope="public_synthetic",
                expected_objective_revision=1,
                idempotency_key="synthetic-steer-after-private-observation",
            ),
            actor="owner",
        )
    assert env.rec.pending_steer is None
    assert env.rec.task.objective_revision == 1
    assert env.rec.task.state != TaskState.SUCCEEDED
    assert len(env.media_calls) == 2


async def test_non_enrolled_legacy_finish_retains_existing_behavior(env):
    env.runtime.task_harness_broker_factory = None
    env.runtime.task_preworker_hook = None
    task = await env.runtime.submit(request(env), actor="decisions")
    await asyncio.wait_for(env.started.wait(), 1)
    env.rec = env.runtime._tasks[task.task_id]
    (env.workspace / "public-fixture.txt").write_text("Synthetic native read fixture")
    await _effect(env, "read_file", {"path": "public-fixture.txt"})
    assert env.rec.task_media_completion_check is None
    await _finish_media(env)
    assert env.rec.task.state == TaskState.SUCCEEDED


WONDER_STANDARD = "muse-wonder-1.0"


def _wonder_owner_plan(env, **changes):
    plan = write_plan(env)
    plan["targets"][1].update(model=WONDER_STANDARD, reasoning="max")
    plan["primary_result_target"]["model"] = WONDER_STANDARD
    plan.update(changes)
    return write_plan(env, **plan)


def _wonder_primary(env):
    env.primary.info = ProviderInfo(provider="meta", model=WONDER_STANDARD, local=False)
    env.primary.reasoning_effort = "max"


async def test_wonder_standard_enrolls_exact_native_pin_and_bound_result_export(env):
    from services.agent.media_tools import MediaToolError

    _wonder_primary(env)
    plan = _wonder_owner_plan(env)
    media_calls = []

    async def observed(prepared, current):
        current()
        media_calls.append(prepared)
        return MediaProviderResult(
            model=WONDER_STANDARD,
            provider_request_sha256="b" * 64,
            provider_response_sha256="c" * 64,
            text="Synthetic Wonder transcript.",
            data={},
        )

    env.factory.provider = observed
    accepted = await env.runtime.submit(request(env), actor="decisions")
    await asyncio.wait_for(env.started.wait(), 1)
    rec = env.calls[0]
    assert accepted.task_id == rec.task.task_id
    assert (
        rec.task.model_pin.provider,
        rec.task.model_pin.model,
        rec.task.model_pin.local,
        rec.task.model_pin.reasoning,
    ) == (
        "meta",
        WONDER_STANDARD,
        False,
        "max",
    )
    assert env.factory.available(env.runtime, rec.task)
    broker = env.factory(env.runtime, rec, lambda: None)
    try:
        assert broker.grant.primary_result_target.model == WONDER_STANDARD
        assert broker.required_operations == {"sam_hands", "transcribe_video"}
        assert broker.grant.targets[1].prompt == plan["targets"][1]["prompt"]
        value = await broker.call(
            {"operation": "transcribe_video", "source_path": "clip.mp4", "source_sha256": SHA}
        )
        assert len(media_calls) == 1 and media_calls[0].target.reasoning == "max"
        assert value.model == WONDER_STANDARD and value.content_scope == "private_cloud_authorized"
        assert broker.check_primary_export(env.primary.info, value) is None
        for info in (
            ProviderInfo(provider="meta", model="muse-spark-1.3-contributor", local=False),
            ProviderInfo(provider="meta", model="muse-wonder-1.0-full", local=False),
            ProviderInfo(provider="meta", model=WONDER_STANDARD, local=True),
        ):
            with pytest.raises(MediaToolError):
                broker.check_primary_export(info, value)
    finally:
        assert await broker.close() is True
    assert env.primary.calls == []


@pytest.mark.parametrize(
    "damage",
    [
        "spark_primary",
        "full_primary",
        "effort",
        "local",
        "plan_primary",
        "plan_effort",
        "missing_primary_target",
    ],
)
async def test_wonder_owner_plan_cannot_change_primary_or_effort_authority(env, damage):
    _wonder_primary(env)
    plan = _wonder_owner_plan(env)
    if damage == "spark_primary":
        env.primary.info = ProviderInfo(
            provider="meta", model="muse-spark-1.3-contributor", local=False
        )
        env.primary.reasoning_effort = "xhigh"
    elif damage == "full_primary":
        env.primary.info = ProviderInfo(provider="meta", model="muse-wonder-1.0-full", local=False)
    elif damage == "effort":
        env.primary.reasoning_effort = "xhigh"
    elif damage == "local":
        env.primary.info = ProviderInfo(provider="meta", model=WONDER_STANDARD, local=True)
    elif damage == "plan_primary":
        plan["primary_result_target"]["model"] = "muse-spark-1.3-contributor"
    elif damage == "plan_effort":
        plan["targets"][1]["reasoning"] = "xhigh"
    else:
        del plan["primary_result_target"]
    env.plan_path.write_text(json.dumps(plan))
    refused = await env.runtime.submit(request(env), actor="decisions")
    assert refused.state == TaskState.FAILED
    assert env.calls == [] and env.primary.calls == []
    rec = env.runtime._tasks[refused.task_id]
    assert rec.submit_confirmed and rec.native_task_identity is not None
    assert not env.factory.available(env.runtime, rec.task)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "muse-spark-1.3-contributor"),
        ("reasoning", "xhigh"),
        ("local", True),
        ("provider", "other"),
    ],
)
async def test_wonder_native_pin_cannot_be_substituted_after_enrollment(env, field, value):
    _wonder_primary(env)
    _wonder_owner_plan(env)
    await env.runtime.submit(request(env), actor="decisions")
    await asyncio.wait_for(env.started.wait(), 1)
    rec = env.calls[0]
    assert env.factory.available(env.runtime, rec.task)
    rec.task.model_pin = rec.task.model_pin.model_copy(update={field: value})
    assert not env.factory.available(env.runtime, rec.task)
    with pytest.raises(RuntimeError, match="enrollment is unavailable"):
        env.factory(env.runtime, rec, lambda: None)
    assert env.primary.calls == []
