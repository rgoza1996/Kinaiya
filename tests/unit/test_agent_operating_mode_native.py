"""Codex native #217 tests: actual SQLite authority, HTTP and admission ordering."""

import asyncio
import hashlib
import json
import sqlite3
import threading

import httpx
import pytest

from core.policy import ToolPolicy
from packages.contracts.agent_control import (
    AgentModeChange,
    AgentSteerAdmissionRequest,
    AgentSteerChange,
    AgentTaskIdentity,
)
from packages.contracts.agent_submit import submit_request
from packages.contracts.client import JournalClient
from packages.contracts.interfaces import (
    AgentControlConflict,
    AgentControlUnavailable,
    AgentModeOff,
)
from packages.contracts.models import AppendRequest, SessionRequest, TaskRequest
from packages.providers.text import ScriptedProvider
from services.agent.app import create_app as agent_app
from services.agent.runtime import AgentRuntime
from services.common.auth import Auth
from services.session.agent_control import AgentControl
from services.session.api import create_app
from services.session.store import Store


def change(controller, mode="off", key="mode-1", **updates):
    state = controller.snapshot()
    return AgentModeChange(
        schema_version="1.0",
        idempotency_key=key,
        expected_backend=state.backend_id,
        expected_instance=state.instance_id,
        expected_revision=state.revision,
        mode=mode,
        active_work_policy="finish_already_accepted",
        **updates,
    )


