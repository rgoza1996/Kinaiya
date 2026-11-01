"""Foreign joins without an initial/reservation cannot borrow original identity.

These focused source-review oracles seal behavior before the context-join
performance repair. Child graphs and their hashes are deliberately valid;
the enclosing original/binding cross-join must still refuse them.
"""

import copy

import pytest
from test_standing_resource_finalization_contracts import (
    ERROR,
    GOLDEN_INTENT,
    contract,
    encoded,
    new_context_hash,
    observation_hash,
    rehash,
    vectors,
)

from packages.contracts.standing_closure import StandingAdmissionClosure
from packages.contracts.standing_resources import StandingResourceObservation


def foreign_context(child, field):
    original = vectors(admitted=False, reserved=False)["StandingResourceFinalizeContext"]
    foreign_intent = copy.deepcopy(GOLDEN_INTENT)
    foreign_intent["task"]["objective"] += " A distinct synthetic original."
    foreign = vectors(admitted=False, reserved=False, original=foreign_intent)[
        "StandingResourceFinalizeContext"
    ]
    bad = copy.deepcopy(original)
    assert bad[child][field] != foreign[child][field]
    bad[child][field] = foreign[child][field]
    if child == "closure":
        rehash(bad["closure"], "closure_digest", "kinaiya.standing.admission-closure.v1")
        bad["observation"]["closure_digest"] = bad["closure"]["closure_digest"]
    observation_hash(bad["observation"])
    new_context_hash(bad)
    assert bad["request"] == original["request"] and bad["binding"] == original["binding"]
    assert bad["closure"]["initial"] is None and bad["observation"]["reservation"] is None
    assert bad["observation"]["accepted_sequence"] is None
    assert bad["observation"]["coverage"] == "legacy-or-unadmitted"
    return original, bad


def materialized_children(raw):
    closure = StandingAdmissionClosure.from_json(encoded(raw["closure"]))
    observation = StandingResourceObservation.from_json(encoded(raw["observation"]))
    # Every old child accepts its complete body and independently correct hash.
    assert closure.canonical_bytes() == encoded(raw["closure"])
    assert observation.canonical_bytes() == encoded(raw["observation"])
    return closure, observation


def invoke(boundary, original, raw):
    module = contract()
    closure, observation = materialized_children(raw)
    if boundary == "raw":
        return module.StandingResourceFinalizeContext.model_validate(raw)
    base = module.StandingResourceFinalizeContext.model_validate(original)
    if boundary == "typed":
        copied = base.model_copy(
            update={
                "closure": closure,
                "observation": observation,
                "context_digest": raw["context_digest"],
            }
        )
        return module.checked_finalization(copied, module.StandingResourceFinalizeContext)
    assert boundary == "hash_helper"
    return module.resource_finalize_context_digest(base.request, base.binding, closure, observation)


@pytest.mark.parametrize("boundary", ["raw", "typed", "hash_helper"])
@pytest.mark.parametrize("child", ["closure", "observation"])
@pytest.mark.parametrize("field", ["intent_digest", "claim_id"])
def test_foreign_unadmitted_child_cannot_borrow_binding_original(boundary, child, field):
    original, raw = foreign_context(child, field)
    with pytest.raises(ValueError) as caught:
        invoke(boundary, original, raw)
    assert ERROR in str(caught.value)
    if boundary != "raw":
        assert type(caught.value) is ValueError and str(caught.value) == ERROR
        assert caught.value.__cause__ is None


@pytest.mark.parametrize("boundary", ["raw", "typed", "hash_helper"])
def test_valid_unadmitted_no_reservation_context_keeps_exact_digest_and_bytes(boundary):
    raw = vectors(admitted=False, reserved=False)["StandingResourceFinalizeContext"]
    result = invoke(boundary, raw, copy.deepcopy(raw))
    if boundary == "hash_helper":
        assert result == raw["context_digest"]
    else:
        assert result.canonical_bytes() == encoded(raw)
        assert result.closure.initial is None and result.observation.reservation is None
