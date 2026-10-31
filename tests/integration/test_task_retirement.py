"""Retirement repair for recovered paused work across a session reset; all synthetic."""

import asyncio
import hashlib
import json

import pytest

from core.policy import ToolPolicy
from packages.contracts.models import (
    InterruptRequest,
    ProviderInfo,
    SessionRequest,
    TaskRequest,
    TaskRetirement,
)
from packages.providers.completion_budget import PreparedComplete
from services.agent import AgentRuntime, IdemConflict
from services.session.store import JournalError, LocalJournal, Store


class HungFixture:
    info = ProviderInfo(provider="synthetic-fixture", model="hung", local=True)

    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()

    async def prepare_complete(self, messages):
        """Explicit local test double; no tokenizer or inference claim."""
        return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

    async def complete(self, messages):
        self.calls += 1
        self.entered.set()
        await asyncio.Event().wait()

    def stream(self, messages):
        raise NotImplementedError


class QuietFixture:
    info = ProviderInfo(provider="synthetic-fixture", model="quiet", local=True)

    def __init__(self):
        self.calls = 0

    async def prepare_complete(self, messages):
        """Explicit local test double; no tokenizer or inference claim."""
        return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

    async def complete(self, messages):
        self.calls += 1
        raise AssertionError("retirement must not invoke inference")

    def stream(self, messages):
        raise NotImplementedError


def stop(key="stop-1", revision=1, reason="owner stop"):
    return InterruptRequest(
        expected_objective_revision=revision, idempotency_key=key, reason=reason
    )


