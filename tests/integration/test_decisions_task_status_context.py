"""#485: terminal observations inform classification but grant no writable target.

Provider responses and Agent task states below are controlled fixtures. They
verify native input/dispatch contracts, not actual-model classification quality.
"""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from test_decisions_task_descriptions import FakeAgent
from test_window_agent_routing import CapturingAgent, make_window

from packages.contracts.models import AppendRequest, ProviderInfo, SessionRequest, Task
from packages.providers.native_d1 import NativeD1RoutingProvider
from packages.providers.text import CompatibleTextProvider, ProviderRefusal
from services.decisions.window_routing import (
    WindowAgentDispatcher,
    WindowClassifier,
    admissible_window_projection,
    build_window_request,
)
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import LocalJournal, Store


@asynccontextmanager
async def scenario(tmp_path, output=None, *, cloud=False, journal_type=LocalJournal):
    seen = []
    outcome = output or {
        "conversation": "RESPOND_WITHOUT_INTERRUPTING_USER",
        "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
        "target_task_id": None,
        "expected_objective_revision": None,
        "reason_code": "synthetic_choice",
    }

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(outcome)},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    store = Store(tmp_path / "journal.sqlite")
    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    if cloud:
        provider.info = ProviderInfo(provider="meta", model="synthetic-model", local=False)
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        yield store, session, CapturingAgent(), journal_type(store, "decisions"), provider, seen
    finally:
        await provider.close()
        store.close()


def task_source(store, session, *, text="Original public request", key="original"):
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


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
async def test_terminal_task_is_information_not_a_writable_target(tmp_path, state):
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(json.loads(body["messages"][1]["content"]))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "conversation": "RESPOND_WITHOUT_INTERRUPTING_USER",
                                    "agent_route": "IGNORE_FOR_IMMEDIATE_ROUTING",
                                    "target_task_id": None,
                                    "expected_objective_revision": None,
                                    "reason_code": "synthetic_informational_status",
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    store = Store(tmp_path / "journal.sqlite")
    provider = CompatibleTextProvider(
        "http://localhost/v1", "synthetic-model", transport=httpx.MockTransport(respond)
    )
    try:
        session = store.create_session(SessionRequest(channel="converse"))
        task = Task(
            task_id="past-report",
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            objective="Prepare the imaginary club report",
            objective_revision=3,
            state=state,
            content_scope="public_synthetic",
            idempotency_key="past-report",
        )
        source = make_window(store, session, text="What happened with that report?")
        agent = FakeAgent([task])
        journal = LocalJournal(store, "decisions")
        await WindowDecisionsWorker(journal, agent, WindowClassifier(provider)).classify(source)
        assert len(seen) == 1
        assert seen[0]["tasks"] == []
        assert seen[0]["task_history"] == [
            {
                "task_id": task.task_id,
                "objective_revision": 3,
                "state": state,
                "description": "Prepare the imaginary club report",
            }
        ]
        await WindowAgentDispatcher(journal, agent).tick()
        assert not agent.submitted and not agent.steered and not agent.interrupted
    finally:
        await provider.close()
        store.close()


