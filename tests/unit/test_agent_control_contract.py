"""Codex-authored pure #217 codec checks; no native/Agent behavior is claimed."""

import copy
import hashlib
import json

import pytest

from packages.contracts.agent_control import (
    AGENT_STEER_PROOF_RESERVE_BYTES,
    MAX_AGENT_CONTROL_BYTES,
    MAX_AGENT_CONTROL_INTEGER,
    MAX_AGENT_CONTROL_JSON_DEPTH,
    MAX_AGENT_CONTROL_JSON_NODES,
    MAX_AGENT_CONTROL_RESPONSE_BYTES,
    MAX_AGENT_STEER_BYTES,
    MAX_AGENT_STEER_CANONICAL_BYTES,
    MAX_AGENT_STEER_RESPONSE_BYTES,
    AgentControlSnapshot,
    AgentModeChange,
    AgentModeLookupResult,
    AgentModeReceipt,
    AgentSteerAdmissionRequest,
    AgentSteerChange,
    AgentSteerLookupResult,
    AgentSteerReceipt,
    AgentTaskIdentity,
    agent_control_json,
)

BACKEND = "a" * 32
INSTANCE = "01234567-89ab-cdef-0123-456789abcdef"
OTHER_INSTANCE = "fedcba98-7654-3210-fedc-ba9876543210"
MARKER = "private-synthetic-input-marker"


def mode_change(**changes):
    return {
        "schema_version": "1.0",
        "idempotency_key": "mode:synthetic-1",
        "expected_backend": BACKEND,
        "expected_instance": INSTANCE,
        "expected_revision": 3,
        "mode": "off",
        "active_work_policy": "finish_already_accepted",
        **changes,
    }


def snapshot(**changes):
    return {
        "schema_version": "1.0",
        "backend_id": BACKEND,
        "instance_id": INSTANCE,
        "revision": 3,
        "mode": "on_demand",
        "active_work_policy": "finish_already_accepted",
        "control_writable": True,
        "available_modes": ["off", "on_demand"],
        "standing_work_available": False,
        **changes,
    }


def mode_receipt(**changes):
    return {
        "schema_version": "1.0",
        "status": "mode_saved",
        "backend_id": BACKEND,
        "service_instance": INSTANCE,
        "revision": 4,
        "request": mode_change(),
        **changes,
    }


def steer_request(**changes):
    return {
        "schema_version": "1.0",
        "expected_backend": BACKEND,
        "task": {
            "schema_version": "1.0",
            "session_id": "session-1",
            "thread_id": "thread-1",
            "epoch": 2,
            "task_id": "task-1",
            "submit_event_id": "event-submit-1",
            "submit_sequence": 10,
            "submit_idempotency_key": "original-submit-key",
            "submit_payload_digest": "b" * 64,
        },
        "actor": "owner",
        "request": {
            "schema_version": "1.0",
            "instruction": "Keep the original goal.\nAlso inspect café.py.",
            "content_scope": "public_synthetic",
            "expected_objective_revision": 1,
            "idempotency_key": "original-steer-key",
            "source_event_ids": ["source-1"],
        },
        "effective_content_scope": "local_only",
        "source_revisions": [
            {
                "schema_version": "1.0",
                "event_id": "source-1",
                "revision": 2,
                "content_scope": "private_cloud_authorized",
            }
        ],
        **changes,
    }


def steer_receipt(**changes):
    return {
        "schema_version": "1.0",
        "disposition": "created",
        "status": "intent_accepted",
        "request": steer_request(),
        "event_id": "event-steer-1",
        "sequence": 11,
        "control_revision": 3,
        "admission_mode": "on_demand",
        "target_objective_revision": 2,
        **changes,
    }


