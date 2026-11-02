"""Independent full-identity Resources metadata; no lease/cleanup authority."""

import copy
import hashlib
import importlib
import json

import pytest
from test_standing_admission_contract import proof as initial_vector
from test_standing_execution_contract import GOLDEN_INTENT, encoded

from packages.contracts.agent_control import AgentTaskIdentity
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_execution import StandingExecutionIntent

KINDS = {
    "TaskResourceQuery": 4096,
    "TaskResourceContext": 180224,
    "StandingResourceReservation": 196608,
    "StandingResourceReservationReceipt": 212992,
    "StandingResourceObservation": 212992,
}
MARKER = "SYNTHETIC-UNTRUSTED-RESOURCE-FIELD"
INSTANCE = "00000000-0000-4000-8000-000000000991"
LEASE = "00000000-0000-4000-8000-000000000992"
GOLDEN_HASHES = {
    "context_digest": "0eadea53ff88517bea5c3a3e945fefae809931b335997e07478840bb24aec9ef",
    "request_digest": "6f602ef483373e0e4e979080223ac528225e9bcf6f56c1d48330d43f9f4b3ad6",
    "observation_digest": "3ee7efd190768ea4f2a2a70e82b053b830e261fd57bc5f994bb63b38bd12bae7",
    "action_digest": "5f92aaa5d94590961bc3f7b0b6cb3a485adc64f4141379062caeaf26c4bd6638",
}


def contract():
    try:
        return importlib.import_module("packages.contracts.standing_resources")
    except ModuleNotFoundError as error:
        if error.name != "packages.contracts.standing_resources":
            raise
        pytest.fail("Frozen standing Resources metadata capability is absent", pytrace=False)


def digest(domain, body):
    return hashlib.sha256(domain.encode() + b"\0" + encoded(body)).hexdigest()


