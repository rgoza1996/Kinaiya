"""Independent Codex regressions for the frozen native standing dispatch owner.

These tests precede the native implementation. Capacity fixtures seed protected
SQLite rows from the published packet grammar; they do not claim 10,000 native
calls, an automatic producer, occurrence admission, or settled external effects.
"""

import importlib
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pytest

from packages.contracts.agent_control import (
    AgentModeChange,
    AgentModeReceipt,
)
from packages.contracts.interfaces import AgentControlConflict, AgentControlUnavailable
from packages.contracts.standing_dispatch import (
    STANDING_PRODUCER_TTL_MS,
    StandingDispatchActivation,
    StandingDispatchActivationReceipt,
    StandingDispatchStop,
    StandingProducerReceipt,
    StandingProducerRenewal,
    activation_digest,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.session.agent_control import AgentControl
from services.session.store import Store

TABLES = {
    "standing_dispatch_meta",
    "standing_dispatch_gate",
    "standing_dispatch_activations",
    "standing_dispatch_producers",
}
MARKER = "public-synthetic-dispatch-fault"


def uid(number):
    return str(uuid.UUID(int=number + 1000))


def native_class():
    try:
        module = importlib.import_module("services.session.standing_dispatch")
    except ModuleNotFoundError as error:
        if error.name != "services.session.standing_dispatch":
            raise
        pytest.fail("Frozen native StandingDispatchOwner capability is absent", pytrace=False)
    return module.StandingDispatchOwner


def database_state(connection):
    """Observe actual user schemas and rows, including SQLite storage classes."""
    schema = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    tables = {}
    for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
    ):
        name = row[0]
        quoted = '"' + name.replace('"', '""') + '"'
        tables[name] = tuple(
            sorted(
                (tuple(item) for item in connection.execute(f"SELECT * FROM {quoted}")), key=repr
            )
        )
    return schema, tables


def gate(connection):
    return dict(connection.execute("SELECT * FROM standing_dispatch_gate WHERE id=1").fetchone())


def ledger_counts(connection):
    return tuple(
        connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name in (
            "agent_control_generations",
            "agent_control_changes",
            "standing_dispatch_activations",
            "standing_dispatch_producers",
        )
    )


class Clock:
    def __init__(self, value=1000):
        self.value = value
        self.calls = 0
        self.forbidden = False

    def __call__(self):
        self.calls += 1
        if self.forbidden:
            raise AssertionError("Historical observation consulted the producer clock")
        return self.value


@dataclass
class Native:
    path: object
    store: Store
    control: AgentControl
    clock: Clock = field(default_factory=Clock)
    owners: list = field(default_factory=list)

    @property
    def backend(self):
        return self.store.db.execute("SELECT backend_id FROM agent_control_state").fetchone()[0]

    def owner(self, *, control=None, clock=None, register=True):
        owner = native_class()(
            self.store,
            control or self.control,
            monotonic_ms=self.clock if clock is None else clock,
        )
        self.owners.append(owner)
        if register:
            owner.register()
        return owner

    def renewal(self, *, producer=1, sequence=1, instance=None):
        return StandingProducerRenewal(
            schema_version="1.0",
            expected_backend=self.backend,
            expected_instance=self.control.instance if instance is None else instance,
            producer_id=uid(producer),
            sequence=sequence,
        )

    def activation(self, owner, *, key="activate:1"):
        state = owner.snapshot()
        return StandingDispatchActivation(
            schema_version="1.0",
            mode_change=mode_request(
                self.backend,
                state.instance_id,
                state.control_revision,
                key=key,
                mode="autonomous",
            ),
            expected_epoch=state.epoch,
            expected_fence_token=state.fence_token,
        )


@pytest.fixture
def native(tmp_path):
    path = tmp_path / "protected-session.sqlite"
    store = Store(path)
    control = AgentControl(store)
    control.register()
    value = Native(path, store, control)
    try:
        yield value
    finally:
        # Deliberately corrupt/poisoned cases must not replace their assertion
        # with a cleanup read of damaged metadata. Close only owned connections.
        try:
            value.store.close()
        except sqlite3.ProgrammingError:
            pass


def mode_request(backend, instance, revision, *, key, mode):
    return AgentModeChange(
        schema_version="1.0",
        idempotency_key=key,
        expected_backend=backend,
        expected_instance=instance,
        expected_revision=revision,
        mode=mode,
        active_work_policy="finish_already_accepted",
    )


def stop_request(state, *, epoch=None, digest=None, instance=None):
    target = state.epoch if epoch is None else epoch
    return StandingDispatchStop(
        schema_version="1.0",
        expected_backend=state.backend_id,
        expected_instance=state.instance_id if instance is None else instance,
        issued_epoch=target,
        activation_digest=state.activation_digest if epoch is None else digest,
        expected_fence_token=state.fence_token,
    )


def active(native):
    owner = native.owner()
    producer = native.renewal()
    lease = owner.renew(producer).validate_request(producer)
    request = native.activation(owner)
    receipt = owner.activate(request).validate_request(request)
    assert owner.snapshot().dispatch_ready is True
    return owner, producer, lease, request, receipt


def assert_no_task(native):
    assert native.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    assert native.store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 0


