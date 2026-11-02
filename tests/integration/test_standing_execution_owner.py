"""Independent #228 native prepare/read oracles, frozen before source read.

Actual protected SQLite and the existing owner/control/dispatch public APIs are
used. This phase has no task admission, worker, inbox, settlement or release.
"""

import importlib
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import pytest
from test_standing_dispatch_owner import SqlBoundary, database_state, seed_generations

from packages.contracts.agent_control import AgentModeChange
from packages.contracts.interfaces import AgentControlUnavailable
from packages.contracts.models import SessionRequest
from packages.contracts.standing_dispatch import (
    StandingDispatchActivation,
    StandingDispatchStop,
    StandingProducerRenewal,
)
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionIntent,
    StandingOccurrence,
    capability_digest,
    claim_digest,
    claim_id,
    intent_digest,
    occurrence_id,
    standing_initial_request,
)
from packages.contracts.standing_work import (
    StandingChange,
    StandingConflict,
    StandingUnavailable,
    latest_relevant,
)
from services.session.agent_control import AgentControl
from services.session.standing_dispatch import StandingDispatchOwner
from services.session.standing_work import StandingOwner
from services.session.store import Store

CLAIM_TABLES = {"standing_execution_meta", "standing_execution_claims"}
RESPONSIBILITY = "22800000-0000-4000-8000-000000000001"
MARKER = "SYNTHETIC-PRIVATE-LEDGER-FAULT"
SECRET = bytes(range(32))  # Public synthetic fixture, never real authorization.


def uid(number):
    return str(uuid.UUID(int=228_000_000 + number))


def native_class():
    try:
        module = importlib.import_module("services.session.standing_execution")
    except ModuleNotFoundError as error:
        if error.name != "services.session.standing_execution":
            raise
        pytest.fail("Native standing execution owner capability is absent", pytrace=False)
    return module.StandingExecutionOwner


class WallClock:
    def __init__(self, value):
        self.value, self.calls = value, 0
        self.forbidden = False
        self.fail_after = None

    def __call__(self):
        self.calls += 1
        if self.forbidden:
            raise AssertionError("Historical claim sampled trusted UTC")
        if self.fail_after is not None and self.calls > self.fail_after:
            return self.value.replace(tzinfo=None)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class ProducerClock:
    def __init__(self, value=1000):
        self.value, self.calls = value, 0
        self.forbidden = False

    def __call__(self):
        self.calls += 1
        if self.forbidden:
            raise AssertionError("Historical claim sampled producer liveness")
        return self.value


def utc_text(value):
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mode_request(control, mode, key):
    state = control.snapshot()
    return AgentModeChange(
        schema_version="1.0",
        idempotency_key=key,
        expected_backend=state.backend_id,
        expected_instance=state.instance_id,
        expected_revision=state.revision,
        mode=mode,
        active_work_policy="finish_already_accepted",
    )


def activate(store, control, dispatch, *, suffix="1"):
    state = dispatch.snapshot()
    producer = dispatch.renew(
        StandingProducerRenewal(
            schema_version="1.0",
            expected_backend=state.backend_id,
            expected_instance=control.instance,
            producer_id=str(uuid.uuid4()),
            sequence=1,
        )
    )
    request = StandingDispatchActivation(
        schema_version="1.0",
        mode_change=mode_request(control, "autonomous", f"claim-activate:{suffix}"),
        expected_epoch=state.epoch,
        expected_fence_token=state.fence_token,
    )
    receipt = dispatch.activate(request).validate_request(request)
    assert dispatch.snapshot().dispatch_ready
    assert not store.db.in_transaction
    return producer, receipt


@dataclass
class Native:
    store: Store
    control: AgentControl
    owner: StandingOwner
    dispatch: StandingDispatchOwner
    wall: WallClock
    mono: ProducerClock
    session: object
    record: object
    producer: object
    activation: object
    due: StandingOccurrence
    ledgers: list = field(default_factory=list)

    def ledger(self, *, register=True, control=None, owner=None, dispatch=None):
        value = native_class()(
            self.store,
            self.control if control is None else control,
            self.owner if owner is None else owner,
            self.dispatch if dispatch is None else dispatch,
            clock=self.wall,
        )
        self.ledgers.append(value)
        if register:
            value.register()
        return value

    def request(self, *, key="standing-claim:1", task_id=None, record=None, due=None):
        record = self.record if record is None else record
        original = record.receipt.request
        authorization = original.authorization
        backend = self.activation.mode_receipt.backend_id
        executor = uid(333)
        return StandingExecutionIntent.model_validate(
            {
                "schema_version": "1.0",
                "idempotency_key": key,
                "expected_backend": backend,
                "expected_instance": self.producer.request.expected_instance,
                "responsibility_id": original.responsibility_id,
                "authorization_revision": record.receipt.revision,
                "authorization_digest": record.receipt.authorization_digest,
                "record_digest": record.record_digest,
                "occurrence": self.due if due is None else due,
                "dispatch_epoch": self.activation.epoch,
                "activation_digest": self.activation.activation_digest,
                "control_revision": self.activation.mode_receipt.revision,
                "producer": self.producer,
                "executor_id": executor,
                "capability_sha256": capability_digest(backend, key, executor, SECRET),
                "task": {
                    "schema_version": "1.0",
                    "task_id": uid(666) if task_id is None else task_id,
                    "task_session": authorization.task_session,
                    "objective": authorization.objective,
                    "content_scope": authorization.content_scope,
                    "workspace_pin": authorization.workspace_pin,
                    "harness_version": "seed-v1",
                    "model_pin": {
                        "schema_version": "1.0",
                        "profile_id": "synthetic-profile",
                        "provider": "synthetic-no-inference",
                        "model": "synthetic-model",
                        "local": True,
                        "reasoning": None,
                        "settings_revision": 0,
                        "catalog_binding": None,
                        "catalog_revision": None,
                    },
                    "tool_subset": "native-files-v1",
                },
            }
        )

    def save(self, *, responsibility=None, **changes):
        record = self.record
        raw = record.receipt.request.model_dump(mode="json")
        raw["expected_instance"] = self.control.instance
        raw["expected_revision"] = record.receipt.revision
        if responsibility is not None:
            raw["responsibility_id"] = responsibility
            raw["expected_revision"] = 0
        raw["idempotency_key"] = (
            f"claim-grant:{raw['responsibility_id']}:{raw['expected_revision'] + 1}"
        )
        raw["authorization"].update(changes)
        request = StandingChange.model_validate(raw)
        self.owner.change(request)
        saved = self.owner.snapshot(raw["responsibility_id"]).record
        if responsibility is None:
            self.record = saved
        return saved


