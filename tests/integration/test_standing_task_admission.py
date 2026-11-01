"""Independent native standing initial-task admission oracles for issue #228.

Frozen from the native v1 boundary before consulting its implementation. These
tests use the actual protected SQLite, grant, gate and immutable claim owners.
They establish neither a worker launch nor effect settlement or claim release.
"""

import importlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from test_agent_submit_admission import proposal as ordinary_proposal
from test_standing_dispatch_owner import MARKER as STORAGE_MARKER
from test_standing_execution_owner import (
    MARKER,
    SECRET,
    SqlBoundary,
    claim_rows,
    database_state,
    make_claim,
    mode_request,
    seed_generations,
    uid,
)
from test_standing_execution_owner import (
    build_native as build_native,
)
from test_standing_execution_owner import (
    native as native,
)
from test_standing_execution_owner import (
    native_class as claim_owner_class,
)

from packages.contracts.interfaces import AgentControlUnavailable
from packages.contracts.models import AppendRequest, SessionRequest
from packages.contracts.standing_admission import (
    StandingInitialProof,
    StandingTaskAdmission,
    standing_entry,
    standing_initial_proof,
)
from packages.contracts.standing_dispatch import StandingDispatchActivation
from packages.contracts.standing_execution import (
    StandingExecutionIntent,
    capability_digest,
    standing_initial_request,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.session.agent_control import AgentControl
from services.session.standing_dispatch import StandingDispatchOwner
from services.session.standing_work import StandingOwner
from services.session.store import JournalError, Store

TABLE = "standing_task_admissions"
KIND = "agent.standing-command"
WRITER = "standing-supervisor"


def task_owner_class():
    # Import inside runtime tests: an absent capability is an observed failure,
    # not a collection error or an excuse to skip the frozen public oracle.
    try:
        module = importlib.import_module("services.session.standing_admission")
    except ModuleNotFoundError as error:
        if error.name != "services.session.standing_admission":
            raise
        pytest.fail("Native standing initial-task owner capability is absent", pytrace=False)
    return module.StandingTaskOwner


@dataclass
class Prepared:
    native: object
    claims: object
    request: StandingExecutionIntent
    claim: object

    def task_owner(self, *, register=True, **changes):
        arguments = {
            "store": self.native.store,
            "control": self.native.control,
            "owner": self.native.owner,
            "dispatch": self.native.dispatch,
            "claims": self.claims,
            **changes,
        }
        owner = task_owner_class()(**arguments)
        self.native.ledgers.append(owner)
        if register:
            owner.register()
        return owner


def prepare(native, *, request=None):
    claims = native.ledger()
    request = native.request() if request is None else request
    result = claims.prepare(request).validate_request(request)
    assert result.disposition == "created"
    assert claims.lookup(request) == claims.unresolved() == result.claim
    assert native.store.db.execute("SELECT count(*) FROM entries").fetchone()[0] == 0
    assert not native.store.db.in_transaction
    return Prepared(native, claims, request, result.claim)


@pytest.fixture
def prepared(native):
    return prepare(native)


def links(connection):
    return [dict(row) for row in connection.execute(f"SELECT * FROM {TABLE} ORDER BY claim_id")]


def standing_rows(connection):
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM entries WHERE kind=? ORDER BY sequence", (KIND,)
        )
    ]


def unchanged(native, error, invoke):
    before = database_state(native.store.db)
    with pytest.raises(error) as refused:
        invoke()
    assert MARKER not in str(refused.value)
    assert STORAGE_MARKER not in str(refused.value)
    assert SECRET.hex() not in str(refused.value)
    assert repr(SECRET) not in str(refused.value)
    assert database_state(native.store.db) == before
    assert not native.store.db.in_transaction


def assert_created(prepared, result):
    native, request, claim = prepared.native, prepared.request, prepared.claim
    assert type(result) is StandingTaskAdmission
    assert result.validate_request(request).disposition == "created"
    proof = result.proof
    assert type(proof) is StandingInitialProof and proof.claim == claim
    (row,) = standing_rows(native.store.db)
    (link,) = links(native.store.db)
    original = standing_initial_request(claim).model_dump(mode="json")
    assert json.loads(row["request"]) == original
    assert len(json.loads(row["request"])) == 9
    assert len(json.loads(row["value"])) == 13
    entry = standing_entry(json.loads(row["value"]), claim)
    assert proof == standing_initial_proof(claim, entry)
    assert proof.journal_entry() == entry == native.store.entry(proof.event_id)
    assert row["writer"] == entry.origin == WRITER
    assert (
        row["event_id"],
        row["sequence"],
        row["session_id"],
        row["thread_id"],
        row["epoch"],
        row["kind"],
        row["idem"],
    ) == (
        entry.event_id,
        entry.sequence,
        entry.session_id,
        entry.thread_id,
        entry.epoch,
        KIND,
        entry.idempotency_key,
    )
    assert link == {
        "claim_id": claim.claim_id,
        "event_id": proof.event_id,
        "sequence": proof.sequence,
        "proof": proof.canonical_bytes().decode(),
    }
    assert len(link["proof"].encode()) <= 163840
    assert SECRET.hex() not in row["request"] + row["value"] + link["proof"]
    assert prepared.claims.unresolved() == claim
    assert len(claim_rows(native.store.db)) == 1
    assert not native.store.db.in_transaction
    return proof


