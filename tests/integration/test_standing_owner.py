"""Independent #228 native owner oracles frozen before native author import.

SQLite commits, retained rows, authenticated ASGI routes and owned threads are
real. Gates control actual commit return/call scheduling, never fabricate an
ACK or permission to execute scheduled work. No provider or task is launched.
"""

import asyncio
import copy
import hashlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from packages.contracts.agent_control import AgentModeChange
from packages.contracts.models import SessionRequest
from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeLookup,
    StandingChangeReceipt,
    StandingConflict,
    StandingRecord,
    StandingResponsibilityPage,
    StandingResponsibilitySnapshot,
    StandingUnavailable,
    canonical_bytes,
    standing_json,
)
from services.common.auth import Auth
from services.session import standing_work, standing_work_api
from services.session.agent_control import AgentControl
from services.session.api import create_app
from services.session.store import LocalJournal, Store

StandingOwner = standing_work.StandingOwner
install_standing_owner_routes = standing_work_api.install_standing_owner_routes

CHANGES = "/v1/standing/changes"
LOOKUP = CHANGES + "/lookup"
LIST = "/v1/standing/responsibilities"
FIRST = "00000000-0000-0000-0000-000000000001"
UNKNOWN = "00000000-0000-0000-0000-999999999999"
TABLES = (
    "standing_owner_meta",
    "standing_owner_changes",
    "standing_authorizations",
    "standing_owner_current",
)
TOKENS = dict(
    zip(
        ("owner", "agent", "converse", "decisions", "resources"),
        (c * 32 for c in "oacdr"),
        strict=True,
    )
)
AUTH = Auth(TOKENS, allowed_origins=("http://synthetic-owner.test",))
MARKER = "PUBLIC-SYNTHETIC-NOT-FOR-ERROR-ECHO"


class Clock:
    def __init__(self):
        self.value = datetime(2024, 1, 1, 0, 0, 0, 250000, tzinfo=UTC)

    def __call__(self):
        return self.value


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def headers(role="owner", **extra):
    return {**({"Authorization": "Bearer " + TOKENS[role]} if role else {}), **extra}


def proposal(env, *, key="standing-1", responsibility=FIRST, revision=0, enabled=True, **changes):
    control = env.control.snapshot()
    return StandingChange.model_validate(
        {
            "schema_version": "1.0",
            "idempotency_key": key,
            "expected_backend": control.backend_id,
            "expected_instance": control.instance_id,
            "expected_revision": revision,
            "responsibility_id": responsibility,
            "authorization": {
                "label": "Synthetic inspection",
                "objective": "Inspect synthetic metadata.\0Literal é/e\u0301.",
                "content_scope": "public_synthetic",
                "enabled": enabled,
                "task_session": {
                    "session_id": env.session.session_id,
                    "thread_id": env.session.thread_id,
                    "epoch": env.session.epoch,
                },
                "workspace_pin": {
                    "schema_version": "1.0",
                    "workspace_id": "b" * 64,
                    "root_identity": "c" * 64,
                    "settings_revision": 0,
                    "source": "host",
                    "shell_backend": "bubblewrap",
                    "mode": "direct",
                },
                "schedule": {
                    "kind": "daily_local",
                    "timezone": "UTC",
                    "local_time": "09:00",
                    "fold_policy": "earlier",
                    "gap_policy": "skip",
                    "grace_seconds": 120,
                    "missed_run_policy": "latest_within_grace",
                    "start_local_date": "2024-01-01",
                    "valid_until_utc": "2024-01-31T00:00:00Z",
                },
                "delivery": {
                    "kind": "in_app",
                    "attention": "routine",
                    "preview": "status_only",
                    "quiet_hours": None,
                },
                "overlap_policy": "one_unresolved_per_authorization",
                "maximum_runs": 3,
                "inference_authorization": "configured_agent_profile",
            },
            **changes,
        }
    )


def revised(original, **changes):
    raw = original.model_dump(mode="json")
    raw.update(changes)
    return StandingChange.model_validate(raw)


def database_rows(connection):
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
        )
    ]
    return tuple(
        (
            name,
            tuple(
                sorted(
                    (
                        tuple(row)
                        for row in connection.execute(
                            'SELECT * FROM "' + name.replace('"', '""') + '"'
                        )
                    ),
                    key=repr,
                )
            ),
        )
        for name in names
    )


