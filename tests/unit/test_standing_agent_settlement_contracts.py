"""Independent frozen settlement codec oracles; no candidate implementation read.

The full old Resource graph comes from the attributed, independently authored
test_standing_resource_finalization_contracts.vectors fixture. New envelopes and
expected digests below use only the frozen public interface and hashlib/json.
All data/secrets are synthetic. Parsing these packets proves neither original
ownership, native history, outcome capture, physical retirement nor slot release.
"""

import copy
import hashlib
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel
from test_standing_execution_contract import task_at_canonical_size
from test_standing_resource_finalization_contracts import (
    GOLDEN_HASHES as RESOURCE_HASHES,
)
from test_standing_resource_finalization_contracts import (
    vectors as resource_vectors,
)

from packages.contracts import agent_control, standing_work
from packages.contracts.standing_execution import StandingExecutionClaim
from packages.contracts.standing_resource_finalization import StandingResourceFinalizationReceipt

MODULE = "packages.contracts.standing_agent_settlement"
ERROR = "Invalid standing Agent settlement contract"
MARKER = "SYNTHETIC-UNTRUSTED-SETTLEMENT-INPUT"
SAFE = 9_007_199_254_740_991
OWNER = "00000000-0000-4000-8000-000000000881"
WRITER = "00000000-0000-4000-8000-000000000882"
OTHER = "abcdefab-cdef-4bcd-8fab-cdefabcdefab"
WHEN = "2026-11-02T08:30:01Z"
KEY = "retirement:synthetic-1"
SETTLEMENT_KEY = "settlement:synthetic-1"
SECRET = bytes(range(32))
CAPS = {
    "StandingAgentRetirementRegistrationRequest": 148480,
    "StandingAgentRetirementRegistrationRecord": 149504,
    "StandingAgentRetirementRegistrationReceipt": 149632,
    "StandingAgentOutcomeReference": 2048,
    "StandingAgentRetirementWork": 3072,
    "StandingAgentSettlementRequest": 972800,
    "StandingAgentSettlementRecord": 973824,
    "StandingAgentSettlementReceipt": 973952,
    "StandingAgentSettlementQuery": 149760,
}
DOMAINS = {
    "capability": "kinaiya.standing.agent-retirement-capability.v1",
    "registration": "kinaiya.standing.agent-retirement-registration.v1",
    "registration_record": "kinaiya.standing.agent-retirement-registration-record.v1",
    "settlement": "kinaiya.standing.agent-settlement.v1",
    "settlement_record": "kinaiya.standing.agent-settlement-record.v1",
}
WORK_KINDS = (
    "launched-outcome-known",
    "launched-outcome-unconfirmed",
    "created-unlaunched",
    "bound-never-admitted",
)


def contract():
    return importlib.import_module(MODULE)


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(domain, body):
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded(body)).hexdigest()


def signed(body, field, domain):
    return {**body, field: digest(domain, body)}


def capability(claim, owner=OWNER, key=KEY, secret=SECRET):
    context = {
        "backend_id": claim["request"]["expected_backend"],
        "claim_id": claim["claim_id"],
        "intent_digest": claim["intent_digest"],
        "executor_id": claim["request"]["executor_id"],
        "owner_id": owner,
        "registration_key": key,
    }
    return hashlib.sha256(
        DOMAINS["capability"].encode() + b"\0" + encoded(context) + b"\0" + secret
    ).hexdigest()


