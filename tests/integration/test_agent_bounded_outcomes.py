"""Issue #487: real SQLite prefix recovery, public files and scripted inference."""

import asyncio
import json

import pytest

from core.policy import ToolPolicy
from packages.contracts.agent_outcome import (
    MAX_OUTCOME_BYTES,
    OutcomeEvidenceUnavailable,
    read_outcome_task,
)
from packages.contracts.agent_submit import AgentSubmitUnavailable
from packages.contracts.models import InterruptRequest, SessionRequest, TaskRequest, TaskState
from packages.providers.text import ScriptedProvider
from services.agent.runtime import AgentRuntime, TaskTerminal
from services.converse.runtime import ConverseRuntime
from services.decisions.runtime import OutcomeWorker
from services.session.store import LocalJournal, Store

SEARCHES = 12


def action(tool, **arguments):
    return json.dumps({"tool": tool, "arguments": arguments})


class Provider(ScriptedProvider):
    def __init__(self, final):
        super().__init__(
            [
                *[action("search", pattern="public", path="public.txt") for _ in range(SEARCHES)],
                RuntimeError("Public synthetic failure")
                if final == "failed"
                else action(
                    "finish", objective_revision=1, summary="Inspected public synthetic files"
                ),
            ]
        )
        self.final = final
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def complete(self, messages):
        if self.final == "cancelled" and len(self.calls) == SEARCHES:
            self.entered.set()
            await self.release.wait()
        return await super().complete(messages)


class FinalJournal:
    def __init__(self, delegate, mode):
        self.delegate, self.mode, self.attempts = delegate, mode, []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def append(self, body):
        if body.kind != "agent.outcome":
            return await self.delegate.append(body)
        self.attempts.append(body.model_copy(deep=True))
        if self.mode == "absent":
            raise OSError("Public synthetic unavailable journal")
        result = await self.delegate.append(body)
        if self.mode == "lost-ack":
            raise OSError("Public synthetic lost acknowledgement")
        return result


async def recover(delegate, workspace, task_id):
    provider = ScriptedProvider([])
    runtime = AgentRuntime(delegate, provider, workspace, ToolPolicy(workspace))
    try:
        await runtime.recover()
        task = await runtime.status(task_id)
        assert not provider.calls
        return task
    finally:
        await asyncio.wait_for(runtime.close(), 3)


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("mode", ["confirmed", "lost-ack", "absent"])
async def test_every_terminal_outcome_is_bounded_confirmed_and_restorable(tmp_path, state, mode):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    public_bytes = ("public synthetic line " + "x" * 240 + "\n") * 40
    (workspace / "public.txt").write_text(public_bytes)
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    delegate = LocalJournal(store, "agent")
    journal, provider = FinalJournal(delegate, mode), Provider(state)
    runtime = AgentRuntime(journal, provider, workspace, ToolPolicy(workspace), max_steps=16)
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Inspect public synthetic file matches",
                content_scope="public_synthetic",
                idempotency_key="public-bounded-outcome",
            )
        )
        if state == "cancelled":
            await asyncio.wait_for(provider.entered.wait(), 3)
            await asyncio.wait_for(
                runtime.interrupt(
                    task.task_id,
                    InterruptRequest(
                        expected_objective_revision=1,
                        idempotency_key="public-stop",
                    ),
                ),
                3,
            )
        final = await runtime.wait(task.task_id, timeout=5)
        expected = TaskState.PAUSED if mode == "absent" else TaskState(state)
        assert final.state == expected, final.error
        assert len(final.evidence) == SEARCHES
        assert (workspace / "public.txt").read_text() == public_bytes
        assert len(journal.attempts) == 1
        body = journal.attempts[0]
        assert len(body.model_dump_json().encode()) <= MAX_OUTCOME_BYTES
        assert body.payload["evidence"] == body.payload["task"]["evidence"] == []
        assert body.payload["evidence_ref"]["evidence_count"] == SEARCHES
        assert "public synthetic line" not in body.model_dump_json()
        tools = store.read(task_id=task.task_id, kinds=["agent.tool"])
        original = [row.payload["evidence"] for row in tools]
        assert [e.model_dump(mode="json") for e in final.evidence] == original
        outcomes = store.read(task_id=task.task_id, kinds=["agent.outcome"])
        assert len(outcomes) == (mode != "absent")
        restored = await recover(delegate, workspace, task.task_id)
        assert restored.state == expected
        assert [e.model_dump(mode="json") for e in restored.evidence] == original
        if mode == "absent":
            assert "unconfirmed" in final.error and final.summary == ""
            attempted = runtime._get(task.task_id).completion_snapshot
            assert attempted.state == TaskState(state)
            assert attempted.evidence == final.evidence
            if state == "failed":
                assert attempted.error == "provider error: Public synthetic failure"
            for operation in (
                runtime.interrupt(
                    task.task_id,
                    InterruptRequest(expected_objective_revision=1, idempotency_key="second-stop"),
                ),
            ):
                with pytest.raises(TaskTerminal, match="unconfirmed"):
                    await asyncio.wait_for(operation, 1)
        else:
            assert runtime._get(task.task_id).completion_snapshot is None
            hydrated = await read_outcome_task(delegate, outcomes[0])
            assert hydrated.evidence == final.evidence
            assert hydrated.state == TaskState(state)
        if state == "succeeded" and mode == "confirmed":
            converse_provider = ScriptedProvider([])
            converse = ConverseRuntime(LocalJournal(store, "converse"), converse_provider)
            try:
                notice = await converse.outcome_notice(outcomes[0])
                assert notice.payload["outcome_event_id"] == outcomes[0].event_id
                assert not converse_provider.calls
            finally:
                await asyncio.wait_for(converse.close(), 3)
        assert len(provider.calls) == SEARCHES + (state != "cancelled")
    finally:
        provider.release.set()
        await asyncio.wait_for(runtime.close(), 3)
        store.close()


