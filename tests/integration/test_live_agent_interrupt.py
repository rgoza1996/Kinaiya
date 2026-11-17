"""Native interrupt provenance and uncertain original handoffs (#671)."""

import asyncio
import json

import httpx
import pytest

from packages.agent_adapters.http import AgentClient
from packages.contracts.live_effects import effect_key
from packages.contracts.models import InterruptRequest, TaskRequest
from packages.providers.text import ScriptedProvider
from services.agent.app import create_app
from services.agent.runtime import IdemConflict
from services.common.auth import Auth
from services.decisions.live_agent import LiveAgentDispatcher
from services.session.live_windows import publish
from services.session.store import LocalJournal
from tests.integration.test_live_agent_consumer import native, selected
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_effect_http import TOKENS
from tests.integration.test_live_window_publication import original


async def running(live, tmp_path):
    entered = asyncio.Event()

    class Waiting(ScriptedProvider):
        async def complete(self, messages):
            entered.set()
            await asyncio.Event().wait()

    agent = native(live, tmp_path, Waiting([]))
    task = await agent.submit(
        TaskRequest(
            session_id=live.session.session_id,
            thread_id=live.session.thread_id,
            epoch=live.session.epoch,
            objective="Public interrupt recovery control",
            content_scope="public_synthetic",
            idempotency_key="public-interrupt-recovery-target",
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    return agent, task


@pytest.mark.parametrize("boundary", ["agent_ack", "journal_ack"])
@pytest.mark.parametrize("committed", [False, True])
async def test_original_interrupt_reconciles_actual_commit_without_replay(
    live, tmp_path, monkeypatch, boundary, committed
):
    agent, target = await running(live, tmp_path)
    item = selected(live, "INTERRUPT_CURRENT_TASK", target)
    actual_append = agent.journal.append
    journal_writes, calls = [], []

    async def append(request):
        if request.kind == "agent.command" and request.payload.get("op") == "interrupt":
            journal_writes.append(request.model_copy(deep=True))
            if committed:
                await actual_append(request)
            raise OSError("Public original interrupt journal acknowledgment lost")
        return await actual_append(request)

    if boundary == "journal_ack":
        monkeypatch.setattr(agent.journal, "append", append)
    asgi = httpx.ASGITransport(app=create_app(agent, Auth(TOKENS)))

    class Lost(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("Public original interrupt Agent acknowledgment lost")
            yield b""

        async def aclose(self):
            pass

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.method == "POST" and request.url.path.endswith("/interrupt"):
                calls.append(json.loads(request.content))
                if boundary == "agent_ack":
                    response = (
                        await asgi.handle_async_request(request)
                        if committed
                        else httpx.Response(200)
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
        assert calls[0]["idempotency_key"] == effect_key(record.receipt)
        assert calls[0]["source_event_ids"] == [live.pair.source.event_id]
        assert calls[0]["source_revision"] == live.pair.source.payload["revision"]
        commands = [
            entry
            for entry in live.store.read(kinds=["agent.command"])
            if entry.payload.get("op") == "interrupt"
        ]
        assert len(commands) == int(committed)
        if committed:
            command = commands[0]
            assert command.origin == "agent-supervisor"
            assert command.payload["idempotency_key"] == calls[0]["idempotency_key"]
            assert command.payload["source_revision"] == calls[0]["source_revision"]
            assert command.causes == calls[0]["source_event_ids"]
            assert command.event_id == record.completion.evidence.event_id
        assert len(journal_writes) == int(boundary == "journal_ack")
        assert await dispatcher.process(item) == record
        replacement = LiveAgentDispatcher(LocalJournal(live.store, "decisions"), client)
        assert await replacement.process(item) == record
        assert len(calls) == 1
        assert len(journal_writes) == int(boundary == "journal_ack")
    finally:
        await dispatcher.close()
        if replacement is not None:
            await replacement.close()
        await client.close()
        await agent.close()


async def test_native_interrupt_idempotency_binds_original_raw_revision(live, tmp_path):
    agent, target = await running(live, tmp_path)
    request = InterruptRequest(
        expected_objective_revision=target.objective_revision,
        idempotency_key="public-interrupt-original-source",
        reason=live.pair.source.payload["text"],
        source_event_ids=[live.pair.source.event_id],
        source_revision=live.pair.source.payload["revision"],
    )
    try:
        await agent.interrupt(target.task_id, request, actor="decisions")
        await agent.interrupt(target.task_id, request.model_copy(deep=True), actor="decisions")
        commands = [
            entry
            for entry in live.store.read(kinds=["agent.command"])
            if entry.payload.get("op") == "interrupt"
        ]
        assert len(commands) == 1
        assert commands[0].payload["source_revision"] == request.source_revision
        publish(live.store, original(live, revision=2))
        changed = request.model_copy(update={"source_revision": 2})
        with pytest.raises(IdemConflict):
            await agent.interrupt(target.task_id, changed, actor="decisions")
        assert (
            len(
                [
                    entry
                    for entry in live.store.read(kinds=["agent.command"])
                    if entry.payload.get("op") == "interrupt"
                ]
            )
            == 1
        )
    finally:
        await agent.close()


async def test_native_interrupt_idempotency_cannot_change_authenticated_actor(live, tmp_path):
    agent, target = await running(live, tmp_path)
    request = InterruptRequest(
        expected_objective_revision=target.objective_revision,
        idempotency_key="public-interrupt-original-actor",
        source_event_ids=[live.pair.source.event_id],
        source_revision=live.pair.source.payload["revision"],
    )
    try:
        await agent.interrupt(target.task_id, request, actor="decisions")
        with pytest.raises(IdemConflict):
            await agent.interrupt(target.task_id, request, actor="owner")
        commands = [
            entry
            for entry in live.store.read(kinds=["agent.command"])
            if entry.payload.get("op") == "interrupt"
        ]
        assert len(commands) == 1
        assert commands[0].payload["actor"] == "decisions"
    finally:
        await agent.close()


async def test_native_interrupt_keeps_original_request_during_source_validation(
    live, tmp_path, monkeypatch
):
    agent, target = await running(live, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    validate = agent._validated_source_texts

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await validate(*args, **kwargs)

    monkeypatch.setattr(agent, "_validated_source_texts", held)
    request = InterruptRequest(
        expected_objective_revision=target.objective_revision,
        idempotency_key="public-interrupt-mutable-caller",
        reason="Public original interrupt",
        source_event_ids=[live.pair.source.event_id],
        source_revision=live.pair.source.payload["revision"],
    )
    original_request = request.model_copy(deep=True)
    caller = asyncio.create_task(agent.interrupt(target.task_id, request, actor="decisions"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        request.source_event_ids.clear()
        request.source_revision = 7
        request.reason = "Public mutated caller metadata"
        release.set()
        await asyncio.wait_for(caller, 2)
        commands = [
            entry
            for entry in live.store.read(kinds=["agent.command"])
            if entry.payload.get("op") == "interrupt"
        ]
        assert len(commands) == 1
        command = commands[0]
        for key in ("idempotency_key", "source_event_ids", "source_revision", "reason"):
            assert command.payload[key] == getattr(original_request, key)
        assert command.causes == original_request.source_event_ids
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await agent.close()
