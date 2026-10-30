"""Durable native scheduling with public synthetic text and classifier fixtures.

No real inference, audio, model-quality evaluation or user conversations.
"""

import asyncio
from collections import Counter
from datetime import datetime, timedelta

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import (
    AppendRequest,
    SessionRequest,
    SpeechWindow,
    Task,
    WindowDecision,
)
from packages.providers.text import ProviderError
from services.common.auth import Auth
from services.decisions.window_routing import WindowAgentDispatcher
from services.decisions.window_worker import JournalUnavailable, WindowDecisionsWorker
from services.session.api import create_app
from services.session.store import JournalError, LocalJournal, Store


class Agent:
    def __init__(self):
        self.tasks = []

    async def status(self):
        return self.tasks

    async def submit(self, request, *, actor):
        assert actor == "decisions"
        task = Task(
            session_id=request.session_id,
            thread_id=request.thread_id,
            epoch=request.epoch,
            objective=request.objective,
            idempotency_key=request.idempotency_key,
            source_event_ids=request.source_event_ids,
        )
        self.tasks.append(task)
        return task


class Classifier:
    def __init__(self, *, queue=False):
        self.calls = []
        self.queue = queue

    async def classify(self, source, *_):
        self.calls.append(source.event_id)
        return {
            "conversation": "WAIT_FOR_NEXT_WINDOW",
            "agent_route": "QUEUE_UNTIL_TASK_DONE"
            if self.queue
            else "IGNORE_FOR_IMMEDIATE_ROUTING",
            "reason_code": "public_synthetic_fixture",
        }


def window(store, session, name, *, index=0, revision=1, text=None):
    data = SpeechWindow(
        session_id=session.session_id,
        thread_id=session.thread_id,
        epoch=session.epoch,
        utterance_id=name,
        window_index=index,
        revision=revision,
        text=text or f"Public synthetic request {name}",
        start_ms=0,
        end_ms=1000 + index,
        user_speaking=False,
        capture_ended=True,
        content_scope="public_synthetic",
    )
    return store.append(
        AppendRequest(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="conversation.window",
            payload=data.model_dump(mode="json"),
            idempotency_key=f"window:{name}:{index}:{revision}",
        ),
        "owner",
    )


async def drain(worker):
    for _ in range(200):
        await worker.tick()
        assert len(worker.pending) <= 32 and len(worker.jobs) <= 4
        if not worker.jobs and not worker.pending:
            return
        await asyncio.wait_for(asyncio.gather(*(t for _, t in worker.jobs.values())), 3)
    pytest.fail("Fresh backlog failed to reach a disposition")


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    yield store
    store.close()


async def test_35_window_burst_and_completed_history_survive_restart(tmp_path):
    path = tmp_path / "journal.sqlite"
    store = Store(path)
    session = store.create_session(SessionRequest(channel="converse"))
    sources = [window(store, session, f"burst-{i}") for i in range(35)]
    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), classifier)
    try:
        await drain(worker)
        assert Counter(classifier.calls) == Counter(s.event_id for s in sources)
        assert all(store.window_decision(s.event_id) is not None for s in sources)
        assert not store.pending_windows()
    finally:
        await worker.close()
        store.close()

    recovered = Store(path)
    after_restart = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(recovered, "decisions"), Agent(), after_restart)
    try:
        await drain(worker)
        assert not after_restart.calls
        latest = window(recovered, session, "after-restart")
        await drain(worker)
        assert after_restart.calls == [latest.event_id]
    finally:
        await worker.close()
        recovered.close()


async def test_restart_recovers_unadmitted_and_uncertain_inflight_windows(tmp_path):
    class Blocked(Classifier):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()

        async def classify(self, source, *_):
            self.calls.append(source.event_id)
            if len(self.calls) == 2:
                self.entered.set()
            await asyncio.Event().wait()

    path = tmp_path / "journal.sqlite"
    store = Store(path)
    session = store.create_session(SessionRequest(channel="converse"))
    sources = [window(store, session, f"restart-{i}") for i in range(35)]
    blocked = Blocked()
    worker = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), blocked)
    await worker.tick()
    await asyncio.wait_for(blocked.entered.wait(), 1)
    assert len(store.pending_windows()) == 35
    assert len(worker.pending) <= 32
    await worker.close()
    store.close()

    recovered = Store(path)
    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(recovered, "decisions"), Agent(), classifier)
    try:
        await drain(worker)
        assert Counter(classifier.calls) == Counter(s.event_id for s in sources)
        assert all(recovered.window_decision(s.event_id) is not None for s in sources)
        assert all(recovered.window_classification_attempts(s) == 2 for s in blocked.calls)
    finally:
        await worker.close()
        recovered.close()