def wire(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def test_mode_request_has_exactly_seven_required_immutable_fields():
    supplied = mode_change()
    parsed = AgentModeChange.from_json(wire(supplied))
    assert set(AgentModeChange.model_fields) == set(supplied)
    assert parsed.model_dump(mode="json") == supplied
    assert parsed.expected_revision == 3
    with pytest.raises(ValueError):
        parsed.mode = "on_demand"
    for field in supplied:
        omitted = dict(supplied)
        omitted.pop(field)
        with pytest.raises(ValueError):
            AgentModeChange.from_json(wire(omitted))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "1.1"},
        {"schema_version": 1},
        {"idempotency_key": ""},
        {"idempotency_key": "x" * 129},
        {"idempotency_key": "unsafe\nkey"},
        {"idempotency_key": "unsafe/key"},
        {"expected_backend": "A" * 32},
        {"expected_backend": "a" * 31},
        {"expected_backend": "a" * 33},
        {"expected_instance": INSTANCE.upper()},
        {"expected_instance": "0123456789abcdef0123456789abcdef"},
        {"expected_instance": INSTANCE[:-1] + "g"},
        {"expected_revision": True},
        {"expected_revision": "3"},
        {"expected_revision": 3.0},
        {"expected_revision": -1},
        {"expected_revision": MAX_AGENT_CONTROL_INTEGER},
        {"expected_revision": MAX_AGENT_CONTROL_INTEGER + 1},
        {"mode": "enabled"},
        {"active_work_policy": "cancel_all"},
        {"actor": "owner"},
        {"reason": MARKER},
    ],
)
def test_mode_request_rejects_coercion_unsafe_identity_and_unknown_fields(changes):
    with pytest.raises(ValueError) as error:
        AgentModeChange.from_json(wire(mode_change(**changes)))
    assert MARKER not in str(error.value)
    assert len(str(error.value)) <= 160


@pytest.mark.parametrize(
    "raw",
    [
        b"{} trailing",
        b'{"x":1,"x":2}',
        b'{"x":{"secret":1,"secret":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1.0}',
        b'{"x":1e9999}',
        b'{"x":"\xff"}',
        b'{"x":"\\ud800"}',
        b'{"\\udfff":0}',
        b"[]",
        b"null",
        b"true",
        b'"object required"',
        b'{"x":9007199254740992}',
        b'{"x":-9007199254740992}',
        b'{"x":' + b"9" * 5000 + b"}",
    ],
)
def test_strict_json_rejects_malformed_ambiguous_nonobject_and_unsafe_numeric_data(raw):
    with pytest.raises(ValueError):
        agent_control_json(raw)


def test_json_depth_nodes_and_bytes_are_bounded_before_schema_parsing():
    deep = b'{"x":' * MAX_AGENT_CONTROL_JSON_DEPTH + b"{}" + b"}" * MAX_AGENT_CONTROL_JSON_DEPTH
    wide = wire({"x": [0] * MAX_AGENT_CONTROL_JSON_NODES})
    exact = b" " * (MAX_AGENT_CONTROL_BYTES - 2) + b"{}"
    assert agent_control_json(exact) == {}
    for raw in [deep, wide, b" " + exact]:
        with pytest.raises(ValueError):
            agent_control_json(raw)
    assert agent_control_json(b'{"x":9007199254740991}') == {"x": MAX_AGENT_CONTROL_INTEGER}
    for raw, maximum in [
        (b"{}", True),
        (b"{}", 0),
        (b"{}", MAX_AGENT_STEER_RESPONSE_BYTES + 1),
        ("{}", 20),
    ]:
        with pytest.raises(ValueError):
            agent_control_json(raw, maximum=maximum)


def test_canonical_request_identity_is_order_independent_and_keeps_every_field():
    original = AgentModeChange.model_validate(mode_change())
    reordered = AgentModeChange.model_validate(dict(reversed(list(mode_change().items()))))
    assert original.canonical_bytes() == reordered.canonical_bytes()
    assert original.request_digest() == hashlib.sha256(original.canonical_bytes()).hexdigest()
    assert original.request_digest() == reordered.request_digest()
    replacements = {
        "idempotency_key": "different-key",
        "expected_backend": "c" * 32,
        "expected_instance": OTHER_INSTANCE,
        "expected_revision": 4,
        "mode": "on_demand",
    }
    for field, value in replacements.items():
        assert (
            AgentModeChange.model_validate(mode_change(**{field: value})).request_digest()
            != original.request_digest()
        )


