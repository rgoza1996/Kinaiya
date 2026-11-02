"""Source-aware late reconciliation-close controls, additive to frozen7322.

The read-only 01026 source audit exposed _joined_call's exceptional reconcile
exit and _settle_native's early metadata-error handling, plus workspace counting
that omitted final_scope.failed. These are review-derived regressions; the
future correction was not consulted. Native settlement/report production is real.
"""

import asyncio
import json

import httpx
import pytest
from test_agent_standing_settlement import (
    SETTLE,
    SETTLEMENT_LOOKUP,
    Transport,
    joined,
    launch,
    lose_completed_ack,
    native_settlements,
    original_scope,
    reached,
    reports,
)
from test_agent_standing_settlement import claim_chain as claim_chain
from test_agent_standing_settlement import factory as factory
from test_agent_standing_settlement import physical as physical
from test_agent_standing_settlement import settlement_chain as settlement_chain
from test_agent_standing_settlement import stack as stack

from packages.contracts.client import JournalClient
from packages.contracts.standing_agent_settlement import (
    StandingAgentSettlementQuery,
    StandingAgentSettlementReceipt,
)
from packages.contracts.standing_work import StandingUnavailable


class LateLookupClose(httpx.AsyncByteStream):
    """One real response stream; keep its original client-created close Task.

    The facade's first abandonment cancel is deliberately resisted. Only after
    that original facade has refused does the owned test fault release/fail or
    cancel this same close. No replacement Task, waiter, response or close owner
    is installed, and actual inner aclose is joined before its fault result.
    """

    def __init__(self, inner):
        self.inner = inner
        self.entered, self.release, self.abandon_observed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.response = self.close_task = None
        self.closes = self.inner_closes = 0
        self.allow_cancel = False
        self.inner_closed = False
        self.closed = self.failed = self.cancelled = False

    async def __aiter__(self):
        async for chunk in self.inner:
            yield chunk

    async def close_inner(self):
        self.inner_closes += 1
        await self.inner.aclose()
        self.inner_closed = self.closed = True

    async def aclose(self):
        self.closes += 1
        self.close_task = asyncio.current_task()
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if self.allow_cancel:
                    await self.close_inner()
                    self.cancelled = True
                    raise
                self.abandon_observed.set()
        await self.close_inner()
        self.failed = True
        raise OSError("Synthetic late original reconciliation close failure")


