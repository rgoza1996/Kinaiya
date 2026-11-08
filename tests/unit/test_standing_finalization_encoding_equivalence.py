"""Pure canonical-byte reuse gate, frozen before a consumer optimization.

Old independent synthetic vectors supply complete literal graphs and golden
hashes. Expected bytes come only from those raw dictionaries and stdlib JSON.
The actual-graph encoder is deliberately not a semantic validator: malformed
semantics must still pass through the complete public parser/checked bridge.
This gate does not prove native row joins, cross-await freshness or cleanup.
"""

import copy
import hashlib
import json

import pytest
from test_standing_resource_finalization_contracts import GOLDEN_HASHES, vectors

from packages.contracts.standing_execution import StandingExecutionClaim
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingRequest,
    StandingResourceFinalization,
    checked_finalization,
    finalization_bytes,
)
from packages.contracts.standing_resource_finalization import (
    _encode as actual_graph_bytes,
)
from packages.contracts.standing_resources import StandingResourceReservation

ERROR = "Invalid standing resource finalization contract"
OLD_ERROR = "Invalid standing Resources contract"
PRIVATE = "SYNTHETIC-PRIVATE-CODEC-INPUT"
TARGETS = (
    ("binding", StandingResourceBindingRequest, 163840),
    ("reservation", StandingResourceReservation, 196608),
    ("finalization", StandingResourceFinalization, 802816),
)
KINDS = {label: kind for label, kind, _ in TARGETS}
PINS = {
    "binding": ("claim", "request", "task", "model_pin"),
    "reservation": ("context", "standing", "claim", "request", "task", "model_pin"),
    "finalization": ("context", "binding", "claim", "request", "task", "model_pin"),
}


def encoded(raw):
    return json.dumps(
        raw, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(domain, raw):
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded(raw)).hexdigest()


def without(raw, field):
    return {name: value for name, value in raw.items() if name != field}


def raw_packet(label, **options):
    bundle = vectors(**options)
    if label == "reservation":
        return copy.deepcopy(
            bundle["StandingResourceFinalization"]["context"]["observation"]["reservation"]
        )
    return copy.deepcopy(bundle[KINDS[label].__name__])


def fresh(label, raw):
    kind = KINDS[label]
    value = kind.from_json(encoded(raw))
    # This is the existing full bridge required for an older Reservation.
    return checked_finalization(value, kind) if label == "reservation" else value


def at(value, path):
    for name in path:
        value = value[name] if type(value) is dict else getattr(value, name)
    return value


def refuses(operation, *args, error=ERROR, suppressed=True):
    with pytest.raises(ValueError) as caught:
        operation(*args)
    assert type(caught.value) is ValueError and str(caught.value) == error
    assert PRIVATE not in str(caught.value) and caught.value.__cause__ is None
    if suppressed:
        assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "label,options,golden",
    (
        ("binding", {}, "binding"),
        ("reservation", {}, None),
        ("finalization", {"admitted": False, "reserved": False}, None),
        ("finalization", {"reserved": False}, None),
        (
            "finalization",
            {"resource_disposition": "registered-no-lease", "restoration": "not-required"},
            None,
        ),
        ("finalization", {}, "finalization"),
    ),
)
def test_fresh_complete_packets_have_literal_public_and_actual_graph_bytes(label, options, golden):
    raw = raw_packet(label, **options)
    value = fresh(label, raw)
    expected = encoded(raw)
    assert type(value) is KINDS[label]
    assert actual_graph_bytes(value) == finalization_bytes(value) == expected
    assert b'"reasoning":null' in expected
    assert "Inspect café.txt and é.txt literally; preserve 🧭.".encode() in expected
    assert b"\\u00e9" not in expected and b"\\ud83e" not in expected
    if label == "reservation":
        # The old public serializer at the intent join remains byte identical.
        assert value.canonical_bytes() == expected
    if golden == "binding":
        assert digest("kinaiya.standing.resource-binding.v1", raw) == GOLDEN_HASHES[golden]
    elif golden == "finalization":
        assert raw["finalization_digest"] == GOLDEN_HASHES[golden]
        assert (
            digest("kinaiya.standing.resource-finalization.v1", without(raw, "finalization_digest"))
            == GOLDEN_HASHES[golden]
        )


@pytest.mark.parametrize(
    "label,path,kind",
    (
        ("binding", ("claim",), StandingExecutionClaim),
        ("reservation", ("context", "standing", "claim"), StandingExecutionClaim),
        ("finalization", ("context", "binding"), StandingResourceBindingRequest),
        (
            "finalization",
            ("context", "observation", "reservation"),
            StandingResourceReservation,
        ),
    ),
)
def test_used_fresh_children_keep_complete_canonical_bytes(label, path, kind):
    raw = raw_packet(label)
    child = at(fresh(label, raw), path)
    expected = encoded(at(raw, path))
    assert type(child) is kind
    assert actual_graph_bytes(child) == finalization_bytes(child) == expected
    detached = checked_finalization(child, kind)
    assert detached is not child and actual_graph_bytes(detached) == expected