def test_largest_representable_next_revision_round_trips_without_float_coercion():
    request = mode_change(expected_revision=MAX_AGENT_CONTROL_INTEGER - 1)
    receipt = AgentModeReceipt.model_validate(
        mode_receipt(request=request, revision=MAX_AGENT_CONTROL_INTEGER)
    ).validate_request(request)
    restored = AgentModeReceipt.from_json(receipt.canonical_bytes())
    assert type(restored.revision) is int
    assert restored.revision == MAX_AGENT_CONTROL_INTEGER
    assert restored.request.expected_revision == MAX_AGENT_CONTROL_INTEGER - 1


def test_scalar_mode_worst_case_request_and_full_lookup_fit_transport_limits():
    request = mode_change(
        idempotency_key="x" * 128,
        expected_revision=MAX_AGENT_CONTROL_INTEGER - 1,
        mode="autonomous",
    )
    receipt = mode_receipt(request=request, revision=MAX_AGENT_CONTROL_INTEGER)
    state = snapshot(
        revision=MAX_AGENT_CONTROL_INTEGER,
        mode="autonomous",
        standing_work_available=True,
        available_modes=["off", "on_demand", "autonomous"],
    )
    lookup = {
        "schema_version": "1.0",
        "request": request,
        "status": "recorded",
        "state": state,
        "receipt": receipt,
        "mutation_fenced": True,
    }
    assert len(wire(request)) <= MAX_AGENT_CONTROL_BYTES == 4096
    assert len(wire(lookup)) <= MAX_AGENT_CONTROL_RESPONSE_BYTES == 16384
    assert (
        AgentModeLookupResult.from_json(wire(lookup)).validate_request(request).receipt.revision
        == MAX_AGENT_CONTROL_INTEGER
    )


def test_snapshot_separates_availability_from_persisted_mode_and_task_health():
    state = AgentControlSnapshot.from_json(
        wire(snapshot(mode="autonomous", control_writable=False))
    )
    assert state.mode == "autonomous"
    assert state.available_modes == ("off", "on_demand")
    assert state.standing_work_available is False
    assert state.control_writable is False
    assert "health" not in state.model_dump()
    assert "active_work" not in state.model_dump()
    enabled = AgentControlSnapshot.model_validate(
        snapshot(
            standing_work_available=True,
            available_modes=["off", "on_demand", "autonomous"],
        )
    )
    assert "autonomous" in enabled.available_modes
    for change in [
        {"available_modes": ["off", "on_demand", "autonomous"]},
        {"available_modes": ["on_demand", "off"]},
        {"available_modes": ["off", "off"]},
        {"standing_work_available": True},
        {"control_writable": 1},
    ]:
        with pytest.raises(ValueError):
            AgentControlSnapshot.model_validate(snapshot(**change))


def test_historical_mode_receipt_matches_all_original_fields_without_reapplying_mode():
    receipt = AgentModeReceipt.from_json(wire(mode_receipt()))
    assert receipt.validate_request(mode_change()).model_dump() == receipt.model_dump()
    for change in [
        {"mode": "on_demand"},
        {"idempotency_key": "different"},
        {"expected_revision": 2},
        {"expected_instance": OTHER_INSTANCE},
        {"expected_backend": "c" * 32},
    ]:
        with pytest.raises(ValueError, match="original request"):
            receipt.validate_request(mode_change(**change))
    for change in [{"revision": 5}, {"backend_id": "c" * 32}, {"service_instance": OTHER_INSTANCE}]:
        with pytest.raises(ValueError):
            AgentModeReceipt.model_validate(mode_receipt(**change))
    lookup = AgentModeLookupResult.model_validate(
        {
            "schema_version": "1.0",
            "status": "recorded",
            "request": mode_change(),
            "state": snapshot(revision=5, mode="on_demand"),
            "receipt": mode_receipt(),
            "mutation_fenced": True,
        }
    )
    assert lookup.receipt.request.mode == "off"
    assert lookup.state.mode == "on_demand"
    assert lookup.validate_request(mode_change()).state.revision == 5


