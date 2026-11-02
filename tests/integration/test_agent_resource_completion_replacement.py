"""Review-derived dependent-client regressions, frozen before their repair.

The original 22 oracles/fixtures remain byte exact. A fallback fixture closer
is recorded separately and runs only after the actual callback has ended.
"""

import asyncio
import json

import pytest
from test_agent_post_worker_resource_completion import (
    ObservableResourceClient,
    Observation,
    Transport,
    joined,
    pending,
    reached,
    terminal_at_close,
)
from test_agent_post_worker_resource_completion import cases as cases
from test_agent_post_worker_resource_completion import factory as factory
from test_agent_post_worker_resource_completion import ordinary as ordinary
from test_agent_post_worker_resource_completion import stack as stack


@pytest.mark.parametrize("replacement", ["client-B", "none"])
async def test_entered_original_callback_keeps_its_exact_client_close_owner_after_replacement(
    cases, record_property, replacement
):
    value = await cases(phase="response_close")
    original_client, transport = value.client, value.transport
    truth = await terminal_at_close(value)
    value.close_gate.set()
    await reached(transport.response_close_entered)
    callback = value.observation.owner
    assert value.worker.done() and callback is not value.worker and not callback.done()
    assert value.cleanup.done() and value.lease.settled and not value.lease.failed
    assert transport.bodies[0].close_owner is callback
    other, other_transport = None, None
    if replacement == "client-B":
        other_transport = Transport(Observation(value), "acquire")
        other_transport.close_gate.set()
        other = ObservableResourceClient(
            "http://127.0.0.1:8763", "a" * 32, transport=other_transport
        )
    value.runtime.resource_client = other
    product_closer, fallback_original, fallback_other = None, False, False
    try:
        for _ in range(2):
            closer = asyncio.create_task(value.runtime.close())
            value.callers.append(closer)
            await joined(closer)
            assert not callback.done() and pending(value.runtime) > 0
            assert not original_client.http.is_closed and transport.close_calls == 0
        transport.response_close_gate.set()
        await reached(value.observation.ended)
        await joined(asyncio.gather(callback, return_exceptions=True))
        # The configured client has changed, but the entered original operation
        # must still own A's dependent transport retirement after it actually ends.
        await reached(transport.close_entered)
        product_closer = transport.close_owner
        assert product_closer is not callback and not product_closer.done()
        assert transport.close_calls == 1 and original_client.http.is_closed
        entered = asyncio.Event()

        async def close_waiter():
            entered.set()
            await value.runtime.close()

        waiter = asyncio.create_task(close_waiter())
        value.callers.append(waiter)
        await reached(entered)
        waiter.cancel()
        await joined(asyncio.gather(waiter, return_exceptions=True))
        assert not product_closer.done() and transport.close_calls == 1
        transport.close_gate.set()
        await joined(product_closer)
        await joined(value.runtime.close())
        assert transport.closed.is_set() and transport.close_calls == 1
        assert len(value.observation.calls) == len(transport.requests) == 1
        assert transport.bodies[0].close_calls == 1
        if other is not None:
            assert other.http.is_closed and other_transport.close_calls == 1
            assert other_transport.requests == []
        assert (await value.runtime.status(value.task_id)).model_dump(mode="json") == truth
    finally:
        for gate in (
            transport.acquire_gate,
            transport.read_gate,
            transport.response_close_gate,
            transport.close_gate,
        ):
            gate.set()
        await joined(asyncio.gather(callback, return_exceptions=True))
        if value.callers:
            await joined(asyncio.gather(*value.callers, return_exceptions=True))
        if transport.close_owner is None:
            fallback_original = True
            actual_cleanup = asyncio.create_task(original_client.close())
            await joined(actual_cleanup)
        else:
            await joined(transport.close_owner)
        if other is not None:
            if other_transport.close_owner is None:
                fallback_other = True
                await joined(other.close())
            else:
                await joined(other_transport.close_owner)
        assert callback.done() and transport.closed.is_set()
        assert transport.close_owner.done() and transport.close_calls == 1
        assert all(body.close_calls == 1 for body in transport.bodies)
        record_property(
            "replacement_actual_cleanup",
            json.dumps(
                {
                    "original_callback_done": callback.done(),
                    "original_worker_done": value.worker.done(),
                    "original_lease_cleanup_done": value.cleanup.done(),
                    "product_original_closer_observed": product_closer is not None,
                    "fixture_original_close_fallback": fallback_original,
                    "fixture_other_close_fallback": fallback_other,
                    "original_actual_close_owner_done": transport.close_owner.done(),
                    "original_close_calls": transport.close_calls,
                    "other_closed": other is None or other.http.is_closed,
                }
            ),
        )
