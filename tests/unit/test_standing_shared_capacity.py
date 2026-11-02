"""Reverse shared-capacity regression: ordinary submit cannot become owner65.

Frozen independently after the original73 cases missed this allocator direction.
All64 occupied exchanges have actually entered cancellation-resistant transport.
"""

import asyncio

import httpx
import pytest
from test_agent_submit_client import proposal, query_dict, response, safe_error
from test_standing_agent_client import (
    BACKEND,
    INSTANCE,
    TOKEN,
    Body,
    call,
    join_exchanges,
    packets,
    resistant_gate,
    wire,
)

from packages.contracts.agent_submit import (
    AgentSubmitLookupRequest,
    AgentSubmitUnavailable,
)
from packages.contracts.client import JournalClient


@pytest.mark.parametrize("operation", ("admit", "lookup"))
async def test_ordinary_submit_refuses_65th_actual_owner_after_cancelled_mixed_calls(operation):
    data = packets()
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests, streams, public_tasks = [], [], []
    old_path = "/v1/transcript/agent-submits" + ("/lookup" if operation == "lookup" else "")
    old_request = AgentSubmitLookupRequest(**query_dict()) if operation == "lookup" else proposal()
    control = {
        "schema_version": "1.0",
        "backend_id": BACKEND,
        "instance_id": INSTANCE,
        "revision": 0,
        "mode": "on_demand",
        "active_work_policy": "finish_already_accepted",
        "control_writable": True,
        "available_modes": ["off", "on_demand"],
        "standing_work_available": False,
    }

    async def handle(request):
        requests.append(request)
        if request.url.path == old_path:
            # If the defect sends an ordinary65th request, return a valid bound
            # old receipt immediately. A synthetic timeout must not mask it.
            raw = wire(response(operation))
        else:
            assert request.url.path in {
                "/v1/agent-control/state",
                "/v1/standing/executions/prepare",
            }
            if len(requests) == 64:
                entered.set()
            await resistant_gate(release, cancelled)
            raw = wire(
                control
                if request.url.path == "/v1/agent-control/state"
                else data.results["standing_prepare"].model_dump(mode="json")
            )
        stream = Body(raw)
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"Content-Type": "application/json"})

    client = JournalClient("http://synthetic.invalid", TOKEN, transport=httpx.MockTransport(handle))
    try:
        for _ in range(32):
            public_tasks.append(asyncio.create_task(client.agent_control_state()))
            public_tasks.append(asyncio.create_task(call(client, "standing_prepare", data)))
        await asyncio.wait_for(entered.wait(), 3)
        assert len(requests) == 64
        owners = set(client._submit_exchanges)
        assert len(owners) == 64 and all(not owner.task.done() for owner in owners)
        for task in public_tasks:
            task.cancel()
        results = await asyncio.gather(*public_tasks, return_exceptions=True)
        assert all(isinstance(value, asyncio.CancelledError) for value in results)
        assert client._submit_exchanges == owners

        ordinary = getattr(client, operation + "_submit")
        with pytest.raises(AgentSubmitUnavailable) as caught:
            await ordinary(old_request)
        safe_error(caught.value)
        assert caught.value.__cause__ is None and len(str(caught.value)) <= 256
        assert len(requests) == 64, "Ordinary submit crossed the same actual-owner capacity"
        assert client._submit_exchanges == owners
        assert all(not owner.task.done() for owner in owners)

        release.set()
        await asyncio.wait_for(join_exchanges(client), 2)
        assert not client._submit_exchanges
        assert len(streams) == 64
        assert all(
            stream.close_calls == 1 and stream.close_completed.is_set() for stream in streams
        )

        result = await ordinary(old_request)
        assert result.entry is not None
        assert len(requests) == 65 and requests[-1].url.path == old_path
        assert streams[-1].close_calls == 1 and streams[-1].close_completed.is_set()
        assert not client._submit_exchanges
    finally:
        release.set()
        for task in public_tasks:
            task.cancel()
        await asyncio.gather(*public_tasks, return_exceptions=True)
        await client.close()
        await asyncio.wait_for(join_exchanges(client), 2)