def packets(*, work_kind=WORK_KINDS[0], reserved=True, resources=None):
    admitted = work_kind != "bound-never-admitted"
    if resources is None:
        resources = resource_vectors(admitted=admitted, reserved=reserved if admitted else False)[
            "StandingResourceFinalizationReceipt"
        ]
    resources = copy.deepcopy(resources)
    claim = copy.deepcopy(resources["finalization"]["context"]["binding"]["claim"])
    registration = signed(
        {
            "schema_version": "1.0",
            "claim": claim,
            "idempotency_key": KEY,
            "owner_id": OWNER,
            "capability_sha256": capability(claim),
        },
        "registration_digest",
        DOMAINS["registration"],
    )
    registration_record = signed(
        {
            "schema_version": "1.0",
            "request": registration,
            "backend_id": claim["request"]["expected_backend"],
            "registered_instance": WRITER,
            "registered_utc": WHEN,
            "claim_origin": "fresh-v2-prepared",
        },
        "record_digest",
        DOMAINS["registration_record"],
    )
    reference = {
        "schema_version": "1.0",
        "task_id": claim["request"]["task"]["task_id"],
        "event_id": "synthetic-outcome-é-🧭",
        "sequence": 18,
        "objective_revision": 2,
        "state": "succeeded",
        # These literal commitments are not a claim of native outcome capture.
        "request_sha256": hashlib.sha256(encoded({"synthetic": "complete request"})).hexdigest(),
        "entry_sha256": hashlib.sha256(encoded({"synthetic": "complete entry"})).hexdigest(),
    }
    work = {
        "schema_version": "1.0",
        "kind": work_kind,
        "outcome": reference if work_kind == "launched-outcome-known" else None,
    }
    settlement = signed(
        {
            "schema_version": "1.0",
            "idempotency_key": SETTLEMENT_KEY,
            "registration": registration_record,
            "resources": resources,
            "work": work,
        },
        "settlement_digest",
        DOMAINS["settlement"],
    )
    settlement_record = signed(
        {
            "schema_version": "1.0",
            "request": settlement,
            "backend_id": claim["request"]["expected_backend"],
            "settled_instance": OTHER,
            "settled_utc": "2026-11-02T08:31:01Z",
        },
        "record_digest",
        DOMAINS["settlement_record"],
    )
    return {
        "StandingAgentRetirementRegistrationRequest": registration,
        "StandingAgentRetirementRegistrationRecord": registration_record,
        "StandingAgentRetirementRegistrationReceipt": {
            "schema_version": "1.0",
            "disposition": "created",
            "registration": registration_record,
        },
        "StandingAgentOutcomeReference": reference,
        "StandingAgentRetirementWork": work,
        "StandingAgentSettlementRequest": settlement,
        "StandingAgentSettlementRecord": settlement_record,
        "StandingAgentSettlementReceipt": {
            "schema_version": "1.0",
            "disposition": "created",
            "settlement": settlement_record,
        },
        "StandingAgentSettlementQuery": {
            "schema_version": "1.0",
            "registration": registration_record,
            "settlement_key": SETTLEMENT_KEY,
        },
    }


def parsed(name, **options):
    return getattr(contract(), name).model_validate(packets(**options)[name])


def refuses(function, *args, normalized=True):
    with pytest.raises(ValueError) as captured:
        function(*args)
    if normalized:
        assert str(captured.value) == ERROR
        assert captured.value.__cause__ is None
        assert captured.value.__suppress_context__ is True
        assert MARKER not in str(captured.value)


def model_paths(value, path=()):
    if isinstance(value, BaseModel):
        yield path, value
        for field, child in value.__dict__.items():
            yield from model_paths(child, (*path, field))


def at_path(value, path):
    for field in path:
        value = value[field] if type(value) is dict else value.__dict__[field]
    return value


def rehash(value, field, domain):
    value[field] = digest(domain, {key: item for key, item in value.items() if key != field})


@pytest.mark.parametrize("name", CAPS)
def test_nine_complete_packets_roundtrip_closed_fields_caps_and_detachment(name):
    module, raw = contract(), packets()[name]
    kind = getattr(module, name)
    value = kind.model_validate(copy.deepcopy(raw))
    assert kind.wire_bytes == CAPS[name]
    assert set(kind.model_fields) == set(raw)
    assert kind.model_config["strict"] is True
    assert kind.model_config["frozen"] is True
    assert kind.model_config["extra"] == "forbid"
    assert kind.model_config["revalidate_instances"] == "always"
    assert kind.model_config["hide_input_in_errors"] is True
    assert value.canonical_bytes() == encoded(raw)
    assert module.agent_settlement_bytes(value) == encoded(raw)
    rebuilt = module.checked_agent_settlement(value, kind)
    assert type(rebuilt) is kind and rebuilt is not value
    assert rebuilt.canonical_bytes() == encoded(raw)
    assert kind.from_json(encoded(raw)).canonical_bytes() == encoded(raw)
    assert kind.model_validate(dict(reversed(list(raw.items())))).canonical_bytes() == encoded(raw)
    raw.clear()
    assert value.canonical_bytes() == encoded(packets()[name])
    with pytest.raises(ValueError):
        value.schema_version = "1.1"
    with pytest.raises(ValueError):
        del value.schema_version