def current_mode(native, mode, key):
    request = mode_request(native.control, mode, key)
    return native.control.change(request).validate_request(request)


def reactivate(native):
    state = native.dispatch.snapshot()
    request = StandingDispatchActivation(
        schema_version="1.0",
        mode_change=mode_request(native.control, "autonomous", "admission-mode-aba"),
        expected_epoch=state.epoch,
        expected_fence_token=state.fence_token,
    )
    return native.dispatch.activate(request).validate_request(request)


def test_fixture_has_actual_prepared_claim_and_only_public_hashes_before_new_admission(native):
    value = prepare(native)
    before = database_state(native.store.db)
    assert value.claim.request == value.request
    assert value.claim.validate_request(value.request) == value.claim
    assert value.claim.claim_id == claim_rows(native.store.db)[0][1]
    assert value.request.expected_instance == native.control.instance
    assert native.owner.snapshot(value.request.responsibility_id).record == native.record
    assert native.dispatch.snapshot().dispatch_ready
    assert standing_initial_request(value.claim).payload["actor"] == "standing"
    assert SECRET.hex() not in value.claim.canonical_bytes().decode()
    assert TABLE not in before[1]
    assert database_state(native.store.db) == before


class NoSql:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, *args, **kwargs):
        self.calls.append("execute")
        raise AssertionError("Passive constructor/close performed SQL")

    def commit(self):
        self.calls.append("commit")
        raise AssertionError("Passive constructor/close committed")

    def rollback(self):
        self.calls.append("rollback")
        raise AssertionError("Passive constructor/close rolled back")


def test_constructor_close_and_existing_bootstrap_do_not_mint_other_authority(prepared):
    native = prepared.native
    connection = native.store.db
    before = database_state(connection)
    clock_calls = native.wall.calls, native.mono.calls
    native.wall.forbidden = native.mono.forbidden = True
    proxy = NoSql(connection)
    native.store.db = proxy
    try:
        passive = prepared.task_owner(register=False)
        assert not passive._registered and not passive._closed and not passive._failed
        passive.close()
        passive.close()
        assert passive._closed and not proxy.calls
    finally:
        native.store.db = connection
    owner = prepared.task_owner()
    after = database_state(connection)
    assert set(after[1]) - set(before[1]) == {TABLE}
    assert after[1][TABLE] == ()
    assert {name: rows for name, rows in after[1].items() if name != TABLE} == before[1]
    assert list(connection.execute(f"PRAGMA foreign_key_list({TABLE})")) == []
    assert (native.wall.calls, native.mono.calls) == clock_calls
    assert owner._registered and not owner._failed
    unchanged(native, StandingConflict, owner.register)
    unchanged(native, StandingUnavailable, lambda: passive.lookup(prepared.request))
    owner.close()
    assert native.control.instance == prepared.request.expected_instance
    assert database_state(connection) == after


def test_incompatible_unregistered_closed_and_failed_capabilities_cannot_bootstrap(build_native):
    for kind in (
        "foreign_store",
        "foreign_control",
        "unregistered_claim",
        "closed_claim",
        "closed_dispatch",
        "failed_owner",
    ):
        native = build_native()
        value = prepare(native)
        changes = {}
        foreign = None
        if kind == "foreign_store":
            foreign = build_native().store
            changes["store"] = foreign
        elif kind == "foreign_control":
            changes["control"] = AgentControl(native.store)
        elif kind == "unregistered_claim":
            changes["claims"] = native.ledger(register=False)
        elif kind == "closed_claim":
            value.claims.close()
        elif kind == "closed_dispatch":
            native.dispatch.close()
        else:
            native.owner._failed = True
        before = database_state(native.store.db)
        foreign_before = database_state(foreign.db) if foreign is not None else None
        rejected = None
        try:
            rejected = value.task_owner(register=False, **changes)
            with pytest.raises(StandingUnavailable):
                rejected.register()
            assert rejected._failed and not rejected._registered
            with pytest.raises(StandingUnavailable):
                rejected.lookup(value.request)
        except StandingUnavailable:
            # Constructor rejection is also safe, although it must remain passive.
            assert rejected is None
        assert database_state(native.store.db) == before
        if foreign is not None:
            assert database_state(foreign.db) == foreign_before
        assert TABLE not in before[1]


@pytest.mark.parametrize("shape", ["sequence_real", "missing_unique", "foreign_key"])
def test_wrong_link_schema_refuses_reads_and_failed_registration_never_repairs(prepared, shape):
    native = prepared.native
    reader = prepared.task_owner()
    sequence = "REAL" if shape == "sequence_real" else "INTEGER"
    unique = "" if shape == "missing_unique" else " UNIQUE"
    foreign = (
        ",FOREIGN KEY(event_id) REFERENCES entries(event_id) ON DELETE CASCADE"
        if shape == "foreign_key"
        else ""
    )
    with native.store.db:
        native.store.db.execute(f"DROP TABLE {TABLE}")
        native.store.db.execute(
            f"CREATE TABLE {TABLE}(claim_id TEXT PRIMARY KEY,event_id TEXT NOT NULL{unique},"
            f"sequence {sequence} NOT NULL UNIQUE,proof TEXT NOT NULL{foreign})"
        )
    fresh = prepared.task_owner(register=False)
    unchanged(native, StandingUnavailable, lambda: reader.lookup(prepared.request))
    unchanged(native, StandingUnavailable, fresh.register)
    assert fresh._failed and not fresh._registered
    with native.store.db:
        native.store.db.execute(f"DROP TABLE {TABLE}")
    unchanged(native, StandingUnavailable, fresh.register)
    assert TABLE not in database_state(native.store.db)[1]


