"""Public entry/digest compatibility gate frozen before validation-reuse source.

The old execution vectors supply complete synthetic claim graphs and their
stdlib hash/projection oracles. These tests grant no native admission/provenance.
No test depends on the proposed private helper or counts internal validations.
"""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from test_standing_execution_contract import (
    GOLDEN_INITIAL,
    OTHER,
    SAFE,
    claim,
    initial_expected,
    intent,
    intent_at_initial_size,
)

from packages.contracts.models import JournalEntry
from packages.contracts.standing_admission import standing_entry, standing_entry_digest
from packages.contracts.standing_execution import StandingExecutionClaim

EVENT = "00000000-0000-0000-0000-000000000777"
CREATED = "2026-11-01T08:30:01.001000Z"
PRIVATE = "SYNTHETIC-PRIVATE-ENTRY-INPUT"
ERROR = "Invalid standing initial proof"
GOLDEN_DIGEST = "c3129fef6eff8b209d811fc1c1e0c79b4f196b58329a0afc74ca9af22b7b5f58"
ENTRY_FIELDS = (
    "schema_version",
    "session_id",
    "thread_id",
    "epoch",
    "kind",
    "payload",
    "causes",
    "task_refs",
    "idempotency_key",
    "event_id",
    "sequence",
    "origin",
    "created_at",
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def native_wire(value):
    # Native model field order, with detached, sorted JSON payload maps. This
    # uses only the wire grammar and stdlib, not a production entry serializer.
    detached = json.loads(canonical(value))
    ordered = {key: detached[key] for key in ENTRY_FIELDS}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )


def expected_digest(record, entry):
    body = {
        "claim_id": record["claim_id"],
        "claim_digest": record["claim_digest"],
        "entry": entry,
    }
    return hashlib.sha256(b"kinaiya.standing.initial.v1\0" + canonical(body)).hexdigest()


def raw_entry(record=None, *, event=EVENT, sequence=17, created=CREATED):
    return {
        **initial_expected(claim() if record is None else record),
        "event_id": event,
        "sequence": sequence,
        "origin": "standing-supervisor",
        "created_at": created,
    }


def typed_entry(raw):
    values = copy.deepcopy(raw)
    values["created_at"] = datetime.fromisoformat(values["created_at"].replace("Z", "+00:00"))
    return JournalEntry.model_validate(values)


def typed_claim(record=None):
    return StandingExecutionClaim.model_validate(claim() if record is None else record)


def refuses(operation, *args):
    with pytest.raises(ValueError) as caught:
        operation(*args)
    assert type(caught.value) is ValueError
    assert str(caught.value) == ERROR
    assert PRIVATE not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def equivalent(supplied, record, retained, expected):
    result = standing_entry(supplied, retained)
    assert type(result) is JournalEntry and result is not supplied
    assert result.model_dump(mode="json") == expected
    assert result.model_dump_json().encode("utf-8") == native_wire(expected)
    assert standing_entry_digest(
        retained, expected["event_id"], expected["sequence"], expected["created_at"]
    ) == expected_digest(record, expected)
    return result


def edit_entry(value, path, replacement):
    current = value.__dict__ if type(value) is JournalEntry else value
    for name in path[:-1]:
        current = current[name]
    current[path[-1]] = replacement


def reverse_maps(value):
    if type(value) is dict:
        return {key: reverse_maps(value[key]) for key in reversed(value)}
    if type(value) is list:
        return [reverse_maps(item) for item in value]
    return value


@pytest.mark.parametrize("form", ("raw", "typed"))
@pytest.mark.parametrize(
    "created",
    (
        "0001-01-01T00:00:00Z",
        "2000-02-29T23:59:59Z",
        "2026-11-01T08:30:01Z",
        "2026-11-01T08:30:01.000001Z",
        CREATED,
        "9999-12-31T23:59:59.999999Z",
    ),
)
def test_full_native_bytes_and_stdlib_digest_for_typed_raw_and_utc_endpoints(form, created):
    record = claim()
    retained = typed_claim(record)
    expected = raw_entry(record, created=created)
    supplied = copy.deepcopy(expected) if form == "raw" else typed_entry(expected)
    result = equivalent(supplied, record, retained, expected)
    result.payload["model_pin"]["catalog_binding"]["generation"] = 999
    result.task_refs.clear()
    assert retained.model_dump(mode="json") == record
    assert (supplied if form == "raw" else supplied.model_dump(mode="json")) == expected
    equivalent(supplied, record, retained, expected)