def disk_record(env, responsibility=FIRST, revision=1):
    with sqlite3.connect(env.path) as connection:
        row = connection.execute(
            "SELECT record FROM standing_authorizations WHERE responsibility_id=? AND revision=?",
            (responsibility, revision),
        ).fetchone()
    assert row is not None
    return StandingRecord.model_validate(standing_json(row[0].encode()))


def counts(env):
    return tuple(
        env.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES[1:]
    )


class CommitBoundary(sqlite3.Connection):
    """A real connection whose one chosen actual COMMIT return is controlled."""

    watched_key = None
    before_commit = None
    after_commit = None

    def _matches(self):
        if self.watched_key is None:
            return False
        return (
            self.execute(
                "SELECT 1 FROM standing_owner_changes WHERE key=?", (self.watched_key,)
            ).fetchone()
            is not None
        )

    def commit(self):
        matching = self._matches()
        if matching and self.before_commit is not None:
            callback, self.before_commit = self.before_commit, None
            callback()
        result = super().commit()
        if matching and self.after_commit is not None:
            callback, self.after_commit = self.after_commit, None
            callback()
        return result

    def __exit__(self, exc_type, exc, traceback):
        # SQLite's C context manager bypasses Python commit overrides.
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def make_store(path):
    # Supply the real SQLite subclass from construction, before any controller
    # may retain its connection. Restore the constructor hook immediately.
    connect = sqlite3.connect

    def owned_connection(*args, **kwargs):
        return connect(*args, **{**kwargs, "factory": CommitBoundary})

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", owned_connection)
        return Store(path)


def controlled_connection(env, request):
    connection = env.store.db
    assert isinstance(connection, CommitBoundary)
    connection.watched_key = request.idempotency_key
    return connection


def commit_gate(connection):
    committed, release = threading.Event(), threading.Event()

    def pause_after_real_commit():
        committed.set()
        assert release.wait(45), "Owned test commit gate was not released"

    connection.after_commit = pause_after_real_commit
    return committed, release


async def entered(event):
    # The 64-owner case intentionally occupies the worker pool. Observing its
    # gate must not queue another worker behind the work awaiting our release.
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(0.005)


def close_native(env):
    try:
        env.owner.close()
    finally:
        try:
            env.control.close()
        finally:
            env.store.close()
            env.closed = True


@pytest.fixture
def native_factory(tmp_path):
    tmp_path.chmod(0o700)
    opened = []

    def make(*, path=None, register=True, clock=None):
        path = tmp_path / "session.sqlite" if path is None else path
        store = make_store(path)
        control = AgentControl(store, new_store=store.agent_control.new_store)
        control.register()
        clock = Clock() if clock is None else clock
        session = store.create_session(SessionRequest(channel="agent"))
        owner = StandingOwner(store, control, clock=clock)
        env = SimpleNamespace(
            path=path,
            store=store,
            control=control,
            owner=owner,
            session=session,
            clock=clock,
            closed=False,
        )
        opened.append(env)
        if register:
            owner.register()
        return env

    try:
        yield make
    finally:
        for env in reversed(opened):
            if not env.closed:
                close_native(env)


@pytest.fixture
def native(native_factory):
    return native_factory()


@pytest.fixture
async def http_native(tmp_path):
    tmp_path.chmod(0o700)
    store = make_store(tmp_path / "http.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    clock = Clock()
    app = create_app(store, AUTH, standing_clock=clock)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://session.test"
            ) as client,
        ):
            yield SimpleNamespace(
                path=tmp_path / "http.sqlite",
                store=store,
                session=session,
                clock=clock,
                app=app,
                control=app.state.agent_control,
                owner=app.state.standing_owner,
                operations=app.state.standing_owner_operations,
                client=client,
            )
    finally:
        store.close()


def response_status(response, status):
    assert response.status_code == status, (response.status_code, response.text)
    assert response.headers.get("cache-control") == "no-store"
    assert len(response.content) <= 262144
    if status >= 400:
        assert MARKER not in response.text


async def post(env, path, body):
    return await env.client.post(
        path,
        content=canonical_bytes(body),
        headers=headers(**{"Content-Type": "application/json; charset=utf-8"}),
    )


def test_save_arm_and_exact_replay_are_durable_without_task_or_mode_effects(native):
    request = proposal(native)
    before_control = native.control.snapshot()
    saved = native.owner.change(request)
    saved.validate_request(request)
    detail = native.owner.snapshot(FIRST)
    assert detail.revision == 1 and detail.record.activation == "armed"
    assert detail.record.authorized_from_utc == "2024-01-01T00:00:01Z"
    assert detail.record.receipt == saved
    before = database_rows(native.store.db)
    assert native.owner.change(request) == saved
    assert native.owner.lookup(request).receipt == saved
    assert database_rows(native.store.db) == before
    assert native.control.snapshot() == before_control
    assert before_control.standing_work_available is False
    assert native.store.read(kinds=["agent.command"]) == []
    native.owner.close()
    assert native.control.snapshot() == before_control