def proposal(session, key="task-1"):
    logical = {
        "session_id": session.session_id,
        "thread_id": session.thread_id,
        "epoch": session.epoch,
        "objective": "Synthetic bounded work",
        "source_event_ids": [],
        "source_revision": 1,
        "content_scope": "local_only",
    }
    digest = hashlib.sha256(
        json.dumps(logical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return submit_request(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.command",
            task_refs=[key],
            idempotency_key=f"agent:{key}:agent.command:1",
            payload={
                "task_id": key,
                "state": "accepted",
                "op": "submit",
                "actor": "owner",
                "session_id": session.session_id,
                "thread_id": session.thread_id,
                "epoch": session.epoch,
                "objective": logical["objective"],
                "objective_revision": 1,
                "source_event_ids": [],
                "source_revision": 1,
                "requested_content_scope": "local_only",
                "idempotency_key": key,
                "payload_digest": digest,
                "harness_version": "seed-v1",
                "model_pin": None,
                "explicit_skill": None,
                "plugin_versions": {},
                "content_scope": "local_only",
            },
        )
    )


def steering(controller, initial, key="steer-1", **updates):
    task = AgentTaskIdentity(
        schema_version="1.0",
        session_id=initial.session_id,
        thread_id=initial.thread_id,
        epoch=initial.epoch,
        task_id=initial.payload["task_id"],
        submit_event_id=initial.event_id,
        submit_sequence=initial.sequence,
        submit_idempotency_key=initial.payload["idempotency_key"],
        submit_payload_digest=initial.payload["payload_digest"],
    )
    return AgentSteerAdmissionRequest(
        schema_version="1.0",
        expected_backend=controller.backend(),
        task=task,
        actor="owner",
        request=AgentSteerChange(
            schema_version="1.0",
            instruction="Synthetic revised goal\0with raw data",
            content_scope="local_only",
            expected_objective_revision=1,
            idempotency_key=key,
            source_event_ids=(),
            **updates,
        ),
        effective_content_scope="local_only",
        source_revisions=(),
    )


@pytest.fixture
def native(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    controller = AgentControl(store, new_store=store.agent_control.new_store)
    controller.register()
    try:
        yield store, controller
    finally:
        store.close()


def test_mode_save_replay_and_shared_reader_open_never_registers_writer(native):
    store, owner = native
    request = change(owner)
    saved = owner.change(request)
    observer = Store(store.path)
    try:
        assert observer.agent_control.snapshot().instance_id == owner.instance
        assert observer.agent_control.snapshot().control_writable is False
        current = owner.change(change(owner, "on_demand", "mode-2"))
        assert current.revision == 2
        assert owner.change(request) == saved
        assert owner.snapshot().mode == "on_demand"
        assert owner.snapshot().revision == 2
        assert owner.lookup(request).receipt == saved
    finally:
        observer.close()


def test_retired_controller_refuses_first_application_but_exact_history_survives(native):
    store, old = native
    retained = change(old)
    receipt = old.change(retained)
    unobserved = change(old, "on_demand", "unobserved")
    replacement = AgentControl(store)
    replacement.register()
    assert old.snapshot().control_writable is False
    with pytest.raises(AgentControlConflict):
        old.change(unobserved)
    assert old.change(retained) == receipt
    assert replacement.lookup(unobserved).mutation_fenced is True
    old.close()
    assert replacement.snapshot().control_writable is True


def test_unknown_instance_cannot_be_classified_as_retired(native):
    _, owner = native
    good = change(owner)
    unknown = good.model_copy(update={"expected_instance": "01234567-89ab-cdef-0123-456789abcdef"})
    for call in [owner.change, owner.lookup]:
        with pytest.raises(AgentControlConflict):
            call(unknown)
    future = good.model_copy(update={"expected_revision": good.expected_revision + 1})
    assert owner.lookup(future).mutation_fenced is False
    assert owner.lookup(good).mutation_fenced is False


@pytest.mark.parametrize("channel", ["agent", "converse"])
def test_off_refuses_only_new_submit_and_preserves_exact_native_receipt(native, channel):
    store, owner = native
    session = store.create_session(SessionRequest(channel=channel))
    first = proposal(session)
    created = store.admit_submit(first, "agent")
    owner.change(change(owner))
    assert store.admit_submit(first, "agent").entry == created.entry
    with pytest.raises(AgentModeOff):
        store.admit_submit(proposal(session, "task-2"), "agent")
    assert (
        store.db.execute("SELECT count(*) FROM entries WHERE kind='agent.command'").fetchone()[0]
        == 1
    )


def test_atomic_admission_commits_before_concurrent_off(native):
    store, owner = native
    session = store.create_session(SessionRequest(channel="agent"))
    request = change(owner)
    second = Store(store.path)
    control = AgentControl(second)
    control.register()
    request = request.model_copy(update={"expected_instance": control.instance})
    entered = threading.Event()
    release = threading.Event()
    off_done = threading.Event()
    append = store._append_entry_locked

    def gated(*args):
        entered.set()
        assert release.wait(2)
        return append(*args)

    store._append_entry_locked = gated
    results = []

    def accept():
        results.append(store.admit_submit(proposal(session), "agent"))

    def turn_off():
        results.append(control.change(request))
        off_done.set()

    first = threading.Thread(target=accept)
    writer = threading.Thread(target=turn_off)
    try:
        first.start()
        assert entered.wait(2)
        writer.start()
        assert not off_done.wait(0.05)
        release.set()
        first.join(2)
        writer.join(2)
        assert not first.is_alive() and not writer.is_alive()
        assert len(results) == 2 and owner.snapshot().mode == "off"
    finally:
        release.set()
        first.join(2)
        if writer.ident is not None:
            writer.join(2)
        store._append_entry_locked = append
        second.close()


def test_explicit_false_bootstrap_only_new_store_and_persisted_mode_wins(tmp_path):
    path = tmp_path / "fresh.sqlite"
    first = Store(path)
    controller = AgentControl(first, new_store=first.agent_control.new_store)
    controller.register(legacy_agent_enabled=False)
    assert controller.snapshot().mode == "off"
    controller.change(change(controller, "on_demand"))
    first.close()
    second = Store(path)
    replacement = AgentControl(second, new_store=second.agent_control.new_store)
    replacement.register(legacy_agent_enabled=False)
    assert replacement.snapshot().mode == "on_demand"
    assert replacement.snapshot().standing_work_available is False
    with pytest.raises(AgentControlConflict):
        replacement.change(change(replacement, "autonomous"))
    second.close()


@pytest.mark.parametrize("damage", ["missing", "marker", "metadata", "receipt"])
def test_corrupt_control_metadata_and_receipts_fail_closed_without_repair(native, damage):
    store, owner = native
    request = change(owner)
    owner.change(request)
    if damage == "missing":
        store.db.execute("DROP TABLE agent_control_changes")
    elif damage == "marker":
        store.db.execute("UPDATE schema_meta SET agent_control_version=NULL")
    elif damage == "metadata":
        store.db.execute("UPDATE agent_control_state SET backend_id='invalid'")
    else:
        store.db.execute(
            "UPDATE agent_control_changes SET receipt=?",
            ('{"x":' + "[" * 2000 + "0" + "]" * 2000 + "}",),
        )
    store.db.commit()
    with pytest.raises(AgentControlUnavailable):
        owner.lookup(request)


class FaultConnection:
    def __init__(self, actual, point, rollback_failure=False):
        self.actual, self.point, self.rollback_failure = actual, point, rollback_failure
        self.triggered = False

    def __getattr__(self, key):
        return getattr(self.actual, key)

    def execute(self, sql, *args):
        result = self.actual.execute(sql, *args)
        if not self.triggered and (
            (self.point == "state" and sql.startswith("UPDATE agent_control_state SET mode"))
            or (self.point == "receipt" and sql.startswith("INSERT INTO agent_control_changes"))
        ):
            self.triggered = True
            raise RuntimeError("synthetic storage failure")
        return result

    def commit(self):
        if self.point == "commit" and not self.triggered:
            self.triggered = True
            self.actual.commit()
            raise RuntimeError("synthetic lost commit ACK")
        return self.actual.commit()

    def rollback(self):
        if self.rollback_failure:
            raise RuntimeError("synthetic failed rollback")
        self.actual.rollback()


@pytest.mark.parametrize("point", ["state", "receipt", "commit"])
def test_actual_dml_or_commit_exception_never_claims_definite_failure(native, point):
    store, owner = native
    request = change(owner)
    actual = store.db
    fault = FaultConnection(actual, point)
    store.db = fault
    with pytest.raises(AgentControlUnavailable):
        owner.change(request)
    assert fault.triggered and actual.in_transaction is False
    result = owner.lookup(request)
    assert result.status == ("recorded" if point == "commit" else "unobserved")
    assert owner.snapshot().mode == ("off" if point == "commit" else "on_demand")


def test_failed_rollback_poison_closes_real_connection(native):
    store, owner = native
    request = change(owner)
    actual = store.db
    store.db = FaultConnection(actual, "receipt", True)
    with pytest.raises(AgentControlUnavailable):
        owner.change(request)
    with pytest.raises(sqlite3.ProgrammingError):
        actual.execute("SELECT 1")


async def test_real_control_http_roles_strict_wire_headers_and_off_submit(tmp_path):
    store = Store(tmp_path / "http.sqlite")
    app = create_app(
        store,
        Auth(
            {
                "owner": "synthetic-owner-token-000000000000",
                "agent": "synthetic-agent-token-000000000000",
                "decisions": "synthetic-decisions-token-000000000000",
            }
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic"
    ) as client:
        state = await client.get(
            "/v1/agent-control/state",
            headers={"Authorization": "Bearer synthetic-agent-token-000000000000"},
        )
        assert state.status_code == 200 and state.headers["cache-control"] == "no-store"
        body = change(app.state.agent_control).model_dump(mode="json")
        denied = await client.post(
            "/v1/agent-control/changes",
            json=body,
            headers={"Authorization": "Bearer synthetic-agent-token-000000000000"},
        )
        assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
        malformed = await client.post(
            "/v1/agent-control/changes",
            content=b'{"schema_version":"1.0","schema_version":"1.0"}',
            headers={
                "Authorization": "Bearer synthetic-owner-token-000000000000",
                "Content-Type": "application/json",
            },
        )
        assert malformed.status_code == 422
        saved = await client.post(
            "/v1/agent-control/changes",
            json=body,
            headers={"Authorization": "Bearer synthetic-owner-token-000000000000"},
        )
        assert saved.status_code == 200 and saved.json()["request"] == body
        session = store.create_session(SessionRequest(channel="agent"))
        refusal = await client.post(
            "/v1/transcript/agent-submits",
            json=proposal(session).model_dump(mode="json"),
            headers={"Authorization": "Bearer synthetic-agent-token-000000000000"},
        )
        assert refusal.status_code == 403 and refusal.json() == {"detail": str(AgentModeOff())}
        assert refusal.headers["cache-control"] == "no-store"
    store.close()


def test_backend_only_read_never_registers_control_writer(tmp_path):
    store = Store(tmp_path / "passive.sqlite")
    try:
        backend = store.agent_control.backend()
        assert store.agent_control.backend() == backend
        assert store.db.execute("SELECT count(*) FROM agent_control_generations").fetchone()[0] == 0
        with pytest.raises(AgentControlUnavailable):
            store.agent_control.snapshot()
        control = AgentControl(store)
        control.register()
        assert control.snapshot().backend_id == backend
    finally:
        store.close()


def test_native_steer_exact_proof_survives_off_and_one_base_slot_is_immutable(native):
    store, owner = native
    session = store.create_session(SessionRequest(channel="agent"))
    initial = store.admit_submit(proposal(session), "agent").entry
    request = steering(owner, initial)
    created = owner.admit_steer(request)
    assert created.disposition == "created" and created.status == "intent_accepted"
    assert created.request.request.instruction == "Synthetic revised goal\0with raw data"
    off = owner.change(change(owner))
    retained = owner.admit_steer(request)
    assert retained.disposition == "retained" and retained.event_id == created.event_id
    assert owner.lookup_steer(request).receipt == retained
    assert owner.snapshot().revision == off.revision
    with pytest.raises(AgentControlConflict):
        owner.admit_steer(steering(owner, initial, "other-key"))
    different = request.model_copy(
        update={"request": request.request.model_copy(update={"instruction": "conflict"})}
    )
    for method in (owner.admit_steer, owner.lookup_steer):
        with pytest.raises(AgentControlConflict):
            method(different)
    assert store.db.execute("SELECT count(*) FROM agent_steer_intents").fetchone()[0] == 1


def test_negative_steer_lookup_never_becomes_absence_fence_after_off_aba(native):
    store, owner = native
    session = store.create_session(SessionRequest(channel="agent"))
    initial = store.admit_submit(proposal(session), "agent").entry
    request = steering(owner, initial)
    owner.change(change(owner))
    with pytest.raises(AgentModeOff):
        owner.admit_steer(request)
    absent = owner.lookup_steer(request)
    assert absent.status == "unobserved" and absent.receipt is None
    assert "mutation_fenced" not in absent.model_dump()
    owner.change(change(owner, "on_demand", "on"))
    assert owner.lookup_steer(request).status == "unobserved"
    assert owner.admit_steer(request).disposition == "created"


@pytest.mark.parametrize("damage", ["foreign-intent", "digest", "none"])
def test_applied_revision_requires_this_initial_submit_and_exact_intent_digest(native, damage):
    store, owner = native
    session = store.create_session(SessionRequest(channel="agent"))
    initial = store.admit_submit(proposal(session), "agent").entry
    reference = initial
    if damage == "foreign-intent":
        reference = store.admit_submit(proposal(session, "other-task"), "agent").entry
    original = steering(owner, reference)
    accepted = owner.admit_steer(original)
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.command",
            task_refs=[initial.payload["task_id"]],
            idempotency_key=f"agent:{initial.payload['task_id']}:agent.command:2",
            payload={
                "task_id": initial.payload["task_id"],
                "state": "running",
                "op": "steer-applied",
                "content_scope": "local_only",
                "objective_revision": 2,
                "steer_event_id": accepted.event_id,
                "intent_digest": original.request_digest() if damage != "digest" else "0" * 64,
            },
        ),
        "agent",
    )
    request = steering(owner, initial, "next")
    request = request.model_copy(
        update={"request": request.request.model_copy(update={"expected_objective_revision": 2})}
    )
    if damage == "none":
        assert owner.admit_steer(request).disposition == "created"
    else:
        with pytest.raises(AgentControlUnavailable):
            owner.admit_steer(request)


def test_steer_history_filters_unrelated_large_tool_before_body_validation(native, monkeypatch):
    store, owner = native
    session = store.create_session(SessionRequest(channel="agent"))
    initial = store.admit_submit(proposal(session), "agent").entry
    other = store.admit_submit(proposal(session, "other-task"), "agent").entry
    unrelated = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.tool",
            task_refs=[other.payload["task_id"]],
            idempotency_key="agent:other-task:agent.tool:2",
            payload={
                "task_id": other.payload["task_id"],
                "state": "running",
                "content_scope": "local_only",
                "output": "Synthetic unrelated tool result. " * 1900,
            },
        ),
        "agent",
    )
    assert len(unrelated.model_dump_json().encode()) > 60000
    inspected = []
    original = owner._event

    def inspect(row):
        inspected.append(row["event_id"])
        return original(row)

    monkeypatch.setattr(owner, "_event", inspect)
    assert owner.admit_steer(steering(owner, initial)).disposition == "created"
    assert unrelated.event_id not in inspected and other.event_id not in inspected


