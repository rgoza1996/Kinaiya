"""Durable pending inputs remain bounded and fair across native sessions."""

from types import SimpleNamespace

import httpx

from packages.contracts.live_decisions_attempts import LiveDecisionsSource
from packages.contracts.models import SessionRequest
from packages.providers.text import CompatibleTextProvider
from services.decisions.live_worker import LiveDecisionsWorker
from services.decisions.model_settings import DecisionsModelSettings
from services.session.live_windows import publish
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import env as env
from tests.integration.test_live_capture_permission import registration
from tests.integration.test_live_completion_worker import Agent
from tests.integration.test_live_decisions_attempts import live as live
from tests.integration.test_live_decisions_pending import query, utterance
from tests.integration.test_live_window_publication import original


async def test_large_session_backlog_cannot_exclude_another_session_from_pending_turn(live):
    for number in range(40):
        utterance(live, number)
    other = SimpleNamespace(
        store=live.store, session=live.store.create_session(SessionRequest(channel="converse"))
    )
    capture = registration(other).model_copy(update={"capture_id": "c" * 32})
    permission = live.store.live_captures.register(capture)
    pair = publish(live.store, original(other, capture_id=capture.capture_id))
    expected = LiveDecisionsSource.of(pair, permission.receipt)
    assert live.owner.pending(query(after=pair.source.sequence - 1)).sources == [expected]
    provider = CompatibleTextProvider(
        "http://127.0.0.1:8897/v1",
        "public-model",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    settings = DecisionsModelSettings(provider, None)
    worker = LiveDecisionsWorker(LocalJournal(live.store, "decisions"), Agent(), settings)
    # The previous actual turn belonged to the session with the large backlog.
    worker._last_session = (live.session.session_id, live.session.thread_id, live.session.epoch)
    try:
        for _ in range(3):
            await worker.scan()
            assert len(worker.pending) <= 32
        assert expected in worker.pending.values()
        assert worker._take_pending() == expected
        # RAM admission/eviction is not a native completion or transcript ACK.
        assert live.owner.pending(query()).sources
        assert live.store.db.execute("SELECT count(*) FROM decisions_attempts").fetchone()[0] == 0
        assert not settings.leases
    finally:
        await worker.close()
        await settings.close()