@pytest.mark.parametrize(
    "state_changes,expected",
    [
        ({}, False),
        ({"mode": "off", "control_writable": False}, False),
        ({"revision": 2}, False),
        ({"revision": 4}, True),
        ({"instance_id": OTHER_INSTANCE}, True),
    ],
)
def test_mode_fence_consistency_after_native_registered_generation_proof(state_changes, expected):
    value = {
        "schema_version": "1.0",
        "status": "unobserved",
        "request": mode_change(),
        "state": snapshot(**state_changes),
        "receipt": None,
        "mutation_fenced": expected,
    }
    result = AgentModeLookupResult.model_validate(value).validate_request(mode_change())
    assert result.mutation_fenced is expected
    with pytest.raises(ValueError):
        AgentModeLookupResult.model_validate({**value, "mutation_fenced": not expected})


def test_mode_lookup_rejects_foreign_backend_inconsistent_status_and_state():
    good = {
        "schema_version": "1.0",
        "status": "recorded",
        "request": mode_change(),
        "state": snapshot(revision=4, mode="off"),
        "receipt": mode_receipt(),
        "mutation_fenced": True,
    }
    for change in [
        {"receipt": None},
        {"status": "unobserved"},
        {"mutation_fenced": False},
        {"state": snapshot(backend_id="c" * 32, revision=4, mode="off")},
        {"state": snapshot(revision=3)},
        {"state": snapshot(revision=4, mode="on_demand")},
    ]:
        with pytest.raises(ValueError):
            AgentModeLookupResult.model_validate({**good, **change})


def test_steer_request_is_deeply_frozen_and_preserves_full_original_projection():
    supplied = steer_request()
    before = copy.deepcopy(supplied)
    request = AgentSteerAdmissionRequest.from_json(wire(supplied))
    supplied["request"]["source_event_ids"].append("later")
    supplied["source_revisions"][0]["revision"] = 99
    assert request.model_dump(mode="json") == before
    assert isinstance(request.request.source_event_ids, tuple)
    assert isinstance(request.source_revisions, tuple)
    with pytest.raises(ValueError):
        request.source_revisions[0].revision = 9
    with pytest.raises(ValueError):
        request.request.instruction = "different goal"
    detached = request.model_dump(mode="json")
    detached["request"]["source_event_ids"].clear()
    assert request.request.source_event_ids == ("source-1",)
    assert AgentSteerAdmissionRequest.from_json(request.canonical_bytes()) == request


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "other-session"),
        ("thread_id", "other-thread"),
        ("epoch", 3),
        ("task_id", "other-task"),
        ("submit_event_id", "other-event"),
        ("submit_sequence", 9),
        ("submit_idempotency_key", "other-key"),
        ("submit_payload_digest", "c" * 64),
    ],
)
def test_same_task_id_cannot_substitute_other_initial_submit_scope(field, value):
    receipt = AgentSteerReceipt.model_validate(steer_receipt())
    changed = steer_request()
    changed["task"][field] = value
    with pytest.raises(ValueError, match="original request"):
        receipt.validate_request(changed)


def test_steer_receipt_full_request_matching_includes_actor_text_sources_scope_and_revision():
    receipt = AgentSteerReceipt.model_validate(steer_receipt())
    variants = []
    for field, value in [
        ("instruction", "Different constraint"),
        ("expected_objective_revision", 2),
        ("idempotency_key", "different"),
        ("content_scope", "local_only"),
    ]:
        changed = steer_request()
        changed["request"][field] = value
        variants.append(changed)
    changed = steer_request(actor="decisions")
    variants.append(changed)
    changed = steer_request()
    changed["source_revisions"][0]["revision"] = 3
    variants.append(changed)
    variants.append(steer_request(expected_backend="c" * 32))
    variants.append(steer_request(effective_content_scope="private_cloud_authorized"))
    for changed in variants:
        with pytest.raises(ValueError, match="original request"):
            receipt.validate_request(changed)


