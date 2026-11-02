"""Contract-first controls for the four original Resources return budgets.

All transport, parsing and complete receipt joins are real. Only the loop clock
advances, after the final actual join/availability; it is restored before the
shared fixture retires its original transport. No wall-clock latency is claimed.
"""

import asyncio

import pytest
from test_standing_resource_barrier_client import OPS, proposal, scenario, start, wire

from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceFinalizeContext,
)
from packages.contracts.standing_work import StandingUnavailable

CASES = (
    ("bind_standing_claim", False),
    ("lookup_standing_binding", False),
    ("seal_standing_claim", False),
    ("lookup_standing_seal", False),
    ("lookup_standing_binding", True),
    ("lookup_standing_seal", True),
)


def positive_originals(env, owners):
    exchange, work, close = owners
    assert exchange.task is work and exchange.close_task is close
    assert work.done() and not work.cancelled() and work.exception() is None
    assert close.done() and not close.cancelled() and close.exception() is None
    assert exchange.close_started
    assert exchange.response is env.responses[0]
    assert len(env.responses) == len(env.bodies) == 1
    assert env.responses[0].is_closed
    assert env.bodies[0].closed and env.bodies[0].close_calls == 1
    assert not env.client._submit_exchanges
    assert not env.client._submit_cleanup_failed
    assert not env.client.http.is_closed


@pytest.mark.parametrize("operation,null_reply", CASES)
@pytest.mark.parametrize("late", (False, True), ids=("healthy", "deadline-crossed"))
async def test_original_budget_includes_actual_final_join_or_null_availability(
    operation, null_reply, late, monkeypatch
):
    async with scenario(operation, raw=b"null" if null_reply else None) as env:
        env.hold_close = True
        original = proposal(env.data, operation)
        expected = wire(env.data[OPS[operation][1]])
        loop, offset = asyncio.get_running_loop(), [0.0]
        actual_time = loop.time
        owners, observed = [], []
        result = None

        def after_actual(point):
            positive_originals(env, owners)
            if late:
                offset[0] = 26.0
            observed.append((point, offset[0]))

        # The clock is unchanged through send/read/parse and the original sole
        # close. The real callable completes before the logical deadline jump.
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: actual_time() + offset[0])
            if null_reply:
                actual_available = env.client.require_available

                def available():
                    actual_available()
                    if owners and owners[1].done() and owners[2].done():
                        after_actual("null-availability")

                clock.setattr(env.client, "require_available", available)
            else:
                binding = OPS[operation][1] == "StandingResourceBindingReceipt"
                kind = (
                    StandingResourceBindingReceipt if binding else StandingResourceFinalizeContext
                )
                method = "validate_claim" if binding else "validate_request"
                actual_join = getattr(kind, method)

                def join(receipt, request):
                    joined = actual_join(receipt, request)
                    assert request.canonical_bytes() == original.canonical_bytes()
                    assert joined.canonical_bytes() == expected
                    after_actual("receipt-join")
                    return joined

                clock.setattr(kind, method, join)

            task = start(env, operation, original)
            await asyncio.wait_for(env.send_entered.wait(), 5)
            (body,) = env.bodies
            await asyncio.wait_for(body.close_entered.wait(), 5)
            (exchange,) = env.client._submit_exchanges
            owners.extend((exchange, exchange.task, exchange.close_task))
            assert owners[1] is not task and owners[2] is not task
            assert not owners[1].done() and not owners[2].done()
            assert body.close_calls == 1 and not body.closed
            body.close_release.set()
            try:
                if late:
                    with pytest.raises(StandingUnavailable):
                        await task
                else:
                    result = await task
            finally:
                # Even a failing baseline assertion cannot leave the logical
                # clock advanced during fixture-owned real cleanup.
                offset[0] = 0.0

        point = "null-availability" if null_reply else "receipt-join"
        assert observed and all(item == (point, 26.0 if late else 0.0) for item in observed)
        positive_originals(env, owners)
        (request,) = env.requests
        assert request.method == "POST" and request.url.path == OPS[operation][0]
        assert not request.url.query
        assert request.headers["authorization"] == "Bearer " + "a" * 32
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept-encoding"] == "identity"
        assert "x-kinaiya-standing-capability" not in request.headers
        assert request.content == original.canonical_bytes()
        if not late:
            if null_reply:
                assert result is None
            else:
                assert result.canonical_bytes() == expected

    transport_close = env.client._close_task
    assert transport_close is not None and transport_close.done()
    assert not transport_close.cancelled() and transport_close.exception() is None
    assert env.client.http.is_closed and not env.client._submit_exchanges
    assert not env.client._submit_cleanup_failed