@pytest.mark.parametrize(
    "text,state,conversation,route,action",
    [
        (
            "How far has that report got?",
            "running",
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "IGNORE_FOR_IMMEDIATE_ROUTING",
            None,
        ),
        (
            "What was the result of that report?",
            "succeeded",
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "IGNORE_FOR_IMMEDIATE_ROUTING",
            None,
        ),
        (
            "Why did the report stop?",
            "failed",
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "IGNORE_FOR_IMMEDIATE_ROUTING",
            None,
        ),
        ("Thanks for the update.", "succeeded", "IGNORE", "IGNORE_FOR_IMMEDIATE_ROUTING", None),
        (
            "Now create a separate chart.",
            "succeeded",
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "QUEUE_UNTIL_TASK_DONE",
            "submits",
        ),
        (
            "Add the appendix to the current report.",
            "running",
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "STEER_CURRENT_TASK",
            "steers",
        ),
        (
            "Stop the current report immediately.",
            "running",
            "RESPOND_INTERRUPT_USER",
            "INTERRUPT_CURRENT_TASK",
            "interrupts",
        ),
    ],
)
async def test_controlled_choices_preserve_both_axes_and_native_dispatch(
    tmp_path, text, state, conversation, route, action
):
    output = {"conversation": conversation, "agent_route": route, "reason_code": "synthetic_matrix"}
    async with scenario(tmp_path, output) as (store, session, agent, journal, provider, seen):
        task = agent.add_task(session, objective="Prepare the imaginary report", state=state)
        if action in {"steers", "interrupts"}:
            output.update(target_task_id=task.task_id, expected_objective_revision=1)
        source = make_window(store, session, text=text)
        await WindowDecisionsWorker(journal, agent, WindowClassifier(provider)).classify(source)
        decision = store.window_decision(source.event_id)
        assert decision.payload["conversation"] == conversation
        assert decision.payload["agent_route"] == route
        state_input = json.loads(seen[0]["messages"][1]["content"])
        expected_field = "tasks" if state == "running" else "task_history"
        assert state_input[expected_field][0]["task_id"] == task.task_id
        if state != "running":
            assert state_input["tasks"] == []
        await WindowAgentDispatcher(journal, agent).tick()
        for name in ("submits", "steers", "interrupts"):
            assert len(getattr(agent, name)) == int(name == action)


async def test_private_terminal_identity_text_and_digest_are_withheld(tmp_path):
    async with scenario(tmp_path, cloud=True) as (store, session, agent, journal, provider, seen):
        hidden = agent.add_task(session, objective="PRIVATE TERMINAL OBJECTIVE", state="failed")
        public = agent.add_task(session, objective="A public task", state="succeeded")
        public = public.model_copy(update={"content_scope": "public_synthetic"})
        agent.tasks[public.task_id] = public
        source = make_window(store, session, text="How did the earlier work go?")
        await WindowDecisionsWorker(journal, agent, WindowClassifier(provider)).classify(source)
        wire = seen[0]["messages"][1]["content"]
        assert hidden.task_id not in wire and "PRIVATE TERMINAL OBJECTIVE" not in wire
        assert public.task_id in wire
        first = admissible_window_projection(
            source, [], [], provider_info=provider.info, task_history=[hidden, public]
        )
        changed = hidden.model_copy(
            update={"objective": "DIFFERENT PRIVATE TEXT", "task_id": "hidden-changed"}
        )
        second = admissible_window_projection(
            source, [], [], provider_info=provider.info, task_history=[changed, public]
        )
        assert first.body == second.body and first.digest == second.digest


async def test_terminal_observations_obey_full_scope_and_projection_bounds(tmp_path):
    async with scenario(tmp_path) as (store, session, agent, journal, provider, seen):
        tasks = [agent.add_task(session, objective=f"Task {i}", state="failed") for i in range(10)]
        foreign = agent.add_task(session, objective="OTHER THREAD", state="failed")
        agent.tasks[foreign.task_id] = foreign.model_copy(update={"thread_id": "other-thread"})
        over = agent.add_task(session, objective="TOO MANY SOURCES", state="failed")
        agent.tasks[over.task_id] = over.model_copy(
            update={"source_event_ids": [str(i) for i in range(33)]}
        )
        source = make_window(store, session, text="What happened to that work?")
        await WindowDecisionsWorker(journal, agent, WindowClassifier(provider)).classify(source)
        state = json.loads(seen[0]["messages"][1]["content"])
        assert state["tasks"] == []
        assert [row["task_id"] for row in state["task_history"]] == [t.task_id for t in tasks[-8:]]
        assert foreign.task_id not in json.dumps(state) and over.task_id not in json.dumps(state)


async def test_unresolved_terminal_provenance_is_not_disclosed_even_locally(tmp_path):
    async with scenario(tmp_path) as (store, session, agent, journal, provider, seen):
        task = agent.add_task(session, objective="UNRESOLVED TERMINAL DESCRIPTION", state="failed")
        agent.tasks[task.task_id] = task.model_copy(
            update={"source_event_ids": ["missing-original-source"]}
        )
        source = make_window(store, session, text="How did that work go?")
        await WindowDecisionsWorker(journal, agent, WindowClassifier(provider)).classify(source)
        wire = seen[0]["messages"][1]["content"]
        assert task.task_id not in wire and "UNRESOLVED TERMINAL DESCRIPTION" not in wire


