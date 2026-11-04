"""Source-aware #337 regressions; synthetic HTTP and native temporary journal only.

Task snapshots are controlled status metadata, not Agent admission/execution proof.
The same-source fault refuses one decision append before native dispatch; it does
not pretend that a committed decision's ACK was lost.
"""

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow, Task
from packages.providers.text import CompatibleTextProvider
from services.decisions.model_settings import DecisionsModelSettings
from services.decisions.window_routing import WindowClassifier
from services.decisions.window_worker import JournalUnavailable, WindowDecisionsWorker
from services.session.store import LocalJournal, Store


class _SnapshotAgent:
    """Same status-only fixture pattern as test_window_cache_identity.FakeAgent."""

    def __init__(self, tasks):
        self.tasks = tuple(tasks)

    async def status(self):
        return [task.model_copy(deep=True) for task in self.tasks]


def _tasks(session, count):
    return tuple(
        Task(
            task_id=f"00000000-0000-4000-8000-{index:012d}",
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="Synthetic status metadata, never executed",
            objective_revision=1,
            state="running",
            idempotency_key=f"snapshot-{index}",
        )
        for index in range(1, count + 1)
    )


def _task_refs(tasks, *, include_descriptions=True):
    return [
        {
            "task_id": task.task_id,
            "objective_revision": 1,
            "state": "running",
            **(
                {"description": task.objective.strip()[:240]}
                if include_descriptions and len(tasks) > 1
                else {}
            ),
        }
        for task in tasks
    ]


def _expected_request(text, task_refs, context):
    # Explicit protocol vector, with no call to the production request builder.
    return {
        "latest": {
            "text": text,
            "user_speaking": False,
            "capture_ended": True,
            "utterance_id": "bound-default",
            "window_index": 0,
            "revision": 1,
        },
        "context": context,
        "tasks": task_refs,
    }


def _window(store, session, text):
    window = SpeechWindow(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        utterance_id="bound-default",
        window_index=0,
        revision=1,
        text=text,
        start_ms=0,
        end_ms=1000,
        user_speaking=False,
        capture_ended=True,
        content_scope="public_synthetic",
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.window",
            payload=window.model_dump(mode="json"),
            idempotency_key="original-window",
        ),
        "owner",
    )


def _context(store, session, text, key):
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={
                "text": text,
                "revision": 1,
                "final": True,
                "content_scope": "public_synthetic",
            },
            idempotency_key=key,
        ),
        "owner",
    )


@asynccontextmanager
async def _environment(tmp_path, *, model_owner=False, cloud=False):
    wire_bodies = []

    def respond(request):
        wire_bodies.append(json.loads(request.content))
        labels = {
            "conversation": "WAIT_FOR_NEXT_WINDOW",
            "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
            "target_task_id": None,
            "expected_objective_revision": None,
            "reason_code": f"observed_{len(wire_bodies)}",
        }
        return httpx.Response(
            200,
            stream=httpx.ByteStream(
                json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": json.dumps(labels)},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ).encode()
            ),
        )

    provider = CompatibleTextProvider(
        "https://synthetic.invalid/v1" if cloud else "http://localhost/v1",
        "synthetic-window-bound",
        allow_cloud=cloud,
        transport=httpx.MockTransport(respond),
    )
    store = Store(tmp_path / "journal.sqlite")
    settings = None
    try:
        settings = DecisionsModelSettings(provider, None) if model_owner else None
        session = store.create_session(SessionRequest(channel="converse"))
        yield store, session, provider, settings, wire_bodies
    finally:
        if settings is not None:
            await settings.close()
        else:
            await provider.close()
        store.close()


@pytest.mark.parametrize("model_owner", [False, True], ids=["direct-host", "native-model-owner"])
async def test_final_bound_refusal_never_becomes_an_empty_task_request(tmp_path, model_owner):
    async with _environment(tmp_path, model_owner=model_owner) as env:
        store, session, provider, settings, wire_bodies = env
        tasks = _tasks(session, 5)
        text = "\u0100" * 5860 + "x" * 500
        expected = _expected_request(text, _task_refs(tasks), [])
        assert len(text) == 6360 < 16000
        assert len(json.dumps(expected)) > 36000
        assert len(json.dumps(_expected_request(text, [], []))) <= 36000
        source = _window(store, session, text)
        classifier = WindowClassifier(provider)
        assert classifier.window_chars == 36000
        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"),
            _SnapshotAgent(tasks),
            classifier,
            model_settings=settings,
        )
        with pytest.raises(ValueError, match="^Current window exceeds classifier context bound$"):
            await worker.classify(source)
        assert wire_bodies == []
        assert not worker.outputs and not worker.output_models
        assert store.window_decision(source.event_id) is None
        assert store.decisions.detail(source.event_id)["calls"] == []