@pytest.fixture
def build_native(tmp_path):
    bundles = []

    def build(
        *,
        timezone_name="UTC",
        local_time="12:00",
        day="2026-11-01",
        arm=None,
        now=None,
        due=None,
        grace=60,
        expiry=None,
        pending=False,
        quiet=False,
        objective="Write one bounded synthetic report.",
    ):
        path = tmp_path / f"protected-{len(bundles)}.sqlite"
        store = Store(path)
        control = AgentControl(store)
        control.register()
        wall = WallClock(arm or datetime(2026, 11, 1, 11, 59, 50, tzinfo=UTC))
        mono = ProducerClock()
        owner = StandingOwner(store, control, clock=wall)
        owner.register()
        dispatch = StandingDispatchOwner(store, control, monotonic_ms=mono)
        dispatch.register()
        session = store.create_session(SessionRequest(channel="agent"))
        state = control.snapshot()
        raw = {
            "schema_version": "1.0",
            "idempotency_key": "claim-initial-grant",
            "expected_backend": state.backend_id,
            "expected_instance": state.instance_id,
            "expected_revision": 0,
            "responsibility_id": RESPONSIBILITY,
            "authorization": {
                "label": "Synthetic native standing claim",
                "objective": objective,
                "content_scope": "local_only",
                "enabled": True,
                "task_session": {
                    "session_id": session.session_id,
                    "thread_id": session.thread_id,
                    "epoch": session.epoch,
                },
                "workspace_pin": {
                    "schema_version": "1.0",
                    "workspace_id": "a" * 64,
                    "root_identity": "b" * 64,
                    "settings_revision": 0,
                    "source": "host",
                    "shell_backend": "bubblewrap",
                    "mode": "direct",
                },
                "schedule": {
                    "kind": "daily_local",
                    "timezone": timezone_name,
                    "local_time": local_time,
                    "fold_policy": "earlier",
                    "gap_policy": "skip",
                    "grace_seconds": grace,
                    "missed_run_policy": "latest_within_grace",
                    "start_local_date": day,
                    "valid_until_utc": expiry or "2026-11-05T00:00:00Z",
                },
                "delivery": {
                    "kind": "in_app",
                    "attention": "routine",
                    "preview": "status_only",
                    "quiet_hours": {
                        "timezone": "UTC",
                        "start_local_time": "00:00",
                        "end_local_time": "23:59",
                    }
                    if quiet
                    else None,
                },
                "overlap_policy": "one_unresolved_per_authorization",
                "maximum_runs": 1,
                "inference_authorization": "configured_agent_profile",
            },
        }
        if pending:
            wall.fail_after = 1
        owner.change(StandingChange.model_validate(raw))
        wall.fail_after = None
        record = owner.snapshot(RESPONSIBILITY).record
        assert record.activation == ("pending" if pending else "armed")
        producer, activation = activate(store, control, dispatch)
        wall.value = now or datetime(2026, 11, 1, 12, 0, 10, tzinfo=UTC)
        occurrence = StandingOccurrence.model_validate(
            due
            or {
                "schema_version": "1.0",
                "local_date": day,
                "due_utc": f"{day}T12:00:00Z",
                "utc_offset_seconds": 0,
            }
        )
        value = Native(
            store,
            control,
            owner,
            dispatch,
            wall,
            mono,
            session,
            record,
            producer,
            activation,
            occurrence,
        )
        bundles.append(value)
        return value

    yield build
    for bundle in reversed(bundles):
        for ledger in bundle.ledgers:
            ledger.close()
        bundle.owner.close()
        bundle.dispatch.close()
        try:
            bundle.store.close()
        except sqlite3.ProgrammingError:
            pass  # A poison assertion already proved actual connection closure.


@pytest.fixture
def native(build_native):
    return build_native()


def claim_rows(connection):
    return [
        tuple(row)
        for row in connection.execute("SELECT * FROM standing_execution_claims ORDER BY key")
    ]


def no_task(store):
    assert store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 0


def expect_unchanged(native, error, invoke):
    before = database_state(native.store.db)
    with pytest.raises(error) as refused:
        invoke()
    assert MARKER not in str(refused.value)
    assert database_state(native.store.db) == before
    assert not native.store.db.in_transaction
    no_task(native.store)


def make_claim(request, prepared="2026-11-01T12:00:10Z"):
    return StandingExecutionClaim(
        schema_version="1.0",
        request=request,
        claim_id=claim_id(request),
        occurrence_id=occurrence_id(request),
        intent_digest=intent_digest(request),
        prepared_utc=prepared,
        claim_digest=claim_digest(request, prepared),
    )


def replace_claim(store, proof):
    request = proof.request
    with store.db:
        store.db.execute("DELETE FROM standing_execution_claims")
        store.db.execute(
            "INSERT INTO standing_execution_claims VALUES(?,?,?,?,?,?)",
            (
                request.idempotency_key,
                proof.claim_id,
                proof.occurrence_id,
                request.responsibility_id,
                request.authorization_revision,
                proof.canonical_bytes().decode(),
            ),
        )
    assert len(claim_rows(store.db)) == 1


