"""Native streaming-window persistence and conversation control; synthetic models."""

import asyncio
from types import SimpleNamespace

import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    SessionRequest,
    SpeechWindow,
    TaskRequest,
    WindowDecision,
)
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import JournalError, LocalJournal, Store


@pytest.fixture
async def env(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    provider = ScriptedProvider(["I can listen.", "Hello there.", "Tell me more."])
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    controller = WindowConverseController(runtime)
    result = SimpleNamespace(
        store=store, session=session, provider=provider, runtime=runtime, controller=controller
    )
    yield result
    await controller.close()
    store.close()


async def window(
    env, text="Could you actually", index=0, speaking=True, utterance="public-utterance", revision=1
):
    s = env.session
    data = SpeechWindow(
        session_id=s.session_id,
        thread_id=s.thread_id,
        epoch=s.epoch,
        utterance_id=utterance,
        window_index=index,
        revision=revision,
        text=text,
        start_ms=0,
        end_ms=1000 + index * 500,
        user_speaking=speaking,
    )
    return await env.controller.ingest(data)


def decide(env, source, conversation, agent_route="IGNORE_FOR_IMMEDIATE_ROUTING", **fields):
    d = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=source.payload["revision"],
        conversation=conversation,
        agent_route=agent_route,
        **fields,
    )
    return env.store.append(
        AppendRequest(
            session_id=source.session_id,
            thread_id=source.thread_id,
            epoch=source.epoch,
            kind="routing.window_decided",
            payload=d.model_dump(mode="json"),
            causes=[source.event_id],
            idempotency_key="decide:" + source.event_id,
        ),
        "decisions",
    )


async def settle(env):
    jobs = [p.task for p in env.controller.pending.values()]
    if jobs:
        await asyncio.wait_for(asyncio.gather(*jobs), 2)
    await env.controller.tick()


async def test_ignored_and_waiting_windows_remain_in_complete_history(env):
    a = await window(env, "Mm-hmm", utterance="backchannel")
    b = await window(env, "Could you actually", utterance="unfinished")
    await env.controller.process(decide(env, a, "IGNORE"))
    await env.controller.process(decide(env, b, "WAIT_FOR_NEXT_WINDOW"))
    assert [e.payload["text"] for e in env.store.read(kinds=["conversation.window"])] == [
        "Mm-hmm",
        "Could you actually",
    ]
    assert not env.provider.calls
    assert not env.store.read(kinds=["conversation.converse"])


async def test_response_is_prepared_but_not_published_over_speaking_user(env):
    source = await window(env, "Also include documentation.")
    await env.controller.process(decide(env, source, "RESPOND_WITHOUT_INTERRUPTING_USER"))
    await settle(env)
    assert len(env.provider.calls) == 1 and not env.store.read(kinds=["conversation.converse"])
    opening = await window(env, "Also include documentation.", index=1, speaking=False)
    await env.controller.process(opening)
    replies = env.store.read(kinds=["conversation.converse"])
    assert len(replies) == 1 and replies[0].payload["window_source_id"] == source.event_id
    assert len(env.provider.calls) == 1
    assert await env.runtime.validate_speech(
        replies[0].event_id, replies[0].payload["generation_epoch"]
    )


async def test_interrupting_response_can_start_before_user_stops(env):
    source = await window(env, "Stop, don't delete that!")
    await env.controller.process(decide(env, source, "RESPOND_INTERRUPT_USER"))
    await settle(env)
    assert len(env.store.read(kinds=["conversation.converse"])) == 1
    assert len(env.provider.calls) == 1


async def test_backchannel_does_not_stop_existing_speech_but_wait_yields(env):
    s = env.session
    reply = await env.runtime.turn(
        ConverseRequest(
            session_id=s.session_id,
            thread_id=s.thread_id,
            epoch=s.epoch,
            text="Hello",
            idempotency_key="typed",
        )
    )
    generation = next(e["generation_epoch"] for e in reply["events"] if e["type"] == "accepted")
    back = await window(env, "Mm-hmm", utterance="backchannel")
    await env.controller.process(decide(env, back, "IGNORE"))
    assert (await env.runtime._epoch(s.session_id)).generation == generation
    unfinished = await window(env, "Could you actually", utterance="unfinished")
    await env.controller.process(decide(env, unfinished, "WAIT_FOR_NEXT_WINDOW"))
    assert (await env.runtime._epoch(s.session_id)).generation > generation
    assert env.store.read(kinds=["delivery.updated"])[-1].payload["status"] == "interrupted"


async def test_corrected_window_revokes_generated_speech_and_rejects_old_decision(env):
    source = await window(env, "Hello", speaking=False)
    old_decision = decide(env, source, "RESPOND_WITHOUT_INTERRUPTING_USER")
    await env.controller.process(old_decision)
    await settle(env)
    reply = env.store.read(kinds=["conversation.converse"])[0]
    await window(env, "Different recognized words", speaking=False, revision=2)
    with pytest.raises(ValueError, match="corrected"):
        await env.runtime.validate_speech(reply.event_id, reply.payload["generation_epoch"])
    assert env.store.resolve(source.event_id).payload["revision"] == 2
    with pytest.raises(JournalError):
        decide(env, source, "RESPOND_INTERRUPT_USER")