@pytest.mark.parametrize("point", ["entry", "intent", "commit"])
def test_native_steer_actual_dml_and_lost_commit_ack_are_unconfirmed(native, point):
    store, owner = native
    session = store.create_session(SessionRequest(channel="agent"))
    initial = store.admit_submit(proposal(session), "agent").entry
    request = steering(owner, initial)
    actual = store.db

    class SteerFault(FaultConnection):
        def execute(self, sql, *args):
            result = self.actual.execute(sql, *args)
            if not self.triggered and (
                (point == "entry" and sql.startswith("INSERT INTO entries"))
                or (point == "intent" and sql.startswith("INSERT INTO agent_steer_intents"))
            ):
                self.triggered = True
                raise RuntimeError("Synthetic native intent storage failure")
            return result

    fault = SteerFault(actual, point)
    store.db = fault
    with pytest.raises(AgentControlUnavailable):
        owner.admit_steer(request)
    assert fault.triggered and actual.in_transaction is False
    observed = owner.lookup_steer(request)
    assert observed.status == ("recorded" if point == "commit" else "unobserved")
    count = actual.execute("SELECT count(*) FROM agent_steer_intents").fetchone()[0]
    assert count == (1 if point == "commit" else 0)


class HeldProvider(ScriptedProvider):
    def __init__(self):
        super().__init__([])
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages):
        self.entered.set()
        await self.release.wait()
        return await super().complete(messages)


