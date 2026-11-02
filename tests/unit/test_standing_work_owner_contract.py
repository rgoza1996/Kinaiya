"""Independent #228 owner DTO oracles, frozen before the new author code.

These models validate supplied proof structure. They cannot establish that a
writer was issued, a database committed, an owner authenticated or work ran.
"""

import copy
import hashlib
import json

import pytest

from packages.contracts.standing_work import (
    StandingChange,
    StandingChangeLookup,
    StandingChangeReceipt,
    StandingConflict,
    StandingCurrent,
    StandingRecord,
    StandingResponsibilityPage,
    StandingResponsibilitySnapshot,
    StandingUnavailable,
    canonical_bytes,
    record_digest,
    standing_json,
)

BACKEND = "a" * 32
INSTANCE = "01234567-89ab-cdef-0123-456789abcdef"
RESTART = "fedcba98-7654-3210-fedc-ba9876543210"
FIRST = "00000000-0000-0000-0000-000000000001"
SECOND = "00000000-0000-0000-0000-000000000002"
ERRORS = (ValueError, TypeError)


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def request(**changes):
    return {
        "schema_version": "1.0",
        "idempotency_key": "owner:synthetic-1",
        "expected_backend": BACKEND,
        "expected_instance": INSTANCE,
        "expected_revision": 3,
        "responsibility_id": FIRST,
        "authorization": {
            "label": "  Café  ",
            "objective": "Inspect é and e\u0301 literally.\0synthetic",
            "content_scope": "public_synthetic",
            "enabled": True,
            "task_session": {"session_id": "session", "thread_id": "thread", "epoch": 1},
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


def receipt(original=None):
    original = request() if original is None else copy.deepcopy(original)
    revision = original["expected_revision"] + 1
    digest_body = {
        "responsibility_id": original["responsibility_id"],
        "revision": revision,
        "request": original,
    }
    return {
        "schema_version": "1.0",
        "status": "responsibility_saved",
        "backend_id": original["expected_backend"],
        "service_instance": original["expected_instance"],
        "revision": revision,
        "request": original,
        "authorization_digest": hashlib.sha256(
            b"kinaiya.standing.authorization.v1\0" + encoded(digest_body)
        ).hexdigest(),
    }


def record(proof=None, activation="pending", clock=None):
    proof = receipt() if proof is None else copy.deepcopy(proof)
    digest_body = {"receipt": proof, "activation": activation, "authorized_from_utc": clock}
    return {
        "schema_version": "1.0",
        **digest_body,
        "record_digest": hashlib.sha256(
            b"kinaiya.standing.record.v1\0" + encoded(digest_body)
        ).hexdigest(),
    }


def snapshot(item=None, **changes):
    return {
        "schema_version": "1.0",
        "backend_id": BACKEND,
        "instance_id": INSTANCE,
        "responsibility_id": FIRST,
        "revision": 0 if item is None else item["receipt"]["revision"],
        "control_writable": True,
        "record": copy.deepcopy(item),
        **changes,
    }


def current_at(revision, instance=INSTANCE):
    return {
        "schema_version": "1.0",
        "backend_id": BACKEND,
        "instance_id": instance,
        "responsibility_id": FIRST,
        "revision": revision,
        "control_writable": True,
    }


def lookup(original=None, **changes):
    return {
        "schema_version": "1.0",
        "status": "unobserved",
        "request": request() if original is None else copy.deepcopy(original),
        "current": current_at(0),
        "request_instance": "current",
        "receipt": None,
        "mutation_fenced": False,
        **changes,
    }


def page(items, **changes):
    return {
        "schema_version": "1.0",
        "backend_id": BACKEND,
        "instance_id": INSTANCE,
        "control_writable": True,
        "items": copy.deepcopy(items),
        "next_cursor": None,
        **changes,
    }


def maximal_record(responsibility_id=FIRST):
    original = request(responsibility_id=responsibility_id, idempotency_key="k" * 128)
    original["authorization"]["label"] = "\x01" * 200
    original["authorization"]["objective"] = "\0" * 16000
    original["authorization"]["task_session"]["session_id"] = "\x02" * 128
    original["authorization"]["task_session"]["thread_id"] = "\x03" * 128
    original["authorization"]["task_session"]["epoch"] = 9_007_199_254_740_991
    original["authorization"]["workspace_pin"]["settings_revision"] = 9_007_199_254_740_990
    original["authorization"]["maximum_runs"] = 32
    original["authorization"]["schedule"]["grace_seconds"] = 86400
    return record(receipt(original))


def test_record_digest_uses_the_exact_independent_domain_and_complete_body():
    proof = receipt()
    pending = record(proof)
    assert (
        pending["record_digest"]
        == "f4a9a880ba437eee0c17e40033b549730a5c15f03065b0d5536e89da2a688052"
    )
    parsed = StandingChangeReceipt.model_validate(proof)
    assert record_digest(parsed, "pending", None) == pending["record_digest"]
    armed = record(proof, "armed", "2024-01-01T00:00:00Z")
    assert record_digest(parsed, "armed", "2024-01-01T00:00:00Z") == armed["record_digest"]
    assert armed["record_digest"] != pending["record_digest"]
    other_proof = receipt(request(idempotency_key="owner:synthetic-2"))
    assert (
        record_digest(StandingChangeReceipt.model_validate(other_proof), "pending", None)
        != pending["record_digest"]
    )


@pytest.mark.parametrize("part", ["key", "responsibility", "literal_objective"])
def test_receipt_matches_the_full_caller_original_not_only_its_authorization(part):
    proof = StandingChangeReceipt.model_validate(receipt())
    original = request()
    assert (
        proof.validate_request(StandingChange.model_validate(original)).request.idempotency_key
        == original["idempotency_key"]
    )
    if part == "key":
        original["idempotency_key"] = "owner:another-key"
    elif part == "responsibility":
        original["responsibility_id"] = SECOND
    else:
        original["authorization"]["objective"] = "Inspect e\u0301 and e\u0301 literally.\0synthetic"
    with pytest.raises(ERRORS):
        proof.validate_request(StandingChange.model_validate(original))


@pytest.mark.parametrize("graph", ["caller", "receipt_request", "receipt"])
def test_original_matcher_revalidates_both_copied_graphs(graph):
    original = StandingChange.model_validate(request())
    proof = StandingChangeReceipt.model_validate(receipt())
    if graph == "caller":
        original = original.model_copy(
            update={"authorization": original.authorization.model_copy(update={"enabled": 1})}
        )
    elif graph == "receipt_request":
        proof = proof.model_copy(
            update={"request": original.model_copy(update={"unknown": "survives"})}
        )
    else:
        proof = proof.model_copy(update={"revision": True})
    with pytest.raises(ERRORS):
        proof.validate_request(original)


@pytest.mark.parametrize(
    ("activation", "clock"), [("pending", None), ("armed", "2024-01-01T00:00:00Z")]
)
def test_pending_definition_and_armed_clock_are_distinct_valid_records(activation, clock):
    parsed = StandingRecord.model_validate(record(activation=activation, clock=clock))
    assert parsed.activation == activation
    assert parsed.authorized_from_utc == clock
    assert parsed.receipt.status == "responsibility_saved"


@pytest.mark.parametrize(
    ("activation", "clock"),
    [
        ("pending", "2024-01-01T00:00:00Z"),
        ("armed", None),
        ("armed", "2024-01-01T00:00:00+00:00"),
        ("armed", "2024-01-31T00:00:00Z"),
        ("armed", "2023-12-31T23:59:59Z"),
        ("running", None),
    ],
)
def test_record_digest_and_record_validation_refuse_invalid_activation_or_horizon(
    activation, clock
):
    proof = StandingChangeReceipt.model_validate(receipt())
    with pytest.raises(ERRORS):
        record_digest(proof, activation, clock)
    with pytest.raises(ERRORS):
        StandingRecord.model_validate(record(activation=activation, clock=clock))


@pytest.mark.parametrize("graph", ["outer", "receipt", "authorization"])
def test_record_revalidates_unknown_fields_and_nested_proof_bypasses(graph):
    parsed = StandingRecord.model_validate(record())
    if graph == "outer":
        forged = parsed.model_copy(update={"unknown": "survives"})
    elif graph == "receipt":
        forged = parsed.model_copy(
            update={"receipt": parsed.receipt.model_copy(update={"revision": True})}
        )
    else:
        auth = parsed.receipt.request.authorization.model_copy(update={"unknown": "survives"})
        original = parsed.receipt.request.model_copy(update={"authorization": auth})
        forged = parsed.model_copy(
            update={"receipt": parsed.receipt.model_copy(update={"request": original})}
        )
    with pytest.raises(ERRORS):
        StandingRecord.model_validate(forged)
    with pytest.raises(ERRORS):
        canonical_bytes(forged)


def test_record_digest_revalidates_a_copied_receipt_graph():
    parsed = StandingChangeReceipt.model_validate(receipt())
    forged = parsed.model_copy(
        update={"request": parsed.request.model_copy(update={"unknown": "survives"})}
    )
    with pytest.raises(ERRORS):
        record_digest(forged, "pending", None)


def test_record_digest_cannot_be_replaced_without_rejecting_the_record():
    raw = record()
    raw["record_digest"] = "0" * 64
    with pytest.raises(ERRORS):
        StandingRecord.model_validate(raw)
    with pytest.raises(ERRORS):
        canonical_bytes(StandingRecord.model_construct(**raw))


def test_snapshot_has_no_record_exactly_at_zero_revision_and_preserves_historical_instance():
    empty = StandingResponsibilitySnapshot.model_validate(snapshot())
    assert empty.revision == 0 and empty.record is None
    historical = StandingResponsibilitySnapshot.model_validate(
        snapshot(record(), instance_id=RESTART, control_writable=False)
    )
    assert historical.instance_id == RESTART
    assert historical.record.receipt.service_instance == INSTANCE
    assert historical.revision == 4
    assert historical.control_writable is False


@pytest.mark.parametrize(
    "fault", ["zero_with_record", "nonzero_without_record", "revision", "backend", "responsibility"]
)
def test_snapshot_requires_exact_retained_revision_backend_and_responsibility(fault):
    raw = snapshot(record())
    if fault == "zero_with_record":
        raw["revision"] = 0
    elif fault == "nonzero_without_record":
        raw["record"] = None
    elif fault == "revision":
        raw["revision"] = 5
    elif fault == "backend":
        raw["backend_id"] = "d" * 32
    else:
        raw["responsibility_id"] = SECOND
    with pytest.raises(ERRORS):
        StandingResponsibilitySnapshot.model_validate(raw)


@pytest.mark.parametrize(
    ("expected_revision", "current_revision", "classification", "fenced"),
    [
        (3, 0, "current", False),
        (3, 3, "current", False),
        (3, 4, "current", True),
        (5, 2, "current", False),
        (3, 0, "retired", True),
        (5, 2, "retired", True),
    ],
)
def test_unobserved_lookup_fence_is_exact_retirement_or_strictly_advanced_revision(
    expected_revision, current_revision, classification, fenced
):
    instance = INSTANCE if classification == "current" else RESTART
    raw = lookup(
        request(expected_revision=expected_revision),
        current=current_at(current_revision, instance),
        request_instance=classification,
        mutation_fenced=fenced,
    )
    parsed = StandingChangeLookup.model_validate(raw)
    assert parsed.mutation_fenced is fenced
    assert parsed.receipt is None
    raw["current"]["control_writable"] = False
    assert StandingChangeLookup.model_validate(raw).mutation_fenced is fenced
    # "retired" is supplied classification, not evidence that this pure DTO issued it.
    raw["mutation_fenced"] = not fenced
    with pytest.raises(ERRORS):
        StandingChangeLookup.model_validate(raw)


def test_positive_retained_lookup_is_not_a_new_mutation_or_current_activation_claim():
    raw = lookup(
        status="recorded",
        current=current_at(6, RESTART),
        request_instance="retired",
        receipt=receipt(),
    )
    parsed = StandingChangeLookup.model_validate(raw)
    assert parsed.receipt.revision == 4
    assert parsed.current.revision == 6
    assert parsed.current.instance_id == RESTART
    assert parsed.receipt.service_instance == INSTANCE
    assert "record" not in parsed.current.model_dump()
    assert parsed.mutation_fenced is False
    raw["mutation_fenced"] = True
    with pytest.raises(ERRORS):
        StandingChangeLookup.model_validate(raw)


def test_lookup_minimal_current_projection_refuses_a_third_copy_of_the_record():
    raw = lookup()
    raw["current"]["record"] = record()
    with pytest.raises(ERRORS):
        StandingChangeLookup.model_validate(raw)


@pytest.mark.parametrize(
    "fault",
    [
        "recorded_without_receipt",
        "unobserved_with_receipt",
        "older_current",
        "changed_original",
        "generation",
        "backend",
        "responsibility",
    ],
)
def test_lookup_refuses_contradictory_or_substituted_proof(fault):
    raw = lookup(status="recorded", current=current_at(4), receipt=receipt())
    if fault == "recorded_without_receipt":
        raw["receipt"] = None
    elif fault == "unobserved_with_receipt":
        raw["status"] = "unobserved"
    elif fault == "older_current":
        raw["current"] = current_at(3)
    elif fault == "changed_original":
        raw["request"]["idempotency_key"] = "owner:substituted"
    elif fault == "generation":
        raw["request_instance"] = "retired"
    elif fault == "backend":
        raw["current"]["backend_id"] = "d" * 32
    else:
        raw["current"]["responsibility_id"] = SECOND
    with pytest.raises(ERRORS):
        StandingChangeLookup.model_validate(raw)
    if fault == "generation":
        raw["current"]["instance_id"] = RESTART
        raw["request_instance"] = "current"
        with pytest.raises(ERRORS):
            StandingChangeLookup.model_validate(raw)


def test_page_accepts_ordered_whole_records_freezes_arrays_and_uses_last_id_cursor():
    items = [
        record(),
        record(receipt(request(responsibility_id=SECOND, expected_instance=RESTART))),
    ]
    parsed = StandingResponsibilityPage.model_validate(page(items, next_cursor=SECOND))
    assert type(parsed.items) is tuple
    assert [item.receipt.request.responsibility_id for item in parsed.items] == [FIRST, SECOND]
    assert parsed.next_cursor == SECOND
    assert StandingResponsibilityPage.model_validate(page([])).items == ()


@pytest.mark.parametrize(
    "fault", ["reversed", "duplicate", "wrong_cursor", "empty_cursor", "backend"]
)
def test_page_refuses_ambiguous_order_cursor_and_backend(fault):
    first, second = record(), record(receipt(request(responsibility_id=SECOND)))
    raw = page([first, second], next_cursor=SECOND)
    if fault == "reversed":
        raw["items"] = [second, first]
    elif fault == "duplicate":
        raw["items"] = [first, first]
        raw["next_cursor"] = FIRST
    elif fault == "wrong_cursor":
        raw["next_cursor"] = FIRST
    elif fault == "empty_cursor":
        raw["items"] = []
    else:
        other = request(responsibility_id=SECOND, expected_backend="d" * 32)
        raw["items"][1] = record(receipt(other))
    with pytest.raises(ERRORS):
        StandingResponsibilityPage.model_validate(raw)


def test_page_refuses_more_than_20_whole_records():
    items = [
        record(receipt(request(responsibility_id=f"00000000-0000-0000-0000-{number:012x}")))
        for number in range(1, 22)
    ]
    with pytest.raises(ERRORS):
        StandingResponsibilityPage.model_validate(page(items))


def test_page_graph_copies_cannot_hide_unknown_nested_fields():
    parsed = StandingResponsibilityPage.model_validate(page([record()]))
    child = parsed.items[0].model_copy(update={"unknown": "survives"})
    forged = parsed.model_copy(update={"items": (child,)})
    with pytest.raises(ERRORS):
        StandingResponsibilityPage.model_validate(forged)
    with pytest.raises(ERRORS):
        canonical_bytes(forged)


def test_maximal_escaped_record_fits_and_page_refuses_over_its_byte_bound():
    first, second = maximal_record(), maximal_record(SECOND)
    assert len(encoded(first)) > 95000
    assert len(canonical_bytes(StandingRecord.model_validate(first))) <= 131072
    assert (
        len(canonical_bytes(StandingResponsibilitySnapshot.model_validate(snapshot(first))))
        <= 147456
    )
    legitimate = page([first, second], next_cursor=SECOND)
    assert 131072 < len(encoded(legitimate)) <= 262144
    parsed = StandingResponsibilityPage.model_validate(legitimate)
    assert parsed.items[0].receipt.request.authorization.objective == "\0" * 16000
    third = maximal_record("00000000-0000-0000-0000-000000000003")
    oversized = page([first, second, third])
    assert len(encoded(oversized)) > 262144
    with pytest.raises(ERRORS):
        StandingResponsibilityPage.model_validate(oversized)


def test_page_node_budget_refuses_a_sub_byte_bound_page_without_truncation():
    items = [
        record(receipt(request(responsibility_id=f"00000000-0000-0000-0000-{number:012x}")))
        for number in range(1, 21)
    ]
    raw = page(items)
    assert len(encoded(raw)) < 262144
    assert len(StandingResponsibilityPage.model_validate(page(items[:19])).items) == 19
    # This literal dataset has 2,153 JSON nodes (keys and values), above 2,048;
    # each record individually fits and the byte bound is not the refusal.
    with pytest.raises(ERRORS):
        StandingResponsibilityPage.model_validate(raw)


@pytest.mark.parametrize("kind", ["page", "lookup"])
def test_full_response_decoding_accepts_valid_proofs_larger_than_request_ingress(kind):
    first, second = maximal_record(), maximal_record(SECOND)
    if kind == "page":
        model = StandingResponsibilityPage
        raw = page([first, second], next_cursor=SECOND)
    else:
        model = StandingChangeLookup
        original = first["receipt"]["request"]
        raw = lookup(original, status="recorded", current=current_at(6), receipt=first["receipt"])
    encoded_response = encoded(raw)
    assert 131072 < len(encoded_response) <= 262144
    parsed = model.model_validate(standing_json(encoded_response, maximum=262144))
    assert len(canonical_bytes(parsed)) == len(encoded_response)
    decoder = getattr(model, "from_json", None)
    if decoder is not None:
        assert canonical_bytes(decoder(encoded_response)) == canonical_bytes(parsed)
    with pytest.raises(ERRORS):
        standing_json(encoded_response)


@pytest.mark.parametrize(
    ("model", "raw"),
    [
        (StandingRecord, record()),
        (StandingCurrent, current_at(0)),
        (StandingResponsibilitySnapshot, snapshot()),
        (StandingChangeLookup, lookup()),
        (StandingResponsibilityPage, page([])),
    ],
)
def test_new_objects_require_their_fields_are_closed_and_remain_frozen(model, raw):
    parsed = model.model_validate(raw)
    for field in raw:
        missing = copy.deepcopy(raw)
        del missing[field]
        with pytest.raises(ERRORS):
            model.model_validate(missing)
    with pytest.raises(ERRORS):
        model.model_validate({**raw, "unexpected": "synthetic"})
    with pytest.raises(ERRORS):
        parsed.schema_version = "1.1"
    for field in ("revision", "control_writable"):
        if field in raw:
            with pytest.raises(ERRORS):
                model.model_validate({**raw, field: True if field == "revision" else 1})


def test_native_exception_types_have_the_fixed_public_messages_only():
    assert str(StandingConflict()) == "Standing work conflicts with retained authority"
    assert str(StandingUnavailable()) == "Standing work authority is unavailable"