@pytest.mark.parametrize("fold", [False, True], ids=["utc", "earlier-fold"])
def test_existing_fixture_has_actual_armed_owner_and_live_current_dispatch_without_new_source(
    build_native, fold
):
    if fold:
        value = build_native(
            timezone_name="America/Los_Angeles",
            local_time="01:30",
            arm=datetime(2026, 11, 1, 8, 0, tzinfo=UTC),
            now=datetime(2026, 11, 1, 8, 30, 10, tzinfo=UTC),
            due={
                "schema_version": "1.0",
                "local_date": "2026-11-01",
                "due_utc": "2026-11-01T08:30:00Z",
                "utc_offset_seconds": -25200,
            },
        )
    else:
        value = build_native()
    request = value.request()
    selected = latest_relevant(
        value.record.receipt.request.authorization.schedule,
        value.record.authorized_from_utc,
        utc_text(value.wall.value),
    )
    assert selected is not None
    assert selected.local_date == request.occurrence.local_date
    assert selected.due_utc == request.occurrence.due_utc
    assert value.record.activation == "armed"
    assert value.control.metadata()["instance_id"] == request.expected_instance
    assert value.dispatch.snapshot().dispatch_ready
    assert StandingExecutionIntent.from_json(request.canonical_bytes()) == request
    assert value.producer == request.producer
    assert CLAIM_TABLES.isdisjoint(
        {
            row[0]
            for row in value.store.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
    )
    no_task(value.store)


def test_construction_and_close_are_passive_and_register_bootstraps_only_the_exact_ledger(native):
    kind = native_class()
    before = database_state(native.store.db)
    clocks = native.wall.calls, native.mono.calls
    traces = []
    native.store.db.set_trace_callback(traces.append)
    passive = kind(native.store, native.control, native.owner, native.dispatch, clock=native.wall)
    passive.close()
    native.store.db.set_trace_callback(None)
    assert traces == []
    assert (native.wall.calls, native.mono.calls) == clocks
    assert database_state(native.store.db) == before
    ledger = native.ledger(register=False)
    expect_unchanged(native, StandingUnavailable, lambda: ledger.lookup(native.request()))
    ledger.register()
    after = database_state(native.store.db)
    assert set(after[1]) - set(before[1]) == CLAIM_TABLES
    assert all(after[1][name] == rows for name, rows in before[1].items())
    assert tuple(native.store.db.execute("SELECT * FROM standing_execution_meta").fetchone()) == (
        1,
        1,
        native.request().expected_backend,
    )
    assert claim_rows(native.store.db) == []
    for table in CLAIM_TABLES:
        assert list(native.store.db.execute(f"PRAGMA foreign_key_list({table})")) == []
    fields = [
        tuple(row)[1:3]
        for row in native.store.db.execute("PRAGMA table_info(standing_execution_claims)")
    ]
    assert fields == [
        ("key", "TEXT"),
        ("claim_id", "TEXT"),
        ("occurrence_id", "TEXT"),
        ("responsibility_id", "TEXT"),
        ("authorization_revision", "INTEGER"),
        ("claim", "TEXT"),
    ]
    unique = set()
    for index in native.store.db.execute("PRAGMA index_list(standing_execution_claims)"):
        if index[2]:
            quoted = '"' + index[1].replace('"', '""') + '"'
            unique.add(
                tuple(row[2] for row in native.store.db.execute(f"PRAGMA index_info({quoted})"))
            )
    assert {("key",), ("claim_id",), ("occurrence_id",)} <= unique
    assert ledger.lookup(native.request()) is None
    assert ledger.unresolved() is None
    expect_unchanged(native, StandingConflict, ledger.register)
    ledger.close()
    expect_unchanged(native, StandingUnavailable, ledger.unresolved)
    assert native.control.metadata()["instance_id"] == native.control.instance
    assert native.dispatch.snapshot().dispatch_ready


def test_incompatible_closed_failed_and_unregistered_collaborators_cannot_bootstrap(
    native, build_native
):
    kind = native_class()
    other = build_native()
    passive_control = AgentControl(native.store)
    passive_owner = StandingOwner(native.store, native.control, clock=native.wall)
    closed_owner = StandingOwner(native.store, native.control, clock=native.wall)
    closed_owner.register()
    closed_owner.close()
    closed_dispatch = StandingDispatchOwner(native.store, native.control, monotonic_ms=native.mono)
    closed_dispatch.register()
    closed_dispatch.close()
    failed_owner = StandingOwner(native.store, native.control, clock=native.wall)
    with native.store.db:
        native.store.db.execute("UPDATE standing_owner_meta SET version=2")
    with pytest.raises(StandingUnavailable):
        failed_owner.register()
    with native.store.db:
        native.store.db.execute("UPDATE standing_owner_meta SET version=1")
    other_before = database_state(other.store.db)
    for control, owner, dispatch in (
        (passive_control, native.owner, native.dispatch),
        (native.control, other.owner, native.dispatch),
        (native.control, native.owner, other.dispatch),
        (native.control, passive_owner, native.dispatch),
        (native.control, closed_owner, native.dispatch),
        (native.control, failed_owner, native.dispatch),
        (native.control, native.owner, closed_dispatch),
    ):
        candidates = []

        def reject_binding(control=control, owner=owner, dispatch=dispatch, candidates=candidates):
            candidate = kind(native.store, control, owner, dispatch, clock=native.wall)
            candidates.append(candidate)
            candidate.register()

        expect_unchanged(native, StandingUnavailable, reject_binding)
        if candidates:
            expect_unchanged(native, StandingUnavailable, candidates[0].unresolved)
            candidates[0].close()
    assert database_state(other.store.db) == other_before
    assert CLAIM_TABLES.isdisjoint(
        {
            row[0]
            for row in native.store.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
    )


def test_partial_schema_failed_register_never_repairs_or_reuses_the_failed_capability(native):
    with native.store.db:
        native.store.db.execute(
            "CREATE TABLE standing_execution_meta(id INTEGER PRIMARY KEY,version INTEGER NOT NULL,backend_id TEXT NOT NULL)"
        )
    ledger = native.ledger(register=False)
    expect_unchanged(native, StandingUnavailable, ledger.register)
    with native.store.db:
        native.store.db.execute("DROP TABLE standing_execution_meta")
    expect_unchanged(native, StandingUnavailable, ledger.register)
    expect_unchanged(native, StandingUnavailable, lambda: ledger.prepare(native.request()))
    assert native.ledger().unresolved() is None


def test_prepare_commits_full_claim_only_and_all_exact_readback_is_passive(native):
    ledger = native.ledger()
    request = native.request()
    native.wall.value = native.wall.value.replace(microsecond=987654)
    before = database_state(native.store.db)
    result = ledger.prepare(request).validate_request(request)
    assert result.disposition == "created"
    proof = result.claim
    assert proof.prepared_utc == "2026-11-01T12:00:10Z"
    assert proof.validate_request(request) == proof
    assert claim_rows(native.store.db) == [
        (
            request.idempotency_key,
            proof.claim_id,
            proof.occurrence_id,
            RESPONSIBILITY,
            1,
            proof.canonical_bytes().decode(),
        )
    ]
    after = database_state(native.store.db)
    assert all(
        rows == after[1][name]
        for name, rows in before[1].items()
        if name != "standing_execution_claims"
    )
    native.wall.forbidden = native.mono.forbidden = True
    clocks = native.wall.calls, native.mono.calls
    assert ledger.lookup(request) == proof
    assert ledger.unresolved() == proof
    replay = ledger.prepare(request).validate_request(request)
    assert replay.disposition == "retained" and replay.claim == proof
    assert (native.wall.calls, native.mono.calls) == clocks
    assert database_state(native.store.db) == after
    no_task(native.store)


def test_lookup_absence_requires_known_backend_and_issued_instance_without_sampling_clocks(native):
    ledger = native.ledger()
    request = native.request()
    native.wall.forbidden = native.mono.forbidden = True
    before = database_state(native.store.db)
    assert ledger.lookup(request) is None
    raw = request.model_dump(mode="json")
    raw["expected_backend"] = raw["producer"]["request"]["expected_backend"] = "f" * 32
    expect_unchanged(
        native, StandingConflict, lambda: ledger.lookup(StandingExecutionIntent.model_validate(raw))
    )
    raw = request.model_dump(mode="json")
    raw["expected_instance"] = raw["producer"]["request"]["expected_instance"] = uid(99999)
    expect_unchanged(
        native, StandingConflict, lambda: ledger.lookup(StandingExecutionIntent.model_validate(raw))
    )
    assert database_state(native.store.db) == before


def test_retained_original_key_body_conflict_precedes_off_or_clock_failure(native):
    ledger = native.ledger()
    request = native.request()
    proof = ledger.prepare(request).claim
    native.save(enabled=False)
    state = native.dispatch.snapshot()
    native.dispatch.stop(
        StandingDispatchStop(
            schema_version="1.0",
            expected_backend=state.backend_id,
            expected_instance=state.instance_id,
            issued_epoch=state.epoch,
            activation_digest=state.activation_digest,
            expected_fence_token=state.fence_token,
        )
    )
    native.control.change(mode_request(native.control, "off", "after-claim:off"))
    native.mono.value = request.producer.expires_monotonic_ms
    native.wall.value = datetime(2026, 1, 1, tzinfo=UTC)
    native.wall.forbidden = native.mono.forbidden = True
    raw = request.model_dump(mode="json")
    raw["task"]["task_id"] = uid(667)
    different = StandingExecutionIntent.model_validate(raw)
    for operation in (ledger.prepare, ledger.lookup):
        expect_unchanged(native, StandingConflict, lambda operation=operation: operation(different))
    assert ledger.lookup(request) == ledger.unresolved() == proof
    assert ledger.prepare(request).disposition == "retained"


def test_new_claim_matches_the_whole_current_grant_gate_and_native_session(native):
    ledger = native.ledger()
    request = native.request()
    changes = (
        (("authorization_revision",), 2),
        (("authorization_digest",), "c" * 64),
        (("record_digest",), "d" * 64),
        (("dispatch_epoch",), request.dispatch_epoch + 1),
        (("activation_digest",), "e" * 64),
        (("control_revision",), request.control_revision + 1),
        (("task", "objective"), "Caller supplied another objective"),
        (("task", "content_scope"), "public_synthetic"),
        (("task", "workspace_pin", "root_identity"), "f" * 64),
        (("task", "task_session", "epoch"), request.task.task_session.epoch + 1),
        (("task", "task_session", "session_id"), "another-session"),
        (("task", "task_session", "thread_id"), "another-thread"),
    )
    for path, value in changes:
        raw = request.model_dump(mode="json")
        target = raw
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
        altered = StandingExecutionIntent.model_validate(raw)
        expect_unchanged(native, StandingConflict, lambda altered=altered: ledger.prepare(altered))
    assert ledger.unresolved() is None
    assert ledger.prepare(request).disposition == "created"


@pytest.mark.parametrize(
    "fence", ["off", "on_demand", "stop", "lease_expiry", "new_controller", "disabled", "pending"]
)
def test_new_reservation_requires_actual_current_enabled_armed_autonomous_authority(
    build_native, fence
):
    value = build_native(pending=fence == "pending")
    ledger = value.ledger()
    request = value.request()
    if fence in {"off", "on_demand"}:
        value.control.change(mode_request(value.control, fence, f"fence:{fence}"))
    elif fence == "stop":
        state = value.dispatch.snapshot()
        value.dispatch.stop(
            StandingDispatchStop(
                schema_version="1.0",
                expected_backend=state.backend_id,
                expected_instance=state.instance_id,
                issued_epoch=state.epoch,
                activation_digest=state.activation_digest,
                expected_fence_token=state.fence_token,
            )
        )
    elif fence == "lease_expiry":
        value.mono.value = value.producer.expires_monotonic_ms
    elif fence == "new_controller":
        replacement = AgentControl(value.store)
        replacement.register()
        assert value.control.metadata()["instance_id"] != request.expected_instance
    elif fence == "disabled":
        value.save(enabled=False)
        request = value.request()
    expect_unchanged(value, StandingConflict, lambda: ledger.prepare(request))
    assert ledger.unresolved() is None


@pytest.mark.parametrize(
    "case",
    [
        "grace_equality",
        "past_grace",
        "expiry_equality",
        "pre_arm",
        "earlier_not_latest",
        "wrong_offset",
        "fold_earlier",
        "fold_later",
        "gap_skip",
        "quiet",
    ],
)
def test_native_schedule_selects_latest_due_exact_offset_and_never_defers_claim_for_quiet(
    build_native, case
):
    options = {}
    created = case in {"grace_equality", "fold_earlier", "quiet"}
    if case == "grace_equality":
        options["now"] = datetime(2026, 11, 1, 12, 1, tzinfo=UTC)
    elif case == "past_grace":
        options["now"] = datetime(2026, 11, 1, 12, 1, 1, tzinfo=UTC)
    elif case == "expiry_equality":
        options.update(
            now=datetime(2026, 11, 1, 12, 0, 30, tzinfo=UTC), expiry="2026-11-01T12:00:30Z"
        )
    elif case == "pre_arm":
        options["arm"] = datetime(2026, 11, 1, 12, 0, 1, tzinfo=UTC)
    elif case == "earlier_not_latest":
        options.update(now=datetime(2026, 11, 2, 12, tzinfo=UTC), grace=86400)
    elif case in {"fold_earlier", "fold_later"}:
        later = case == "fold_later"
        options.update(
            timezone_name="America/Los_Angeles",
            local_time="01:30",
            grace=7200,
            arm=datetime(2026, 11, 1, 8, tzinfo=UTC),
            now=datetime(2026, 11, 1, 9 if later else 8, 30, 10, tzinfo=UTC),
            due={
                "schema_version": "1.0",
                "local_date": "2026-11-01",
                "due_utc": "2026-11-01T09:30:00Z" if later else "2026-11-01T08:30:00Z",
                "utc_offset_seconds": -28800 if later else -25200,
            },
        )
    elif case == "gap_skip":
        options.update(
            timezone_name="America/Los_Angeles",
            local_time="02:30",
            day="2026-03-08",
            arm=datetime(2026, 3, 8, 9, tzinfo=UTC),
            now=datetime(2026, 3, 8, 10, 30, 10, tzinfo=UTC),
            expiry="2026-03-12T00:00:00Z",
            due={
                "schema_version": "1.0",
                "local_date": "2026-03-08",
                "due_utc": "2026-03-08T10:30:00Z",
                "utc_offset_seconds": -25200,
            },
        )
    elif case == "quiet":
        options["quiet"] = True
    value = build_native(**options)
    ledger = value.ledger()
    request = value.request()
    if case == "wrong_offset":
        raw = request.model_dump(mode="json")
        raw["occurrence"]["utc_offset_seconds"] = 1
        request = StandingExecutionIntent.model_validate(raw)
    if created:
        result = ledger.prepare(request).validate_request(request)
        assert result.disposition == "created"
        assert result.claim.prepared_utc == utc_text(value.wall.value)
        assert ledger.unresolved() == result.claim
        no_task(value.store)
    else:
        expect_unchanged(value, StandingConflict, lambda: ledger.prepare(request))
        assert ledger.unresolved() is None


@pytest.mark.parametrize(
    "clock_value",
    [
        datetime(2026, 11, 1, 12),
        datetime(2026, 11, 1, 13, tzinfo=timezone(timedelta(hours=1))),
        RuntimeError(MARKER),
    ],
    ids=["naive", "non_utc", "raised"],
)
def test_untrusted_utc_clock_is_unavailable_and_never_writes(native, clock_value):
    ledger = native.ledger()
    request = native.request()
    native.wall.value = clock_value
    expect_unchanged(native, StandingUnavailable, lambda: ledger.prepare(request))
    assert ledger.unresolved() is None


def test_retained_producer_receipt_survives_higher_renewal_but_same_sequence_must_match(native):
    ledger = native.ledger()
    request = native.request()
    proof = ledger.prepare(request).claim
    current = native.producer.request
    next_renewal = current.model_copy(update={"sequence": current.sequence + 1})
    renewed = native.dispatch.renew(next_renewal)
    assert renewed.request.sequence == request.producer.request.sequence + 1
    assert renewed.lease_token != request.producer.lease_token
    native.wall.forbidden = native.mono.forbidden = True
    before = database_state(native.store.db)
    assert ledger.lookup(request) == ledger.unresolved() == proof
    assert ledger.prepare(request).disposition == "retained"
    assert database_state(native.store.db) == before
    # Restoring a valid same-sequence row with a different receipt must not
    # turn the original saved claim into genuine issued producer proof.
    different = request.producer.model_copy(update={"lease_token": uid(919)})
    with native.store.db:
        native.store.db.execute(
            "UPDATE standing_dispatch_producers SET sequence=?,receipt=? WHERE producer_id=?",
            (current.sequence, different.canonical_bytes().decode(), current.producer_id),
        )
    expect_unchanged(native, StandingUnavailable, lambda: ledger.lookup(request))


@pytest.mark.parametrize("change", ["revision", "responsibility"])
def test_one_unresolved_reservation_survives_all_revisions_and_other_responsibilities(
    native, change
):
    ledger = native.ledger()
    first_request = native.request()
    first = ledger.prepare(first_request).claim
    native.wall.value = datetime(2026, 11, 2, 11, 59, 50, tzinfo=UTC)
    saved = native.save(
        responsibility=uid(222) if change == "responsibility" else None,
        label="A later enabled finite grant",
    )
    native.wall.value = datetime(2026, 11, 2, 12, 0, 10, tzinfo=UTC)
    due = StandingOccurrence(
        schema_version="1.0",
        local_date="2026-11-02",
        due_utc="2026-11-02T12:00:00Z",
        utc_offset_seconds=0,
    )
    fresh = native.request(key="standing-claim:new-grant", task_id=uid(667), record=saved, due=due)
    selected = latest_relevant(
        saved.receipt.request.authorization.schedule,
        saved.authorized_from_utc,
        utc_text(native.wall.value),
    )
    assert selected is not None and selected.due_utc == due.due_utc
    assert fresh.authorization_revision == (1 if change == "responsibility" else 2)
    expect_unchanged(native, StandingConflict, lambda: ledger.prepare(fresh))
    assert ledger.lookup(first_request) == ledger.unresolved() == first
    assert ledger.lookup(fresh) is None
    assert len(claim_rows(native.store.db)) == 1


@pytest.mark.parametrize("action", ["reset", "delete"])
def test_historical_claim_survives_native_session_reset_or_delete_without_cascading(native, action):
    ledger = native.ledger()
    request = native.request()
    proof = ledger.prepare(request).claim
    if action == "reset":
        assert native.store.reset(native.session.session_id).epoch == native.session.epoch + 1
    else:
        native.store.delete(native.session.session_id)
        assert (
            native.store.db.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (native.session.session_id,)
            ).fetchone()
            is None
        )
    native.wall.forbidden = native.mono.forbidden = True
    before = database_state(native.store.db)
    assert ledger.lookup(request) == ledger.unresolved() == proof
    assert ledger.prepare(request).disposition == "retained"
    assert database_state(native.store.db) == before


def test_actual_reopen_new_controller_preserves_original_claim_and_only_local_close(native):
    ledger = native.ledger()
    request = native.request()
    proof = ledger.prepare(request).claim
    ledger.close()
    native.control.close()
    native.store.close()
    store = Store(native.store.path)
    control = AgentControl(store)
    owner = StandingOwner(store, control, clock=native.wall)
    dispatch = StandingDispatchOwner(store, control, monotonic_ms=native.mono)
    peer = None
    try:
        control.register()
        owner.register()
        dispatch.register()
        assert control.instance != request.expected_instance
        before = database_state(store.db)
        native.wall.forbidden = native.mono.forbidden = True
        peer = native_class()(store, control, owner, dispatch, clock=native.wall)
        peer.register()
        assert peer.lookup(request) == peer.unresolved() == proof
        assert peer.prepare(request).disposition == "retained"
        assert database_state(store.db) == before
        peer.close()
        assert control.metadata()["instance_id"] == control.instance
        assert store.db.execute("SELECT count(*) FROM standing_execution_claims").fetchone()[0] == 1
    finally:
        if peer is not None:
            peer.close()
        owner.close()
        dispatch.close()
        store.close()


def test_retained_capacity_history_uses_passive_owner_without_fabricating_null_controller(
    native, monkeypatch
):
    ledger = native.ledger()
    request = native.request()
    proof = ledger.prepare(request).claim
    native.control.close()
    seed_generations(native, total=10000)  # Actual schema seed, not10000 native registrations.
    store = Store(native.store.path)
    control = AgentControl(store)
    passive = StandingOwner(store, control, clock=native.wall)
    dispatch = StandingDispatchOwner(store, control, monotonic_ms=native.mono)
    peer = None
    try:
        with pytest.raises(AgentControlUnavailable):
            control.register()
        assert control.instance is None
        assert control.metadata()["instance_id"] is None
        dispatch.register()
        assert not passive._registered and not passive._closed and not passive._failed

        def forbidden(*args, **kwargs):
            raise AssertionError(
                "Passive historical join called an owner registration/public observation"
            )

        for name in ("register", "snapshot", "lookup", "page", "_available", "_observation"):
            monkeypatch.setattr(passive, name, forbidden)
        before = database_state(store.db)
        native.wall.forbidden = native.mono.forbidden = True
        peer = native_class()(store, control, passive, dispatch, clock=native.wall)
        peer.register()
        assert peer.lookup(request) == peer.unresolved() == proof
        assert peer.prepare(request).disposition == "retained"
        assert not passive._registered
        assert control.instance is None
        assert database_state(store.db) == before
        raw = request.model_dump(mode="json")
        raw["idempotency_key"] = "standing-claim:at-capacity"
        new_request = StandingExecutionIntent.model_validate(raw)
        with pytest.raises((StandingConflict, StandingUnavailable)):
            peer.prepare(new_request)
        assert database_state(store.db) == before
    finally:
        if peer is not None:
            peer.close()
        passive.close()
        dispatch.close()
        store.close()


def test_two_simultaneous_originals_create_one_global_claim_without_winner_substitution(native):
    first = native.ledger()
    second = native.ledger()
    requests = [
        native.request(key=f"standing-race:{index}", task_id=uid(700 + index)) for index in range(2)
    ]
    start = threading.Barrier(2)

    def invoke(index):
        start.wait(timeout=3)
        try:
            return (first if index == 0 else second).prepare(requests[index])
        except StandingConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, index) for index in range(2)]
        results = [future.result(timeout=5) for future in futures]
    winners = [
        index for index, result in enumerate(results) if not isinstance(result, StandingConflict)
    ]
    assert len(winners) == 1
    winner = winners[0]
    proof = results[winner].validate_request(requests[winner]).claim
    assert results[winner].disposition == "created"
    assert first.unresolved() == second.unresolved() == proof
    assert first.lookup(requests[1 - winner]) is None
    assert first.lookup(requests[winner]) == proof
    assert len(claim_rows(native.store.db)) == 1
    no_task(native.store)


