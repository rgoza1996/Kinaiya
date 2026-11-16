"""Actual journal permission: original registration, pre-ACK fence and source bounds."""

import concurrent.futures
from types import SimpleNamespace

import pytest

from packages.contracts.live_captures import (
    MAX_ACTIVE,
    PROTOCOL,
    LiveCaptureFence,
    LiveCaptureRegistration,
)
from packages.contracts.models import SessionRequest
from services.session.live_captures import LiveCaptureStore
from services.session.live_windows import publish
from services.session.store import Store
from tests.integration.test_live_window_publication import original


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "native-capture.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    assert store.live_captures.activate()
    yield SimpleNamespace(store=store, session=session)
    store.close()


def registration(env, **change):
    return LiveCaptureRegistration(
        schema_version="2.0",
        protocol=PROTOCOL,
        backend=env.store.live_captures.backend(),
        owner_instance="a" * 32,
        capture_id="b" * 32,
        session_id=env.session.session_id,
        thread_id=env.session.thread_id,
        epoch=env.session.epoch,
        **change,
    )


def fence(request, **change):
    return LiveCaptureFence.model_validate(
        dict(
            schema_version="2.0",
            protocol=PROTOCOL,
            original=request.model_dump(mode="json"),
            request_id="c" * 32,
            reason="owner_off",
        )
        | change,
        strict=True,
    )


def test_original_registration_is_exact_current_and_read_only_lookup_does_not_wake(env):
    owner = env.store.live_captures
    request = registration(env)
    unknown = owner.lookup(request)
    assert unknown.receipt is unknown.fence is None and not unknown.capture_open
    state = owner.register(request)
    assert state.capture_open and state.receipt.original == request
    assert state.receipt.registered_sequence == 0
    assert owner.register(request) == state
    cursor = env.store.journal_wake.snapshot()
    assert owner.lookup(request) == state
    assert owner.backend() == state.backend
    assert env.store.journal_wake.snapshot() == cursor
    assert env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 1


def test_cancel_before_delayed_registration_never_reopens_or_claims_a_registration(env):
    owner = env.store.live_captures
    request = registration(env)
    stop = fence(request)
    closed = owner.fence(stop)
    assert closed.receipt is None and closed.fence.request == stop
    assert not closed.capture_open
    assert owner.register(request) == closed
    assert owner.fence(stop) == closed
    assert owner.lookup(request) == closed
    with pytest.raises(ValueError):
        owner.fence(fence(request, request_id="d" * 32))
    assert not env.store.db.execute("SELECT receipt FROM live_captures").fetchone()[0]


def test_positive_fence_retains_original_receipt_and_does_not_delete_transcript(env):
    owner = env.store.live_captures
    request = registration(env)
    start = owner.register(request)
    source = publish(env.store, original(env))
    allowed = owner.source_state(source.original)
    assert allowed.capture_permits_source and allowed.capture.receipt == start.receipt
    stopped = owner.fence(fence(request))
    assert stopped.receipt == start.receipt and not stopped.capture_open
    late = publish(env.store, original(env, 1, text="Public late recognized text"))
    assert not owner.source_state(late.original).capture_permits_source
    assert env.store.entry(late.source.event_id).payload["text"] == "Public late recognized text"
    assert owner.register(request) == stopped


def test_registration_never_adopts_a_preexisting_source(env):
    source = publish(env.store, original(env))
    owner = env.store.live_captures
    assert not owner.source_state(source.original).capture_permits_source
    registered = owner.register(registration(env))
    assert registered.receipt.registered_sequence == source.capture.sequence
    assert not owner.source_state(source.original).capture_permits_source
    newer = publish(env.store, original(env, 1, text="Public new eligible input"))
    assert owner.source_state(newer.original).capture_permits_source


def test_competing_register_and_fence_serialize_without_resurrection(env):
    owner = env.store.live_captures
    request = registration(env)
    stop = fence(request)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = [
            pool.submit(owner.fence if i % 2 else owner.register, stop if i % 2 else request)
            for i in range(32)
        ]
        for result in results:
            result.result()
    final = owner.lookup(request)
    assert not final.capture_open and final.fence.request == stop
    assert owner.register(request) == final
    assert env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 1