def test_disable_and_noop_are_new_cas_revisions_not_deletions(native):
    first = proposal(native)
    original = native.owner.change(first)
    noop = revised(first, expected_revision=1, idempotency_key="noop")
    assert native.owner.change(noop).revision == 2
    disabled = proposal(native, key="disabled", revision=2, enabled=False)
    assert native.owner.change(disabled).revision == 3
    assert native.owner.snapshot(FIRST).record.receipt.request.authorization.enabled is False
    assert counts(native) == (3, 3, 1)
    assert native.owner.change(first) == original
    with pytest.raises(StandingConflict):
        native.owner.change(revised(first, idempotency_key="stale"))


@pytest.mark.parametrize("fault", ["channel", "thread", "epoch", "missing"])
def test_new_save_requires_actual_agent_session_thread_and_epoch(native, fault):
    request = proposal(native)
    raw = request.model_dump(mode="json")
    if fault == "channel":
        session = native.store.create_session(SessionRequest(channel="converse"))
        raw["authorization"]["task_session"] = {
            "session_id": session.session_id,
            "thread_id": session.thread_id,
            "epoch": session.epoch,
        }
    elif fault == "thread":
        raw["authorization"]["task_session"]["thread_id"] = "other-thread"
    elif fault == "epoch":
        raw["authorization"]["task_session"]["epoch"] += 1
    else:
        raw["authorization"]["task_session"]["session_id"] = "missing-session"
    with pytest.raises(StandingConflict):
        native.owner.change(StandingChange.model_validate(raw))
    assert counts(native) == (0, 0, 0)


@pytest.mark.parametrize("changed", ["epoch", "deleted", "expired"])
def test_retained_exact_proof_precedes_later_source_and_clock_eligibility(native, changed):
    request = proposal(native)
    saved = native.owner.change(request)
    if changed == "epoch":
        native.store.reset(native.session.session_id)
    elif changed == "deleted":
        native.store.delete(native.session.session_id)
    else:
        native.clock.value = datetime(2024, 2, 1, tzinfo=UTC)
    before = database_rows(native.store.db)
    assert native.owner.change(request) == saved
    assert native.owner.lookup(request).receipt == saved
    with pytest.raises(StandingConflict):
        native.owner.change(
            revised(
                request,
                authorization={
                    **request.authorization.model_dump(mode="json"),
                    "objective": "changed",
                },
            )
        )
    assert database_rows(native.store.db) == before


async def test_two_simultaneous_cas_calls_have_one_actual_sqlite_winner(native):
    start = threading.Barrier(3)

    def attempt(request):
        start.wait(5)
        try:
            return native.owner.change(request)
        except StandingConflict:
            return None

    requests = [proposal(native, key="cas-a"), proposal(native, key="cas-b")]
    tasks = [asyncio.create_task(asyncio.to_thread(attempt, request)) for request in requests]
    try:
        await asyncio.to_thread(start.wait, 5)
        results = await asyncio.gather(*tasks)
        winners = [item for item in results if item is not None]
        assert len(winners) == 1 and winners[0].revision == 1
        assert counts(native) == (1, 1, 1)
        with sqlite3.connect(native.path) as observer:
            assert (
                observer.execute("SELECT count(*) FROM standing_owner_changes").fetchone()[0] == 1
            )
    finally:
        start.abort()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_known_retirement_is_native_proof_but_off_equal_future_and_unknown_are_not(
    native, native_factory
):
    unobserved = proposal(native, key="unobserved")
    assert native.owner.lookup(unobserved).mutation_fenced is False
    state = native.control.snapshot()
    native.control.change(
        AgentModeChange(
            schema_version="1.0",
            idempotency_key="off",
            expected_backend=state.backend_id,
            expected_instance=state.instance_id,
            expected_revision=state.revision,
            mode="off",
            active_work_policy="finish_already_accepted",
        )
    )
    assert native.owner.lookup(unobserved).mutation_fenced is False
    assert native.owner.lookup(revised(unobserved, expected_revision=5)).mutation_fenced is False
    unknown = revised(unobserved, expected_instance=UNKNOWN)
    with pytest.raises(StandingConflict):
        native.owner.lookup(unknown)
    replacement = native_factory(path=native.path)
    proof = replacement.owner.lookup(unobserved)
    assert proof.request_instance == "retired" and proof.mutation_fenced is True
    with pytest.raises(StandingConflict):
        native.owner.change(unobserved)
    native.owner.close()
    assert replacement.control.snapshot().control_writable is True


