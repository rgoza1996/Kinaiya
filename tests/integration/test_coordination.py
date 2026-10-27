import asyncio
import json

import httpx
import pytest

from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    DeliveryRequest,
    SessionRequest,
    Task,
)
from packages.providers.text import ScriptedProvider
from services.converse.runtime import ConverseRuntime
from services.decisions.runtime import DecisionsWorker, ModelClassifier
from services.session.store import LocalJournal, Store


class RecordingAgent:
    """Explicit deterministic Agent adapter for routing contract tests only."""

    def __init__(self):
        self.submissions = []

    async def status(self):
        return []

    async def submit(self, request, actor="owner"):
        if not any(x[0].idempotency_key == request.idempotency_key for x in self.submissions):
            self.submissions.append((request, actor))


@pytest.fixture
def conversation(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    yield store, session
    store.close()


def request(session, text="Hello", key="turn-1"):
    return ConverseRequest(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        text=text,
        idempotency_key=key,
    )


async def test_converse_reply_finishes_while_classifier_is_blocked(conversation):
    store, session = conversation
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider(["Hello there!"]))
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockedClassifier:
        async def classify(self, *args):
            entered.set()
            await release.wait()
            return {"route": "ignore"}

    worker = DecisionsWorker(
        LocalJournal(store, "decisions"), RecordingAgent(), BlockedClassifier()
    )
    task = asyncio.create_task(runtime.turn(request(session)))
    while not store.read(kinds=["conversation.user"]):
        await asyncio.sleep(0)
    routing = asyncio.create_task(worker.tick())
    await asyncio.wait_for(entered.wait(), 1)
    result = await asyncio.wait_for(task, 1)
    assert result["text"] == "Hello there!"
    assert not routing.done()
    release.set()
    await routing


async def test_selective_routing_references_and_replay(conversation):
    store, session = conversation
    source = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=1,
            kind="conversation.user",
            payload={"text": "Create note.txt with synthetic text", "final": True, "revision": 1},
            idempotency_key="source",
        ),
        "owner",
    )
    classifier = ModelClassifier(ScriptedProvider([json.dumps({"route": "queue"})]))
    agent = RecordingAgent()
    worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, classifier)
    await worker.tick()
    await worker.tick()
    assert len(agent.submissions) == 1
    routed, actor = agent.submissions[0]
    assert routed.source_event_ids == [source.event_id]
    assert routed.objective == source.payload["text"]
    assert actor == "decisions"
    assert "transcript" not in routed.model_dump()


async def test_agent_off_never_replays_surprise_work(conversation):
    store, session = conversation
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider(["I hear you."]))
    await runtime.turn(request(session, "Make a file"))
    agent = RecordingAgent()
    worker = DecisionsWorker(
        LocalJournal(store, "decisions"),
        agent,
        ModelClassifier(ScriptedProvider([])),
        enabled=False,
    )
    await worker.tick()
    worker.enabled = True
    await worker.tick()
    assert agent.submissions == []
    assert all(e.payload["route"] == "ignore" for e in store.read(kinds=["routing.decided"]))


async def test_completion_claim_is_blocked_without_evidence(conversation):
    store, session = conversation
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"), ScriptedProvider(["Done. I saved all your files."])
    )
    result = await runtime.turn(request(session, "How is the work?"))
    assert "I saved" not in result["text"]
    assert store.read(kinds=["agent.outcome"]) == []
    assert store.read(kinds=["converse.safety"])


async def test_late_audio_rejected_and_speech_interrupt_is_independent(conversation):
    store, session = conversation
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider(["Hello there."]))
    result = await runtime.turn(request(session))
    entry = next(e["entry"] for e in result["events"] if e["type"] == "text")
    generation = entry["payload"]["generation_epoch"]
    ack = DeliveryRequest(
        event_id=entry["event_id"],
        generation_epoch=generation,
        chunk_id=entry["payload"]["chunk_id"],
        status="playing",
    )
    await runtime.acknowledge_delivery(ack)
    stopped = await runtime.interrupt(session.session_id)
    assert stopped["agent_cancelled"] is False
    with pytest.raises(ValueError, match="Obsolete"):
        await runtime.acknowledge_delivery(ack.model_copy(update={"status": "played"}))
    assert store.read(kinds=["agent.command"]) == []


async def test_converse_slash_input_stays_plain_text(conversation):
    store, session = conversation
    provider = ScriptedProvider(["We can talk about that text."])
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    await runtime.turn(request(session, "/stop"))
    assert provider.calls[0][-1]["content"] == "/stop"
    assert store.read(kinds=["agent.command"]) == []


async def test_grounded_notice_needs_runtime_evidence_and_current_revision(conversation):
    store, session = conversation
    runtime = ConverseRuntime(LocalJournal(store, "converse"), ScriptedProvider([]))
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=1,
        objective="synthetic",
        idempotency_key="task-1",
        state="succeeded",
    )
    outcome = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=1,
            kind="agent.outcome",
            payload=task.model_dump(mode="json"),
            task_refs=[task.task_id],
            idempotency_key="outcome",
        ),
        "agent",
    )
    with pytest.raises(ValueError, match="evidenced"):
        await runtime.outcome_notice(outcome)


async def test_task_finishing_during_classification_does_not_deadlock_router(conversation):
    store, session = conversation
    task = Task(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=1,
        objective="Original work",
        idempotency_key="original",
        state="running",
    )

    class FinishedDuringClassification(RecordingAgent):
        calls = 0

        async def status(self):
            return [task]

        async def steer(self, *args, **kwargs):
            self.calls += 1
            request = httpx.Request("POST", "http://agent/v1/steer")
            raise httpx.HTTPStatusError(
                "Task already finished",
                request=request,
                response=httpx.Response(409, request=request),
            )

    source = store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=1,
            kind="conversation.user",
            payload={"text": "Use another filename", "final": True},
            idempotency_key="correction",
        ),
        "owner",
    )
    agent = FinishedDuringClassification()
    classifier = ModelClassifier(
        ScriptedProvider(
            [
                json.dumps(
                    {
                        "route": "steer",
                        "target_task_id": task.task_id,
                        "expected_objective_revision": 1,
                    }
                )
            ]
        )
    )
    worker = DecisionsWorker(LocalJournal(store, "decisions"), agent, classifier)
    await worker.tick()
    await worker.tick()
    assert agent.calls == 1
    assert store.cursor("decisions", worker.consumer) >= source.sequence
    rejected = store.read(kinds=["routing.dispatch"])
    assert len(rejected) == 1 and rejected[0].payload["state"] == "rejected"


def test_streamed_decimal_boundary_waits_for_disambiguation():
    from services.converse.runtime import SpeechChunker

    chunker = SpeechChunker()
    assert chunker.feed("Pi is 3.") == []
    assert chunker.feed("14. Next sentence! ") == ["Pi is 3.14.", "Next sentence!"]
    assert chunker.feed("Done.") == []
    assert chunker.feed("", final=True) == ["Done."]


async def test_late_interruption_cannot_stop_a_newer_reply(conversation):
    store, session = conversation
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"), ScriptedProvider(["First sentence.", "Newer reply."])
    )
    await runtime.turn(request(session, key="first"))
    old = runtime.playback[session.session_id].generation
    await runtime.turn(request(session, key="second"))
    current = runtime.playback[session.session_id].generation
    with pytest.raises(ValueError, match="Obsolete interruption"):
        await runtime.interrupt(session.session_id, expected_generation=old)
    assert runtime.playback[session.session_id].generation == current
