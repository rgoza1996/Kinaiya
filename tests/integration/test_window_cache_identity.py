"""Streaming Decisions classification cache identity and disclosure regression tests (#337)."""

import json

import httpx

from packages.contracts.models import (
    AppendRequest,
    ProviderInfo,
    SessionRequest,
    SpeechWindow,
    Task,
)
from packages.providers.text import CompatibleTextProvider
from services.decisions.window_routing import WindowClassifier
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import LocalJournal, Store


class FakeAgent:
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])

    async def status(self):
        return list(self.tasks)


def _append_window(
    store,
    session,
    *,
    utterance_id="utt-1",
    window_index=0,
    revision=1,
    text="yes",
    user_speaking=False,
    capture_ended=True,
    content_scope="public_synthetic",
    key=None,
):
    window = SpeechWindow(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        utterance_id=utterance_id,
        window_index=window_index,
        revision=revision,
        text=text,
        start_ms=0,
        end_ms=1000 + window_index * 500,
        user_speaking=user_speaking,
        capture_ended=capture_ended,
        content_scope=content_scope,
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.window",
            payload=window.model_dump(mode="json"),
            idempotency_key=key or f"win:{utterance_id}:{window_index}:r{revision}",
        ),
        "owner",
    )


def _chat_response(labels: dict) -> httpx.Response:
    body = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(labels)},
                "finish_reason": "stop",
            }
        ]
    }
    return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))


async def test_changed_context_with_identical_utterance_and_words_invalidates_cache(tmp_path):
    """Audit KIN-03 reproduction: identical utterance/words after changed context must not reuse."""
    seen_bodies = []

    def respond(request):
        payload = json.loads(request.content)
        user_body = json.loads(payload["messages"][1]["content"])
        seen_bodies.append(user_body)
        context_texts = [item["text"] for item in user_body["context"]]
        if any("create a table" in text.lower() for text in context_texts):
            return _chat_response(
                {
                    "conversation": "RESPOND_WITHOUT_INTERRUPTING_USER",
                    "agent_route": "QUEUE_UNTIL_TASK_DONE",
                    "target_task_id": None,
                    "expected_objective_revision": None,
                    "reason_code": "table_confirmed",
                }
            )
        return _chat_response(
            {
                "conversation": "RESPOND_WITHOUT_INTERRUPTING_USER",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "target_task_id": None,
                "expected_objective_revision": None,
                "reason_code": "explanation_ack",
            }
        )

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.converse",
                payload={
                    "text": "Did that explanation make sense?",
                    "final": True,
                    "content_scope": "public_synthetic",
                },
                idempotency_key="converse-1",
            ),
            "converse",
        )
        w0 = _append_window(store, session, utterance_id="u-yes", window_index=0, text="yes")

        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"), FakeAgent(), WindowClassifier(provider)
        )
        await worker.classify(w0)
        d0 = store.window_decision(w0.event_id).payload
        assert len(seen_bodies) == 1
        assert d0["classification_reused"] is False
        assert d0["agent_route"] == "IGNORE_FOR_IMMEDIATE_ROUTING"

        # Add new relevant conversation context while keeping current utterance/words/revision identical.
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.converse",
                payload={
                    "text": "Would you like me to create a table now?",
                    "final": True,
                    "content_scope": "public_synthetic",
                },
                idempotency_key="converse-2",
            ),
            "converse",
        )
        w1 = _append_window(store, session, utterance_id="u-yes", window_index=1, text="yes")
        await worker.classify(w1)

        d1 = store.window_decision(w1.event_id).payload
        assert len(seen_bodies) == 2
        assert d1["classification_reused"] is False
        assert d1["agent_route"] == "QUEUE_UNTIL_TASK_DONE"
        assert d1["reason_code"] == "table_confirmed"
    finally:
        await provider.close()
        store.close()