async def test_passive_store_journal_owner_reads_never_initialize_or_register(native_factory):
    env = native_factory(register=False)
    before = database_rows(env.store.db)
    request = proposal(env)
    observer = Store(env.path)
    reader = StandingOwner(observer, observer.agent_control, clock=env.clock)
    try:
        assert await LocalJournal(observer, "agent").read() == []
        for operation in [
            lambda: reader.snapshot(FIRST),
            reader.page,
            lambda: reader.lookup(request),
        ]:
            with pytest.raises(StandingUnavailable):
                operation()
        assert database_rows(env.store.db) == before
        assert not set(TABLES) & {
            row[0]
            for row in observer.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
    finally:
        reader.close()
        observer.close()


def test_standing_register_requires_actual_registered_control_not_store_bootstrap(tmp_path):
    store = Store(tmp_path / "unregistered.sqlite")
    owner = StandingOwner(store, store.agent_control, clock=Clock())
    before = database_rows(store.db)
    try:
        with pytest.raises(StandingUnavailable):
            owner.register()
        assert database_rows(store.db) == before
    finally:
        owner.close()
        store.close()


@pytest.mark.parametrize("fault", ["partial", "version", "backend"])
def test_partial_foreign_or_wrong_version_schema_is_never_reset(native, fault):
    with native.store.db:
        if fault == "partial":
            native.store.db.execute("DROP TABLE standing_owner_current")
        elif fault == "version":
            native.store.db.execute("UPDATE standing_owner_meta SET version=2")
        else:
            native.store.db.execute("UPDATE standing_owner_meta SET backend_id=?", ("d" * 32,))
    before = database_rows(native.store.db)
    observer = StandingOwner(native.store, native.control, clock=native.clock)
    try:
        with pytest.raises(StandingUnavailable):
            observer.register()
        with pytest.raises(StandingUnavailable):
            observer.snapshot(FIRST)
        assert database_rows(native.store.db) == before
    finally:
        observer.close()


def test_real_schema_commit_failure_rolls_back_without_passive_repair(native_factory):
    env = native_factory(register=False)
    connection = controlled_connection(env, proposal(env))
    before = database_rows(connection)

    def fail_commit():
        raise sqlite3.OperationalError(MARKER)

    # Registration has no changes row; observe its actual transaction boundary.
    connection._matches = lambda: (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='standing_owner_meta'"
        ).fetchone()
        is not None
    )
    connection.before_commit = fail_commit
    with pytest.raises(StandingUnavailable) as error:
        env.owner.register()
    assert MARKER not in str(error.value)
    assert database_rows(connection) == before
    with pytest.raises(StandingUnavailable):
        env.owner.snapshot(FIRST)


def test_first_commit_ack_loss_is_pending_and_retry_lookup_never_arm(native):
    request = proposal(native)
    connection = controlled_connection(native, request)

    def lose_ack():
        raise sqlite3.OperationalError(MARKER)

    connection.after_commit = lose_ack
    with pytest.raises(StandingUnavailable):
        native.owner.change(request)
    pending = disk_record(native)
    assert pending.activation == "pending" and pending.authorized_from_utc is None
    before = database_rows(connection)
    retained = native.owner.lookup(request)
    assert retained.status == "recorded" and retained.receipt is not None
    assert native.owner.change(request) == retained.receipt
    assert native.owner.snapshot(FIRST).record == pending
    assert database_rows(connection) == before