async def test_once_responded_same_utterance_cannot_generate_duplicate_reply(env):
    source = await window(env, "Hello", speaking=False)
    await env.controller.process(decide(env, source, "RESPOND_WITHOUT_INTERRUPTING_USER"))
    await settle(env)
    later = await window(env, "Hello", index=1, speaking=False)
    await env.controller.process(decide(env, later, "RESPOND_WITHOUT_INTERRUPTING_USER"))
    await settle(env)
    assert (
        len(env.provider.calls) == 1 and len(env.store.read(kinds=["conversation.converse"])) == 1
    )


async def test_stale_epoch_and_unauthenticated_window_sources_fail_closed(env, tmp_path):
    source = await window(env)
    with pytest.raises(PermissionError):
        env.store.append(
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="conversation.window",
                payload=source.payload,
                idempotency_key="forged",
            ),
            "agent",
        )
    env.store.reset(env.session.session_id)
    with pytest.raises(JournalError):
        await window(env, index=1)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = AgentRuntime(
        LocalJournal(env.store, "agent"), ScriptedProvider([]), workspace, ToolPolicy(workspace)
    )
    try:
        with pytest.raises((PolicyDenied, ValueError)):
            await agent.submit(
                TaskRequest(
                    session_id=source.session_id,
                    thread_id=source.thread_id,
                    epoch=source.epoch,
                    objective=source.payload["text"],
                    source_event_ids=[source.event_id],
                    idempotency_key="stale",
                ),
                actor="decisions",
            )
    finally:
        await agent.close()


async def test_window_classifier_does_not_wait_for_unavailable_agent(env):
    class NoAgent:
        async def status(self):
            await asyncio.Event().wait()

    class Classifier:
        calls = 0

        async def classify(self, source, context, tasks):
            self.calls += 1
            return {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
            }

    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(env.store, "decisions"), NoAgent(), classifier)
    source = await window(env)
    try:
        await worker.tick()
        await asyncio.wait_for(asyncio.gather(*(job for _, job in worker.jobs.values())), 1)
        decision = env.store.window_decision(source.event_id)
        assert decision and classifier.calls == 1
    finally:
        await worker.close()


async def test_late_reply_cannot_revive_speech_after_newer_wait(env):
    older = await window(env, "What is a comet?", speaking=False, utterance="earlier")
    later = await window(env, "Actually, let me", utterance="later")
    await env.controller.process(decide(env, later, "WAIT_FOR_NEXT_WINDOW"))
    await env.controller.process(decide(env, older, "RESPOND_INTERRUPT_USER"))
    await settle(env)
    assert not env.provider.calls and not env.store.read(kinds=["conversation.converse"])


async def test_later_wait_revokes_published_speech_before_observer_tick(env):
    older = await window(env, "Hello", speaking=False)
    await env.controller.process(decide(env, older, "RESPOND_WITHOUT_INTERRUPTING_USER"))
    await settle(env)
    reply = env.store.read(kinds=["conversation.converse"])[0]
    later = await window(env, "Actually, let me", utterance="later")
    decide(env, later, "WAIT_FOR_NEXT_WINDOW")
    with pytest.raises(ValueError, match="revoked"):
        await env.runtime.validate_speech(reply.event_id, reply.payload["generation_epoch"])


async def test_transient_final_window_failure_gets_one_bounded_retry(env):
    class Agent:
        async def status(self):
            return []

    class Classifier:
        calls = 0

        async def classify(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("synthetic transient")
            return {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
            }

    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(env.store, "decisions"), Agent(), classifier)
    source = await window(env)
    await worker._job(source)
    assert classifier.calls == 2 and env.store.window_decision(source.event_id)
    assert any(
        e.payload["phase"] == "retrying" for e in env.store.read(kinds=["routing.window_status"])
    )


async def test_classification_cache_reuses_speech_only_while_task_binding_matches(env):
    class Agent:
        tasks = []

        async def status(self):
            return self.tasks

    class Classifier:
        calls = 0

        async def classify(self, *args):
            self.calls += 1
            return {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
            }

    agent, classifier = Agent(), Classifier()
    worker = WindowDecisionsWorker(LocalJournal(env.store, "decisions"), agent, classifier)
    await worker.classify(await window(env))
    await worker.classify(await window(env, index=1))
    assert classifier.calls == 1
    agent.tasks = [
        SimpleNamespace(
            task_id="new-task",
            session_id=env.session.session_id,
            epoch=1,
            objective_revision=1,
            state="running",
        )
    ]
    await worker.classify(await window(env, index=2))
    assert classifier.calls == 2


async def test_conversational_completion_words_are_preserved(env):
    env.runtime.provider = ScriptedProvider(["Well done. I finished my introduction."])
    source = await window(env, "Thanks for explaining.", speaking=False)
    await env.controller.process(decide(env, source, "RESPOND_WITHOUT_INTERRUPTING_USER"))
    await settle(env)
    spoken = " ".join(e.payload["text"] for e in env.store.read(kinds=["conversation.converse"]))
    assert spoken == "Well done. I finished my introduction."