def test_steer_sources_are_exact_ordered_observations_and_disclosure_cannot_widen():
    for changes in [
        {"source_revisions": []},
        {"effective_content_scope": "public_synthetic"},
        {
            "source_revisions": [
                {**steer_request()["source_revisions"][0], "event_id": "other-source"}
            ]
        },
    ]:
        with pytest.raises(ValueError):
            AgentSteerAdmissionRequest.model_validate(steer_request(**changes))
    repeated = steer_request()
    repeated["request"]["source_event_ids"] *= 2
    repeated["source_revisions"] *= 2
    assert AgentSteerAdmissionRequest.model_validate(repeated).request.source_event_ids == (
        "source-1",
        "source-1",
    )
    repeated["source_revisions"][1] = {**repeated["source_revisions"][1], "revision": 9}
    with pytest.raises(ValueError):
        AgentSteerAdmissionRequest.model_validate(repeated)
    empty = steer_request()
    empty["request"]["source_event_ids"] = []
    empty["source_revisions"] = []
    assert AgentSteerAdmissionRequest.model_validate(empty).actor == "owner"
    with pytest.raises(ValueError):
        AgentSteerAdmissionRequest.model_validate({**empty, "actor": "decisions"})


@pytest.mark.parametrize(
    "changes",
    [
        {"instruction": ""},
        {"instruction": "x" * 16001},
        {"source_event_ids": ["x"] * 33},
        {"source_event_ids": ["unsafe\0source"]},
        {"expected_objective_revision": True},
        {"expected_objective_revision": MAX_AGENT_CONTROL_INTEGER},
        {"idempotency_key": "x" * 257},
    ],
)
def test_steer_original_request_bounds(changes):
    with pytest.raises(ValueError):
        AgentSteerChange.model_validate({**steer_request()["request"], **changes})


def test_invalid_task_identity_and_constructed_models_fail_closed():
    for changes in [
        {"epoch": False},
        {"submit_sequence": 0},
        {"task_id": "unsafe\0identity"},
        {"submit_idempotency_key": "\0"},
        {"submit_payload_digest": "F" * 64},
    ]:
        with pytest.raises(ValueError):
            AgentTaskIdentity.model_validate({**steer_request()["task"], **changes})
    valid = AgentModeChange.model_validate(mode_change())
    invalid = valid.model_copy(update={"expected_revision": True})
    with pytest.raises(ValueError):
        invalid.canonical_bytes()
    good = AgentSteerReceipt.model_validate(steer_receipt())
    invalid_receipt = good.model_copy(update={"target_objective_revision": True})
    with pytest.raises(ValueError, match="original request"):
        invalid_receipt.validate_request(steer_request())


def test_invalid_constructed_model_cannot_emit_input_in_serialization_warning(recwarn):
    invalid = AgentModeChange.model_construct(**mode_change(expected_revision=MARKER))
    with pytest.raises(ValueError) as error:
        invalid.canonical_bytes()
    assert MARKER not in str(error.value)
    assert list(recwarn) == []


def test_steer_intent_receipt_never_claims_applied_and_lookup_never_grants_created():
    receipt = AgentSteerReceipt.from_json(wire(steer_receipt()))
    assert receipt.status == "intent_accepted"
    assert receipt.target_objective_revision == 2
    for changes in [
        {"status": "applied"},
        {"target_objective_revision": 3},
        {"event_id": "event-submit-1"},
        {"sequence": 10},
        {"admission_mode": "off"},
    ]:
        with pytest.raises(ValueError):
            AgentSteerReceipt.model_validate(steer_receipt(**changes))
    observed = {
        "schema_version": "1.0",
        "status": "recorded",
        "request": steer_request(),
        "receipt": steer_receipt(disposition="retained"),
    }
    result = AgentSteerLookupResult.from_json(wire(observed)).validate_request(steer_request())
    assert result.receipt.disposition == "retained"
    with pytest.raises(ValueError):
        AgentSteerLookupResult.model_validate({**observed, "receipt": steer_receipt()})
    negative = {**observed, "status": "unobserved", "receipt": None}
    assert AgentSteerLookupResult.model_validate(negative).receipt is None
    with pytest.raises(ValueError):
        AgentSteerLookupResult.model_validate({**negative, "mutation_fenced": True})


def test_aggregate_steer_bytes_and_error_reasons_are_bounded():
    oversized = steer_request()
    oversized["request"]["instruction"] = "x" * (MAX_AGENT_STEER_BYTES + 1) + MARKER
    with pytest.raises(ValueError) as error:
        AgentSteerAdmissionRequest.from_json(wire(oversized))
    assert MARKER not in str(error.value)
    assert len(str(error.value)) <= 160
    malformed = steer_request()
    malformed["request"]["instruction"] = "\ud800"
    with pytest.raises(ValueError):
        AgentSteerAdmissionRequest.model_validate(malformed)