def test_public_hash_helpers_match_independent_complete_domain_bodies():
    module, raw = contract(), packets()
    registration = parsed("StandingAgentRetirementRegistrationRequest")
    record = parsed("StandingAgentRetirementRegistrationRecord")
    request = parsed("StandingAgentSettlementRequest")
    settlement = parsed("StandingAgentSettlementRecord")
    assert module.agent_retirement_capability_digest(registration.claim, OWNER, KEY, SECRET) == (
        capability(raw["StandingAgentRetirementRegistrationRequest"]["claim"])
    )
    assert (
        module.agent_retirement_registration_digest(
            registration.claim, KEY, OWNER, registration.capability_sha256
        )
        == raw["StandingAgentRetirementRegistrationRequest"]["registration_digest"]
    )
    assert (
        module.agent_retirement_registration_record_digest(
            record.request,
            record.backend_id,
            record.registered_instance,
            record.registered_utc,
            record.claim_origin,
        )
        == raw["StandingAgentRetirementRegistrationRecord"]["record_digest"]
    )
    assert (
        module.agent_settlement_digest(
            request.registration, request.resources, request.work, request.idempotency_key
        )
        == raw["StandingAgentSettlementRequest"]["settlement_digest"]
    )
    assert (
        module.agent_settlement_record_digest(
            settlement.request,
            settlement.backend_id,
            settlement.settled_instance,
            settlement.settled_utc,
        )
        == raw["StandingAgentSettlementRecord"]["record_digest"]
    )
    assert (
        len(
            {
                body[field]
                for body, field in (
                    (raw["StandingAgentRetirementRegistrationRequest"], "registration_digest"),
                    (raw["StandingAgentRetirementRegistrationRecord"], "record_digest"),
                    (raw["StandingAgentSettlementRequest"], "settlement_digest"),
                    (raw["StandingAgentSettlementRecord"], "record_digest"),
                )
            }
        )
        == 4
    )


@pytest.mark.parametrize("change", ("secret", "owner", "key", "claim"))
def test_capability_commitment_has_exact_purpose_context_and_secret(change):
    module = contract()
    claim = parsed("StandingAgentRetirementRegistrationRequest").claim
    owner, key, secret = OWNER, KEY, SECRET
    if change == "secret":
        secret = bytes(reversed(SECRET))
    elif change == "owner":
        owner = OTHER
    elif change == "key":
        key += ":other"
    else:
        original = claim.request.model_dump(mode="json")
        original["task"]["objective"] += " A distinct literal objective."
        raw = resource_vectors(admitted=False, reserved=False, original=original)
        claim = StandingExecutionClaim.model_validate(
            raw["StandingResourceBindingRequest"]["claim"]
        )
    actual = module.agent_retirement_capability_digest(claim, owner, key, secret)
    assert actual == capability(claim.model_dump(mode="json"), owner, key, secret)
    assert actual != packets()["StandingAgentRetirementRegistrationRequest"]["capability_sha256"]
    assert actual != claim.request.capability_sha256


@pytest.mark.parametrize("secret", (b"", b"x" * 31, b"x" * 33, "x" * 32, bytearray(32), None))
def test_secret_is_exact_32_raw_bytes_and_never_coerced(secret):
    module = contract()
    claim = parsed("StandingAgentRetirementRegistrationRequest").claim
    refuses(module.agent_retirement_capability_digest, claim, OWNER, KEY, secret)


@pytest.mark.parametrize("work_kind", WORK_KINDS)
@pytest.mark.parametrize("reserved", (False, True))
def test_four_truthful_work_kinds_join_full_resource_graph(work_kind, reserved):
    raw = packets(work_kind=work_kind, reserved=reserved)
    request = contract().StandingAgentSettlementRequest.model_validate(
        raw["StandingAgentSettlementRequest"]
    )
    assert request.canonical_bytes() == encoded(raw["StandingAgentSettlementRequest"])
    assert (request.work.outcome is not None) == (work_kind == "launched-outcome-known")
    assert (request.resources.finalization.context.closure.initial is None) == (
        work_kind == "bound-never-admitted"
    )
    assert (
        request.registration.request.claim == request.resources.finalization.context.binding.claim
    )
    assert (
        request.registration.backend_id
        != request.resources.finalization.context.binding.resource_backend
    )


