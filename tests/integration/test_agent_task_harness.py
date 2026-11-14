"""Synthetic native task loop with real isolated generated code, no providers."""

import asyncio
import copy
import hashlib
import json

import pytest

from core.policy import PolicyDenied, ToolPolicy
from core.task_harness import json_bytes
from harnesses.seed import parse_action
from packages.contracts.agent_tools import LEGACY_NATIVE_TOOL_ORDER, native_tool_snapshot
from packages.contracts.models import (
    InterruptRequest,
    ProviderInfo,
    SessionRequest,
    TaskRequest,
    TaskState,
    ToolAction,
)
from packages.providers.completion_budget import PreparedComplete
from packages.providers.meta_action_wire import action_schema, decode_action, encode_action
from packages.providers.text import _gateway_action_object
from services.agent.artifacts import ArtifactStore
from services.agent.runtime import AgentRuntime
from services.session.store import LocalJournal, Store

SOURCE = """def run(payload):
    if payload["observation"] is None:
        return {"op": "consult_media", "request": payload["input"], "state": None}
    return {"op": "finish", "result": {"transcript": payload["observation"]["text"]}}
"""
SOURCE_SHA = hashlib.sha256(SOURCE.encode()).hexdigest()
MEDIA_REQUEST = {
    "operation": "transcribe_video",
    "source_path": "synthetic.mp4",
    "source_sha256": "a" * 64,
}
RUN = {
    "tool": "run_task_harness",
    "arguments": {"source_path": "task.py", "source_sha256": SOURCE_SHA, "input": MEDIA_REQUEST},
}
WRITE = {
    "tool": "write_file",
    "arguments": {"path": "task.py", "content": SOURCE, "objective_revision": 1},
}
FINISH = {
    "tool": "finish",
    "arguments": {
        "summary": "Completed synthetic task harness and published result",
        "objective_revision": 1,
    },
}
UI = {
    "tool": "publish_ui",
    "arguments": {
        "title": "Synthetic transcript",
        "summary": "Synthetic fixture only",
        "components": [
            {
                "id": "transcript",
                "type": "text",
                "data": {"text": "Synthetic transcript observation"},
            }
        ],
    },
}


class Scripted:
    info = ProviderInfo(provider="fixture", model="scripted-harness", local=True)

    def __init__(self, actions):
        self.actions, self.calls = list(actions), []

    async def prepare_complete(self, messages):
        return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

    async def complete(self, messages):
        self.calls.append(copy.deepcopy(messages))
        return json.dumps(self.actions.pop(0) if self.actions else FINISH)


class Observed:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **_):
        return copy.deepcopy(self.value)


class Broker:
    def __init__(self, factory, rec, current):
        self.factory, self.rec, self.current = factory, rec, current
        self.required_operations = factory.required_operations
        self.calls, self.results, self.closes = [], [], 0

    async def call(self, request):
        self.current()
        self.calls.append(request)
        task = self.rec.task
        result = {
            "text": "Synthetic transcript observation",
            "data": {},
            "operation": request["operation"],
            "status": "completed",
            "model": "sam-3.1"
            if request["operation"] == "sam_hands"
            else "muse-spark-1.3-contributor",
            "binding": {
                "task_id": task.task_id,
                "session_id": task.session_id,
                "thread_id": task.thread_id,
                "epoch": task.epoch,
                "objective_revision": task.objective_revision,
            },
            "source_path": request["source_path"],
            "source_sha256": request["source_sha256"],
            "source_bytes": 16,
            "content_scope": self.factory.scope,
            "request_sha256": "b" * 64,
            "provider_request_sha256": "c" * 64,
            "provider_response_sha256": "d" * 64,
        }
        result["result_sha256"] = hashlib.sha256(json_bytes(result)).hexdigest()
        self.results.append(copy.deepcopy(result))
        return Observed(result)

    def check_primary_export(self, provider_info, result):
        self.current()
        if not self.factory.allowed or provider_info != Scripted.info or result not in self.results:
            raise PolicyDenied("Synthetic exact primary-result grant withdrawn")

    async def close(self):
        self.closes += 1
        return self.factory.settled


class Factory:
    def __init__(self, *, settled=True):
        self.allowed, self.settled, self.brokers = True, settled, []
        self.required_operations = frozenset()
        self.scope = "public_synthetic"

    def available(self, runtime, task):
        return self.allowed and runtime.provider.info == Scripted.info

    def __call__(self, runtime, rec, current):
        broker = Broker(self, rec, current)
        self.brokers.append(broker)
        return broker


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent", harness_version="seed-v1"))
    provider = Scripted([WRITE, RUN, UI, FINISH])
    runtime = AgentRuntime(
        LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace, allow_write=True)
    )
    runtime.artifact_store = ArtifactStore(tmp_path / "artifacts.sqlite")
    factory = Factory()
    runtime.task_harness_broker_factory = factory
    try:
        yield runtime, provider, store, session, factory, workspace
    finally:
        await runtime.close()
        store.close()


