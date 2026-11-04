"""Finalized Decisions malformed classification refusal and bounded retry tests."""

import json

import httpx
import pytest

from packages.contracts.models import AppendRequest, SessionRequest, Task
from packages.providers.text import CompatibleTextProvider, ProviderError
from services.decisions.runtime import (
    MAX_ATTEMPTS,
    DecisionsWorker,
    InvalidClassificationError,
    ModelClassifier,
)
from services.session.store import LocalJournal, Store


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
        updated = target.model_copy(update={"objective_revision": target.objective_revision + 1})
        self.tasks = [updated if t.task_id == task_id else t for t in self.tasks]
        return updated

    async def interrupt(self, task_id, request, *, actor):
        self.interrupted.append((task_id, request, actor))
        target = next(t for t in self.tasks if t.task_id == task_id)
        updated = target.model_copy(update={"state": "cancelled"})
        self.tasks = [updated if t.task_id == task_id else t for t in self.tasks]
        return updated


def _append_user_entry(store, session, text, *, key, scope="public_synthetic"):
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
                "content_scope": scope,
            },
            idempotency_key=key,
        ),
        "owner",
    )


def _chat_response(content: str, *, finish_reason: str = "stop") -> httpx.Response:
    body = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ]
    }
    return httpx.Response(200, stream=httpx.ByteStream(json.dumps(body).encode()))


@pytest.mark.parametrize(
    ("raw_content", "expected_reason"),
    [
        ("{not json}", "malformed_json"),
        ("[]", "invalid_shape"),
        ("{}", "missing_route"),
        ('{"route": "launch_missiles"}', "invalid_route"),
        ('{"route": "ignore", "unexpected": true}', "invalid_shape"),
        (
            '{"route": "steer", "target_task_id": 123, "expected_objective_revision": 1}',
            "invalid_target_fields",
        ),
        (
            '{"route": "steer", "target_task_id": "t1", "expected_objective_revision": 0}',
            "invalid_target_fields",
        ),
        ('{"route": "ignore", "reason_code": ""}', "invalid_reason_code"),
        ('{"route": "ignore", "confidence": 1.5}', "invalid_confidence"),
        ('{"route": "ignore", "confidence": true}', "invalid_confidence"),
    ],
)
async def test_malformed_or_invalid_classification_records_refusal_and_never_dispatches(
    tmp_path, raw_content, expected_reason
):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return _chat_response(raw_content)

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    agent = FakeAgent()
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        first = _append_user_entry(
            store,
            session,
            "First message with invalid model classification.",
            key="user-invalid",
        )
        worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))
        await worker.tick()

        assert len(calls) == 1
        assert store.cursor("decisions", worker.consumer) >= first.sequence
        assert not agent.submitted
        assert not store.read(kinds=["routing.decided", "routing.dispatch"])

        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].causes == [first.event_id]
        assert refusals[0].payload == {
            "source_event_id": first.event_id,
            "source_revision": 1,
            "reason": expected_reason,
            "error_type": InvalidClassificationError.__name__,
        }

        detail = store.decisions.detail(first.event_id)
        assert detail["summary"]["disposition"] == "refused"
        assert detail["summary"]["call_count"] == 1
        assert detail["calls"][0]["status"] == "refused"
        assert detail["calls"][0]["error_type"] == InvalidClassificationError.__name__
        assert (
            json.loads(detail["calls"][0]["raw_response"])["choices"][0]["message"]["content"]
            == raw_content
        )
        assert detail["calls"][0]["validated_output"] is None
    finally:
        await provider.close()
        store.close()


async def test_empty_instruction_for_queue_or_steer_refuses_without_dispatch(tmp_path):
    def respond(_request):
        return _chat_response(json.dumps({"route": "queue", "reason_code": "empty_test"}))

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    agent = FakeAgent()
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        blank = _append_user_entry(store, session, "   ", key="user-blank")
        worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))
        await worker.tick()

        assert store.cursor("decisions", worker.consumer) >= blank.sequence
        assert not agent.submitted
        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].payload["reason"] == "invalid_instruction"
        assert refusals[0].payload["error_type"] == InvalidClassificationError.__name__
    finally:
        await provider.close()
        store.close()


async def test_two_queued_entries_first_invalid_second_valid_routes_without_head_of_line_blocking(
    tmp_path,
):
    responses = [
        "{not json}",
        json.dumps({"route": "queue", "reason_code": "valid_second_entry"}),
        json.dumps({"route": "queue", "reason_code": "other_session_entry"}),
    ]
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return _chat_response(responses[len(calls) - 1])

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    agent = FakeAgent()
    try:
        session_a = store.create_session(SessionRequest(channel="converse"))
        session_b = store.create_session(SessionRequest(channel="converse"))
        first = _append_user_entry(
            store, session_a, "Malformed classification entry", key="session-a-1"
        )
        second = _append_user_entry(
            store, session_a, "Summarize the public notes", key="session-a-2"
        )
        third = _append_user_entry(
            store, session_b, "Run public check in session B", key="session-b-1"
        )

        worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))
        await worker.tick()

        assert len(calls) == 3
        assert store.cursor("decisions", worker.consumer) >= third.sequence

        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].causes == [first.event_id]
        assert refusals[0].payload["reason"] == "malformed_json"

        decided = store.read(kinds=["routing.decided"])
        assert [entry.causes for entry in decided] == [[second.event_id], [third.event_id]]
        assert len(agent.submitted) == 2
        assert [req.objective for req, _ in agent.submitted] == [
            "Summarize the public notes",
            "Run public check in session B",
        ]
    finally:
        await provider.close()
        store.close()