def vectors():
    initial = initial_vector()
    query = {"schema_version": "1.0", "task_id": GOLDEN_INTENT["task"]["task_id"]}
    body = {
        "schema_version": "1.0",
        "query": query,
        "backend_id": GOLDEN_INTENT["expected_backend"],
        "family": "standing",
        "standing": initial,
        "ordinary": None,
    }
    context = {**body, "context_digest": digest("kinaiya.standing.resource-context.v1", body)}
    # Existing Resources action hashing intentionally uses JSON's default ASCII escaping.
    action = {
        "operation": "handoff",
        "idempotency_key": "resource-golden",
        "task_id": query["task_id"],
        "required_bytes": 134217728,
        "reason": 'Synthetic café "handoff" 🧭',
    }
    reservation = {
        "schema_version": "1.0",
        "context": context,
        "resource_backend": "b" * 32,
        "resource_instance": INSTANCE,
        "action_digest": hashlib.sha256(
            json.dumps(action, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "lease_id": LEASE,
    }
    receipt = {
        "schema_version": "1.0",
        "disposition": "created",
        "request": reservation,
        "request_digest": digest("kinaiya.standing.resource-reservation.v1", reservation),
    }
    closure = {
        "schema_version": "1.0",
        "intent_digest": initial["claim"]["intent_digest"],
        "claim_id": initial["claim"]["claim_id"],
        "initial": initial,
    }
    observed = {
        "schema_version": "1.0",
        "intent_digest": initial["claim"]["intent_digest"],
        "claim_id": initial["claim"]["claim_id"],
        "closure_digest": digest("kinaiya.standing.admission-closure.v1", closure),
        "coverage_sequence": 16,
        "accepted_sequence": 17,
        "coverage": "accepted-after-bootstrap",
        "reservation": reservation,
    }
    observation = {
        **observed,
        "observation_digest": digest("kinaiya.standing.resource-observation.v1", observed),
    }
    return dict(zip(KINDS, (query, context, reservation, receipt, observation), strict=True))


def refuses(invoke, *arguments):
    with pytest.raises(ValueError) as caught:
        invoke(*arguments)
    assert MARKER not in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize("name", KINDS)
def test_literal_full_identity_hashes_roundtrip_and_detach(name):
    module, raw = contract(), vectors()[name]
    kind = getattr(module, name)
    value = kind.model_validate(copy.deepcopy(raw))
    assert kind.wire_bytes == KINDS[name]
    assert value.canonical_bytes() == encoded(raw)
    assert kind.from_json(encoded(raw)) == value
    if name == "TaskResourceContext":
        assert raw["context_digest"] == GOLDEN_HASHES["context_digest"]
        assert (
            module.task_resource_context_digest(
                value.query, value.backend_id, value.family, value.standing, value.ordinary
            )
            == raw["context_digest"]
        )
        assert value.validate_request(value.query) == value
    elif name == "StandingResourceReservationReceipt":
        assert raw["request_digest"] == GOLDEN_HASHES["request_digest"]
        assert module.standing_resource_digest(value.request) == raw["request_digest"]
        assert value.validate_request(value.request) == value
    elif name == "StandingResourceObservation":
        assert raw["observation_digest"] == GOLDEN_HASHES["observation_digest"]
        body = {k: v for k, v in raw.items() if k != "observation_digest"}
        assert module.standing_resource_observation_digest(body) == raw["observation_digest"]
        assert (
            value.validate_request(
                StandingExecutionIntent.model_validate(copy.deepcopy(GOLDEN_INTENT))
            )
            == value
        )
    elif name == "StandingResourceReservation":
        assert raw["action_digest"] == GOLDEN_HASHES["action_digest"]
    raw.clear()
    assert value.canonical_bytes() == encoded(vectors()[name])
    refuses(setattr, value, "schema_version", MARKER)


def test_every_new_field_is_explicit_closed_and_required_including_nulls():
    module = contract()
    for name, raw in vectors().items():
        kind = getattr(module, name)
        for key in raw:
            refuses(kind.model_validate, {k: v for k, v in raw.items() if k != key})
        refuses(kind.model_validate, {**raw, MARKER: True})
    for task_id in ("", "a/b", " a", "a:b", "é", "x" * 129, True):
        refuses(
            module.TaskResourceQuery.model_validate, {"schema_version": "1.0", "task_id": task_id}
        )
    assert module.TaskResourceQuery(
        schema_version="1.0", task_id="A_9-" + "x" * 124
    ).task_id.endswith("x")


def test_context_family_backend_and_task_identity_are_actual_full_joins():
    module, raw = contract(), vectors()["TaskResourceContext"]
    for change in (
        {"standing": None},
        {"family": "ordinary"},
        {"backend_id": "f" * 32},
        {"query": {"schema_version": "1.0", "task_id": "another_task"}},
    ):
        bad = {**copy.deepcopy(raw), **change}
        body = {k: v for k, v in bad.items() if k != "context_digest"}
        bad["context_digest"] = digest("kinaiya.standing.resource-context.v1", body)
        refuses(module.TaskResourceContext.model_validate, bad)
    ordinary = AgentTaskIdentity(
        schema_version="1.0",
        session_id="session",
        thread_id="thread",
        epoch=1,
        task_id="ordinary_task",
        submit_event_id="native-event",
        submit_sequence=19,
        submit_idempotency_key="ordinary-key",
        submit_payload_digest="a" * 64,
    )
    query = module.TaskResourceQuery(schema_version="1.0", task_id=ordinary.task_id)
    body = {
        "schema_version": "1.0",
        "query": query.model_dump(mode="json"),
        "backend_id": "a" * 32,
        "family": "ordinary",
        "standing": None,
        "ordinary": ordinary.model_dump(mode="json"),
    }
    value = module.TaskResourceContext.model_validate(
        {**body, "context_digest": digest("kinaiya.standing.resource-context.v1", body)}
    )
    assert value.validate_request(query).ordinary == ordinary
    refuses(
        module.StandingResourceReservation.model_validate,
        {**vectors()["StandingResourceReservation"], "context": value},
    )


@pytest.mark.parametrize("forgery", ("outer-extra", "nested-extra", "subclass"))
def test_copied_actual_graphs_cannot_lose_unknown_fields_or_typed_class(forgery):
    module = contract()
    value = module.StandingResourceReservation.model_validate(
        vectors()["StandingResourceReservation"]
    )
    if forgery == "outer-extra":
        bad = value.model_copy(update={MARKER: True})
    elif forgery == "nested-extra":
        bad = value.model_copy(update={"context": value.context.model_copy(update={MARKER: True})})
    else:

        class SubProof(StandingInitialProof):
            pass

        proof = SubProof.model_validate(value.context.standing.model_dump(mode="python"))
        bad = value.model_copy(
            update={"context": value.context.model_copy(update={"standing": proof})}
        )
    refuses(module.standing_resource_digest, bad)
    refuses(bad.canonical_bytes)


def test_receipt_binds_all_original_fields_and_context_query_not_just_task_id():
    module = contract()
    receipt = module.StandingResourceReservationReceipt.model_validate(
        vectors()["StandingResourceReservationReceipt"]
    )
    for field, change in (
        ("lease_id", INSTANCE),
        ("resource_backend", "e" * 32),
        ("action_digest", "f" * 64),
        ("resource_instance", LEASE),
    ):
        changed = module.StandingResourceReservation.model_validate(
            {**receipt.request.model_dump(mode="python"), field: change}
        )
        refuses(receipt.validate_request, changed)
    query = module.TaskResourceQuery(schema_version="1.0", task_id="different_task")
    refuses(receipt.request.context.validate_request, query)


def test_observation_coverage_relation_full_initial_and_original_binding():
    module, raw = contract(), vectors()["StandingResourceObservation"]
    for coverage_sequence, accepted, coverage in (
        (16, 17, "accepted-after-bootstrap"),
        (17, 17, "legacy-or-unadmitted"),
        (18, None, "legacy-or-unadmitted"),
    ):
        body = {
            **raw,
            "coverage_sequence": coverage_sequence,
            "accepted_sequence": accepted,
            "coverage": coverage,
            "reservation": None,
        }
        body.pop("observation_digest")
        observed = module.StandingResourceObservation.model_validate(
            {**body, "observation_digest": digest("kinaiya.standing.resource-observation.v1", body)}
        )
        assert observed.accepted_sequence == accepted
        body["coverage"] = (
            "legacy-or-unadmitted"
            if coverage == "accepted-after-bootstrap"
            else "accepted-after-bootstrap"
        )
        refuses(
            module.StandingResourceObservation.model_validate,
            {
                **body,
                "observation_digest": digest("kinaiya.standing.resource-observation.v1", body),
            },
        )
    body = {k: v for k, v in raw.items() if k != "observation_digest"}
    body["accepted_sequence"] = 18
    refuses(
        module.StandingResourceObservation.model_validate,
        {**body, "observation_digest": digest("kinaiya.standing.resource-observation.v1", body)},
    )
    observed = module.StandingResourceObservation.model_validate(raw)
    changed = copy.deepcopy(GOLDEN_INTENT)
    changed["task"]["objective"] += MARKER
    refuses(observed.validate_request, StandingExecutionIntent.model_validate(changed))


def test_raw_decoder_bounds_duplicates_utf8_boolean_and_frozen_actual_types():
    module = contract()
    for name, raw in vectors().items():
        kind, body = getattr(module, name), encoded(raw)
        assert kind.from_json(b" " * (kind.wire_bytes - len(body)) + body).canonical_bytes() == body
        for invalid in (
            b" " * (kind.wire_bytes + 1),
            b"null",
            b"[]",
            b"\xff",
            b'{"schema_version":"1.0",' + body[1:],
        ):
            refuses(kind.from_json, invalid)
    raw = vectors()["StandingResourceObservation"]
    raw["coverage_sequence"] = True
    refuses(module.StandingResourceObservation.model_validate, raw)