def test_reset_fences_source_and_deletion_absence_is_unknown(env):
    owner = env.store.live_captures
    request = registration(env)
    started = owner.register(request)
    source = publish(env.store, original(env))
    env.store.reset(env.session.session_id)
    state = owner.lookup(request)
    assert state.receipt == started.receipt and not state.scope_current and not state.capture_open
    assert not owner.source_state(source.original).capture_permits_source
    assert owner.fence(fence(request)).fence is not None
    env.store.delete(env.session.session_id)
    absent = owner.lookup(request)
    assert absent.receipt is absent.fence is None and not absent.capture_open


def test_controller_replacement_preserves_receipt_without_reattaching_permission(env):
    first = env.store.live_captures
    request = registration(env)
    started = first.register(request)
    replacement = Store(env.store.path)
    try:
        assert replacement.live_captures.activate()
        with pytest.raises(ValueError):
            first.backend()
        state = replacement.live_captures.lookup(request)
        assert state.receipt == started.receipt and not state.capture_open
        assert state.backend.backend_id == request.backend.backend_id
        assert state.backend.controller_id != request.backend.controller_id
        assert replacement.live_captures.register(request) == state
    finally:
        replacement.close()


@pytest.mark.parametrize(
    "column,value",
    [
        ("request", " " * 4097),
        ("receipt", " " * 16385),
        ("thread_id", "foreign"),
        ("epoch", 2),
        ("controller_id", "d" * 32),
        ("request_sha256", "f" * 64),
    ],
    ids=["request-bound", "receipt-bound", "thread", "epoch", "controller", "digest"],
)
def test_corrupt_protected_capture_columns_cannot_report_current_permission(env, column, value):
    request = registration(env)
    owner = env.store.live_captures
    owner.register(request)
    with env.store.db:
        env.store.db.execute(f"UPDATE live_captures SET {column}=?", (value,))
    with pytest.raises(ValueError):
        owner.lookup(request)


def test_corrupt_native_identity_is_not_repaired_or_a_legacy_service_outage(env):
    request = registration(env)
    env.store.live_captures.register(request)
    with env.store.db:
        env.store.db.execute("DELETE FROM live_capture_meta")
    broken = LiveCaptureStore(env.store)
    assert not broken.activate()
    assert broken.failed
    with pytest.raises(ValueError):
        broken.backend()
    assert env.store.db.execute("SELECT count(*) FROM live_capture_meta").fetchone()[0] == 0
    assert env.store.create_session(SessionRequest(channel="agent")).channel == "agent"


def test_current_permission_capacity_is_bounded_but_a_fence_releases_only_permission(env):
    owner = env.store.live_captures
    requests = []
    for i in range(MAX_ACTIVE):
        request = registration(env).model_copy(update={"capture_id": f"{i:032x}"})
        assert owner.register(request).capture_open
        requests.append(request)
    overflow = registration(env).model_copy(update={"capture_id": "f" * 32})
    with pytest.raises(ValueError):
        owner.register(overflow)
    assert not owner.fence(fence(requests[0])).capture_open
    assert owner.register(overflow).capture_open
    assert (
        env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == MAX_ACTIVE + 1
    )


@pytest.mark.parametrize("operation", ["register", "fence", "initialize"])
def test_refusing_an_existing_transaction_never_rolls_back_the_original_owner(env, operation):
    request = registration(env)
    db = env.store.db
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "UPDATE sessions SET value=json_set(value,'$.name','public-uncommitted-owner') WHERE session_id=?",
        (env.session.session_id,),
    )
    try:
        if operation == "initialize":
            initializer = LiveCaptureStore(env.store)
            assert not initializer.activate() and initializer.failed
        else:
            with pytest.raises(ValueError):
                getattr(env.store.live_captures, operation)(
                    request if operation == "register" else fence(request)
                )
        assert db.in_transaction
        assert env.store.session(env.session.session_id).name == "public-uncommitted-owner"
        assert db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 0
    finally:
        db.rollback()


def test_passive_observer_never_claims_or_replaces_registered_capture_permission(env):
    owner = env.store.live_captures
    request = registration(env)
    registered = owner.register(request)
    before = tuple(tuple(row) for row in env.store.db.execute("SELECT * FROM live_capture_meta"))
    observer = Store(env.store.path)
    try:
        assert not observer.live_captures.activated
        with pytest.raises(ValueError):
            observer.live_captures.backend()
        assert (
            tuple(tuple(row) for row in env.store.db.execute("SELECT * FROM live_capture_meta"))
            == before
        )
        assert owner.lookup(request) == registered and owner.backend() == request.backend
        assert observer.live_captures.activate()
        assert not owner.activate()  # A replaced controller cannot reclaim itself.
        assert not observer.live_captures.lookup(request).capture_open
    finally:
        observer.close()
