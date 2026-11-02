"""Source-aware F2 regression: reconcile only after original binding cleanup.

The f237 source was inspected; the repair was not. A cancelled client facade
cannot prove absence while its actual HTTP acquisition and sole-close remain
owned. All positive binding/report values below come from native ASGI/SQLite.
"""

import asyncio
import json

from test_agent_standing_claim_retirement import (
    FINALIZE,
    Gate,
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
from test_standing_agent_runtime import ADMIT, REFUSALS, entries
from test_standing_physical_finalization import reports

from packages.contracts.standing_resource_finalization import StandingResourceBindingReceipt
from packages.contracts.standing_work import StandingUnavailable
from services.agent.resource_completion import StandingClaimOwner

BIND = "/v1/resources/standing/binding"


async def test_missing_binding_reconciliation_joins_original_send_and_sole_close(
    claim_chain, monkeypatch, record_property
):
    value = await claim_chain()
    send_entered, send_release, send_cancelled, send_returned = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    settle_entered = asyncio.Event()
    close = Gate(BIND, "close")
    value.transport.gate = close
    value.gates.append(close)
    delegate = value.transport.delegate
    actual_send = delegate.handle_async_request
    actual_bind, actual_lookup = (
        value.client.bind_standing_claim,
        value.client.lookup_standing_binding,
    )
    actual_settle = StandingClaimOwner._settle_work
    bind_callers, original_sends, lookups = [], [], []
    original = None

    async def send(request):
        if request.url.path == BIND:
            original_sends.append(asyncio.current_task())
            send_entered.set()
            while not send_release.is_set():
                try:
                    await send_release.wait()
                except asyncio.CancelledError:
                    send_cancelled.set()
        response = await actual_send(request)
        if request.url.path == BIND:
            send_returned.set()
        return response

    async def bind(claim):
        bind_callers.append(asyncio.current_task())
        return await actual_bind(claim)

    async def settle(owner):
        if owner.resources is value.client:
            settle_entered.set()
        return await actual_settle(owner)

    async def lookup(claim):
        healthy_original = (
            original is not None
            and send_returned.is_set()
            and original.task.done()
            and original.close_task is not None
            and original.close_task.done()
            and not original.close_task.cancelled()
            and original.close_task.exception() is None
            and original not in value.client._submit_exchanges
        )
        lookups.append(healthy_original)
        assert healthy_original, "Binding lookup preceded its actual original work/sole-close"
        result = await actual_lookup(claim)
        assert type(result) is StandingResourceBindingReceipt
        value.resources.binding = result.validate_claim(value.agent.claim)
        return result

    monkeypatch.setattr(delegate, "handle_async_request", send)
    monkeypatch.setattr(value.client, "bind_standing_claim", bind)
    monkeypatch.setattr(value.client, "lookup_standing_binding", lookup)
    monkeypatch.setattr(StandingClaimOwner, "_settle_work", settle)
    public = launch(value)
    try:
        await reached(send_entered)
        assert len(bind_callers) == len(original_sends) == 1
        (facade,) = bind_callers
        (send_owner,) = original_sends
        original = next(
            exchange for exchange in value.client._submit_exchanges if exchange.task is send_owner
        )
        _, scope = original_scope(value)
        assert facade is not public and facade is not send_owner
        assert facade in scope.handles and original.actual_owner is scope
        assert original in scope.handles and original.response is None
        assert original.close_task is None and not send_returned.is_set()
        assert value.resources.binding is None and value.agent.made == []
        assert value.agent.transport.paths(ADMIT) == [] and entries(value.agent) == []

        facade.cancel()
        await reached(send_cancelled)
        await reached(settle_entered)
        assert facade.cancelled() and original.abandoned
        assert lookups == []
        assert not original.task.done() and original in scope.handles and not scope.finished
        assert value.transport.paths(BIND + "/lookup") == []
        assert value.transport.paths(FINALIZE) == [] and reports(value.resources) == []
        assert value.agent.made == [] and value.agent.runtime._tasks == {}

        send_release.set()
        await reached(close.entered)
        assert send_returned.is_set() and original.response is not None
        assert original.close_task is close.close_task and not close.close_task.done()
        assert lookups == [] and original in value.client._submit_exchanges
        assert not original.task.done() and not scope.finished
        assert value.transport.paths(FINALIZE) == [] and reports(value.resources) == []
        close.release.set()
        await joined(asyncio.gather(original.task, return_exceptions=True))
        receipt = await automatic_report(value)
        assert lookups == [True]
        assert receipt.finalization.context.closure.initial is None
        assert scope.finished and not scope.failed
        assert not original.task.cancelled()
        assert isinstance(original.task.exception(), StandingUnavailable)
        assert close.close_task.done() and close.close_task.exception() is None
        assert original not in value.client._submit_exchanges
        assert not value.client._submit_cleanup_failed
        assert len(value.transport.paths(BIND)) == len(value.transport.paths(BIND + "/lookup")) == 1
        assert value.agent.transport.paths(ADMIT) == [] and entries(value.agent) == []
        assert value.agent.made == [] and value.agent.runtime._tasks == {}
        outcome = await joined(asyncio.gather(public, return_exceptions=True))
        assert isinstance(outcome[0], (*REFUSALS, asyncio.CancelledError))
        assert all(
            body.closes == 1 and body.closed and not body.cancelled and not body.failed
            for body in value.transport.bodies
        )
        record_property(
            "binding_reconciliation_actual_owners",
            json.dumps(
                {
                    "original_send_cancellation_observed_before_native_delegate": send_cancelled.is_set(),
                    "lookup_after_original_work_and_sole_close": lookups == [True],
                    "automatic_native_report": True,
                    "model_allocations": len(value.agent.made),
                    "actual_inference_calls": 0,
                },
                sort_keys=True,
            ),
        )
    finally:
        send_release.set()
        close.release.set()
        await joined(asyncio.gather(public, *bind_callers, return_exceptions=True))
        if original is not None:
            await joined(asyncio.gather(original.task, return_exceptions=True))