@pytest.mark.parametrize("identity_char", ["\U0001f600", "\x01"])
def test_maximal_canonical_steer_request_closes_under_twice_echoed_full_proof(identity_char):
    # Native IDs preserve controls other than NUL. Both four-byte UTF-8 and
    # six-byte JSON control escaping must fit; original steer text/key may NUL.
    identity = identity_char * 128
    original = steer_request(actor="decisions")
    for field in ["session_id", "thread_id", "task_id", "submit_event_id"]:
        original["task"][field] = identity
    original["task"].update(
        epoch=MAX_AGENT_CONTROL_INTEGER,
        submit_sequence=MAX_AGENT_CONTROL_INTEGER - 1,
        submit_idempotency_key=identity_char * 256,
    )
    original["request"].update(
        instruction="",
        idempotency_key="\0" * 256,
        expected_objective_revision=MAX_AGENT_CONTROL_INTEGER - 1,
        source_event_ids=[identity] * 32,
        content_scope="private_cloud_authorized",
    )
    original["source_revisions"] = [
        {
            "schema_version": "1.0",
            "event_id": identity,
            "revision": MAX_AGENT_CONTROL_INTEGER,
            "content_scope": "private_cloud_authorized",
        }
        for _ in range(32)
    ]
    fixed = len(json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    remaining = MAX_AGENT_STEER_CANONICAL_BYTES - fixed
    original["request"]["instruction"] = "\0" * (remaining // 6) + "x" * (remaining % 6)
    assert len(original["request"]["instruction"]) < 16000
    assert AGENT_STEER_PROOF_RESERVE_BYTES == 1024
    assert len(wire(original)) <= MAX_AGENT_STEER_BYTES
    parsed = AgentSteerAdmissionRequest.from_json(wire(original))
    assert len(parsed.canonical_bytes()) == MAX_AGENT_STEER_CANONICAL_BYTES
    receipt = steer_receipt(
        request=original,
        disposition="retained",
        event_id=("\U0001f601" if identity_char == "\U0001f600" else "\x02") * 128,
        sequence=MAX_AGENT_CONTROL_INTEGER,
        control_revision=MAX_AGENT_CONTROL_INTEGER,
        admission_mode="autonomous",
        target_objective_revision=MAX_AGENT_CONTROL_INTEGER,
    )
    lookup = {
        "schema_version": "1.0",
        "status": "recorded",
        "request": original,
        "receipt": receipt,
    }
    assert len(wire(lookup)) <= MAX_AGENT_STEER_RESPONSE_BYTES
    result = AgentSteerLookupResult.from_json(wire(lookup)).validate_request(original)
    assert result.receipt.target_objective_revision == MAX_AGENT_CONTROL_INTEGER
    oversized = copy.deepcopy(original)
    oversized["request"]["instruction"] += "x"
    with pytest.raises(ValueError):
        AgentSteerAdmissionRequest.from_json(wire(oversized))


def test_original_steer_nul_and_native_id_controls_round_trip_without_sanitization():
    original = steer_request()
    original["task"]["submit_idempotency_key"] = "native\nkey\x7f"
    original["task"]["session_id"] = "native\nsession\x7f"
    original["request"]["instruction"] = "literal\0instruction\n\x01"
    original["request"]["idempotency_key"] = "literal\0key\n\x7f"
    original["request"]["source_event_ids"] = ["native\nsource\x7f"]
    original["source_revisions"][0]["event_id"] = "native\nsource\x7f"
    parsed = AgentSteerAdmissionRequest.from_json(wire(original))
    assert parsed.model_dump(mode="json") == original
    receipt = AgentSteerReceipt.model_validate(steer_receipt(request=original))
    assert receipt.validate_request(original).request == parsed
    sanitized = copy.deepcopy(original)
    sanitized["request"]["instruction"] = sanitized["request"]["instruction"].replace("\0", "")
    with pytest.raises(ValueError, match="original request"):
        receipt.validate_request(sanitized)