async def test_revised_historical_context_invalidates_cache_and_passes_revised_text(tmp_path):
    seen_bodies = []

    def respond(request):
        payload = json.loads(request.content)
        user_body = json.loads(payload["messages"][1]["content"])
        seen_bodies.append(user_body)
        return _chat_response(
            {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "target_task_id": None,
                "expected_objective_revision": None,
                "reason_code": f"call_{len(seen_bodies)}",
            }
        )

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        user_turn = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.user",
                payload={
                    "text": "Draft the initial outline",
                    "revision": 1,
                    "final": True,
                    "content_scope": "public_synthetic",
                },
                idempotency_key="user-turn-1",
            ),
            "owner",
        )
        w0 = _append_window(store, session, utterance_id="u-rev", window_index=0, text="Proceed")
        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"), FakeAgent(), WindowClassifier(provider)
        )
        await worker.classify(w0)
        assert len(seen_bodies) == 1
        assert seen_bodies[0]["context"] == [
            {"kind": "conversation.user", "text": "Draft the initial outline"}
        ]

        # Revise the historical user turn; next window with identical words must re-classify.
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.revision",
                payload={
                    "target_event_id": user_turn.event_id,
                    "text": "Draft the final architecture table",
                    "revision": 2,
                    "content_scope": "public_synthetic",
                },
                idempotency_key="user-turn-1-r2",
            ),
            "owner",
        )
        w1 = _append_window(store, session, utterance_id="u-rev", window_index=1, text="Proceed")
        await worker.classify(w1)

        assert len(seen_bodies) == 2
        assert seen_bodies[1]["context"] == [
            {"kind": "conversation.user", "text": "Draft the final architecture table"}
        ]
        assert store.window_decision(w1.event_id).payload["classification_reused"] is False
    finally:
        await provider.close()
        store.close()


async def test_task_order_changes_invalidate_cache_while_identical_order_reuses(tmp_path):
    seen_bodies = []

    def respond(request):
        payload = json.loads(request.content)
        seen_bodies.append(json.loads(payload["messages"][1]["content"]))
        return _chat_response(
            {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "target_task_id": None,
                "expected_objective_revision": None,
                "reason_code": f"order_{len(seen_bodies)}",
            }
        )

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        task_a = Task(
            task_id="task-a",
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="First task",
            objective_revision=1,
            state="running",
            idempotency_key="k-a",
        )
        task_b = Task(
            task_id="task-b",
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="Second task",
            objective_revision=1,
            state="running",
            idempotency_key="k-b",
        )
        agent = FakeAgent([task_a, task_b])
        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"), agent, WindowClassifier(provider)
        )

        w0 = _append_window(store, session, utterance_id="u-order", window_index=0, text="Hold on")
        await worker.classify(w0)
        assert len(seen_bodies) == 1
        assert [t["task_id"] for t in seen_bodies[0]["tasks"]] == ["task-a", "task-b"]

        # Swap task order; sorted tuple would have matched, but ordered projection must invalidate.
        agent.tasks = [task_b, task_a]
        w1 = _append_window(store, session, utterance_id="u-order", window_index=1, text="Hold on")
        await worker.classify(w1)
        assert len(seen_bodies) == 2
        assert [t["task_id"] for t in seen_bodies[1]["tasks"]] == ["task-b", "task-a"]
        assert store.window_decision(w1.event_id).payload["classification_reused"] is False

        # Keeping the same order and admissible input reuses the second call.
        w2 = _append_window(store, session, utterance_id="u-order", window_index=2, text="Hold on")
        await worker.classify(w2)
        assert len(seen_bodies) == 2
        assert store.window_decision(w2.event_id).payload["classification_reused"] is True
        detail_w1 = store.decisions.detail(w1.event_id)
        detail_w2 = store.decisions.detail(w2.event_id)
        assert detail_w2["calls"][0]["status"] == "reused"
        assert detail_w2["calls"][0]["reused_from_call_id"] == detail_w1["calls"][0]["call_id"]
        assert detail_w2["calls"][0]["reused_from_source_event_id"] == w1.event_id
    finally:
        await provider.close()
        store.close()