@pytest.mark.parametrize("cutoff", ["cancelled", "deadline"])
async def test_acknowledged_save_after_cutoff_returns_receipt_but_cannot_arm(native, cutoff):
    request = proposal(native)
    initial_cancel = threading.Event()
    if cutoff == "cancelled":
        initial_cancel.set()
        with pytest.raises(StandingUnavailable):
            native.owner.change(request, cancelled=initial_cancel.is_set)
    else:
        with pytest.raises(StandingUnavailable):
            native.owner.change(request, deadline=time.monotonic() - 1)
    assert counts(native) == (0, 0, 0)
    connection = controlled_connection(native, request)
    committed, release = commit_gate(connection)
    cancelled = threading.Event()
    deadline = time.monotonic() + (10 if cutoff == "deadline" else 25)
    task = asyncio.create_task(
        asyncio.to_thread(
            native.owner.change, request, deadline=deadline, cancelled=cancelled.is_set
        )
    )
    try:
        await entered(committed)
        assert disk_record(native).activation == "pending"
        if cutoff == "cancelled":
            cancelled.set()
        else:
            await asyncio.sleep(max(0, deadline - time.monotonic()) + 0.01)
        release.set()
        saved = await task
        saved.validate_request(request)
        assert native.owner.snapshot(FIRST).record.activation == "pending"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_new_controller_and_revision_commit_cannot_be_overwritten_by_old_activation(
    native, native_factory
):
    first = proposal(native)
    connection = controlled_connection(native, first)
    committed, release = commit_gate(connection)
    task = asyncio.create_task(asyncio.to_thread(native.owner.change, first))
    try:
        await entered(committed)
        replacement = native_factory(path=native.path)
        second = proposal(replacement, key="new-revision", revision=1, enabled=False)
        current = replacement.owner.change(second)
        assert current.revision == 2
        release.set()
        retained = await task
        retained.validate_request(first)
        assert replacement.owner.snapshot(FIRST).record.receipt == current
        assert disk_record(native, revision=1).activation == "pending"
        assert replacement.owner.lookup(first).receipt == retained
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_same_backend_restart_preserves_armed_clock_and_historical_receipt(
    native, native_factory
):
    request = proposal(native)
    connection = controlled_connection(native, request)
    committed, release = commit_gate(connection)
    task = asyncio.create_task(asyncio.to_thread(native.owner.change, request))
    try:
        await entered(committed)
        native.clock.value = datetime(2024, 1, 1, 0, 0, 3, 250000, tzinfo=UTC)
        release.set()
        saved = await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    original = native.owner.snapshot(FIRST).record
    assert original.authorized_from_utc == "2024-01-01T00:00:04Z"
    close_native(native)
    later = Clock()
    later.value = datetime(2024, 1, 2, tzinfo=UTC)
    reopened = native_factory(path=native.path, clock=later)
    assert reopened.control.snapshot().backend_id == request.expected_backend
    assert reopened.owner.snapshot(FIRST).record == original
    assert reopened.owner.change(request) == saved
    assert reopened.owner.lookup(request).receipt == saved
    assert reopened.owner.snapshot(FIRST).record.authorized_from_utc == "2024-01-01T00:00:04Z"


@pytest.mark.parametrize("fault", ["digest", "request_bool", "receipt", "record", "pointer"])
def test_corrupt_retained_rows_are_unavailable_not_absence_or_normalized_proof(native, fault):
    request = proposal(native)
    saved = native.owner.change(request)
    retained_record = native.owner.snapshot(FIRST).record
    with native.store.db:
        if fault == "digest":
            native.store.db.execute("UPDATE standing_owner_changes SET digest=?", ("0" * 64,))
        elif fault == "request_bool":
            raw = request.model_dump(mode="json")
            raw["authorization"]["task_session"]["epoch"] = True
            native.store.db.execute(
                "UPDATE standing_owner_changes SET request=?,digest=?",
                (encoded(raw).decode(), hashlib.sha256(encoded(raw)).hexdigest()),
            )
        elif fault == "receipt":
            raw = saved.model_dump(mode="json")
            raw["revision"] = True
            native.store.db.execute(
                "UPDATE standing_owner_changes SET receipt=?", (encoded(raw).decode(),)
            )
        elif fault == "record":
            raw = retained_record.model_dump(mode="json")
            raw["record_digest"] = "0" * 64
            native.store.db.execute(
                "UPDATE standing_authorizations SET record=?", (encoded(raw).decode(),)
            )
        else:
            native.store.db.execute("UPDATE standing_owner_current SET revision=2")
    before = database_rows(native.store.db)
    for operation in [
        lambda: native.owner.lookup(request),
        lambda: native.owner.change(request),
        lambda: native.owner.snapshot(FIRST),
        native.owner.page,
    ]:
        with pytest.raises(StandingUnavailable):
            operation()
    assert database_rows(native.store.db) == before