def test_replaced_producer_cannot_reserve_using_a_historical_live_looking_packet(native):
    ledger = native.ledger()
    original = native.request()
    native.mono.value = original.producer.expires_monotonic_ms
    renewal = StandingProducerRenewal(
        schema_version="1.0",
        expected_backend=original.expected_backend,
        expected_instance=original.expected_instance,
        producer_id=uid(818),
        sequence=1,
    )
    newer = native.dispatch.renew(renewal)
    assert newer.request.producer_id != original.producer.request.producer_id
    assert native.dispatch.snapshot().dispatch_ready
    expect_unchanged(native, StandingConflict, lambda: ledger.prepare(original))
    native.producer = newer
    request = native.request()
    assert ledger.prepare(request).validate_request(request).disposition == "created"


@pytest.mark.parametrize("missing", ["claim_id", "occurrence_id", "native_column_type"])
def test_declared_native_column_types_and_both_unique_indexes_are_required(native, missing):
    ledger = native.ledger()
    original = native.request()
    ledger.prepare(original)
    connection = native.store.db
    with connection:
        connection.execute("ALTER TABLE standing_execution_claims RENAME TO original_claim_table")
        revision_type = "NUMERIC" if missing == "native_column_type" else "INTEGER"
        claim_unique = "" if missing == "claim_id" else " UNIQUE"
        occurrence_unique = "" if missing == "occurrence_id" else " UNIQUE"
        connection.execute(
            "CREATE TABLE standing_execution_claims(key TEXT PRIMARY KEY,"
            f"claim_id TEXT NOT NULL{claim_unique},occurrence_id TEXT NOT NULL{occurrence_unique},"
            f"responsibility_id TEXT NOT NULL,authorization_revision {revision_type} NOT NULL,claim TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO standing_execution_claims SELECT * FROM original_claim_table"
        )
        connection.execute("DROP TABLE original_claim_table")
    assert (
        connection.execute(
            "SELECT typeof(authorization_revision) FROM standing_execution_claims"
        ).fetchone()[0]
        == "integer"
    )
    expect_unchanged(native, StandingUnavailable, lambda: ledger.lookup(original))