def test_literal_golden_digest_order_independence_and_no_returned_payload_alias():
    record = claim()
    assert initial_expected(record) == GOLDEN_INITIAL
    expected = raw_entry(record)
    assert expected_digest(record, expected) == GOLDEN_DIGEST
    supplied = reverse_maps(expected)
    retained = typed_claim(record)
    result = equivalent(supplied, record, retained, expected)
    supplied["payload"]["workspace_pin"]["root_identity"] = "0" * 64
    assert result.model_dump(mode="json") == expected
    refuses(standing_entry, supplied, retained)
    assert standing_entry_digest(retained, EVENT, 17, CREATED) == GOLDEN_DIGEST


@pytest.mark.parametrize(
    "objective", ("Café / e\u0301 / 🧭 / 中文", "Cafe\u0301 / é / 🧭 / 中文", 'x\0y\n\t\\"')
)
def test_unicode_and_control_characters_are_preserved_without_normalization(objective):
    original = intent()
    original["task"]["objective"] = objective
    record = claim(original)
    retained = typed_claim(record)
    expected = raw_entry(record, event="é🧭" * 64, sequence=SAFE)
    result = equivalent(typed_entry(expected), record, retained, expected)
    assert result.payload["objective"] == objective
    assert result.event_id == "é🧭" * 64 and result.sequence == SAFE
    assert expected_digest(record, expected) != GOLDEN_DIGEST


def test_valid_later_metadata_and_new_complete_claim_never_reuse_an_earlier_digest():
    record = claim()
    retained = typed_claim(record)
    first = raw_entry(record)
    supplied = typed_entry(first)
    first_result = equivalent(supplied, record, retained, first)
    second = raw_entry(
        record, event="second-native-event", sequence=18, created="2026-11-01T08:30:02Z"
    )
    supplied.__dict__.update(
        event_id=second["event_id"],
        sequence=second["sequence"],
        created_at=datetime(2026, 11, 1, 8, 30, 2, tzinfo=UTC),
    )
    equivalent(supplied, record, retained, second)
    assert first_result.model_dump(mode="json") == first
    assert expected_digest(record, first) != expected_digest(record, second)
    other_record = claim(prepared="2026-11-01T08:30:02Z")
    other_retained = typed_claim(other_record)
    other_entry = raw_entry(other_record)
    refuses(standing_entry, first, other_retained)
    refuses(standing_entry, other_entry, retained)
    equivalent(other_entry, other_record, other_retained, other_entry)
    assert expected_digest(other_record, other_entry) != GOLDEN_DIGEST


@pytest.mark.parametrize(
    "path,key,operation,replacement",
    (
        ((), "extra", "set", PRIVATE),
        (("request",), "extra", "set", PRIVATE),
        (("request", "task"), "extra", "set", PRIVATE),
        (("request", "task", "model_pin"), "extra", "set", PRIVATE),
        (("request", "task", "model_pin", "catalog_binding"), "extra", "set", PRIVATE),
        (("request", "task", "workspace_pin"), "extra", "set", PRIVATE),
        (("request", "producer", "request"), "extra", "set", PRIVATE),
        ((), "request", "remove", None),
        (("request", "task"), "tool_subset", "remove", None),
        (("request", "task", "model_pin"), "reasoning", "remove", None),
        (("request", "task", "model_pin"), "local", "set", 1),
        (("request", "task", "task_session"), "epoch", "set", True),
        (("request", "producer", "request"), "sequence", "set", 2.0),
        ((), "claim_digest", "set", "0" * 64),
        (("request", "task"), "objective", "set", PRIVATE),
        (("request", "task", "model_pin", "catalog_binding"), "generation", "set", 4),
        (("request", "task", "model_pin"), "extra", "field-set", PRIVATE),
        (("request", "task", "workspace_pin"), "extra", "pydantic-extra", PRIVATE),
    ),
)
def test_both_public_calls_freshly_reject_mutated_actual_claims_after_a_valid_call(
    path, key, operation, replacement
):
    record = claim()
    retained = typed_claim(record)
    expected = raw_entry(record)
    equivalent(expected, record, retained, expected)
    target = retained
    for name in path:
        target = getattr(target, name)
    if operation == "remove":
        target.__dict__.pop(key)
    elif operation == "field-set":
        target.__pydantic_fields_set__.add(key)
    elif operation == "pydantic-extra":
        object.__setattr__(target, "__pydantic_extra__", {key: replacement})
    else:
        target.__dict__[key] = replacement
    refuses(standing_entry, expected, retained)
    refuses(standing_entry_digest, retained, EVENT, 17, CREATED)
    fresh = typed_claim(record)
    equivalent(expected, record, fresh, expected)


