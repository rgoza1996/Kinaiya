"""Native live Agent handoffs and original ownership, with synthetic providers only."""

import asyncio
import json

import httpx
import pytest

from core.policy import ToolPolicy
from packages.agent_adapters.http import AgentClient
from packages.contracts.live_effect_pending import LiveEffectPendingItem
from packages.contracts.live_effects import effect_key
from packages.contracts.models import Task, TaskRequest
from packages.providers.text import ScriptedProvider
from services.agent.app import create_app as agent_app
from services.agent.runtime import AgentRuntime
from services.common.auth import Auth
from services.decisions.api import create_app
from services.decisions.live_agent import LiveAgentDispatcher
from services.session.live_windows import publish
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import fence
from tests.integration.test_live_decisions_attempts import admission, finish, output, publication
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_effect_http import TOKENS
from tests.integration.test_live_effects import outcome
from tests.integration.test_live_window_publication import original


def selected(live, route="QUEUE_UNTIL_TASK_DONE", target=None):
    if target is None:
        proof = outcome(live, "IGNORE", route)
    else:
        attempt = live.owner.admit(admission(live))
        decision = output(live, "IGNORE", route).model_copy(
            update={
                "target_task_id": target.task_id,
                "expected_objective_revision": target.objective_revision,
            }
        )
        completed = live.owner.finish(finish(live, attempt, decision))
        proof = publication(live, completed)
        live.owner.publish_result(proof)
    entry = live.owner.lookup_publication(proof).receipt
    return LiveEffectPendingItem(
        event_id=entry.event_id, sequence=entry.sequence, publication=proof
    )


def native(live, tmp_path, provider=None):
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    return AgentRuntime(
        LocalJournal(live.store, "agent"),
        provider
        if provider is not None
        else ScriptedProvider(
            [
                json.dumps(
                    {
                        "tool": "finish",
                        "arguments": {"summary": "Public fixture", "objective_revision": 1},
                    }
                )
            ]
        ),
        workspace,
        ToolPolicy(workspace),
    )


async def test_native_queue_records_one_authentic_agent_command_and_never_replays(live, tmp_path):
    item = selected(live)
    live.store.live_captures.fence(fence(live.source.capture.original))
    agent = native(live, tmp_path)
    dispatcher = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), agent)
    try:
        record = await dispatcher.process(item)
        original = dispatcher.routes[item.event_id]
        assert record.completion is not None, (
            str(original.finish_task.exception())
            if original.finish_task is not None
            else "no finish",
            [
                (entry.origin, entry.payload.get("op"))
                for entry in live.store.read(kinds=["agent.command"])
            ],
        )
        assert record.completion.status == "recorded"
        entries = live.store.read(kinds=["agent.command"])
        assert len(entries) == 1
        command = entries[0]
        assert command.payload["idempotency_key"] == effect_key(record.receipt)
        assert command.payload["objective"] == live.pair.source.payload["text"]
        assert command.payload["source_event_ids"] == [live.pair.source.event_id]
        assert command.payload["actor"] == "decisions"
        assert command.event_id == record.completion.evidence.event_id
        assert await dispatcher.process(item) == record
        replacement = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), agent)
        assert await replacement.process(item) == record
        assert len(live.store.read(kinds=["agent.command"])) == 1
        assert await replacement.close()
    finally:
        await dispatcher.close()
        await agent.close()