@pytest.mark.parametrize(
    "corruption",
    [
        "blob",
        "noncanonical",
        "duplicate_json",
        "fractional_counter",
        "sql_occurrence",
        "missing_meta",
        "meta_version",
        "meta_backend",
        "two_valid_claims",
    ],
)
def test_stored_scalar_canonical_cross_index_and_reservation_proofs_fail_closed(native, corruption):
    ledger = native.ledger()
    request = native.request()
    proof = ledger.prepare(request).claim
    connection = native.store.db
    with connection:
        if corruption == "blob":
            connection.execute(
                "UPDATE standing_execution_claims SET claim=?", (proof.canonical_bytes(),)
            )
            assert (
                connection.execute(
                    "SELECT typeof(claim) FROM standing_execution_claims"
                ).fetchone()[0]
                == "blob"
            )
        elif corruption == "noncanonical":
            connection.execute(
                "UPDATE standing_execution_claims SET claim=?",
                (json.dumps(proof.model_dump(mode="json"), indent=2),),
            )
        elif corruption == "duplicate_json":
            body = proof.canonical_bytes().decode()
            connection.execute(
                "UPDATE standing_execution_claims SET claim=?",
                ('{"schema_version":"1.0",' + body[1:],),
            )
        elif corruption == "fractional_counter":
            connection.execute("UPDATE standing_execution_claims SET authorization_revision=1.5")
            assert (
                connection.execute(
                    "SELECT typeof(authorization_revision) FROM standing_execution_claims"
                ).fetchone()[0]
                == "real"
            )
        elif corruption == "sql_occurrence":
            connection.execute("UPDATE standing_execution_claims SET occurrence_id=?", ("f" * 64,))
        elif corruption == "missing_meta":
            connection.execute("DELETE FROM standing_execution_meta")
        elif corruption == "meta_version":
            connection.execute("UPDATE standing_execution_meta SET version=2")
        elif corruption == "meta_backend":
            connection.execute("UPDATE standing_execution_meta SET backend_id=?", ("f" * 32,))
        else:
            raw = request.model_dump(mode="json")
            raw["idempotency_key"] = "standing-seeded:second"
            raw["occurrence"].update(local_date="2026-11-02", due_utc="2026-11-02T12:00:00Z")
            raw["task"]["task_id"] = uid(999)
            other = make_claim(StandingExecutionIntent.model_validate(raw), "2026-11-02T12:00:10Z")
            connection.execute(
                "INSERT INTO standing_execution_claims VALUES(?,?,?,?,?,?)",
                (
                    other.request.idempotency_key,
                    other.claim_id,
                    other.occurrence_id,
                    other.request.responsibility_id,
                    other.request.authorization_revision,
                    other.canonical_bytes().decode(),
                ),
            )
            assert len(claim_rows(connection)) == 2
    expect_unchanged(native, StandingUnavailable, lambda: ledger.lookup(request))
    expect_unchanged(native, StandingUnavailable, ledger.unresolved)
    expect_unchanged(native, StandingUnavailable, lambda: ledger.prepare(request))


