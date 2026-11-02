"""Independent native permanent admission-close oracles; never release work.

Actual qualified Store/owners/claim/initial receipts are used. Only explicit
SQLite commit and concurrency boundaries are injected. No worker or Resources
settlement, new HTTP role, generic writer grant or provider is exercised.
"""

import importlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from test_standing_execution_owner import (
    SECRET,
    SqlBoundary,
    claim_rows,
    database_state,
    mode_request,
    seed_generations,
    uid,
)
from test_standing_execution_owner import native_class as claim_owner_class
from test_standing_export_admission import old_export
from test_standing_task_admission import (
    NoSql,
    assert_created,
    links,
    standing_rows,
    task_owner_class,
)
from test_standing_task_admission import build_native as build_native
from test_standing_task_admission import native as native
from test_standing_task_admission import prepared as prepared

from packages.contracts.interfaces import AgentControlUnavailable
from packages.contracts.standing_admission import standing_initial_proof
from packages.contracts.standing_execution import StandingExecutionIntent, capability_digest
from packages.contracts.standing_export import StandingExportRequest
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.session.agent_control import AgentControl
from services.session.standing_dispatch import StandingDispatchOwner
from services.session.standing_work import StandingOwner
from services.session.store import Store

META = "standing_admission_closure_meta"
ROWS = "standing_admission_closures"
MAXIMUM = 180224
MARKER = "SYNTHETIC-CLOSURE-FAULT-DO-NOT-ECHO"


def feature(owner):
    try:
        importlib.import_module("services.session.standing_closure")
    except ModuleNotFoundError as error:
        if error.name != "services.session.standing_closure":
            raise
        pytest.fail("Frozen native admission-close capability is absent", pytrace=False)
    assert callable(getattr(owner, "close_admission", None)), "Native close method absent"
    assert callable(getattr(owner, "lookup_closure", None)), "Native closure lookup absent"
    return owner


def closure_type():
    return importlib.import_module("packages.contracts.standing_closure").StandingAdmissionClosure


def rows(connection):
    return [dict(row) for row in connection.execute(f"SELECT * FROM {ROWS} ORDER BY claim_id")]


def unchanged(prepared, invoke, errors=(StandingConflict, StandingUnavailable)):
    connection = prepared.native.store.db
    before = database_state(connection)
    with pytest.raises(errors) as refused:
        invoke()
    assert MARKER not in str(refused.value) and SECRET.hex() not in str(refused.value)
    assert database_state(connection) == before and not connection.in_transaction


def assert_fence(prepared, owner, result, initial):
    assert type(result) is closure_type()
    assert result.validate_request(prepared.request) == result
    assert result.claim_id == prepared.claim.claim_id
    assert result.intent_digest == prepared.claim.intent_digest
    assert result.initial == initial
    (row,) = rows(prepared.native.store.db)
    assert row == {
        "claim_id": prepared.claim.claim_id,
        "closure": result.canonical_bytes().decode(),
    }
    assert SECRET.hex() not in row["closure"] and repr(SECRET) not in row["closure"]
    before = database_state(prepared.native.store.db)
    assert owner.lookup_closure(prepared.request) == result
    assert prepared.claims.lookup(prepared.request) == prepared.claim
    assert prepared.claims.unresolved() == prepared.claim
    assert len(claim_rows(prepared.native.store.db)) == 1
    assert database_state(prepared.native.store.db) == before
    return result


def peer(prepared, store, control, *, passive=False):
    native = prepared.native
    owner = StandingOwner(store, control, clock=native.wall)
    if not passive:
        owner.register()
    dispatch = StandingDispatchOwner(store, control, monotonic_ms=native.mono)
    dispatch.register()
    claims = claim_owner_class()(store, control, owner, dispatch, clock=native.wall)
    claims.register()
    tasks = task_owner_class()(store, control, owner, dispatch, claims)
    tasks.register()
    native.ledgers.extend((owner, dispatch, claims, tasks))
    return feature(tasks), claims


