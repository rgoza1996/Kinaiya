"""Contract-first external control ownership, with genuine native metadata.

The standing chain supplies the only positive finalization trigger. The window
controls separately exercise existing native reads; their routing metadata
does not grant execution authority. No candidate implementation was consulted.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_agent_standing_claim_retirement import (
    FINALIZE,
    Gate,
    Transport,
    automatic_report,
    joined,
    launch,
    original_scope,
    reached,
)
from test_agent_standing_claim_retirement import claim_chain as claim_chain
from test_agent_standing_claim_retirement import factory as factory
from test_agent_standing_claim_retirement import physical as physical
from test_agent_standing_claim_retirement import stack as stack

from packages.contracts.client import JournalClient
from packages.contracts.models import (
    AppendRequest,
    InterruptRequest,
    JournalEntry,
    SessionRequest,
    SpeechWindow,
    TaskState,
    WindowDecision,
)
from packages.contracts.operation_ownership import ActualOperation, actual_owner, current_owner


async def test_external_interrupt_caller_is_not_joined_by_automatic_finalization(
    claim_chain, monkeypatch, record_property
):
    value = await claim_chain()
    value.generation.clear()
    accepted = await launch(value)
    assert accepted.disposition == "created"
    rec, provider = value.agent.record, value.agent.made[0]
    await reached(provider.generating)
    worker = rec.worker
    assert not worker.done() and not provider.generations and not provider.calls

    gate = Gate("/v1/transcript/entries", "close")
    value.gates.append(gate)
    boundary = value.journal_transport
    delegate = boundary.delegate
    actual_send, actual_append = delegate.handle_async_request, value.agent.journal.append
    publication = []
    reason = "Public synthetic external caller cancellation"

    async def append(request):
        if request.kind == "agent.command" and request.payload.get("op") == "interrupt":
            owner, scope = asyncio.current_task(), current_owner()
            publication.append(
                (owner, scope, isinstance(scope, ActualOperation) and owner in scope.handles)
            )
        return await actual_append(request)

    async def send(request):
        response = await actual_send(request)
        # Select only the genuine, already committed interrupt response. This
        # synchronous handoff lets the existing boundary wrap its original
        # body before another publication can consume the one-shot close gate.
        if request.url.path == gate.path:
            body = json.loads(request.content)
            if body.get("kind") == "agent.command" and body["payload"].get("op") == "interrupt":
                boundary.gate = gate
        return response

    monkeypatch.setattr(value.agent.journal, "append", append)
    monkeypatch.setattr(delegate, "handle_async_request", send)
    returned, park = asyncio.Event(), asyncio.Event()
    answers = []

    async def external_caller():
        answers.append(
            await value.agent.runtime.interrupt(
                value.agent.task_id,
                InterruptRequest(
                    expected_objective_revision=1,
                    idempotency_key="external-public-interrupt",
                    reason=reason,
                ),
            )
        )
        returned.set()
        await park.wait()

    caller = asyncio.create_task(external_caller())
    value.public.append(caller)
    try:
        await reached(gate.entered)
        assert len(publication) == 1
        control, control_scope, retained_before_native_await = publication[0]
        _, scope = original_scope(value)
        assert control is not caller and control is not worker
        assert control_scope is scope and retained_before_native_await
        assert caller not in scope.handles and control in scope.handles
        exchange = next(
            item
            for item in value.agent.journal._submit_exchanges
            if item.task is gate.exchange_task
        )
        assert exchange.actual_owner is scope and exchange in scope.handles
        assert exchange.close_task is gate.close_task and not gate.close_task.done()
        commands = value.agent.env.store.read(
            session_id=rec.task.session_id, kinds=["agent.command"], limit=200
        )
        assert (
            len(
                [
                    entry
                    for entry in commands
                    if entry.payload.get("op") == "interrupt"
                    and entry.payload.get("task_id") == value.agent.task_id
                    and entry.payload.get("reason") == reason
                ]
            )
            == 1
        )
        await joined(worker)
        assert rec.model_lease.settled and provider.closed and provider.close_calls == 1
        assert not control.done() and not returned.is_set() and not scope.finished
        assert value.transport.paths(FINALIZE) == [] and value.receipts == []
        gate.release.set()
        await reached(returned)
        assert not caller.done() and caller not in scope.handles
        assert answers[0].task_id == value.agent.task_id
        receipt = await automatic_report(value)
        assert receipt.finalization.context.closure.initial == accepted.proof
        assert scope.finished and not scope.failed and caller not in scope.handles
        assert control.done() and control.exception() is None and not caller.done()
        assert exchange.task.done() and exchange.task.exception() is None
        assert gate.close_task.done() and gate.close_task.exception() is None
        assert not value.agent.journal._submit_cleanup_failed
        assert value.legacy.calls == []
        assert rec.task.state == TaskState.CANCELLED
        assert rec.done.is_set() and rec.task.evidence == []
        assert not (value.agent.workspace / "result.txt").exists()
        assert provider.generations == [] and provider.calls == []
        assert all(body.closes == 1 and body.closed for body in boundary.bodies)
        record_property(
            "external_control_actual_retirement",
            json.dumps(
                {
                    "separate_control_retained_before_native_append": retained_before_native_await,
                    "caller_parked_during_full_native_finalization": not caller.done(),
                    "original_control_work_and_sole_close_joined": exchange.task.done()
                    and gate.close_task.done(),
                    "actual_inference_calls": 0,
                },
                sort_keys=True,
            ),
        )
    finally:
        gate.release.set()
        park.set()
        await joined(asyncio.gather(caller, return_exceptions=True))


async def window_fixture(factory, populated):
    env = await factory()
    session = env.store.create_session(SessionRequest(channel="converse"))
    window = decision = None
    if populated:
        source = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="external-control-source",
            window_index=3,
            text="Public synthetic retained speech section.",
            start_ms=0,
            end_ms=1000,
            user_speaking=False,
            capture_ended=True,
            content_scope="public_synthetic",
        )
        window = env.store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window",
                payload=source.model_dump(mode="json"),
                idempotency_key="external-control-window",
            ),
            "converse",
        )
        route = WindowDecision(
            source_event_id=window.event_id,
            source_sequence=window.sequence,
            source_revision=source.revision,
            conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
            agent_route="INTERRUPT_CURRENT_TASK",
            target_task_id="synthetic-observed-target",
            expected_objective_revision=1,
        )
        decision = env.store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="routing.window_decided",
                payload=route.model_dump(mode="json"),
                causes=[window.event_id],
                idempotency_key="external-control-decision",
            ),
            "decisions",
        )
    transport = Transport(env.app)
    return SimpleNamespace(
        env=env,
        session=session,
        window=window,
        decision=decision,
        transport=transport,
        client=JournalClient("http://standing.test", "a" * 32, transport=transport),
    )


@pytest.mark.parametrize("method", ("latest_window", "window_control"))
@pytest.mark.parametrize("populated", (False, True), ids=("native-null", "native-record"))
async def test_external_window_metadata_keeps_original_exchange_after_cancel(
    factory, method, populated
):
    value = await window_fixture(factory, populated)
    expected = value.window if method == "latest_window" else value.decision

    def invoke():
        if method == "latest_window":
            return value.client.latest_window(
                value.session.session_id,
                value.session.epoch,
                "external-control-source",
                window_index=3,
            )
        return value.client.window_control(
            value.session.session_id, value.session.epoch, agent_interrupt=True
        )

    scope, root = ActualOperation(lambda _: None), object()
    scope.hold(root)
    path = "/v1/transcript/windows/" + ("latest" if method == "latest_window" else "control")
    gate = Gate(path, "close")
    public = None
    try:
        assert await invoke() == expected
        value.transport.gate = gate
        with actual_owner(scope):
            public = asyncio.create_task(invoke())
        await reached(gate.entered)
        exchange = next(
            item for item in value.client._submit_exchanges if item.task is gate.exchange_task
        )
        assert exchange.actual_owner is scope and exchange in scope.handles
        closer, body = gate.close_task, value.transport.bodies[-1]
        assert exchange.close_task is closer and not closer.done() and body.closes == 1
        public.cancel()
        with pytest.raises(asyncio.CancelledError):
            await public
        assert exchange in value.client._submit_exchanges and exchange in scope.handles
        assert not exchange.task.done() and not closer.done() and not scope.finished
        assert exchange.close_task is closer and body.closes == 1 and not body.closed
        assert not value.client.http.is_closed and value.transport.close_calls == 0
        gate.release.set()
        result = await joined(exchange.task)
        assert (None if result is None else JournalEntry.model_validate(result)) == expected
        scope.release(root)
        await joined(scope.join())
        assert scope.finished and not scope.failed
        assert closer.done() and closer.exception() is None and body.closed and body.closes == 1
        assert not body.cancelled and not body.failed
        assert value.client._submit_exchanges == set() and not value.client._submit_cleanup_failed
        with actual_owner(ActualOperation(lambda _: None)):
            assert await invoke() == expected
        assert len(value.transport.paths(path)) == 3
    finally:
        gate.release.set()
        if public is not None:
            await joined(asyncio.gather(public, return_exceptions=True))
        for exchange in tuple(value.client._submit_exchanges):
            await joined(asyncio.gather(exchange.task, return_exceptions=True))
        if root in scope.handles:
            scope.release(root)
        await joined(scope.join())
        await value.client.close()
        assert value.transport.close_calls == 1 and value.transport.closed.is_set()
        assert all(body.closes == 1 and body.closed for body in value.transport.bodies)