@pytest.mark.parametrize("corruption", ["objective", "utc_offset", "activation"])
def test_fully_rehashed_claim_still_requires_the_original_native_grant_due_and_activation(
    native, corruption
):
    ledger = native.ledger()
    request = native.request()
    ledger.prepare(request)
    raw = request.model_dump(mode="json")
    if corruption == "objective":
        raw["task"]["objective"] = "Not the owner's authorized objective"
    elif corruption == "utc_offset":
        raw["occurrence"]["utc_offset_seconds"] = 1
    else:
        raw["activation_digest"] = "f" * 64
    altered = StandingExecutionIntent.model_validate(raw)
    forged = make_claim(altered)
    assert StandingExecutionClaim.from_json(forged.canonical_bytes()) == forged
    replace_claim(native.store, forged)
    expect_unchanged(native, StandingUnavailable, lambda: ledger.lookup(altered))
    expect_unchanged(native, StandingUnavailable, ledger.unresolved)


def test_known_initial_serialization_oversize_is_rejected_before_claim_insert(build_native):
    value = build_native(objective="\0" * 10600)
    ledger = value.ledger()
    request = value.request()
    assert len(request.task.canonical_bytes()) <= 65536
    with pytest.raises(ValueError):
        standing_initial_request(make_claim(request))
    expect_unchanged(
        value, (StandingConflict, StandingUnavailable), lambda: ledger.prepare(request)
    )
    assert ledger.unresolved() is None