@pytest.mark.parametrize(
    "path,replacement",
    (
        (("session_id",), "foreign-session"),
        (("thread_id",), "foreign-thread"),
        (("epoch",), 4),
        (("kind",), "agent.command"),
        (("origin",), "agent-supervisor"),
        (("schema_version",), "1.1"),
        (("idempotency_key",), "foreign-native-key"),
        (("task_refs",), [OTHER]),
        (("causes",), ["synthetic-source"]),
        (("payload", "actor"), "owner"),
        (("payload", "objective"), PRIVATE),
        (("payload", "objective_revision"), True),
        (("payload", "source_revision"), True),
        (("payload", "source_event_ids"), ["synthetic-source"]),
        (("payload", "requested_content_scope"), "local_only"),
        (("payload", "model_pin", "local"), 1),
        (("payload", "model_pin", "model"), "foreign-model"),
        (("payload", "workspace_pin", "root_identity"), "0" * 64),
        (("payload", "standing", "capability_sha256"), "0" * 64),
        (("payload", "standing", "producer_id"), OTHER),
    ),
)
def test_foreign_entry_joins_and_bool_int_changes_are_refused_in_raw_and_actual_typed_graphs(
    path, replacement
):
    record = claim()
    retained = typed_claim(record)
    expected = raw_entry(record)
    for supplied in (copy.deepcopy(expected), typed_entry(expected)):
        edit_entry(supplied, path, copy.deepcopy(replacement))
        refuses(standing_entry, supplied, retained)
    assert standing_entry_digest(retained, EVENT, 17, CREATED) == GOLDEN_DIGEST


@pytest.mark.parametrize(
    "case",
    (
        "tuple-causes",
        "tuple-source-events",
        "typed-model-pin",
        "typed-catalog-binding",
        "float",
        "nan",
        "non-string-key",
        "object",
        "bytes",
        "invalid-unicode",
        "depth13",
        "nodes2049",
        "cycle",
    ),
)
def test_actual_nonjson_graphs_and_aggregate_bounds_cannot_be_normalized_before_refusal(case):
    record = claim()
    retained = typed_claim(record)
    expected = raw_entry(record)
    path = ("payload", "objective")
    if case == "tuple-causes":
        path, replacement = ("causes",), ()
    elif case == "tuple-source-events":
        path, replacement = ("payload", "source_event_ids"), ()
    elif case == "typed-model-pin":
        path, replacement = ("payload", "model_pin"), retained.request.task.model_pin
    elif case == "typed-catalog-binding":
        path = ("payload", "model_pin", "catalog_binding")
        replacement = retained.request.task.model_pin.catalog_binding
    elif case == "depth13":
        replacement = PRIVATE
        for _ in range(13):
            replacement = [replacement]
    elif case == "nodes2049":
        replacement = [PRIVATE] * 2049
    elif case == "cycle":
        replacement = []
        replacement.append(replacement)
    else:
        replacement = {
            "float": 1.0,
            "nan": float("nan"),
            "non-string-key": {1: PRIVATE},
            "object": object(),
            "bytes": PRIVATE.encode(),
            "invalid-unicode": PRIVATE + "\ud800",
        }[case]
    for supplied in (copy.deepcopy(expected), typed_entry(expected)):
        edit_entry(supplied, path, replacement)
        refuses(standing_entry, supplied, retained)


def test_entry_field_closure_and_actual_model_metadata_are_not_dumped_away():
    record = claim()
    retained = typed_claim(record)
    expected = raw_entry(record)
    for field in ENTRY_FIELDS:
        raw = copy.deepcopy(expected)
        raw.pop(field)
        refuses(standing_entry, raw, retained)
        native = typed_entry(expected)
        native.__dict__.pop(field)
        refuses(standing_entry, native, retained)
    for path in ((), ("payload",), ("payload", "standing"), ("payload", "model_pin")):
        for supplied in (copy.deepcopy(expected), typed_entry(expected)):
            current = supplied.__dict__ if type(supplied) is JournalEntry else supplied
            for key in path:
                current = current[key]
            current["extra"] = PRIVATE
            refuses(standing_entry, supplied, retained)
    native = typed_entry(expected)
    native.__pydantic_fields_set__.add(PRIVATE)
    refuses(standing_entry, native, retained)
    native = typed_entry(expected)
    object.__setattr__(native, "__pydantic_extra__", {PRIVATE: True})
    refuses(standing_entry, native, retained)


