"""Public Resources compatibility oracles frozen before local-join reuse.

Old independent vectors provide complete synthetic native-identity graphs and
literal hashes. This is packet grammar, never native persistence/cleanup proof.
No test mirrors a private validator or counts its internal validations.
"""

import copy
import hashlib
import json

import pytest
from test_standing_execution_contract import GOLDEN_INTENT, OTHER
from test_standing_resources_contract import GOLDEN_HASHES, vectors

from packages.contracts.agent_control import AgentTaskIdentity
from packages.contracts.standing_execution import StandingExecutionIntent
from packages.contracts.standing_resources import (
    StandingResourceObservation,
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    TaskResourceContext,
    TaskResourceQuery,
    checked_resource,
    standing_resource_digest,
    standing_resource_observation_digest,
    task_resource_context_digest,
)

PRIVATE = "SYNTHETIC-PRIVATE-RESOURCE-INPUT"
ERROR = "Invalid standing Resources contract"
SAFE = 9_007_199_254_740_991
TARGETS = (
    ("TaskResourceContext", TaskResourceContext, 180224, "context_digest"),
    (
        "StandingResourceReservationReceipt",
        StandingResourceReservationReceipt,
        212992,
        "request_digest",
    ),
    ("StandingResourceObservation", StandingResourceObservation, 212992, "observation_digest"),
)
DOMAINS = {
    "TaskResourceContext": b"kinaiya.standing.resource-context.v1",
    "StandingResourceReservationReceipt": b"kinaiya.standing.resource-reservation.v1",
    "StandingResourceObservation": b"kinaiya.standing.resource-observation.v1",
}


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(name, body):
    return hashlib.sha256(DOMAINS[name] + b"\0" + encoded(body)).hexdigest()


def without_digest(raw, field):
    return {key: value for key, value in raw.items() if key != field}


def original_for(name):
    if name == "TaskResourceContext":
        return TaskResourceQuery.model_validate(vectors()["TaskResourceQuery"])
    if name == "StandingResourceReservationReceipt":
        return StandingResourceReservation.model_validate(vectors()["StandingResourceReservation"])
    return StandingExecutionIntent.model_validate(copy.deepcopy(GOLDEN_INTENT))


def context_of(name, value):
    if name == "TaskResourceContext":
        return value
    if name == "StandingResourceReservationReceipt":
        return value.request.context
    return value.reservation.context


def public_digest(name, value):
    if name == "TaskResourceContext":
        return task_resource_context_digest(
            value.query, value.backend_id, value.family, value.standing, value.ordinary
        )
    if name == "StandingResourceReservationReceipt":
        return standing_resource_digest(value.request)
    return standing_resource_observation_digest(
        without_digest(value.__dict__, "observation_digest")
    )


def refuses(operation, *args):
    with pytest.raises(ValueError) as caught:
        operation(*args)
    assert type(caught.value) is ValueError and str(caught.value) == ERROR
    assert PRIVATE not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def refuses_model(kind, value):
    # Native Pydantic construction retains its standard ValidationError wrapper.
    with pytest.raises(ValueError) as caught:
        kind.model_validate(value)
    assert PRIVATE not in str(caught.value) and caught.value.__cause__ is None


def ordinary_body():
    return {
        "schema_version": "1.0",
        "query": {"schema_version": "1.0", "task_id": "ordinary_task"},
        "backend_id": "a" * 32,
        "family": "ordinary",
        "standing": None,
        "ordinary": {
            "schema_version": "1.0",
            "session_id": "synthetic-session-é",
            "thread_id": "synthetic-thread-🧭",
            "epoch": 1,
            "task_id": "ordinary_task",
            "submit_event_id": "synthetic-native-event",
            "submit_sequence": 19,
            "submit_idempotency_key": "synthetic-ordinary-key",
            "submit_payload_digest": "a" * 64,
        },
    }


