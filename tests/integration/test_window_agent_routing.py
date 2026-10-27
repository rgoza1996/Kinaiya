"""Window classifier + agent dispatcher tests (issue #49, clarification §10).

Uses the real Store/LocalJournal and a SYNTHETIC capturing Agent fixture.
This fixture is NOT a live executor: it only records decisions-routed calls
so routing, privacy, ordering, dedupe, and recovery behavior can be checked.
Native Agent integration and provider checks are owned by the lead.
"""

import asyncio
import json

import pytest

from core.policy import ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    InterruptRequest,
    SessionRequest,
    SpeechWindow,
    SteerRequest,
    Task,
    TaskRequest,
    TaskState,
    WindowDecision,
    new_id,
)
from packages.providers.text import ScriptedProvider as NativeScriptedProvider
from services.agent import AgentRuntime
from services.decisions.window_routing import WindowAgentDispatcher, WindowClassifier
from services.session.store import LocalJournal, Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    yield store
    store.close()


@pytest.fixture
def session(store):
    return store.create_session(SessionRequest())


def owner_journal(store):
    return LocalJournal(store, "owner")


def converse_journal(store):
    return LocalJournal(store, "converse")


def decisions_journal(store):
    return LocalJournal(store, "decisions")


def make_window(
    store,
    session,
    utterance_id="u1",
    window_index=0,
    revision=1,
    text="hello",
    scope="public_synthetic",
    role="owner",
    key=None,
):
    payload = SpeechWindow(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        utterance_id=utterance_id,
        window_index=window_index,
        revision=revision,
        text=text,
        start_ms=0,
        end_ms=1000,
        user_speaking=False,
        capture_ended=True,
        content_scope=scope,
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.window",
            payload=payload.model_dump(mode="json"),
            idempotency_key=key or f"w:{utterance_id}:{window_index}:r{revision}",
        ),
        role,
    )


def make_decision(store, session, source, conversation, agent_route, target=None, revision=None):
    decision = WindowDecision(
        source_event_id=source.event_id,
        source_sequence=source.sequence,
        source_revision=source.payload.get("revision", 1),
        conversation=conversation,
        agent_route=agent_route,
        target_task_id=target,
        expected_objective_revision=revision,
        reason_code="test",
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="routing.window_decided",
            payload=decision.model_dump(mode="json"),
            causes=[source.event_id],
            task_refs=[target] if target else [],
            idempotency_key=f"decided:{decision.decision_id}",
        ),
        "decisions",
    )


class CapturingAgent:
    """SYNTHETIC capturing Agent fixture — not a live executor.

    Records decisions-routed submit/steer/interrupt calls and serves a small
    in-memory task list. Never executes tools or proves completion.
    """

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.submits: list = []
        self.steers: list = []
        self.interrupts: list = []
        self.fail_next: dict[str, Exception] = {}

    def add_task(self, session, objective="work", revision=1, state=TaskState.RUNNING):
        task = Task(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective=objective,
            objective_revision=revision,
            state=state,
            source_event_ids=[],
            idempotency_key=f"seed:{new_id()}",
        )
        self.tasks[task.task_id] = task
        return task

    def set_state(self, task_id, state):
        self.tasks[task_id] = self.tasks[task_id].model_copy(update={"state": state})

    async def status(self, task_id=None):
        if task_id is not None:
            return self.tasks[task_id]
        return list(self.tasks.values())

    async def submit(self, request: TaskRequest, actor="owner"):
        assert actor == "decisions"
        if "submit" in self.fail_next:
            raise self.fail_next.pop("submit")
        self.submits.append(request)
        task = Task(
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            objective=request.objective,
            objective_revision=request.source_revision,
            state=TaskState.ACCEPTED,
            source_event_ids=list(request.source_event_ids),
            idempotency_key=request.idempotency_key,
        )
        self.tasks[task.task_id] = task
        return task.model_copy(deep=True)

    async def steer(self, task_id: str, request: SteerRequest, actor="owner"):
        assert actor == "decisions"
        if "steer" in self.fail_next:
            raise self.fail_next.pop("steer")
        self.steers.append((task_id, request))
        task = self.tasks[task_id]
        updated = task.model_copy(
            update={"objective_revision": task.objective_revision + 1, "state": TaskState.RUNNING}
        )
        self.tasks[task_id] = updated
        return updated.model_copy(deep=True)

    async def interrupt(self, task_id: str, request: InterruptRequest, actor="owner"):
        assert actor == "decisions"
        if "interrupt" in self.fail_next:
            raise self.fail_next.pop("interrupt")
        self.interrupts.append((task_id, request))
        task = self.tasks[task_id]
        updated = task.model_copy(update={"state": TaskState.CANCEL_REQUESTED})
        self.tasks[task_id] = updated
        return updated.model_copy(deep=True)