def test_32_enabled_flags_include_expired_definitions_until_owner_disables(native):
    originals = []
    for number in range(1, 33):
        request = proposal(
            native, key=f"enabled-{number}", responsibility=f"00000000-0000-0000-0000-{number:012x}"
        )
        originals.append(request)
        native.owner.change(request)
    native.clock.value = datetime(2024, 2, 1, tzinfo=UTC)
    fresh = proposal(
        native, key="thirty-third", responsibility="00000000-0000-0000-0000-000000000033"
    )
    raw = fresh.model_dump(mode="json")
    raw["authorization"]["schedule"].update(
        start_local_date="2024-02-01", valid_until_utc="2024-02-20T00:00:00Z"
    )
    fresh = StandingChange.model_validate(raw)
    with pytest.raises(StandingUnavailable):
        native.owner.change(fresh)
    disabled = revised(
        fresh,
        idempotency_key="disable-first",
        responsibility_id=FIRST,
        expected_revision=1,
        authorization={**fresh.authorization.model_dump(mode="json"), "enabled": False},
    )
    assert native.owner.change(disabled).revision == 2
    assert native.owner.change(fresh).revision == 1
    assert counts(native) == (34, 34, 33)
    assert native.owner.lookup(originals[0]).status == "recorded"


def seed_retained_history(env, first, count):
    """Synthetic valid history, not a claim of performing count native saves.

    Target boundary calls remain real. Every seeded request/proof/record uses
    the frozen canonical domains and actual retained Session/backend identity.
    """
    base = first.model_dump(mode="json")
    changes, records = [], []
    for revision in range(2, count + 1):
        original = copy.deepcopy(base)
        original.update(expected_revision=revision - 1, idempotency_key=f"seed-{revision}")
        proof = {
            "schema_version": "1.0",
            "status": "responsibility_saved",
            "backend_id": original["expected_backend"],
            "service_instance": original["expected_instance"],
            "revision": revision,
            "request": original,
        }
        digest_body = {"responsibility_id": FIRST, "revision": revision, "request": original}
        proof["authorization_digest"] = hashlib.sha256(
            b"kinaiya.standing.authorization.v1\0" + encoded(digest_body)
        ).hexdigest()
        body = {"receipt": proof, "activation": "pending", "authorized_from_utc": None}
        retained = {
            "schema_version": "1.0",
            **body,
            "record_digest": hashlib.sha256(
                b"kinaiya.standing.record.v1\0" + encoded(body)
            ).hexdigest(),
        }
        changes.append(
            (
                original["idempotency_key"],
                hashlib.sha256(encoded(original)).hexdigest(),
                encoded(original).decode(),
                encoded(proof).decode(),
                FIRST,
                revision,
            )
        )
        records.append((FIRST, revision, encoded(retained).decode()))
    # Independently validated end specimens ensure fixture formulas/identities
    # close before filling the actual, unreduced 10,000-row boundary.
    StandingRecord.model_validate(standing_json(records[0][2].encode()))
    StandingRecord.model_validate(standing_json(records[-1][2].encode()))
    with env.store.lock, env.store.db:
        # Defer fixture insertion order only; SQLite still checks the complete
        # actual history at COMMIT, regardless of legal table FK direction.
        env.store.db.execute("PRAGMA defer_foreign_keys=ON")
        env.store.db.executemany(
            "INSERT INTO standing_owner_changes(key,digest,request,receipt,responsibility_id,revision) VALUES(?,?,?,?,?,?)",
            changes,
        )
        env.store.db.executemany(
            "INSERT INTO standing_authorizations(responsibility_id,revision,record) VALUES(?,?,?)",
            records,
        )
        env.store.db.execute(
            "UPDATE standing_owner_current SET revision=? WHERE responsibility_id=?", (count, FIRST)
        )
    assert env.store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_actual_10000_row_boundary_reserves_a_disable_then_preserves_exact_history(native):
    first = proposal(native)
    saved = native.owner.change(first)
    seed_retained_history(native, first, 9998)
    assert counts(native) == (9998, 9998, 1)
    penultimate = proposal(native, key="row-9999", revision=9998)
    assert native.owner.change(penultimate).revision == 9999
    with pytest.raises(StandingUnavailable):
        native.owner.change(proposal(native, key="enabled-row-10000", revision=9999))
    disabled = proposal(native, key="reserved-disable", revision=9999, enabled=False)
    assert native.owner.change(disabled).revision == 10000
    assert counts(native) == (10000, 10000, 1)
    assert native.owner.snapshot(FIRST).record.receipt.request.authorization.enabled is False
    with pytest.raises(StandingUnavailable):
        native.owner.change(proposal(native, key="row-10001", revision=10000, enabled=False))
    assert native.owner.change(first) == saved
    assert native.owner.lookup(first).receipt == saved
    assert counts(native) == (10000, 10000, 1)