async def test_later_sessions_progress_while_earlier_session_inference_is_blocked(store):
    sessions = [store.create_session(SessionRequest(channel="converse")) for _ in range(3)]
    for i in range(80):
        window(store, sessions[0], f"earlier-{i}")
    later = [window(store, s, f"later-{i}") for i, s in enumerate(sessions[1:])]

    class BlockEarlier(Classifier):
        async def classify(self, source, *_):
            if source.session_id == sessions[0].session_id:
                await asyncio.Event().wait()
            return await super().classify(source)

    classifier = BlockEarlier()
    worker = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), classifier)
    try:
        async with asyncio.timeout(2):
            while not all(store.window_decision(s.event_id) for s in later):
                await worker.tick()
                await asyncio.sleep(0)
        assert Counter(classifier.calls) == Counter(s.event_id for s in later)
    finally:
        await worker.close()


async def test_superseded_windows_get_dispositions_without_classification(store):
    session = store.create_session(SessionRequest(channel="converse"))
    old = window(store, session, "correction", text="Public synthetic obsolete instruction")
    revised = window(
        store, session, "correction", revision=2, text="Public synthetic corrected text"
    )
    latest = window(store, session, "correction", index=1, text="Public synthetic current text")
    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), classifier)
    try:
        await drain(worker)
        assert classifier.calls == [latest.event_id]
        superseded = {
            e.payload["source_event_id"]
            for e in store.read(kinds=["routing.window_status"])
            if e.payload["phase"] == "superseded"
        }
        assert superseded == {old.event_id, revised.event_id}
        assert not store.pending_windows()
    finally:
        await worker.close()


async def test_expired_windows_are_explicit_and_cannot_recover_after_restart(store, monkeypatch):
    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "expired")

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=61)

    classifier = Classifier()
    journal = LocalJournal(store, "decisions")
    worker = WindowDecisionsWorker(journal, Agent(), classifier)
    with monkeypatch.context() as clock:
        clock.setattr("services.decisions.window_worker.datetime", Later)
        await drain(worker)
    await worker.close()
    assert not classifier.calls and store.window_decision(source.event_id) is None
    receipts = store.read(kinds=["routing.window_status"])
    assert receipts[-1].payload["phase"] == "expired"
    recovered = WindowDecisionsWorker(journal, Agent(), classifier)
    try:
        await drain(recovered)
        assert not classifier.calls and not store.pending_windows()
    finally:
        await recovered.close()


async def test_retry_limit_survives_restart_during_backoff(store):
    class Unavailable(Classifier):
        async def classify(self, source, *_):
            self.calls.append(source.event_id)
            raise ProviderError("Laya endpoint is unavailable")

    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "failed")
    classifier = Unavailable()
    journal = LocalJournal(store, "decisions")
    worker = WindowDecisionsWorker(journal, Agent(), classifier)
    await worker.tick()
    async with asyncio.timeout(1):
        while not any(
            e.payload["phase"] == "retrying" for e in store.read(kinds=["routing.window_status"])
        ):
            await asyncio.sleep(0)
    assert store.pending_windows()[0].event_id == source.event_id
    await worker.close()
    restarted = WindowDecisionsWorker(journal, Agent(), classifier)
    try:
        await drain(restarted)
        assert classifier.calls == [source.event_id, source.event_id]
        assert store.window_classification_attempts(source.event_id) == 2
        assert not store.pending_windows()
        assert store.read(kinds=["routing.window_status"])[-1].payload == {
            "source_event_id": source.event_id,
            "source_sequence": source.sequence,
            "source_revision": source.payload["revision"],
            "phase": "error",
            "reason": "provider_unavailable",
        }
    finally:
        await restarted.close()
    final = WindowDecisionsWorker(journal, Agent(), classifier)
    try:
        await drain(final)
        assert len(classifier.calls) == 2
    finally:
        await final.close()


