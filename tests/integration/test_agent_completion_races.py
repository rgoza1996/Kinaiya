"""Issue #167: deterministic control/shutdown races against real SQLite truth.

The gates delay actual LocalJournal responses, never replace authentication or
durable outcomes. All inference is a synthetic two-response ScriptedProvider.
"""

import asyncio
import json
from types import SimpleNamespace

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
from packages.providers.text import ScriptedProvider
from services.agent.runtime import AgentRuntime, TaskTerminal
from services.session.store import LocalJournal, Store


class Gate:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.defer_cancellation = False

    async def hold(self):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if not self.defer_cancellation:
                raise
            # Exercise the authority recheck even if verification finishes
            # after its caller has already requested cancellation.
            await self.release.wait()


class GatedJournal:
    def __init__(self, delegate):
        self.delegate = delegate
        self.validation = Gate()
        self.final_append = Gate()
        self.readback = Gate()
        self.control_task = None
        self.validation_method = None
        self.hold_final_append = True
        self.lose_final_ack = False
        self.hold_readback = False
        self.final_attempts = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def _validation_response(self, method, result):
        if method == self.validation_method and asyncio.current_task() is self.control_task:
            await self.validation.hold()
        return result

    async def resolve(self, event_id):
        result = await self.delegate.resolve(event_id)
        return await self._validation_response("resolve", result)

    async def session(self, session_id):
        result = await self.delegate.session(session_id)
        return await self._validation_response("session", result)

    async def append(self, body):
        if body.kind != "agent.outcome" or body.payload.get("state") != "succeeded":
            return await self.delegate.append(body)
        self.final_attempts.append(body.model_copy(deep=True))
        if self.hold_final_append:
            await self.final_append.hold()
        result = await self.delegate.append(body)
        if self.lose_final_ack:
            raise OSError("synthetic lost final acknowledgement")
        return result

    async def read(self, **query):
        result = await self.delegate.read(**query)
        if self.hold_readback and self.final_attempts:
            await self.readback.hold()
        return result


@pytest.fixture
async def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    delegate = LocalJournal(store, "agent")
    journal = GatedJournal(delegate)
    provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "tool": "write_file",
                    "arguments": {
                        "path": "result.txt",
                        "content": "Synthetic verified result",
                        "objective_revision": 1,
                    },
                }
            ),
            json.dumps(
                {
                    "tool": "finish",
                    "arguments": {
                        "summary": "Verified synthetic file",
                        "objective_revision": 1,
                    },
                }
            ),
        ]
    )
    runtime = AgentRuntime(
        journal, provider, workspace, ToolPolicy(workspace, allow_write=True, yolo=True)
    )
    verification = Gate()
    verify = runtime._verify_finish_files

    async def held_verification(rec):
        await verification.hold()
        return await verify(rec)

    monkeypatch.setattr(runtime, "_verify_finish_files", held_verification)
    result = SimpleNamespace(
        workspace=workspace,
        store=store,
        session=session,
        delegate=delegate,
        journal=journal,
        provider=provider,
        runtime=runtime,
        verification=verification,
    )
    try:
        yield result
    finally:
        for gate in (verification, journal.validation, journal.final_append, journal.readback):
            gate.release.set()
        if journal.control_task is not None:
            journal.control_task.cancel()
            await asyncio.gather(journal.control_task, return_exceptions=True)
        await asyncio.wait_for(runtime.close(), timeout=3)
        store.close()


async def start_verifying(env, *, source_ids=()):
    task = await env.runtime.submit(
        TaskRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            objective="Write a synthetic file",
            content_scope="public_synthetic",
            source_event_ids=list(source_ids),
            idempotency_key="synthetic-race-task",
        )
    )
    await asyncio.wait_for(env.verification.entered.wait(), timeout=3)
    assert not env.runtime._get(task.task_id).completion_pending
    assert len(env.provider.calls) == 2
    assert (env.workspace / "result.txt").read_text() == "Synthetic verified result"
    return task


async def authenticated_source(env, text, *, content_scope="local_only"):
    source = await LocalJournal(env.store, "owner").append(
        AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.user",
            payload={
                "text": text,
                "final": True,
                "revision": 1,
                "content_scope": content_scope,
            },
            idempotency_key="synthetic-race-source",
        )
    )
    assert source.origin == "authenticated-user"
    return source