@pytest.fixture
async def http_stack(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "stack.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    owner_token = "synthetic-owner-token-000000000000"
    agent_token = "synthetic-agent-token-000000000000"
    auth = Auth({"owner": owner_token, "agent": agent_token})
    session_api = create_app(store, auth)
    journal = JournalClient(
        "http://session.test", agent_token, transport=httpx.ASGITransport(app=session_api)
    )
    provider = HeldProvider()
    runtime = AgentRuntime(journal, provider, workspace, ToolPolicy(workspace))
    request = TaskRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Synthetic unchanged initial goal",
        idempotency_key="http-original",
    )
    task = await runtime.submit(request)
    await asyncio.wait_for(provider.entered.wait(), 2)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent_app(runtime, auth)),
        base_url="http://agent.test",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    try:
        yield store, session_api.state.agent_control, runtime, journal, client, task, request
    finally:
        await client.aclose()
        await runtime.close()
        await journal.close()
        store.close()


def steer_body(key="http-steer"):
    return {
        "instruction": "Synthetic revised goal",
        "expected_objective_revision": 1,
        "idempotency_key": key,
    }


async def test_actual_http_off_refuses_new_goal_preserves_receipt_and_stop(http_stack):
    store, owner, runtime, journal, client, task, request = http_stack
    owner.change(change(owner))
    off = await client.post(f"/v1/agent/tasks/{task.task_id}/steer", json=steer_body())
    assert off.status_code == 403 and off.json() == {"detail": str(AgentModeOff())}
    assert runtime._tasks[task.task_id].task.objective == request.objective
    assert runtime._tasks[task.task_id].pending_steer is None
    assert store.db.execute("SELECT count(*) FROM agent_steer_intents").fetchone()[0] == 0
    assert (await runtime.submit(request)).task_id == task.task_id
    fresh = request.model_copy(update={"idempotency_key": "first-refused-submit"})
    with pytest.raises(AgentModeOff):
        await runtime.submit(fresh)
    assert (fresh.session_id, fresh.idempotency_key) not in runtime._pending_submits
    state = await journal.agent_control_state()
    assert state.mode == "off" and state.control_writable is True
    owner.change(change(owner, "on_demand", "on"))
    assert (await runtime.submit(fresh)).task_id != task.task_id
    accepted = await client.post(f"/v1/agent/tasks/{task.task_id}/steer", json=steer_body())
    assert accepted.status_code == 200, accepted.text
    assert store.db.execute("SELECT count(*) FROM agent_steer_intents").fetchone()[0] == 1
    stopped = await client.post(
        f"/v1/agent/tasks/{task.task_id}/interrupt",
        json={"expected_objective_revision": 1, "idempotency_key": "stop"},
    )
    assert stopped.status_code == 200