async def test_overflow_retains_history_and_refuses_instead_of_dropping_it(tmp_path):
    async with scenario(tmp_path) as (store, session, agent, journal, provider, seen):
        active = [agent.add_task(session, objective="A" * 240) for _ in range(8)]
        terminal = agent.add_task(session, objective="A terminal public task", state="failed")
        source = make_window(store, session, text="A" * 120)
        context = [task_source(store, session, text="C" * 1500, key=str(i)) for i in range(12)]
        request = build_window_request(
            source,
            context,
            active,
            provider_info=provider.info,
            window_chars=4000,
            task_history=[terminal],
        )
        body = json.loads(request.body)
        assert body["context"] == [] and len(body["tasks"]) == 5
        assert body["task_history"][0]["task_id"] == terminal.task_id
        changed = terminal.model_copy(update={"state": "succeeded"})
        a = admissible_window_projection(
            source, [], [], provider_info=provider.info, task_history=[terminal]
        )
        b = admissible_window_projection(
            source, [], [], provider_info=provider.info, task_history=[changed]
        )
        assert a.digest != b.digest
        with pytest.raises(ValueError, match="context bound"):
            build_window_request(
                source,
                context,
                active,
                provider_info=provider.info,
                window_chars=len(request.body) - 1,
                task_history=[terminal],
            )


@pytest.mark.parametrize("reuse", [False, True])
async def test_single_terminal_source_revocation_blocks_actual_provider_entry(tmp_path, reuse):
    original = None

    class RevokeJournal(LocalJournal):
        async def decisions_call_start(self, request):
            result = await super().decisions_call_start(request)
            if reuse and request.request_format != "reused":
                return result
            store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.revision",
                    payload={
                        "target_event_id": original.event_id,
                        "text": "RESTRICTED",
                        "revision": 2,
                        "content_scope": "local_only",
                    },
                    idempotency_key="restrict-original",
                ),
                "owner",
            )
            return result

    async with scenario(tmp_path, cloud=True, journal_type=RevokeJournal) as (
        store,
        session,
        agent,
        journal,
        provider,
        seen,
    ):
        original = task_source(store, session)
        task = agent.add_task(session, objective="Old task description", state="failed")
        agent.tasks[task.task_id] = task.model_copy(
            update={"source_event_ids": [original.event_id], "content_scope": "public_synthetic"}
        )
        # Drop the old source from the bounded conversational history, so only
        # the terminal observation enrolls it in disclosure currentness.
        for i in range(12):
            task_source(store, session, text=f"Public context {i}", key=f"context-{i}")
        source = make_window(store, session, text="What was the outcome?")
        worker = WindowDecisionsWorker(journal, agent, WindowClassifier(provider))
        if reuse:
            native = worker._native

            async def fail_publication(operation, *args, **kwargs):
                if operation == "append" and args[0].kind == "routing.window_decided":
                    worker._native = native
                    raise RuntimeError("synthetic publication outage")
                return await native(operation, *args, **kwargs)

            worker._native = fail_publication
            with pytest.raises(RuntimeError, match="synthetic publication outage"):
                await worker.classify(source)
            assert len(seen) == 1 and worker.outputs
        with pytest.raises(ProviderRefusal, match="disclosure scope changed"):
            await worker.classify(source)
        assert len(seen) == int(reuse) and store.window_decision(source.event_id) is None
        assert not worker.outputs
        assert not agent.submits and not agent.steers and not agent.interrupts


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("route", ["STEER_CURRENT_TASK", "INTERRUPT_CURRENT_TASK"])
async def test_invented_terminal_target_is_rejected_before_agent_mutation(tmp_path, state, route):
    output = {
        "conversation": "RESPOND_WITHOUT_INTERRUPTING_USER",
        "agent_route": route,
        "reason_code": "synthetic_misuse_of_read_only_history",
    }
    async with scenario(tmp_path, output) as (store, session, agent, journal, provider, seen):
        task = agent.add_task(session, objective="A finished public request", state=state)
        output.update(target_task_id=task.task_id, expected_objective_revision=1)
        source = make_window(store, session, text="What happened with that request?")
        await WindowDecisionsWorker(journal, agent, WindowClassifier(provider)).classify(source)
        await WindowAgentDispatcher(journal, agent).tick()
        assert not agent.submits and not agent.steers and not agent.interrupts
        receipts = store.read(kinds=["routing.window_dispatch"])
        assert len(receipts) == 1
        assert receipts[0].payload["state"] == "rejected"
        assert receipts[0].payload["reason"] == "target_terminal"