async def submit(env):
    runtime, _, _, session, _, _ = env
    task = await runtime.submit(
        TaskRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="Author and execute a new task-specific harness, then publish its synthetic transcript",
            content_scope="public_synthetic",
            idempotency_key="synthetic-task-harness",
        )
    )
    return await runtime.wait(task.task_id, timeout=20)


async def test_native_write_isolated_execution_publication_and_completion(env):
    runtime, provider, store, _, factory, workspace = env
    final = await submit(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert (workspace / "task.py").read_text() == SOURCE
    assert [item.kind for item in final.evidence] == ["file", "extension", "artifact"]
    assert len(factory.brokers) == 1
    assert factory.brokers[0].calls == [MEDIA_REQUEST]
    assert factory.brokers[0].closes == 1
    assert len(provider.calls) == 4
    harness = json.loads(next(item.output for item in final.evidence if item.kind == "extension"))
    assert harness["task_id"] == final.task_id
    assert harness["consultations"][0]["provider_request_sha256"] == "c" * 64
    assert harness["task_completion"] == "not_established"
    entries = store.read(task_id=final.task_id, limit=100)
    assert any(
        item.kind == "agent.tool" and item.payload.get("phase") == "media_consultation"
        for item in entries
    )
    assert sum(item.kind == "agent.outcome" for item in entries) == 1


async def test_enrolled_source_metadata_is_captured_once_before_first_inference(env):
    runtime, provider, _, _, factory, _ = env
    descriptions = []
    description = {
        "source_path": MEDIA_REQUEST["source_path"],
        "source_sha256": MEDIA_REQUEST["source_sha256"],
        "source_bytes": 16,
        "allowed_operations": ["sam_hands", "transcribe_video"],
        "required_operations": [],
    }

    def describe(actual_runtime, task):
        assert actual_runtime is runtime
        assert task.state == TaskState.RUNNING
        descriptions.append(copy.deepcopy(description))
        return description

    factory.describe = describe
    final = await submit(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert descriptions == [description]
    encoded = json.dumps(description, sort_keys=True, separators=(",", ":"))
    assert all(encoded in messages[1]["content"] for messages in provider.calls)
    assert runtime._get(final.task_id).task_harness_description == description


async def test_existing_source_without_native_write_cannot_execute_or_hide_failure(env):
    _, provider, _, _, factory, workspace = env
    (workspace / "task.py").write_text(SOURCE)
    provider.actions = [
        RUN,
        {
            "tool": "write_file",
            "arguments": {
                "path": "unrelated.txt",
                "content": "independent file",
                "objective_revision": 1,
            },
        },
        FINISH,
    ]
    final = await submit(env)
    assert final.state == TaskState.FAILED
    assert "task harness" in final.error
    assert factory.brokers == []


async def test_repeated_identical_action_reuses_original_result_without_provider_retry(env):
    _, provider, _, _, factory, _ = env
    provider.actions = [WRITE, RUN, RUN, UI, FINISH]
    final = await submit(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert len(factory.brokers) == 1
    assert factory.brokers[0].calls == [MEDIA_REQUEST]
    assert factory.brokers[0].closes == 1


async def test_grant_withdrawal_after_receipt_prevents_next_primary_export(env):
    runtime, provider, _, _, factory, _ = env
    original = runtime._append

    async def append(rec, kind, payload, **kwargs):
        result = await original(rec, kind, payload, **kwargs)
        if (
            kind == "agent.tool"
            and payload.get("tool") == "run_task_harness"
            and payload.get("ok")
            and "phase" not in payload
        ):
            factory.allowed = False
        return result

    runtime._append = append
    final = await submit(env)
    assert final.state == TaskState.FAILED
    assert len(provider.calls) == 2
    assert len(factory.brokers[0].calls) == 1
    assert not any(item.kind == "artifact" for item in final.evidence)


async def test_unconfirmed_media_close_stays_busy_and_cannot_finish(env):
    runtime, provider, _, _, factory, _ = env
    factory.settled = False
    final = await submit(env)
    assert final.state == TaskState.FAILED
    assert runtime.runtime_settings_busy()
    assert len(factory.brokers[0].calls) == 1
    assert factory.brokers[0].closes == 1
    assert len(provider.calls) == 2


async def test_native_asl_requirement_refuses_a_program_which_omits_sam(env):
    _, _, _, _, factory, _ = env
    factory.required_operations = frozenset({"sam_hands", "transcribe_video"})
    final = await submit(env)
    assert final.state == TaskState.FAILED
    assert "task harness" in final.error
    assert [item["operation"] for item in factory.brokers[0].calls] == ["transcribe_video"]


async def test_native_asl_requirement_observes_both_actual_operations(env):
    _, provider, _, _, factory, _ = env
    source = """def run(payload):
    if payload["state"] is None:
        return {"op":"consult_media", "request":dict(payload["input"], operation="sam_hands"), "state":"sam_done"}
    if payload["state"] == "sam_done":
        return {"op":"consult_media", "request":payload["input"], "state":"transcription_done"}
    return {"op":"finish", "result":{"transcript":payload["observation"]["text"]}}
"""
    sha = hashlib.sha256(source.encode()).hexdigest()
    factory.required_operations = frozenset({"sam_hands", "transcribe_video"})
    provider.actions = [
        {
            "tool": "write_file",
            "arguments": {"path": "task.py", "content": source, "objective_revision": 1},
        },
        {
            "tool": "run_task_harness",
            "arguments": {"source_path": "task.py", "source_sha256": sha, "input": MEDIA_REQUEST},
        },
        UI,
        FINISH,
    ]
    final = await submit(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert [item["operation"] for item in factory.brokers[0].calls] == [
        "sam_hands",
        "transcribe_video",
    ]


async def test_private_result_persists_private_artifact_and_outcome_scope(env):
    runtime, provider, store, _, factory, _ = env
    factory.scope = "private_cloud_authorized"
    final = await submit(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    assert len(provider.calls) == 4
    entries = store.read(task_id=final.task_id, limit=100)
    publications = [item for item in entries if item.kind == "artifact.updated"]
    outcomes = [item for item in entries if item.kind == "agent.outcome"]
    consultations = [item for item in entries if item.payload.get("phase") == "media_consultation"]
    assert publications and outcomes and consultations
    assert all(
        item.payload["content_scope"] == "private_cloud_authorized"
        for item in publications + outcomes + consultations
    )
    assert outcomes[-1].payload["task"]["content_scope"] == "private_cloud_authorized"
    evidence = next(item for item in final.evidence if item.kind == "artifact")
    assert json.loads(evidence.output) == {"native_publication_scope": "private_cloud_authorized"}
    private_task = final.model_copy(update={"content_scope": "private_cloud_authorized"})
    assert runtime.artifact_store.verify(evidence, private_task)
    assert final.content_scope == "private_cloud_authorized"
    assert not runtime.artifact_store.verify(
        evidence, final.model_copy(update={"content_scope": "public_synthetic"})
    )


async def test_earlier_public_placeholder_and_later_private_result_keep_native_scopes(env):
    runtime, provider, store, _, factory, _ = env
    factory.scope = "private_cloud_authorized"
    provider.actions = [UI, WRITE, RUN, UI, FINISH]
    final = await submit(env)
    assert final.state == TaskState.SUCCEEDED, final.error
    entries = store.read(task_id=final.task_id, limit=100)
    scopes = [item.payload["content_scope"] for item in entries if item.kind == "artifact.updated"]
    assert scopes == ["public_synthetic", "private_cloud_authorized"]
    receipt_scopes = [
        json.loads(item.output)["native_publication_scope"]
        for item in final.evidence
        if item.kind == "artifact"
    ]
    assert receipt_scopes == scopes
    assert runtime._verify_finish_artifacts(runtime._get(final.task_id)) is None


async def test_private_receipt_retains_private_terminal_scope_after_owner_cancel(env):
    runtime, provider, store, _, factory, _ = env
    factory.scope = "private_cloud_authorized"
    inference_started = asyncio.Event()
    original_complete = provider.complete

    async def complete(messages):
        if len(provider.calls) == 2:
            inference_started.set()
            await asyncio.Event().wait()
        return await original_complete(messages)

    provider.complete = complete
    waiting = asyncio.create_task(submit(env))
    await asyncio.wait_for(inference_started.wait(), timeout=10)
    task_id = factory.brokers[0].rec.task.task_id
    await runtime.interrupt(
        task_id,
        InterruptRequest(expected_objective_revision=1, idempotency_key="stop-private-task"),
    )
    final = await waiting
    assert final.state == TaskState.CANCELLED
    assert final.content_scope == "private_cloud_authorized"
    outcomes = [
        item for item in store.read(task_id=task_id, limit=100) if item.kind == "agent.outcome"
    ]
    assert len(outcomes) == 1
    assert outcomes[0].payload["content_scope"] == "private_cloud_authorized"
    assert outcomes[0].payload["task"]["content_scope"] == "private_cloud_authorized"
    assert factory.brokers[0].closes == 1


def test_optional_action_preserves_legacy_schema_and_advertisement():
    assert "run_task_harness" not in LEGACY_NATIVE_TOOL_ORDER
    assert "run_task_harness" not in ToolAction.model_json_schema()["properties"]["tool"]["enum"]
    assert not native_tool_snapshot().supports("run_task_harness")
    with pytest.raises(ValueError):
        parse_action(json.dumps(RUN))
    parsed = parse_action(json.dumps(RUN), allow_task_harness=True)
    assert parsed.tool == "run_task_harness"


def test_typed_meta_wire_adds_only_the_enabled_harness_branch():
    legacy = native_tool_snapshot()
    enabled = native_tool_snapshot(task_harness_available=True)
    assert "RunTaskHarness" not in json.dumps(action_schema(legacy.available_tools))
    wire = {
        "tool": "run_task_harness",
        "arguments": {
            "source_path": "task.py",
            "source_sha256": SOURCE_SHA,
            "harness_input_json": json.dumps(MEDIA_REQUEST),
        },
    }
    envelope = encode_action(wire)
    decoded = decode_action(envelope, enabled.available_tools)
    assert _gateway_action_object(decoded) == RUN