def test_pages_are_stable_bounded_whole_records_and_large_lookup_keeps_only_two_copies(native):
    originals = []
    for number in range(1, 21):
        original = proposal(
            native, key=f"page-{number}", responsibility=f"00000000-0000-0000-0000-{number:012x}"
        )
        if number <= 3:
            raw = original.model_dump(mode="json")
            raw["authorization"]["objective"] = "\0" * 16000
            original = StandingChange.model_validate(raw)
        originals.append(original)
        native.owner.change(original)
    before = database_rows(native.store.db)
    seen, cursor = [], None
    for _ in range(21):
        result = native.owner.page(after=cursor, limit=20)
        assert len(canonical_bytes(result)) <= 262144
        assert len(result.items) <= 20
        ids = [item.receipt.request.responsibility_id for item in result.items]
        assert ids == sorted(ids) and not set(seen) & set(ids)
        seen.extend(ids)
        if result.next_cursor is None:
            break
        assert ids and result.next_cursor == ids[-1] and result.next_cursor != cursor
        cursor = result.next_cursor
    else:
        pytest.fail("Native keyset page did not terminate")
    assert seen == [request.responsibility_id for request in originals]
    positive = native.owner.lookup(originals[0])
    assert len(canonical_bytes(positive)) > 131072
    assert "record" not in positive.current.model_dump()
    assert positive.receipt.validate_request(originals[0])
    assert database_rows(native.store.db) == before


async def test_real_session_routes_save_lookup_detail_page_and_leave_producer_unavailable(
    http_native,
):
    env = http_native
    original = proposal(env)
    response = await post(env, CHANGES, original)
    response_status(response, 200)
    saved = StandingChangeReceipt.from_json(response.content).validate_request(original)
    response = await post(env, LOOKUP, original)
    response_status(response, 200)
    assert StandingChangeLookup.from_json(response.content).receipt == saved
    response = await env.client.get(LIST + "/" + FIRST, headers=headers())
    response_status(response, 200)
    detail = StandingResponsibilitySnapshot.from_json(response.content)
    assert detail.record.activation == "armed" and detail.record.receipt == saved
    response = await env.client.get(LIST, headers=headers())
    response_status(response, 200)
    assert StandingResponsibilityPage.from_json(response.content).items == (detail.record,)
    control = await env.client.get("/v1/agent-control/state", headers=headers())
    assert control.json()["standing_work_available"] is False
    assert env.store.read(kinds=["agent.command"]) == []


@pytest.mark.parametrize(
    ("role", "origin", "status"),
    [(None, None, 401), ("agent", None, 403), ("owner", "http://foreign.test", 403)],
)
async def test_auth_role_and_origin_precede_body_and_datastore_for_all_owner_routes(
    http_native, role, origin, status
):
    env = http_native
    before = database_rows(env.store.db)

    async def unread_body():
        pytest.fail("Unauthorized standing route consumed a body")
        yield b"unreachable"

    extra = {"Origin": origin} if origin else {}
    for method, path in [
        ("POST", CHANGES),
        ("POST", LOOKUP),
        ("GET", LIST),
        ("GET", LIST + "/" + FIRST),
    ]:
        response = await env.client.request(
            method, path, content=unread_body(), headers=headers(role, **extra)
        )
        response_status(response, status)
    assert database_rows(env.store.db) == before


@pytest.mark.parametrize(
    "extra",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ],
)
async def test_owner_posts_reject_non_json_and_compressed_bodies_without_writes(http_native, extra):
    env = http_native
    original = proposal(env)
    for path in [CHANGES, LOOKUP]:
        response = await env.client.post(
            path, content=canonical_bytes(original), headers=headers(**extra)
        )
        response_status(response, 415)
    assert counts(env) == (0, 0, 0)


@pytest.mark.parametrize("fault", ["duplicate", "bool", "oversize", "nodes"])
async def test_http_strict_raw_body_bounds_and_boolean_identity_refuse_before_writes(
    http_native, fault
):
    env = http_native
    original = proposal(env)
    raw = original.model_dump(mode="json")
    if fault == "duplicate":
        body = encoded(raw)[:-1] + b',"idempotency_key":"' + MARKER.encode() + b'"}'
    elif fault == "bool":
        raw["authorization"]["task_session"]["epoch"] = True
        body = encoded(raw)
    elif fault == "oversize":
        body = encoded(raw) + b" " * (131073 - len(encoded(raw)))
    else:
        body = encoded({**raw, "extra": [None] * 2050})
    for path in [CHANGES, LOOKUP]:
        response = await env.client.post(
            path, content=body, headers=headers(**{"Content-Type": "application/json"})
        )
        response_status(response, 422)
    assert counts(env) == (0, 0, 0)


