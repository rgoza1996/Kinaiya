"""Independent closure metadata oracles, frozen before its implementation."""

import copy
import hashlib
import importlib

import pytest
from test_standing_admission_contract import proof as initial_vector
from test_standing_execution_contract import GOLDEN_INTENT, MARKER, encoded

from packages.contracts.agent_submit import submit_entry
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_execution import StandingExecutionIntent

MAXIMUM = 180224
EMPTY_DIGEST = "f51e908e38a6b1b812df1e8b49dc701344a38e803457ffc1e97e7e604184aab7"
ACCEPTED_DIGEST = "7808fe54c23284ccc5f0a4f19774f31e0a63d35b03be53444a5e2fea4dcd00f9"
DOMAIN = b"kinaiya.standing.admission-closure.v1\0"


def contract():
    try:
        return importlib.import_module("packages.contracts.standing_closure")
    except ModuleNotFoundError as error:
        if error.name != "packages.contracts.standing_closure":
            raise
        pytest.fail("Frozen standing admission closure grammar is absent", pytrace=False)


def vector(initial=None, *, original=None):
    raw = copy.deepcopy(GOLDEN_INTENT if original is None else original)
    whole = hashlib.sha256(b"kinaiya.standing.intent.v1\0" + encoded(raw)).hexdigest()
    identifier = hashlib.sha256(
        b"kinaiya.standing.claim.id.v1\0" + encoded({"intent_digest": whole})
    ).hexdigest()
    body = {
        "schema_version": "1.0",
        "intent_digest": whole,
        "claim_id": identifier,
        "initial": copy.deepcopy(initial),
    }
    return {**body, "closure_digest": hashlib.sha256(DOMAIN + encoded(body)).hexdigest()}


def rehash(raw):
    body = {key: value for key, value in raw.items() if key != "closure_digest"}
    return {**body, "closure_digest": hashlib.sha256(DOMAIN + encoded(body)).hexdigest()}


def original():
    return StandingExecutionIntent.model_validate(copy.deepcopy(GOLDEN_INTENT))


def refuses(invoke, *arguments):
    with pytest.raises(ValueError) as refused:
        invoke(*arguments)
    assert MARKER not in str(refused.value)
    assert refused.value.__cause__ is None


def test_existing_vectors_and_stdlib_golden_do_not_need_new_closure_code():
    assert vector()["closure_digest"] == EMPTY_DIGEST
    assert vector(initial_vector())["closure_digest"] == ACCEPTED_DIGEST
    accepted = StandingInitialProof.model_validate(initial_vector()).validate_request(original())
    assert accepted.claim.request == original()
    refuses(submit_entry, accepted.journal_entry())


@pytest.mark.parametrize("accepted", [False, True], ids=["empty-fence", "accepted-fence"])
def test_full_literal_golden_roundtrip_full_original_binding_and_frozen_detachment(accepted):
    module = contract()
    initial = initial_vector() if accepted else None
    raw = vector(initial)
    value = module.StandingAdmissionClosure.model_validate(raw)
    proof = None if initial is None else StandingInitialProof.model_validate(initial)
    assert module.StandingAdmissionClosure.wire_bytes == MAXIMUM
    assert module.standing_closure_digest(original(), proof) == raw["closure_digest"]
    assert value.closure_digest == (ACCEPTED_DIGEST if accepted else EMPTY_DIGEST)
    assert value.canonical_bytes() == encoded(raw)
    checked = value.validate_request(original())
    assert type(checked) is module.StandingAdmissionClosure
    assert checked.canonical_bytes() == encoded(raw)
    assert (
        module.StandingAdmissionClosure.from_json(encoded(raw)).validate_request(original())
        == value
    )
    reordered = dict(reversed(list(raw.items())))
    assert module.StandingAdmissionClosure.model_validate(reordered).canonical_bytes() == encoded(
        raw
    )
    raw.clear()
    assert value.canonical_bytes() == encoded(vector(initial))
    refuses(setattr, value, "initial", None if accepted else proof)


def test_all_five_fields_are_explicit_complete_and_closed_even_required_null():
    kind = contract().StandingAdmissionClosure
    raw = vector()
    assert set(raw) == {"schema_version", "intent_digest", "claim_id", "initial", "closure_digest"}
    for field in raw:
        missing = {key: item for key, item in raw.items() if key != field}
        refuses(kind.model_validate, missing)
    refuses(kind.model_validate, {**raw, "private_untrusted_extra": MARKER})
    for version in (None, True, 1, "1.1"):
        refuses(kind.model_validate, {**raw, "schema_version": version})


@pytest.mark.parametrize("accepted", [False, True])
def test_exact_raw_byte_bound_duplicate_keys_unicode_and_nonobject_input(accepted):
    kind = contract().StandingAdmissionClosure
    body = encoded(vector(initial_vector() if accepted else None))
    exact = b" " * (MAXIMUM - len(body)) + body
    assert kind.from_json(exact).canonical_bytes() == body
    bad = [
        b" " + exact,
        b'{"schema_version":"1.0",' + body[1:],
        b"\xff" + body,
        body + b" {}",
        b"[]",
        b"null",
        b"true",
        b'"' + MARKER.encode() + b'"',
        body.decode(),
        bytearray(body),
        memoryview(body),
    ]
    for raw in bad:
        refuses(kind.from_json, raw)


