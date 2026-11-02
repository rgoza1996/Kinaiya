"""Primitive-walk compatibility oracles frozen before the proposed fast paths.

Shapes below have explicit mathematical boundary sizes; there is no reference
walker or internal-call-count assertion. Generic tree acceptance does not grant
native provenance, authorize work, or relax a public family's narrower grammar.
"""

import copy
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from test_standing_execution_contract import GOLDEN_HASHES, GOLDEN_INITIAL, claim
from test_standing_work_contract import change

from packages.contracts.standing_admission import standing_entry, standing_entry_digest
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    claim_id,
    intent_digest,
    occurrence_id,
)
from packages.contracts.standing_work import (
    StandingChange,
    StandingTaskSession,
    _tree,
    authorization_digest,
    canonical_bytes,
)

SAFE = 9_007_199_254_740_991
PRIVATE = "SYNTHETIC-PRIVATE-TREE-INPUT"
ERROR = "Invalid standing work contract"


class IntSubclass(int):
    pass


class StrSubclass(str):
    pass


class DictSubclass(dict):
    pass


class ListSubclass(list):
    pass


class TupleSubclass(tuple):
    pass


class SetSubclass(set):
    pass


class IntegerEnum(IntEnum):
    ONE = 1


class StringEnum(StrEnum):
    TEXT = "synthetic"


class Leaf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any


class DefaultedLeaf(Leaf):
    title: str = "native-existing-🧭"


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def refuses_tree(value):
    # Direct _tree preserves the old UTF-8 exception on a surrogate. Only the
    # public helpers promise their fixed, input-free error wrappers.
    with pytest.raises(ValueError):
        _tree(value)


def refuses_canonical(value):
    with pytest.raises(ValueError) as caught:
        canonical_bytes(value)
    assert type(caught.value) is ValueError and str(caught.value) == ERROR
    assert PRIVATE not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize(
    "value",
    (None, False, True, 0, 1, -1, SAFE, -SAFE, "", "é", "e\u0301", "🧭", "x\0y", "\n\t"),
)
def test_exact_scalar_acceptance_preserves_values_and_literal_public_bytes(value):
    assert _tree(value) is None
    leaf = Leaf(value=value)
    assert canonical_bytes(leaf) == encoded({"value": value})
    assert leaf.value == value and type(leaf.value) is type(value)


def invalid_value(case):
    return {
        "positive-unsafe-int": SAFE + 1,
        "negative-unsafe-int": -SAFE - 1,
        "surrogate-high": PRIVATE + "\ud800",
        "surrogate-low": PRIVATE + "\udfff",
        "surrogate-pair-text": "\ud83e\udded",
        "float": 1.0,
        "nan": float("nan"),
        "infinity": float("inf"),
        "decimal": Decimal(1),
        "complex": 1j,
        "bytes": b"synthetic",
        "bytearray": bytearray(b"synthetic"),
        "memoryview": memoryview(b"synthetic"),
        "object": object(),
        "datetime": datetime(2026, 11, 1, tzinfo=UTC),
        "set": {1},
        "frozenset": frozenset({1}),
        "range": range(1),
        "int-subclass": IntSubclass(1),
        "str-subclass": StrSubclass("synthetic"),
        "int-enum": IntegerEnum.ONE,
        "str-enum": StringEnum.TEXT,
        "dict-subclass": DictSubclass({"x": 1}),
        "list-subclass": ListSubclass([1]),
        "tuple-subclass": TupleSubclass((1,)),
    }[case]


@pytest.mark.parametrize(
    "case",
    (
        "positive-unsafe-int",
        "negative-unsafe-int",
        "surrogate-high",
        "surrogate-low",
        "surrogate-pair-text",
        "float",
        "nan",
        "infinity",
        "decimal",
        "complex",
        "bytes",
        "bytearray",
        "memoryview",
        "object",
        "datetime",
        "set",
        "frozenset",
        "range",
        "int-subclass",
        "str-subclass",
        "int-enum",
        "str-enum",
        "dict-subclass",
        "list-subclass",
        "tuple-subclass",
    ),
)
def test_unknown_values_and_scalar_container_subclasses_are_never_coerced(case):
    value = invalid_value(case)
    refuses_tree(value)
    leaf = Leaf(value=None)
    leaf.__dict__["value"] = value  # Inspect the actual value before any model dump.
    refuses_tree(leaf)
    refuses_canonical(leaf)


@pytest.mark.parametrize("key", (1, True, None, b"x", ("x",), StrSubclass("x"), StringEnum.TEXT))
def test_dictionary_keys_are_exact_strings_including_inside_actual_models(key):
    value = {key: "synthetic"}
    refuses_tree(value)
    leaf = Leaf(value=value)
    refuses_canonical(leaf)