def insert_unlinked_entry(prepared):
    """Install a deliberate protected-row corruption, never a native ACK."""
    native = prepared.native
    original = standing_initial_request(prepared.claim)
    entry = standing_entry(
        {
            **original.model_dump(mode="json"),
            "event_id": uid(744),
            "sequence": 1,
            "origin": WRITER,
            "created_at": "2026-11-01T12:00:10Z",
        },
        prepared.claim,
    )
    with native.store.db:
        native.store.db.execute(
            "INSERT INTO entries(sequence,event_id,session_id,thread_id,epoch,kind,writer,idem,request,value)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                entry.sequence,
                entry.event_id,
                entry.session_id,
                entry.thread_id,
                entry.epoch,
                KIND,
                WRITER,
                entry.idempotency_key,
                original.model_dump_json(),
                entry.model_dump_json(),
            ),
        )
    (row,) = standing_rows(native.store.db)
    assert standing_initial_proof(prepared.claim, json.loads(row["value"])).journal_entry() == entry


def test_bootstrap_never_adopts_an_unlinked_standing_initial_entry(prepared):
    insert_unlinked_entry(prepared)
    owner = prepared.task_owner(register=False)
    unchanged(prepared.native, StandingUnavailable, owner.register)
    assert owner._failed and TABLE not in database_state(prepared.native.store.db)[1]
    assert len(standing_rows(prepared.native.store.db)) == 1


def test_created_ack_binds_actual_entry_link_and_retained_lookup_is_clock_free(prepared):
    owner = prepared.task_owner()
    assert owner.lookup(prepared.request) is None
    before_claim = claim_rows(prepared.native.store.db)
    proof = assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    native = prepared.native
    assert native.store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 0
    before = database_state(native.store.db)
    changes = native.store.db.total_changes
    clock_calls = native.wall.calls, native.mono.calls
    native.wall.forbidden = native.mono.forbidden = True
    assert owner.lookup(prepared.request) == proof
    retained = owner.admit(prepared.request, capability=b"")
    assert retained.validate_request(prepared.request).disposition == "retained"
    assert retained.proof == proof
    assert claim_rows(native.store.db) == before_claim
    assert database_state(native.store.db) == before
    assert native.store.db.total_changes == changes
    assert (native.wall.calls, native.mono.calls) == clock_calls


def test_lookup_requires_native_identity_but_neither_a_claim_nor_current_clocks(native):
    claims = native.ledger()
    value = Prepared(native, claims, native.request(), None)
    owner = value.task_owner()
    native.wall.forbidden = native.mono.forbidden = True
    before = database_state(native.store.db)
    calls = native.wall.calls, native.mono.calls
    assert owner.lookup(value.request) is None
    for field, replacement in (("expected_backend", "f" * 32), ("expected_instance", uid(921))):
        raw = value.request.model_dump(mode="json")
        raw[field] = raw["producer"]["request"][field] = replacement
        raw["capability_sha256"] = capability_digest(
            raw["expected_backend"], raw["idempotency_key"], raw["executor_id"], SECRET
        )
        candidate = StandingExecutionIntent.model_validate(raw)
        unchanged(native, StandingConflict, lambda candidate=candidate: owner.lookup(candidate))
    unchanged(native, StandingConflict, lambda: owner.admit(value.request, capability=SECRET))
    assert database_state(native.store.db) == before
    assert (native.wall.calls, native.mono.calls) == calls


@pytest.mark.parametrize(
    "capability", [b"x" * 32, SECRET[:-1], SECRET + b"x", SECRET.hex(), bytearray(SECRET)]
)
def test_readable_prepared_claim_does_not_replace_the_exact_private_bytes(prepared, capability):
    owner = prepared.task_owner()
    unchanged(
        prepared.native,
        (StandingConflict, StandingUnavailable),
        lambda: owner.admit(prepared.request, capability=capability),
    )
    assert owner.lookup(prepared.request) is None
    assert_created(prepared, owner.admit(prepared.request, capability=SECRET))


def test_native_boundary_rechecks_actual_copied_and_nested_typed_graphs(prepared):
    owner = prepared.task_owner()
    request = prepared.request
    malformed = (
        request.model_copy(update={"synthetic_unknown_key": MARKER}),
        request.model_copy(update={"control_revision": True}),
        request.model_copy(
            update={
                "task": request.task.model_copy(
                    update={"model_pin": request.task.model_pin.model_copy(update={"local": 1})}
                )
            }
        ),
        StandingExecutionIntent.model_construct(
            schema_version="1.0", idempotency_key=request.idempotency_key
        ),
    )
    for candidate in malformed:
        for invoke in (
            lambda candidate=candidate: owner.lookup(candidate),
            lambda candidate=candidate: owner.admit(candidate, capability=SECRET),
        ):
            unchanged(prepared.native, StandingUnavailable, invoke)
    assert_created(prepared, owner.admit(request, capability=SECRET))