def command_state(rec):
    return (
        rec.pending_steer,
        dict(rec.steer_idem),
        dict(rec.interrupt_idem),
        rec.cancel_requested,
        rec.task.content_scope,
        list(rec.task.source_event_ids),
        rec.task.objective,
        rec.task.objective_revision,
        rec.journal_seq,
    )


async def resume_control_after_fence(env, task, *, confirmed=False, interrupt=False):
    journal = env.journal
    await asyncio.wait_for(journal.validation.entered.wait(), timeout=3)
    env.verification.release.set()
    await asyncio.wait_for(journal.final_append.entered.wait(), timeout=3)
    rec = env.runtime._get(task.task_id)
    assert rec.completion_pending
    assert rec.task.state == TaskState.VERIFYING
    assert rec.task.summary == ""
    if confirmed:
        journal.final_append.release.set()
        final = await env.runtime.wait(task.task_id, timeout=3)
        assert final.state == TaskState.SUCCEEDED
        assert not rec.completion_pending
    before = command_state(rec)
    before_task = rec.task.model_copy(deep=True)
    entries = env.store.read(task_id=task.task_id)
    journal.validation.release.set()
    # Validation cannot admit a mutation after either a pending completion or
    # its confirmed ACK. Interrupt retains its native terminal read behavior.
    if confirmed and interrupt:
        response = await asyncio.wait_for(journal.control_task, timeout=0.5)
        assert response == before_task
    else:
        with pytest.raises(TaskTerminal, match=None if confirmed else "confirmation"):
            await asyncio.wait_for(journal.control_task, timeout=0.5)
    assert journal.final_append.release.is_set() == confirmed
    assert command_state(rec) == before
    assert rec.task == before_task
    assert rec.pending_steer is None
    assert rec.steer_idem == rec.interrupt_idem == {}
    assert not rec.cancel_requested
    assert rec.task.content_scope == "public_synthetic"
    assert env.store.read(task_id=task.task_id) == entries
    journal.final_append.release.set()
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.SUCCEEDED
    assert final.objective_revision == 1
    assert final.content_scope == "public_synthetic"
    assert len(journal.final_attempts) == 1
    assert len(env.provider.calls) == 2
    outcomes = env.store.read(task_id=task.task_id, kinds=["agent.outcome"])
    assert len(outcomes) == 1
    assert outcomes[0].payload["state"] == "succeeded"
    assert outcomes[0].payload["task"]["state"] == "succeeded"


@pytest.mark.parametrize("completion_state", ["pending", "confirmed"])
@pytest.mark.parametrize("validation_method", ["resolve", "session"])
async def test_steer_started_before_fence_cannot_mutate_after_validation(
    env, validation_method, completion_state
):
    source = await authenticated_source(env, "Use a different synthetic result")
    task = await start_verifying(env)
    env.journal.validation_method = validation_method
    env.journal.control_task = asyncio.create_task(
        env.runtime.steer(
            task.task_id,
            SteerRequest(
                instruction=source.payload["text"],
                source_event_ids=[source.event_id],
                content_scope="local_only",
                expected_objective_revision=1,
                idempotency_key="synthetic-racing-steer",
            ),
        )
    )
    await resume_control_after_fence(env, task, confirmed=completion_state == "confirmed")


@pytest.mark.parametrize("completion_state", ["pending", "confirmed"])
@pytest.mark.parametrize("qualification_failure", [False, True])
async def test_steer_task_scope_read_cannot_narrow_after_completion_fence(
    env, monkeypatch, qualification_failure, completion_state
):
    source = await authenticated_source(
        env, "Write a synthetic file", content_scope="public_synthetic"
    )
    task = await start_verifying(env, source_ids=[source.event_id])
    qualify = env.runtime._source_scope
    qualifications = []

    async def held_qualification(source_id, resolved):
        scope = await qualify(source_id, resolved)
        if asyncio.current_task() is env.journal.control_task:
            assert source_id == source.event_id and scope == "public_synthetic"
            qualifications.append(source_id)
            await env.journal.validation.hold()
            if qualification_failure:
                raise OSError("synthetic qualification read unavailable")
            # A delayed qualification result narrows the floor. It must not
            # mutate the frozen task or append disclosure-narrowed after the
            # completion fence, even though this steer started before it.
            return "local_only"
        return scope

    monkeypatch.setattr(env.runtime, "_source_scope", held_qualification)
    env.journal.control_task = asyncio.create_task(
        env.runtime.steer(
            task.task_id,
            SteerRequest(
                instruction="Apply a synthetic constraint",
                content_scope="public_synthetic",
                expected_objective_revision=1,
                idempotency_key="synthetic-qualification-steer",
            ),
        )
    )
    await resume_control_after_fence(env, task, confirmed=completion_state == "confirmed")
    assert qualifications == [source.event_id]