@pytest.mark.parametrize("phase", ["before", "after"])
def test_real_claim_insert_commit_failure_retains_only_actual_durable_history(native, phase):
    ledger = native.ledger()
    request = native.request()
    connection = native.store.db
    fault = SqlBoundary(connection, phase=phase)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable) as error:
            ledger.prepare(request)
        assert MARKER not in str(error.value)
        assert fault.fired and fault.rollback_fired
        assert fault.at_fault != fault.before
        assert len(fault.at_fault[1]["standing_execution_claims"]) == 1
        assert not connection.in_transaction
        if phase == "before":
            assert not fault.committed
            assert database_state(connection) == fault.before
        else:
            assert fault.committed
            assert len(claim_rows(connection)) == 1
    finally:
        native.store.db = connection
    if phase == "before":
        assert ledger.lookup(request) is None and ledger.unresolved() is None
        assert ledger.prepare(request).disposition == "created"
    else:
        native.wall.forbidden = native.mono.forbidden = True
        before = database_state(connection)
        proof = ledger.lookup(request)
        assert proof.validate_request(request) == proof
        assert ledger.unresolved() == proof
        assert ledger.prepare(request).disposition == "retained"
        assert database_state(connection) == before
    no_task(native.store)


def test_failed_rollback_poison_closes_the_actual_connection_and_never_reports_absence(native):
    ledger = native.ledger()
    request = native.request()
    connection = native.store.db
    fault = SqlBoundary(connection, phase="before", rollback_failure=True)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable):
            ledger.prepare(request)
        assert fault.fired and fault.rollback_fired and fault.closed
        assert len(fault.at_fault[1]["standing_execution_claims"]) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with pytest.raises(StandingUnavailable):
            ledger.lookup(request)
        with pytest.raises(StandingUnavailable):
            ledger.unresolved()
    finally:
        native.store.db = connection
    with sqlite3.connect(native.store.path) as disk:
        assert disk.execute("SELECT count(*) FROM standing_execution_claims").fetchone()[0] == 0