class ScriptedProvider:
    def __init__(self, script, *, provider="fixture", local=True):
        from packages.contracts.models import ProviderInfo

        self.script = list(script)
        self.info = ProviderInfo(provider=provider, model="scripted", local=local)
        self.seen: list = []

    async def complete(self, messages):
        self.seen.append(messages)
        item = self.script.pop(0)
        return item if isinstance(item, str) else json.dumps(item)


def dispatch_receipts(store, session):
    return store.read(session_id=session.session_id, kinds=["routing.window_dispatch"])


async def test_classifier_dual_labels_wait_plus_steer(store, session):
    source = make_window(store, session, text="Also include the documentation.")
    provider = ScriptedProvider(
        [
            {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "STEER_CURRENT_TASK",
                "target_task_id": "t1",
                "expected_objective_revision": 2,
                "reason_code": "mid_task_addition",
            }
        ]
    )
    out = await WindowClassifier(provider).classify(source, [], [])
    assert out["conversation"] == "WAIT_FOR_NEXT_WINDOW"
    assert out["agent_route"] == "STEER_CURRENT_TASK"
    assert out["target_task_id"] == "t1"
    assert out["expected_objective_revision"] == 2


async def test_classifier_rejects_missing_and_invalid(store, session):
    source = make_window(store, session, text="hi")
    with pytest.raises(ValueError):
        await WindowClassifier(
            ScriptedProvider(
                [json.dumps({"agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING", "reason_code": "x"})]
            )
        ).classify(source, [], [])
    with pytest.raises(ValueError):
        await WindowClassifier(
            ScriptedProvider(
                [json.dumps({"conversation": "IGNORE", "agent_route": "NOPE", "reason_code": "x"})]
            )
        ).classify(source, [], [])
    with pytest.raises(ValueError):
        await WindowClassifier(ScriptedProvider(["not json"])).classify(source, [], [])


async def test_classifier_never_fabricates_bindings(store, session):
    source = make_window(store, session, text="real text")
    provider = ScriptedProvider(
        [
            {
                "conversation": "IGNORE",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "target_task_id": "invented",
                "expected_objective_revision": 9,
                "reason_code": "x",
            }
        ]
    )
    with pytest.raises(ValueError):
        await WindowClassifier(provider).classify(source, [], [])


async def test_classifier_privacy_filtering_per_entry(store, session):
    make_window(store, session, utterance_id="priv", text="local secret", scope="local_only")
    public = make_window(store, session, utterance_id="pub", text="public synthetic note")
    provider = ScriptedProvider(
        [
            {
                "conversation": "IGNORE",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "target_task_id": None,
                "expected_objective_revision": None,
                "reason_code": "x",
            }
        ],
        provider="meta",
        local=False,
    )
    context = store.read(session_id=session.session_id, kinds=["conversation.window"])
    await WindowClassifier(provider).classify(public, context, [])
    sent = json.dumps(provider.seen[0])
    assert "public synthetic note" in sent
    assert "local secret" not in sent