@pytest.mark.parametrize("route", ["STEER_CURRENT_TASK", "INTERRUPT_CURRENT_TASK"])
async def test_real_native_targeted_handoffs_preserve_exact_source_and_revision(
    live, tmp_path, route
):
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(ScriptedProvider):
        async def complete(self, messages):
            entered.set()
            await release.wait()
            return await super().complete(messages)

    agent = native(live, tmp_path, Waiting([]))
    dispatcher = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), agent)
    try:
        target = await agent.submit(
            TaskRequest(
                session_id=live.session.session_id,
                thread_id=live.session.thread_id,
                epoch=live.session.epoch,
                objective="Public held owner task",
                content_scope="public_synthetic",
                idempotency_key="public-held-native-task",
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        item = selected(live, route, target)
        result = await dispatcher.process(item)
        assert result.completion.status == "recorded", dispatcher.last_error
        evidence = live.store.entry(result.completion.evidence.event_id)
        assert evidence.origin == "agent-supervisor"
        assert evidence.payload["task_id"] == target.task_id
        assert (
            evidence.causes
            if route == "STEER_CURRENT_TASK"
            else evidence.payload["source_event_ids"]
        ) == [live.pair.source.event_id]
        assert evidence.payload["expected_objective_revision"] == target.objective_revision
        assert evidence.payload["actor"] == "decisions"
    finally:
        release.set()
        await dispatcher.close()
        await agent.close()


async def test_cancelled_delivery_facade_retains_original_handoff_task(live, tmp_path):
    item = selected(live)
    agent = native(live, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class HeldAgent:
        async def status(self):
            return await agent.status()

        async def submit(self, request, *, actor):
            calls.append(request)
            entered.set()
            await release.wait()
            return await agent.submit(request, actor=actor)

    dispatcher = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), HeldAgent())
    caller = asyncio.create_task(dispatcher.process(item))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert len(calls) == 1 and not dispatcher.routes[item.event_id].task.done()
        release.set()
        result = await dispatcher.process(item)
        assert result.completion.status == "recorded"
        assert len(calls) == 1 and len(live.store.read(kinds=["agent.command"])) == 1
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await dispatcher.close()
        await agent.close()


async def test_identical_native_raw_revision_cannot_stifle_canonical_agent_handoff(live, tmp_path):
    item = selected(live)
    duplicate = publish(live.store, original(live, revision=2))
    assert duplicate.source.payload["text"] == live.pair.source.payload["text"]
    assert live.store.resolve(live.pair.source.event_id).payload["revision"] == 2
    agent = native(live, tmp_path)
    dispatcher = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), agent)
    try:
        record = await dispatcher.process(item)
        assert record.completion.status == "recorded", dispatcher.last_error
        command = live.store.entry(record.completion.evidence.event_id)
        assert command.payload["objective"] == live.pair.source.payload["text"]
        assert command.payload["source_event_ids"] == [live.pair.source.event_id]
    finally:
        await dispatcher.close()
        await agent.close()


async def test_waiting_queue_retains_bounded_actual_owner_history(live, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(ScriptedProvider):
        async def complete(self, messages):
            entered.set()
            await release.wait()
            return await super().complete(messages)

    agent = native(live, tmp_path, Waiting([]))
    dispatcher = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), agent)
    try:
        await agent.submit(
            TaskRequest(
                session_id=live.session.session_id,
                thread_id=live.session.thread_id,
                epoch=live.session.epoch,
                objective="Public held owner task",
                content_scope="public_synthetic",
                idempotency_key="public-queue-boundary",
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        item = selected(live)
        for _ in range(20):
            record = await dispatcher.process(item)
            assert record.started is None and record.completion is None
        assert len(dispatcher.routes[item.event_id].owners) <= 2
        assert len(live.store.read(kinds=["agent.command"])) == 1
    finally:
        release.set()
        await dispatcher.close()
        await agent.close()


async def test_targeted_queue_cannot_assume_a_missing_native_target_has_finished(live):
    target = Task(
        session_id=live.session.session_id,
        thread_id=live.session.thread_id,
        epoch=live.session.epoch,
        objective="Public target no longer available",
        idempotency_key="public-gone-target",
    )
    item = selected(live, target=target)
    calls = []

    class Missing:
        async def status(self):
            return []

        async def submit(self, request, *, actor):
            calls.append(request)
            raise RuntimeError("Must not submit without native target status")

    dispatcher = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), Missing())
    try:
        record = await dispatcher.process(item)
        assert record.started is None and record.completion is None
        assert not calls
    finally:
        await dispatcher.close()


