"""Synthetic native program authority/provenance; no model or external calls."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.policy import PolicyDenied, ToolPolicy
from core.supervision import command_evidence, file_evidence
from packages.contracts.agent_tools import agent_tool_scope, native_tool_snapshot
from packages.contracts.models import ProviderInfo, TaskState
from packages.contracts.task_program import RunTaskProgramArguments
from packages.providers.meta_action_wire import action_schema, decode_action, encode_action
from services.agent import task_program as native
from services.agent.media_tools import MediaToolError
from services.agent.tools import ToolFailure


def sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    source = b"# Public synthetic task-authored program\nprint('fixture')\n"
    video = b"\x00\x00\x00\x18ftypmp42public synthetic binary input\x00\xff"
    (tmp_path / "program.py").write_bytes(source)
    (tmp_path / "input.mp4").write_bytes(video)
    policy = ToolPolicy(tmp_path, allow_write=True, allow_shell=True)
    task = SimpleNamespace(
        task_id="task-fixture",
        session_id="session-fixture",
        thread_id="thread-fixture",
        epoch=1,
        objective_revision=1,
        state=TaskState.RUNNING,
        evidence=[file_evidence(tmp_path / "program.py", source, 1, "Native fixture write")],
    )
    rec = SimpleNamespace(
        task=task,
        recovered=False,
        closing=False,
        cancel_requested=False,
        pending_steer=None,
        task_program_executions=[],
        task_program_proof=None,
        proc_slot={},
        task_harness_publication_scope="public_synthetic",
        task_harness_export_check=None,
    )
    info = ProviderInfo(provider="meta", model="muse-spark-1.3-contributor", local=False)
    grant = SimpleNamespace(
        source_path="input.mp4",
        source_sha256=sha(video),
        source_bytes=len(video),
        content_scope="private_cloud_authorized",
        primary_result_target=info,
    )
    enrolled = SimpleNamespace(grant=grant)
    provider = object()
    snapshot = native_tool_snapshot(task_harness_available=True, task_program_available=True)
    decision = None
    epoch_error = None
    speech = []

    async def scope(_):
        return None

    async def epoch(_):
        return epoch_error

    async def uncommitted(_):
        return speech

    runtime = SimpleNamespace(
        _closed=False,
        policy=policy,
        _tasks={task.task_id: rec},
        max_steps=2000,
        provider_for=lambda _: provider,
        provider_info_for=lambda _: info,
        tool_snapshot=lambda _: snapshot,
        _permission_fingerprint=lambda _: (policy.allow_shell, policy.deny_shell, policy.network),
        _permission_decision=lambda *_: decision,
        _task_scope=scope,
        _epoch_error=epoch,
        _uncommitted_speech_sources=uncommitted,
    )

    class Factory:
        allowed = True

        def _current(self, owner, record):
            if not self.allowed or owner is not runtime or record is not rec:
                raise PolicyDenied("Fixture owner authority withdrawn")
            return enrolled

        def program_available(self, owner, selected):
            return self.allowed and owner is runtime and selected is task

    factory = Factory()
    runtime.task_harness_broker_factory = factory
    calls = []

    async def execute(owner, fd, interpreter, argv, timeout, revision, **kwargs):
        assert owner is policy and interpreter == "python3" and revision == 1
        assert kwargs["source_sha256"] == sha(source)
        assert kwargs["input_sha256"] == sha(video)
        calls.append((fd, argv, kwargs))
        assert argv == ["/kinaiya-input", "/workspace/result.txt"]
        (tmp_path / "result.txt").write_text("Actual synthetic program output\n")
        return "fixture stdout", command_evidence("fixture", 0, "fixture", 1, "fixture exit")

    monkeypatch.setattr(native, "run_program", execute)
    arguments = {
        "interpreter": "python3",
        "source_path": "program.py",
        "source_sha256": sha(source),
        "entrypoint_path": "program.py",
        "entrypoint_sha256": sha(source),
        "input_path": "input.mp4",
        "input_sha256": sha(video),
        "output_path": "result.txt",
        "argv": ["{input}", "{output}"],
        "timeout_s": 120,
    }
    return SimpleNamespace(**locals())


@pytest.mark.parametrize(
    "change",
    [
        {"interpreter": "python"},
        {"source_path": "../private.py"},
        {"entrypoint_path": "/usr/bin/true"},
        {"input_path": "a//input"},
        {"output_path": "program.py"},
        {"timeout_s": True},
        {"timeout_s": 121},
        {"entrypoint_sha256": "wrong"},
        {"argv": ["{input}", "--output={output}"]},
        {"argv": ["{input}", "{output}", "{output}"]},
        {"argv": ["{input}", "{output}", "bad\0"]},
        {"argv": ["{input}", "{output}", "é" * 2049]},
        {"argv": ["{input}", "{output}"] + ["x"] * 31},
    ],
)
def test_closed_program_contract_refuses_malformed_or_unbounded_request(env, change):
    with pytest.raises((ValueError, ValidationError)):
        RunTaskProgramArguments.model_validate({**env.arguments, **change}, strict=True)


def test_program_advertisement_and_typed_wire_are_opt_in(env):
    legacy = native_tool_snapshot()
    assert not legacy.supports("run_task_program")
    selected = env.snapshot
    with agent_tool_scope(selected):
        wire = encode_action({"tool": "run_task_program", "arguments": env.arguments})
        assert decode_action(wire, selected.available_tools) == {
            "tool": "run_task_program",
            "arguments": env.arguments,
        }
        assert "run_task_program" in json.dumps(action_schema(selected.available_tools))
    assert "run_task_program" not in json.dumps(action_schema(legacy.available_tools))


async def test_native_receipt_binds_real_program_input_result_and_current_author(env):
    output, evidence = await native.run_task_program(env.runtime, env.rec, env.arguments)
    value = json.loads(output)
    assert value["text"] == "Actual synthetic program output\n"
    receipt = value["receipt"]
    assert receipt["contract"] == "task_program.v1"
    assert receipt["entrypoint_sha256"] == env.arguments["entrypoint_sha256"]
    assert receipt["input_sha256"] == env.arguments["input_sha256"]
    assert receipt["output_sha256"] == sha(b"Actual synthetic program output\n")
    assert receipt["exit_code"] == 0 and receipt["settled"] is True
    assert receipt["compiler_causality"] == receipt["task_completion"] == "not_established"
    assert env.rec.task_harness_publication_scope == "private_cloud_authorized"
    assert env.rec.task_program_export_check() is True
    env.task.evidence.append(evidence)
    assert native.check_program_completion(env.factory, env.runtime, env.rec) == value["text"]
    assert len(env.calls) == 1
    assert await native.run_task_program(env.runtime, env.rec, env.arguments) == (output, evidence)
    assert len(env.calls) == 1


@pytest.mark.parametrize(
    "damage",
    ["missing_author", "source_changed", "wrong_input", "shell_denied", "shell_ask", "withdrawn"],
)
async def test_no_execution_without_current_native_authority(env, damage):
    if damage == "missing_author":
        env.task.evidence.clear()
    elif damage == "source_changed":
        (env.tmp_path / "program.py").write_text("other source")
    elif damage == "wrong_input":
        env.arguments["input_sha256"] = "0" * 64
    elif damage == "shell_denied":
        env.policy.deny_shell = True
    elif damage == "shell_ask":
        env.policy.allow_shell = False
    else:
        env.factory.allowed = False
    with pytest.raises((ToolFailure, PolicyDenied)):
        await native.run_task_program(env.runtime, env.rec, env.arguments)
    assert not env.calls and env.rec.task_program_proof is None


async def test_fresh_output_prevents_previous_file_from_impersonating_execution(env):
    (env.tmp_path / "result.txt").write_text("stale result")
    with pytest.raises(ToolFailure, match="fresh"):
        await native.run_task_program(env.runtime, env.rec, env.arguments)
    assert not env.calls


@pytest.mark.parametrize("earlier_runs", [33, 1999])
async def test_program_runs_follow_existing_native_step_bound_without_32_run_cutoff(
    env, earlier_runs
):
    env.rec.task_program_executions[:] = [
        {"settled": True, "binding": b"earlier", "request_sha256": str(index), "result": None}
        for index in range(earlier_runs)
    ]
    await native.run_task_program(env.runtime, env.rec, env.arguments)
    assert len(env.rec.task_program_executions) == earlier_runs + 1
    assert len(env.calls) == 1


async def test_input_replacement_revokes_retained_primary_export_even_without_media_run(env):
    await native.run_task_program(env.runtime, env.rec, env.arguments)
    assert env.rec.task_program_export_check() is True
    (env.tmp_path / "input.mp4").write_bytes(env.video[:-1] + b"x")
    with pytest.raises(MediaToolError, match="source_changed"):
        env.rec.task_program_export_check()


@pytest.mark.parametrize(
    "damage",
    [
        "output_changed",
        "source_changed",
        "author_removed",
        "evidence_removed",
        "revision",
        "unsettled",
        "private_owner_revoked",
    ],
)
async def test_completion_refuses_changed_or_unconfirmed_native_result(env, damage):
    _, evidence = await native.run_task_program(env.runtime, env.rec, env.arguments)
    env.task.evidence.append(evidence)
    if damage == "output_changed":
        (env.tmp_path / "result.txt").write_text("replacement result")
    elif damage == "source_changed":
        (env.tmp_path / "program.py").write_text("replacement source")
    elif damage == "author_removed":
        env.task.evidence[:] = [evidence]
    elif damage == "evidence_removed":
        env.task.evidence.remove(evidence)
    elif damage == "revision":
        env.task.objective_revision += 1
    elif damage == "unsettled":
        env.rec.proc_slot["proc"] = object()
    else:
        env.factory.allowed = False
    with pytest.raises((ToolFailure, PolicyDenied)):
        native.check_program_completion(env.factory, env.runtime, env.rec)


async def test_failed_original_is_not_replayed_and_retains_native_run_record(env, monkeypatch):
    calls = []

    async def fail(*args, **kwargs):
        calls.append(True)
        raise ToolFailure("Synthetic original failed")

    monkeypatch.setattr(native, "run_program", fail)
    with pytest.raises(ToolFailure, match="original failed"):
        await native.run_task_program(env.runtime, env.rec, env.arguments)
    assert env.rec.task_program_executions[0]["settled"] is True
    with pytest.raises(ToolFailure, match="will not be replayed"):
        await native.run_task_program(env.runtime, env.rec, env.arguments)
    assert calls == [True] and env.rec.task_program_proof is None
    assert env.rec.task_harness_publication_scope == "private_cloud_authorized"
    assert env.rec.task_program_export_check() is True


async def test_cancellation_with_retained_original_blocks_replacement(env, monkeypatch):
    async def uncertain(*args, **kwargs):
        env.rec.proc_slot["proc"] = object()
        raise asyncio.CancelledError

    monkeypatch.setattr(native, "run_program", uncertain)
    with pytest.raises(asyncio.CancelledError):
        await native.run_task_program(env.runtime, env.rec, env.arguments)
    assert env.rec._cleanup_uncertain is True
    assert env.rec.task_program_executions[0]["settled"] is False
    with pytest.raises(native.ProviderCleanupError):
        await native.run_task_program(env.runtime, env.rec, env.arguments)