async def test_legacy_retry_receipts_cannot_admit_a_third_uncertain_call(store):
    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "legacy")
    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), classifier)
    # Old code reused the first receipt when attempt two started. A crash
    # leaves no evidence distinguishing one admitted call from two.
    await worker.status(source, "classifying", "content_window")
    await worker.status(source, "retrying", "TimeoutError")
    await worker.status(source, "classifying", "content_window")
    try:
        await drain(worker)
        assert not classifier.calls and not store.pending_windows()
        assert store.window_decision(source.event_id) is None
        assert (
            store.read(kinds=["routing.window_status"])[-1].payload["reason"]
            == "classification_attempts_exhausted"
        )
    finally:
        await worker.close()


async def test_completed_prefix_beyond_one_page_cannot_hide_new_work(store):
    session = store.create_session(SessionRequest(channel="converse"))
    journal = LocalJournal(store, "decisions")
    completed = Classifier()
    worker = WindowDecisionsWorker(journal, Agent(), completed)
    try:
        for i in range(205):
            await worker.classify(window(store, session, f"completed-{i}"))
    finally:
        await worker.close()
    sources = [window(store, session, f"new-{i}") for i in range(3)]
    classifier = Classifier()
    restarted = WindowDecisionsWorker(journal, Agent(), classifier)
    try:
        await drain(restarted)
        assert Counter(classifier.calls) == Counter(s.event_id for s in sources)
        assert len(completed.calls) == 205
    finally:
        await restarted.close()


def test_2000_completed_sources_use_indexed_receipt_lookups():
    store = Store(":memory:")
    session = store.create_session(SessionRequest(channel="converse"))
    for i in range(2000):
        source = window(store, session, f"indexed-{i}")
        if i % 2:
            kind = "routing.window_status"
            payload = {
                "source_event_id": source.event_id,
                "phase": "error",
                "reason": "synthetic_refusal",
            }
        else:
            kind = "routing.window_decided"
            payload = WindowDecision(
                source_event_id=source.event_id,
                source_sequence=source.sequence,
                source_revision=1,
                conversation="IGNORE",
                agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
            ).model_dump(mode="json")
        store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind=kind,
                payload=payload,
                causes=[source.event_id],
                idempotency_key=f"completed-{i}",
            ),
            "decisions",
        )
    queries = []
    progress_calls = 0

    def budget():
        nonlocal progress_calls
        progress_calls += 1
        # Bound this fixed fixture's query work without wall-clock assumptions.
        # Indexed searches have generous headroom below one million VM
        # operations; repeated full receipt scans exhaust that budget.
        return progress_calls > 1000

    store.db.set_trace_callback(queries.append)
    store.db.set_progress_handler(budget, 1000)
    try:
        assert not store.pending_windows(limit=1)
    finally:
        store.db.set_trace_callback(None)
        store.db.set_progress_handler(None, 0)
    query = next(q for q in queries if q.startswith("WITH pending"))
    plan = [row["detail"] for row in store.db.execute("EXPLAIN QUERY PLAN " + query)]
    for index in ("entries_window_decision_source", "entries_window_status_source"):
        assert any(f"SEARCH d USING INDEX {index} (<expr>=?" in step for step in plan), plan
    store.close()


async def test_failed_backlog_read_leaves_fresh_work_recoverable(store):
    class OnceUnavailable(LocalJournal):
        first = True

        async def pending_windows(self, **kwargs):
            if self.first:
                self.first = False
                raise httpx.ConnectError("Synthetic journal transport failure")
            return await super().pending_windows(**kwargs)

    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "journal-retry")
    classifier = Classifier()
    worker = WindowDecisionsWorker(OnceUnavailable(store, "decisions"), Agent(), classifier)
    try:
        with pytest.raises(JournalUnavailable):
            await worker.tick()
        assert store.pending_windows()[0].event_id == source.event_id
        assert not classifier.calls
        await drain(worker)
        assert classifier.calls == [source.event_id]
    finally:
        await worker.close()