@pytest.mark.parametrize("name,kind,maximum,field", TARGETS)
@pytest.mark.parametrize("form", ("raw", "typed"))
def test_complete_typed_raw_json_bytes_literal_hashes_and_returned_detachment(
    name, kind, maximum, field, form
):
    raw = copy.deepcopy(vectors()[name])
    before = encoded(raw)
    supplied = copy.deepcopy(raw) if form == "raw" else kind.model_validate(copy.deepcopy(raw))
    value = kind.model_validate(supplied)
    checked = checked_resource(value, kind)
    joined = value.validate_request(original_for(name))
    assert checked is not value and joined is not value
    assert kind.wire_bytes == maximum
    for result in (value, checked, joined, kind.from_json(before)):
        assert type(result) is kind and result.canonical_bytes() == before
        assert public_digest(name, result) == GOLDEN_HASHES[field] == raw[field]
    body = (
        raw["request"]
        if name == "StandingResourceReservationReceipt"
        else without_digest(raw, field)
    )
    assert digest(name, body) == raw[field]
    if form == "raw":
        supplied.clear()
    else:
        context_of(name, supplied).query.__dict__["task_id"] = "changed-after-return"
    assert (
        value.canonical_bytes() == checked.canonical_bytes() == joined.canonical_bytes() == before
    )


@pytest.mark.parametrize("family", ("standing", "ordinary"))
@pytest.mark.parametrize("nested_form", ("raw", "typed"))
def test_public_context_digest_rebuilds_full_standing_or_ordinary_typed_raw_children(
    family, nested_form
):
    body = (
        without_digest(vectors()["TaskResourceContext"], "context_digest")
        if family == "standing"
        else ordinary_body()
    )
    expected = digest("TaskResourceContext", body)
    raw = {**copy.deepcopy(body), "context_digest": expected}
    value = TaskResourceContext.model_validate(raw)
    standing = body["standing"] if nested_form == "raw" else value.standing
    ordinary = body["ordinary"] if nested_form == "raw" else value.ordinary
    assert (
        task_resource_context_digest(value.query, value.backend_id, family, standing, ordinary)
        == expected
    )
    assert value.validate_request(value.query).canonical_bytes() == encoded(raw)
    assert raw["standing"] is None if family == "ordinary" else raw["ordinary"] is None
    if family == "ordinary":
        # Ordinary metadata remains genuine ordinary family, never a standing reservation.
        refuses_model(
            StandingResourceReservation,
            {**vectors()["StandingResourceReservation"], "context": value},
        )


@pytest.mark.parametrize(
    "coverage_sequence,accepted,reserved,coverage",
    (
        (0, 17, False, "accepted-after-bootstrap"),
        (16, 17, True, "accepted-after-bootstrap"),
        (17, 17, True, "legacy-or-unadmitted"),
        (17, 17, False, "legacy-or-unadmitted"),
        (18, None, False, "legacy-or-unadmitted"),
        (SAFE, SAFE, False, "legacy-or-unadmitted"),
    ),
)
def test_coverage_relations_explicit_nulls_and_optional_reservation_keep_full_hash_body(
    coverage_sequence, accepted, reserved, coverage
):
    body = without_digest(vectors()["StandingResourceObservation"], "observation_digest")
    body.update(
        coverage_sequence=coverage_sequence,
        accepted_sequence=accepted,
        coverage=coverage,
        reservation=body["reservation"] if reserved else None,
    )
    expected = digest("StandingResourceObservation", body)
    value = StandingResourceObservation.model_validate({**body, "observation_digest": expected})
    assert value.validate_request(original_for("StandingResourceObservation")) == value
    assert value.canonical_bytes() == encoded({**body, "observation_digest": expected})
    assert standing_resource_observation_digest(body) == expected
    typed_body = without_digest(value.__dict__, "observation_digest")
    assert standing_resource_observation_digest(typed_body) == expected


