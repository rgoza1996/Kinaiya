"""Source-aware regressions for the complete Resources finalization budget.

Written after read-only review of sealed 28c3210 and before its repair. The
original 40 independent transport cases remain unchanged. These controls advance
only the logical loop clock after the actual full serializer/readiness/join;
they make no performance or Agent retirement claim.
"""

import asyncio

import pytest
from test_standing_resources_finalization_transport_client import (
    OPS,
    RECEIPT,
    invoke,
    scenario,
)

from packages.contracts.standing_resource_finalization import StandingResourceFinalizationReceipt
from packages.contracts.standing_work import StandingUnavailable
from services.resources import standing_client


@pytest.mark.parametrize("name,path", OPS)
@pytest.mark.parametrize("phase", ("freeze", "readiness", "receipt-join"))
async def test_new_public_operation_keeps_one25second_budget_through_actual_work(
    name, path, phase, monkeypatch
):
    async with scenario() as value:
        loop, offset = asyncio.get_running_loop(), [0.0]
        actual_time = loop.time
        actual_wire = standing_client.finalization_bytes
        actual_ready = value.client._settle_ready
        actual_join = StandingResourceFinalizationReceipt.validate_request
        serialized, ready, joined = [], [], []

        def wire(packet):
            raw = actual_wire(packet)
            serialized.append(raw)
            if len(serialized) == 1 and phase != "receipt-join":
                offset[0] += 26 if phase == "freeze" else 20
            return raw

        async def settle(deadline):
            await actual_ready(deadline)
            ready.append(deadline)
            if phase == "readiness":
                offset[0] += 6

        def join(receipt, original):
            result = actual_join(receipt, original)
            assert result.model_dump(mode="json") == value.data[RECEIPT]
            joined.append(result)
            if phase == "receipt-join":
                offset[0] += 26
            return result

        # Restore the real clock before the shared fixture's actual retained
        # exchange/client retirement. No original operation or owner is faked.
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: actual_time() + offset[0])
            clock.setattr(standing_client, "finalization_bytes", wire)
            clock.setattr(value.client, "_settle_ready", settle)
            clock.setattr(StandingResourceFinalizationReceipt, "validate_request", join)
            with pytest.raises(StandingUnavailable):
                await invoke(value, name)
            assert serialized and offset[0] >= 26
            if phase == "receipt-join":
                assert joined and len(value.requests) == 1
                assert value.requests[0].url.path == path
                assert value.body.close_calls == 1 and value.body.closed.is_set()
                assert (
                    not value.client._submit_exchanges and not value.client._submit_cleanup_failed
                )
            else:
                assert value.requests == [] and value.responses == []
                assert not value.client._submit_exchanges and joined == []
                if phase == "readiness":
                    assert ready