def test_existing_native_claim_admission_and_full_export_controls(prepared):
    owner = prepared.task_owner()
    proof = assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    request = StandingExportRequest(schema_version="1.0", initial=proof, export=old_export(proof))
    assert (
        owner.export_admission(request).validate_request(request).admission.task_id
        == prepared.request.task.task_id
    )
    assert owner.lookup(prepared.request) == proof
    assert prepared.claims.unresolved() == prepared.claim


def test_constructor_is_passive_and_register_adds_only_complete_closure_family(prepared):
    connection = prepared.native.store.db
    # Simulate the exact prior qualified task-link schema, not new helper state.
    with connection:
        connection.execute(
            "CREATE TABLE standing_task_admissions(claim_id TEXT PRIMARY KEY,"
            "event_id TEXT NOT NULL UNIQUE,sequence INTEGER NOT NULL UNIQUE,proof TEXT NOT NULL)"
        )
    before = database_state(connection)
    proxy = NoSql(connection)
    prepared.native.store.db = proxy
    try:
        owner = feature(prepared.task_owner(register=False))
        assert proxy.calls == []
    finally:
        prepared.native.store.db = connection
    prepared.native.wall.forbidden = prepared.native.mono.forbidden = True
    owner.register()
    after = database_state(connection)
    assert set(after[1]) - set(before[1]) == {META, ROWS}
    assert after[1][ROWS] == ()
    meta = connection.execute(f"SELECT * FROM {META}").fetchone()
    assert tuple(meta) == (1, 1, prepared.request.expected_backend)
    assert owner.lookup_closure(prepared.request) is None
    owner.close()
    assert database_state(connection) == after


@pytest.mark.parametrize("accepted", [False, True], ids=["empty", "accepted"])
def test_closure_is_exact_native_permanent_fence_and_never_releases_claim(prepared, accepted):
    owner = feature(prepared.task_owner())
    initial = (
        assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
        if accepted
        else None
    )
    before_claims = claim_rows(prepared.native.store.db)
    before_entries = standing_rows(prepared.native.store.db)
    before_links = links(prepared.native.store.db)
    prepared.native.wall.forbidden = prepared.native.mono.forbidden = True
    result = assert_fence(
        prepared, owner, owner.close_admission(prepared.request, capability=SECRET), initial
    )
    before = database_state(prepared.native.store.db)
    changes = prepared.native.store.db.total_changes
    assert owner.close_admission(prepared.request, capability=SECRET) == result
    assert database_state(prepared.native.store.db) == before
    assert prepared.native.store.db.total_changes == changes
    if initial is None:
        unchanged(
            prepared, lambda: owner.admit(prepared.request, capability=SECRET), (StandingConflict,)
        )
        assert owner.lookup(prepared.request) is None
    else:
        assert owner.admit(prepared.request, capability=b"").proof == initial
        assert owner.lookup(prepared.request) == initial
        request = StandingExportRequest(
            schema_version="1.0", initial=initial, export=old_export(initial)
        )
        assert (
            owner.export_admission(request).validate_request(request).admission.task_id
            == prepared.request.task.task_id
        )
    assert claim_rows(prepared.native.store.db) == before_claims
    assert standing_rows(prepared.native.store.db) == before_entries
    assert links(prepared.native.store.db) == before_links
    assert prepared.claims.prepare(prepared.request).disposition == "retained"
    raw = prepared.request.model_dump(mode="json")
    raw["idempotency_key"] = "closure-cannot-release-global-slot"
    raw["capability_sha256"] = capability_digest(
        raw["expected_backend"], raw["idempotency_key"], raw["executor_id"], SECRET
    )
    unchanged(
        prepared,
        lambda: prepared.claims.prepare(StandingExecutionIntent.model_validate(raw)),
        (StandingConflict,),
    )


