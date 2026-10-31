"""Issue #167: real SQLite completion truth, lost ACKs, and lifecycle races.

Only synthetic scripted inference is used. No effect or model call is retried
when the final journal write's outcome is uncertain.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.policy import ToolPolicy
from packages.contracts.models import (
    InterruptRequest,
    SessionRequest,
    SteerRequest,
    TaskRequest,
    TaskState,
)
from packages.providers.text import ScriptedProvider
from services.agent.runtime import AgentRuntime, IdemConflict, TaskTerminal
from services.session.store import LocalJournal, Store


def write(revision=1):
    return json.dumps(
        {
            "tool": "write_file",
            "arguments": {
                "path": "result.txt",
                "content": f"Synthetic revision {revision}",
                "objective_revision": revision,
            },
        }
    )


def finish(revision=1):
    return json.dumps(
        {
            "tool": "finish",
            "arguments": {
                "summary": "Verified synthetic file",
                "objective_revision": revision,
            },
        }
    )


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    delegate = LocalJournal(store, "agent")
    provider = ScriptedProvider([write(), finish()])
    runtime = AgentRuntime(
        delegate, provider, workspace, ToolPolicy(workspace, allow_write=True, yolo=True)
    )
    result = SimpleNamespace(
        workspace=workspace,
        store=store,
        session=session,
        delegate=delegate,
        provider=provider,
        runtime=runtime,
    )
    yield result
    await runtime.close()
    store.close()


def request(env):
    return TaskRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        objective="Write a synthetic file",
        idempotency_key="synthetic-completion",
    )


class CompletionJournal:
    def __init__(self, delegate, *, commit=False, hold=False, fail=True, read_failure=False):
        self.delegate = delegate
        self.commit = commit
        self.hold = hold
        self.fail = fail
        self.read_failure = read_failure
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.attempts = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def append(self, body):
        if body.kind != "agent.outcome" or body.payload.get("state") != "succeeded":
            return await self.delegate.append(body)
        self.attempts.append(body.model_copy(deep=True))
        entry = await self.delegate.append(body) if self.commit else None
        self.entered.set()
        if self.hold:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("synthetic-secret-must-not-escape")
        return entry if entry is not None else await self.delegate.append(body)

    async def read(self, **query):
        if self.read_failure:
            raise RuntimeError("synthetic-readback-secret")
        return await self.delegate.read(**query)


async def recovered(env, task_id):
    provider = ScriptedProvider([])
    runtime = AgentRuntime(env.delegate, provider, env.workspace, ToolPolicy(env.workspace))
    try:
        await runtime.recover()
        task = await runtime.status(task_id)
        assert provider.calls == []
        return task
    finally:
        await runtime.close()


@pytest.mark.parametrize("commit", [False, True])
async def test_final_append_failure_uses_durable_truth_without_repeating_work(env, commit):
    journal = CompletionJournal(env.delegate, commit=commit)
    env.runtime.journal = journal
    task = await env.runtime.submit(request(env))
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == (TaskState.SUCCEEDED if commit else TaskState.PAUSED)
    assert len(journal.attempts) == 1
    assert len(env.provider.calls) == 2
    assert (env.workspace / "result.txt").read_text() == "Synthetic revision 1"
    outcomes = env.store.read(task_id=task.task_id, kinds=["agent.outcome"])
    assert len(outcomes) == int(commit)
    if not commit:
        assert final.summary == ""
        assert "unconfirmed" in final.error and "without retry" in final.error
        assert "secret" not in final.error
    assert (await recovered(env, task.task_id)).state == final.state


async def test_lost_readback_stays_unconfirmed_until_restart(env):
    journal = CompletionJournal(env.delegate, commit=True, read_failure=True)
    env.runtime.journal = journal
    task = await env.runtime.submit(request(env))
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.PAUSED
    assert final.summary == "" and "secret" not in final.error
    for operation in (
        env.runtime.interrupt(
            task.task_id,
            InterruptRequest(expected_objective_revision=1, idempotency_key="uncertain-stop"),
        ),
        env.runtime.steer(
            task.task_id,
            SteerRequest(
                instruction="Different task",
                expected_objective_revision=1,
                idempotency_key="uncertain-steer",
            ),
        ),
    ):
        with pytest.raises(TaskTerminal, match="unconfirmed"):
            await asyncio.wait_for(operation, timeout=0.2)
    assert (await recovered(env, task.task_id)).state == TaskState.SUCCEEDED
    assert len(env.store.read(task_id=task.task_id, kinds=["agent.outcome"])) == 1
    assert len(journal.attempts) == 1 and len(env.provider.calls) == 2


@pytest.mark.parametrize("commit", [False, True])
async def test_final_append_fence_prevents_false_success_and_conflicting_controls(env, commit):
    journal = CompletionJournal(env.delegate, commit=commit, hold=True, fail=False)
    env.runtime.journal = journal
    task = await env.runtime.submit(request(env))
    await asyncio.wait_for(journal.entered.wait(), timeout=3)
    try:
        visible = await env.runtime.status(task.task_id)
        assert visible.state == TaskState.VERIFYING
        assert visible.summary == ""
        for operation in (
            env.runtime.interrupt(
                task.task_id,
                InterruptRequest(expected_objective_revision=1, idempotency_key="during-stop"),
            ),
            env.runtime.steer(
                task.task_id,
                SteerRequest(
                    instruction="Change synthetic file",
                    expected_objective_revision=1,
                    idempotency_key="during-steer",
                ),
            ),
        ):
            with pytest.raises(TaskTerminal, match="confirmation"):
                await asyncio.wait_for(operation, timeout=0.2)
        assert not env.runtime._get(task.task_id).cancel_requested
        assert env.runtime._get(task.task_id).pending_steer is None
    finally:
        journal.release.set()
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.SUCCEEDED
    outcomes = env.store.read(task_id=task.task_id, kinds=["agent.outcome"])
    assert len(outcomes) == 1 and outcomes[0].payload["task"]["state"] == "succeeded"
    assert len(journal.attempts) == 1


@pytest.mark.parametrize("commit", [False, True])
async def test_shutdown_during_completion_does_not_append_contradictory_outcome(env, commit):
    journal = CompletionJournal(env.delegate, commit=commit, hold=True)
    env.runtime.journal = journal
    task = await env.runtime.submit(request(env))
    await asyncio.wait_for(journal.entered.wait(), timeout=3)
    await env.runtime.close()
    final = await env.runtime.wait(task.task_id, timeout=0.2)
    assert final.state == TaskState.PAUSED and "unconfirmed" in final.error
    assert len(env.store.read(task_id=task.task_id, kinds=["agent.outcome"])) == int(commit)
    assert (await recovered(env, task.task_id)).state == (
        TaskState.SUCCEEDED if commit else TaskState.PAUSED
    )
    assert len(env.provider.calls) == 2


async def test_empty_readback_does_not_authorize_cancel_before_delayed_commit(env):
    gate = asyncio.Event()
    late = []

    class DelayedCommit(CompletionJournal):
        async def append(self, body):
            if body.kind == "agent.outcome" and body.payload.get("state") == "succeeded":
                self.attempts.append(body.model_copy(deep=True))

                async def commit_later():
                    await gate.wait()
                    await self.delegate.append(body)

                late.append(asyncio.create_task(commit_later()))
                raise RuntimeError("synthetic lost connection")
            return await self.delegate.append(body)

    journal = DelayedCommit(env.delegate)
    env.runtime.journal = journal
    task = await env.runtime.submit(request(env))
    try:
        assert (await env.runtime.wait(task.task_id, timeout=3)).state == TaskState.PAUSED
        assert env.store.read(task_id=task.task_id, kinds=["agent.outcome"]) == []
        with pytest.raises(TaskTerminal):
            await env.runtime.interrupt(
                task.task_id,
                InterruptRequest(expected_objective_revision=1, idempotency_key="late-stop"),
            )
    finally:
        gate.set()
        await asyncio.gather(*late)
    assert (await recovered(env, task.task_id)).state == TaskState.SUCCEEDED
    assert len(env.store.read(task_id=task.task_id, kinds=["agent.outcome"])) == 1
    assert len(journal.attempts) == 1


@pytest.mark.parametrize("invalidate_old_evidence", [False, True])
async def test_steer_accepted_before_completion_fence_replans_instead_of_finishing(
    env, monkeypatch, invalidate_old_evidence
):
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.runtime._verify_finish_files
    calls = 0

    async def held_verification(rec):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(rec)

    monkeypatch.setattr(env.runtime, "_verify_finish_files", held_verification)
    journal = CompletionJournal(env.delegate, hold=True, fail=False)
    env.runtime.journal = journal
    env.provider.responses.extend([write(2), finish(2)])
    task = await env.runtime.submit(request(env))
    await asyncio.wait_for(entered.wait(), timeout=3)
    steer = SteerRequest(
        instruction="Use synthetic revision 2",
        expected_objective_revision=1,
        idempotency_key="before-fence",
    )
    await env.runtime.steer(task.task_id, steer)
    if invalidate_old_evidence:
        (env.workspace / "result.txt").unlink()
    release.set()
    await asyncio.wait_for(journal.entered.wait(), timeout=3)
    try:
        replay = await env.runtime.steer(task.task_id, steer)
        assert replay.state == TaskState.VERIFYING and replay.objective_revision == 2
        with pytest.raises(IdemConflict):
            await env.runtime.steer(
                task.task_id, steer.model_copy(update={"instruction": "Conflicting replay"})
            )
        assert len(env.store.read(task_id=task.task_id, kinds=["agent.command"])) == 3
    finally:
        journal.release.set()
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.SUCCEEDED and final.objective_revision == 2
    assert (env.workspace / "result.txt").read_text() == "Synthetic revision 2"
    outcomes = env.store.read(task_id=task.task_id, kinds=["agent.outcome"])
    assert len(outcomes) == 1 and outcomes[0].payload["objective_revision"] == 2