def test_generic_exact_tuple_and_nul_acceptance_keep_literal_json_serialization():
    value = (None, True, False, SAFE, -SAFE, "é", "e\u0301", "x\0y")
    assert _tree(value) is None
    assert _tree({"key\0é": [value, (), {}, []]}) is None
    literal = (
        b'{"value":[null,true,false,9007199254740991,-9007199254740991,'
        b'"\xc3\xa9","e\xcc\x81","x\\u0000y"]}'
    )
    assert canonical_bytes(Leaf(value=value)) == literal
    assert canonical_bytes(Leaf(value=list(value))) == literal


def nested(value, count, container):
    # Shape construction only: count containers place a leaf at depth count+1.
    for _ in range(count):
        if container == "list":
            value = [value]
        elif container == "tuple":
            value = (value,)
        else:
            value = {"key": value}
    return value


@pytest.mark.parametrize("container", ("list", "tuple", "dict"))
@pytest.mark.parametrize("primitive", (None, False, 0, "é"))
def test_depth12_accepts_and_depth13_refuses_even_when_the_last_node_is_primitive(
    container, primitive
):
    assert _tree(nested(primitive, 11, container)) is None
    refuses_tree(nested(primitive, 12, container))


@pytest.mark.parametrize("container", (list, tuple))
@pytest.mark.parametrize("primitive", (None, True, SAFE, "🧭"))
def test_flat_node2048_accepts_and_node2049_refuses_final_primitive(container, primitive):
    # One root plus 2047/2048 primitive visits. All leaves count independently.
    assert _tree(container([primitive] * 2047)) is None
    refuses_tree(container([primitive] * 2048))


@pytest.mark.parametrize("primitive", (None, False, SAFE, "é"))
def test_dictionary_keys_count_and_a_final_primitive_cannot_escape_node2049(primitive):
    # Dict: 1+1023 keys+1023 values=2047. Outer list contributes the 2048th
    # node; the additional primitive is the 2049th visit in the existing order.
    mapping = {str(index): 0 for index in range(1023)}
    assert _tree([mapping]) is None
    refuses_tree([primitive, mapping])
    # A 1024-pair dictionary itself also has 2049 nodes, counting its keys.
    refuses_tree({str(index): 0 for index in range(1024)})


@pytest.mark.parametrize("kind", ("list", "dict", "model"))
def test_shared_children_are_revisited_and_not_identity_deduplicated(kind):
    if kind == "list":
        child = [None, True]
    elif kind == "dict":
        child = {"key": None}
    else:
        child = Leaf(value=None)
    # Each shared child costs exactly 3 visits; 1+682*3=2047. Prefix one
    # primitive reaches 2048, two reach2049, without changing object identity.
    repeated = [child] * 682
    assert repeated[0] is repeated[-1]
    assert _tree([False, *repeated]) is None
    refuses_tree([None, False, *repeated])


def test_model_values_dict_is_not_an_extra_node_and_its_keys_are_still_counted():
    # A one-field model contributes itself, its key, its list, and each leaf:
    # 1+1+1+2045=2048. There is no additional visited __dict__ wrapper node.
    assert _tree(Leaf(value=[None] * 2045)) is None
    refuses_tree(Leaf(value=[None] * 2046))


@pytest.mark.parametrize("kind", ("list", "dict", "model"))
def test_cycles_terminate_with_refusal_without_a_visited_object_shortcut(kind):
    if kind == "list":
        cycle = []
        cycle.append(cycle)
    elif kind == "dict":
        cycle = {}
        cycle["self"] = cycle
    else:
        cycle = Leaf(value=None)
        cycle.__dict__["value"] = cycle
    refuses_tree(cycle)
    outer = Leaf(value=None)
    outer.__dict__["value"] = cycle
    refuses_canonical(outer)


def test_complete_basemodel_subclasses_and_existing_default_field_metadata_are_valid():
    model = DefaultedLeaf(value={"x": (None, True, "é\0")})
    assert model.__pydantic_fields_set__ == {"value"}
    assert set(model.__dict__) == {"title", "value"}
    assert _tree(model) is None
    expected = {"title": "native-existing-🧭", "value": {"x": [None, True, "é\0"]}}
    assert canonical_bytes(model) == encoded(expected)
    model.__pydantic_fields_set__.clear()  # All actual values still exist.
    assert _tree(model) is None
    assert canonical_bytes(model) == encoded(expected)