class SqlBoundary:
    """A real connection fault after actual DML; never a fake database result."""

    def __init__(self, connection, *, phase=None, rollback_failure=False, after_write=None):
        self.connection = connection
        self.phase = phase
        self.rollback_failure = rollback_failure
        self.after_write = after_write
        self.before = database_state(connection)
        self.dirty = self.fired = self.rollback_fired = self.closed = False
        self.committed = False
        self.at_fault = None
        self.write_hook_fired = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        cursor = self.connection.execute(statement, parameters)
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE"}:
            self.dirty = True
            if self.after_write is not None and not self.write_hook_fired:
                self.write_hook_fired = True
                self.after_write()
        return cursor

    def commit(self):
        if self.phase is not None and self.dirty and not self.fired:
            self.fired = True
            self.at_fault = database_state(self.connection)
            assert self.connection.in_transaction
            assert self.at_fault != self.before, "Fault did not follow real native DML"
            if self.phase == "after":
                self.connection.commit()
                self.committed = True
            raise RuntimeError(MARKER)
        return self.connection.commit()

    def rollback(self):
        self.rollback_fired = True
        if self.rollback_failure:
            raise RuntimeError(MARKER)
        return self.connection.rollback()

    def close(self):
        self.closed = True
        return self.connection.close()


def test_independent_fixture_uses_real_registered_native_control_and_strict_packets(native):
    snapshot = native.control.snapshot()
    assert snapshot.control_writable is True
    assert snapshot.mode == "on_demand"
    assert snapshot.available_modes == ("off", "on_demand")
    assert snapshot.standing_work_available is False
    renewal = native.renewal()
    assert StandingProducerRenewal.from_json(renewal.canonical_bytes()) == renewal
    mode = mode_request(native.backend, native.control.instance, 0, key="control", mode="off")
    receipt = native.control.change(mode).validate_request(mode)
    assert receipt.revision == 1
    assert native.control.snapshot().mode == "off"
    assert not native.store.db.in_transaction
    assert_no_task(native)


def test_constructor_is_passive_and_explicit_register_creates_only_gate_authority(native):
    before = database_state(native.store.db)
    instance = native.control.instance
    owner = native.owner(register=False)
    assert database_state(native.store.db) == before
    assert native.clock.calls == 0
    owner.register()
    assert native.clock.calls == 0
    state = owner.snapshot()
    assert (state.epoch, state.stopped_through_epoch) == (0, 0)
    assert state.instance_id == instance
    assert state.backend_id == native.backend
    assert state.mode == "on_demand"
    assert state.control_writable is True
    assert state.producer is None
    assert state.activation_digest is None
    assert state.activation_control_revision is None
    assert state.dispatch_ready is False
    assert len(state.fence_token) == 36
    assert native.control.instance == instance
    names = set(database_state(native.store.db)[1])
    assert names - set(before[1]) == TABLES
    assert ledger_counts(native.store.db) == (1, 0, 0, 0)
    assert_no_task(native)


def test_unregistered_methods_cannot_initialize_storage_or_authority(native):
    owner = native.owner(register=False)
    before = database_state(native.store.db)
    mode = mode_request(native.backend, native.control.instance, 0, key="absent", mode="autonomous")
    request = StandingDispatchActivation(
        schema_version="1.0", mode_change=mode, expected_epoch=0, expected_fence_token=uid(90)
    )
    stop = StandingDispatchStop(
        schema_version="1.0",
        expected_backend=native.backend,
        expected_instance=native.control.instance,
        issued_epoch=0,
        activation_digest=None,
        expected_fence_token=uid(90),
    )
    calls = (
        owner.snapshot,
        lambda: owner.renew(native.renewal()),
        lambda: owner.activate(request),
        lambda: owner.lookup(request),
        lambda: owner.stop(stop),
    )
    for call in calls:
        with pytest.raises(StandingUnavailable):
            call()
        assert database_state(native.store.db) == before


@pytest.mark.parametrize("mismatch", ["different_store", "unregistered_control"])
def test_registration_refuses_foreign_store_and_cannot_register_a_control_writer(
    native, tmp_path, mismatch
):
    other = Store(tmp_path / "other.sqlite")
    try:
        control = AgentControl(other if mismatch == "different_store" else native.store)
        if mismatch == "different_store":
            control.register()
        before = database_state(native.store.db), database_state(other.db)
        with pytest.raises((StandingConflict, StandingUnavailable)):
            owner = native_class()(native.store, control, monotonic_ms=native.clock)
            owner.register()
        assert (database_state(native.store.db), database_state(other.db)) == before
        assert native.control.snapshot().control_writable is True
    finally:
        other.close()


def test_double_registration_and_local_close_do_not_retire_another_controller(native):
    owner = native.owner()
    before = database_state(native.store.db)
    with pytest.raises(StandingConflict):
        owner.register()
    assert database_state(native.store.db) == before
    replacement = AgentControl(native.store)
    replacement.register()
    current = native.owner(control=replacement)
    before = database_state(native.store.db)
    owner.close()
    owner.close()
    assert database_state(native.store.db) == before
    assert replacement.snapshot().control_writable is True
    assert current.snapshot().control_writable is True
    with pytest.raises(StandingUnavailable):
        owner.snapshot()