@pytest.mark.parametrize("retained", [False, True])
def test_exact_private_capability_is_required_even_for_retained_close(prepared, retained):
    owner = feature(prepared.task_owner())
    if retained:
        owner.close_admission(prepared.request, capability=SECRET)
    for secret in (None, b"", SECRET[:-1], b"x" * 32, bytearray(SECRET)):
        unchanged(
            prepared,
            lambda secret=secret: owner.close_admission(prepared.request, capability=secret),
        )
    unchanged(
        prepared,
        lambda: owner.close_admission(prepared.request),
        (TypeError, StandingConflict, StandingUnavailable),
    )
    result = owner.lookup_closure(prepared.request)
    assert (result is not None) is retained


def test_lookup_absence_does_not_close_or_create_and_full_key_conflicts_are_not_absence(prepared):
    owner = feature(prepared.task_owner())
    before = database_state(prepared.native.store.db)
    assert owner.lookup_closure(prepared.request) is None
    absent = prepared.request.model_dump(mode="json")
    absent["idempotency_key"] = "closure-absent-prepared-key"
    absent["capability_sha256"] = capability_digest(
        absent["expected_backend"], absent["idempotency_key"], absent["executor_id"], SECRET
    )
    absent = StandingExecutionIntent.model_validate(absent)
    assert owner.lookup_closure(absent) is None
    unchanged(
        prepared, lambda: owner.close_admission(absent, capability=SECRET), (StandingConflict,)
    )
    changed = prepared.request.model_dump(mode="json")
    changed["task"]["objective"] += " Different full original."
    changed = StandingExecutionIntent.model_validate(changed)
    for invoke in (
        lambda: owner.lookup_closure(changed),
        lambda: owner.close_admission(changed, capability=SECRET),
    ):
        unchanged(prepared, invoke, (StandingConflict,))
    assert database_state(prepared.native.store.db) == before
    assert_created(prepared, owner.admit(prepared.request, capability=SECRET))


@pytest.mark.parametrize("fence", ["off", "stop", "disabled", "expired"])
def test_first_close_denies_future_admission_without_current_work_eligibility(prepared, fence):
    native = prepared.native
    owner = feature(prepared.task_owner())
    if fence == "off":
        native.control.change(mode_request(native.control, "off", "closure-owner-off"))
    elif fence == "stop":
        native.dispatch.stop(native.dispatch.stop_context())
    elif fence == "disabled":
        native.save(enabled=False)
    else:
        native.mono.value = native.producer.expires_monotonic_ms
        native.wall.value = datetime(2026, 11, 8, tzinfo=UTC)
    native.wall.forbidden = native.mono.forbidden = True
    result = owner.close_admission(prepared.request, capability=SECRET)
    assert_fence(prepared, owner, result, None)
    assert owner.close_admission(prepared.request, capability=SECRET) == result


def test_new_current_controller_can_close_original_claim_but_expired_writer_cannot(prepared):
    native = prepared.native
    old = feature(prepared.task_owner())
    control = AgentControl(native.store)
    control.register()
    assert control.instance != prepared.request.expected_instance
    unchanged(
        prepared,
        lambda: old.close_admission(prepared.request, capability=SECRET),
        (StandingConflict,),
    )
    current, claims = peer(prepared, native.store, control)
    native.wall.forbidden = native.mono.forbidden = True
    result = current.close_admission(prepared.request, capability=SECRET)
    assert result.initial is None and result.claim_id == prepared.claim.claim_id
    assert current.lookup_closure(prepared.request) == result
    assert old.lookup_closure(prepared.request) == result
    assert claims.unresolved() == prepared.claim


@pytest.mark.parametrize("accepted", [False, True])
def test_closure_survives_real_store_reopen_without_clock_or_new_receipt(prepared, accepted):
    native = prepared.native
    owner = feature(prepared.task_owner())
    proof = owner.admit(prepared.request, capability=SECRET).proof if accepted else None
    result = owner.close_admission(prepared.request, capability=SECRET)
    owner.close()
    prepared.claims.close()
    native.owner.close()
    native.dispatch.close()
    native.store.close()
    store = Store(native.store.path)
    control = AgentControl(store)
    control.register()
    try:
        native.wall.forbidden = native.mono.forbidden = True
        reader, claims = peer(prepared, store, control)
        before = database_state(store.db)
        assert reader.lookup_closure(prepared.request) == result
        assert reader.close_admission(prepared.request, capability=SECRET) == result
        assert reader.lookup(prepared.request) == proof
        assert claims.unresolved() == prepared.claim
        assert database_state(store.db) == before
    finally:
        store.close()


