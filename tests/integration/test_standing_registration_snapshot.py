"""Independent safety oracles for a TEST-ONLY registration-check snapshot.

The native claim, admission and reservation are real SQLite owner operations.
The proposed helper may cache a validated parse, never weaker checks. This is
neither a Resources product change nor a physical-finalization certificate.
"""

import copy
import importlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_standing_resources_registration import ROWS, accepted, feature, query, reservation
from test_standing_resources_registration import registered as registered
from test_standing_task_admission import build_native as build_native
from test_standing_task_admission import native as native
from test_standing_task_admission import prepared as prepared

from packages.contracts.standing_resources import (
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    standing_resource_digest,
)

REFUSALS = (AssertionError, ValueError, TypeError, AttributeError, RuntimeError)

OTHER_LEASE = "00000000-0000-4000-8000-000000000998"


def helper():
    method = getattr(
        importlib.import_module("test_standing_resources_handoff"),
        "_registration_snapshot",
        None,
    )
    assert callable(method), "Frozen test-only _registration_snapshot helper is absent"
    return method


def original_inline(native_rows, local_rows, expected_proof):
    """Exact original registered_before_effect validation, including all joins."""
    assert len(native_rows) == len(local_rows) == 1 and local_rows[0][1] is not None
    request = feature().StandingResourceReservation.from_json(native_rows[0][0].encode())
    assert request.context.standing == expected_proof
    assert json.loads(local_rows[0][0]) == request.model_dump(mode="json")
    receipt = feature().StandingResourceReservationReceipt.from_json(local_rows[0][1].encode())
    assert receipt.validate_request(request).disposition == "created"


@pytest.fixture
def packets(registered, tmp_path, record_property):
    reg = registered
    proof = accepted(reg)
    request = reservation(reg.owner.context(query(proof.claim.request.task.task_id)))
    receipt = reg.owner.reserve(request).validate_request(request)
    assert receipt.disposition == "created"
    connection = sqlite3.connect(tmp_path / "pure-local-rows.sqlite")
    connection.execute("CREATE TABLE local_rows(request TEXT,receipt TEXT)")
    connection.execute(
        "INSERT INTO local_rows VALUES(?,?)",
        (
            request.canonical_bytes().decode(),
            receipt.canonical_bytes().decode(),
        ),
    )
    connection.commit()
    rows = reg.native.store.db.execute(f"SELECT request FROM {ROWS}").fetchall()
    local = connection.execute("SELECT request,receipt FROM local_rows").fetchall()
    original_inline(rows, local, proof)
    try:
        yield SimpleNamespace(
            native=rows, local=local, proof=proof, request=request, receipt=receipt
        )
    finally:
        connection.close()
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        record_property(
            "actual_cleanup",
            "local SQLite closed; native fixture owns original Store/ledgers; no workers/inference",
        )


def fresh_rows(data):
    return [tuple(row) for row in data.native], [tuple(row) for row in data.local]


def altered_receipt(data, *, retained=False, foreign=False):
    raw = data.receipt.model_dump(mode="json")
    if retained:
        raw["disposition"] = "retained"
    if foreign:
        raw["request"]["lease_id"] = OTHER_LEASE
        request = StandingResourceReservation.model_validate(raw["request"])
        raw["request_digest"] = standing_resource_digest(request)
    result = StandingResourceReservationReceipt.model_validate(raw)
    return result.canonical_bytes().decode()


def corrupt(data, fault):
    native_rows, local_rows = fresh_rows(data)
    if fault == "native-zero":
        native_rows.clear()
    elif fault == "native-two":
        native_rows.append(native_rows[0])
    elif fault == "local-zero":
        local_rows.clear()
    elif fault == "local-two":
        local_rows.append(local_rows[0])
    elif fault == "receipt-null":
        local_rows[0] = (local_rows[0][0], None)
    elif fault == "native-invalid-json":
        native_rows[0] = ('{"broken":',)
    elif fault == "local-invalid-json":
        local_rows[0] = ('{"broken":', local_rows[0][1])
    elif fault == "receipt-invalid-json":
        local_rows[0] = (local_rows[0][0], '{"broken":')
    elif fault == "native-bad-context-digest":
        raw = data.request.model_dump(mode="json")
        raw["context"]["context_digest"] = "f" * 64
        native_rows[0] = (json.dumps(raw),)
    elif fault == "local-different-request":
        raw = data.request.model_dump(mode="json")
        raw["lease_id"] = OTHER_LEASE
        local_rows[0] = (json.dumps(raw), local_rows[0][1])
    elif fault == "native-changed-valid-request":
        changed = data.request.model_copy(update={"lease_id": OTHER_LEASE})
        native_rows[0] = (changed.canonical_bytes().decode(),)
    elif fault == "receipt-bad-digest":
        raw = data.receipt.model_dump(mode="json")
        raw["request_digest"] = "f" * 64
        local_rows[0] = (local_rows[0][0], json.dumps(raw))
    elif fault == "retained-receipt":
        local_rows[0] = (local_rows[0][0], altered_receipt(data, retained=True))
    elif fault == "valid-foreign-receipt":
        local_rows[0] = (local_rows[0][0], altered_receipt(data, foreign=True))
    elif fault == "local-extra-field":
        raw = data.request.model_dump(mode="json")
        raw["untrusted_extra"] = True
        local_rows[0] = (json.dumps(raw), local_rows[0][1])
    else:
        raise AssertionError(fault)
    return native_rows, local_rows