def test_retained_full_body_conflict_precedes_off_and_all_current_clock_failures(prepared):
    owner = prepared.task_owner()
    original = owner.admit(prepared.request, capability=SECRET).proof
    current_mode(prepared.native, "off", "after-standing-accept")
    prepared.native.wall.forbidden = prepared.native.mono.forbidden = True
    raw = prepared.request.model_dump(mode="json")
    raw["task"]["harness_version"] = "v-" + "c" * 24
    candidate = StandingExecutionIntent.model_validate(raw)
    for invoke in (
        lambda: owner.lookup(candidate),
        lambda: owner.admit(candidate, capability=SECRET),
    ):
        unchanged(prepared.native, StandingConflict, invoke)
    retained = owner.admit(prepared.request, capability=b"not-the-secret")
    assert retained.disposition == "retained" and retained.proof == original


@pytest.mark.parametrize(
    "fence",
    [
        "grant_revision",
        "disabled",
        "off",
        "on_demand",
        "mode_aba",
        "stop",
        "session_reset",
        "session_delete",
        "lease_expiry",
        "utc_rollback",
        "latest_due",
    ],
)
def test_first_admission_rechecks_current_native_authority_after_prepare(prepared, fence):
    native = prepared.native
    owner = prepared.task_owner()
    if fence == "grant_revision":
        native.save(label="New revision with the same literal objective")
    elif fence == "disabled":
        native.save(enabled=False)
    elif fence in {"off", "on_demand"}:
        current_mode(native, fence, "admission-fence:" + fence)
    elif fence == "mode_aba":
        current_mode(native, "off", "admission-fence:aba-off")
        assert reactivate(native).epoch > prepared.request.dispatch_epoch
        assert native.control.snapshot().mode == "autonomous"
    elif fence == "stop":
        native.dispatch.stop(native.dispatch.stop_context())
    elif fence == "session_reset":
        assert (
            native.store.reset(native.session.session_id).epoch
            > prepared.request.task.task_session.epoch
        )
    elif fence == "session_delete":
        native.store.delete(native.session.session_id)
        assert len(claim_rows(native.store.db)) == 1
    elif fence == "lease_expiry":
        native.mono.value = native.producer.expires_monotonic_ms
    elif fence == "utc_rollback":
        native.wall.value -= timedelta(seconds=1)
    else:
        native.wall.value = datetime(2026, 11, 2, 12, 0, 10, tzinfo=UTC)
    before_claim = claim_rows(native.store.db)
    unchanged(
        native,
        (StandingConflict, StandingUnavailable),
        lambda: owner.admit(prepared.request, capability=SECRET),
    )
    assert not standing_rows(native.store.db) and not links(native.store.db)
    assert claim_rows(native.store.db) == before_claim
    assert owner.lookup(prepared.request) is None


def test_higher_same_producer_heartbeat_can_admit_the_frozen_original_claim(prepared):
    native = prepared.native
    owner = prepared.task_owner()
    original = prepared.request.canonical_bytes()
    native.mono.value += 100
    renewed = native.dispatch.renew(native.producer.request.model_copy(update={"sequence": 2}))
    assert renewed.request.producer_id == prepared.request.producer.request.producer_id
    assert renewed.request.sequence > prepared.request.producer.request.sequence
    assert renewed.lease_token != native.producer.lease_token
    assert native.dispatch.snapshot().producer == renewed
    assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    assert prepared.request.canonical_bytes() == original
    assert prepared.claims.unresolved().request.producer == native.producer


def test_a_different_current_producer_cannot_use_the_original_prepared_packet(prepared):
    native = prepared.native
    owner = prepared.task_owner()
    native.mono.value = native.producer.expires_monotonic_ms
    replacement = native.dispatch.renew(
        native.producer.request.model_copy(update={"producer_id": uid(932)})
    )
    assert replacement.request.producer_id != prepared.request.producer.request.producer_id
    assert native.mono.value >= prepared.request.producer.expires_monotonic_ms
    assert replacement.issued_monotonic_ms <= native.mono.value < replacement.expires_monotonic_ms
    state = native.dispatch.snapshot()
    assert state.producer == replacement and state.dispatch_ready
    unchanged(native, StandingConflict, lambda: owner.admit(prepared.request, capability=SECRET))
    assert owner.lookup(prepared.request) is None


def test_a_new_actual_controller_fences_an_unadmitted_claim_without_erasing_it(prepared):
    native = prepared.native
    owner = prepared.task_owner()
    replacement = AgentControl(native.store)
    replacement.register()
    assert replacement.instance != prepared.request.expected_instance
    unchanged(native, StandingConflict, lambda: owner.admit(prepared.request, capability=SECRET))
    native.wall.forbidden = native.mono.forbidden = True
    before = database_state(native.store.db)
    assert owner.lookup(prepared.request) is None
    assert prepared.claims.unresolved() == prepared.claim
    assert database_state(native.store.db) == before