def test_null_controller_capacity_retains_readonly_closure_without_inventing_a_writer(prepared):
    native = prepared.native
    owner = feature(prepared.task_owner())
    result = owner.close_admission(prepared.request, capability=SECRET)
    native.control.close()
    seed_generations(native, total=10000)  # Schema seed, not 10,000 native registrations.
    store = Store(native.store.path)
    control = AgentControl(store)
    try:
        with pytest.raises(AgentControlUnavailable):
            control.register()
        assert control.instance is None
        native.wall.forbidden = native.mono.forbidden = True
        reader, claims = peer(prepared, store, control, passive=True)
        before = database_state(store.db)
        assert reader.lookup_closure(prepared.request) == result
        assert claims.unresolved() == prepared.claim
        assert control.instance is None and database_state(store.db) == before
    finally:
        store.close()


class TransactionGate(SqlBoundary):
    def __init__(self, connection, table):
        super().__init__(connection)
        self.table = table.upper()
        self.entered, self.release = threading.Event(), threading.Event()

    def execute(self, statement, parameters=()):
        cursor = super().execute(statement, parameters)
        normalized = " ".join(statement.upper().split())
        if not self.fired and normalized.startswith("INSERT") and self.table in normalized:
            self.fired = True
            self.at_fault = database_state(self.connection)
            assert self.connection.in_transaction
            self.entered.set()
            assert self.release.wait(8), "Actual native transaction gate was not released"
        return cursor


@pytest.mark.parametrize("first", ["close", "admit"])
def test_actual_same_store_close_admit_serialization_has_no_null_then_initial(prepared, first):
    owner, other = feature(prepared.task_owner()), feature(prepared.task_owner())
    native, connection = prepared.native, prepared.native.store.db
    gate = TransactionGate(connection, ROWS if first == "close" else "standing_task_admissions")
    native.store.db = gate
    second_started = threading.Event()

    def close():
        return owner.close_admission(prepared.request, capability=SECRET)

    def admit():
        try:
            return other.admit(prepared.request, capability=SECRET)
        except StandingConflict as error:
            return error

    def second():
        second_started.set()
        return admit() if first == "close" else close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            leading = pool.submit(close if first == "close" else admit)
            try:
                assert gate.entered.wait(8) and gate.fired
                following = pool.submit(second)
                assert second_started.wait(3) and not following.done()
            finally:
                gate.release.set()
            a, b = leading.result(timeout=10), following.result(timeout=10)
        closure, admission = (a, b) if first == "close" else (b, a)
    finally:
        gate.release.set()
        native.store.db = connection
    if first == "close":
        assert closure.initial is None and isinstance(admission, StandingConflict)
        assert standing_rows(connection) == [] and links(connection) == []
    else:
        assert admission.disposition == "created" and closure.initial == admission.proof
        assert len(standing_rows(connection)) == len(links(connection)) == 1
    assert_fence(prepared, owner, closure, closure.initial)
    assert not connection.in_transaction