async def test_steer_sends_exact_text_and_revision(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session, revision=3)
    source = make_window(store, session, text="Also include the documentation.")
    make_decision(
        store,
        session,
        source,
        "RESPOND_WITHOUT_INTERRUPTING_USER",
        "STEER_CURRENT_TASK",
        task.task_id,
        3,
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    assert len(agent.steers) == 1
    target_id, request = agent.steers[0]
    assert target_id == task.task_id
    assert request.instruction == "Also include the documentation."
    assert request.expected_objective_revision == 3
    assert request.source_event_ids == [source.event_id]
    receipts = dispatch_receipts(store, session)
    assert any(r.payload["state"] == "applied" for r in receipts)


async def test_interrupt_never_claims_stop(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session, revision=1)
    source = make_window(store, session, text="Stop, don't delete that!")
    make_decision(
        store, session, source, "RESPOND_INTERRUPT_USER", "INTERRUPT_CURRENT_TASK", task.task_id, 1
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    assert len(agent.interrupts) == 1
    receipts = dispatch_receipts(store, session)
    applied = [r for r in receipts if r.payload["state"] == "applied"]
    assert applied and applied[0].payload["reason"] == "interrupt_requested"


async def test_queue_waits_for_target_then_drains_in_order(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session, revision=2)
    first = make_window(store, session, utterance_id="q1", text="Once done, check changelog.")
    second = make_window(store, session, utterance_id="q2", text="Then update readme.")
    make_decision(
        store,
        session,
        first,
        "RESPOND_WITHOUT_INTERRUPTING_USER",
        "QUEUE_UNTIL_TASK_DONE",
        task.task_id,
        2,
    )
    make_decision(
        store,
        session,
        second,
        "RESPOND_WITHOUT_INTERRUPTING_USER",
        "QUEUE_UNTIL_TASK_DONE",
        task.task_id,
        2,
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    assert agent.submits == []
    assert sum(1 for r in dispatch_receipts(store, session) if r.payload["state"] == "queued") == 2
    agent.set_state(task.task_id, TaskState.SUCCEEDED)
    reopened = WindowAgentDispatcher(decisions_journal(store), agent)
    await reopened.tick()
    await reopened.tick()
    assert [s.objective for s in agent.submits] == ["Once done, check changelog."]
    queued_task = next(
        t for t in agent.tasks.values() if t.objective == "Once done, check changelog."
    )
    agent.set_state(queued_task.task_id, TaskState.SUCCEEDED)
    await reopened.tick()
    assert [s.objective for s in agent.submits] == [
        "Once done, check changelog.",
        "Then update readme.",
    ]
    assert agent.submits[0].source_event_ids == [first.event_id]


async def test_untargeted_queue_submits_only_when_idle(store, session):
    agent = CapturingAgent()
    busy = agent.add_task(session)
    source = make_window(store, session, text="Write a summary.")
    make_decision(
        store, session, source, "RESPOND_WITHOUT_INTERRUPTING_USER", "QUEUE_UNTIL_TASK_DONE"
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    assert agent.submits == []
    agent.set_state(busy.task_id, TaskState.SUCCEEDED)
    await WindowAgentDispatcher(decisions_journal(store), agent).tick()
    await WindowAgentDispatcher(decisions_journal(store), agent).tick()
    assert len(agent.submits) == 1
    assert agent.submits[0].objective == "Write a summary."


async def test_same_text_new_window_does_not_duplicate_steer(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session, revision=1)
    first = make_window(
        store, session, utterance_id="u9", window_index=0, text="Also include docs.", revision=1
    )
    make_decision(
        store,
        session,
        first,
        "RESPOND_WITHOUT_INTERRUPTING_USER",
        "STEER_CURRENT_TASK",
        task.task_id,
        1,
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    assert len(agent.steers) == 1
    # Same cumulative text, only flags changed: still valid, but no duplicate action.
    second = make_window(
        store,
        session,
        utterance_id="u9",
        window_index=1,
        text="Also include docs.",
        revision=1,
        key="w:u9:1:r1",
    )
    agent.tasks[task.task_id] = agent.tasks[task.task_id].model_copy(
        update={"objective_revision": 1}
    )
    make_decision(
        store,
        session,
        second,
        "RESPOND_WITHOUT_INTERRUPTING_USER",
        "STEER_CURRENT_TASK",
        task.task_id,
        1,
    )
    await worker.tick()
    assert len(agent.steers) == 1
    assert any(
        r.payload.get("reason") == "duplicate_suppressed" for r in dispatch_receipts(store, session)
    )


async def test_changed_text_invalidates_old_decision(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session, revision=1)
    first = make_window(
        store, session, utterance_id="u7", window_index=0, text="Include docs.", revision=1
    )
    decision_entry = make_decision(
        store,
        session,
        first,
        "RESPOND_WITHOUT_INTERRUPTING_USER",
        "STEER_CURRENT_TASK",
        task.task_id,
        1,
    )
    make_window(
        store,
        session,
        utterance_id="u7",
        window_index=0,
        text="Include docs and tests.",
        revision=2,
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    assert agent.steers == []
    receipts = [r for r in dispatch_receipts(store, session) if r.payload.get("decision_id")]
    assert any(
        r.payload["reason"] in {"superseded_by_newer_window", "stale_or_unknown_source"}
        for r in receipts
    )
    assert decision_entry is not None


async def test_stale_revision_unknown_target_and_reset_rejected(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session, revision=5)
    stale = make_window(store, session, utterance_id="s1", text="Steer this.")
    make_decision(store, session, stale, "IGNORE", "STEER_CURRENT_TASK", task.task_id, 4)
    unknown = make_window(store, session, utterance_id="s2", text="Steer that.")
    make_decision(store, session, unknown, "IGNORE", "STEER_CURRENT_TASK", "nope", 1)
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    assert agent.steers == []
    reasons = {r.payload["reason"] for r in dispatch_receipts(store, session)}
    assert "stale_revision" in reasons
    assert "unknown_target" in reasons
    store.reset(session.session_id)
    worker2 = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker2.tick()
    assert agent.steers == []


async def test_unknown_acknowledgment_does_not_block_urgent(store, session):
    import httpx

    agent = CapturingAgent()
    task = agent.add_task(session, revision=1)
    agent.fail_next["steer"] = httpx.ConnectError("link down")
    first = make_window(store, session, utterance_id="t1", text="Add docs.")
    make_decision(store, session, first, "IGNORE", "STEER_CURRENT_TASK", task.task_id, 1)
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    urgent_src = make_window(store, session, utterance_id="t2", text="Stop now!")
    make_decision(
        store,
        session,
        urgent_src,
        "RESPOND_INTERRUPT_USER",
        "INTERRUPT_CURRENT_TASK",
        task.task_id,
        1,
    )
    await worker.tick()
    states = {r.payload["state"] for r in dispatch_receipts(store, session)}
    assert "unknown" in states
    assert len(agent.interrupts) == 1
    assert worker.last_error is None or isinstance(worker.last_error, str)


async def test_disabled_records_non_application_without_replay(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session, revision=1)
    source = make_window(store, session, text="Add docs.")
    make_decision(store, session, source, "IGNORE", "STEER_CURRENT_TASK", task.task_id, 1)
    worker = WindowAgentDispatcher(decisions_journal(store), agent, enabled=False)
    await worker.tick()
    assert agent.steers == []
    assert any(r.payload["state"] == "not_applied" for r in dispatch_receipts(store, session))
    worker.enabled = True
    await worker.tick()
    assert agent.steers == []


async def test_cancelled_target_does_not_release_followup(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session)
    source = make_window(store, session, text="After that, check the public readme.")
    make_decision(store, session, source, "IGNORE", "QUEUE_UNTIL_TASK_DONE", task.task_id, 1)
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.tick()
    agent.set_state(task.task_id, TaskState.CANCELLED)
    await worker.tick()
    assert not agent.submits
    assert any(e.payload["reason"] == "target_cancelled" for e in dispatch_receipts(store, session))


async def test_receipts_after_long_history_prevent_replay(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session)
    for n in range(220):
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                kind="routing.window_dispatch",
                payload={"decision_id": f"old-{n}", "state": "applied"},
                idempotency_key=f"old-{n}",
            ),
            "decisions",
        )
    source = make_window(store, session, text="Add a public changelog.")
    decision = make_decision(
        store, session, source, "IGNORE", "STEER_CURRENT_TASK", task.task_id, 1
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.process(decision)
    await WindowAgentDispatcher(decisions_journal(store), agent).process(decision)
    assert len(agent.steers) == 1


async def test_late_older_action_cannot_revive_work_after_interrupt(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session)
    earlier = make_window(store, session, utterance_id="earlier", text="Then write a summary.")
    later = make_window(store, session, utterance_id="later", text="Stop now.")
    stop = make_decision(
        store, session, later, "WAIT_FOR_NEXT_WINDOW", "INTERRUPT_CURRENT_TASK", task.task_id, 1
    )
    queued = make_decision(store, session, earlier, "IGNORE", "QUEUE_UNTIL_TASK_DONE")
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.process(stop)
    await worker.process(queued)
    assert len(agent.interrupts) == 1 and not agent.submits
    assert any(e.payload["reason"] == "later_interrupt" for e in dispatch_receipts(store, session))


async def test_same_speech_cannot_submit_twice_when_model_changes_route(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session)
    first = make_window(store, session, text="Add public documentation.")
    queued = make_decision(
        store, session, first, "IGNORE", "QUEUE_UNTIL_TASK_DONE", task.task_id, 1
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    await worker.process(queued)
    later = make_window(store, session, window_index=1, text="Add public documentation.")
    steered = make_decision(store, session, later, "IGNORE", "STEER_CURRENT_TASK", task.task_id, 1)
    await worker.process(steered)
    assert not agent.steers and not agent.submits


async def test_disabling_agent_retires_already_queued_work(store, session):
    agent = CapturingAgent()
    task = agent.add_task(session)
    source = make_window(store, session, text="After this, check the readme.")
    decision = make_decision(
        store, session, source, "IGNORE", "QUEUE_UNTIL_TASK_DONE", task.task_id, 1
    )
    await WindowAgentDispatcher(decisions_journal(store), agent).process(decision)
    agent.set_state(task.task_id, TaskState.SUCCEEDED)
    disabled = WindowAgentDispatcher(decisions_journal(store), agent, enabled=False)
    await disabled.tick()
    disabled.enabled = True
    await disabled.tick()
    assert not agent.submits


@pytest.mark.parametrize("scope", ["local_only", "private_cloud_authorized"])
async def test_contributor_never_receives_private_window_source(store, session, scope):
    source = make_window(store, session, text="PRIVATE_FIXTURE_SOURCE", scope=scope)
    provider = ScriptedProvider([], provider="meta", local=False)
    with pytest.raises(ValueError, match="disclosure"):
        await WindowClassifier(provider).classify(source, [], [])
    assert not provider.seen


async def test_classifier_receives_full_current_hypothesis(store, session):
    text = "Public words " * 190 + "Stop, do not delete that."
    source = make_window(store, session, text=text)
    provider = ScriptedProvider(
        [
            {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "reason_code": "test",
            }
        ]
    )
    await WindowClassifier(provider).classify(source, [], [])
    assert json.loads(provider.seen[0][1]["content"])["latest"]["text"] == text


async def test_maximum_normal_window_can_classify_without_truncation(store, session):
    text = "Public sentence. " * 999
    text = text[:16000]
    source = make_window(store, session, text=text)
    provider = ScriptedProvider(
        [
            {
                "conversation": "WAIT_FOR_NEXT_WINDOW",
                "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "reason_code": "long_window",
            }
        ]
    )
    await WindowClassifier(provider).classify(source, [], [])
    assert json.loads(provider.seen[0][1]["content"])["latest"]["text"] == text


class BoundaryProvider(NativeScriptedProvider):
    """Explicit synthetic inference; the native Agent still executes real file tools."""

    def __init__(self, actions):
        super().__init__([json.dumps(a) for a in actions])
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def complete(self, messages):
        if not self.calls:
            self.entered.set()
            await self.release.wait()
        return await super().complete(messages)


def write(path, text):
    return {"tool": "write_file", "arguments": {"path": path, "content": text}}


def finish(revision=1):
    return {
        "tool": "finish",
        "arguments": {
            "summary": "Verified public synthetic fixture",
            "objective_revision": revision,
        },
    }


async def test_native_agent_queue_executes_only_after_boundary(store, session, tmp_path):
    provider = BoundaryProvider(
        [write("first.txt", "first"), finish(), write("second.txt", "second"), finish()]
    )
    agent = AgentRuntime(
        LocalJournal(store, "agent"), provider, tmp_path, ToolPolicy(tmp_path, allow_write=True)
    )
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    try:
        task = await agent.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                objective="Write first.txt with public synthetic text.",
                idempotency_key="first",
            )
        )
        await asyncio.wait_for(provider.entered.wait(), 1)
        source = make_window(
            store, session, text="Once that finishes, write second.txt with public synthetic text."
        )
        decision = make_decision(
            store,
            session,
            source,
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "QUEUE_UNTIL_TASK_DONE",
            task.task_id,
            1,
        )
        await worker.process(decision)
        assert not (tmp_path / "second.txt").exists()
        assert len(await agent.status()) == 1
        provider.release.set()
        assert (await agent.wait(task.task_id)).state == TaskState.SUCCEEDED
        await worker.tick()
        followup = next(t for t in await agent.status() if t.task_id != task.task_id)
        result = await agent.wait(followup.task_id)
        assert result.state == TaskState.SUCCEEDED, result.error
        assert (tmp_path / "second.txt").read_text() == "second"
        assert result.source_event_ids == [source.event_id]
        assert result.evidence[0].sha256
    finally:
        await agent.close()


async def test_native_agent_window_interrupt_cancels_inflight_work(store, session, tmp_path):
    provider = BoundaryProvider([write("must-not-exist.txt", "late"), finish()])
    agent = AgentRuntime(
        LocalJournal(store, "agent"), provider, tmp_path, ToolPolicy(tmp_path, allow_write=True)
    )
    try:
        task = await agent.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                objective="Public synthetic work",
                idempotency_key="work",
            )
        )
        await asyncio.wait_for(provider.entered.wait(), 1)
        source = make_window(store, session, text="Stop, do not write that.")
        decision = make_decision(
            store,
            session,
            source,
            "RESPOND_INTERRUPT_USER",
            "INTERRUPT_CURRENT_TASK",
            task.task_id,
            1,
        )
        await WindowAgentDispatcher(decisions_journal(store), agent).process(decision)
        assert (await agent.wait(task.task_id)).state == TaskState.CANCELLED
        assert not (tmp_path / "must-not-exist.txt").exists()
    finally:
        await agent.close()


async def test_native_agent_rechecks_window_steering_at_execution_boundary(
    store, session, tmp_path
):
    provider = BoundaryProvider(
        [
            write("discard.txt", "obsolete inference"),
            write("original.txt", "valid original"),
            finish(),
        ]
    )
    agent = AgentRuntime(
        LocalJournal(store, "agent"), provider, tmp_path, ToolPolicy(tmp_path, allow_write=True)
    )
    try:
        task = await agent.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                objective="Public synthetic original task",
                idempotency_key="work",
            )
        )
        await asyncio.wait_for(provider.entered.wait(), 1)
        source = make_window(store, session, text="Also include public documentation.")
        decision = make_decision(
            store, session, source, "IGNORE", "STEER_CURRENT_TASK", task.task_id, 1
        )
        await WindowAgentDispatcher(decisions_journal(store), agent).process(decision)
        make_window(store, session, text="Corrected recognition", revision=2)
        provider.release.set()
        result = await agent.wait(task.task_id)
        assert result.state == TaskState.SUCCEEDED, result.error
        assert result.objective_revision == 1 and source.event_id not in result.source_event_ids
        assert not (tmp_path / "discard.txt").exists()
        assert any(
            e.payload.get("op") == "steer-rejected"
            for e in store.read(task_id=task.task_id, limit=200)
        )
    finally:
        await agent.close()