async def test_explicit_file_completion_claim_needs_native_evidence(env):
    env.runtime.provider = ScriptedProvider(["I created the file."])
    source = await window(env, "Please write a file.", speaking=False)
    await env.controller.process(
        decide(env, source, "RESPOND_WITHOUT_INTERRUPTING_USER", "QUEUE_UNTIL_TASK_DONE")
    )
    await settle(env)
    spoken = env.store.read(kinds=["conversation.converse"])[0].payload["text"]
    assert spoken == "The Agent has not provided a verified result for that claim."


async def test_out_of_order_window_does_not_cancel_newer_prepared_reply(env):
    source = await window(env, "Could you explain comets?", index=1)
    await env.controller.process(decide(env, source, "RESPOND_WITHOUT_INTERRUPTING_USER"))
    await settle(env)
    prepared = env.controller.pending[source.session_id]
    late = await window(env, "Could you", index=0)
    await env.controller.process(late)
    assert env.controller.pending[source.session_id] is prepared
    assert not prepared.task.cancelled()


async def test_newer_recognition_revokes_old_hypothesis_speech(env):
    source = await window(env, "Hello", speaking=False)
    await env.controller.process(decide(env, source, "RESPOND_WITHOUT_INTERRUPTING_USER"))
    await settle(env)
    reply = env.store.read(kinds=["conversation.converse"])[0]
    await window(env, "Hello, actually let me finish", index=1)
    with pytest.raises(ValueError, match="superseded"):
        await env.runtime.validate_speech(reply.event_id, reply.payload["generation_epoch"])


async def test_budget_refusal_is_visible_and_never_retried(env):
    from packages.providers.text import ProviderError

    class Agent:
        async def status(self):
            return []

    class Classifier:
        calls = 0

        async def classify(self, *args):
            self.calls += 1
            raise ProviderError("Meta HTTP 402; no automatic provider fallback")

    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(env.store, "decisions"), Agent(), classifier)
    source = await window(env)
    await worker._job(source)
    assert classifier.calls == 1
    assert (
        env.store.read(kinds=["routing.window_status"])[-1].payload["reason"]
        == "provider_budget_refused"
    )


@pytest.mark.parametrize("timeout", [True, 0, 31, "15"])
async def test_classification_timeout_config_is_explicitly_bounded(env, timeout):
    with pytest.raises(ValueError, match="timeout"):
        WindowDecisionsWorker(LocalJournal(env.store, "decisions"), None, None, timeout=timeout)


async def test_native_agent_can_retrieve_ignored_and_unfinished_windows_on_demand(env, tmp_path):
    import json

    ignored = await window(env, "Mm-hmm.", utterance="ignored")
    unfinished = await window(env, "Could you actually", utterance="unfinished")
    decide(env, ignored, "IGNORE")
    decide(env, unfinished, "WAIT_FOR_NEXT_WINDOW")
    provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "tool": "read_transcript",
                    "arguments": {"event_ids": [ignored.event_id, unfinished.event_id]},
                }
            ),
            json.dumps(
                {
                    "tool": "finish",
                    "arguments": {
                        "summary": "Read retained transcript windows.",
                        "objective_revision": 1,
                    },
                }
            ),
        ]
    )
    agent = AgentRuntime(LocalJournal(env.store, "agent"), provider, tmp_path, ToolPolicy(tmp_path))
    try:
        task = await agent.submit(
            TaskRequest(
                session_id=env.session.session_id,
                thread_id=env.session.thread_id,
                epoch=1,
                objective="Read the ignored and unfinished transcript windows.",
                idempotency_key="retrieve-windows",
            )
        )
        final = await agent.wait(task.task_id)
        assert final.state == "succeeded", final.error
        assert final.evidence[0].kind == "read"
        received = json.dumps(provider.calls[1])
        assert "Mm-hmm." in received and "Could you actually" in received
    finally:
        await agent.close()


async def test_slow_classifier_keeps_one_request_and_latest_pending_window(env):
    class Agent:
        async def status(self):
            return []

    class Classifier:
        def __init__(self):
            self.calls = []
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def classify(self, source, *args):
            self.calls.append(source.event_id)
            self.entered.set()
            await self.release.wait()
            return {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
            }

    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(env.store, "decisions"), Agent(), classifier)
    first = await window(env, "Could you")
    await worker.tick()
    await asyncio.wait_for(classifier.entered.wait(), 1)
    original = next(iter(worker.jobs.values()))[1]
    for index in range(1, 8):
        latest = await window(env, "Could you explain " + "comets " * index, index=index)
        await worker.tick()
    assert not original.cancelled() and not original.done()
    assert len(worker.jobs) == 1 and len(worker.pending) == 1
    classifier.release.set()
    await asyncio.gather(original)
    await worker.tick()
    await asyncio.gather(*(t for _, t in worker.jobs.values()))
    assert classifier.calls == [first.event_id, latest.event_id]
    assert env.store.window_decision(first.event_id) is None
    assert env.store.window_decision(latest.event_id)
    await worker.close()