@pytest.mark.parametrize("phase", ["before", "after"])
def test_real_closure_commit_failure_rolls_back_or_retains_exact_fence(prepared, phase):
    owner = feature(prepared.task_owner())
    native, connection = prepared.native, prepared.native.store.db
    fault = SqlBoundary(connection, phase=phase)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable):
            owner.close_admission(prepared.request, capability=SECRET)
        assert fault.fired and fault.rollback_fired
        assert len(fault.at_fault[1][ROWS]) == 1
        assert len(fault.at_fault[1]["standing_execution_claims"]) == 1
        assert fault.at_fault[1]["entries"] == ()
        assert not connection.in_transaction
        if phase == "before":
            assert database_state(connection) == fault.before and not fault.committed
        else:
            assert fault.committed and len(rows(connection)) == 1
    finally:
        native.store.db = connection
    if phase == "before":
        assert owner.lookup_closure(prepared.request) is None
        assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    else:
        result = owner.lookup_closure(prepared.request)
        assert_fence(prepared, owner, result, None)
        unchanged(
            prepared, lambda: owner.admit(prepared.request, capability=SECRET), (StandingConflict,)
        )


@pytest.mark.parametrize("signal", ["cancel", "deadline"])
def test_final_close_guard_runs_after_real_insert_and_blocking_sql(prepared, signal):
    owner = feature(prepared.task_owner())
    native, connection = prepared.native, prepared.native.store.db
    cancelled = threading.Event()
    deadline = time.monotonic() + 3

    def pulse():
        if signal == "deadline":
            threading.Event().wait(max(0, deadline - time.monotonic()))
            assert time.monotonic() >= deadline
        else:
            cancelled.set()

    fault = SqlBoundary(connection, after_write=pulse)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable):
            owner.close_admission(
                prepared.request, capability=SECRET, deadline=deadline, cancelled=cancelled.is_set
            )
        assert fault.write_hook_fired and fault.rollback_fired
        assert database_state(connection) == fault.before and not connection.in_transaction
    finally:
        native.store.db = connection
    assert owner.lookup_closure(prepared.request) is None
    assert prepared.claims.unresolved() == prepared.claim


class PostCommitCancellation(SqlBoundary):
    def __init__(self, connection, cancelled):
        super().__init__(connection)
        self.cancelled = cancelled

    def commit(self):
        self.connection.commit()
        if self.dirty:
            self.committed = self.fired = True
            self.cancelled.set()


def test_actual_commit_then_cancelled_ack_is_permanent_and_readback_never_reopens(prepared):
    owner = feature(prepared.task_owner())
    native, connection = prepared.native, prepared.native.store.db
    cancelled = threading.Event()
    fault = PostCommitCancellation(connection, cancelled)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable):
            owner.close_admission(prepared.request, capability=SECRET, cancelled=cancelled.is_set)
        assert fault.fired and fault.committed and len(rows(connection)) == 1
    finally:
        native.store.db = connection
    result = owner.lookup_closure(prepared.request)
    assert_fence(prepared, owner, result, None)
    unchanged(
        prepared, lambda: owner.admit(prepared.request, capability=SECRET), (StandingConflict,)
    )


def test_rollback_failure_poison_closes_real_connection_without_releasing_claim(prepared):
    owner = feature(prepared.task_owner())
    native, connection = prepared.native, prepared.native.store.db
    fault = SqlBoundary(connection, phase="before", rollback_failure=True)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable):
            owner.close_admission(prepared.request, capability=SECRET)
        assert fault.fired and fault.rollback_fired and fault.closed
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with pytest.raises(StandingUnavailable):
            owner.lookup_closure(prepared.request)
    finally:
        native.store.db = connection
    with sqlite3.connect(native.store.path) as disk:
        assert disk.execute(f"SELECT count(*) FROM {ROWS}").fetchone()[0] == 0
        assert disk.execute("SELECT count(*) FROM standing_execution_claims").fetchone()[0] == 1


@pytest.mark.parametrize("missing", [META, ROWS])
def test_partial_closure_schema_never_repairs_or_exposes_existing_task_history(prepared, missing):
    owner = feature(prepared.task_owner())
    owner.close_admission(prepared.request, capability=SECRET)
    with prepared.native.store.db:
        prepared.native.store.db.execute(f"DROP TABLE {missing}")
    fresh = prepared.task_owner(register=False)
    for invoke in (
        fresh.register,
        lambda: owner.lookup_closure(prepared.request),
        lambda: owner.lookup(prepared.request),
    ):
        unchanged(prepared, invoke, (StandingUnavailable,))