@pytest.mark.parametrize("state", ("succeeded", "failed", "cancelled"))
def test_known_terminal_references_allow_changed_objective_revision_without_inventing_native_proof(
    state,
):
    raw = packets()["StandingAgentSettlementRequest"]
    raw["work"]["outcome"].update(state=state, objective_revision=2)
    rehash(raw, "settlement_digest", DOMAINS["settlement"])
    value = contract().StandingAgentSettlementRequest.from_json(encoded(raw))
    assert value.work.outcome.state == state
    assert value.work.outcome.objective_revision == 2
    assert value.resources.finalization.context.closure.initial.sequence == 17


@pytest.mark.parametrize(
    "name,member",
    (
        ("StandingAgentRetirementRegistrationReceipt", "registration"),
        ("StandingAgentSettlementReceipt", "settlement"),
    ),
)
def test_rpc_created_retained_are_valid_envelopes_without_rewriting_record(name, member):
    kind, raw = getattr(contract(), name), packets()[name]
    original = encoded(raw[member])
    for disposition in ("created", "retained"):
        raw["disposition"] = disposition
        value = kind.from_json(encoded(raw))
        assert getattr(value, member).canonical_bytes() == original
        assert value.disposition == disposition
    for invalid in ("absent", "registered", "settled", None):
        refuses(kind.from_json, encoded({**raw, "disposition": invalid}))


@pytest.mark.parametrize(
    "name,field",
    (
        ("StandingAgentRetirementRegistrationRequest", "registration_digest"),
        ("StandingAgentRetirementRegistrationRecord", "record_digest"),
        ("StandingAgentSettlementRequest", "settlement_digest"),
        ("StandingAgentSettlementRecord", "record_digest"),
    ),
)
def test_each_own_complete_body_digest_is_checked(name, field):
    raw = packets()[name]
    raw[field] = "0" * 64
    refuses(getattr(contract(), name).from_json, encoded(raw))


@pytest.mark.parametrize("kind", WORK_KINDS)
def test_work_outcome_presence_is_explicit_and_discriminator_bound(kind):
    module, raw = contract(), packets()["StandingAgentRetirementWork"]
    raw["kind"] = kind
    if kind == "launched-outcome-known":
        raw["outcome"] = None
    refuses(module.StandingAgentRetirementWork.model_validate, raw, normalized=False)
    del raw["outcome"]
    refuses(module.StandingAgentRetirementWork.model_validate, raw, normalized=False)


@pytest.mark.parametrize(
    "change",
    ("foreign-claim", "missing-initial", "foreign-task", "sequence-equal", "sequence-before"),
)
def test_self_consistent_resource_and_reference_bodies_still_must_join_registration(change):
    raw = packets()["StandingAgentSettlementRequest"]
    if change == "foreign-claim":
        original = copy.deepcopy(raw["registration"]["request"]["claim"]["request"])
        original["task"]["objective"] += " Different registered work."
        raw["resources"] = resource_vectors(original=original)[
            "StandingResourceFinalizationReceipt"
        ]
        # This foreign Resource packet is valid in its own independently checked
        # family; only the complete new registration join should reject it.
        assert StandingResourceFinalizationReceipt.model_validate(raw["resources"])
    elif change == "missing-initial":
        raw["resources"] = resource_vectors(admitted=False, reserved=False)[
            "StandingResourceFinalizationReceipt"
        ]
    elif change == "foreign-task":
        raw["work"]["outcome"]["task_id"] = OTHER
    else:
        raw["work"]["outcome"]["sequence"] = raw["resources"]["finalization"]["context"]["closure"][
            "initial"
        ]["sequence"] - (1 if change == "sequence-before" else 0)
    rehash(raw, "settlement_digest", DOMAINS["settlement"])
    refuses(contract().StandingAgentSettlementRequest.from_json, encoded(raw))


@pytest.mark.parametrize("work_kind", WORK_KINDS[1:])
def test_initial_null_matches_actual_work_disposition_not_only_digest(work_kind):
    raw = packets(work_kind=work_kind)["StandingAgentSettlementRequest"]
    admitted = work_kind == "bound-never-admitted"
    raw["resources"] = resource_vectors(admitted=admitted, reserved=False)[
        "StandingResourceFinalizationReceipt"
    ]
    rehash(raw, "settlement_digest", DOMAINS["settlement"])
    refuses(contract().StandingAgentSettlementRequest.from_json, encoded(raw))


