"""#199: native submit acceptance must precede receipts and worker ownership.

The journal and its SQLite transactions are real. Gates interrupt only the
submit transport boundary; the worker is an explicit no-inference observer.
"""

import asyncio
from types import SimpleNamespace

import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.models import AppendRequest, SessionRequest, Task, TaskRequest, TaskState
from packages.providers.text import ScriptedProvider
from services.agent.model_settings import AgentModelSettings
from services.agent.runtime import AgentRuntime, IdemConflict
from services.session.store import LocalJournal, Store

OBJECTIVE = "Observe the public synthetic submit receipt without doing work."
ACTIVE = {
    TaskState.ACCEPTED,
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.VERIFYING,
    TaskState.AWAITING_APPROVAL,
}


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
            # A remote/native operation may finish after its caller cancels.
            await self.release.wait()


class SubmitJournal:
    """The same body gates work with legacy append and native admission.

    admit_submit is forwarded lazily so baseline failures exercise the actual
    existing API rather than a missing future contract import or method.
    """

    def __init__(self, delegate):
        self.delegate = delegate
        self.before = Gate()
        self.after = Gate()
        self.hold_before = False
        self.hold_after = False
        self.failure = None
        self.bad_ack = None
        self.attempts = []
        self.committed = asyncio.Event()
        self.finished = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def append(self, body):
        if body.kind != "agent.command" or body.payload.get("op") != "submit":
            return await self.delegate.append(body)
        return await self._submit("append", body)

    async def admit_submit(self, body):
        return await self._submit("admit_submit", body)

    async def _submit(self, method, body):
        self.attempts.append(body.model_copy(deep=True))
        try:
            if self.hold_before:
                await self.before.hold()
            if self.failure == "before_commit":
                raise OSError("Synthetic submit failed before commit")
            result = await getattr(self.delegate, method)(body)
            self.committed.set()
            if self.hold_after:
                await self.after.hold()
            if self.failure == "lost_ack":
                raise OSError("Synthetic submit acknowledgement unavailable")
            if self.bad_ack is not None:
                # Corrupt only the returned receipt, never durable SQLite truth.
                entry = getattr(result, "entry", result)
                if self.bad_ack == "origin":
                    entry = entry.model_copy(update={"origin": "untrusted"}, deep=True)
                elif self.bad_ack == "key":
                    entry = entry.model_copy(update={"idempotency_key": "other-key"}, deep=True)
                elif self.bad_ack == "objective":
                    payload = {**entry.payload, "objective": "A different synthetic objective"}
                    entry = entry.model_copy(update={"payload": payload}, deep=True)
                elif self.bad_ack == "task_refs":
                    entry = entry.model_copy(update={"task_refs": []}, deep=True)
                else:
                    raise AssertionError("Unknown synthetic receipt mutation")
                return (
                    result.model_copy(update={"entry": entry}, deep=True)
                    if hasattr(result, "entry")
                    else entry
                )
            return result
        finally:
            self.finished.set()


async def outcome(awaitable):
    try:
        return await awaitable
    except (OSError, RuntimeError, ValueError, asyncio.CancelledError) as exc:
        return exc


async def settled(operation):
    _done, pending = await asyncio.wait({operation}, timeout=2)
    assert not pending, "Synthetic submit did not settle after its gate was released"
    return operation.result()