async def test_owned_service_loop_wakes_on_native_publication_without_idle_polling(live, tmp_path):
    class ObservedJournal(LocalJournal):
        calls = 0

        async def pending_live_effects(self, request):
            self.calls += 1
            return await super().pending_live_effects(request)

    journal = ObservedJournal(live.store, "decisions")
    agent = native(live, tmp_path)
    dispatcher = LiveAgentDispatcher(journal, agent)

    class PassiveFinalized:
        def __init__(self):
            self.journal = journal
            self.closed = False

        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.closed = True

    finalized = PassiveFinalized()
    app = create_app(finalized, Auth(TOKENS), live_dispatcher=dispatcher)
    try:
        async with app.router.lifespan_context(app):
            async with asyncio.timeout(3):
                while not live.store.journal_wake.waiters:
                    await asyncio.sleep(0)
            before = journal.calls
            await asyncio.sleep(0.02)
            assert journal.calls == before and dispatcher.ready
            item = selected(live)
            async with asyncio.timeout(3):
                while True:
                    state = await journal.live_effect_lookup(item.query("agent_route"))
                    if state.record is not None and state.record.completion is not None:
                        break
                    await asyncio.sleep(0)
            assert state.record.completion.status == "recorded"
            assert len(live.store.read(kinds=["agent.command"])) == 1
        assert finalized.closed and not dispatcher.running
        assert not live.store.journal_wake.waiters
    finally:
        await dispatcher.close()
        await agent.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_lost_agent_http_ack_never_replays_and_native_evidence_resolves_only_actual_commit(
    live, tmp_path, committed
):
    item = selected(live)
    agent = native(live, tmp_path)
    asgi = httpx.ASGITransport(app=agent_app(agent, Auth(TOKENS)))
    calls = []

    class Lost(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("Public synthetic lost native Agent ACK")
            yield b""

        async def aclose(self):
            pass

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.method == "POST" and request.url.path == "/v1/agent/tasks":
                calls.append(request.content)
                response = (
                    await asgi.handle_async_request(request) if committed else httpx.Response(200)
                )
                response.stream = Lost()
                return response
            return await asgi.handle_async_request(request)

    client = AgentClient("http://agent.local", TOKENS["decisions"], transport=Transport())
    dispatcher = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), client)
    replacement = None
    try:
        record = await dispatcher.process(item)
        assert record.completion.status == ("recorded" if committed else "uncertain")
        assert len(calls) == 1
        replacement = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), client)
        assert (await replacement.process(item)).completion.status == record.completion.status
        assert len(calls) == 1
        assert len(live.store.read(kinds=["agent.command"])) == int(committed)
    finally:
        await dispatcher.close()
        if replacement is not None:
            await replacement.close()
        await client.close()
        await agent.close()


async def test_benign_raw_revision_after_steer_acceptance_cannot_block_execution_boundary(
    live, tmp_path
):
    entered, release = asyncio.Event(), asyncio.Event()
    applying, apply_release = asyncio.Event(), asyncio.Event()

    class Waiting(ScriptedProvider):
        async def complete(self, messages):
            entered.set()
            await release.wait()
            return await super().complete(messages)

    agent = native(
        live,
        tmp_path,
        Waiting(
            [
                json.dumps(
                    {
                        "tool": "finish",
                        "arguments": {
                            "summary": "Public old-revision reply",
                            "objective_revision": 1,
                        },
                    }
                )
            ]
        ),
    )
    dispatcher = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), agent)
    apply_original = agent._apply_steer

    async def held_apply(rec):
        if rec.pending_steer is not None:
            applying.set()
            await apply_release.wait()
        return await apply_original(rec)

    agent._apply_steer = held_apply
    try:
        target = await agent.submit(
            TaskRequest(
                session_id=live.session.session_id,
                thread_id=live.session.thread_id,
                epoch=live.session.epoch,
                objective="Public original owner instruction",
                content_scope="public_synthetic",
                idempotency_key="public-late-native-revision",
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        item = selected(live, "STEER_CURRENT_TASK", target)
        record = await dispatcher.process(item)
        assert record.completion.status == "recorded"
        release.set()
        await asyncio.wait_for(applying.wait(), 2)
        publish(live.store, original(live, revision=2))
        apply_release.set()
        async with asyncio.timeout(3):
            while (
                agent._tasks[target.task_id].task.objective_revision == 1
                and not agent._tasks[target.task_id].done.is_set()
            ):
                await asyncio.sleep(0)
        updated = agent._tasks[target.task_id].task
        assert updated.objective_revision == 2, updated.error
        assert updated.objective.endswith(live.pair.source.payload["text"])
    finally:
        release.set()
        apply_release.set()
        await dispatcher.close()
        await agent.close()