@pytest.mark.parametrize(
    "name,field,domain",
    (
        ("StandingAgentRetirementRegistrationRecord", "backend_id", "registration_record"),
        ("StandingAgentSettlementRecord", "backend_id", "settlement_record"),
    ),
)
def test_native_record_backend_must_join_full_prepared_registration(name, field, domain):
    raw = packets()[name]
    raw[field] = "c" * 32
    rehash(raw, "record_digest", DOMAINS[domain])
    refuses(getattr(contract(), name).from_json, encoded(raw))


@pytest.mark.parametrize(
    "name",
    (
        "StandingAgentRetirementRegistrationRecord",
        "StandingAgentRetirementRegistrationReceipt",
        "StandingAgentSettlementRecord",
        "StandingAgentSettlementReceipt",
    ),
)
def test_record_and_receipt_validate_complete_original_request_detached(name):
    module = contract()
    value = parsed(name)
    registration = "RetirementRegistration" in name
    request_name = (
        "StandingAgentRetirementRegistrationRequest"
        if registration
        else "StandingAgentSettlementRequest"
    )
    request = parsed(request_name)
    detached = value.validate_request(request)
    assert detached is not value
    assert detached.canonical_bytes() == value.canonical_bytes()
    altered = request.model_dump(mode="json")
    if registration:
        altered["owner_id"] = OTHER
        rehash(altered, "registration_digest", DOMAINS["registration"])
    else:
        altered["resources"]["disposition"] = "retained"
        rehash(altered, "settlement_digest", DOMAINS["settlement"])
    foreign = getattr(module, request_name).model_validate(altered)
    refuses(value.validate_request, foreign)
    retained_request = (
        detached.request
        if name.endswith("Record")
        else (detached.registration.request if registration else detached.settlement.request)
    )
    if registration:
        retained_request.claim.request.task.__dict__["objective"] = MARKER
    else:
        retained_request.registration.request.claim.request.task.__dict__["objective"] = MARKER
    # All returned values, including receipt grandchildren, must be detached.
    assert value.canonical_bytes() == encoded(packets()[name])


@pytest.mark.parametrize(
    "name", ("StandingAgentSettlementRecord", "StandingAgentSettlementReceipt")
)
@pytest.mark.parametrize("change", ("key", "full-registration"))
def test_query_lookup_validation_is_complete_registration_and_exact_key(name, change):
    module, value = contract(), parsed(name)
    query = parsed("StandingAgentSettlementQuery")
    detached = value.validate_query(query)
    assert detached is not value
    assert detached.canonical_bytes() == value.canonical_bytes()
    request = detached.request if name.endswith("Record") else detached.settlement.request
    request.registration.request.claim.request.task.__dict__["objective"] = MARKER
    assert value.canonical_bytes() == encoded(packets()[name])
    raw = packets()["StandingAgentSettlementQuery"]
    if change == "key":
        raw["settlement_key"] += ":other"
    else:
        # Same claim, owner, key and capability; different valid native metadata
        # is still a different complete registration, not a digest-only lookup.
        raw["registration"]["registered_instance"] = OTHER
        rehash(raw["registration"], "record_digest", DOMAINS["registration_record"])
    refuses(value.validate_query, module.StandingAgentSettlementQuery.model_validate(raw))


@pytest.mark.parametrize("name", CAPS)
def test_every_declared_field_is_required_and_extra_fields_are_refused(name):
    module, raw = contract(), packets()[name]
    kind = getattr(module, name)
    for field in raw:
        incomplete = copy.deepcopy(raw)
        del incomplete[field]
        refuses(kind.from_json, encoded(incomplete))
    refuses(kind.from_json, encoded({**raw, MARKER: True}))
    for version in (None, "1", "1.1", 1, True):
        refuses(kind.from_json, encoded({**raw, "schema_version": version}))


@pytest.mark.parametrize("name", CAPS)
def test_raw_cap_minus_one_exact_plus_one_and_strict_byte_input(name):
    kind, raw = getattr(contract(), name), packets()[name]
    body, maximum = encoded(raw), CAPS[name]
    assert len(body) < maximum - 1
    for size in (maximum - 1, maximum):
        assert kind.from_json(b" " * (size - len(body)) + body).canonical_bytes() == body
    refuses(kind.from_json, b" " * (maximum + 1 - len(body)) + body)
    for invalid in (body.decode(), bytearray(body), memoryview(body), None):
        refuses(kind.from_json, invalid)