async def test_http_journal_attempt_lookup_outage_does_not_retire_unclassified_work(store):
    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "http-journal-recovery")
    auth = Auth({"decisions": "synthetic-decisions-token-0000000000"})

    class OnceFailure(httpx.AsyncBaseTransport):
        failed = False

        def __init__(self):
            self.actual = httpx.ASGITransport(app=create_app(store, auth))

        async def handle_async_request(self, request):
            if request.url.path == "/v1/transcript/windows/attempts" and not self.failed:
                self.failed = True
                return httpx.Response(503, json={"detail": "Synthetic journal outage"})
            return await self.actual.handle_async_request(request)

    client = JournalClient("http://fixture", auth.tokens["decisions"], transport=OnceFailure())
    classifier = Classifier()
    worker = WindowDecisionsWorker(client, Agent(), classifier)
    try:
        await worker.tick()
        await asyncio.wait_for(asyncio.gather(*(t for _, t in worker.jobs.values())), 2)
        assert not classifier.calls and store.window_classification_attempts(source.event_id) == 0
        assert [s.event_id for s in store.pending_windows()] == [source.event_id]
        assert worker.last_error == "journal_unavailable"
        await drain(worker)
        assert classifier.calls == [source.event_id]
        assert store.window_decision(source.event_id) is not None
        assert not store.pending_windows()
    finally:
        await worker.close()
        await client.close()


async def test_lost_native_decision_acknowledgment_reconciles_before_retry_or_restart(store):
    class LostAck(LocalJournal):
        lost = False

        async def append(self, request):
            entry = await super().append(request)
            if request.kind == "routing.window_decided" and not self.lost:
                self.lost = True
                raise httpx.ReadTimeout("Synthetic lost acknowledgment after native commit")
            return entry

    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "lost-ack")
    classifier = Classifier()
    journal = LostAck(store, "decisions")
    worker = WindowDecisionsWorker(journal, Agent(), classifier)
    try:
        await drain(worker)
        assert classifier.calls == [source.event_id]
        assert store.window_decision(source.event_id) is not None
        assert store.window_classification_attempts(source.event_id) == 1
        assert not store.pending_windows()
        assert not any(
            e.payload["phase"] == "error" for e in store.read(kinds=["routing.window_status"])
        )
    finally:
        await worker.close()
    restarted = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), classifier)
    try:
        await drain(restarted)
        assert classifier.calls == [source.event_id]
    finally:
        await restarted.close()


@pytest.mark.parametrize("attempt", [True, False, 1.0, None, "1", -1, 3])
async def test_malformed_historical_attempt_numbering_fails_closed(store, attempt):
    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "malformed-legacy")
    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), classifier)
    await worker.status(source, "classifying", "content_window", attempt=1)
    receipt = store.read(kinds=["routing.window_status"])[0]
    # Simulate malformed historical bytes; the current append boundary rejects
    # these values. json_extract alone collapses JSON true into integer 1.
    receipt.payload["attempt"] = attempt
    with store.db:
        store.db.execute(
            "UPDATE entries SET value=? WHERE event_id=?",
            (receipt.model_dump_json(), receipt.event_id),
        )
    try:
        await drain(worker)
        assert not classifier.calls and not store.pending_windows()
        assert store.window_classification_attempts(source.event_id) == 2
    finally:
        await worker.close()


async def test_source_expiring_during_inference_cannot_publish_a_decision(store, monkeypatch):
    class Paused(Classifier):
        entered, release = asyncio.Event(), asyncio.Event()

        async def classify(self, source, *_):
            self.entered.set()
            await self.release.wait()
            return await super().classify(source)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=61)

    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "expires-inflight")
    classifier = Paused()
    worker = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), classifier)
    try:
        await worker.tick()
        await asyncio.wait_for(classifier.entered.wait(), 1)
        monkeypatch.setattr("services.decisions.window_worker.datetime", Later)
        classifier.release.set()
        await drain(worker)
        assert classifier.calls == [source.event_id]
        assert store.window_decision(source.event_id) is None
        assert store.read(kinds=["routing.window_status"])[-1].payload["phase"] == "expired"
    finally:
        await worker.close()


async def test_restart_cannot_duplicate_native_dispatch_or_replay_agent_off_windows(store):
    session = store.create_session(SessionRequest(channel="converse"))
    journal = LocalJournal(store, "decisions")
    agent, classifier = Agent(), Classifier(queue=True)
    off_source = window(store, session, "agent-off")
    off = WindowDecisionsWorker(journal, agent, classifier, agent_enabled=False)
    await drain(off)
    await off.close()
    assert (
        store.window_decision(off_source.event_id).payload["agent_route"]
        == "IGNORE_FOR_IMMEDIATE_ROUTING"
    )
    source = window(store, session, "one-task")
    worker = WindowDecisionsWorker(journal, agent, classifier)
    await drain(worker)
    await worker.close()
    dispatcher = WindowAgentDispatcher(journal, agent)
    decision = store.window_decision(source.event_id)
    await dispatcher.process(decision)
    assert len(agent.tasks) == 1
    restarted = WindowDecisionsWorker(journal, agent, classifier)
    try:
        await drain(restarted)
        await WindowAgentDispatcher(journal, agent).process(decision)
        assert len(agent.tasks) == 1
        assert Counter(classifier.calls) == Counter([off_source.event_id, source.event_id])
    finally:
        await restarted.close()