def test_digest_and_response_binding_cover_full_original_even_with_null_initial():
    module = contract()
    value = module.StandingAdmissionClosure.model_validate(vector())
    for path, replacement in [
        (("task", "objective"), "Different literal objective."),
        (("task", "model_pin", "settings_revision"), 12),
        (("record_digest",), "d" * 64),
        (("executor_id",), "00000000-0000-0000-0000-000000000099"),
    ]:
        raw = copy.deepcopy(GOLDEN_INTENT)
        parent = raw
        for field in path[:-1]:
            parent = parent[field]
        parent[path[-1]] = replacement
        changed = StandingExecutionIntent.model_validate(raw)
        assert (
            module.standing_closure_digest(changed, None) == vector(original=raw)["closure_digest"]
        )
        assert module.standing_closure_digest(changed, None) != EMPTY_DIGEST
        refuses(value.validate_request, changed)


def test_full_accepted_proof_identity_cannot_be_borrowed_for_other_original_or_metadata():
    module = contract()
    raw = vector(initial_vector())
    value = module.StandingAdmissionClosure.model_validate(raw)
    changed = original().model_copy(update={"record_digest": "d" * 64})
    refuses(value.validate_request, changed)
    refuses(module.standing_closure_digest, changed, value.initial)
    for field in ("intent_digest", "claim_id"):
        other = {**copy.deepcopy(raw), field: "e" * 64}
        refuses(module.StandingAdmissionClosure.model_validate, rehash(other))
    other = copy.deepcopy(raw)
    other["initial"]["sequence"] += 1
    refuses(module.StandingAdmissionClosure.model_validate, rehash(other))
    other = copy.deepcopy(raw)
    other["closure_digest"] = "e" * 64
    refuses(module.StandingAdmissionClosure.model_validate, other)


def test_null_metadata_may_not_validate_against_an_unrelated_full_original():
    module = contract()
    raw = vector()
    raw["intent_digest"] = "e" * 64
    candidate = rehash(raw)
    try:
        value = module.StandingAdmissionClosure.model_validate(candidate)
    except ValueError:
        return  # An even stronger internal claim-id consistency check is fine.
    refuses(value.validate_request, original())


@pytest.mark.parametrize("forgery", ["outer-extra", "missing-null", "nested-extra", "nested-bool"])
def test_actual_constructed_and_copied_models_are_checked_before_any_lossy_dump(forgery):
    module = contract()
    kind = module.StandingAdmissionClosure
    value = kind.model_validate(vector(initial_vector()))
    if forgery == "outer-extra":
        forged = value.model_copy(update={"unknown_private_field": MARKER})
    elif forgery == "missing-null":
        raw = vector()
        raw.pop("initial")
        forged = kind.model_construct(**raw)
    elif forgery == "nested-extra":
        forged = value.model_copy(
            update={"initial": value.initial.model_copy(update={"extra": MARKER})}
        )
    else:
        forged = value.model_copy(
            update={"initial": value.initial.model_copy(update={"sequence": True})}
        )
    refuses(forged.canonical_bytes)
    refuses(forged.validate_request, original())
    refuses(kind.model_validate, forged)


def test_typed_subclasses_and_forged_originals_or_proofs_cannot_gain_hash_authority():
    module = contract()

    class OtherIntent(StandingExecutionIntent):
        pass

    class OtherProof(StandingInitialProof):
        pass

    proof = StandingInitialProof.model_validate(initial_vector())
    for forged in (
        original().model_copy(update={"private_extra": MARKER}),
        original().model_copy(update={"authorization_revision": True}),
        OtherIntent.model_construct(**original().__dict__),
    ):
        refuses(module.standing_closure_digest, forged, None)
    refuses(
        module.standing_closure_digest, original(), OtherProof.model_construct(**proof.__dict__)
    )
    refuses(module.standing_closure_digest, original(), proof.model_copy(update={"extra": MARKER}))


def test_literal_unicode_is_not_normalized_and_wrong_scalar_types_are_not_coerced():
    module = contract()
    raw = copy.deepcopy(GOLDEN_INTENT)
    raw["task"]["objective"] = "café e\u0301 日本 \0 literal"
    value = StandingExecutionIntent.model_validate(raw)
    assert module.standing_closure_digest(value, None) == vector(original=raw)["closure_digest"]
    raw["task"]["objective"] = "café é 日本 \0 literal"
    assert module.standing_closure_digest(StandingExecutionIntent.model_validate(raw), None) != (
        module.standing_closure_digest(value, None)
    )
    for field in ("intent_digest", "claim_id", "closure_digest"):
        for replacement in (True, 1, 1.0, None, MARKER, "F" * 64):
            refuses(
                module.StandingAdmissionClosure.model_validate, {**vector(), field: replacement}
            )
    for replacement in ([], (), True, MARKER):
        refuses(
            module.StandingAdmissionClosure.model_validate, {**vector(), "initial": replacement}
        )


def test_oversized_canonical_actual_graph_and_nested_raw_omissions_fail_without_echo():
    module = contract()
    kind = module.StandingAdmissionClosure
    raw = vector(initial_vector())
    del raw["initial"]["claim"]["request"]["task"]["model_pin"]["catalog_revision"]
    refuses(kind.model_validate, raw)
    value = kind.model_validate(vector(initial_vector()))
    huge = value.initial.model_copy(update={"event_id": MARKER * MAXIMUM})
    refuses(value.model_copy(update={"initial": huge}).canonical_bytes)
    cyclic = {"cycle": None}
    cyclic["cycle"] = cyclic
    refuses(kind.model_validate, {**vector(), "initial": cyclic})