async def test_unrelated_journal_traffic_and_withheld_private_context_do_not_invalidate_cache(
    tmp_path,
):
    seen_bodies = []

    def respond(request):
        payload = json.loads(request.content)
        seen_bodies.append(payload)
        return _chat_response(
            {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "target_task_id": None,
                "expected_objective_revision": None,
                "reason_code": "public_cached",
            }
        )

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-contributor", transport=httpx.MockTransport(respond)
    )
    provider.info = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        other_session = store.create_session(SessionRequest(channel="converse"))
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.user",
                payload={
                    "text": "Public context turn",
                    "revision": 1,
                    "final": True,
                    "content_scope": "public_synthetic",
                },
                idempotency_key="pub-ctx",
            ),
            "owner",
        )
        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"), FakeAgent(), WindowClassifier(provider)
        )
        w0 = _append_window(
            store,
            session,
            utterance_id="u-cloud",
            window_index=0,
            text="Continue",
            content_scope="public_synthetic",
        )
        await worker.classify(w0)
        assert len(seen_bodies) == 1

        # 1) Traffic in another session, and 2) a local_only turn in the same session
        # that is withheld from the cloud provider must not cause unnecessary inference
        # or leak into the request/digest.
        store.append(
            AppendRequest(
                session_id=other_session.session_id,
                thread_id=other_session.thread_id,
                epoch=other_session.epoch,
                kind="conversation.user",
                payload={
                    "text": "Other session activity",
                    "revision": 1,
                    "final": True,
                    "content_scope": "public_synthetic",
                },
                idempotency_key="other-session-msg",
            ),
            "owner",
        )
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.user",
                payload={
                    "text": "FORBIDDEN_PRIVATE_LOCAL_ONLY_TEXT",
                    "revision": 1,
                    "final": True,
                    "content_scope": "local_only",
                },
                idempotency_key="private-local-msg",
            ),
            "owner",
        )
        w1 = _append_window(
            store,
            session,
            utterance_id="u-cloud",
            window_index=1,
            text="Continue",
            content_scope="public_synthetic",
        )
        await worker.classify(w1)

        assert len(seen_bodies) == 1
        assert "FORBIDDEN_PRIVATE_LOCAL_ONLY_TEXT" not in json.dumps(seen_bodies)
        assert store.window_decision(w1.event_id).payload["classification_reused"] is True
    finally:
        await provider.close()
        store.close()


async def test_revoked_context_disclosure_invalidates_cache_and_withholds_restricted_entry(
    tmp_path,
):
    seen_bodies = []

    def respond(request):
        payload = json.loads(request.content)
        seen_bodies.append(json.loads(payload["messages"][1]["content"]))
        return _chat_response(
            {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "target_task_id": None,
                "expected_objective_revision": None,
                "reason_code": f"scope_{len(seen_bodies)}",
            }
        )

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-contributor", transport=httpx.MockTransport(respond)
    )
    provider.info = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
    store = Store(tmp_path / "journal.sqlite")
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        ctx_entry = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.user",
                payload={
                    "text": "Initially public context",
                    "revision": 1,
                    "final": True,
                    "content_scope": "public_synthetic",
                },
                idempotency_key="ctx-revocable",
            ),
            "owner",
        )
        worker = WindowDecisionsWorker(
            LocalJournal(store, "decisions"), FakeAgent(), WindowClassifier(provider)
        )
        w0 = _append_window(
            store,
            session,
            utterance_id="u-revoke",
            window_index=0,
            text="Okay",
            content_scope="public_synthetic",
        )
        await worker.classify(w0)
        assert len(seen_bodies) == 1
        assert seen_bodies[0]["context"] == [
            {"kind": "conversation.user", "text": "Initially public context"}
        ]

        # Restrict the historical entry to local_only; sticky scope withholds it from cloud
        # and invalidates the cached classification that depended on it.
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.revision",
                payload={
                    "target_event_id": ctx_entry.event_id,
                    "text": "Withdrawn local-only context",
                    "revision": 2,
                    "content_scope": "local_only",
                },
                idempotency_key="ctx-revocable-r2",
            ),
            "owner",
        )
        w1 = _append_window(
            store,
            session,
            utterance_id="u-revoke",
            window_index=1,
            text="Okay",
            content_scope="public_synthetic",
        )
        await worker.classify(w1)

        assert len(seen_bodies) == 2
        assert seen_bodies[1]["context"] == []
        assert "Withdrawn local-only context" not in json.dumps(seen_bodies)
        assert store.window_decision(w1.event_id).payload["classification_reused"] is False
    finally:
        await provider.close()
        store.close()