def test_activation_atomically_retains_both_full_proofs_without_nested_public_change(
    native, monkeypatch
):
    owner = native.owner()
    request = native.activation(owner)
    before = database_state(native.store.db)
    with pytest.raises(AgentControlConflict):
        native.control.change(request.mode_change)
    assert database_state(native.store.db) == before
    owner.renew(native.renewal())
    original_change = native.control.change

    def forbidden(*args, **kwargs):
        raise AssertionError("Nested public mode/submit transaction")

    monkeypatch.setattr(native.control, "change", forbidden)
    monkeypatch.setattr(native.store, "admit_submit", forbidden)
    receipt = owner.activate(request).validate_request(request)
    assert receipt.epoch == 1
    assert receipt.activation_digest == activation_digest(request, 1)
    assert receipt.mode_receipt.request == request.mode_change
    assert receipt.mode_receipt.revision == 1
    row = native.store.db.execute("SELECT * FROM standing_dispatch_activations").fetchone()
    assert row["key"] == request.mode_change.idempotency_key
    assert row["epoch"] == 1
    assert row["request"] == request.canonical_bytes().decode()
    assert row["receipt"] == receipt.canonical_bytes().decode()
    mode_row = native.store.db.execute("SELECT * FROM agent_control_changes").fetchone()
    assert mode_row["request"] == request.mode_change.canonical_bytes().decode()
    assert mode_row["receipt"] == receipt.mode_receipt.canonical_bytes().decode()
    assert mode_row["digest"] == request.mode_change.request_digest()
    before = database_state(native.store.db)
    total = native.store.db.total_changes
    assert original_change(request.mode_change) == receipt.mode_receipt
    assert database_state(native.store.db) == before
    assert native.store.db.total_changes == total
    assert owner.snapshot().dispatch_ready is True
    assert native.control.snapshot().standing_work_available is False
    assert_no_task(native)


def test_exact_activation_history_survives_stop_off_and_controller_replacement_without_clock(
    native,
):
    owner, _, _, request, receipt = active(native)
    owner.stop(stop_request(owner.snapshot()))
    off = mode_request(native.backend, native.control.instance, 1, key="owner:off", mode="off")
    native.control.change(off)
    replacement = AgentControl(native.store)
    replacement.register()
    native.clock.forbidden = True
    # An explicit unhealthy clock observation is allowed to omit producer; exact
    # replay itself must return before asking that clock for new eligibility.
    before = database_state(native.store.db)
    calls = native.clock.calls
    total = native.store.db.total_changes
    assert owner.activate(request) == receipt
    assert native.clock.calls == calls
    lookup = owner.lookup(request).validate_request(request)
    assert lookup.status == "recorded"
    assert lookup.receipt == receipt
    assert lookup.mutation_fenced is True
    assert lookup.state.mode == "off"
    assert lookup.state.instance_id == replacement.instance
    assert lookup.state.control_writable is False
    assert lookup.state.producer is None
    assert lookup.state.dispatch_ready is False
    assert database_state(native.store.db) == before
    assert native.store.db.total_changes == total
    assert_no_task(native)


@pytest.mark.parametrize("field_name", ["fence", "epoch", "mode_revision"])
def test_retained_mode_key_binds_entire_activation_envelope(native, field_name):
    owner, _, _, request, _ = active(native)
    raw = request.model_dump(mode="python")
    if field_name == "fence":
        raw["expected_fence_token"] = uid(90)
    elif field_name == "epoch":
        raw["expected_epoch"] = 2
    else:
        raw["mode_change"]["expected_revision"] = 2
    conflict = StandingDispatchActivation.model_validate(raw)
    before = database_state(native.store.db)
    for call in (owner.activate, owner.lookup):
        with pytest.raises(StandingConflict):
            call(conflict)
        assert database_state(native.store.db) == before


def test_ordinary_mode_history_cannot_be_grafted_into_an_activation(native):
    owner = native.owner()
    owner.renew(native.renewal())
    mode = mode_request(
        native.backend, native.control.instance, 0, key="shared:key", mode="on_demand"
    )
    native.control.change(mode)
    request = native.activation(owner, key="shared:key")
    before = database_state(native.store.db)
    for call in (owner.activate, owner.lookup):
        with pytest.raises((StandingConflict, StandingUnavailable)):
            call(request)
        assert database_state(native.store.db) == before


def test_unobserved_lookup_is_read_only_and_unknown_generation_is_not_retirement(native):
    owner = native.owner()
    request = native.activation(owner)
    before = database_state(native.store.db)
    lookup = owner.lookup(request).validate_request(request)
    assert lookup.status == "unobserved"
    assert lookup.receipt is None
    assert lookup.mutation_fenced is False
    raw = request.model_dump(mode="python")
    raw["mode_change"]["expected_instance"] = uid(200)
    unknown = StandingDispatchActivation.model_validate(raw)
    with pytest.raises(StandingConflict):
        owner.lookup(unknown)
    assert database_state(native.store.db) == before
    malformed = (
        request.model_copy(update={"synthetic_unknown_key": MARKER}),
        request.model_copy(
            update={
                "mode_change": request.mode_change.model_copy(
                    update={"synthetic_unknown_key": MARKER}
                )
            }
        ),
        request.model_copy(
            update={
                "mode_change": request.mode_change.model_copy(update={"expected_revision": True})
            }
        ),
        StandingDispatchActivation.model_construct(
            schema_version="1.0", mode_change=request.mode_change, expected_epoch=0
        ),
    )
    for packet in malformed:
        for call in (owner.activate, owner.lookup):
            with pytest.raises(StandingUnavailable) as raised:
                call(packet)
            assert MARKER not in str(raised.value)
            assert database_state(native.store.db) == before
    incomplete = StandingProducerRenewal.model_construct(
        schema_version="1.0",
        expected_backend=native.backend,
        expected_instance=native.control.instance,
        producer_id=uid(1),
    )
    with pytest.raises(StandingUnavailable):
        owner.renew(incomplete)
    copied_stop = stop_request(lookup.state).model_copy(update={"synthetic_unknown_key": MARKER})
    with pytest.raises(StandingUnavailable) as raised:
        owner.stop(copied_stop)
    assert MARKER not in str(raised.value)
    assert database_state(native.store.db) == before
    assert_no_task(native)