def test_exact_accepted_history_survives_all_current_fences_and_reset_without_new_rows(prepared):
    native = prepared.native
    owner = prepared.task_owner()
    proof = assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    native.save(enabled=False)
    current_mode(native, "off", "accepted-history:off")
    native.dispatch.stop(native.dispatch.stop_context())
    native.mono.value = native.producer.expires_monotonic_ms
    native.wall.value = datetime(2026, 11, 8, 12, 0, 10, tzinfo=UTC)
    native.store.reset(native.session.session_id)
    replacement = AgentControl(native.store)
    replacement.register()
    assert replacement.instance != prepared.request.expected_instance
    native.wall.forbidden = native.mono.forbidden = True
    calls = native.wall.calls, native.mono.calls
    before = database_state(native.store.db)
    total = native.store.db.total_changes
    assert owner.lookup(prepared.request) == proof
    result = owner.admit(prepared.request, capability=b"")
    assert result.validate_request(prepared.request).disposition == "retained"
    assert result.proof == proof
    assert prepared.claims.unresolved() == prepared.claim
    assert database_state(native.store.db) == before
    assert native.store.db.total_changes == total
    assert (native.wall.calls, native.mono.calls) == calls


def test_reopen_joins_full_retained_proof_and_never_recreates_a_created_disposition(prepared):
    native = prepared.native
    owner = prepared.task_owner()
    proof = assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    owner.close()
    prepared.claims.close()
    native.owner.close()
    native.dispatch.close()
    native.store.close()
    store = Store(native.store.path)
    control = AgentControl(store)
    control.register()
    passive_owner = StandingOwner(store, control, clock=native.wall)
    dispatch = StandingDispatchOwner(store, control, monotonic_ms=native.mono)
    claims = peer = None
    try:
        passive_owner.register()
        dispatch.register()
        native.wall.forbidden = native.mono.forbidden = True
        claims = claim_owner_class()(store, control, passive_owner, dispatch, clock=native.wall)
        claims.register()
        peer = task_owner_class()(store, control, passive_owner, dispatch, claims)
        before = database_state(store.db)
        peer.register()
        assert control.instance != prepared.request.expected_instance
        assert peer.lookup(prepared.request) == proof
        result = peer.admit(prepared.request, capability=b"")
        assert result.disposition == "retained" and result.proof == proof
        assert database_state(store.db) == before
        assert len(standing_rows(store.db)) == len(links(store.db)) == 1
    finally:
        if peer is not None:
            peer.close()
        if claims is not None:
            claims.close()
        passive_owner.close()
        dispatch.close()
        store.close()


@pytest.mark.parametrize("has_history", [False, True], ids=["no-task-schema", "retained-task"])
def test_null_controller_capacity_allows_only_established_read_only_task_history(
    prepared, monkeypatch, has_history
):
    native = prepared.native
    proof = None
    if has_history:
        proof = prepared.task_owner().admit(prepared.request, capability=SECRET).proof
    native.control.close()
    seed_generations(native, total=10000)  # Protected schema seed, not10000 native calls.
    store = Store(native.store.path)
    control = AgentControl(store)
    passive = StandingOwner(store, control, clock=native.wall)
    dispatch = StandingDispatchOwner(store, control, monotonic_ms=native.mono)
    claims = peer = None
    try:
        with pytest.raises(AgentControlUnavailable):
            control.register()
        assert control.instance is None and control.metadata()["instance_id"] is None
        dispatch.register()
        native.wall.forbidden = native.mono.forbidden = True

        def forbidden(*args, **kwargs):
            raise AssertionError("Historical task join used public owner registration/observation")

        for name in ("register", "snapshot", "lookup", "page", "_available", "_observation"):
            monkeypatch.setattr(passive, name, forbidden)
        claims = claim_owner_class()(store, control, passive, dispatch, clock=native.wall)
        claims.register()
        assert not passive._registered
        before = database_state(store.db)
        peer = task_owner_class()(store, control, passive, dispatch, claims)
        if has_history:
            peer.register()
            assert peer.lookup(prepared.request) == proof
            retained = peer.admit(prepared.request, capability=b"")
            assert retained.disposition == "retained" and retained.proof == proof
        else:
            with pytest.raises(StandingUnavailable):
                peer.register()
            assert TABLE not in database_state(store.db)[1]
        assert not passive._registered and control.instance is None
        assert database_state(store.db) == before
    finally:
        if peer is not None:
            peer.close()
        if claims is not None:
            claims.close()
        passive.close()
        dispatch.close()
        store.close()


def test_public_append_rejects_every_role_even_exact_retained_native_body_before_idempotency(
    prepared,
):
    owner = prepared.task_owner()
    proof = assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    exact = standing_initial_request(prepared.claim)
    for role in ("owner", "agent", "converse", "decisions", "resources", WRITER, "unknown"):
        unchanged(
            prepared.native,
            JournalError,
            lambda role=role: prepared.native.store.append(exact, role),
        )
    assert owner.lookup(prepared.request) == proof
    assert (
        prepared.native.store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0]
        == 0
    )


def test_actual_session_delete_cannot_cascade_an_accepted_initial_or_proof(prepared):
    owner = prepared.task_owner()
    proof = assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    unchanged(
        prepared.native,
        JournalError,
        lambda: prepared.native.store.delete(prepared.native.session.session_id),
    )
    assert (
        prepared.native.store.session(prepared.native.session.session_id).epoch
        == prepared.request.task.task_session.epoch
    )
    assert owner.lookup(prepared.request) == proof
    assert len(standing_rows(prepared.native.store.db)) == len(links(prepared.native.store.db)) == 1


