"""Issue #141: native receipts with synthetic ASGI/transport fixtures only.

No models, external services, private conversations or paid inference are used.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from core.policy import ToolPolicy
from packages.agent_adapters.http import AgentClient
from packages.commands import Registry
from packages.contracts.models import (
    AppendRequest,
    Decision,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    Task,
    TaskState,
    WindowDecision,
)
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime
from services.agent import create_app as agent_app
from services.common.auth import Auth
from services.decisions.api import create_app as decisions_app
from services.decisions.runtime import DecisionsWorker
from services.decisions.window_routing import WindowAgentDispatcher
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import JournalError, LocalJournal, Store

AUTH = Auth({"owner": "owner-synthetic-token-141", "decisions": "decisions-synthetic-token-141"})
ERROR_TEXT = "Synthetic untrusted body claims privacy, completion and secrets."


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    yield store, session, LocalJournal(store, "decisions")
    store.close()


def source_entry(store, session, path, *, scope="public_synthetic", index=0, revision=1):
    if path == "window":
        if revision > 1:
            source_entry(store, session, path, scope=scope, index=index, revision=revision - 1)
        payload = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="synthetic-utterance-141",
            window_index=index,
            revision=revision,
            text=f"Public synthetic instruction {index}",
            start_ms=0,
            end_ms=1000 + index,
            user_speaking=False,
            capture_ended=True,
            content_scope=scope,
        ).model_dump(mode="json")
    else:
        payload = {
            "text": "Public synthetic instruction",
            "final": True,
            "revision": revision,
            "content_scope": scope,
        }
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.window" if path == "window" else "conversation.user",
            payload=payload,
            idempotency_key=f"source:{path}:{index}:{revision}",
        ),
        "owner",
    )


class Labels:
    def __init__(self, route="queue", task=None):
        self.route, self.task, self.calls = route, task, 0

    async def classify(self, *_):
        self.calls += 1
        return {
            "route": self.route,
            **(
                {
                    "target_task_id": self.task.task_id,
                    "expected_objective_revision": self.task.objective_revision,
                }
                if self.task
                else {}
            ),
        }


def routed_worker(store, session, journal, source, path, client, route="queue", task=None):
    if path == "typed":
        return DecisionsWorker(journal, client, Labels(route, task)), source
    decision = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=source.payload["revision"],
        conversation="WAIT_FOR_NEXT_WINDOW",
        agent_route={
            "queue": "QUEUE_UNTIL_TASK_DONE",
            "steer": "STEER_CURRENT_TASK",
            "interrupt": "INTERRUPT_CURRENT_TASK",
        }[route],
        target_task_id=task.task_id if task else None,
        expected_objective_revision=task.objective_revision if task else None,
    )
    entry = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="routing.window_decided",
            payload=decision.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key=f"decision:{decision.decision_id}",
        ),
        "decisions",
    )
    return WindowAgentDispatcher(journal, client), entry


def terminal_receipts(store, path):
    kind = "routing.dispatch" if path == "typed" else "routing.window_dispatch"
    return [e for e in store.read(kinds=[kind]) if e.payload["state"] != "queued"]


class ASGIAgent:
    """Authenticated HTTP executor fixture; records admission, never executes work."""

    def __init__(self, session, mode=201, task=None):
        self.session, self.mode, self.posts = session, mode, []
        self.tasks = [task] if task else []
        self.status_unavailable = False
        self.ack_mutation = None
        app = FastAPI()

        @app.get("/v1/agent/tasks", dependencies=[Depends(AUTH.require("decisions"))])
        async def status():
            if self.status_unavailable:
                raise HTTPException(503, ERROR_TEXT)
            return self.tasks

        @app.post("/v1/agent/tasks", dependencies=[Depends(AUTH.require("decisions"))])
        @app.post(
            "/v1/agent/tasks/{task_id}/{operation}",
            dependencies=[Depends(AUTH.require("decisions"))],
        )
        async def mutate(request: Request):
            body = await request.json()
            self.posts.append((request.url.path, body))
            if isinstance(self.mode, int) and self.mode != 201:
                raise HTTPException(self.mode, ERROR_TEXT)
            if "/tasks/" in request.url.path:
                task = self.tasks[0].model_copy(update={"state": TaskState.CANCEL_REQUESTED})
            else:
                task = Task(
                    session_id=body["session_id"],
                    thread_id=body["thread_id"],
                    epoch=body["epoch"],
                    objective=body["objective"],
                    source_event_ids=body["source_event_ids"],
                    idempotency_key=body["idempotency_key"],
                )
                self.tasks.append(task)
            if self.mode == "timeout_after_admission":
                raise httpx.ReadTimeout(ERROR_TEXT)
            if self.mode == "disconnect_after_admission":
                raise httpx.ConnectError(ERROR_TEXT)
            if self.mode == "deadline_after_admission":
                await asyncio.Event().wait()
            result = task.model_dump(mode="json")
            if self.ack_mutation:
                result.update(self.ack_mutation)
            return result

        self.client = AgentClient(
            "http://synthetic-agent",
            AUTH.tokens["decisions"],
            transport=httpx.ASGITransport(app=app),
        )


@pytest.mark.parametrize("path", ["typed", "window"])
@pytest.mark.parametrize("route", ["queue", "steer", "interrupt"])
@pytest.mark.parametrize("status", [403, 404, 409])
async def test_authenticated_http_rejection_is_definitive_generic_and_not_replayed(
    env, path, route, status
):
    store, session, journal = env
    target = (
        Task(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="Public synthetic existing work",
            state="running",
            idempotency_key="existing",
        )
        if route != "queue"
        else None
    )
    host = ASGIAgent(session, status, target)
    source = source_entry(store, session, path, revision=2)
    worker, event = routed_worker(store, session, journal, source, path, host.client, route, target)
    try:
        await worker.process(event)
        await worker.process(event)
        fresh_worker = (
            type(worker)(journal, host.client, Labels(route, target))
            if path == "typed"
            else type(worker)(journal, host.client)
        )
        await fresh_worker.process(event)
        receipts = terminal_receipts(store, path)
        assert len(host.posts) == 1 and len(receipts) == 1
        receipt = receipts[0]
        assert receipt.origin == "decisions"
        assert (receipt.session_id, receipt.thread_id, receipt.epoch) == (
            source.session_id,
            source.thread_id,
            source.epoch,
        )
        assert receipt.payload["state"] == "rejected"
        assert receipt.payload["reason"] == "executor_rejected"
        assert receipt.payload["http_status"] == status
        assert receipt.payload["source_event_id"] == source.event_id
        assert receipt.payload["source_sequence"] == source.sequence
        assert receipt.payload["source_revision"] == 2
        assert "native_task_id" not in receipt.payload
        assert ERROR_TEXT not in json.dumps(receipt.model_dump(mode="json"))
    finally:
        await host.client.close()


@pytest.mark.parametrize("path", ["typed", "window"])
@pytest.mark.parametrize(
    "mode",
    [
        500,
        503,
        400,
        "timeout_after_admission",
        "disconnect_after_admission",
        "deadline_after_admission",
    ],
)
async def test_unconfirmed_admission_is_unknown_and_restart_does_not_blindly_replay(
    env, path, mode, monkeypatch
):
    store, session, journal = env
    host = ASGIAgent(session, mode)
    source = source_entry(store, session, path)
    worker, event = routed_worker(store, session, journal, source, path, host.client)
    if mode == "deadline_after_admission" and path == "typed":
        monkeypatch.setattr("services.decisions.runtime.DISPATCH_TIMEOUT", 0.02)
    try:
        await worker.process(event)
        restarted = (
            DecisionsWorker(journal, host.client, Labels())
            if path == "typed"
            else WindowAgentDispatcher(journal, host.client)
        )
        await restarted.process(event)
        await restarted.tick()
        receipts = terminal_receipts(store, path)
        assert len(host.posts) == 1 and len(receipts) == 1
        assert receipts[0].payload["state"] == "unknown"
        assert receipts[0].payload["reason"] == "executor_ack_unknown"
        assert "http_status" not in receipts[0].payload
        assert "native_task_id" not in receipts[0].payload
        if isinstance(mode, str):
            assert len(host.tasks) == 1  # The effect exists despite lost acknowledgment.
    finally:
        await host.client.close()


@pytest.mark.parametrize("path", ["typed", "window"])
async def test_positive_receipt_uses_actual_acknowledged_identity_and_state(env, path):
    store, session, journal = env
    host = ASGIAgent(session)
    source = source_entry(store, session, path)
    worker, event = routed_worker(store, session, journal, source, path, host.client)
    try:
        await worker.process(event)
        await worker.process(event)
        receipt = terminal_receipts(store, path)[0]
        assert len(host.posts) == 1
        assert receipt.payload["state"] == ("accepted" if path == "typed" else "applied")
        assert receipt.payload["native_task_id"] == host.tasks[0].task_id
        assert receipt.payload["native_task_state"] == "accepted"
        assert receipt.payload["target_task_id"] is None
        assert receipt.payload["source_revision"] == 1
    finally:
        await host.client.close()


@pytest.mark.parametrize("path", ["typed", "window"])
@pytest.mark.parametrize("route", ["steer", "interrupt"])
@pytest.mark.parametrize("foreign_ack", [False, True])
async def test_positive_control_ack_must_match_the_requested_native_target(
    env, path, route, foreign_ack
):
    store, session, journal = env
    target = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Public synthetic existing work",
        state="running",
        idempotency_key="existing",
    )
    host = ASGIAgent(session, task=target)
    host.ack_mutation = {"task_id": "different-native-task"} if foreign_ack else None
    source = source_entry(store, session, path)
    worker, event = routed_worker(store, session, journal, source, path, host.client, route, target)
    try:
        await worker.process(event)
        receipt = terminal_receipts(store, path)[0]
        assert len(host.posts) == 1
        if foreign_ack:
            assert receipt.payload["state"] == "unknown"
            assert "native_task_id" not in receipt.payload
        else:
            assert receipt.payload["state"] == ("accepted" if path == "typed" else "applied")
            assert receipt.payload["native_task_id"] == target.task_id
            assert receipt.payload["native_task_state"] == "cancel_requested"
    finally:
        await host.client.close()


async def test_typed_correction_receipt_binds_original_sequence_and_current_revision(env):
    store, session, journal = env
    host = ASGIAgent(session)
    original = source_entry(store, session, "typed")
    correction = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.revision",
            payload={
                "target_event_id": original.event_id,
                "revision": 2,
                "text": "Corrected public synthetic request",
                "content_scope": "public_synthetic",
            },
            idempotency_key="correction",
        ),
        "owner",
    )
    worker = DecisionsWorker(journal, host.client, Labels())
    try:
        await worker.process(correction)
        receipt = terminal_receipts(store, "typed")[0]
        assert receipt.causes == [correction.event_id]
        assert receipt.payload["source_event_id"] == original.event_id
        assert receipt.payload["source_sequence"] == original.sequence
        assert receipt.payload["source_revision"] == 2
        assert host.posts[0][1]["source_event_ids"] == [original.event_id]
        assert host.posts[0][1]["source_revision"] == 2
    finally:
        await host.client.close()


async def test_typed_terminal_receipt_beyond_one_page_and_legacy_rejection_prevent_replay(env):
    store, session, journal = env
    host = ASGIAgent(session)
    source = source_entry(store, session, "typed")
    decision = Decision(
        route="queue",
        source_event_ids=[source.event_id],
        instruction=source.payload["text"],
        idempotency_key="legacy-native-key",
    )
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="routing.decided",
            payload=decision.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key=decision.idempotency_key,
        ),
        "decisions",
    )
    for i in range(201):
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="routing.dispatch",
                payload={"decision_id": f"unrelated-{i}", "state": "unknown"},
                causes=[source.event_id],
                idempotency_key=f"unrelated-{i}",
            ),
            "decisions",
        )
    # Historical rejection shape intentionally has no new identity fields.
    store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="routing.dispatch",
            payload={
                "decision_id": decision.decision_id,
                "state": "rejected",
                "reason": "source_or_target_changed",
                "target_task_id": None,
            },
            causes=[source.event_id],
            idempotency_key=f"dispatch-rejected:{decision.decision_id}",
        ),
        "decisions",
    )
    worker = DecisionsWorker(journal, host.client, Labels())
    try:
        await worker.process(source)
        assert not host.posts and worker.classifier.calls == 0
    finally:
        await host.client.close()


async def test_window_queue_target_boundary_ack_identifies_the_new_task(env):
    store, session, journal = env
    prior = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Public synthetic completed work",
        state="succeeded",
        idempotency_key="prior",
    )
    host = ASGIAgent(session, task=prior)
    source = source_entry(store, session, "window")
    worker, event = routed_worker(
        store, session, journal, source, "window", host.client, task=prior
    )
    try:
        await worker.process(event)
        receipt = terminal_receipts(store, "window")[0]
        assert receipt.payload["state"] == "applied"
        assert receipt.payload["target_task_id"] == prior.task_id
        assert receipt.payload["native_task_id"] == host.tasks[-1].task_id != prior.task_id
    finally:
        await host.client.close()


@pytest.mark.parametrize("route", ["steer", "interrupt"])
async def test_window_unavailable_target_status_is_not_a_fabricated_missing_target(env, route):
    store, session, journal = env
    target = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Public synthetic existing work",
        state="running",
        idempotency_key="existing",
    )
    host = ASGIAgent(session, task=target)
    host.status_unavailable = True
    source = source_entry(store, session, "window")
    worker, event = routed_worker(
        store, session, journal, source, "window", host.client, route, target
    )
    try:
        await worker.process(event)
        receipt = terminal_receipts(store, "window")[0]
        assert receipt.payload["state"] == "not_applied"
        assert receipt.payload["reason"] == "agent_status_unavailable"
        assert worker.last_error == "HTTPStatusError"
        assert not host.posts
    finally:
        await host.client.close()


@pytest.mark.parametrize("path", ["typed", "window"])
@pytest.mark.parametrize(
    "mutation",
    [
        {"session_id": "foreign-session"},
        {"thread_id": "foreign-thread"},
        {"epoch": 2},
        {"state": "untrusted-done"},
        {"idempotency_key": "another-submission"},
        {"task_id": ""},
        {"task_id": "x" * 129},
    ],
)
async def test_invalid_or_foreign_ack_is_unknown_never_fabricated_success(env, path, mutation):
    store, session, journal = env
    host = ASGIAgent(session)
    host.ack_mutation = mutation
    source = source_entry(store, session, path)
    worker, event = routed_worker(store, session, journal, source, path, host.client)
    try:
        await worker.process(event)
        await worker.process(event)
        receipt = terminal_receipts(store, path)[0]
        assert len(host.posts) == 1
        assert receipt.payload["state"] == "unknown"
        assert receipt.payload["reason"] == "executor_ack_unknown"
        assert "native_task_id" not in receipt.payload
    finally:
        await host.client.close()


@pytest.mark.parametrize("path", ["typed", "window"])
@pytest.mark.parametrize("over_http", [False, True])
async def test_native_agent_http_disclosure_refusal_remains_enforced(
    env, tmp_path, path, over_http
):
    store, session, journal = env
    provider = ScriptedProvider([])
    provider.info = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
    runtime = AgentRuntime(LocalJournal(store, "agent"), provider, tmp_path, ToolPolicy(tmp_path))
    commands = Registry(runtime)
    client = AgentClient(
        "http://native-agent",
        AUTH.tokens["decisions"],
        transport=httpx.ASGITransport(app=agent_app(runtime, AUTH, commands)),
    )
    source = source_entry(store, session, path, scope="local_only")
    worker, event = routed_worker(
        store, session, journal, source, path, client if over_http else runtime
    )
    try:
        await worker.process(event)
        receipt = terminal_receipts(store, path)[0]
        assert receipt.payload["state"] == "rejected"
        if over_http:
            assert receipt.payload["http_status"] == 403
        else:
            assert "http_status" not in receipt.payload
        assert receipt.payload["reason"] == "executor_rejected"
        assert not provider.calls and not await runtime.status()
        assert not store.read(kinds=["agent.command"])
        assert "disclosure" not in json.dumps(receipt.payload)
    finally:
        await client.close()
        await runtime.close()
        commands.close()


class WindowLabels:
    async def classify(self, *_):
        return {
            "conversation": "WAIT_FOR_NEXT_WINDOW",
            "agent_route": "QUEUE_UNTIL_TASK_DONE",
            "reason_code": "synthetic_work_request",
        }


@pytest.mark.parametrize("fence", [None, "superseded", "reset"])
async def test_status_failure_preserves_intent_but_never_submits_until_current_boundary(env, fence):
    store, session, journal = env
    host = ASGIAgent(session)
    host.status_unavailable = True
    source = source_entry(store, session, "window")
    worker = WindowDecisionsWorker(journal, host.client, WindowLabels())
    dispatcher = WindowAgentDispatcher(journal, host.client)
    try:
        await worker.classify(source)
        event = store.window_decision(source.event_id)
        assert event.payload["agent_route"] == "QUEUE_UNTIL_TASK_DONE"
        assert event.payload["reason_code"] == "synthetic_work_request"
        assert worker.agent_available is False
        statuses = store.read(kinds=["routing.window_status"])
        unavailable = next(e for e in statuses if e.payload["phase"] == "agent_unavailable")
        assert unavailable.payload["reason"] == "agent_status_unavailable"
        assert unavailable.payload["source_sequence"] == source.sequence
        assert unavailable.payload["source_revision"] == 1
        await dispatcher.process(event)
        assert not host.posts
        assert not terminal_receipts(store, "window")
        assert store.window_dispatches(pending_only=True)
        if fence == "superseded":
            source_entry(store, session, "window", index=1)
        elif fence == "reset":
            store.reset(session.session_id)
        host.status_unavailable = False
        restarted = WindowAgentDispatcher(journal, host.client)
        await restarted.tick()
        await restarted.tick()
        assert len(host.posts) == (1 if fence is None else 0)
        if fence is None:
            assert terminal_receipts(store, "window")[-1].payload["state"] == "applied"
    finally:
        await host.client.close()
        await worker.close()


async def test_explicit_agent_disabled_still_ignores_work_without_future_replay(env):
    store, session, journal = env
    host = ASGIAgent(session)
    source = source_entry(store, session, "window")
    worker = WindowDecisionsWorker(journal, host.client, WindowLabels(), agent_enabled=False)
    try:
        await worker.classify(source)
        event = store.window_decision(source.event_id)
        assert event.payload["agent_route"] == "IGNORE_FOR_IMMEDIATE_ROUTING"
        worker.agent_enabled = True
        await worker.classify(source)
        await WindowAgentDispatcher(journal, host.client).process(event)
        assert not host.posts and worker.agent_available is None
    finally:
        await host.client.close()
        await worker.close()


@pytest.mark.parametrize(
    "field,bad",
    [
        ("source_sequence", 2),
        ("source_revision", 2),
        ("source_sequence", True),
        ("source_revision", True),
    ],
)
def test_status_optional_identity_is_validated_against_authentic_window(env, field, bad):
    store, session, _ = env
    source = source_entry(store, session, "window")
    payload = {
        "source_event_id": source.event_id,
        "phase": "agent_unavailable",
        "reason": "agent_status_unavailable",
        field: bad,
    }
    with pytest.raises(JournalError):
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="routing.window_status",
                payload=payload,
                causes=[source.event_id],
                idempotency_key=f"bad:{field}:{bad}",
            ),
            "decisions",
        )


async def test_health_reports_observed_agent_status_and_dispatcher_error_without_claiming_ready(
    env,
):
    _, _, journal = env
    provider = ScriptedProvider([])
    typed = SimpleNamespace(
        classifier=SimpleNamespace(provider=provider),
        running=True,
        enabled=True,
        last_error=None,
        journal=journal,
        consumer="decisions-v1",
    )
    window = SimpleNamespace(agent_available=False, last_error=None)
    dispatcher = SimpleNamespace(last_error="HTTPStatusError", running=True)
    async with httpx.AsyncClient(
        base_url="http://decisions",
        transport=httpx.ASGITransport(app=decisions_app(typed, AUTH, window, dispatcher)),
    ) as client:
        health = (await client.get("/health")).json()
        assert health["window_agent_available"] is False
        assert health["window_dispatch_error"] == "HTTPStatusError"
        assert health["window_dispatch_running"] is True
        assert (await client.get("/v1/decisions/status")).status_code == 401
        status = await client.get(
            "/v1/decisions/status", headers={"Authorization": f"Bearer {AUTH.tokens['owner']}"}
        )
        assert status.json()["window_agent_available"] is False