@pytest.mark.parametrize("model_owner", [False, True], ids=["direct-host", "native-model-owner"])
async def test_fitting_default_overflow_sends_the_exact_five_task_body(tmp_path, model_owner):
    async with _environment(tmp_path, model_owner=model_owner) as env:
        store, session, provider, settings, wire_bodies = env
        history = ["h" * 2000 for _ in range(12)]
        for index, text in enumerate(history):
            _context(store, session, text, f"history-{index}")
        tasks = _tasks(session, 8)
        text = "p" * 12000
        full = _expected_request(
            text,
            _task_refs(tasks),
            [{"kind": "conversation.user", "text": value} for value in history],
        )
        folded = _expected_request(text, _task_refs(tasks[:5]), [])
        assert len(json.dumps(full)) > 36000
        assert len(json.dumps(folded)) <= 36000
        source = _window(store, session, text)
        classifier = WindowClassifier(provider)
        assert classifier.window_chars == 36000
        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"),
            _SnapshotAgent(tasks),
            classifier,
            model_settings=settings,
        )
        await worker.classify(source)
        assert len(wire_bodies) == 1
        actual = wire_bodies[0]["messages"][1]["content"]
        assert actual == json.dumps(folded)
        assert len(json.loads(actual)["tasks"]) == 5
        assert json.loads(actual)["context"] == []
        decision = store.window_decision(source.event_id).payload
        assert decision["classification_reused"] is False
        assert decision["reason_code"] == "observed_1"
        call = store.decisions.detail(source.event_id)["calls"][0]
        assert call["status"] == "completed" and call["request"] == wire_bodies[0]
        if model_owner:
            assert call["model_pin"]["selection"] == {"source": "host", "catalog_binding": None}
            assert call["model_pin"]["backend_id"] == settings.backend_id


@pytest.mark.parametrize("change", ["unchanged", "context-revision", "context-scope", "task-order"])
async def test_same_source_retry_binds_only_the_changed_eligible_input(
    tmp_path, monkeypatch, change
):
    async with _environment(tmp_path, cloud=True) as env:
        store, session, provider, settings, wire_bodies = env
        context_text = "Public earlier context version one"
        original_context = _context(store, session, context_text, "eligible-earlier-context")
        tasks = _tasks(session, 2)
        agent = _SnapshotAgent(tasks)
        source = _window(store, session, "Continue")
        original_source = source.model_dump(mode="json")
        assert original_context.sequence < source.sequence
        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"), agent, WindowClassifier(provider)
        )
        append = store.append
        refused_requests = []

        def refuse_one_decision(request, role):
            if request.kind == "routing.window_decided" and not refused_requests:
                refused_requests.append(request.model_copy(deep=True))
                # Known no-effect refusal, before the real native append is called.
                raise OSError("Synthetic decision append unavailable before dispatch")
            return append(request, role)

        with monkeypatch.context() as patch:
            patch.setattr(store, "append", refuse_one_decision)
            with pytest.raises(JournalUnavailable):
                await worker.classify(source, attempt=1)
        assert len(refused_requests) == 1
        assert refused_requests[0].causes == [source.event_id]
        assert store.window_decision(source.event_id) is None
        assert len(worker.outputs) == 1 and len(wire_bodies) == 1
        original_call = store.decisions.detail(source.event_id)["calls"][0]
        assert original_call["status"] == "completed"
        assert original_call["request"] == wire_bodies[0]
        assert json.loads(wire_bodies[0]["messages"][1]["content"])["context"] == [
            {"kind": "conversation.user", "text": context_text}
        ]

        if change in {"context-revision", "context-scope"}:
            revised = store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.revision",
                    payload={
                        "target_event_id": original_context.event_id,
                        "text": "Public earlier context version two"
                        if change == "context-revision"
                        else context_text,
                        "revision": 2,
                        "content_scope": "local_only"
                        if change == "context-scope"
                        else "public_synthetic",
                    },
                    idempotency_key="eligible-context-r2",
                ),
                "owner",
            )
            assert revised.sequence > source.sequence
            assert store.resolve(original_context.event_id).payload["revision"] == 2
        elif change == "task-order":
            agent.tasks = tuple(reversed(tasks))

        await worker.classify(source, attempt=2)
        assert store.entry(source.event_id).model_dump(mode="json") == original_source
        assert store.latest_window(session.session_id, session.epoch, "bound-default").event_id == (
            source.event_id
        )
        decision = store.window_decision(source.event_id).payload
        calls = store.decisions.detail(source.event_id)["calls"]
        assert len(calls) == 2
        newest, retained_original = calls
        assert retained_original["call_id"] == original_call["call_id"]
        assert retained_original["request"] == original_call["request"]
        if change == "unchanged":
            assert len(wire_bodies) == 1
            assert decision["classification_reused"] is True
            assert newest["status"] == "reused" and newest["request"] is None
            assert newest["reused_from_call_id"] == original_call["call_id"]
            assert newest["reused_from_source_event_id"] == source.event_id
            assert decision["reason_code"] == "observed_1"
        else:
            assert len(wire_bodies) == 2
            assert decision["classification_reused"] is False
            assert newest["status"] == "completed" and newest["request"] == wire_bodies[1]
            assert newest["call_id"] != original_call["call_id"]
            actual = json.loads(wire_bodies[1]["messages"][1]["content"])
            if change == "context-revision":
                assert actual["context"] == [
                    {"kind": "conversation.user", "text": "Public earlier context version two"}
                ]
            elif change == "context-scope":
                assert actual["context"] == []
            else:
                assert actual["tasks"] == _task_refs(
                    tuple(reversed(tasks)), include_descriptions=False
                )
            assert decision["reason_code"] == "observed_2"
