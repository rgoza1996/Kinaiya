"""Original native source/capture pairs through the actual SQLite journal."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from packages.contracts.live_windows import (
    CAPTURE_KIND,
    PROTOCOL,
    LiveWindowCapture,
    LiveWindowPublication,
    LiveWindowPublicationProof,
)
from packages.contracts.models import AppendRequest, SessionRequest, SpeechWindow
from services.session.live_windows import latest, lookup, publish
from services.session.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "original-live.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    yield SimpleNamespace(store=store, session=session)
    store.close()


def original(env, index=0, *, capture_id="b" * 32, **changes):
    window = (
        SpeechWindow(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            utterance_id="public-live-utterance",
            window_index=index,
            revision=1,
            text="A public continuous thought café Ω",
            start_ms=0,
            end_ms=1000 + index,
            user_speaking=True,
            capture_ended=False,
            content_scope="public_synthetic",
        ).model_dump(mode="json")
        | changes
    )
    return LiveWindowPublication(
        schema_version="2.0",
        protocol=PROTOCOL,
        owner_instance="a" * 32,
        capture_id=capture_id,
        request=AppendRequest(
            session_id=env.session.session_id,
            thread_id=env.session.thread_id,
            epoch=env.session.epoch,
            kind="conversation.window",
            payload=window,
            idempotency_key=f"public-live-{index}-{window['revision']}",
        ),
    )


def candidate(env):
    return latest(
        env.store,
        env.session.session_id,
        env.session.thread_id,
        env.session.epoch,
        "public-live-utterance",
    )


def test_atomic_pair_preserves_v1_source_and_exact_lost_ack_lookup(env):
    request = original(env)
    before = env.store.journal_wake.snapshot()
    receipt = publish(env.store, request)
    assert receipt.source.payload == request.request.payload
    assert receipt.source.schema_version == "1.0"
    assert receipt.source.origin == "authenticated-user"
    assert receipt.capture.kind == CAPTURE_KIND
    assert receipt.capture.sequence == receipt.source.sequence + 1
    assert lookup(env.store, request.reference()).receipt == receipt
    assert publish(env.store, request) == receipt
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 2
    assert not env.store.db.in_transaction
    assert env.store.journal_wake.snapshot().revision > before.revision
    assert candidate(env).eligible == candidate(env).latest == receipt


def test_failure_between_source_and_marker_rolls_back_both(env, monkeypatch):
    append = env.store._append_entry_locked

    def fail(request, role):
        if request.kind == CAPTURE_KIND:
            assert env.store.db.in_transaction
            raise RuntimeError("Public synthetic marker fault")
        return append(request, role)

    monkeypatch.setattr(env.store, "_append_entry_locked", fail)
    request = original(env)
    with pytest.raises(RuntimeError):
        publish(env.store, request)
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    assert lookup(env.store, request.reference()).receipt is None
    assert candidate(env) is None
    assert not env.store.db.in_transaction


def test_duplicate_raw_snapshots_keep_the_first_eligible_original(env):
    first = publish(env.store, original(env))
    second = publish(env.store, original(env, 1, audio_sha256="c" * 64))
    third = publish(env.store, original(env, 2, revision=1, end_ms=2000))
    current = candidate(env)
    assert current.eligible == first and current.latest == third
    assert first.source.event_id != second.source.event_id != third.source.event_id
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 6
    markers = [LiveWindowCapture.model_validate(r.capture.payload) for r in (first, second, third)]
    assert {m.eligible_revision for m in markers} == {1}
    assert {m.eligible_source_event_id for m in markers} == {first.source.event_id}
    assert env.store.pending_windows() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "A public continuous thought café Ω!"},
        {"user_speaking": False},
        {"user_speaking": False, "capture_ended": True},
        {"content_scope": "local_only"},
    ],
)
def test_exact_content_hint_or_scope_change_advances_eligible_revision(env, changes):
    first = publish(env.store, original(env))
    current = publish(env.store, original(env, 1, **changes))
    marker = LiveWindowCapture.model_validate(current.capture.payload)
    assert marker.eligible_revision == 2
    assert marker.eligible_source_event_id == current.source.event_id
    assert candidate(env).eligible == current
    assert lookup(env.store, first.original).receipt == first


def test_live_and_legacy_utterances_are_partitioned_without_retroactive_conversion(env):
    legacy_request = original(env).request
    legacy = env.store.append(legacy_request, "converse")
    assert env.store.pending_windows() == [legacy]
    with pytest.raises(ValueError):
        publish(env.store, original(env, 1))
    live = publish(env.store, original(env, 1, utterance_id="separate-live"))
    assert env.store.pending_windows() == [legacy]
    with pytest.raises(ValueError):
        env.store.append(original(env, 2, utterance_id="separate-live").request, "converse")
    assert lookup(env.store, live.original).receipt == live


def test_capture_owner_and_original_digest_cannot_be_replaced(env):
    receipt = publish(env.store, original(env))
    for request in (original(env, 1, capture_id="d" * 32), original(env, text="changed")):
        with pytest.raises(ValueError):
            publish(env.store, request)
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 2
    with pytest.raises(ValueError):
        env.store.append(
            AppendRequest(
                **receipt.capture.model_dump(
                    exclude={"event_id", "sequence", "origin", "created_at"}
                )
            ),
            "converse",
        )


def test_concurrent_exact_publications_commit_one_pair(env):
    request = original(env)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: publish(env.store, request), range(16)))
    assert all(result == results[0] for result in results)
    assert env.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 2


@pytest.mark.parametrize(
    "column,value", [("request", " " * 65537), ("value", " " * 131073), ("event_id", "x" * 129)]
)
def test_oversized_protected_columns_fail_before_original_lookup(env, column, value):
    request = original(env)
    receipt = publish(env.store, request)
    # Corrupt a protected source column, not the client-provided reference.
    with env.store.db:
        env.store.db.execute(
            f"UPDATE entries SET {column}=? WHERE event_id=?", (value, receipt.source.event_id)
        )
    with pytest.raises(ValueError):
        lookup(env.store, request.reference())


@pytest.mark.parametrize(
    "column,value",
    [("thread_id", "foreign-thread"), ("epoch", 2), ("kind", "conversation.converse")],
)
def test_protected_native_column_disagreement_cannot_prove_publication(env, column, value):
    request = original(env)
    receipt = publish(env.store, request)
    with env.store.db:
        env.store.db.execute(
            f"UPDATE entries SET {column}=? WHERE event_id=?", (value, receipt.source.event_id)
        )
    with pytest.raises(ValueError):
        lookup(env.store, request.reference())


def test_pair_json_roundtrip_keeps_datetime_and_exact_identity(env):
    receipt = publish(env.store, original(env))
    assert (
        LiveWindowPublicationProof.model_validate_json(receipt.model_dump_json(), strict=True)
        == receipt
    )


def test_other_connection_never_observes_unannotated_live_source(env, monkeypatch):
    observer = Store(env.store.path)
    append = env.store._append_entry_locked
    observations = []

    def inspect(request, role):
        result = append(request, role)
        if request.kind == "conversation.window":
            observations.append(observer.db.execute("SELECT count(*) FROM entries").fetchone()[0])
            assert observer.pending_windows() == []
        return result

    monkeypatch.setattr(env.store, "_append_entry_locked", inspect)
    try:
        publish(env.store, original(env))
        assert observations == [0]
        assert observer.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 2
        assert observer.pending_windows() == []
    finally:
        observer.close()


def test_restart_reset_and_deletion_never_convert_missing_proof_into_no_effect(env):
    request = original(env)
    receipt = publish(env.store, request)
    reopened = Store(env.store.path)
    try:
        assert lookup(reopened, request.reference()).receipt == receipt
    finally:
        reopened.close()
    env.store.reset(env.session.session_id)
    assert lookup(env.store, request.reference()).receipt == receipt
    with pytest.raises(ValueError):
        candidate(env)
    env.store.delete(env.session.session_id)
    assert lookup(env.store, request.reference()).receipt is None


async def test_rolling_worker_cannot_admit_live_source(env):
    from services.decisions.window_worker import WindowDecisionsWorker
    from services.session.store import LocalJournal

    class NeverCalled:
        async def classify(self, *_):
            pytest.fail("The four-outcome worker consumed an Adaptive source")

    publish(env.store, original(env))
    worker = WindowDecisionsWorker(LocalJournal(env.store, "decisions"), None, NeverCalled())
    try:
        assert await worker.tick() == 0
        assert not worker.jobs and not worker.pending
    finally:
        await worker.close()


@pytest.mark.parametrize("kind", ["routing.window_decided", "routing.window_status"])
def test_native_v1_publication_cannot_claim_a_live_v2_source(env, kind):
    from packages.contracts.models import WindowDecision

    source = publish(env.store, original(env)).source
    common = dict(
        source_event_id=source.event_id, source_sequence=source.sequence, source_revision=1
    )
    payload = (
        WindowDecision(
            **common,
            conversation="WAIT_FOR_NEXT_WINDOW",
            agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
        ).model_dump(mode="json")
        if kind == "routing.window_decided"
        else common | {"phase": "classifying", "reason": "public_synthetic_control", "attempt": 1}
    )
    with pytest.raises(ValueError):
        env.store.append(
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind=kind,
                payload=payload,
                causes=[source.event_id],
                idempotency_key="public-old-worker-publication",
            ),
            "decisions",
        )


def test_changed_native_stt_identity_advances_without_semantic_rewriting(env):
    from packages.contracts.models import STTModelPin

    first = publish(env.store, original(env, text="x" * 16000))
    pin = STTModelPin(
        source="host",
        backend_id="d" * 32,
        instance_id="e" * 32,
        settings_revision=0,
        catalog_revision=None,
        catalog_binding=None,
        provider=None,
        model=None,
        local=None,
    )
    second = publish(
        env.store, original(env, 1, text="x" * 16000, stt_model=pin.model_dump(mode="json"))
    )
    assert second.source.payload["text"] == first.source.payload["text"] == "x" * 16000
    assert LiveWindowCapture.model_validate(second.capture.payload).eligible_revision == 2
    assert candidate(env).eligible == second