@pytest.mark.parametrize(
    "native_initial", [True, False], ids=["real-old-initial", "untrusted-agent-text"]
)
def test_task_identity_collision_uses_actual_native_initial_and_not_arbitrary_model_text(
    prepared, native_initial
):
    native = prepared.native
    owner = prepared.task_owner()
    other = native.store.create_session(SessionRequest(channel="agent"))
    if native_initial:
        body = ordinary_proposal(
            other, task_id=prepared.request.task.task_id, key="cross-session-old-task"
        )
        receipt = native.store.admit_submit(body, "agent").validate_request(body)
        assert receipt.disposition == "created"
        assert receipt.entry.session_id != prepared.request.task.task_session.session_id
        unchanged(
            native,
            (StandingConflict, StandingUnavailable),
            lambda: owner.admit(prepared.request, capability=SECRET),
        )
        assert not links(native.store.db) and not standing_rows(native.store.db)
        assert native.store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 1
    else:
        native.store.append(
            AppendRequest(
                session_id=other.session_id,
                thread_id=other.thread_id,
                epoch=other.epoch,
                kind="agent.command",
                payload={
                    "op": "untrusted-text",
                    "task_id": prepared.request.task.task_id,
                    "text": "Not a native submit",
                },
                idempotency_key="synthetic-agent-model-text",
            ),
            "agent",
        )
        assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
        assert native.store.db.execute("SELECT count(*) FROM agent_submit_slots").fetchone()[0] == 0


@pytest.mark.parametrize("same_body", [True, False], ids=["exact-retry", "same-key-different-body"])
def test_two_native_callers_have_one_atomic_winner_and_no_body_substitution(prepared, same_body):
    owners = (prepared.task_owner(), prepared.task_owner())
    raw = prepared.request.model_dump(mode="json")
    if not same_body:
        raw["task"]["model_pin"]["model"] = "different-caller-model"
    requests = (prepared.request, StandingExecutionIntent.model_validate(raw))
    barrier = threading.Barrier(3)

    def invoke(index):
        barrier.wait(timeout=3)
        try:
            return owners[index].admit(requests[index], capability=SECRET)
        except StandingConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, index) for index in range(2)]
        barrier.wait(timeout=3)
        results = [future.result(timeout=8) for future in futures]
    if same_body:
        assert sorted(result.disposition for result in results) == ["created", "retained"]
        assert results[0].proof == results[1].proof
    else:
        assert isinstance(results[1], StandingConflict)
        assert results[0].disposition == "created"
    winner = results[0] if results[0].disposition == "created" else results[1]
    assert_created(prepared, winner)
    assert owners[0].lookup(prepared.request) == winner.proof


def corrupt_retained(prepared, corruption):
    native = prepared.native
    (row,) = standing_rows(native.store.db)
    (link,) = links(native.store.db)
    request, value = json.loads(row["request"]), json.loads(row["value"])
    with native.store.db:
        if corruption == "request_epoch_bool":
            request["epoch"] = True
        elif corruption == "request_missing_causes":
            request.pop("causes")
        elif corruption == "value_unknown_key":
            value["synthetic_unknown_key"] = MARKER
        elif corruption == "value_origin":
            value["origin"] = "agent-supervisor"
        elif corruption == "value_sequence_bool":
            value["sequence"] = True
        elif corruption == "paired_native_idem":
            request["idempotency_key"] = value["idempotency_key"] = "paired-wrong-native-idem"
            native.store.db.execute(
                "UPDATE entries SET idem=? WHERE event_id=?",
                (request["idempotency_key"], row["event_id"]),
            )
        elif corruption == "sql_writer":
            native.store.db.execute(
                "UPDATE entries SET writer='agent' WHERE event_id=?", (row["event_id"],)
            )
        elif corruption == "sql_epoch":
            native.store.db.execute(
                "UPDATE entries SET epoch=epoch+1 WHERE event_id=?", (row["event_id"],)
            )
        elif corruption == "link_sequence_real":
            native.store.db.execute(f"UPDATE {TABLE} SET sequence=1.5")
        elif corruption == "link_proof_duplicate_key":
            text = link["proof"][:-1] + ',"schema_version":"1.0"}'
            native.store.db.execute(f"UPDATE {TABLE} SET proof=?", (text,))
        elif corruption == "link_proof_noncanonical":
            native.store.db.execute(
                f"UPDATE {TABLE} SET proof=?", (json.dumps(json.loads(link["proof"]), indent=2),)
            )
        elif corruption == "link_proof_blob":
            native.store.db.execute(f"UPDATE {TABLE} SET proof=?", (link["proof"].encode(),))
        elif corruption == "link_proof_oversize":
            native.store.db.execute(f"UPDATE {TABLE} SET proof=?", (" " * 163841,))
        elif corruption == "missing_entry":
            native.store.db.execute("DELETE FROM entries WHERE event_id=?", (row["event_id"],))
        elif corruption == "missing_link":
            native.store.db.execute(f"DELETE FROM {TABLE}")
        elif corruption == "rehashed_wrong_original":
            raw = prepared.request.model_dump(mode="json")
            raw["task"]["objective"] += " Rehashed substitution."
            forged_claim = make_claim(StandingExecutionIntent.model_validate(raw))
            request = standing_initial_request(forged_claim).model_dump(mode="json")
            value = {
                **request,
                **{key: value[key] for key in ("event_id", "sequence", "origin", "created_at")},
            }
            forged = standing_initial_proof(forged_claim, value)
            assert forged.validate_request(forged_claim.request) == forged
            assert forged.claim.claim_id != prepared.claim.claim_id
            assert forged.claim.intent_digest != prepared.claim.intent_digest
            native.store.db.execute(
                f"UPDATE {TABLE} SET claim_id=?,proof=?",
                (forged.claim.claim_id, forged.canonical_bytes().decode()),
            )
        else:
            raise AssertionError(corruption)
        if (
            corruption.startswith("request_")
            or corruption.startswith("value_")
            or corruption in {"paired_native_idem", "rehashed_wrong_original"}
        ):
            native.store.db.execute(
                "UPDATE entries SET request=?,value=? WHERE event_id=?",
                (json.dumps(request), json.dumps(value), row["event_id"]),
            )