async def test_sparse_recovery_and_notice_refuse_missing_authentic_receipt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "public.txt").write_text(("public " + "x" * 240 + "\n") * 40)
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    delegate, provider = LocalJournal(store, "agent"), Provider("succeeded")
    runtime = AgentRuntime(delegate, provider, workspace, ToolPolicy(workspace), max_steps=16)
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                objective="Inspect public files",
                content_scope="public_synthetic",
                idempotency_key="public",
            )
        )
        assert (await runtime.wait(task.task_id, timeout=5)).state == TaskState.SUCCEEDED
        missing = store.read(task_id=task.task_id, kinds=["agent.tool"])[4].event_id

        class Missing:
            def __getattr__(self, name):
                return getattr(delegate, name)

            async def read(self, **query):
                return [row for row in await delegate.read(**query) if row.event_id != missing]

        with pytest.raises(
            AgentSubmitUnavailable, match="Agent submission admission is unavailable"
        ):
            await recover(Missing(), workspace, task.task_id)
        converse = ConverseRuntime(Missing(), ScriptedProvider([]))
        try:
            outcome = store.read(task_id=task.task_id, kinds=["agent.outcome"])[0]
            with pytest.raises(OutcomeEvidenceUnavailable):
                await converse.outcome_notice(outcome)
            assert not store.read(kinds=["conversation.converse"])
        finally:
            await asyncio.wait_for(converse.close(), 3)
    finally:
        await asyncio.wait_for(runtime.close(), 3)
        store.close()


