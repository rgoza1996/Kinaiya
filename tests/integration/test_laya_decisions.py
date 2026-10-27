"""Native classifier/loader integration with an explicit synthetic Jev transport."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow, Task, TaskState
from packages.providers.laya import LayaRoutingProvider
from packages.providers.text import ProviderRefusal, ScriptedProvider
from services.common.auth import Auth
from services.decisions.api import create_app
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.decisions.window_routing import WindowClassifier
from services.launcher import provider as configure_provider
from services.session.store import LocalJournal, Store


def _transport(*, window=True, request_observer=None):
    def handler(request):
        if request.url.path == "/health":
            body = {
                "status": "ok",
                "loaded": ["english"],
                "revisions": {"english": "a" * 40},
                "device": "cpu",
                "device_is_preference": False,
            }
        else:
            body = json.loads(request.content)
            if request_observer:
                request_observer(body)
            assert body["model"] == "english"
            assert all(
                set(q) == {"type", "instructions", "criteria"} for q in body["questions"].values()
            )
            assert isinstance(body["state"], str) and "Latest transcript" in body["state"]
            picks = {
                "conversation": "ready",
                "agent_route": "correction",
                "route": "correction",
                "target": "t0",
            }
            answers = {}
            for name, question in body["questions"].items():
                labels = list(question["criteria"])
                selected = picks[name]
                assert selected in labels
                answers[name] = {
                    "type": "choice",
                    "choice": selected,
                    "probabilities": {k: float(k == selected) for k in labels},
                    "answer_confidence": 1.0,
                }
            body = {
                "answers": answers,
                "usage": {"truncated": False, "state_tokens_dropped": 0, "truncated_questions": []},
                "routing": {"model": "english"},
            }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))

    return httpx.MockTransport(handler)


def _task(session):
    return Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        objective="Public synthetic current task",
        state=TaskState.RUNNING,
        objective_revision=3,
        idempotency_key="current",
    )


@pytest.mark.parametrize("mode", ["window", "finalized"])
async def test_native_classifier_preserves_source_kind_and_task_binding(tmp_path, mode):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    task = _task(session)
    seen = []
    p = LayaRoutingProvider(
        "http://127.0.0.1:8870", transport=_transport(request_observer=seen.append)
    )
    try:
        text = "Also include the documentation."
        payload = (
            SpeechWindow(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                utterance_id="public",
                window_index=0,
                text=text,
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                content_scope="public_synthetic",
            ).model_dump(mode="json")
            if mode == "window"
            else {"text": text, "content_scope": "public_synthetic", "final": True}
        )
        source = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window" if mode == "window" else "conversation.user",
                payload=payload,
                idempotency_key="source",
            ),
            "owner",
        )
        classifier = WindowClassifier(p) if mode == "window" else ModelClassifier(p)
        result = await classifier.classify(source, [], [task])
        assert result["agent_route" if mode == "window" else "route"] == (
            "STEER_CURRENT_TASK" if mode == "window" else "steer"
        )
        assert (
            result["target_task_id"] == task.task_id and result["expected_objective_revision"] == 3
        )
        assert text in seen[0]["state"]
    finally:
        await p.close()
        store.close()


async def test_long_message_drops_context_without_truncating_current_intent(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    entries = []
    for i in range(12):
        entries.append(
            store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.user",
                    payload={
                        "text": "z" * 2000,
                        "content_scope": "public_synthetic",
                        "final": True,
                    },
                    idempotency_key=f"context-{i}",
                ),
                "owner",
            )
        )
    text = "Public synthetic current request. " + "x" * 15900
    source = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.user",
            payload={"text": text, "content_scope": "public_synthetic", "final": True},
            idempotency_key="long",
        ),
        "owner",
    )
    fixture = ScriptedProvider([json.dumps({"route": "ignore"})])
    try:
        await ModelClassifier(fixture, window_chars=17000).classify(source, entries, [])
        body = json.loads(fixture.calls[0][1]["content"])
        assert body["context"] == [] and body["latest"] == {
            "kind": "conversation.user",
            "text": text,
        }
        with pytest.raises(ProviderRefusal, match="context bound"):
            await ModelClassifier(fixture, window_chars=100).classify(source, entries, [])
        assert len(fixture.calls) == 1
    finally:
        store.close()


def test_laya_loader_is_decisions_only_and_uses_separate_explicit_key(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_LAYA_KEY", "public-test-laya-key")
    config = {
        "providers": {
            role: {
                "kind": "laya",
                "base_url": "http://127.0.0.1:8870",
                "model": "english",
                "api_key_env": "SYNTHETIC_LAYA_KEY",
            }
            for role in ["agent", "converse", "decisions"]
        }
    }
    for role in ["agent", "converse"]:
        with pytest.raises(SystemExit, match="typed Decisions provider"):
            configure_provider(config, role)
    p = configure_provider(config, "decisions")
    assert p.info.provider == "laya" and p.info.local and not p.info.streaming
    assert p.http.headers["authorization"] == "Bearer public-test-laya-key"
    asyncio.run(p.close())


async def test_decisions_health_reports_actual_provider_readiness():
    p = LayaRoutingProvider("http://127.0.0.1:8870", transport=_transport())
    worker = SimpleNamespace(
        classifier=SimpleNamespace(provider=p), running=True, enabled=True, last_error=None
    )
    auth = Auth({"owner": "synthetic-owner-for-health-00000"})
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(worker, auth)), base_url="http://fixture"
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["provider_readiness"] == {
            "ready": True,
            "model": "english",
            "revision": "a" * 40,
            "device": "cpu",
        }
    finally:
        await p.close()


async def test_decisions_health_does_not_claim_loaded_model_when_endpoint_down():
    async def down(request):
        raise httpx.ConnectError("synthetic unavailable")

    p = LayaRoutingProvider("http://127.0.0.1:8870", transport=httpx.MockTransport(down))
    worker = SimpleNamespace(
        classifier=SimpleNamespace(provider=p), running=True, enabled=True, last_error=None
    )
    auth = Auth({"owner": "synthetic-owner-for-health-00000"})
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(worker, auth)), base_url="http://fixture"
        ) as client:
            r = await client.get("/health")
        assert r.status_code == 200 and r.json()["provider_readiness"]["ready"] is False
    finally:
        await p.close()


async def test_truncated_finalized_source_is_reported_without_blocking_later_work(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        body = {
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "new_task",
                    "probabilities": {
                        "conversation": 0.0,
                        "correction": 0.0,
                        "cancellation": 0.0,
                        "new_task": 1.0,
                    },
                    "answer_confidence": 1.0,
                }
            },
            "usage": {
                "truncated": len(calls) == 1,
                "state_tokens_dropped": 1 if len(calls) == 1 else 0,
                "truncated_questions": [],
            },
            "routing": {"model": "english"},
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))

    class Agent:
        submitted = []

        async def status(self):
            return []

        async def submit(self, request, actor):
            self.submitted.append(request)

    source_ids = []
    for index, text in enumerate(["Public synthetic long instruction", "Create a sample file"]):
        entry = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.user",
                payload={"text": text, "final": True, "content_scope": "public_synthetic"},
                idempotency_key=f"source-{index}",
            ),
            "owner",
        )
        source_ids.append(entry.event_id)
    # A receipt beyond one journal page must still stop reclassification after
    # a crash between persistence and cursor acknowledgment.
    for index in range(205):
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="routing.decided",
                payload={"route": "ignore", "reason_code": "synthetic_unrelated_receipt"},
                idempotency_key=f"unrelated-{index}",
            ),
            "decisions",
        )
    provider = LayaRoutingProvider("http://127.0.0.1:8870", transport=httpx.MockTransport(handler))
    agent = Agent()
    worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))
    try:
        await worker.tick()
        await worker.tick()
        await worker.process(store.entry(source_ids[0]))
        assert len(calls) == 2 and len(agent.submitted) == 1
        assert agent.submitted[0].source_event_ids == [source_ids[1]]
        refused = store.read(kinds=["routing.classification_refused"])
        assert len(refused) == 1 and refused[0].causes == [source_ids[0]]
        assert refused[0].payload == {
            "source_event_id": source_ids[0],
            "source_revision": 1,
            "reason": "provider_refused",
            "error_type": "ProviderRefusal",
        }
        decisions = [
            e for e in store.read(after=205, kinds=["routing.decided"], limit=200) if e.causes
        ]
        assert len(decisions) == 1 and decisions[0].causes == [source_ids[1]]
    finally:
        await provider.close()
        store.close()