@pytest.mark.parametrize("name", CAPS)
def test_raw_lexical_rejection_is_fixed_and_does_not_echo_input(name):
    kind, body = getattr(contract(), name), encoded(packets()[name])
    invalid = (
        b' {"schema_version":"1.0",' + body[1:],
        b' {"schema_\\u0076ersion":"1.0",' + body[1:],
        b"\xff" + body,
        body + b" {}",
        body[:-1] + b',"' + MARKER.encode() + b'":"\\ud800"}',
        b"[]",
        b"null",
        b"true",
        b"1",
        b"1.0",
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"{",
    )
    for raw in invalid:
        refuses(kind.from_json, raw)
    for token in (b"1.0", b"1e0", b"NaN", b"Infinity", b"-Infinity", str(SAFE + 1).encode()):
        refuses(kind.from_json, body[:-1] + b',"' + MARKER.encode() + b'":' + token + b"}")


def test_depth_and_node_preflight_precedes_recursive_stdlib_json_decoder(monkeypatch):
    kind = contract().StandingAgentSettlementReceipt
    deep = b'{"x":' * 21 + b"0" + b"}" * 21
    wide = b'{"x":[' + b",".join([b"0"] * 20481) + b"]}"

    def forbidden(*args, **kwargs):
        raise AssertionError("Recursive decoder entered before lexical graph bound")

    # This checks public ordering against the standard recursive parser, with no
    # dependency on private helper names, caches, or validation-call counts.
    monkeypatch.setattr(json, "loads", forbidden)
    for raw in (deep, wide):
        assert len(raw) < CAPS["StandingAgentSettlementReceipt"]
        refuses(kind.from_json, raw)


@pytest.mark.parametrize("field", ("sequence", "objective_revision"))
def test_reference_safe_integer_boundaries_and_exact_scalar_types(field):
    kind, raw = contract().StandingAgentOutcomeReference, packets()["StandingAgentOutcomeReference"]
    for valid in (1, SAFE):
        assert kind.model_validate({**raw, field: valid}).__dict__[field] == valid
    for invalid in (0, -1, SAFE + 1, True, 1.0, "1", None):
        refuses(kind.from_json, encoded({**raw, field: invalid}))


@pytest.mark.parametrize(
    "field,values",
    (
        ("task_id", (OTHER.upper(), "not-a-uuid", OTHER + "\n", None)),
        ("event_id", ("", "x" * 129, "bad\0id", "bad\ud800id", 1, None)),
        ("state", ("accepted", "running", "paused", "unknown", None)),
        ("request_sha256", ("A" * 64, "a" * 63, "a" * 65, None)),
        ("entry_sha256", ("A" * 64, "a" * 63, "a" * 65, None)),
    ),
)
def test_reference_closed_identity_state_and_digest_grammars(field, values):
    kind, raw = contract().StandingAgentOutcomeReference, packets()["StandingAgentOutcomeReference"]
    for invalid in values:
        refuses(kind.model_validate, {**raw, field: invalid}, normalized=False)
    for state in ("succeeded", "failed", "cancelled"):
        assert kind.model_validate({**raw, "state": state}).state == state
    for event in ("é", "e\u0301", "🧭" * 128, "\x01" * 128):
        assert kind.model_validate({**raw, "event_id": event}).event_id == event


@pytest.mark.parametrize(
    "name,field",
    (
        ("StandingAgentRetirementRegistrationRequest", "idempotency_key"),
        ("StandingAgentSettlementRequest", "idempotency_key"),
        ("StandingAgentSettlementQuery", "settlement_key"),
    ),
)
def test_control_keys_are_ascii_complete_and_no_secret_field_is_accepted(name, field):
    kind, raw = getattr(contract(), name), packets()[name]
    for valid in ("a", "x" * 128, "Ab:09._-"):
        changed = {**raw, field: valid}
        if name == "StandingAgentRetirementRegistrationRequest":
            rehash(changed, "registration_digest", DOMAINS["registration"])
        elif name == "StandingAgentSettlementRequest":
            rehash(changed, "settlement_digest", DOMAINS["settlement"])
        assert kind.from_json(encoded(changed)).__dict__[field] == valid
    for invalid in ("", "é", "bad/key", "bad\nkey", "x" * 129, 1, None):
        changed = {**raw, field: invalid}
        if name == "StandingAgentRetirementRegistrationRequest":
            rehash(changed, "registration_digest", DOMAINS["registration"])
        elif name == "StandingAgentSettlementRequest":
            rehash(changed, "settlement_digest", DOMAINS["settlement"])
        refuses(kind.from_json, encoded(changed))
    refuses(kind.from_json, encoded({**raw, "secret32": "00" * 32}))