class EntrySubclass(JournalEntry):
    pass


class DictSubclass(dict):
    pass


def test_lookalike_entries_and_raw_claims_do_not_gain_typed_native_identity():
    record = claim()
    retained = typed_claim(record)
    expected = raw_entry(record)
    for supplied in (
        None,
        [],
        True,
        DictSubclass(expected),
        EntrySubclass(**typed_entry(expected).__dict__),
    ):
        refuses(standing_entry, supplied, retained)
    for supplied in (None, True, record, retained.request):
        refuses(standing_entry, expected, supplied)
        refuses(standing_entry_digest, supplied, EVENT, 17, CREATED)


@pytest.mark.parametrize(
    "field,replacement",
    (
        ("event_id", ""),
        ("event_id", "x" * 129),
        ("event_id", PRIVATE + "\0"),
        ("event_id", PRIVATE + "\ud800"),
        ("event_id", True),
        ("sequence", 0),
        ("sequence", -1),
        ("sequence", SAFE + 1),
        ("sequence", True),
        ("sequence", 17.0),
        ("sequence", "17"),
        ("created_at", "1900-02-29T00:00:00Z"),
        ("created_at", "2026-11-01T08:30:60Z"),
        ("created_at", "2026-11-01T08:30:01+00:00"),
        ("created_at", "2026-11-01T08:30:01.001Z"),
        ("created_at", "2026-11-01T08:30:01.000000Z"),
        ("created_at", "2026-11-01T08:30:01Z\n"),
        ("created_at", "0000-01-01T00:00:00Z"),
        ("created_at", PRIVATE),
        ("created_at", None),
    ),
)
def test_metadata_rejection_and_input_free_errors_match_at_both_public_boundaries(
    field, replacement
):
    record = claim()
    retained = typed_claim(record)
    expected = raw_entry(record)
    for supplied in (copy.deepcopy(expected), typed_entry(expected)):
        edit_entry(supplied, (field,), replacement)
        refuses(standing_entry, supplied, retained)
    args = {"event_id": EVENT, "sequence": 17, "created_at": CREATED, field: replacement}
    refuses(standing_entry_digest, retained, args["event_id"], args["sequence"], args["created_at"])


def test_actual_native_datetime_is_utc_aware_and_raw_time_is_never_coerced():
    retained = typed_claim()
    expected = raw_entry()
    for created in (
        CREATED,
        datetime(2026, 11, 1),
        datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=1))),
    ):
        native = typed_entry(expected)
        native.__dict__["created_at"] = created
        refuses(standing_entry, native, retained)
    native = typed_entry(expected)
    native.__dict__["created_at"] = datetime(
        2026, 11, 1, 8, 30, 1, 1000, tzinfo=timezone(timedelta(0), "synthetic-utc")
    )
    equivalent(native, claim(), retained, expected)
    raw = raw_entry()
    raw["created_at"] = datetime(2026, 11, 1, 8, 30, 1, 1000, tzinfo=UTC)
    refuses(standing_entry, raw, retained)
    refuses(standing_entry_digest, retained, EVENT, 17, raw["created_at"])


@pytest.mark.parametrize("request_size", (65535, 65536, 65537))
def test_exact_existing_request_byte_limit_is_not_bypassed_by_public_digest(request_size):
    record = claim(intent_at_initial_size(request_size))
    retained = typed_claim(record)
    expected = raw_entry(record)
    # The old independent fixture measures the native AppendRequest serializer.
    # For this public synthetic graph stdlib UTF-8 has the same exact size.
    assert len(canonical(initial_expected(record))) == request_size
    if request_size > 65536:
        refuses(standing_entry, expected, retained)
        refuses(standing_entry_digest, retained, EVENT, 17, CREATED)
    else:
        result = equivalent(expected, record, retained, expected)
        assert len(result.model_dump_json().encode("utf-8")) <= 69632
        assert result.payload["objective"] == record["request"]["task"]["objective"]


def test_oversized_actual_entry_is_refused_without_clipping_or_mutating_input():
    retained = typed_claim()
    expected = raw_entry()
    oversized = copy.deepcopy(expected)
    oversized["payload"]["objective"] = PRIVATE + "\0" * 12000
    before = canonical(oversized)
    assert len(before) > 69632
    for supplied in (oversized, typed_entry(oversized)):
        refuses(standing_entry, supplied, retained)
    assert canonical(oversized) == before
    equivalent(expected, claim(), retained, expected)