@pytest.mark.parametrize("name,kind,maximum,field", TARGETS)
@pytest.mark.parametrize(
    "mutation",
    ("extra", "missing", "fieldset", "missing-fieldset", "pydantic-extra", "nested-extra"),
)
def test_public_methods_revalidate_actual_graph_and_metadata_after_prior_valid_use(
    name, kind, maximum, field, mutation
):
    raw = copy.deepcopy(vectors()[name])
    value = kind.model_validate(raw)
    original = original_for(name)
    assert value.validate_request(original).canonical_bytes() == encoded(raw)
    assert public_digest(name, value) == raw[field]
    if mutation == "extra":
        value.__dict__[PRIVATE] = True
    elif mutation == "missing":
        value.__dict__.pop(field)
    elif mutation == "fieldset":
        value.__pydantic_fields_set__.add(PRIVATE)
    elif mutation == "missing-fieldset":
        object.__delattr__(value, "__pydantic_fields_set__")
    elif mutation == "pydantic-extra":
        object.__setattr__(value, "__pydantic_extra__", {PRIVATE: True})
    else:
        context_of(name, value).standing.claim.request.task.model_pin.__dict__[PRIVATE] = True
    refuses_model(kind, value)
    refuses(value.canonical_bytes)
    refuses(checked_resource, value, kind)
    refuses(value.validate_request, original)
    if mutation == "nested-extra":
        refuses(public_digest, name, value)
    assert kind.from_json(encoded(raw)).canonical_bytes() == encoded(raw)


@pytest.mark.parametrize("mutation", ("extra", "missing", "bool-epoch", "fieldset", "subclass"))
def test_older_ordinary_identity_actual_fields_are_rebuilt_at_public_context_boundary(mutation):
    body = ordinary_body()
    value = TaskResourceContext.model_validate(
        {**body, "context_digest": digest("TaskResourceContext", body)}
    )
    assert type(value.ordinary) is AgentTaskIdentity
    assert public_digest("TaskResourceContext", value) == value.context_digest
    if mutation == "extra":
        value.ordinary.__dict__[PRIVATE] = True
    elif mutation == "missing":
        value.ordinary.__dict__.pop("submit_event_id")
    elif mutation == "bool-epoch":
        value.ordinary.__dict__["epoch"] = True
    elif mutation == "fieldset":
        value.ordinary.__pydantic_fields_set__.add(PRIVATE)
    else:
        subclass = type("SyntheticOrdinarySubclass", (AgentTaskIdentity,), {})
        value.__dict__["ordinary"] = subclass.model_construct(**value.ordinary.__dict__)
    refuses(public_digest, "TaskResourceContext", value)
    refuses(value.canonical_bytes)
    refuses(value.validate_request, value.query)


@pytest.mark.parametrize("name,kind,maximum,field", TARGETS)
def test_actual_nested_initial_subclass_cannot_gain_supported_resource_identity(
    name, kind, maximum, field
):
    value = kind.model_validate(vectors()[name])
    context = context_of(name, value)
    subclass = type("SyntheticInitialSubclass", (type(context.standing),), {})
    context.__dict__["standing"] = subclass.model_construct(**context.standing.__dict__)
    refuses_model(kind, value)
    refuses(value.canonical_bytes)
    refuses(checked_resource, value, kind)
    refuses(public_digest, name, value)


def test_public_digest_arguments_keep_exact_typed_query_request_and_eight_field_body_shapes():
    context = TaskResourceContext.model_validate(vectors()["TaskResourceContext"])
    refuses(
        task_resource_context_digest,
        context.query.model_dump(mode="json"),
        context.backend_id,
        context.family,
        context.standing,
        context.ordinary,
    )
    refuses(standing_resource_digest, vectors()["StandingResourceReservation"])
    raw = vectors()["StandingResourceObservation"]
    observed = StandingResourceObservation.model_validate(raw)
    refuses(standing_resource_observation_digest, observed)
    refuses(standing_resource_observation_digest, raw)
    body = without_digest(raw, "observation_digest")
    assert standing_resource_observation_digest(body) == raw["observation_digest"]
    for missing in body:
        refuses(
            standing_resource_observation_digest,
            {key: child for key, child in body.items() if key != missing},
        )


