"""Legacy duplicate IDs must preserve each captured resolved-view check."""

import hashlib
import json

import pytest
from test_agent_transcript_paging import MeasuredFixture, append, finish, observation
from test_agent_transcript_paging import env as env

from core.policy import ToolPolicy
from packages.contracts.models import TaskRequest
from services.agent.runtime import AgentRuntime
from services.session.store import LocalJournal

OBSERVATIONS = []


@pytest.mark.parametrize("correction", [False, True])
async def test_legacy_duplicate_ids_refuse_inconsistent_current_views(env, correction, monkeypatch):
    original_text = "Synthetic original duplicate-ID view."
    corrected_text = "Synthetic corrected duplicate-ID view."
    target = append(env, original_text)
    snapshots, observations, published = [], [], []

    class RevisingJournal(LocalJournal):
        async def resolve(self, event_id):
            if event_id == target.event_id and len(snapshots) == 1 and correction:
                append(
                    env,
                    corrected_text,
                    kind="conversation.revision",
                    payload={"target_event_id": target.event_id, "revision": 2},
                )
            value = await super().resolve(event_id)
            if event_id == target.event_id:
                snapshots.append(
                    (
                        value.payload.get("revision", 1),
                        hashlib.sha256(value.payload["text"].encode()).hexdigest(),
                    )
                )
            return value

    def choose(provider, messages):
        if len(provider.calls) == 1:
            return {
                "tool": "read_transcript",
                "arguments": {"event_ids": [target.event_id, target.event_id]},
            }
        observations.append(observation(messages))
        return finish()

    provider = MeasuredFixture(choose)
    workspace = env.store.path.rsplit("/", 1)[0]
    runtime = AgentRuntime(
        RevisingJournal(env.store, "agent"), provider, workspace, ToolPolicy(workspace), max_steps=4
    )
    env.runtimes.append(runtime)
    invoke = runtime._read_transcript

    async def observed(rec, arguments):
        result = await invoke(rec, arguments)
        published.append(json.loads(result[0]))
        return result

    monkeypatch.setattr(runtime, "_read_transcript", observed)
    task = await runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Inspect two selected references to the same current view.",
            content_scope="public_synthetic",
            idempotency_key="legacy-duplicate-views",
        )
    )
    final = await runtime.wait(task.task_id, timeout=8)
    await runtime.close()
    if provider.failures:
        raise provider.failures[0]
    OBSERVATIONS.append(
        {
            "correction": correction,
            "initial_revision_views": [item[0] for item in snapshots[:2]],
            "initial_text_sha256": [item[1] for item in snapshots[:2]],
            "published_revisions": [entry["revision"] for page in published for entry in page],
            "published_text_sha256": [
                hashlib.sha256(entry["text"].encode()).hexdigest()
                for page in published
                for entry in page
            ],
            "actual_final_state": final.state,
            "actual_final_scope": final.content_scope,
            "measured_generation_calls": len(provider.calls),
            "observed_tool_ok": [item[0] for item in observations],
            "retained_context_roots": len(runtime._tasks[task.task_id].context_event_ids),
        }
    )
    assert [item[0] for item in snapshots[:2]] == ([1, 2] if correction else [1, 1])
    assert len(observations) == 1
    assert final.content_scope == "public_synthetic" and final.source_event_ids == []
    assert original_text not in json.dumps(provider.calls[0])
    assert corrected_text not in json.dumps(provider.calls[0])
    if correction:
        assert not published, "Inconsistent captured resolved views were published"
        assert final.state == "failed" and not observations[0][0]
        assert original_text not in json.dumps(provider.calls)
    else:
        assert final.state == "succeeded", final.error
        assert observations[0][0] and len(published) == 1
        expected = {
            "event_id": target.event_id,
            "kind": target.kind,
            "origin": target.origin,
            "sequence": target.sequence,
            "revision": 1,
            "text": original_text,
        }
        assert published[0] == [expected, expected]
        assert runtime._tasks[task.task_id].context_event_ids == {target.event_id}
