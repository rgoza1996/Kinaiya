"""Multi-task steering and interruption task-description projection tests (#339)."""

import json

import httpx
import pytest

from packages.contracts.models import (
    AppendRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    Task,
)
from packages.providers.laya import MAX_TASK_DESCRIPTION_CHARS, LayaRoutingProvider
from packages.providers.laya_artifact import ARTIFACT_HEADER
from packages.providers.native_d1 import NativeD1RoutingProvider
from packages.providers.text import CompatibleTextProvider, ProviderRefusal
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.decisions.window_routing import WindowAgentDispatcher, WindowClassifier
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import LocalJournal, Store

DUMMY_ARTIFACT = "a" * 64


class FakeAgent:
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.submitted = []
        self.steered = []
        self.interrupted = []

    async def status(self):
        return list(self.tasks)

    async def submit(self, request, *, actor):
        self.submitted.append((request, actor))
        task = Task(
            task_id=f"task-{len(self.submitted)}",
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            objective=request.objective,
            content_scope=request.content_scope,
            submission_key=request.idempotency_key,
            source_event_ids=request.source_event_ids,
            source_revision=request.source_revision,
        )
        self.tasks.append(task)
        return task

    async def steer(self, task_id, request, *, actor):
        self.steered.append((task_id, request, actor))
        target = next(t for t in self.tasks if t.task_id == task_id)
        if target.objective_revision != request.expected_objective_revision:
            raise ValueError("Stale task revision")
        updated = target.model_copy(update={"objective_revision": target.objective_revision + 1})
        self.tasks = [updated if t.task_id == task_id else t for t in self.tasks]
        return updated

    async def interrupt(self, task_id, request, *, actor):
        self.interrupted.append((task_id, request, actor))
        target = next(t for t in self.tasks if t.task_id == task_id)
        if target.objective_revision != request.expected_objective_revision:
            raise ValueError("Stale task revision")
        updated = target.model_copy(update={"state": "cancelled"})
        self.tasks = [updated if t.task_id == task_id else t for t in self.tasks]
        return updated


def _make_two_tasks(
    session, desc_a: str, desc_b: str, *, scope_a="public_synthetic", scope_b="public_synthetic"
):
    return [
        Task(
            task_id="task-a",
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective=desc_a,
            content_scope=scope_a,
            objective_revision=1,
            state="running",
            idempotency_key="key-a",
        ),
        Task(
            task_id="task-b",
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective=desc_b,
            content_scope=scope_b,
            objective_revision=1,
            state="running",
            idempotency_key="key-b",
        ),
    ]


@pytest.mark.parametrize("mode", ["finalized", "window"])
@pytest.mark.parametrize(
    ("desc_a", "desc_b", "expected_target"),
    [
        ("Build the quarterly spreadsheet", "Render the sales chart", "task-a"),
        ("Render the sales chart", "Build the quarterly spreadsheet", "task-b"),
    ],
)
async def test_multi_task_swapped_descriptions_route_to_exact_native_target(
    tmp_path, mode, desc_a, desc_b, expected_target
):
    """Audit KIN-05 counterexample: swapping two task descriptions routes to the matching task."""
    captured_requests = []

    def respond(request):
        payload = json.loads(request.content)
        user_state = json.loads(payload["messages"][1]["content"])
        captured_requests.append(user_state)
        matched = next(
            t for t in user_state["tasks"] if "spreadsheet" in t.get("description", "").lower()
        )
        if mode == "finalized":
            out = {
                "route": "interrupt",
                "target_task_id": matched["task_id"],
                "expected_objective_revision": matched["objective_revision"],
                "reason_code": "cancel_spreadsheet",
                "confidence": 0.95,
            }
        else:
            out = {
                "conversation": "RESPOND_INTERRUPT_USER",
                "agent_route": "INTERRUPT_CURRENT_TASK",
                "target_task_id": matched["task_id"],
                "expected_objective_revision": matched["objective_revision"],
                "reason_code": "cancel_spreadsheet",
            }
        body = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(out)},
                    "finish_reason": "stop",
                }
            ]
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        agent = FakeAgent(_make_two_tasks(session, desc_a, desc_b))
        journal = LocalJournal(store, "decisions")

        if mode == "finalized":
            store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.user",
                    payload={
                        "text": "Cancel the spreadsheet task, keep the chart running.",
                        "revision": 1,
                        "final": True,
                        "content_scope": "public_synthetic",
                    },
                    idempotency_key="cancel-spreadsheet",
                ),
                "owner",
            )
            worker = DecisionsWorker(journal, agent, ModelClassifier(provider))
            await worker.tick()
        else:
            win = SpeechWindow(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                utterance_id="utt-cancel",
                window_index=0,
                revision=1,
                text="Cancel the spreadsheet task, keep the chart running.",
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                capture_ended=True,
                content_scope="public_synthetic",
            )
            entry = store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.window",
                    payload=win.model_dump(mode="json"),
                    idempotency_key="win-cancel",
                ),
                "owner",
            )
            worker = WindowDecisionsWorker(journal, agent, WindowClassifier(provider))
            await worker.classify(entry)
            await WindowAgentDispatcher(journal, agent).tick()

        assert len(captured_requests) == 1
        assert captured_requests[0]["tasks"] == [
            {
                "task_id": "task-a",
                "objective_revision": 1,
                "state": "running",
                "description": desc_a,
            },
            {
                "task_id": "task-b",
                "objective_revision": 1,
                "state": "running",
                "description": desc_b,
            },
        ]
        assert len(agent.interrupted) == 1
        assert agent.interrupted[0][0] == expected_target
        assert agent.interrupted[0][1].expected_objective_revision == 1
    finally:
        await provider.close()
        store.close()