@pytest.mark.parametrize("fail_after_commit", [False, True])
async def test_refusal_append_failure_does_not_advance_cursor_and_reconciles_without_recalling_provider(
    tmp_path, fail_after_commit
):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return _chat_response("{}")

    class FlakyRefusalJournal(LocalJournal):
        def __init__(self, store, role):
            super().__init__(store, role)
            self.fail_refusal_once = True

        async def append(self, request):
            if request.kind == "routing.classification_refused" and self.fail_refusal_once:
                self.fail_refusal_once = False
                if fail_after_commit:
                    await super().append(request)
                raise OSError("synthetic refusal append failure")
            return await super().append(request)

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    agent = FakeAgent()
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        entry = _append_user_entry(
            store, session, "Entry whose refusal append fails once", key="flaky-refusal"
        )
        journal = FlakyRefusalJournal(store, "decisions")
        worker = DecisionsWorker(journal, agent, ModelClassifier(provider))

        with pytest.raises(OSError, match="synthetic refusal append failure"):
            await worker.tick()

        assert len(calls) == 1
        assert store.cursor("decisions", worker.consumer) < entry.sequence

        # Retry on next tick must reconcile the refusal without calling the provider again.
        await worker.tick()
        assert len(calls) == 1
        assert store.cursor("decisions", worker.consumer) >= entry.sequence
        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].causes == [entry.event_id]
        assert refusals[0].payload["reason"] == "missing_route"
        assert not agent.submitted
    finally:
        await provider.close()
        store.close()


async def test_transient_transport_failure_recovers_on_second_attempt(tmp_path):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            raise httpx.ConnectError("synthetic transient connection error")
        return _chat_response(json.dumps({"route": "queue", "reason_code": "recovered"}))

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    agent = FakeAgent()
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        entry = _append_user_entry(
            store, session, "Recover after one transient failure", key="transient-1"
        )
        worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))

        with pytest.raises(ProviderError):
            await worker.tick()
        assert len(calls) == 1
        assert store.cursor("decisions", worker.consumer) < entry.sequence
        assert not store.read(kinds=["routing.classification_refused", "routing.decided"])

        await worker.tick()
        assert len(calls) == 2
        assert store.cursor("decisions", worker.consumer) >= entry.sequence
        assert len(agent.submitted) == 1
        detail = store.decisions.detail(entry.event_id)
        assert detail["summary"]["call_count"] == 2
        assert [c["attempt"] for c in reversed(detail["calls"])] == [1, 2]
        assert [c["status"] for c in reversed(detail["calls"])] == ["error", "completed"]
    finally:
        await provider.close()
        store.close()


async def test_persistent_transport_failure_refuses_after_bounded_attempts_even_across_restart(
    tmp_path,
):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) <= MAX_ATTEMPTS:
            raise httpx.ConnectError("persistent provider outage")
        return _chat_response(json.dumps({"route": "queue", "reason_code": "later_entry"}))

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    path = tmp_path / "journal.sqlite"
    store = Store(path)
    agent = FakeAgent()
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        first = _append_user_entry(
            store, session, "Entry experiencing provider outage", key="outage-1"
        )
        second = _append_user_entry(
            store, session, "Later entry that should proceed", key="outage-2"
        )
        worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))

        # Attempt 1 fails and leaves cursor behind first entry.
        with pytest.raises(ProviderError):
            await worker.tick()
        assert len(calls) == 1
        assert store.cursor("decisions", worker.consumer) < first.sequence

        # Simulate worker restart before attempt 2; persisted decisions_calls tracks attempt 1.
        restarted = DecisionsWorker(
            LocalJournal(store, "decisions"), agent, ModelClassifier(provider)
        )
        await restarted.tick()

        assert len(calls) == MAX_ATTEMPTS + 1
        assert store.cursor("decisions", restarted.consumer) >= second.sequence

        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].causes == [first.event_id]
        assert refusals[0].payload == {
            "source_event_id": first.event_id,
            "source_revision": 1,
            "reason": "provider_failed",
            "error_type": "ProviderError",
        }

        decided = store.read(kinds=["routing.decided"])
        assert len(decided) == 1
        assert decided[0].causes == [second.event_id]
        assert len(agent.submitted) == 1
        assert agent.submitted[0][0].objective == "Later entry that should proceed"
    finally:
        await provider.close()
        store.close()


async def test_token_budget_or_402_error_refuses_immediately_on_first_attempt(tmp_path):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            402,
            stream=httpx.ByteStream(b'{"error":"Local daily token budget exceeded"}'),
        )

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    store = Store(tmp_path / "journal.sqlite")
    agent = FakeAgent()
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        entry = _append_user_entry(
            store, session, "Entry hitting 402 token budget limit", key="budget-402"
        )
        worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, ModelClassifier(provider))
        await worker.tick()

        assert len(calls) == 1
        assert store.cursor("decisions", worker.consumer) >= entry.sequence
        refusals = store.read(kinds=["routing.classification_refused"])
        assert len(refusals) == 1
        assert refusals[0].causes == [entry.event_id]
        assert refusals[0].payload["reason"] == "token_budget_exceeded"
        assert not agent.submitted
    finally:
        await provider.close()
        store.close()