@pytest.mark.parametrize(
    "mutation",
    (
        "unknown-value-field",
        "missing-value-field",
        "dict-subclass",
        "value-key-subclass",
        "fieldset-unknown",
        "fieldset-none",
        "fieldset-list",
        "fieldset-subclass",
        "fieldset-missing",
        "extra-empty",
        "extra-value",
        "extra-missing",
    ),
)
def test_actual_model_closure_fieldset_extra_and_missing_metadata_are_freshly_checked(mutation):
    model = Leaf(value=None)
    assert _tree(model) is None and canonical_bytes(model) == b'{"value":null}'
    if mutation == "unknown-value-field":
        model.__dict__[PRIVATE] = True
    elif mutation == "missing-value-field":
        model.__dict__.pop("value")
    elif mutation == "dict-subclass":
        object.__setattr__(model, "__dict__", DictSubclass(model.__dict__))
    elif mutation == "value-key-subclass":
        object.__setattr__(model, "__dict__", {StrSubclass("value"): None})
    elif mutation == "fieldset-unknown":
        model.__pydantic_fields_set__.add(PRIVATE)
    elif mutation == "fieldset-none":
        object.__setattr__(model, "__pydantic_fields_set__", None)
    elif mutation == "fieldset-list":
        object.__setattr__(model, "__pydantic_fields_set__", ["value"])
    elif mutation == "fieldset-subclass":
        object.__setattr__(model, "__pydantic_fields_set__", SetSubclass({"value"}))
    elif mutation == "fieldset-missing":
        object.__delattr__(model, "__pydantic_fields_set__")
    elif mutation == "extra-empty":
        object.__setattr__(model, "__pydantic_extra__", {})
    elif mutation == "extra-value":
        object.__setattr__(model, "__pydantic_extra__", {PRIVATE: True})
    else:
        object.__delattr__(model, "__pydantic_extra__")
    refuses_tree(model)
    refuses_canonical(model)
    outer = Leaf(value=None)
    outer.__dict__["value"] = model
    refuses_tree(outer)
    refuses_canonical(outer)


def test_shared_actual_child_mutation_cannot_reuse_an_earlier_valid_walk_or_canonical_result():
    child = Leaf(value={"literal": "é\0"})
    model = Leaf(value=[child, child])
    before = b'{"value":[{"value":{"literal":"\xc3\xa9\\u0000"}},{"value":{"literal":"\xc3\xa9\\u0000"}}]}'
    assert _tree(model) is None and canonical_bytes(model) == before
    child.value["literal"] = StrSubclass("é\0")
    refuses_tree(model)
    refuses_canonical(model)
    child.value["literal"] = "é\0"
    assert _tree(model) is None and canonical_bytes(model) == before


def test_complete_public_authorization_and_execution_hash_vectors_stay_exact():
    raw = change()
    parsed = StandingChange.model_validate(raw)
    assert canonical_bytes(parsed) == encoded(raw)
    body = {"responsibility_id": raw["responsibility_id"], "revision": 4, "request": raw}
    expected = hashlib.sha256(b"kinaiya.standing.authorization.v1\0" + encoded(body)).hexdigest()
    assert authorization_digest(parsed, 4) == expected
    retained_raw = claim()
    retained = StandingExecutionClaim.model_validate(retained_raw)
    assert canonical_bytes(retained) == encoded(retained_raw)
    assert occurrence_id(retained.request) == GOLDEN_HASHES["occurrence_id"]
    assert intent_digest(retained.request) == GOLDEN_HASHES["intent_digest"]
    assert claim_id(retained.request) == GOLDEN_HASHES["claim_id"]
    assert (
        standing_entry_digest(
            retained,
            "00000000-0000-0000-0000-000000000777",
            17,
            "2026-11-01T08:30:01.001000Z",
        )
        == "c3129fef6eff8b209d811fc1c1e0c79b4f196b58329a0afc74ca9af22b7b5f58"
    )


def test_family_specific_tuple_and_nul_refusals_remain_separate_from_generic_walk():
    retained = StandingExecutionClaim.model_validate(claim())
    entry = {
        **copy.deepcopy(GOLDEN_INITIAL),
        "event_id": "00000000-0000-0000-0000-000000000777",
        "sequence": 17,
        "origin": "standing-supervisor",
        "created_at": "2026-11-01T08:30:01.001000Z",
    }
    assert standing_entry(entry, retained).model_dump(mode="json") == entry
    entry["causes"] = ()
    assert _tree(entry) is None
    with pytest.raises(ValueError, match="^Invalid standing initial proof$"):
        standing_entry(entry, retained)
    session = {"session_id": "x\0y", "thread_id": "thread", "epoch": 1}
    assert _tree(session) is None
    with pytest.raises(ValueError):
        StandingTaskSession.model_validate(session)