async def checkpoint():
    # Let already-scheduled workers run without a timing assumption or sleep.
    ready = asyncio.Event()
    asyncio.get_running_loop().call_soon(ready.set)
    await ready.wait()


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    source = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={
                "text": OBJECTIVE,
                "final": True,
                "revision": 1,
                "content_scope": "public_synthetic",
            },
            idempotency_key="synthetic-source",
        ),
        "owner",
    )
    journal = SubmitJournal(LocalJournal(store, "agent"))
    provider = ScriptedProvider([])
    runtime = AgentRuntime(journal, provider, workspace, ToolPolicy(workspace))
    runtime.model_settings = AgentModelSettings(provider, {}, path=tmp_path / "models.sqlite")
    worker_started = asyncio.Event()
    worker_release = asyncio.Event()
    worker_calls = []

    async def observed_worker(rec):
        worker_calls.append(rec.task.task_id)
        worker_started.set()
        await worker_release.wait()

    runtime._run = observed_worker
    result = SimpleNamespace(
        store=store,
        session=session,
        source=source,
        journal=journal,
        provider=provider,
        runtime=runtime,
        worker_started=worker_started,
        worker_release=worker_release,
        worker_calls=worker_calls,
        operations=[],
    )
    try:
        yield result
    finally:
        journal.before.release.set()
        journal.after.release.set()
        worker_release.set()
        for operation in result.operations:
            if not operation.done():
                operation.cancel()
        if result.operations:
            _done, pending = await asyncio.wait(result.operations, timeout=2)
            assert not pending, "Synthetic submit tasks were not reaped"
        if not runtime._closed:
            await asyncio.wait_for(runtime.close(), timeout=3)
        store.close()


def request(env, *, sources=True):
    return TaskRequest(
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        objective=OBJECTIVE,
        content_scope="local_only",
        source_event_ids=[env.source.event_id] if sources else [],
        idempotency_key="synthetic-uncertain-submit",
    )


def start(env, body):
    operation = asyncio.create_task(outcome(env.runtime.submit(body)))
    env.operations.append(operation)
    return operation


def submits(env):
    return [
        entry
        for entry in env.store.read(kinds=["agent.command"])
        if entry.payload.get("op") == "submit"
    ]


def assert_no_worker(env):
    assert env.worker_calls == [], "Unconfirmed or retired submission launched a worker"
    assert all(rec.worker is None for rec in env.runtime._tasks.values())
    assert env.provider.calls == []


async def assert_no_confirmed_status(env):
    visible = await env.runtime.status()
    assert all(task.state not in ACTIVE for task in visible), (
        "An unconfirmed native submission is exposed as accepted/active"
    )


def assert_not_active(result):
    assert not isinstance(result, Task) or result.state not in ACTIVE


async def test_before_commit_failure_retry_stays_unconfirmed(env):
    env.journal.failure = "before_commit"
    body = request(env)
    first = await outcome(env.runtime.submit(body))
    assert isinstance(first, (OSError, RuntimeError, ValueError))
    assert submits(env) == []
    retry = await outcome(env.runtime.submit(body))
    assert_not_active(retry)
    await assert_no_confirmed_status(env)
    assert len(env.journal.attempts) == 1
    assert_no_worker(env)


async def test_commit_before_lost_ack_reconciles_to_paused_without_worker(env):
    env.journal.failure = "lost_ack"
    body = request(env)
    await outcome(env.runtime.submit(body))
    native = submits(env)
    assert len(native) == 1
    retry = await outcome(env.runtime.submit(body))
    assert isinstance(retry, Task), "The exact durable receipt should be recoverable"
    assert retry.state == TaskState.PAUSED
    assert retry.task_id == native[0].payload["task_id"]
    assert retry.model_pin.model_dump(mode="json") == native[0].payload["model_pin"]
    assert retry.content_scope == native[0].payload["content_scope"] == "local_only"
    assert len(env.journal.attempts) == 1
    assert_no_worker(env)


@pytest.mark.parametrize("phase", ["before", "after"])
async def test_held_native_ack_is_not_a_confirmed_status(env, phase):
    setattr(env.journal, "hold_" + phase, True)
    operation = start(env, request(env))
    gate = getattr(env.journal, phase)
    await asyncio.wait_for(gate.entered.wait(), timeout=2)
    assert len(submits(env)) == (phase == "after")
    assert not operation.done()
    await assert_no_confirmed_status(env)
    assert_no_worker(env)