@pytest.mark.parametrize("adapter_kind", ["laya", "native_d1"])
async def test_laya_and_native_d1_adapters_preserve_multi_task_descriptions_and_choices(
    tmp_path, adapter_kind
):
    seen_bodies = []

    def respond(request):
        body = json.loads(request.content)
        seen_bodies.append(body)
        target_criteria = body["questions"]["target"]["criteria"]
        selected_key = next(
            k for k, desc in target_criteria.items() if "spreadsheet" in desc.lower()
        )
        target_probs = {k: (1.0 if k == selected_key else 0.0) for k in target_criteria}
        route_probs = {
            "conversation": 0.0,
            "correction": 0.0,
            "cancellation": 1.0,
            "new_task": 0.0,
        }
        if adapter_kind == "laya":
            resp = {
                "routing": {"model": "english"},
                "usage": {
                    "truncated": False,
                    "state_tokens_dropped": 0,
                    "truncated_questions": [],
                },
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "cancellation",
                        "probabilities": route_probs,
                        "answer_confidence": 1.0,
                    },
                    "target": {
                        "type": "choice",
                        "choice": selected_key,
                        "probabilities": target_probs,
                        "answer_confidence": 1.0,
                    },
                },
            }
            return httpx.Response(
                200,
                headers={ARTIFACT_HEADER: DUMMY_ARTIFACT},
                stream=httpx.ByteStream(json.dumps(resp).encode()),
            )
        resp = {
            "model": "lfm3-d1",
            "usage": {"input_tokens": 32, "output_tokens": 0},
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "cancellation",
                    "probabilities": route_probs,
                    "confidence": 1.0,
                },
                "target": {
                    "type": "choice",
                    "choice": selected_key,
                    "probabilities": target_probs,
                    "confidence": 1.0,
                },
            },
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(resp).encode()))

    provider = (
        LayaRoutingProvider(
            "http://127.0.0.1:8870",
            "english",
            expected_artifact_sha256=DUMMY_ARTIFACT,
            transport=httpx.MockTransport(respond),
        )
        if adapter_kind == "laya"
        else NativeD1RoutingProvider(
            "http://127.0.0.1:8870",
            "lfm3-d1",
            min_confidence=0.6,
            transport=httpx.MockTransport(respond),
        )
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        tasks = _make_two_tasks(
            session, "Render the sales chart", "Build the quarterly spreadsheet"
        )
        source = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.user",
                payload={
                    "text": "Cancel the spreadsheet task.",
                    "revision": 1,
                    "final": True,
                    "content_scope": "public_synthetic",
                },
                idempotency_key="native-multi",
            ),
            "owner",
        )
        result = await ModelClassifier(provider).classify(source, [], tasks)
        assert result["route"] == "interrupt"
        assert result["target_task_id"] == "task-b"
        assert result["expected_objective_revision"] == 1
        assert "Build the quarterly spreadsheet" in seen_bodies[0]["state"]
        assert (
            "Build the quarterly spreadsheet"
            in seen_bodies[0]["questions"]["target"]["criteria"]["t1"]
        )
    finally:
        await provider.close()
        store.close()