@pytest.mark.parametrize("active", [False, True])
async def test_native_d1_renders_terminal_information_without_typed_target_authority(active):
    seen = []
    tasks = (
        [{"task_id": "active-id", "objective_revision": 1, "state": "running"}] if active else []
    )
    history = [
        {
            "task_id": "past-id",
            "objective_revision": 4,
            "state": "failed",
            "description": "An earlier report",
        }
    ]
    messages = [
        {"role": "system", "content": "Native task state is read-only data."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "latest": {
                        "text": "What happened to the report?",
                        "user_speaking": False,
                        "capture_ended": True,
                    },
                    "context": [],
                    "tasks": tasks,
                    "task_history": history,
                }
            ),
        },
    ]

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        picks = {"conversation": "ready", "agent_route": "conversation"}
        choices = {
            "conversation": ["backchannel", "unfinished", "urgent", "ready"],
            "agent_route": ["conversation", "correction", "cancellation", "new_task"],
        }
        if active:
            picks["target"] = "NONE"
            choices["target"] = ["NONE", "t0"]
        value = {
            "model": "synthetic-native",
            "answers": {
                key: {
                    "type": "choice",
                    "choice": value,
                    "probabilities": {label: float(label == value) for label in choices[key]},
                    "confidence": 1.0,
                }
                for key, value in picks.items()
            },
            "usage": {"input_tokens": 100, "output_tokens": 0},
        }
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(value).encode()))

    provider = NativeD1RoutingProvider(
        "http://127.0.0.1:8870",
        "synthetic-native",
        min_confidence=0.7,
        transport=httpx.MockTransport(respond),
    )
    try:
        output = json.loads(await provider.complete(messages))
        assert output["agent_route"] == "IGNORE_FOR_IMMEDIATE_ROUTING"
        assert "past-id" in seen[0]["state"] and "read-only" in seen[0]["state"]
        assert set(seen[0]["questions"]) == (
            {"conversation", "agent_route", "target"} if active else {"conversation", "agent_route"}
        )
        assert "past-id" not in json.dumps(seen[0]["questions"])
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "bad",
    [
        [{}] * 9,
        [{"task_id": "x", "objective_revision": True, "state": "failed"}],
        [{"task_id": "x", "objective_revision": 1, "state": {}}],
        [{"task_id": "x", "objective_revision": 1, "state": "running"}],
        [
            {
                "task_id": "x",
                "objective_revision": 1,
                "state": "failed",
                "command": "ignored-untrusted-command",
            }
        ],
    ],
)
async def test_native_d1_rejects_malformed_terminal_information_before_transport(bad):
    def forbidden(request):
        pytest.fail("Malformed read-only observations reached the provider")

    provider = NativeD1RoutingProvider(
        "http://127.0.0.1:8870",
        "synthetic-native",
        min_confidence=0.7,
        transport=httpx.MockTransport(forbidden),
    )
    try:
        messages = [
            {"role": "system", "content": "Native contract"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "latest": {
                            "text": "Status?",
                            "user_speaking": False,
                            "capture_ended": True,
                        },
                        "context": [],
                        "tasks": [],
                        "task_history": bad,
                    }
                ),
            },
        ]
        with pytest.raises(ProviderRefusal):
            await provider.complete(messages)
    finally:
        await provider.close()