def test_public_reservation_hash_observes_new_valid_actual_identity_without_stale_receipt_reuse():
    raw = copy.deepcopy(vectors()["StandingResourceReservationReceipt"])
    receipt = StandingResourceReservationReceipt.model_validate(raw)
    assert standing_resource_digest(receipt.request) == raw["request_digest"]
    receipt.request.__dict__["lease_id"] = OTHER
    changed = receipt.request.model_dump(mode="json")
    expected = digest("StandingResourceReservationReceipt", changed)
    assert expected != raw["request_digest"]
    assert standing_resource_digest(receipt.request) == expected
    refuses(receipt.canonical_bytes)
    corrected = StandingResourceReservationReceipt.model_validate(
        {**raw, "request": changed, "request_digest": expected}
    )
    assert corrected.validate_request(receipt.request).request_digest == expected


@pytest.mark.parametrize("family", ("ordinary", "observation"))
def test_valid_nested_changes_produce_new_public_hashes_without_reusing_old_packet(family):
    if family == "ordinary":
        name, kind, field = "TaskResourceContext", TaskResourceContext, "context_digest"
        body = ordinary_body()
        raw = {**body, field: digest(name, body)}
    else:
        name, kind, field = (
            "StandingResourceObservation",
            StandingResourceObservation,
            "observation_digest",
        )
        raw = copy.deepcopy(vectors()[name])
    value = kind.model_validate(copy.deepcopy(raw))
    assert public_digest(name, value) == raw[field]
    if family == "ordinary":
        value.ordinary.__dict__["submit_payload_digest"] = "f" * 64
        raw["ordinary"]["submit_payload_digest"] = "f" * 64
    else:
        value.reservation.__dict__["lease_id"] = OTHER
        raw["reservation"]["lease_id"] = OTHER
    expected = digest(name, without_digest(raw, field))
    assert expected != raw[field] and public_digest(name, value) == expected
    refuses(value.canonical_bytes)
    raw[field] = expected
    corrected = kind.model_validate(raw)
    assert corrected.canonical_bytes() == encoded(raw)
    assert public_digest(name, corrected) == expected


@pytest.mark.parametrize("name,kind,maximum,field", TARGETS)
def test_all_original_request_joins_are_full_and_fresh(name, kind, maximum, field):
    value = kind.model_validate(vectors()[name])
    original = original_for(name)
    assert value.validate_request(original).canonical_bytes() == value.canonical_bytes()
    different = original.model_copy(deep=True)
    if name == "TaskResourceContext":
        different.__dict__["task_id"] = "foreign-task"
    elif name == "StandingResourceReservationReceipt":
        different.__dict__["lease_id"] = OTHER
    else:
        different.__dict__["executor_id"] = OTHER
    # Each alternative is independently valid; it is foreign to this packet.
    different = type(original).model_validate(different.model_dump(mode="python"))
    refuses(value.validate_request, different)
    original.__dict__[PRIVATE] = True
    refuses(value.validate_request, original)
    refuses(value.validate_request, original.model_dump(mode="json"))


