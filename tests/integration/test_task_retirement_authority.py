"""Native journal/recovery boundary tests; all task content is synthetic."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.commands import Registry
from packages.contracts.models import (
    AppendRequest,
    CommandRequest,
    InterruptRequest,
    MCPControl,
    ProviderInfo,
    SessionRequest,
    SteerRequest,
    Task,
    TaskRequest,
    TaskRetirement,
    TaskState,
)
from packages.providers.completion_budget import PreparedComplete
from services.agent import AgentRuntime
from services.agent.mcp_controls import MCPManagementBusy, apply_mcp_control
from services.session.store import JournalError, LocalJournal, Store


class NoInferenceFixture:
    info = ProviderInfo(provider="synthetic-fixture", model="no-inference", local=True)

    async def prepare_complete(self, messages):
        """Explicit local test double; no tokenizer or inference claim."""
        return PreparedComplete(None, lambda: self.complete(messages), fixture=True)

    async def complete(self, messages):
        raise AssertionError("Recovery/retirement must not invoke inference")


class ManagedFixture:
    """Control-plane fixture; real MCP transports are exercised separately."""

    def __init__(self):
        self.host = self
        self.calls = []

    def list_servers(self):
        return []

    async def control(self, operation, **fields):
        self.calls.append(operation)
        return {"status": "connected"}

    async def close(self):
        pass


def receipt(task, **changes):
    fields = dict(
        task_id=task.task_id,
        task_session_id=task.session_id,
        task_thread_id=task.thread_id,
        task_epoch=task.epoch,
        objective_revision=task.objective_revision,
        request_key_sha256=hashlib.sha256(b"synthetic-stop").hexdigest(),
        request_digest="1" * 64,
    )
    fields.update(changes)
    return TaskRetirement(**fields).model_dump(mode="json")


def seed_native_submit(store, session, task):
    # Explicit native journal fixture representing interrupted execution.
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.command",
            payload={"op": "submit", **task.model_dump(mode="json")},
            task_refs=[task.task_id],
            idempotency_key="synthetic-native-submit",
        ),
        "agent",
    )


@pytest.fixture
async def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="SYNTHETIC_OLD_CONTEXT_MUST_NOT_ENTER_NEW_EPOCH",
        idempotency_key="synthetic-task",
    )
    submitted = seed_native_submit(store, session, task)
    current = store.reset(session.session_id)
    journal = LocalJournal(store, "agent")
    controller = ManagedFixture()
    runtime = AgentRuntime(
        journal,
        NoInferenceFixture(),
        workspace,
        ToolPolicy(workspace),
        mcp_controller=controller,
    )
    await runtime.recover()
    state = SimpleNamespace(
        runtime=runtime,
        store=store,
        old=session,
        current=current,
        task=task,
        submitted=submitted,
        controller=controller,
    )
    yield state
    await state.runtime.close()
    store.close()


def stop_request():
    return InterruptRequest(expected_objective_revision=1, idempotency_key="synthetic-stop")


@pytest.mark.parametrize("actor", ["converse", "agent", "external", "decisions"])
async def test_non_owner_cannot_retire_recovered_work(env, actor):
    with pytest.raises(PolicyDenied):
        await env.runtime.interrupt(env.task.task_id, stop_request(), actor=actor)
    assert (await env.runtime.status(env.task.task_id)).state == "paused"
    assert not env.store.read(kinds=["agent.retirement"])


@pytest.mark.parametrize("role", ["owner", "converse", "decisions", "resources"])
async def test_retirement_origin_requires_native_agent_writer(env, role):
    request = AppendRequest(
        session_id=env.current.session_id,
        thread_id=env.current.thread_id,
        epoch=env.current.epoch,
        kind="agent.retirement",
        payload=receipt(env.task),
        task_refs=[env.task.task_id],
        idempotency_key="not-native-agent",
    )
    with pytest.raises(PermissionError):
        env.store.append(request, role)


async def test_ordinary_stale_epochs_and_cross_epoch_causes_remain_forbidden(env):
    for kind in ["agent.command", "agent.outcome", "agent.retirement"]:
        with pytest.raises(JournalError, match="Stale epoch"):
            env.store.append(
                AppendRequest(
                    session_id=env.old.session_id,
                    thread_id=env.old.thread_id,
                    epoch=env.old.epoch,
                    kind=kind,
                    payload={},
                    idempotency_key="stale-" + kind,
                ),
                "agent",
            )
    with pytest.raises(JournalError, match="Causal reference"):
        env.store.append(
            AppendRequest(
                session_id=env.current.session_id,
                thread_id=env.current.thread_id,
                epoch=env.current.epoch,
                kind="agent.retirement",
                payload=receipt(env.task),
                causes=[env.submitted.event_id],
                task_refs=[env.task.task_id],
                idempotency_key="old-cause",
            ),
            "agent",
        )


@pytest.mark.parametrize(
    "changes,refs",
    [
        ({"task_epoch": 999}, None),
        ({"task_thread_id": "another-thread"}, None),
        ({"task_session_id": "another-session"}, None),
        ({"objective_revision": 99}, None),
        ({"objective": "untrusted copied context"}, None),
        ({"schema_version": "999"}, None),
        ({}, ["another-task"]),
    ],
)
async def test_malformed_or_mismatched_retirement_cannot_settle_work(env, changes, refs):
    payload = {**receipt(env.task), **changes}
    env.store.append(
        AppendRequest(
            session_id=env.current.session_id,
            thread_id=env.current.thread_id,
            epoch=env.current.epoch,
            kind="agent.retirement",
            payload=payload,
            task_refs=refs if refs is not None else [env.task.task_id],
            idempotency_key="malformed-native-receipt",
        ),
        "agent",
    )
    await env.runtime.close()
    env.runtime = AgentRuntime(
        LocalJournal(env.store, "agent"),
        NoInferenceFixture(),
        env.runtime.workspace,
        env.runtime.policy,
        mcp_controller=env.controller,
    )
    await env.runtime.recover()
    assert (await env.runtime.status(env.task.task_id)).state == "paused"
    with pytest.raises(MCPManagementBusy):
        await apply_mcp_control(
            env.runtime,
            MCPControl(operation="test", server_id="synth", idempotency_key="test"),
        )


async def test_retiring_old_work_does_not_release_current_active_work_gate(env):
    entered = asyncio.Event()

    async def hung(messages):
        entered.set()
        await asyncio.Event().wait()

    env.runtime.provider.complete = hung
    current_task = await env.runtime.submit(
        TaskRequest(
            session_id=env.current.session_id,
            thread_id=env.current.thread_id,
            epoch=env.current.epoch,
            objective="SYNTHETIC_CURRENT_WORK",
            idempotency_key="current-task",
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    retired = await env.runtime.interrupt(env.task.task_id, stop_request())
    assert retired.state == "cancelled" and retired.epoch == env.old.epoch
    assert (await env.runtime.status(current_task.task_id)).state == "running"
    with pytest.raises(MCPManagementBusy):
        await apply_mcp_control(
            env.runtime,
            MCPControl(operation="test", server_id="synth", idempotency_key="still-busy"),
        )
    assert not env.controller.calls
    await env.runtime.interrupt(current_task.task_id, stop_request())
    assert (await env.runtime.wait(current_task.task_id)).state == "cancelled"
    result = await apply_mcp_control(
        env.runtime,
        MCPControl(operation="test", server_id="synth", idempotency_key="now-idle"),
    )
    assert result["status"] == "connected"


def test_retirement_contract_cannot_contain_execution_context():
    for changes in [{"task_epoch": True}, {"objective_revision": "1"}]:
        with pytest.raises(ValueError):
            receipt(
                Task(session_id="s", thread_id="t", epoch=1, objective="x", idempotency_key="x"),
                **changes,
            )


async def test_residual_process_handle_prevents_retirement(env):
    record = env.runtime._tasks[env.task.task_id]
    record.proc_slot["proc"] = object()  # Deliberately unconfirmed native handle.
    try:
        with pytest.raises(ValueError, match="process"):
            await env.runtime.interrupt(env.task.task_id, stop_request())
        assert (await env.runtime.status(env.task.task_id)).state == "paused"
        assert not env.store.read(kinds=["agent.retirement"])
    finally:
        record.proc_slot.clear()


@pytest.mark.parametrize("service_name", ["plugin_service", "skill_service"])
async def test_unconfirmed_pin_cleanup_cannot_publish_retirement(env, service_name):
    class PinFixture:
        calls = 0

        async def release_task(self, task_id):
            assert task_id == env.task.task_id
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Synthetic unconfirmed pin cleanup")

        async def close(self):
            pass

    pins = PinFixture()
    setattr(env.runtime, service_name, pins)
    try:
        with pytest.raises(RuntimeError, match="unconfirmed pin"):
            await env.runtime.interrupt(env.task.task_id, stop_request())
        assert (await env.runtime.status(env.task.task_id)).state == "paused"
        assert not env.store.read(kinds=["agent.retirement"])
        assert (await env.runtime.interrupt(env.task.task_id, stop_request())).state == "cancelled"
        assert pins.calls == 2
    finally:
        setattr(env.runtime, service_name, None)


async def test_cancelled_append_remains_paused_and_retryable(env, monkeypatch):
    entered = asyncio.Event()
    original = env.runtime.journal.append

    async def pending(request):
        if request.kind == "agent.retirement":
            entered.set()
            await asyncio.Event().wait()
        return await original(request)

    monkeypatch.setattr(env.runtime.journal, "append", pending)
    stopping = asyncio.create_task(env.runtime.interrupt(env.task.task_id, stop_request()))
    await asyncio.wait_for(entered.wait(), 1)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert (await env.runtime.status(env.task.task_id)).state == "paused"
    assert not env.store.read(kinds=["agent.retirement"])
    monkeypatch.setattr(env.runtime.journal, "append", original)
    assert (await env.runtime.interrupt(env.task.task_id, stop_request())).state == "cancelled"


async def test_steering_cannot_change_recovered_identity_during_retirement(env, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.runtime.journal.append

    async def delayed(request):
        if request.kind == "agent.retirement":
            entered.set()
            await release.wait()
        return await original(request)

    monkeypatch.setattr(env.runtime.journal, "append", delayed)
    stopping = asyncio.create_task(env.runtime.interrupt(env.task.task_id, stop_request()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ValueError, match="requires a new task"):
            await env.runtime.steer(
                env.task.task_id,
                SteerRequest(
                    instruction="Synthetic replacement",
                    expected_objective_revision=1,
                    idempotency_key="steer-during-retire",
                ),
            )
        assert env.runtime._tasks[env.task.task_id].pending_steer is None
    finally:
        release.set()
        await stopping
    assert (await env.runtime.status(env.task.task_id)).objective_revision == 1


@pytest.mark.parametrize("other_session", [False, True])
async def test_stop_command_journal_does_not_copy_old_context_into_new_epoch(env, other_session):
    commands = Registry(env.runtime, LocalJournal(env.store, "owner"))
    selected = (
        env.store.create_session(SessionRequest(channel="agent")) if other_session else env.current
    )
    try:
        result = await commands.execute(
            CommandRequest(
                session_id=selected.session_id,
                thread_id=selected.thread_id,
                epoch=selected.epoch,
                input="/stop " + env.task.task_id,
                idempotency_key="synthetic-command-stop",
            )
        )
        assert result["status"] == "ok" and result["result"]["state"] == "cancelled"
        # The authorized owner response may inspect the old task. The current
        # epoch's command receipt must contain only its administrative identity.
        assert result["result"]["objective"] == env.task.objective
        current = [
            e for e in env.store.read(session_id=selected.session_id) if e.epoch == selected.epoch
        ]
        assert current and all(env.task.objective not in e.model_dump_json() for e in current)
    finally:
        commands.close()


async def test_receipt_cannot_retire_a_task_submitted_later(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Synthetic later task",
        idempotency_key="later-task",
    )
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.retirement",
            payload=receipt(task),
            task_refs=[task.task_id],
            idempotency_key="before-submission",
        ),
        "agent",
    )
    seed_native_submit(store, session, task)
    store.reset(session.session_id)
    runtime = AgentRuntime(
        LocalJournal(store, "agent"), NoInferenceFixture(), workspace, ToolPolicy(workspace)
    )
    try:
        await runtime.recover()
        assert (await runtime.status(task.task_id)).state == "paused"
    finally:
        await runtime.close()
        store.close()


async def test_retirement_cannot_override_native_terminal_outcome(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Synthetic already cancelled work",
        idempotency_key="terminal-task",
        state="cancelled",
        summary="Native cancellation before reset",
    )
    seed_native_submit(store, session, task.model_copy(update={"state": TaskState.ACCEPTED}))
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.outcome",
            payload={"task": task.model_dump(mode="json")},
            task_refs=[task.task_id],
            idempotency_key="native-terminal-outcome",
        ),
        "agent",
    )
    current = store.reset(session.session_id)
    store.append(
        AppendRequest(
            session_id=current.session_id,
            thread_id=current.thread_id,
            epoch=current.epoch,
            kind="agent.retirement",
            payload=receipt(task),
            task_refs=[task.task_id],
            idempotency_key="later-retirement",
        ),
        "agent",
    )
    runtime = AgentRuntime(
        LocalJournal(store, "agent"), NoInferenceFixture(), workspace, ToolPolicy(workspace)
    )
    try:
        await runtime.recover()
        recovered = await runtime.status(task.task_id)
        assert recovered.state == "cancelled"
        assert recovered.summary == "Native cancellation before reset"
    finally:
        await runtime.close()
        store.close()