@pytest.mark.parametrize(
    "corruption",
    [
        "request_epoch_bool",
        "request_missing_causes",
        "value_unknown_key",
        "value_origin",
        "value_sequence_bool",
        "paired_native_idem",
        "sql_writer",
        "sql_epoch",
        "link_sequence_real",
        "link_proof_duplicate_key",
        "link_proof_noncanonical",
        "link_proof_blob",
        "link_proof_oversize",
        "missing_entry",
        "missing_link",
        "rehashed_wrong_original",
    ],
)
def test_retained_proof_validates_full_native_claim_entry_columns_request_and_link(
    prepared, corruption
):
    owner = prepared.task_owner()
    assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    reader = prepared.task_owner()
    fresh = prepared.task_owner(register=False)
    corrupt_retained(prepared, corruption)
    native = prepared.native
    native.wall.forbidden = native.mono.forbidden = True
    for invoke in (
        lambda: reader.lookup(prepared.request),
        lambda: owner.admit(prepared.request, capability=SECRET),
        fresh.register,
    ):
        unchanged(native, StandingUnavailable, invoke)


@pytest.mark.parametrize("phase", ["before", "after"])
def test_actual_entry_and_link_commit_failure_preserves_only_the_durable_winner(prepared, phase):
    owner = prepared.task_owner()
    native = prepared.native
    connection = native.store.db
    fault = SqlBoundary(connection, phase=phase)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable) as error:
            owner.admit(prepared.request, capability=SECRET)
        assert MARKER not in str(error.value)
        assert STORAGE_MARKER not in str(error.value)
        assert fault.fired and fault.rollback_fired
        assert len(fault.at_fault[1][TABLE]) == 1
        assert len([row for row in fault.at_fault[1]["entries"] if row[5] == KIND]) == 1
        assert not connection.in_transaction
        if phase == "before":
            assert not fault.committed and database_state(connection) == fault.before
        else:
            assert fault.committed and len(links(connection)) == len(standing_rows(connection)) == 1
    finally:
        native.store.db = connection
    if phase == "before":
        assert owner.lookup(prepared.request) is None
        assert_created(prepared, owner.admit(prepared.request, capability=SECRET))
    else:
        native.wall.forbidden = native.mono.forbidden = True
        before = database_state(connection)
        proof = owner.lookup(prepared.request)
        assert proof.validate_request(prepared.request) == proof
        replay = owner.admit(prepared.request, capability=b"")
        assert replay.disposition == "retained" and replay.proof == proof
        assert database_state(connection) == before
    assert prepared.claims.unresolved() == prepared.claim


def test_failed_rollback_closes_the_actual_connection_without_losing_the_prepared_claim(prepared):
    owner = prepared.task_owner()
    native = prepared.native
    connection = native.store.db
    fault = SqlBoundary(connection, phase="before", rollback_failure=True)
    native.store.db = fault
    try:
        with pytest.raises(StandingUnavailable):
            owner.admit(prepared.request, capability=SECRET)
        assert fault.fired and fault.rollback_fired and fault.closed
        assert len(fault.at_fault[1][TABLE]) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with pytest.raises(StandingUnavailable):
            owner.lookup(prepared.request)
    finally:
        native.store.db = connection
    with sqlite3.connect(native.store.path) as disk:
        assert disk.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 0
        assert disk.execute("SELECT count(*) FROM entries WHERE kind=?", (KIND,)).fetchone()[0] == 0
        assert disk.execute("SELECT count(*) FROM standing_execution_claims").fetchone()[0] == 1


class AbandonedOperation(BaseException):
    pass


class AbortAfterLink(SqlBoundary):
    def execute(self, statement, parameters=()):
        cursor = super().execute(statement, parameters)
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("INSERT") and TABLE.upper() in normalized:
            self.fired = True
            self.at_fault = database_state(self.connection)
            assert len(self.at_fault[1][TABLE]) == 1
            assert len([row for row in self.at_fault[1]["entries"] if row[5] == KIND]) == 1
            raise AbandonedOperation(MARKER)
        return cursor