@pytest.mark.parametrize("ending", ["failed", "cancelled"])
async def test_reconciliation_facade_refusal_retains_original_close_and_sticky_failure_after_replacement(
    settlement_chain, monkeypatch, record_property, ending
):
    value = await settlement_chain()
    original_journal = value.agent.journal
    completed = lose_completed_ack(value, monkeypatch, SETTLE)
    boundary = value.journal_transport
    delegate = boundary.delegate
    send = boundary.handle_async_request
    bodies = []
    refused = asyncio.Event()
    failures = []
    lookup = original_journal.standing_settlement_lookup

    async def observed_lookup(query):
        try:
            return await lookup(query)
        except StandingUnavailable as error:
            failures.append(type(error))
            refused.set()
            raise

    async def held_native_lookup(request):
        if request.url.path != SETTLEMENT_LOOKUP:
            return await send(request)
        # Retain the original acquired ASGI Response. Only this path uses the
        # late-fault stream directly: the old Body fixture retries inner close
        # on cancellation, which would replace this test's single-close seam.
        boundary.requests.append(request)
        boundary.work.append(asyncio.current_task())
        response = await delegate.handle_async_request(request)
        assert response.status_code == 200
        body = LateLookupClose(response.stream)
        body.response = response
        response.stream = body
        boundary.bodies.append(body)
        boundary.replies.append(response)
        bodies.append(body)
        return response

    monkeypatch.setattr(original_journal, "standing_settlement_lookup", observed_lookup)
    monkeypatch.setattr(boundary, "handle_async_request", held_native_lookup)
    assert (await launch(value)).disposition == "created"
    await joined(value.agent.record.worker)
    # The exact lookup request is emitted only after the real completed settle
    # ACK was lost. This readiness observation creates no production owner.
    async with asyncio.timeout(30):
        while not bodies:
            await asyncio.sleep(0)
    assert len(bodies) == 1
    body = bodies[0]
    await reached(body.entered)
    (exchange,) = [
        item for item in original_journal._submit_exchanges if item.response is body.response
    ]
    _, work_scope = original_scope(value)
    owner = work_scope.standing_claim
    assert exchange.actual_owner is owner.final_scope and exchange in owner.final_scope.handles
    assert exchange.close_task is body.close_task and not body.close_task.done()
    assert owner.scope.finished and not owner.scope.failed
    assert value.agent.record.model_lease.settled and value.agent.record.worker.done()
    assert len(completed) == len(reports(value.resources)) == len(native_settlements(value)) == 1
    native_ack = StandingAgentSettlementReceipt.from_json(completed[0])
    request = value.settlement_calls[0][0]
    assert native_ack.validate_request(request).settlement.request == request
    assert value.settlements == [] and owner.settlement is None
    assert not owner.operation.done() and not owner.final_scope.finished

    other_transport = Transport(value.agent.env.app)
    other = JournalClient("http://standing.test", "a" * 32, transport=other_transport)
    value.agent.runtime.journal = other
    closes = []
    try:
        # Observe the actual unchanged 25-second client deadline. There is no
        # shortened timer, clock manipulation, wrapper timeout or extra lookup.
        await asyncio.wait_for(refused.wait(), 30)
        await reached(body.abandon_observed)
        assert failures == [StandingUnavailable]
        assert body.close_task is exchange.close_task and not body.close_task.done()
        assert exchange in original_journal._submit_exchanges
        assert exchange in owner.final_scope.handles and not owner.final_scope.finished
        assert value.settlements == [] and owner.settlement is None
        assert not original_journal.http.is_closed and boundary.close_calls == 0
        assert value.agent.runtime.workspace_operation_state()["cleanup_pending"] > 0
        assert other_transport.requests == []

        closer = asyncio.create_task(value.agent.runtime.close())
        value.public.append(closer)
        closes.append(closer)
        await joined(asyncio.gather(closer, return_exceptions=True), 8)
        assert not owner.operation.done() and not original_journal.http.is_closed
        assert body.closes == 1 and body.inner_closes == 0
        assert boundary.close_calls == 0 and value.settlements == []

        if ending == "cancelled":
            body.allow_cancel = True
            body.close_task.cancel()
        else:
            body.release.set()
        await joined(asyncio.gather(exchange.task, body.close_task, return_exceptions=True))
        await joined(asyncio.gather(owner.operation, return_exceptions=True))
        assert body.closes == body.inner_closes == 1 and body.inner_closed
        assert (
            body.close_task.cancelled()
            if ending == "cancelled"
            else isinstance(body.close_task.exception(), OSError)
        )
        assert owner.final_scope.finished and owner.final_scope.failed
        assert original_journal._submit_cleanup_failed and owner.failed
        assert value.agent.runtime.workspace_operation_state()["cleanup_failed"] > 0
        assert value.settlements == [] and owner.settlement is None
        assert len(boundary.paths(SETTLE)) == len(boundary.paths(SETTLEMENT_LOOKUP)) == 1
        assert other_transport.paths(SETTLE) == other_transport.paths(SETTLEMENT_LOOKUP) == []

        await joined(asyncio.gather(value.agent.runtime.close(), return_exceptions=True))
        shutdown = value.agent.runtime._standing_shutdown
        assert shutdown is not None
        await joined(asyncio.gather(shutdown, return_exceptions=True))
        value.agent.runtime_closed = True
        assert original_journal.http.is_closed and boundary.closed.is_set()
        assert boundary.close_calls == 1
        assert not other.http.is_closed and other_transport.close_calls == 0
        assert value.agent.runtime.workspace_operation_state()["cleanup_failed"] > 0
        # Genuine committed native metadata is retained knowledge only. It does
        # not turn the failed original close/Agent finalizer into healthy proof.
        query = StandingAgentSettlementQuery.from_json(value.settlement_reads[0])
        retained = await value.resources.agent.standing_settlement_lookup(query)
        assert retained is not None and retained.settlement == native_ack.settlement
        assert owner.failed and owner.final_scope.failed and owner.settlement is None
        record_property(
            "late_reconciliation_original_cleanup",
            json.dumps(
                {
                    "ending": ending,
                    "original_facade_refused": refused.is_set(),
                    "original_close_once_and_inner_joined": body.closes == body.inner_closes == 1
                    and body.inner_closed,
                    "original_scope_failed": owner.final_scope.failed,
                    "sticky_cleanup_failed": value.agent.runtime.workspace_operation_state()[
                        "cleanup_failed"
                    ]
                    > 0,
                    "original_transport_closed_once_replacement_untouched": boundary.close_calls
                    == 1
                    and not other.http.is_closed
                    and other_transport.close_calls == 0,
                    "committed_metadata_is_retained_not_original_retirement_proof": owner.settlement
                    is None,
                    "actual_inference_calls": 0,
                },
                sort_keys=True,
            ),
        )
    finally:
        # Unwind only the test-owned fault gate; the actual existing close Task
        # and product retirement owner are still the ones that must be joined.
        body.release.set()
        if closes:
            await joined(asyncio.gather(*closes, return_exceptions=True))
        await value.retire()
        await joined(other.close())