@pytest.mark.parametrize("query", ["limit=0", "limit=21", "after=not-a-uuid"])
async def test_keyset_queries_are_strict_and_refuse_invalid_identifiers(http_native, query):
    response = await http_native.client.get(LIST + "?" + query, headers=headers())
    response_status(response, 422)
    if query.startswith("after="):
        response_status(await http_native.client.get(LIST + "/not-a-uuid", headers=headers()), 422)
    assert counts(http_native) == (0, 0, 0)


async def test_no_store_covers_unknown_standing_paths_and_method_errors(http_native):
    for method, path, expected in [
        ("GET", "/v1/standing/not-an-endpoint", 404),
        ("DELETE", CHANGES, 405),
    ]:
        response = await http_native.client.request(method, path, headers=headers())
        response_status(response, expected)


async def test_five_second_ingress_deadline_is_503_and_settles_body_owner(http_native):
    env = http_native
    consumed, closed = asyncio.Event(), asyncio.Event()

    async def blocked_body():
        try:
            consumed.set()
            yield b"{"
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(
        env.client.post(
            CHANGES, content=blocked_body(), headers=headers(**{"Content-Type": "application/json"})
        )
    )
    try:
        await asyncio.wait_for(consumed.wait(), 2)
        response = await asyncio.wait_for(asyncio.shield(task), 7)
        response_status(response, 503)
        await asyncio.wait_for(closed.wait(), 2)
        async with asyncio.timeout(2):
            while env.operations.active_operations:
                await asyncio.sleep(0.005)
        assert counts(env) == (0, 0, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_total_25_second_timeout_retains_actual_commit_owner_until_settlement(http_native):
    env = http_native
    original = proposal(env)
    connection = controlled_connection(env, original)
    committed, release = commit_gate(connection)
    close_task = None
    task = asyncio.create_task(post(env, CHANGES, original))
    try:
        await entered(committed)
        assert disk_record(env).activation == "pending"
        response = await asyncio.wait_for(asyncio.shield(task), 27)
        response_status(response, 503)
        assert env.operations.active_operations == 1
        close_task = asyncio.create_task(env.operations.close())
        await asyncio.sleep(0)
        assert not close_task.done()
        release.set()
        await close_task
        assert env.operations.active_operations == 0
        assert disk_record(env).activation == "pending"
        assert env.owner.lookup(original).status == "recorded"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if close_task is not None:
            await close_task


async def test_64_actual_operation_cap_and_cancel_close_keep_owned_threads_until_they_settle(
    native, monkeypatch
):
    app = FastAPI()
    operations = install_standing_owner_routes(app, native.owner, AUTH)
    original = proposal(native)
    actual_change = native.owner.change
    started, release = threading.Event(), threading.Event()
    observed_cancel = []
    guard = threading.Lock()

    def blocked_change(request, *, deadline=None, cancelled=None):
        started.set()
        assert release.wait(45), "Owned native call gate was not released"
        with guard:
            observed_cancel.append(cancelled is not None and cancelled())
        return actual_change(request, deadline=deadline, cancelled=cancelled)

    monkeypatch.setattr(native.owner, "change", blocked_change)
    tasks, close_task = [], None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://session.test"
        ) as client:
            body = canonical_bytes(original)
            tasks = [
                asyncio.create_task(
                    client.post(
                        CHANGES,
                        content=body,
                        headers=headers(**{"Content-Type": "application/json"}),
                    )
                )
                for _ in range(64)
            ]
            await entered(started)
            async with asyncio.timeout(5):
                while operations.active_operations != 64:
                    await asyncio.sleep(0.005)
            refused = await asyncio.wait_for(
                client.post(
                    CHANGES, content=body, headers=headers(**{"Content-Type": "application/json"})
                ),
                5,
            )
            response_status(refused, 503)
            assert operations.active_operations == 64
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert operations.active_operations == 64
            close_task = asyncio.create_task(operations.close())
            await asyncio.sleep(0)
            assert not close_task.done() and operations.active_operations == 64
            release.set()
            await close_task
            assert operations.active_operations == 0
            assert len(observed_cancel) == 64 and all(observed_cancel)
            assert counts(native) == (0, 0, 0)
            response_status(
                await client.post(
                    CHANGES, content=body, headers=headers(**{"Content-Type": "application/json"})
                ),
                503,
            )
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if close_task is not None:
            await close_task
        await operations.close()