def test_unicode_and_nul_in_genuine_action_grammar_keep_utf8_and_json_escaping():
    raw = raw_packet("finalization")
    action_key = 'handoff: café é 🧭 "literal"\0tail'
    reservation = raw["context"]["observation"]["reservation"]
    action = {
        "operation": "handoff",
        "idempotency_key": action_key,
        "task_id": raw["context"]["request"]["original"]["task"]["task_id"],
        "required_bytes": 134217728,
        "reason": "Synthetic café 🧭",
    }
    # The unchanged old action grammar intentionally uses ASCII escaping.
    action_digest = hashlib.sha256(
        json.dumps(action, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    reservation["action_digest"] = raw["handoff_action_digest"] = action_digest
    raw["handoff_action_key"] = action_key
    observation = raw["context"]["observation"]
    observation["observation_digest"] = digest(
        "kinaiya.standing.resource-observation.v1", without(observation, "observation_digest")
    )
    context = raw["context"]
    context["context_digest"] = digest(
        "kinaiya.standing.resource-finalize-context.v1", without(context, "context_digest")
    )
    raw["finalization_digest"] = digest(
        "kinaiya.standing.resource-finalization.v1", without(raw, "finalization_digest")
    )
    value = fresh("finalization", raw)
    expected = encoded(raw)
    assert b"\\u0000" in expected and b"\0" not in expected
    assert "café é 🧭".encode() in expected
    assert actual_graph_bytes(value) == finalization_bytes(value) == expected
    assert value.handoff_action_key == action_key


@pytest.mark.parametrize("label,kind,maximum", TARGETS)
@pytest.mark.parametrize("fault", ("duplicate", "missing-required-null", "strict-scalar"))
def test_malformed_raw_inputs_still_require_complete_strict_parsing(label, kind, maximum, fault):
    raw = raw_packet(label, reserved=False) if label == "finalization" else raw_packet(label)
    if fault == "duplicate":
        data = b'{"schema_version":"' + PRIVATE.encode() + b'",' + encoded(raw)[1:]
    else:
        if fault == "missing-required-null":
            if label == "binding":
                pin = at(raw, PINS[label])
                assert pin.pop("reasoning") is None
            elif label == "reservation":
                assert raw["context"].pop("ordinary") is None
            else:
                assert raw.pop("handoff_action_key") is None
        else:
            raw["schema_version"] = True
        data = encoded(raw)
    assert len(data) < maximum
    refuses(kind.from_json, data, error=OLD_ERROR if label == "reservation" else ERROR)


@pytest.mark.parametrize("label,kind,maximum", TARGETS)
@pytest.mark.parametrize("fault", ("fields-set", "extra-metadata"))
def test_actual_nested_metadata_cannot_be_lost_by_encoding(label, kind, maximum, fault):
    value = fresh(label, raw_packet(label))
    pin = at(value, PINS[label])
    if fault == "fields-set":
        supplied = set(pin.__pydantic_fields_set__)
        supplied.remove("schema_version")
        object.__setattr__(pin, "__pydantic_fields_set__", supplied)
    else:
        object.__setattr__(pin, "__pydantic_extra__", {PRIVATE: True})
    refuses(actual_graph_bytes, value, suppressed=False)
    refuses(finalization_bytes, value)
    refuses(checked_finalization, value, kind)


@pytest.mark.parametrize("label,kind,maximum", TARGETS)
@pytest.mark.parametrize("excess", (0, 1))
def test_each_existing_raw_packet_cap_is_enforced_before_canonical_comparison(
    label, kind, maximum, excess
):
    raw = raw_packet(label)
    expected = encoded(raw)
    assert kind.wire_bytes == maximum and len(expected) < maximum
    padded = expected + b" " * (maximum + excess - len(expected))
    assert len(padded) == maximum + excess
    if excess:
        refuses(kind.from_json, padded, error=OLD_ERROR if label == "reservation" else ERROR)
    else:
        value = kind.from_json(padded)
        if label == "reservation":
            value = checked_finalization(value, kind)
        assert actual_graph_bytes(value) == finalization_bytes(value) == expected


@pytest.mark.parametrize("label,kind,maximum", TARGETS)
def test_checked_results_are_detached_from_the_actual_parsed_graph(label, kind, maximum):
    raw = raw_packet(label)
    expected = encoded(raw)
    supplied = fresh(label, raw)
    result = checked_finalization(supplied, kind)
    before = at(supplied, PINS[label])
    after = at(result, PINS[label])
    assert result is not supplied and after is not before
    before.__dict__["model"] = PRIVATE
    raw.clear()
    assert actual_graph_bytes(result) == finalization_bytes(result) == expected
    refuses(finalization_bytes, supplied)


@pytest.mark.parametrize(
    "label,path,replacement",
    (
        ("binding", ("claim", "request", "task", "objective"), "A changed synthetic objective."),
        ("reservation", ("context", "query", "task_id"), "different_task"),
        ("finalization", ("policy_revision",), True),
    ),
)
def test_semantic_mutation_requires_fresh_full_validation_not_only_shape_encoding(
    label, path, replacement
):
    raw = raw_packet(label)
    value = fresh(label, raw)
    before = finalization_bytes(value)
    parent = at(value, path[:-1])
    parent.__dict__[path[-1]] = replacement
    at(raw, path[:-1])[path[-1]] = replacement
    # These scalar graphs are encodable but semantically invalid. This is why
    # a previously checked result cannot replace a new full check after await.
    assert actual_graph_bytes(value) == encoded(raw) != before
    refuses(finalization_bytes, value)
    refuses(checked_finalization, value, KINDS[label])
    refuses(
        KINDS[label].from_json,
        encoded(raw),
        error=OLD_ERROR if label == "reservation" else ERROR,
    )


def test_public_fixed_error_envelope_retains_unicode_failure_normalization():
    raw = raw_packet("finalization")
    value = fresh("finalization", raw)
    value.__dict__["handoff_action_key"] = PRIVATE + "\ud800"
    # The low-level encoder can raise UnicodeError; it is not the public
    # normalized error boundary and cannot be used on unvalidated input.
    with pytest.raises(UnicodeError):
        actual_graph_bytes(value)
    refuses(finalization_bytes, value)
    refuses(checked_finalization, value, StandingResourceFinalization)