async def epoch_reset_recovered(tmp_path, provider=None):
    """Submit into epoch 1 with hung inference, close, reset, recover."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    hung = HungFixture()
    first = AgentRuntime(LocalJournal(store, "agent"), hung, workspace, ToolPolicy(workspace))
    task = await first.submit(
        TaskRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="SYNTHETIC_OLD_OBJECTIVE_MUST_NOT_ENTER_NEW_EPOCH",
            idempotency_key="task-1",
        )
    )
    await asyncio.wait_for(hung.entered.wait(), 5)
    await first.close()
    current = store.reset(session.session_id)
    provider = provider if provider is not None else QuietFixture()
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, workspace, ToolPolicy(workspace))
    await runtime.recover()
    return (store, session, current, runtime, task, provider)


async def test_reset_recover_stop_reopen_regression(tmp_path):
    store, old, current, runtime, task, provider = await epoch_reset_recovered(tmp_path)
    try:
        assert (await runtime.status(task.task_id)).state == "paused"
        retired = await runtime.interrupt(task.task_id, stop())
        assert retired.state == "cancelled" and retired.epoch == old.epoch
        assert "unreconciled" in retired.summary
        assert provider.calls == 0
        await runtime.close()
        reopened = AgentRuntime(
            LocalJournal(store, "agent"),
            QuietFixture(),
            runtime.workspace,
            ToolPolicy(runtime.workspace),
        )
        try:
            await reopened.recover()
            final = await reopened.status(task.task_id)
            assert final.state == "cancelled" and final.epoch == old.epoch
        finally:
            await reopened.close()
    finally:
        await runtime.close()
        store.close()


async def test_identity_unchanged_and_receipt_minimal(tmp_path):
    store, old, current, runtime, task, provider = await epoch_reset_recovered(tmp_path)
    try:
        retired = await runtime.interrupt(task.task_id, stop())
        assert (retired.session_id, retired.thread_id, retired.epoch) == (
            task.session_id,
            task.thread_id,
            task.epoch,
        )
        assert retired.objective == task.objective
        assert retired.objective_revision == task.objective_revision
        assert not retired.evidence
        entries = store.read(session_id=old.session_id, kinds=["agent.retirement"])
        assert len(entries) == 1
        entry = entries[0]
        assert entry.origin == "agent-supervisor" and entry.epoch == current.epoch
        assert entry.task_refs == [task.task_id] and list(entry.causes) == []
        payload = entry.payload
        assert set(payload) == set(TaskRetirement.model_fields)
        assert "objective" not in payload and "evidence" not in payload
        assert "reason" not in payload and payload["task_epoch"] == old.epoch
        assert payload["request_key_sha256"] == hashlib.sha256(b"stop-1").hexdigest()
        assert payload["disposition"] == "cancelled"
        assert payload["effect_status"] == "unreconciled"
        assert not store.read(session_id=old.session_id, kinds=["agent.outcome"])
        assert provider.calls == 0 and not (tmp_path / "workspace" / "new.txt").exists()
    finally:
        await runtime.close()
        store.close()


async def test_retry_after_append_failure_leaves_no_poison(tmp_path):
    store, old, current, runtime, task, _ = await epoch_reset_recovered(tmp_path)
    try:
        journal, attempts = runtime.journal, {"n": 0}
        real_append = journal.append

        async def fail_once(request):
            if request.kind == "agent.retirement" and attempts["n"] == 0:
                attempts["n"] += 1
                raise JournalError("synthetic storage failure")
            return await real_append(request)

        journal.append = fail_once
        with pytest.raises(JournalError):
            await runtime.interrupt(task.task_id, stop())
        assert (await runtime.status(task.task_id)).state == "paused"
        assert not store.read(session_id=old.session_id, kinds=["agent.retirement"])
        journal.append = real_append
        retired = await runtime.interrupt(task.task_id, stop())
        assert retired.state == "cancelled"
        assert len(store.read(session_id=old.session_id, kinds=["agent.retirement"])) == 1
    finally:
        journal.append = real_append
        await runtime.close()
        store.close()


async def test_lost_acknowledgment_settles_without_duplicate(tmp_path):
    store, old, current, runtime, task, _ = await epoch_reset_recovered(tmp_path)
    try:
        journal = runtime.journal
        real_append = journal.append
        lost = {"n": 0}

        async def commit_then_lose(request):
            entry = await real_append(request)
            if request.kind == "agent.retirement" and lost["n"] == 0:
                lost["n"] += 1
                raise JournalError("synthetic acknowledgment lost")
            return entry

        journal.append = commit_then_lose
        with pytest.raises(JournalError):
            await runtime.interrupt(task.task_id, stop())
        assert (await runtime.status(task.task_id)).state == "paused"
        journal.append = real_append
        retired = await runtime.interrupt(task.task_id, stop())
        assert retired.state == "cancelled"
        assert len(store.read(session_id=old.session_id, kinds=["agent.retirement"])) == 1
    finally:
        journal.append = real_append
        await runtime.close()
        store.close()


async def test_concurrent_stops_issue_one_receipt(tmp_path):
    store, old, current, runtime, task, _ = await epoch_reset_recovered(tmp_path)
    try:
        results = await asyncio.gather(*[runtime.interrupt(task.task_id, stop()) for _ in range(5)])
        assert all(r.state == "cancelled" for r in results)
        assert len(store.read(session_id=old.session_id, kinds=["agent.retirement"])) == 1
    finally:
        await runtime.close()
        store.close()


async def test_key_conflicts_survive_reopen_but_new_keys_are_harmless(tmp_path):
    store, old, current, runtime, task, _ = await epoch_reset_recovered(tmp_path)
    try:
        await runtime.interrupt(task.task_id, stop())
        await runtime.close()
        reopened = AgentRuntime(
            LocalJournal(store, "agent"),
            QuietFixture(),
            runtime.workspace,
            ToolPolicy(runtime.workspace),
        )
        try:
            await reopened.recover()
            with pytest.raises(IdemConflict):
                await reopened.interrupt(task.task_id, stop(reason="different reason"))
            harmless = await reopened.interrupt(task.task_id, stop(key="stop-2"))
            assert harmless.state == "cancelled"
            assert len(store.read(session_id=old.session_id, kinds=["agent.retirement"])) == 1
        finally:
            await reopened.close()
    finally:
        store.close()


async def test_decisions_and_residual_handles_cannot_retire(tmp_path):
    store, old, current, runtime, task, _ = await epoch_reset_recovered(tmp_path)
    try:
        from core.policy import PolicyDenied

        with pytest.raises(PolicyDenied):
            await runtime.interrupt(task.task_id, stop(), actor="decisions")
        assert (await runtime.status(task.task_id)).state == "paused"
        runtime._tasks[task.task_id].proc_slot["proc"] = object()
        with pytest.raises(ValueError, match="residual process"):
            await runtime.interrupt(task.task_id, stop())
        assert (await runtime.status(task.task_id)).state == "paused"
        assert not store.read(session_id=old.session_id, kinds=["agent.retirement"])
    finally:
        runtime._tasks[task.task_id].proc_slot.pop("proc", None)
        await runtime.close()
        store.close()


def test_receipt_payload_hashes_match_owner_request():
    digest = hashlib.sha256(
        json.dumps(
            {"expected_objective_revision": 1, "reason": "owner stop", "source_event_ids": []},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    receipt = TaskRetirement(
        task_id="t",
        task_session_id="s",
        task_thread_id="h",
        task_epoch=1,
        objective_revision=1,
        request_key_sha256=hashlib.sha256(b"stop-1").hexdigest(),
        request_digest=digest,
    )
    assert receipt.model_dump(mode="json")["request_digest"] == digest