@pytest.mark.parametrize(
    "name,path,replacement",
    (
        ("TaskResourceContext", ("family",), "ordinary"),
        ("TaskResourceContext", ("backend_id",), "f" * 32),
        ("TaskResourceContext", ("query", "task_id"), "foreign-task"),
        ("TaskResourceContext", ("standing", "sequence"), True),
        ("TaskResourceContext", ("standing", "claim", "request", "task", "model_pin", "local"), 1),
        ("StandingResourceReservationReceipt", ("disposition",), True),
        ("StandingResourceReservationReceipt", ("request_digest",), "0" * 64),
        ("StandingResourceReservationReceipt", ("request", "resource_instance"), OTHER.upper()),
        ("StandingResourceObservation", ("coverage_sequence",), True),
        ("StandingResourceObservation", ("accepted_sequence",), 17.0),
        ("StandingResourceObservation", ("accepted_sequence",), 18),
        ("StandingResourceObservation", ("coverage",), "legacy-or-unadmitted"),
        ("StandingResourceObservation", ("claim_id",), "0" * 64),
    ),
)
def test_strict_scalars_and_internal_full_joins_refuse_even_with_recomputed_outer_hash(
    name, path, replacement
):
    kind = next(kind for n, kind, _, _ in TARGETS if n == name)
    field = next(field for n, _, _, field in TARGETS if n == name)
    raw = copy.deepcopy(vectors()[name])
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    if path != (field,):
        body = (
            raw["request"]
            if name == "StandingResourceReservationReceipt"
            else without_digest(raw, field)
        )
        raw[field] = digest(name, body)
    refuses_model(kind, raw)
    refuses(kind.from_json, encoded(raw))


@pytest.mark.parametrize(
    "case", ("tuple", "surrogate", "object", "depth13", "nodes2049", "bad-key")
)
def test_malformed_actual_nested_graphs_cannot_be_dumped_or_reused(case):
    if case == "depth13":
        bad = PRIVATE
        for _ in range(13):
            bad = [bad]
    else:
        bad = {
            "tuple": (PRIVATE,),
            "surrogate": PRIVATE + "\ud800",
            "object": object(),
            "nodes2049": [PRIVATE] * 2049,
            "bad-key": {1: PRIVATE},
        }[case]
    for name, kind, _, _ in TARGETS:
        value = kind.model_validate(vectors()[name])
        context_of(name, value).standing.claim.request.task.__dict__["objective"] = bad
        refuses(checked_resource, value, kind)
        refuses(value.canonical_bytes)
        refuses(public_digest, name, value)


@pytest.mark.parametrize("name,kind,maximum,field", TARGETS)
def test_subclasses_and_missing_explicit_fields_do_not_gain_packet_identity(
    name, kind, maximum, field
):
    raw = copy.deepcopy(vectors()[name])
    value = kind.model_validate(raw)
    subclass = type("SyntheticResourceSubclass", (kind,), {})
    forged = subclass.model_construct(**value.__dict__)
    refuses_model(kind, forged)
    refuses(checked_resource, forged, kind)
    refuses(forged.canonical_bytes)
    for missing in raw:
        bad = {key: child for key, child in raw.items() if key != missing}
        refuses_model(kind, bad)
        refuses(kind.from_json, encoded(bad))
    refuses_model(kind, {**raw, PRIVATE: None})


@pytest.mark.parametrize("name,kind,maximum,field", TARGETS)
def test_exact_raw_byte_caps_and_complete_payload_are_preserved(name, kind, maximum, field):
    body = encoded(vectors()[name])
    exact = b" " * (maximum - len(body)) + body
    assert len(exact) == maximum
    assert kind.from_json(exact).canonical_bytes() == body
    refuses(kind.from_json, b" " + exact)
    for unsupported in (body.decode(), bytearray(body), memoryview(body)):
        refuses(kind.from_json, unsupported)


@pytest.mark.parametrize("case", ("duplicate", "utf8", "trailing", "array", "null", "float"))
def test_closed_raw_json_codec_remains_input_free_and_uncoercing(case):
    for name, kind, _, _ in TARGETS:
        body = encoded(vectors()[name])
        bad = {
            "duplicate": b'{"schema_version":"1.0",' + body[1:],
            "utf8": b"\xff" + body,
            "trailing": body + b" {}",
            "array": b"[]",
            "null": b"null",
            "float": body.replace(b'"epoch":3', b'"epoch":3.0', 1),
        }[case]
        assert bad != body
        refuses(kind.from_json, bad)
