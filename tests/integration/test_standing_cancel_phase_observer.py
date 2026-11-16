"""#648: observe the intended final phase through legal per-call latency."""

import asyncio
from types import SimpleNamespace

import pytest
from test_standing_agent_runtime import MeasuredFiles
from test_standing_agent_runtime import factory as factory
from test_standing_agent_runtime import stack as stack
from test_standing_bounded_outcomes import execute, wait_for_cancel_phase


async def test_legal_serial_provider_delay_reaches_original_cancel_phase(stack, monkeypatch):
    initialize = MeasuredFiles.__init__

    def legal_delay(self):
        initialize(self)
        # Forty ordinary measured requests consume over sixteen seconds. Each
        # remains below its native per-operation deadline; no model rerun or
        # alternative action is used to reach the intended cancellation phase.
        self.delay = 0.4

    monkeypatch.setattr(MeasuredFiles, "__init__", legal_delay)
    value, _, final, attempts = await execute(stack, monkeypatch, "cancelled", "confirmed")
    assert final.state.value == "cancelled"
    assert len(attempts) == 1 and len(final.evidence) == 40
    assert value.made[0].final_entered.is_set()


@pytest.mark.parametrize("terminal", [False, True])
async def test_phase_observer_never_cancels_or_replaces_its_original(terminal):
    release = asyncio.Event()
    worker = asyncio.create_task(release.wait())
    provider = SimpleNamespace(
        final_entered=asyncio.Event(), model_progress=asyncio.Event(), model_entered=0
    )
    value = SimpleNamespace(made=[provider], record=SimpleNamespace(worker=worker))
    if terminal:
        release.set()
        await worker
    try:
        with pytest.raises(AssertionError if terminal else TimeoutError):
            await wait_for_cancel_phase(value, 40, stall_timeout=0.02)
        assert value.record.worker is worker and not worker.cancelled()
        assert worker.done() is terminal
    finally:
        release.set()
        await worker