async def test_epoch_reset_fences_backlog_and_later_current_windows_progress(store):
    original = store.create_session(SessionRequest(channel="converse"))
    stale = window(store, original, "old-epoch")
    current = store.reset(original.session_id)
    fresh = window(store, current, "new-epoch")
    classifier = Classifier()
    worker = WindowDecisionsWorker(LocalJournal(store, "decisions"), Agent(), classifier)
    try:
        await drain(worker)
        assert classifier.calls == [fresh.event_id]
        assert store.window_decision(stale.event_id) is None
        assert store.entry(stale.event_id).epoch == original.epoch
    finally:
        await worker.close()


@pytest.mark.parametrize("limit", [True, 0, 201, -1, 1.5, "2"])
def test_native_pending_query_bounds(store, limit):
    with pytest.raises(JournalError):
        store.pending_windows(limit=limit)


@pytest.mark.parametrize("max_age", [True, 0, -1, 3601, float("inf"), float("nan"), "60"])
def test_invalid_expiry_bounds_are_rejected(store, max_age):
    with pytest.raises(ValueError, match="maximum age"):
        WindowDecisionsWorker(
            LocalJournal(store, "decisions"), Agent(), Classifier(), max_age=max_age
        )


def test_status_cannot_retire_another_session_window_or_omit_its_source_cause(store):
    sessions = [store.create_session(SessionRequest(channel="converse")) for _ in range(2)]
    sources = [window(store, session, f"source-{i}") for i, session in enumerate(sessions)]
    for session, causes in ((sessions[1], [sources[1].event_id]), (sessions[0], [])):
        with pytest.raises(JournalError, match="authenticated source"):
            store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="routing.window_status",
                    payload={
                        "source_event_id": sources[0].event_id,
                        "phase": "expired",
                        "reason": "invalid_scope",
                    },
                    causes=causes,
                    idempotency_key="invalid-status",
                ),
                "decisions",
            )
    assert {s.event_id for s in store.pending_windows()} == {s.event_id for s in sources}


async def test_window_admission_http_queries_authentication_scope_and_bounds(store):
    session = store.create_session(SessionRequest(channel="converse"))
    source = window(store, session, "http")
    auth = Auth({r: (r + "-") * 32 for r in ("owner", "decisions", "agent", "converse")})
    transport = httpx.ASGITransport(app=create_app(store, auth))
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as http:
        routes = [
            ("/v1/transcript/windows/pending", {}),
            ("/v1/transcript/windows/attempts", {"source_event_id": source.event_id}),
        ]
        for path, params in routes:
            assert (await http.get(path, params=params)).status_code == 401
            for role in ("agent", "converse"):
                response = await http.get(
                    path, params=params, headers={"Authorization": "Bearer " + auth.tokens[role]}
                )
                assert response.status_code == 403
            assert (
                await http.get(
                    path,
                    params=params,
                    headers={
                        "Authorization": "Bearer " + auth.tokens["owner"],
                        "Origin": "https://untrusted.invalid",
                    },
                )
            ).status_code == 403
        owner = {"Authorization": "Bearer " + auth.tokens["owner"]}
        for limit in (0, 201):
            assert (
                await http.get(routes[0][0], params={"limit": limit}, headers=owner)
            ).status_code == 422
        for reference in ("", "x" * 129):
            assert (
                await http.get(routes[1][0], params={"source_event_id": reference}, headers=owner)
            ).status_code == 422
    for role in ("decisions", "owner"):
        client = JournalClient("http://fixture", auth.tokens[role], transport=transport)
        try:
            assert [s.event_id for s in await client.pending_windows(limit=1)] == [source.event_id]
            assert await client.window_classification_attempts(source.event_id) == 0
        finally:
            await client.close()
    with pytest.raises(PermissionError):
        await LocalJournal(store, "agent").pending_windows()
    with pytest.raises(PermissionError):
        await LocalJournal(store, "converse").window_classification_attempts(source.event_id)