@pytest.mark.parametrize(
    "name,field",
    (
        ("StandingAgentRetirementRegistrationRecord", "registered_utc"),
        ("StandingAgentSettlementRecord", "settled_utc"),
    ),
)
def test_native_timestamp_and_origin_fields_are_exact_wire_values(name, field):
    kind, raw = getattr(contract(), name), packets()[name]
    domain = DOMAINS["registration_record" if "Registration" in name else "settlement_record"]
    for invalid in (
        "2026-02-30T00:00:00Z",
        "2026-11-02T08:30:01+00:00",
        "2026-11-02T08:30:01.0Z",
        "2026-11-02t08:30:01z",
        "2026-11-02T08:30:01Z\n",
        None,
    ):
        changed = {**raw, field: invalid}
        rehash(changed, "record_digest", domain)
        refuses(kind.from_json, encoded(changed))
    refuses(
        kind.model_validate, {**raw, field: datetime(2026, 11, 2, tzinfo=UTC)}, normalized=False
    )
    if "claim_origin" in raw:
        for invalid in ("migrated-v1-uncovered", "fresh", None):
            changed = {**raw, "claim_origin": invalid}
            rehash(changed, "record_digest", domain)
            refuses(kind.from_json, encoded(changed))


@pytest.mark.parametrize("name", CAPS)
def test_actual_model_metadata_is_complete_and_never_normalized_away(name):
    module, value = contract(), parsed(name)
    for path, _child in model_paths(value):
        for forgery in ("extra", "missing", "fields-set", "fields-set-type", "extra-metadata"):
            bad = value.model_copy(deep=True)
            target = at_path(bad, path)
            if forgery == "extra":
                target.__dict__[MARKER] = True
            elif forgery == "missing":
                target.__dict__.pop(next(iter(target.__dict__)))
            elif forgery == "fields-set":
                target.__pydantic_fields_set__.add(MARKER)
            elif forgery == "fields-set-type":
                object.__setattr__(
                    target, "__pydantic_fields_set__", tuple(target.__pydantic_fields_set__)
                )
            else:
                object.__setattr__(target, "__pydantic_extra__", {MARKER: True})
            refuses(module.checked_agent_settlement, bad, type(value))
            refuses(module.agent_settlement_bytes, bad)
            refuses(bad.canonical_bytes)
            refuses(type(value).model_validate, bad, normalized=False)


def test_actual_new_and_old_child_subclasses_dict_replacements_and_missing_nulls_refuse():
    module, value = contract(), parsed("StandingAgentSettlementReceipt")
    for path, child in model_paths(value):
        subclass = type("SyntheticSubclass", (type(child),), {})
        forged = subclass.model_construct(**copy.deepcopy(child.__dict__))
        bad = value.model_copy(deep=True)
        if path:
            at_path(bad, path[:-1]).__dict__[path[-1]] = forged
        else:
            bad = forged
        refuses(module.checked_agent_settlement, bad, type(value))
        refuses(module.agent_settlement_bytes, bad)
        if path:
            bad = value.model_copy(deep=True)
            at_path(bad, path[:-1]).__dict__[path[-1]] = child.model_dump(mode="python")
            refuses(module.agent_settlement_bytes, bad)
    work = parsed("StandingAgentRetirementWork", work_kind="created-unlaunched")
    work.__pydantic_fields_set__.remove("outcome")
    refuses(module.agent_settlement_bytes, work)