@pytest.mark.parametrize("state", ["succeeded", "paused"])
async def test_legacy_inline_snapshot_survives_unindexable_old_tool_row(tmp_path, state):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "public.txt").write_text("public synthetic bytes")
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    delegate = LocalJournal(store, "agent")
    provider = ScriptedProvider(
        [
            action("read_file", path="public.txt", max_bytes=100),
            action("finish", objective_revision=1, summary="Read public synthetic bytes"),
        ]
    )
    runtime = AgentRuntime(delegate, provider, workspace, ToolPolicy(workspace))
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                objective="Read public file",
                content_scope="public_synthetic",
                idempotency_key="legacy",
            )
        )
        final = await runtime.wait(task.task_id, timeout=3)
        assert final.state == TaskState.SUCCEEDED

        class OldSnapshot:
            def __getattr__(self, name):
                return getattr(delegate, name)

            async def read(self, **query):
                page = await delegate.read(**query)
                result = []
                for original in page:
                    row = original.model_copy(deep=True)
                    if row.kind == "agent.tool":
                        row.payload["evidence"]["kind"] = "old-unindexable-kind"
                    elif row.kind == "agent.outcome":
                        # Represent a legacy paused or terminal snapshot without
                        # changing the actual SQLite journal or any effect.
                        row.payload["state"] = row.payload["task"]["state"] = state
                    result.append(row)
                return result

        restored = await recover(OldSnapshot(), workspace, task.task_id)
        assert restored.state == TaskState(state)
        assert restored.evidence == final.evidence
        assert len(provider.calls) == 2
    finally:
        await asyncio.wait_for(runtime.close(), 3)
        store.close()


async def test_outcome_worker_holds_cursor_and_retries_only_unavailable_reads(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    journal = LocalJournal(store, "agent")
    from packages.contracts.models import AppendRequest

    entry = await journal.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=1,
            kind="agent.outcome",
            task_refs=["public-worker-task"],
            idempotency_key="public-worker",
            payload={"state": "succeeded"},
        )
    )
    entered, release, progressed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Consumer:
        calls = 0

        async def outcome_notice(self, outcome):
            self.calls += 1
            entered.set()
            if not release.is_set():
                raise OutcomeEvidenceUnavailable
            progressed.set()

    consumer = Consumer()
    worker = OutcomeWorker(journal, consumer)
    operation = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await journal.cursor("converse-outcomes-v1") == 0
        assert not operation.done() and not progressed.is_set()
        release.set()
        await asyncio.wait_for(progressed.wait(), 2)
        async with asyncio.timeout(1):
            while await journal.cursor("converse-outcomes-v1") != entry.sequence:
                await asyncio.sleep(0)
        assert consumer.calls == 2
    finally:
        operation.cancel()
        await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), 1)
        store.close()


async def test_converse_converts_only_early_read_timeout_to_unavailable(tmp_path, monkeypatch):
    from packages.contracts.models import AppendRequest
    from services.converse import runtime as converse_module

    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    journal = LocalJournal(store, "agent")
    outcome = await journal.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=1,
            kind="agent.outcome",
            task_refs=["public-task"],
            idempotency_key="read-timeout",
            payload={"state": "succeeded", "task": {"state": "succeeded"}, "evidence_ref": {}},
        )
    )

    async def timeout_read(*args):
        raise TimeoutError("Synthetic read-only timeout")

    monkeypatch.setattr(converse_module, "read_outcome_task", timeout_read)
    provider = ScriptedProvider([])
    converse = ConverseRuntime(LocalJournal(store, "converse"), provider)
    try:
        with pytest.raises(OutcomeEvidenceUnavailable):
            await converse.outcome_notice(outcome)
        assert not provider.calls and not store.read(kinds=["conversation.converse"])
    finally:
        await asyncio.wait_for(converse.close(), 3)
        store.close()


async def test_late_announcement_timeout_is_not_retried_or_acknowledged(tmp_path):
    from packages.contracts.models import AppendRequest

    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    journal = LocalJournal(store, "agent")
    await journal.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=1,
            kind="agent.outcome",
            task_refs=["public-task"],
            idempotency_key="late-timeout",
            payload={"state": "succeeded"},
        )
    )

    class LateFailure:
        calls = 0

        async def outcome_notice(self, outcome):
            self.calls += 1
            raise TimeoutError("Synthetic timeout after announcement began")

    consumer = LateFailure()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(OutcomeWorker(journal, consumer).run(), 1)
        assert consumer.calls == 1 and await journal.cursor("converse-outcomes-v1") == 0
    finally:
        store.close()
