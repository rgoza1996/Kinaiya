"""Failed physical cleanup and wake outages cannot fund replacement owners."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.live_decisions_attempts import LiveDecisionsAttemptLookup
from packages.providers.text import CompatibleTextProvider, ProviderCleanupError, ProviderError
from services.decisions import live_worker as runtime
from services.decisions.live_worker import LiveDecisionsWorker
from services.decisions.model_settings import DecisionsModelSettings
from services.session.live_windows import publish
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import fence
from tests.integration.test_live_completion_worker import Agent
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_decisions_http import response
from tests.integration.test_live_window_publication import original


async def test_failed_wake_is_not_immediately_recreated_before_its_retry_boundary(
    live, monkeypatch
):
    live.store.live_captures.fence(fence(live.source.capture.original))
    journal = LocalJournal(live.store, "decisions")
    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1",
        "public-model",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    settings = DecisionsModelSettings(provider, None)
    worker = LiveDecisionsWorker(journal, Agent(), settings)
    wakes, scans = [], []
    pending = journal.pending_live_decisions

    async def failed_wake(*args, **kwargs):
        wakes.append(True)
        raise RuntimeError("Public synthetic journal wake outage")

    async def observed_scan(request):
        scans.append(True)
        if len(scans) == 5:
            worker._stop.set()
        return await pending(request)

    # Freeze only the worker's retry clock, leaving asyncio's actual clock intact.
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(journal, "wait_for_journal_change", failed_wake)
    monkeypatch.setattr(journal, "pending_live_decisions", observed_scan)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(task, 8)
        assert len(scans) == 5 and len(wakes) == 1
        assert worker.slot is None and not settings.leases
        assert live.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 0
    finally:
        await worker.close()
        await asyncio.gather(task, return_exceptions=True)
        await settings.close()


async def test_failed_physical_close_retains_slot_and_lease_against_fresh_input(live):
    closing = asyncio.Event()
    calls = []

    class BrokenClose(httpx.AsyncByteStream):
        closes = 0

        async def __aiter__(self):
            yield response()

        async def aclose(self):
            self.closes += 1
            closing.set()
            raise RuntimeError("Public synthetic unconfirmed original physical close")

    stream = BrokenClose()

    def model(request):
        calls.append(request.content)
        return httpx.Response(200, stream=stream)

    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1", "public-model", transport=httpx.MockTransport(model)
    )
    settings = DecisionsModelSettings(provider, None)
    journal = LocalJournal(live.store, "decisions")
    worker = LiveDecisionsWorker(journal, Agent(), settings)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(closing.wait(), 5)
        slot = worker.slot
        result = await asyncio.gather(slot.task, return_exceptions=True)
        assert isinstance(result[0], ProviderCleanupError)
        worker._harvest()
        assert worker.slot is slot and slot.observed and slot.lease in settings.leases
        assert not worker.ready and worker.last_error == "original_live_cleanup_unconfirmed"
        latest = publish(
            live.store, original(live, 1, text="Public fresh input after failed cleanup")
        )
        await worker.scan()
        await asyncio.sleep(0)
        assert any(s.source_event_id == latest.source.event_id for s in worker.pending.values())
        assert len(calls) == stream.closes == 1 and worker.slot is slot
        state = await journal.live_decisions_attempt_lookup(
            LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
        )
        assert state.state == "uncertain" and state.charged_attempts == 1
        assert live.store.read(kinds=["routing.live_window_decided"]) == []
        assert not await worker.close()
        await asyncio.wait_for(task, 5)
        assert slot.lease in settings.leases and settings.busy()
        assert worker.slot is slot and stream.closes == 1
    finally:
        await worker.close()
        await asyncio.gather(task, return_exceptions=True)
        await settings.close()
        await provider.close()


async def test_settled_transport_failure_uses_only_the_original_native_allowance(live):
    calls, closes = [], []
    active = 0

    class ErrorResponse(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":"PUBLIC SYNTHETIC TRANSIENT TRANSPORT"}'

        async def aclose(self):
            nonlocal active
            closes.append(True)
            active -= 1

    def model(request):
        nonlocal active
        active += 1
        assert active == 1
        calls.append(request.content)
        return httpx.Response(503, stream=ErrorResponse())

    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1", "public-model", transport=httpx.MockTransport(model)
    )
    settings = DecisionsModelSettings(provider, None)
    journal = LocalJournal(live.store, "decisions")
    worker = LiveDecisionsWorker(journal, Agent(), settings)
    try:
        slot = SimpleNamespace(source=live.source, stamp=settings.cache_stamp(), lease=None)
        with pytest.raises(ProviderError):
            await worker._job(slot)
        state = await journal.live_decisions_attempt_lookup(
            LiveDecisionsAttemptLookup(contract_version=2, source=live.source)
        )
        assert state.state == "exhausted" and state.charged_attempts == state.max_attempts == 2
        assert len(calls) == len(closes) == 2 and active == 0
        assert calls[0] == calls[1]
        assert live.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 2
        assert live.store.read(kinds=["routing.live_window_decided"]) == []
        assert not settings.leases
    finally:
        await settings.close()
