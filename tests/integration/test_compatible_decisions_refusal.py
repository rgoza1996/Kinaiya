"""Invalid completion terminals cannot become native window routing decisions."""

import asyncio
import json

import httpx
import pytest

from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow
from packages.providers.text import CompatibleTextProvider, ProviderRefusal
from services.decisions.window_routing import WindowAgentDispatcher, WindowClassifier
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import LocalJournal, Store


class ObservedClassifier(WindowClassifier):
    def __init__(self, provider):
        super().__init__(provider)
        self.refusals = []

    async def classify(self, *args):
        try:
            return await super().classify(*args)
        except ProviderRefusal as exc:
            self.refusals.append(type(exc))
            raise


class Agent:
    def __init__(self):
        self.submitted = []

    async def status(self):
        return []

    async def submit(self, request, *, actor):
        self.submitted.append((request, actor))
        pytest.fail("A refused completion must not dispatch native Agent work")


@pytest.mark.parametrize("terminal", ["length", "missing"])
async def test_completion_refusal_is_one_call_and_durable_across_restart(tmp_path, terminal):
    calls = []
    # These labels would queue work if accepted; refusal must come from the
    # real adapter's terminal validation, not malformed classifier JSON.
    labels = {
        "conversation": "RESPOND_WITHOUT_INTERRUPTING_USER",
        "agent_route": "QUEUE_UNTIL_TASK_DONE",
        "target_task_id": None,
        "expected_objective_revision": None,
        "reason_code": "public_synthetic_work",
    }

    def respond(request):
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        calls.append(json.loads(request.content))
        assert calls[-1]["stream"] is False
        choice = {"index": 0, "message": {"role": "assistant", "content": json.dumps(labels)}}
        if terminal != "missing":
            choice["finish_reason"] = terminal
        return httpx.Response(
            200, stream=httpx.ByteStream(json.dumps({"choices": [choice]}).encode())
        )

    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic", transport=httpx.MockTransport(respond)
    )
    path = tmp_path / "journal.sqlite"
    store = Store(path)
    worker = None
    agent = Agent()
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        window = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="synthetic_request",
            window_index=0,
            text="Prepare the public synthetic checklist.",
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
        source = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window",
                payload=window.model_dump(mode="json"),
                idempotency_key="synthetic-window",
            ),
            "owner",
        )
        classifier = ObservedClassifier(provider)
        journal = LocalJournal(store, "decisions")
        worker = WindowDecisionsWorker(journal, agent, classifier)
        await worker.tick()
        await asyncio.wait_for(asyncio.gather(*(task for _, task in worker.jobs.values())), 2)
        await worker.tick()
        await WindowAgentDispatcher(journal, agent).tick()

        assert classifier.refusals == [ProviderRefusal]
        assert len(calls) == 1 and not worker.jobs and not worker.pending
        assert store.window_classification_attempts(source.event_id) == 1
        statuses = store.read(kinds=["routing.window_status"])
        assert [entry.payload["phase"] for entry in statuses] == ["classifying", "error"]
        assert statuses[-1].causes == [source.event_id]
        assert statuses[-1].payload["reason"] == "provider_unavailable"
        assert not store.pending_windows()
        error_id = statuses[-1].event_id
        await worker.close()
        worker = None
        store.close()

        store = Store(path)
        journal = LocalJournal(store, "decisions")
        restarted = ObservedClassifier(provider)
        worker = WindowDecisionsWorker(journal, agent, restarted)
        await worker.tick()
        await worker.tick()
        await WindowAgentDispatcher(journal, agent).tick()
        assert len(calls) == 1 and not restarted.refusals
        assert not worker.jobs and not worker.pending and not store.pending_windows()
        assert store.read(kinds=["routing.window_status"])[-1].event_id == error_id
        assert not store.read(
            kinds=["routing.window_decided", "routing.window_dispatch", "task.created"]
        )
        assert not agent.submitted
    finally:
        if worker is not None:
            await worker.close()
        await provider.close()
        store.close()