def test_stop_zero_rotates_before_already_fenced_branch_and_blocks_queued_activation(native):
    owner = native.owner()
    owner.renew(native.renewal())
    queued = native.activation(owner)
    stop = stop_request(owner.snapshot())
    before_counts = ledger_counts(native.store.db)
    proof = owner.stop(stop).validate_request(stop)
    assert proof.target_fenced is True
    assert proof.state.epoch == proof.state.stopped_through_epoch == 0
    assert proof.state.fence_token != stop.expected_fence_token
    after = database_state(native.store.db)
    assert owner.stop(stop).validate_request(stop).state.fence_token == proof.state.fence_token
    assert database_state(native.store.db) == after
    with pytest.raises(StandingConflict):
        owner.activate(queued)
    assert owner.lookup(queued).mutation_fenced is True
    assert ledger_counts(native.store.db) == before_counts
    assert_no_task(native)


def test_two_simultaneous_stops_rotate_the_current_token_once(native):
    owner = native.owner()
    second = native.owner()
    request = stop_request(owner.snapshot())
    barrier = threading.Barrier(2)
    counts = ledger_counts(native.store.db)

    def run(capability):
        barrier.wait(timeout=3)
        return capability.stop(request, deadline=time.monotonic() + 3).validate_request(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, capability) for capability in (owner, second)]
        proofs = [future.result(timeout=5) for future in futures]
    current = gate(native.store.db)
    assert current["fence_token"] != request.expected_fence_token
    assert {proof.state.fence_token for proof in proofs} == {current["fence_token"]}
    assert ledger_counts(native.store.db) == counts
    assert current["epoch"] == current["stopped_through_epoch"] == 0