FAULTS = [
    "native-zero",
    "native-two",
    "local-zero",
    "local-two",
    "receipt-null",
    "native-invalid-json",
    "local-invalid-json",
    "receipt-invalid-json",
    "native-bad-context-digest",
    "local-different-request",
    "native-changed-valid-request",
    "receipt-bad-digest",
    "retained-receipt",
    "valid-foreign-receipt",
    "local-extra-field",
]


def test_genuine_native_packet_control_passes_every_original_inline_assertion(packets):
    original_inline(packets.native, packets.local, packets.proof)
    assert packets.receipt.validate_request(packets.request).disposition == "created"
    assert packets.request.context.standing == packets.proof


def test_original_inline_rejects_valid_retained_and_foreign_receipt_controls(packets):
    for fault in ["retained-receipt", "valid-foreign-receipt"]:
        rows, local = corrupt(packets, fault)
        with pytest.raises(REFUSALS):
            original_inline(rows, local, packets.proof)


def test_valid_snapshot_and_exact_stable_repeats_preserve_complete_checks(packets):
    invoke = helper()
    snapshot = invoke(packets.native, packets.local, packets.proof)
    assert snapshot is not None
    for _ in range(4):
        # Fresh SQL-shaped containers with the same complete row bytes/counts.
        rows, local = fresh_rows(packets)
        snapshot = invoke(rows, local, copy.deepcopy(packets.proof), previous=snapshot)
        assert snapshot is not None
        original_inline(rows, local, packets.proof)


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("cached", [False, True], ids=["first-call", "after-snapshot"])
def test_invalid_rows_counts_and_every_original_join_refuse_before_and_after_cache(
    packets, fault, cached
):
    invoke = helper()
    previous = invoke(packets.native, packets.local, packets.proof) if cached else None
    rows, local = corrupt(packets, fault)
    with pytest.raises(REFUSALS):
        original_inline(rows, local, packets.proof)
    with pytest.raises(REFUSALS):
        invoke(rows, local, packets.proof, previous=previous)


@pytest.mark.parametrize(
    "fault", ["native-two", "local-two", "native-bad-context-digest", "receipt-bad-digest"]
)
def test_input_container_alias_cannot_update_the_retained_snapshot_in_place(packets, fault):
    invoke = helper()
    rows, local = fresh_rows(packets)
    previous = invoke(rows, local, packets.proof)
    changed_rows, changed_local = corrupt(packets, fault)
    rows[:], local[:] = changed_rows, changed_local
    with pytest.raises(REFUSALS):
        invoke(rows, local, packets.proof, previous=previous)


@pytest.mark.parametrize("field", ["event", "entry-digest", "claim-digest", "objective", "model"])
@pytest.mark.parametrize("cached", [False, True], ids=["first-call", "after-snapshot"])
def test_actual_typed_expected_proof_is_rechecked_even_when_same_object_mutates(
    packets, field, cached
):
    invoke = helper()
    expected = copy.deepcopy(packets.proof)
    previous = invoke(packets.native, packets.local, expected) if cached else None
    if field == "event":
        object.__setattr__(expected, "event_id", OTHER_LEASE)
    elif field == "entry-digest":
        object.__setattr__(expected, "entry_digest", "f" * 64)
    elif field == "claim-digest":
        object.__setattr__(expected.claim, "claim_digest", "f" * 64)
    elif field == "objective":
        object.__setattr__(
            expected.claim.request.task, "objective", "Changed current expected proof"
        )
    else:
        object.__setattr__(expected.claim.request.task.model_pin, "model", "changed-model")
    with pytest.raises(REFUSALS):
        original_inline(packets.native, packets.local, expected)
    with pytest.raises(REFUSALS):
        invoke(packets.native, packets.local, expected, previous=previous)


@pytest.mark.parametrize("field", ["native", "local-request", "local-receipt"])
def test_semantically_equal_changed_row_bytes_require_fresh_validation_or_refusal(
    packets, monkeypatch, field
):
    invoke = helper()
    rows, local = fresh_rows(packets)
    previous = invoke(rows, local, packets.proof)
    if field == "native":
        rows[0] = (rows[0][0] + " ",)
    elif field == "local-request":
        local[0] = (local[0][0] + " ", local[0][1])
    else:
        local[0] = (local[0][0], local[0][1] + " ")
    # These are still valid for every original inline assertion; a cache must
    # not pretend the complete original bytes are identical.
    original_inline(rows, local, packets.proof)
    parsed = []
    actual_request = StandingResourceReservation.from_json
    actual_receipt = StandingResourceReservationReceipt.from_json

    def request_parser(cls, raw):
        parsed.append("request")
        return actual_request(raw)

    def receipt_parser(cls, raw):
        parsed.append("receipt")
        return actual_receipt(raw)

    monkeypatch.setattr(StandingResourceReservation, "from_json", classmethod(request_parser))
    monkeypatch.setattr(
        StandingResourceReservationReceipt, "from_json", classmethod(receipt_parser)
    )
    try:
        snapshot = invoke(rows, local, packets.proof, previous=previous)
    except REFUSALS:
        return  # Strict refusal on any changed complete bytes is allowed.
    assert snapshot is not None and {"request", "receipt"} <= set(parsed)


def test_unrelated_opaque_previous_cannot_bless_invalid_rows(packets):
    invoke = helper()
    rows, local = corrupt(packets, "valid-foreign-receipt")
    with pytest.raises(REFUSALS):
        invoke(rows, local, packets.proof, previous=object())