async def test_queue_drain_does_not_starve_ninth_session(store, session):
    agent = CapturingAgent()
    worker = WindowAgentDispatcher(decisions_journal(store), agent)
    tasks = []
    for n in range(9):
        current = store.create_session(SessionRequest(channel="converse"))
        task = agent.add_task(current)
        tasks.append(task)
        source = make_window(store, current, text=f"After that, write public note {n}.")
        decision = make_decision(
            store, current, source, "IGNORE", "QUEUE_UNTIL_TASK_DONE", task.task_id, 1
        )
        await worker.process(decision)
    agent.set_state(tasks[-1].task_id, TaskState.SUCCEEDED)
    await worker._drain_all()
    await worker._drain_all()
    assert len(agent.submits) == 1
    assert agent.submits[0].session_id == tasks[-1].session_id


async def test_native_source_admission_rejects_late_work_after_stop(store, session, tmp_path):
    from core.policy import PolicyDenied

    source = make_window(store, session, text="Write a public note.")
    stop = make_window(store, session, utterance_id="stop", text="Stop now.")
    make_decision(
        store, session, stop, "WAIT_FOR_NEXT_WINDOW", "INTERRUPT_CURRENT_TASK", "task-id", 1
    )
    provider = NativeScriptedProvider([])
    agent = AgentRuntime(LocalJournal(store, "agent"), provider, tmp_path, ToolPolicy(tmp_path))
    try:
        with pytest.raises(PolicyDenied, match="interruption"):
            await agent.submit(
                TaskRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=1,
                    objective=source.payload["text"],
                    source_event_ids=[source.event_id],
                    idempotency_key="late-source",
                ),
                actor="decisions",
            )
        assert not provider.calls and not await agent.status()
    finally:
        await agent.close()