def test_two_simultaneous_activations_use_one_cas_and_one_ordinary_mode_row(native):
    owner = native.owner()
    second = native.owner()
    owner.renew(native.renewal())
    first = native.activation(owner, key="race:first")
    other = native.activation(second, key="race:other")
    barrier = threading.Barrier(2)

    def run(capability, request):
        barrier.wait(timeout=3)
        try:
            return capability.activate(request, deadline=time.monotonic() + 3)
        except StandingConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, *pair) for pair in ((owner, first), (second, other))]
        results = [future.result(timeout=5) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    winner = winners[0]
    winner.validate_request(first if results[0] is not None else other)
    assert ledger_counts(native.store.db) == (1, 1, 1, 1)
    assert owner.snapshot().epoch == 1
    assert owner.snapshot().control_revision == 1
    assert_no_task(native)


def test_older_unfenced_stop_cannot_rotate_or_disable_the_newer_epoch(native):
    owner, _, _, _, first = active(native)
    old_stop = stop_request(owner.snapshot())
    newer = native.activation(owner, key="activate:second")
    owner.activate(newer)
    before = owner.snapshot()
    proof = owner.stop(old_stop).validate_request(old_stop)
    assert proof.state.stopped_through_epoch == first.epoch
    assert proof.state.epoch == before.epoch == 2
    assert proof.state.fence_token == before.fence_token
    assert proof.state.activation_digest == before.activation_digest
    assert proof.state.dispatch_ready is True
    counts = ledger_counts(native.store.db)
    after = database_state(native.store.db)
    owner.stop(old_stop)
    assert database_state(native.store.db) == after
    assert ledger_counts(native.store.db) == counts


def test_lost_old_stop_retry_never_stops_a_later_explicit_activation(native):
    owner, _, _, _, _ = active(native)
    original_stop = stop_request(owner.snapshot())
    first_proof = owner.stop(original_stop)
    request = native.activation(owner, key="reactivate")
    current = owner.activate(request)
    before = database_state(native.store.db)
    proof = owner.stop(original_stop).validate_request(original_stop)
    assert proof.state.epoch == current.epoch == 2
    assert proof.state.stopped_through_epoch == 1
    assert proof.state.fence_token == first_proof.state.fence_token
    assert proof.state.dispatch_ready is True
    assert database_state(native.store.db) == before


@pytest.mark.parametrize("wrong", ["backend", "generation", "digest", "future_epoch"])
def test_stop_rejects_unissued_or_foreign_target_without_writes(native, wrong):
    owner, _, _, _, _ = active(native)
    raw = stop_request(owner.snapshot()).model_dump(mode="python")
    if wrong == "backend":
        raw["expected_backend"] = "f" * 32 if native.backend != "f" * 32 else "e" * 32
    elif wrong == "generation":
        raw["expected_instance"] = uid(200)
    elif wrong == "digest":
        raw["activation_digest"] = "f" * 64
    else:
        raw["issued_epoch"] = 2
    request = StandingDispatchStop.model_validate(raw)
    before = database_state(native.store.db)
    with pytest.raises(StandingConflict):
        owner.stop(request)
    assert database_state(native.store.db) == before


def test_native_lease_interval_has_exact_expiry_and_does_not_change_mode_or_epoch(native):
    owner = native.owner()
    before = owner.snapshot()
    renewal = native.renewal()
    receipt = owner.renew(renewal).validate_request(renewal)
    assert receipt.issued_monotonic_ms == 1000
    assert receipt.expires_monotonic_ms == 1000 + STANDING_PRODUCER_TTL_MS
    observed = owner.snapshot()
    assert observed.producer == receipt
    assert observed.mode == before.mode
    assert observed.control_revision == before.control_revision
    assert observed.epoch == before.epoch
    assert observed.fence_token == before.fence_token
    native.clock.value = receipt.expires_monotonic_ms - 1
    assert owner.snapshot().producer == receipt
    native.clock.value = receipt.expires_monotonic_ms
    assert owner.snapshot().producer is None
    request = native.activation(owner)
    before = database_state(native.store.db)
    with pytest.raises((StandingConflict, StandingUnavailable)):
        owner.activate(request)
    assert database_state(native.store.db) == before
    # Expiry does not ban the current nonretired producer UUID forever. A
    # genuine next-sequence ACK creates a new interval; old sequence does not.
    next_request = native.renewal(sequence=2)
    renewed = owner.renew(next_request).validate_request(next_request)
    assert renewed.issued_monotonic_ms == receipt.expires_monotonic_ms
    assert renewed.lease_token != receipt.lease_token
    assert owner.snapshot().producer == renewed
    assert ledger_counts(native.store.db) == (1, 0, 0, 1)


def test_same_sequence_is_original_history_without_clock_extension_and_old_sequence_conflicts(
    native,
):
    owner = native.owner()
    request = native.renewal()
    first = owner.renew(request)
    before = database_state(native.store.db)
    total = native.store.db.total_changes
    native.clock.forbidden = True
    calls = native.clock.calls
    assert owner.renew(request).validate_request(request) == first
    assert native.clock.calls == calls
    assert database_state(native.store.db) == before
    assert native.store.db.total_changes == total
    native.clock.forbidden = False
    native.clock.value += 5000
    second_request = native.renewal(sequence=2)
    second = owner.renew(second_request)
    assert second.issued_monotonic_ms == 6000
    assert second.lease_token != first.lease_token
    assert ledger_counts(native.store.db) == (1, 0, 0, 1)
    before = database_state(native.store.db)
    for request in (native.renewal(sequence=1), native.renewal(sequence=4)):
        with pytest.raises(StandingConflict):
            owner.renew(request)
        assert database_state(native.store.db) == before


def test_only_expired_producer_can_be_replaced_and_retired_uuid_never_resurrects(native):
    owner = native.owner()
    first_request = native.renewal(producer=1)
    first = owner.renew(first_request)
    contender = native.renewal(producer=2)
    before = database_state(native.store.db)
    with pytest.raises(StandingConflict):
        owner.renew(contender)
    assert database_state(native.store.db) == before
    native.clock.value = first.expires_monotonic_ms
    replacement = owner.renew(contender).validate_request(contender)
    assert replacement.issued_monotonic_ms == first.expires_monotonic_ms
    rows = native.store.db.execute(
        "SELECT producer_id,retired FROM standing_dispatch_producers ORDER BY producer_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(uid(1), 1), (uid(2), 0)]
    before = database_state(native.store.db)
    with pytest.raises(StandingConflict):
        owner.renew(native.renewal(producer=1, sequence=2))
    assert database_state(native.store.db) == before
    assert owner.snapshot().producer == replacement


@pytest.mark.parametrize("bad_clock", [True, 0.5, "backward"])
def test_invalid_or_backward_native_clock_latches_lease_unavailable_but_stop_still_works(
    native, bad_clock
):
    owner = native.owner()
    owner.renew(native.renewal())
    state = owner.snapshot()
    stop = stop_request(state)
    request = native.activation(owner)
    native.clock.value = 999 if bad_clock == "backward" else bad_clock
    observed = owner.snapshot()
    assert observed.producer is None
    assert observed.dispatch_ready is False
    before = database_state(native.store.db)
    for call in (lambda: owner.renew(native.renewal(sequence=2)), lambda: owner.activate(request)):
        with pytest.raises(StandingUnavailable):
            call()
        assert database_state(native.store.db) == before
    native.clock.value = 2000
    with pytest.raises(StandingUnavailable):
        owner.renew(native.renewal(sequence=2))
    assert database_state(native.store.db) == before
    proof = owner.stop(stop).validate_request(stop)
    assert proof.state.fence_token != state.fence_token
    assert proof.state.producer is None


def test_controller_replacement_invalidates_old_clock_interval_but_new_lease_preserves_finite_intent(
    native,
):
    owner, original_request, _, activation, receipt = active(native)
    replacement = AgentControl(native.store)
    replacement.register()
    earlier_clock = Clock(10)
    current = native.owner(control=replacement, clock=earlier_clock)
    observed = current.snapshot()
    assert observed.mode == "autonomous"
    assert observed.epoch == receipt.epoch
    assert observed.producer is None
    assert observed.dispatch_ready is False
    before = database_state(native.store.db)
    stale = original_request.model_copy(update={"sequence": 2})
    with pytest.raises(StandingConflict):
        owner.renew(stale)
    assert database_state(native.store.db) == before
    next_request = native.renewal(producer=2, instance=replacement.instance)
    next_lease = current.renew(next_request)
    assert next_lease.issued_monotonic_ms == 10
    assert current.snapshot().dispatch_ready is True
    before = database_state(native.store.db)
    assert current.activate(activation) == receipt
    assert database_state(native.store.db) == before
    owner.close()
    assert current.snapshot().control_writable is True
    assert replacement.snapshot().control_writable is True
    assert_no_task(native)


def test_off_on_demand_aba_and_lease_renewal_never_restore_old_activation(native):
    owner, _, _, original, receipt = active(native)
    for revision, mode in ((1, "off"), (2, "on_demand")):
        request = mode_request(
            native.backend, native.control.instance, revision, key=f"mode:{mode}", mode=mode
        )
        native.control.change(request)
        owner.renew(native.renewal(sequence=revision + 1))
        assert owner.snapshot().dispatch_ready is False
        before = database_state(native.store.db)
        assert owner.activate(original) == receipt
        assert native.control.change(original.mode_change) == receipt.mode_receipt
        assert database_state(native.store.db) == before
        assert owner.snapshot().mode == mode
    new_request = native.activation(owner, key="fresh:owner:activation")
    assert new_request.expected_epoch == 1
    new_receipt = owner.activate(new_request)
    assert new_receipt.epoch == 2
    assert new_receipt.mode_receipt.revision == 4
    assert owner.snapshot().dispatch_ready is True


def test_same_database_restart_requires_new_native_lease_and_replay_never_renews(native):
    owner, _, _, original, receipt = active(native)
    old_instance = native.control.instance
    owner.close()
    native.control.close()
    native.store.close()
    reopened = Store(native.path)
    native.store = reopened
    native.control = AgentControl(reopened)
    native.control.register()
    assert native.control.instance != old_instance
    native.clock = Clock(3)
    current = native.owner()
    state = current.snapshot()
    assert state.mode == "autonomous"
    assert state.epoch == receipt.epoch
    assert state.producer is None
    assert state.dispatch_ready is False
    before = database_state(reopened.db)
    assert current.lookup(original).receipt == receipt
    assert current.activate(original) == receipt
    assert database_state(reopened.db) == before
    current.renew(native.renewal(producer=3))
    assert current.snapshot().dispatch_ready is True
    assert ledger_counts(reopened.db) == (2, 1, 1, 2)
    assert_no_task(native)


@pytest.mark.parametrize("operation", ["activate", "renew", "stop"])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_real_dml_commit_fault_rolls_back_or_preserves_exact_unknown_history(
    native, operation, phase
):
    owner = native.owner()
    if operation == "activate":
        owner.renew(native.renewal())
        request = native.activation(owner)
    elif operation == "renew":
        request = native.renewal()
    else:
        request = stop_request(owner.snapshot())
    connection = native.store.db
    fault = SqlBoundary(connection, phase=phase)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable) as raised:
            getattr(owner, operation)(request)
        assert MARKER not in str(raised.value)
        assert fault.fired and fault.rollback_fired
        assert fault.at_fault != fault.before
        assert fault.committed is (phase == "after")
        assert not connection.in_transaction
    finally:
        native.store.db = connection
    if phase == "before":
        assert database_state(connection) == fault.before
        if operation == "activate":
            assert owner.lookup(request).status == "unobserved"
        result = getattr(owner, operation)(request).validate_request(request)
    else:
        committed = database_state(connection)
        total = connection.total_changes
        result = getattr(owner, operation)(request).validate_request(request)
        assert database_state(connection) == committed
        assert connection.total_changes == total
    if operation == "activate":
        assert result.epoch == 1
        assert ledger_counts(connection) == (1, 1, 1, 1)
        assert owner.lookup(request).receipt == result
    elif operation == "renew":
        assert result.request.sequence == 1
        assert ledger_counts(connection) == (1, 0, 0, 1)
    else:
        assert result.state.fence_token != request.expected_fence_token
        assert ledger_counts(connection) == (1, 0, 0, 0)
    assert_no_task(native)


def test_failed_rollback_poison_closes_actual_writer_and_preserves_disk_history(native):
    owner = native.owner()
    owner.renew(native.renewal())
    request = native.activation(owner)
    connection = native.store.db
    fault = SqlBoundary(connection, phase="before", rollback_failure=True)
    native.store.db = fault
    with pytest.raises(StandingUnavailable):
        owner.activate(request)
    assert fault.fired and fault.rollback_fired
    assert fault.at_fault != fault.before
    assert fault.closed, "Unresolved protected transaction remained usable"
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with sqlite3.connect(native.path) as inspector:
        assert database_state(inspector) == fault.before
    with pytest.raises(StandingUnavailable):
        owner.activate(request)


@pytest.mark.parametrize("retirement", ["cancel", "close", "lease_expired"])
def test_actual_post_dml_precommit_guard_rolls_back_both_mode_and_activation(native, retirement):
    owner = native.owner()
    lease = owner.renew(native.renewal())
    request = native.activation(owner)
    cancelled = threading.Event()
    before = database_state(native.store.db)
    with pytest.raises(StandingUnavailable):
        owner.activate(request, deadline=time.monotonic() - 1)
    assert database_state(native.store.db) == before

    def after_write():
        if retirement == "cancel":
            cancelled.set()
        elif retirement == "close":
            owner.close()
        else:
            native.clock.value = lease.expires_monotonic_ms

    connection = native.store.db
    hook = SqlBoundary(connection, after_write=after_write)
    native.store.db = hook
    try:
        with pytest.raises(StandingUnavailable):
            owner.activate(request, deadline=time.monotonic() + 10, cancelled=cancelled.is_set)
        assert hook.write_hook_fired
        assert hook.dirty
        assert hook.rollback_fired
        assert database_state(connection) == hook.before
        assert not connection.in_transaction
    finally:
        native.store.db = connection
    assert_no_task(native)


def seed_generations(native, *, total=10000):
    """Actual SQLite seed, not a claim of total calls to AgentControl.register."""
    connection = native.store.db
    connection.execute("BEGIN IMMEDIATE")
    connection.executemany(
        "INSERT INTO agent_control_generations(instance_id,retired) VALUES(?,1)",
        ((uid(100_000 + number),) for number in range(total - 1)),
    )
    connection.commit()
    assert (
        connection.execute("SELECT count(*) FROM agent_control_generations").fetchone()[0] == total
    )


def seed_activation_history(native, state, *, total=10000):
    """Seed canonical, contiguous paired ordinary/activation history via public DTOs."""
    backend = native.backend
    instance = native.control.instance
    ordinary = []
    activations = []
    last = None
    for epoch in range(1, total + 1):
        mode = mode_request(
            backend,
            instance,
            epoch - 1,
            key=f"seeded:activation:{epoch}",
            mode="autonomous",
        )
        request = StandingDispatchActivation(
            schema_version="1.0",
            mode_change=mode,
            expected_epoch=epoch - 1,
            expected_fence_token=state.fence_token,
        )
        mode_proof = AgentModeReceipt(
            schema_version="1.0",
            status="mode_saved",
            backend_id=backend,
            service_instance=instance,
            revision=epoch,
            request=mode,
        )
        last = StandingDispatchActivationReceipt(
            schema_version="1.0",
            request=request,
            mode_receipt=mode_proof,
            epoch=epoch,
            activation_digest=activation_digest(request, epoch),
        )
        ordinary.append(
            (
                mode.idempotency_key,
                mode.request_digest(),
                mode.canonical_bytes().decode(),
                mode_proof.canonical_bytes().decode(),
            )
        )
        activations.append(
            (
                mode.idempotency_key,
                epoch,
                request.canonical_bytes().decode(),
                last.canonical_bytes().decode(),
            )
        )
    connection = native.store.db
    connection.execute("BEGIN IMMEDIATE")
    connection.executemany("INSERT INTO agent_control_changes VALUES(?,?,?,?)", ordinary)
    connection.executemany("INSERT INTO standing_dispatch_activations VALUES(?,?,?,?)", activations)
    connection.execute(
        "UPDATE agent_control_state SET revision=?,mode='autonomous' WHERE id=1", (total,)
    )
    connection.execute("UPDATE standing_dispatch_gate SET epoch=? WHERE id=1", (total,))
    connection.commit()
    assert tuple(
        connection.execute(
            "SELECT count(*),min(epoch),max(epoch) FROM standing_dispatch_activations"
        ).fetchone()
    ) == (total, 1, total)
    assert (
        connection.execute(
            "SELECT count(*) FROM standing_dispatch_activations WHERE typeof(epoch)!='integer'"
        ).fetchone()[0]
        == 0
    )
    return last


@pytest.mark.parametrize("capacity", ["activation_and_mode", "producers", "controller_generations"])
def test_valid_seeded_capacity_does_not_block_bounded_current_safety_operations(native, capacity):
    owner = native.owner()
    producer_request = native.renewal()
    lease = owner.renew(producer_request)
    if capacity == "activation_and_mode":
        last = seed_activation_history(native, owner.snapshot())
        assert owner.lookup(last.request).receipt == last
        state = owner.snapshot()
        assert state.epoch == 10000
        off = mode_request(
            native.backend, native.control.instance, 10000, key="full:off", mode="off"
        )
        before = database_state(native.store.db)
        with pytest.raises(AgentControlUnavailable):
            native.control.change(off)
        assert database_state(native.store.db) == before
        counts = ledger_counts(native.store.db)
        proof = owner.stop(stop_request(state))
        assert proof.state.stopped_through_epoch == 10000
        assert proof.state.fence_token != state.fence_token
        assert ledger_counts(native.store.db) == counts == (1, 10000, 10000, 1)
        assert owner.lookup(last.request).receipt == last
    elif capacity == "producers":
        rows = []
        for number in range(9999):
            request = native.renewal(producer=200_000 + number)
            proof = StandingProducerReceipt(
                schema_version="1.0",
                request=request,
                lease_token=uid(300_000 + number),
                issued_monotonic_ms=0,
                expires_monotonic_ms=STANDING_PRODUCER_TTL_MS,
            )
            rows.append(
                (
                    request.producer_id,
                    request.expected_instance,
                    1,
                    proof.canonical_bytes().decode(),
                    1,
                )
            )
        connection = native.store.db
        connection.executemany("INSERT INTO standing_dispatch_producers VALUES(?,?,?,?,?)", rows)
        connection.commit()
        assert owner.snapshot().producer == lease
        renewed = owner.renew(native.renewal(sequence=2))
        assert renewed.request.sequence == 2
        native.clock.value = renewed.expires_monotonic_ms
        before = database_state(connection)
        with pytest.raises(StandingUnavailable):
            owner.renew(native.renewal(producer=99))
        assert database_state(connection) == before
        state = owner.snapshot()
        counts = ledger_counts(connection)
        assert owner.stop(stop_request(state)).target_fenced is True
        assert ledger_counts(connection) == counts == (1, 0, 0, 10000)
    else:
        request = native.activation(owner)
        receipt = owner.activate(request)
        known = native.control.instance
        seed_generations(native)
        native.control.close()
        failed_control = AgentControl(native.store)
        with pytest.raises(AgentControlUnavailable):
            failed_control.register()
        native.clock.forbidden = True
        stop_capability = native.owner(control=failed_control)
        calls = native.clock.calls
        state = stop_capability.snapshot()
        assert state.instance_id is None
        assert state.control_writable is False
        assert state.producer is None
        assert native.clock.calls == calls
        counts = ledger_counts(native.store.db)
        proof = stop_capability.stop(stop_request(state, instance=known))
        assert proof.state.stopped_through_epoch == receipt.epoch
        assert proof.state.instance_id is None
        assert proof.state.mode == "autonomous"
        assert ledger_counts(native.store.db) == counts == (10000, 1, 1, 1)
        assert (
            native.store.db.execute("SELECT instance_id FROM agent_control_state").fetchone()[0]
            is None
        )
        assert stop_capability.lookup(request).receipt == receipt
        # Exhaustion cannot bootstrap a missing gate through a read/Stop
        # capability. This separate protected DB has valid mode history only.
        absent_path = native.path.parent / "no-standing-gate.sqlite"
        absent_store = Store(absent_path)
        try:
            absent_control = AgentControl(absent_store)
            absent_control.register()
            absent = Native(absent_path, absent_store, absent_control)
            seed_generations(absent)
            absent_control.close()
            refused_control = AgentControl(absent_store)
            with pytest.raises(AgentControlUnavailable):
                refused_control.register()
            before = database_state(absent_store.db)
            capability = native_class()(absent_store, refused_control, monotonic_ms=native.clock)
            with pytest.raises(StandingUnavailable):
                capability.register()
            assert database_state(absent_store.db) == before
            assert not TABLES & set(before[1])
        finally:
            absent_store.close()
    assert_no_task(native)


@pytest.mark.parametrize(
    "corruption",
    [
        "fractional_gate",
        "fractional_epoch",
        "fractional_sequence",
        "request_json",
        "ordinary_receipt",
    ],
)
def test_corrupt_selected_native_counters_and_full_proofs_fail_closed_without_repair(
    native, corruption
):
    owner, _, _, original, _ = active(native)
    connection = native.store.db
    if corruption == "fractional_gate":
        connection.execute("UPDATE standing_dispatch_gate SET epoch=1.5 WHERE id=1")
        assert (
            connection.execute("SELECT typeof(epoch) FROM standing_dispatch_gate").fetchone()[0]
            == "real"
        )
    elif corruption == "fractional_epoch":
        connection.execute("UPDATE standing_dispatch_activations SET epoch=1.5")
        assert (
            connection.execute(
                "SELECT typeof(epoch) FROM standing_dispatch_activations"
            ).fetchone()[0]
            == "real"
        )
    elif corruption == "fractional_sequence":
        connection.execute("UPDATE standing_dispatch_producers SET sequence=1.5")
        assert (
            connection.execute(
                "SELECT typeof(sequence) FROM standing_dispatch_producers"
            ).fetchone()[0]
            == "real"
        )
    elif corruption == "request_json":
        # Duplicate key retains the same last-value JSON projection; raw
        # original identity still must be rejected, never normalized/repaired.
        text = original.canonical_bytes().decode()
        damaged = '{"schema_version":"1.0",' + text[1:]
        assert json.loads(damaged) == json.loads(text)
        connection.execute("UPDATE standing_dispatch_activations SET request=?", (damaged,))
    else:
        row = connection.execute("SELECT receipt FROM agent_control_changes").fetchone()
        damaged = json.loads(row[0])
        damaged["revision"] = 2
        connection.execute("UPDATE agent_control_changes SET receipt=?", (json.dumps(damaged),))
    connection.commit()
    before = database_state(connection)
    with pytest.raises(StandingUnavailable):
        owner.snapshot()
    with pytest.raises(StandingUnavailable):
        owner.lookup(original)
    with pytest.raises(StandingUnavailable):
        owner.activate(original)
    assert database_state(connection) == before
    assert not connection.in_transaction


def test_failed_registration_is_permanently_closed_and_new_owner_must_register_explicitly(native):
    owner = native.owner(register=False)
    connection = native.store.db
    fault = SqlBoundary(connection, phase="before")
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable):
            owner.register()
        assert fault.fired and fault.rollback_fired
        assert database_state(connection) == fault.before
    finally:
        native.store.db = connection
    before = database_state(connection)
    with pytest.raises(StandingUnavailable):
        owner.register()
    with pytest.raises(StandingUnavailable):
        owner.snapshot()
    assert database_state(connection) == before
    fresh = native.owner()
    assert fresh.snapshot().epoch == 0
    assert native.control.snapshot().control_writable is True