def test_actual_scalar_subclasses_nonjson_values_cycles_and_hostile_keys_refuse():
    module, value = contract(), parsed("StandingAgentOutcomeReference")

    class StringSubclass(str):
        pass

    class IntegerSubclass(int):
        pass

    for field, invalid in (
        ("event_id", StringSubclass("synthetic")),
        ("sequence", IntegerSubclass(18)),
        ("sequence", True),
        ("objective_revision", 2.0),
        ("state", ("succeeded",)),
        ("event_id", object()),
        ("event_id", float("nan")),
    ):
        bad = value.model_copy(deep=True)
        bad.__dict__[field] = invalid
        refuses(module.agent_settlement_bytes, bad)
    bad = value.model_copy(deep=True)
    bad.__dict__["event_id"] = bad
    refuses(module.agent_settlement_bytes, bad)
    for invalid in ({}, None, "literal", 1):
        refuses(module.checked_agent_settlement, invalid, type(value))
    refuses(module.checked_agent_settlement, value, BaseModel)

    class Unrelated(BaseModel):
        schema_version: str

    refuses(module.agent_settlement_bytes, Unrelated(schema_version="1.0"))
    bad = value.model_copy(deep=True)
    object.__setattr__(
        bad,
        "__dict__",
        {
            StringSubclass(key) if key == "event_id" else key: item
            for key, item in bad.__dict__.items()
        },
    )
    refuses(module.agent_settlement_bytes, bad)


def test_hash_helpers_validate_full_actual_inputs_before_hashing_or_normalizing():
    module = contract()
    registration = parsed("StandingAgentRetirementRegistrationRequest")
    request = parsed("StandingAgentSettlementRequest")
    registration.claim.request.task.workspace_pin.__dict__[MARKER] = True
    refuses(module.agent_retirement_capability_digest, registration.claim, OWNER, KEY, SECRET)
    refuses(
        module.agent_retirement_registration_digest,
        registration.claim,
        KEY,
        OWNER,
        registration.capability_sha256,
    )
    refuses(
        module.agent_retirement_registration_record_digest,
        registration,
        registration.claim.request.expected_backend,
        WRITER,
        WHEN,
        "fresh-v2-prepared",
    )
    request.resources.finalization.context.observation.reservation.context.standing.claim.request.task.model_pin.__dict__[
        "local"
    ] = 1
    refuses(
        module.agent_settlement_digest,
        request.registration,
        request.resources,
        request.work,
        request.idempotency_key,
    )
    refuses(
        module.agent_settlement_record_digest,
        request,
        request.registration.backend_id,
        OTHER,
        WHEN,
    )


def test_old_nested_task_canonical_cap_is_not_enlarged_by_new_envelope():
    original = resource_vectors(admitted=False, reserved=False)["StandingResourceBindingRequest"][
        "claim"
    ]["request"]
    original["task"] = task_at_canonical_size(65537)
    assert len(encoded(original["task"])) == 65537
    assert len(original["task"]["objective"]) <= 16000
    resources = resource_vectors(admitted=False, reserved=False, original=original)[
        "StandingResourceFinalizationReceipt"
    ]
    raw = packets(work_kind="bound-never-admitted", resources=resources)[
        "StandingAgentSettlementRequest"
    ]
    assert len(encoded(raw)) < CAPS["StandingAgentSettlementRequest"]
    refuses(contract().StandingAgentSettlementRequest.from_json, encoded(raw))


def test_existing_codecs_full_resource_hashes_and_exported_schema_bytes_stay_exact():
    resources = resource_vectors()
    assert (
        resources["StandingResourceBindingReceipt"]["request_digest"] == RESOURCE_HASHES["binding"]
    )
    assert (
        resources["StandingResourceFinalizeContext"]["context_digest"] == RESOURCE_HASHES["context"]
    )
    assert (
        resources["StandingResourceFinalization"]["finalization_digest"]
        == RESOURCE_HASHES["finalization"]
    )
    raw = resources["StandingResourceFinalizationReceipt"]
    assert StandingResourceFinalizationReceipt.from_json(encoded(raw)).canonical_bytes() == encoded(
        raw
    )
    assert agent_control.MAX_AGENT_CONTROL_JSON_DEPTH == 12
    assert agent_control.MAX_AGENT_CONTROL_JSON_NODES == 2048
    with pytest.raises(ValueError):
        standing_work.standing_json(encoded(raw), maximum=819200)
    root = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "schemas"
    body = bytearray()
    paths = sorted(path for path in root.glob("*.json") if path.stem not in CAPS)
    for path in paths:
        body.extend(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    assert len(paths) == 192
    assert (
        hashlib.sha256(body).hexdigest()
        == "0f822da9f49dbefbb72be7a6107fb08f1c9a65189f7f96a806b8f8b47723dc82"
    )