@pytest.mark.parametrize("mode", ["finalized", "window"])
async def test_private_task_descriptions_withheld_from_cloud_and_guessed_target_refused_as_ambiguous(
    tmp_path, mode
):
    seen_requests = []

    def respond(request):
        payload = json.loads(request.content)
        seen_requests.append(payload)
        # Simulate a cloud model guessing task-a even though task-a's private description was withheld.
        if mode == "finalized":
            out = {
                "route": "steer",
                "target_task_id": "task-a",
                "expected_objective_revision": 1,
                "reason_code": "guessed_private_target",
            }
        else:
            out = {
                "conversation": "RESPOND_WITHOUT_INTERRUPTING_USER",
                "agent_route": "STEER_CURRENT_TASK",
                "target_task_id": "task-a",
                "expected_objective_revision": 1,
                "reason_code": "guessed_private_target",
            }
        body = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(out)},
                    "finish_reason": "stop",
                }
            ]
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-contributor", transport=httpx.MockTransport(respond)
    )
    provider.info = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        tasks = _make_two_tasks(
            session,
            "PRIVATE_LOCAL_SPREADSHEET_OBJECTIVE",
            "Public chart objective",
            scope_a="local_only",
            scope_b="public_synthetic",
        )
        agent = FakeAgent(tasks)
        journal = LocalJournal(store, "decisions")

        if mode == "finalized":
            entry = store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.user",
                    payload={
                        "text": "Update the spreadsheet task.",
                        "revision": 1,
                        "final": True,
                        "content_scope": "public_synthetic",
                    },
                    idempotency_key="cloud-steer",
                ),
                "owner",
            )
            worker = DecisionsWorker(journal, agent, ModelClassifier(provider))
            await worker.tick()
            decided = store.read(kinds=["routing.decided"])
            assert len(decided) == 1
            assert decided[0].payload["route"] == "ignore"
            assert decided[0].payload["reason_code"] == "ambiguous_target"
        else:
            win = SpeechWindow(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                utterance_id="utt-cloud-steer",
                window_index=0,
                revision=1,
                text="Update the spreadsheet task.",
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                capture_ended=True,
                content_scope="public_synthetic",
            )
            entry = store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.window",
                    payload=win.model_dump(mode="json"),
                    idempotency_key="win-cloud-steer",
                ),
                "owner",
            )
            worker = WindowDecisionsWorker(journal, agent, WindowClassifier(provider))
            await worker.classify(entry)
            await WindowAgentDispatcher(journal, agent).tick()
            decision = store.window_decision(entry.event_id).payload
            assert decision["agent_route"] == "IGNORE_FOR_IMMEDIATE_ROUTING"
            assert decision["target_task_id"] is None
            assert decision["reason_code"] == "ambiguous_target"

        assert len(seen_requests) == 1
        wire_dump = json.dumps(seen_requests)
        assert "PRIVATE_LOCAL_SPREADSHEET_OBJECTIVE" not in wire_dump
        assert "Public chart objective" in wire_dump
        detail = store.decisions.detail(entry.event_id)
        assert "PRIVATE_LOCAL_SPREADSHEET_OBJECTIVE" not in json.dumps(detail)
        assert not agent.steered
    finally:
        await provider.close()
        store.close()


async def test_task_description_bounds_and_streaming_cache_invalidation_on_description_swap(
    tmp_path,
):
    seen_bodies = []

    def respond(request):
        payload = json.loads(request.content)
        seen_bodies.append(json.loads(payload["messages"][1]["content"]))
        return httpx.Response(
            200,
            stream=httpx.ByteStream(
                json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        {
                                            "conversation": "WAIT_FOR_NEXT_WINDOW",
                                            "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                                            "target_task_id": None,
                                            "expected_objective_revision": None,
                                            "reason_code": f"call_{len(seen_bodies)}",
                                        }
                                    ),
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ).encode()
            ),
        )

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        long_desc = "A" * 400
        tasks = _make_two_tasks(session, long_desc, "Second task")
        agent = FakeAgent(tasks)
        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"), agent, WindowClassifier(provider)
        )

        w0 = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="u-desc-cache",
            window_index=0,
            revision=1,
            text="Check the active tasks",
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
        e0 = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window",
                payload=w0.model_dump(mode="json"),
                idempotency_key="w0-desc",
            ),
            "owner",
        )
        await worker.classify(e0)
        assert len(seen_bodies) == 1
        assert len(seen_bodies[0]["tasks"][0]["description"]) == MAX_TASK_DESCRIPTION_CHARS

        # Swap descriptions across identical task IDs/revisions/states -> must invalidate window cache.
        agent.tasks = _make_two_tasks(session, "Second task", long_desc)
        w1 = w0.model_copy(update={"window_index": 1, "end_ms": 1500})
        e1 = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window",
                payload=w1.model_dump(mode="json"),
                idempotency_key="w1-desc",
            ),
            "owner",
        )
        await worker.classify(e1)
        assert len(seen_bodies) == 2
        assert seen_bodies[1]["tasks"][0]["description"] == "Second task"
        assert store.window_decision(e1.event_id).payload["classification_reused"] is False

        # Oversized raw description in Laya parser is rejected.
        laya = LayaRoutingProvider("http://127.0.0.1:8870", "english")
        try:
            with pytest.raises(ProviderRefusal, match="tasks are malformed"):
                laya._parse_messages(
                    [
                        {"role": "system", "content": "sys"},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "context": [],
                                    "latest": {"kind": "conversation.user", "text": "hi"},
                                    "tasks": [
                                        {
                                            "task_id": "t1",
                                            "objective_revision": 1,
                                            "state": "running",
                                            "description": "X" * (MAX_TASK_DESCRIPTION_CHARS + 1),
                                        }
                                    ],
                                }
                            ),
                        },
                    ]
                )
        finally:
            await laya.close()
    finally:
        await provider.close()
        store.close()