def test_base_exception_after_real_link_insert_rolls_back_both_rows(prepared):
    owner = prepared.task_owner()
    native = prepared.native
    connection = native.store.db
    fault = AbortAfterLink(connection)
    native.store.db = fault
    try:
        with pytest.raises((AbandonedOperation, StandingUnavailable)):
            owner.admit(prepared.request, capability=SECRET)
        assert fault.fired and fault.rollback_fired
        assert database_state(connection) == fault.before and not connection.in_transaction
    finally:
        native.store.db = connection
    assert owner.lookup(prepared.request) is None
    assert prepared.claims.unresolved() == prepared.claim


class FinalSqlPulse(SqlBoundary):
    """Signal at real post-entry-and-link current-control SQL, not a fake ACK."""

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
            state = database_state(self.connection)
            if (
                len(state[1][TABLE]) == 1
                and len([row for row in state[1]["entries"] if row[5] == KIND]) == 1
            ):
                assert self.connection.in_transaction
                self.pulsed, self.at_pulse = True, state
                self.signal()
        return cursor


@pytest.mark.parametrize(
    "signal", ["control", "cancel", "deadline", "lease_expiry", "grant_expiry"]
)
def test_final_authority_and_guard_follow_actual_entry_link_and_blocking_sql(prepared, signal):
    native = prepared.native
    owner = prepared.task_owner()
    cancelled = threading.Event()
    deadline = time.monotonic() + 2.0

    def pulse():
        if signal == "cancel":
            cancelled.set()
        elif signal == "deadline":
            remaining = deadline - time.monotonic()
            if remaining > 0:
                threading.Event().wait(remaining)
            assert time.monotonic() >= deadline
        elif signal == "lease_expiry":
            native.mono.value = native.producer.expires_monotonic_ms
        elif signal == "grant_expiry":
            native.wall.value = datetime(2026, 11, 5, 0, 0, 0, tzinfo=UTC)

    connection = native.store.db
    proxy = FinalSqlPulse(connection, pulse)
    native.store.db = proxy
    result = failure = None
    try:
        try:
            result = owner.admit(
                prepared.request, capability=SECRET, deadline=deadline, cancelled=cancelled.is_set
            )
        except (StandingConflict, StandingUnavailable) as error:
            failure = error
        assert proxy.pulsed, "No actual post-entry-and-link current metadata check was observed"
        assert proxy.at_pulse != proxy.before and not connection.in_transaction
        if signal == "control":
            assert failure is None
            assert_created(prepared, result)
        else:
            assert result is None
            allowed = (
                (StandingConflict, StandingUnavailable)
                if signal in {"lease_expiry", "grant_expiry"}
                else (StandingUnavailable,)
            )
            assert isinstance(failure, allowed)
            assert proxy.rollback_fired and database_state(connection) == proxy.before
    finally:
        native.store.db = connection
    assert prepared.claims.unresolved() == prepared.claim


def test_invalid_options_and_actual_lock_wait_do_not_open_an_admission_window(prepared):
    owner = prepared.task_owner()
    native = prepared.native
    for deadline, cancelled in (
        (True, None),
        (float("nan"), None),
        (float("inf"), None),
        (time.monotonic(), None),
        (time.monotonic() + 5, lambda: "false"),
        (time.monotonic() + 5, lambda: True),
    ):
        unchanged(
            native,
            StandingUnavailable,
            lambda deadline=deadline, cancelled=cancelled: owner.admit(
                prepared.request, capability=SECRET, deadline=deadline, cancelled=cancelled
            ),
        )
    entered = threading.Event()
    deadline = time.monotonic() + 0.05
    before = database_state(native.store.db)

    def waiting():
        entered.set()
        with pytest.raises(StandingUnavailable):
            owner.admit(prepared.request, capability=SECRET, deadline=deadline)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with native.store.lock:
            future = pool.submit(waiting)
            assert entered.wait(timeout=2)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                threading.Event().wait(remaining)
            assert time.monotonic() >= deadline
        future.result(timeout=3)
    assert database_state(native.store.db) == before and not native.store.db.in_transaction
    assert owner.lookup(prepared.request) is None


def test_actual_second_connection_writer_replacement_waits_for_the_complete_admission_transaction(
    prepared,
):
    native = prepared.native
    owner = prepared.task_owner()
    other_store = Store(native.store.path)
    contender = AgentControl(other_store)
    inserted, registration_started = threading.Event(), threading.Event()

    def after_write():
        inserted.set()
        assert registration_started.wait(timeout=3)

    def replace_controller():
        assert inserted.wait(timeout=3)
        registration_started.set()
        contender.register()
        return contender.instance

    connection = native.store.db
    proxy = SqlBoundary(connection, after_write=after_write)
    native.store.db = proxy
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(replace_controller)
            result = owner.admit(prepared.request, capability=SECRET)
            assert proxy.write_hook_fired
            proof = assert_created(prepared, result)
            replacement = future.result(timeout=5)
        assert replacement is not None and replacement != prepared.request.expected_instance
        assert (
            other_store.db.execute("SELECT instance_id FROM agent_control_state").fetchone()[0]
            == replacement
        )
        assert len(standing_rows(other_store.db)) == len(links(other_store.db)) == 1
        native.wall.forbidden = native.mono.forbidden = True
        assert owner.lookup(prepared.request) == proof
        assert not connection.in_transaction and not other_store.db.in_transaction
    finally:
        native.store.db = connection
        other_store.close()