async def test_http_steer_uncooperative_preflight_is_bounded_and_remains_owned(
    http_stack, monkeypatch
):
    import services.agent.app as app_module

    _, _, runtime, journal, client, task, _ = http_stack
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = journal.session
    operation = None
    calls_after_retirement = []
    retired = False

    async def resistant(session_id):
        nonlocal operation
        operation = asyncio.current_task()
        result = await original(session_id)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return result

    def tracked(name):
        method = getattr(journal, name)

        async def call(*args, **kwargs):
            if retired:
                calls_after_retirement.append(name)
            return await method(*args, **kwargs)

        return call

    monkeypatch.setattr(journal, "session", resistant)
    for name in ("resolve", "entry", "read", "agent_control_backend", "admit_steer"):
        monkeypatch.setattr(journal, name, tracked(name))
    monkeypatch.setattr(app_module, "AGENT_STEER_REQUEST_TIMEOUT_S", 0.05)
    call = asyncio.create_task(
        client.post(f"/v1/agent/tasks/{task.task_id}/steer", json=steer_body())
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        result = await asyncio.wait_for(call, 1)
        retired = True
        assert result.status_code == 503
        await asyncio.wait_for(cancelled.wait(), 1)
        assert operation is not None and not operation.done()
        assert operation in runtime._submit_operations
        assert runtime._tasks[task.task_id].steer_attempt.active is False
    finally:
        release.set()
        if operation is not None:
            await asyncio.wait_for(asyncio.gather(operation, return_exceptions=True), 2)
        await asyncio.gather(call, return_exceptions=True)
    await asyncio.sleep(0)
    assert operation not in runtime._submit_operations
    assert calls_after_retirement == []
    assert runtime._tasks[task.task_id].pending_steer is None


@pytest.mark.parametrize("committed", [False, True])
async def test_actual_lost_steer_ack_remains_sticky_and_lookup_never_installs(
    http_stack, committed
):
    store, owner, runtime, journal, client, task, request = http_stack
    delegate = journal.http._transport
    admissions = []

    class LostAck(httpx.AsyncBaseTransport):
        async def handle_async_request(self, wire):
            if wire.url.path == "/v1/transcript/agent-steers":
                admissions.append(json.loads(await wire.aread()))
                if not committed:
                    raise httpx.ConnectError("Synthetic unknown request outcome", request=wire)
                response = await delegate.handle_async_request(wire)
                assert response.status_code == 200
                await response.aclose()
                raise httpx.ReadError("Synthetic lost native ACK", request=wire)
            return await delegate.handle_async_request(wire)

        async def aclose(self):
            await delegate.aclose()

    journal.http._transport = LostAck()
    first = await client.post(f"/v1/agent/tasks/{task.task_id}/steer", json=steer_body())
    assert first.status_code == 503
    owner.change(change(owner))
    retry = await client.post(f"/v1/agent/tasks/{task.task_id}/steer", json=steer_body())
    assert retry.status_code == 503
    owner.change(change(owner, "on_demand", "on"))
    again = await client.post(f"/v1/agent/tasks/{task.task_id}/steer", json=steer_body())
    assert again.status_code == 503
    different = await client.post(
        f"/v1/agent/tasks/{task.task_id}/steer", json=steer_body("different-key")
    )
    assert different.status_code == 409
    attempt = runtime._tasks[task.task_id].steer_attempt
    assert attempt.active is False and attempt.installed is False
    assert (attempt.receipt is not None) is committed
    assert runtime._tasks[task.task_id].pending_steer is None
    assert runtime._tasks[task.task_id].task.objective == request.objective
    assert len(admissions) == 1
    assert store.db.execute("SELECT count(*) FROM agent_steer_intents").fetchone()[0] == int(
        committed
    )


@pytest.mark.parametrize("method", ["submit", "steer"])
@pytest.mark.parametrize("exact_marker", [False, True])
async def test_only_complete_fixed_off_marker_is_a_typed_refusal(native, method, exact_marker):
    from packages.contracts.agent_submit import AgentSubmitUnavailable

    store, owner = native
    session = store.create_session(SessionRequest(channel="agent"))
    proposed = proposal(session)
    initial = store.admit_submit(proposed, "agent").entry
    body = {"detail": str(AgentModeOff()) if exact_marker else "Forbidden"}
    journal = JournalClient(
        "http://synthetic.test",
        "synthetic-agent-token-000000000000",
        transport=httpx.MockTransport(lambda wire: httpx.Response(403, json=body)),
    )
    try:
        expected = (
            AgentModeOff
            if exact_marker
            else (AgentSubmitUnavailable if method == "submit" else AgentControlUnavailable)
        )
        with pytest.raises(expected):
            if method == "submit":
                await journal.admit_submit(proposed)
            else:
                await journal.admit_steer(steering(owner, initial))
    finally:
        await journal.close()