async def test_late_ack_after_close_never_launches_a_worker(env):
    env.journal.hold_after = True
    operation = start(env, request(env))
    await asyncio.wait_for(env.journal.after.entered.wait(), timeout=2)
    assert len(submits(env)) == 1
    await asyncio.wait_for(env.runtime.close(), timeout=2)
    env.journal.after.release.set()
    result = await settled(operation)
    await checkpoint()
    assert_not_active(result)
    assert_no_worker(env)


async def test_cancellation_wins_even_if_native_ack_ignores_cancellation(env):
    env.journal.hold_after = True
    env.journal.after.defer_cancellation = True
    operation = start(env, request(env))
    await asyncio.wait_for(env.journal.after.entered.wait(), timeout=2)
    operation.cancel()
    await checkpoint()
    env.journal.after.release.set()
    result = await settled(operation)
    await asyncio.wait_for(env.journal.finished.wait(), timeout=2)
    await checkpoint()
    assert len(submits(env)) == 1
    assert_not_active(result)
    assert_no_worker(env)


@pytest.mark.parametrize("phase", ["before", "after"])
async def test_reset_while_submit_is_held_prevents_late_launch(env, phase):
    setattr(env.journal, "hold_" + phase, True)
    operation = start(env, request(env))
    gate = getattr(env.journal, phase)
    await asyncio.wait_for(gate.entered.wait(), timeout=2)
    current = env.store.reset(env.session.session_id)
    assert current.epoch == env.session.epoch + 1
    gate.release.set()
    result = await settled(operation)
    await checkpoint()
    assert len(submits(env)) == (phase == "after")
    assert_not_active(result)
    await assert_no_confirmed_status(env)
    assert_no_worker(env)


@pytest.mark.parametrize("mutation", ["origin", "key", "objective", "task_refs"])
async def test_mismatched_submit_ack_cannot_launch_work(env, mutation):
    env.journal.bad_ack = mutation
    result = await outcome(env.runtime.submit(request(env)))
    await checkpoint()
    native = submits(env)
    assert len(native) == 1
    assert native[0].origin == "agent-supervisor"
    assert native[0].payload["objective"] == OBJECTIVE
    assert len(native[0].task_refs) == 1
    assert_not_active(result)
    assert_no_worker(env)


@pytest.mark.parametrize("actor", ["owner", "decisions"])
async def test_confirmed_live_exact_replay_preserves_its_single_worker(env, actor):
    body = request(env)
    accepted = await env.runtime.submit(body, actor=actor)
    await asyncio.wait_for(env.worker_started.wait(), timeout=2)
    worker = env.runtime._get(accepted.task_id).worker
    assert worker is not None and not worker.done()
    retry = await env.runtime.submit(body, actor=actor)
    assert retry == accepted
    assert retry.model_pin is not None
    assert env.runtime._get(accepted.task_id).worker is worker
    assert env.worker_calls == [accepted.task_id]
    assert len(submits(env)) == len(env.journal.attempts) == 1
    assert env.provider.calls == []


@pytest.mark.parametrize("actor", ["agent", "converse", "resources", "decisions"])
async def test_role_denials_do_not_reserve_or_append_a_submit(env, actor):
    with pytest.raises(PolicyDenied):
        await env.runtime.submit(request(env, sources=False), actor=actor)
    assert env.journal.attempts == []
    assert submits(env) == []
    assert await env.runtime.status() == []
    assert_no_worker(env)


@pytest.mark.parametrize("changed", ["objective", "content_scope"])
async def test_uncertain_key_does_not_accept_a_conflicting_request(env, changed):
    env.journal.failure = "before_commit"
    body = request(env)
    await outcome(env.runtime.submit(body))
    value = "A different synthetic objective" if changed == "objective" else "public_synthetic"
    different = body.model_copy(update={changed: value})
    with pytest.raises(IdemConflict):
        await env.runtime.submit(different)
    assert submits(env) == []
    assert len(env.journal.attempts) == 1
    assert_no_worker(env)