def corrupt(prepared, closure, name):
    connection = prepared.native.store.db
    body = closure.canonical_bytes().decode()
    with connection:
        if name == "noncanonical":
            connection.execute(
                f"UPDATE {ROWS} SET closure=?", (json.dumps(json.loads(body), indent=2),)
            )
        elif name == "duplicate_key":
            connection.execute(
                f"UPDATE {ROWS} SET closure=?", ('{"schema_version":"1.0",' + body[1:],)
            )
        elif name == "blob":
            connection.execute(f"UPDATE {ROWS} SET closure=?", (body.encode(),))
        elif name == "oversize":
            connection.execute(f"UPDATE {ROWS} SET closure=?", (" " * (MAXIMUM + 1),))
        elif name == "meta_counter":
            connection.execute(f"UPDATE {META} SET version=1.5")
            assert connection.execute(f"SELECT typeof(version) FROM {META}").fetchone()[0] == "real"
        elif name == "backend":
            connection.execute(f"UPDATE {META} SET backend_id=?", ("f" * 32,))
        elif name == "orphan_key":
            connection.execute(f"UPDATE {ROWS} SET claim_id=?", ("f" * 64,))
        elif name == "foreign_key":
            row = rows(connection)[0]
            connection.execute(f"DROP TABLE {ROWS}")
            connection.execute(
                f"CREATE TABLE {ROWS}(claim_id TEXT PRIMARY KEY,closure TEXT NOT NULL,"
                "FOREIGN KEY(claim_id) REFERENCES standing_execution_claims(claim_id) ON DELETE CASCADE)"
            )
            connection.execute(f"INSERT INTO {ROWS} VALUES(?,?)", (row["claim_id"], row["closure"]))
            assert connection.execute(f"PRAGMA foreign_key_list({ROWS})").fetchall()
        else:
            module = importlib.import_module("packages.contracts.standing_closure")
            raw = closure.model_dump(mode="json")
            if name == "null_despite_initial":
                raw["initial"] = None
            else:
                assert name == "foreign_initial"
                entry = closure.initial.journal_entry().model_copy(
                    update={"event_id": uid(777), "sequence": closure.initial.sequence + 1}
                )
                raw["initial"] = standing_initial_proof(prepared.claim, entry).model_dump(
                    mode="json"
                )
            initial = (
                None
                if raw["initial"] is None
                else type(closure.initial).model_validate(raw["initial"])
            )
            raw["closure_digest"] = module.standing_closure_digest(prepared.request, initial)
            forged = module.StandingAdmissionClosure.model_validate(raw).validate_request(
                prepared.request
            )
            connection.execute(f"UPDATE {ROWS} SET closure=?", (forged.canonical_bytes().decode(),))


@pytest.mark.parametrize(
    "corruption",
    [
        "noncanonical",
        "duplicate_key",
        "blob",
        "oversize",
        "meta_counter",
        "backend",
        "orphan_key",
        "foreign_key",
        "null_despite_initial",
        "foreign_initial",
    ],
)
def test_stored_closure_must_join_canonical_native_claim_and_exact_actual_initial(
    prepared, corruption
):
    owner = feature(prepared.task_owner())
    proof = owner.admit(prepared.request, capability=SECRET).proof
    closure = owner.close_admission(prepared.request, capability=SECRET)
    corrupt(prepared, closure, corruption)
    fresh = prepared.task_owner(register=False)
    request = StandingExportRequest(schema_version="1.0", initial=proof, export=old_export(proof))
    for invoke in (
        lambda: owner.lookup_closure(prepared.request),
        lambda: owner.close_admission(prepared.request, capability=SECRET),
        lambda: owner.lookup(prepared.request),
        lambda: owner.admit(prepared.request, capability=SECRET),
        lambda: owner.export_admission(request),
        fresh.register,
    ):
        unchanged(prepared, invoke, (StandingUnavailable,))