class FinalSqlPulse(SqlBoundary):
    """Signal after real claim DML and the subsequent actual metadata SQL read."""

    def __init__(self, connection, signal):
        super().__init__(connection)
        self.signal = signal
        self.pulsed = False
        self.at_pulse = None

    def execute(self, statement, parameters=()):
        cursor = super().execute(statement, parameters)
        normalized = " ".join(statement.upper().split())
        if (
            self.dirty
            and not self.pulsed
            and normalized.startswith("SELECT")
            and "FROM AGENT_CONTROL_STATE" in normalized
        ):
            self.pulsed = True
            self.at_pulse = database_state(self.connection)
            assert self.connection.in_transaction
            assert len(self.at_pulse[1]["standing_execution_claims"]) == 1
            self.signal()
        return cursor


@pytest.mark.parametrize(
    "signal", ["control", "cancel", "deadline", "lease_expiry", "grant_expiry"]
)
def test_final_native_checks_and_absolute_guard_follow_actual_post_insert_sql(native, signal):
    ledger = native.ledger()
    request = native.request()
    cancelled = threading.Event()
    deadline = time.monotonic() + 2.0

    def pulse():
        if signal == "cancel":
            cancelled.set()
        elif signal == "deadline":
            # This is actual elapsed native time at an observed post-DML SQL
            # boundary, not a patched process-wide clock or a test timeout.
            remaining = deadline - time.monotonic()
            if remaining > 0:
                threading.Event().wait(remaining)
            assert time.monotonic() >= deadline
        elif signal == "lease_expiry":
            native.mono.value = request.producer.expires_monotonic_ms
        elif signal == "grant_expiry":
            native.wall.value = datetime(2026, 11, 5, tzinfo=UTC)

    connection = native.store.db
    proxy = FinalSqlPulse(connection, pulse)
    native.store.db = proxy
    result = failure = None
    try:
        try:
            result = ledger.prepare(request, deadline=deadline, cancelled=cancelled.is_set)
        except (StandingConflict, StandingUnavailable) as error:
            failure = error
        assert proxy.pulsed, "No actual post-insert current metadata check was observed"
        assert proxy.at_pulse != proxy.before
        assert not connection.in_transaction
        if signal == "control":
            assert failure is None
            assert result.validate_request(request).disposition == "created"
            assert len(claim_rows(connection)) == 1
        else:
            assert result is None
            allowed = (
                (StandingConflict, StandingUnavailable)
                if signal in {"lease_expiry", "grant_expiry"}
                else (StandingUnavailable,)
            )
            assert isinstance(failure, allowed)
            assert proxy.rollback_fired
            assert database_state(connection) == proxy.before
    finally:
        native.store.db = connection
    no_task(native.store)


def test_deadline_includes_real_lock_wait_and_invalid_options_never_write(native):
    ledger = native.ledger()
    request = native.request()
    for deadline, cancelled in (
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        (time.monotonic(), None),
        (time.monotonic() + 5, lambda: "false"),
        (time.monotonic() + 5, lambda: True),
    ):
        expect_unchanged(
            native,
            StandingUnavailable,
            lambda deadline=deadline, cancelled=cancelled: ledger.prepare(
                request, deadline=deadline, cancelled=cancelled
            ),
        )
    entered = threading.Event()
    deadline = time.monotonic() + 0.05
    before = database_state(native.store.db)

    def waiting():
        entered.set()
        try:
            ledger.prepare(request, deadline=deadline)
        except StandingUnavailable as error:
            return error
        raise AssertionError("Expired blocked caller created a claim")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with native.store.lock:
            future = pool.submit(waiting)
            assert entered.wait(timeout=2)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                threading.Event().wait(remaining)
            assert time.monotonic() >= deadline
        assert isinstance(future.result(timeout=3), StandingUnavailable)
    assert database_state(native.store.db) == before
    assert ledger.unresolved() is None


def test_actual_two_connection_controller_registration_serializes_after_claim_commit(native):
    ledger = native.ledger()
    request = native.request()
    other_store = Store(native.store.path)
    contender = AgentControl(other_store)
    inserted, registration_started = threading.Event(), threading.Event()
    connection = native.store.db

    def after_write():
        inserted.set()
        assert registration_started.wait(timeout=3)

    def replace_controller():
        assert inserted.wait(timeout=3)
        registration_started.set()
        contender.register()
        return contender.instance

    proxy = SqlBoundary(connection, after_write=after_write)
    native.store.db = proxy
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(replace_controller)
            result = ledger.prepare(request).validate_request(request)
            assert proxy.write_hook_fired
            assert result.disposition == "created"
            replacement = future.result(timeout=5)
        assert replacement is not None and replacement != request.expected_instance
        assert (
            other_store.db.execute("SELECT count(*) FROM standing_execution_claims").fetchone()[0]
            == 1
        )
        assert (
            other_store.db.execute("SELECT instance_id FROM agent_control_state").fetchone()[0]
            == replacement
        )
        assert ledger.lookup(request) == ledger.unresolved() == result.claim
        assert len(claim_rows(connection)) == 1
        assert not connection.in_transaction and not other_store.db.in_transaction
    finally:
        native.store.db = connection
        other_store.close()
    no_task(native.store)