@pytest.mark.parametrize("completion_state", ["pending", "confirmed"])
async def test_decisions_interrupt_validating_before_fence_cannot_cancel_afterward(
    env, completion_state
):
    source = await authenticated_source(env, "Stop this synthetic task")
    task = await start_verifying(env)
    env.journal.validation_method = "resolve"
    env.journal.control_task = asyncio.create_task(
        env.runtime.interrupt(
            task.task_id,
            InterruptRequest(
                source_event_ids=[source.event_id],
                source_revision=1,
                expected_objective_revision=1,
                idempotency_key="synthetic-racing-decisions-stop",
            ),
            actor="decisions",
        )
    )
    await resume_control_after_fence(
        env, task, confirmed=completion_state == "confirmed", interrupt=True
    )


async def test_owner_interrupt_admitted_during_verification_cancels_without_success(env):
    env.verification.defer_cancellation = True
    task = await start_verifying(env)
    await asyncio.wait_for(
        env.runtime.interrupt(
            task.task_id,
            InterruptRequest(
                expected_objective_revision=1,
                idempotency_key="synthetic-before-fence-stop",
            ),
        ),
        timeout=0.5,
    )
    await asyncio.wait_for(env.verification.cancelled.wait(), timeout=3)
    rec = env.runtime._get(task.task_id)
    assert rec.cancel_requested and not rec.completion_pending
    assert "synthetic-before-fence-stop" in rec.interrupt_idem
    env.verification.release.set()
    final = await env.runtime.wait(task.task_id, timeout=3)
    assert final.state == TaskState.CANCELLED
    assert final.summary == ""
    assert env.journal.final_attempts == []
    outcomes = env.store.read(task_id=task.task_id, kinds=["agent.outcome"])
    assert len(outcomes) == 1
    assert outcomes[0].payload["state"] == "cancelled"
    assert outcomes[0].payload["task"]["state"] == "cancelled"
    assert len(env.provider.calls) == 2


async def test_shutdown_during_lost_ack_readback_pauses_then_recovers_durable_success(env):
    env.journal.hold_final_append = False
    env.journal.lose_final_ack = True
    env.journal.hold_readback = True
    task = await start_verifying(env)
    env.verification.release.set()
    await asyncio.wait_for(env.journal.readback.entered.wait(), timeout=3)
    outcomes = env.store.read(task_id=task.task_id, kinds=["agent.outcome"])
    assert len(outcomes) == 1 and outcomes[0].payload["state"] == "succeeded"
    assert (await env.runtime.status(task.task_id)).state == TaskState.VERIFYING
    await asyncio.wait_for(env.runtime.close(), timeout=3)
    assert env.journal.readback.cancelled.is_set()
    paused = await env.runtime.wait(task.task_id, timeout=0.5)
    assert paused.state == TaskState.PAUSED
    assert paused.summary == "" and "unconfirmed" in paused.error
    assert env.runtime._get(task.task_id).completion_pending
    assert env.store.read(task_id=task.task_id, kinds=["agent.outcome"]) == outcomes
    assert len(env.journal.final_attempts) == 1
    assert len(env.provider.calls) == 2

    provider = ScriptedProvider([])
    recovered = AgentRuntime(env.delegate, provider, env.workspace, ToolPolicy(env.workspace))
    try:
        await asyncio.wait_for(recovered.recover(), timeout=3)
        restored = await recovered.status(task.task_id)
        assert restored.state == TaskState.SUCCEEDED
        assert restored.summary == "Verified synthetic file"
        assert restored.evidence == paused.evidence
        assert provider.calls == []
        assert env.store.read(task_id=task.task_id, kinds=["agent.outcome"]) == outcomes
    finally:
        await asyncio.wait_for(recovered.close(), timeout=3)
