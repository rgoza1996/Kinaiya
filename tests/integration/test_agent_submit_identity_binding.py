"""Native SQLite receipt identity must survive runtime task-ID collisions.

Task IDs are safe strings on the native wire, not a global uniqueness claim.
These tests use real session-scoped native slots and synthetic providers only.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_agent_submit_admission import proposal

from core.policy import ToolPolicy
from packages.contracts.agent_submit import AgentSubmitUnavailable
from packages.contracts.models import ProviderInfo, SessionRequest, Task, TaskRequest
from services.agent import AgentRuntime
from services.session.store import LocalJournal, Store

SHARED_TASK_ID = "shared-safe-native-task-id"


class NoInference:
    info = ProviderInfo(provider="synthetic", model="no-inference", local=True)

    async def prepare_complete(self, messages):
        raise AssertionError("Identity reconciliation must not invoke inference")

    async def complete(self, messages):
        raise AssertionError("Identity reconciliation must not invoke inference")


class AdmissionGate(LocalJournal):
    def __init__(self, store):
        super().__init__(store, "agent")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def admit_submit(self, request):
        self.entered.set()
        await self.release.wait()
        return await super().admit_submit(request)


def task_request(body):
    return TaskRequest(
        session_id=body.session_id,
        thread_id=body.thread_id,
        epoch=body.epoch,
        objective=body.payload["objective"],
        content_scope=body.payload["requested_content_scope"],
        idempotency_key=body.payload["idempotency_key"],
        source_event_ids=list(body.payload["source_event_ids"]),
        source_revision=body.payload["source_revision"],
    )


@asynccontextmanager
async def native_pair(tmp_path, *, shared=True, seed_second=True, gate=False):
    store = Store(tmp_path / "identity.sqlite")
    sessions = [store.create_session(SessionRequest(channel="agent")) for _ in range(2)]
    bodies = [
        proposal(
            session,
            task_id=SHARED_TASK_ID if shared else f"native-task-{index}",
            key=f"native-logical-{index}",
            label=str(index),
            objective=f"Synthetic native work for session {index}",
        )
        for index, session in enumerate(sessions)
    ]
    entries = []
    for body in bodies if seed_second else bodies[:1]:
        created = store.admit_submit(body, "agent")
        assert created.disposition == "created"
        retained = store.admit_submit(body, "agent")
        assert retained.disposition == "retained" and retained.entry == created.entry
        entries.append(created.entry)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    journal = AdmissionGate(store) if gate else LocalJournal(store, "agent")
    runtime = AgentRuntime(journal, NoInference(), workspace, ToolPolicy(workspace))
    try:
        yield SimpleNamespace(
            store=store,
            runtime=runtime,
            journal=journal,
            bodies=bodies,
            requests=[task_request(body) for body in bodies],
            entries=entries,
        )
    finally:
        await runtime.close()
        store.close()


async def test_retained_receipt_never_substitutes_another_sessions_confirmed_task(tmp_path):
    async with native_pair(tmp_path) as env:
        first = await env.runtime.submit(env.requests[0])
        record = env.runtime._tasks[first.task_id]
        confirmed = dict(env.runtime._submit_idem)
        assert first.session_id == env.requests[0].session_id
        with pytest.raises(AgentSubmitUnavailable):
            reply = await env.runtime.submit(env.requests[1])
            assert (reply.session_id, reply.idempotency_key) == (
                env.requests[1].session_id,
                env.requests[1].idempotency_key,
            ), "A retained receipt returned another session's Task"
        assert env.runtime._tasks[first.task_id] is record
        assert env.runtime._submit_idem == confirmed
        assert (await env.runtime.status(first.task_id)) == first
        assert record.worker is None


async def test_cold_recovery_refuses_cross_session_task_id_ambiguity_without_ram_receipts(tmp_path):
    async with native_pair(tmp_path) as env:
        with pytest.raises(AgentSubmitUnavailable):
            recovered = await env.runtime.recover()
            assert len(recovered) == 2, "Cold recovery silently discarded a valid native session"
        assert await env.runtime.status() == []
        assert env.runtime._submit_idem == {}


async def test_fresh_task_id_collision_cannot_replace_a_confirmed_record(tmp_path, monkeypatch):
    async with native_pair(tmp_path, seed_second=False, gate=True) as env:
        first = await env.runtime.submit(env.requests[0])
        record = env.runtime._tasks[first.task_id]
        confirmed = dict(env.runtime._submit_idem)

        class CollidingTask(Task):
            # Keep Pydantic construction and isinstance semantics while fixing
            # the generated ID. Explicit native hydration IDs still take precedence.
            task_id: str = SHARED_TASK_ID

        monkeypatch.setattr("services.agent.runtime.Task", CollidingTask)
        operation = asyncio.create_task(env.runtime.submit(env.requests[1]))
        entered = asyncio.create_task(env.journal.entered.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, entered}, timeout=1, return_when=asyncio.FIRST_COMPLETED
            )
            assert env.runtime._tasks[first.task_id] is record, (
                "A colliding provisional record overwrote an existing confirmed Task"
            )
            assert operation in done, "A colliding provisional record reached native admission"
            with pytest.raises(AgentSubmitUnavailable):
                operation.result()
            assert env.runtime._tasks[first.task_id] is record
            assert env.runtime._submit_idem == confirmed
            assert record.worker is None
            assert not env.journal.entered.is_set()
            assert len(env.store.read(kinds=["agent.command"])) == 1
        finally:
            operation.cancel()
            entered.cancel()
            await asyncio.gather(operation, entered, return_exceptions=True)


async def test_distinct_native_task_ids_keep_both_canonical_receipts_without_workers(tmp_path):
    async with native_pair(tmp_path, shared=False) as env:
        recovered = await env.runtime.recover()
        assert len(recovered) == 2
        by_id = {task.task_id: task for task in recovered}
        for body, request, entry in zip(env.bodies, env.requests, env.entries, strict=True):
            replay = await env.runtime.submit(request)
            assert replay == by_id[entry.payload["task_id"]]
            assert (replay.session_id, replay.thread_id, replay.epoch, replay.idempotency_key) == (
                body.session_id,
                body.thread_id,
                body.epoch,
                body.payload["idempotency_key"],
            )
            assert replay.state == "paused"
            assert replay.model_pin.model_dump(mode="json") == entry.payload["model_pin"]
            assert env.runtime._tasks[replay.task_id].worker is None
