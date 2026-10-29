"""Strict wire-contract tests for the optional Converse memory core."""

import inspect
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from packages.contracts import memory
from packages.contracts.models import MAX_PLAYBACK_GENERATION

MODELS = [
    cls
    for _, cls in vars(memory).items()
    if inspect.isclass(cls)
    and cls.__module__ == memory.__name__
    and issubclass(cls, memory.MemoryWire)
]

EXPECTED_MODELS = {
    "MemoryBinding",
    "MemoryState",
    "MemoryConfigure",
    "MemoryFact",
    "MemoryCapture",
    "MemoryInspect",
    "MemoryQuery",
    "MemoryForget",
    "MemoryNode",
    "MemoryCaptureResult",
    "MemoryQueryResult",
    "MemoryForgetResult",
    "MemoryWire",
}

BASE = {"session_id": "s1", "thread_id": "t1", "epoch": 1}


def _fact(value="v"):
    return memory.MemoryFact(key="k/1", value=value)


def _capture(**over):
    params = {
        **BASE,
        "expected_revision": 0,
        "idempotency_key": "k1",
        "kind": "assertion",
        "source_event_id": "e1",
        "source_revision": 1,
        "body": "hello",
        "fact": _fact("v"),
    }
    params.update(over)
    return memory.MemoryCapture(**params)


def _node(**over):
    params = {
        **BASE,
        "node_id": "n1",
        "ordinal": 1,
        "kind": "assertion",
        "source_event_id": "e1",
        "source_revision": 1,
        "source_sequence": 1,
        "body": "hello",
        "fact": _fact("v"),
        "state": "active",
        "content_scope": "local_only",
        "provenance": "owner_curated",
    }
    params.update(over)
    return memory.MemoryNode(**params)


def _state(**over):
    params = {**BASE, "enabled": True, "revision": 0, "node_count": 0, "status": "ready"}
    params.update(over)
    return memory.MemoryState(**params)


def test_all_named_wire_models_exist():
    assert {c.__name__ for c in MODELS} == EXPECTED_MODELS


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_exported_memory_schema_matches_python(model):
    path = (
        Path(__file__).resolve().parents[1]
        / "packages/contracts/schemas"
        / (model.__name__ + ".json")
    )
    stored = json.loads(path.read_text())
    assert stored == model.model_json_schema()
    assert stored["additionalProperties"] is False
    jsonschema.Draft202012Validator.check_schema(stored)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_schema_version_preserved(model):
    schema = model.model_json_schema()
    assert schema["properties"]["schema_version"]["const"] == "1.0"


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (memory.MemoryBinding, BASE),
        (
            memory.MemoryState,
            {**BASE, "enabled": True, "revision": 0, "node_count": 0, "status": "ready"},
        ),
        (memory.MemoryConfigure, {**BASE, "expected_revision": 0, "enabled": True}),
        (memory.MemoryFact, {"key": "k", "value": "v"}),
        (memory.MemoryInspect, {**BASE, "node_id": "n1"}),
        (
            memory.MemoryForget,
            {**BASE, "expected_revision": 0, "idempotency_key": "k", "node_id": "n1"},
        ),
    ],
    ids=["binding", "state", "configure", "fact", "inspect", "forget"],
)
def test_extra_fields_rejected(model, kwargs):
    with pytest.raises(ValidationError):
        model(**kwargs, extra_field="nope")


def test_capture_rejects_server_derived_fields():
    server_fields = [
        {"node_id": "n1"},
        {"ordinal": 1},
        {"source_sequence": 1},
        {"state": "active"},
        {"content_scope": "local_only"},
        {"provenance": "owner_curated"},
        {"revision": 0},
        {"node_count": 0},
        {"status": "ready"},
    ]
    for extra in server_fields:
        with pytest.raises(ValidationError):
            _capture(**extra)


@pytest.mark.parametrize("wrong", ["2.0", "1", "", "1.0.0"])
def test_wrong_schema_version_rejected(wrong):
    with pytest.raises(ValidationError):
        memory.MemoryBinding(**BASE, schema_version=wrong)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {**BASE, "enabled": 1, "revision": 0, "node_count": 0, "status": "ready"},
        {**BASE, "enabled": "true", "revision": 0, "node_count": 0, "status": "ready"},
        {**BASE, "enabled": True, "revision": "0", "node_count": 0, "status": "ready"},
        {**BASE, "enabled": True, "revision": 1.0, "node_count": 0, "status": "ready"},
        {**BASE, "enabled": True, "revision": True, "node_count": 0, "status": "ready"},
        {**BASE, "enabled": True, "revision": 0, "node_count": "0", "status": "ready"},
    ],
)
def test_state_strict_no_coercion(kwargs):
    with pytest.raises(ValidationError):
        memory.MemoryState(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expected_revision": "0"},
        {"expected_revision": 1.0},
        {"expected_revision": True},
        {"idempotency_key": 123},
        {"source_revision": "1"},
        {"source_revision": 1.0},
        {"source_revision": True},
        {"body": 123},
        {"body": True},
    ],
)
def test_capture_strict_no_coercion(kwargs):
    base = {
        "expected_revision": 0,
        "idempotency_key": "k1",
        "kind": "assertion",
        "source_event_id": "e1",
        "source_revision": 1,
        "body": "hello",
        "fact": _fact("v"),
    }
    base.update(kwargs)
    with pytest.raises(ValidationError):
        memory.MemoryCapture(**BASE, **base)


def test_fact_bool_int_distinction_preserved():
    as_bool = memory.MemoryFact(key="k", value=True)
    as_int = memory.MemoryFact(key="k", value=1)
    assert type(as_bool.value) is bool
    assert type(as_int.value) is int
    assert as_bool.value is True
    assert as_int.value == 1
    # False vs 0 likewise distinct
    assert type(memory.MemoryFact(key="k", value=False).value) is bool
    assert type(memory.MemoryFact(key="k", value=0).value) is int


@pytest.mark.parametrize("bad", [True, False])
def test_safe_int_fields_reject_bool(bad):
    with pytest.raises(ValidationError):
        memory.MemoryState(**BASE, enabled=True, revision=bad, node_count=0, status="ready")
    with pytest.raises(ValidationError):
        _node(ordinal=bad)
    with pytest.raises(ValidationError):
        _capture(source_revision=bad)


@pytest.mark.parametrize("bad", [1.0, [1], {"v": 1}, float("inf")])
def test_fact_rejects_float_container_nonfinite(bad):
    with pytest.raises(ValidationError):
        memory.MemoryFact(key="k", value=bad)


@pytest.mark.parametrize("bad", [9007199254740992, -9007199254740992])
def test_fact_int_safe_bounds(bad):
    with pytest.raises(ValidationError):
        memory.MemoryFact(key="k", value=bad)


@pytest.mark.parametrize("good", [9007199254740991, -9007199254740991, 0])
def test_fact_int_safe_bounds_ok(good):
    assert memory.MemoryFact(key="k", value=good).value == good


def test_fact_value_required_not_missing():
    with pytest.raises(ValidationError):
        memory.MemoryFact(key="k")  # type: ignore[call-arg]


@pytest.mark.parametrize("key", ["", "has space", "bad!", "x" * 129, "k?"])
def test_fact_key_pattern(key):
    with pytest.raises(ValidationError):
        memory.MemoryFact(key=key, value="v")


@pytest.mark.parametrize("ident", ["", "has space", "bad!", "x" * 129, "a/b"])
def test_identifier_pattern(ident):
    with pytest.raises(ValidationError):
        memory.MemoryBinding(session_id=ident, thread_id="t1", epoch=1)


@pytest.mark.parametrize("key", ["", "has space", "x" * 129])
def test_idempotency_pattern(key):
    with pytest.raises(ValidationError):
        _capture(idempotency_key=key if key != "" else "")


def test_idempotency_allows_printable_ascii():
    _capture(idempotency_key="!-~AZaz09_.:-")


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "  \n\t  ",
        "hi\x00there",
        "hi\x01",
        "hi\rthere",
        "hi\x7f",
        "x" * 2049,
        "😀" * 513,
    ],
)
def test_body_byte_control_bounds_rejected(body):
    with pytest.raises(ValidationError):
        _capture(body=body)


def test_body_unicode_byte_bound_ok():
    _capture(body="😀" * 512)  # exactly 2048 UTF-8 bytes
    _capture(body="hello\nworld\t!")


def test_fact_string_byte_bound():
    with pytest.raises(ValidationError):
        memory.MemoryFact(key="k", value="x" * 513)
    with pytest.raises(ValidationError):
        memory.MemoryFact(key="k", value="😀" * 129)
    assert memory.MemoryFact(key="k", value="😀" * 128).value is not None
    with pytest.raises(ValidationError):
        memory.MemoryFact(key="k", value="hi\x00")
    memory.MemoryFact(key="k", value="hi\n\t")


@pytest.mark.parametrize("bad", ["hi\ud800", "hi\udc00"])
def test_body_malformed_surrogate_rejected(bad):
    with pytest.raises(ValidationError):
        _capture(body=bad)


@pytest.mark.parametrize("kind", ["bogus", "", "ASSERTION", "Assertion", 123])
def test_bad_kind_rejected(kind):
    with pytest.raises(ValidationError):
        _capture(kind=kind)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["assertion", "observation"])
def test_assertion_observation_require_fact(kind):
    with pytest.raises(ValidationError):
        _capture(kind=kind, fact=None)


def test_inference_forbids_fact_and_needs_parents():
    with pytest.raises(ValidationError):
        _capture(kind="inference", fact=_fact("v"), prerequisite_ids=["p1"])
    with pytest.raises(ValidationError):
        _capture(kind="inference", fact=None, prerequisite_ids=[])


def test_inference_ok_with_parents():
    _capture(kind="inference", fact=None, prerequisite_ids=["p1"])


@pytest.mark.parametrize("parents", [["a", "a"], ["p1", "P1", "p1"], ["x", "x", "y", "z"]])
def test_parent_ids_distinct(parents):
    with pytest.raises(ValidationError):
        _capture(prerequisite_ids=parents)


def test_parent_ids_max8():
    _capture(prerequisite_ids=[f"p{i}" for i in range(8)])
    with pytest.raises(ValidationError):
        _capture(prerequisite_ids=[f"p{i}" for i in range(9)])


def test_node_shares_capture_type_rules():
    with pytest.raises(ValidationError):
        _node(kind="assertion", fact=None)
    with pytest.raises(ValidationError):
        _node(kind="inference", fact=_fact("v"), prerequisite_ids=["p1"])
    with pytest.raises(ValidationError):
        _node(kind="inference", fact=None, prerequisite_ids=[])
    with pytest.raises(ValidationError):
        _node(body="   ")
    with pytest.raises(ValidationError):
        _node(body="hi\x00")
    with pytest.raises(ValidationError):
        _node(prerequisite_ids=["a", "a"])


@pytest.mark.parametrize("state", ["active", "invalidated", "tainted"])
def test_node_non_forgotten_requires_body(state):
    with pytest.raises(ValidationError):
        _node(state=state, body=None, fact=None)


def test_node_forgotten_null_body_fact():
    n = _node(state="forgotten", body=None, fact=None)
    assert n.body is None and n.fact is None
    with pytest.raises(ValidationError):
        _node(state="forgotten", body="kept", fact=None)
    with pytest.raises(ValidationError):
        _node(state="forgotten", body=None, fact=_fact("v"))


@pytest.mark.parametrize("state", ["bogus", "", "deleted"])
def test_node_bad_state(state):
    with pytest.raises(ValidationError):
        _node(state=state)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "scope", ["public_synthetic", "private_cloud_authorized", "", "LOCAL_ONLY"]
)
def test_node_scope_literal(scope):
    with pytest.raises(ValidationError):
        _node(content_scope=scope)  # type: ignore[arg-type]


@pytest.mark.parametrize("prov", ["other", "", "owner"])
def test_node_provenance_literal(prov):
    with pytest.raises(ValidationError):
        _node(provenance=prov)  # type: ignore[arg-type]


def test_query_requires_exactly_one_selector():
    with pytest.raises(ValidationError):
        memory.MemoryQuery(**BASE)
    with pytest.raises(ValidationError):
        memory.MemoryQuery(**BASE, seed_ids=["a"], fact_keys=["k"])
    memory.MemoryQuery(**BASE, seed_ids=["a"])
    memory.MemoryQuery(**BASE, fact_keys=["k"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed_ids": []},
        {"fact_keys": []},
        {"seed_ids": ["a", "a"]},
        {"fact_keys": ["k", "k"]},
        {"seed_ids": [f"s{i}" for i in range(9)]},
        {"fact_keys": [f"k{i}" for i in range(9)]},
        {"seed_ids": ["bad id"]},
        {"fact_keys": ["bad key!"]},
    ],
)
def test_query_bad_selectors(kwargs):
    with pytest.raises(ValidationError):
        memory.MemoryQuery(**BASE, **kwargs)


def test_query_max8_ok():
    memory.MemoryQuery(**BASE, seed_ids=[f"s{i}" for i in range(8)])
    memory.MemoryQuery(**BASE, fact_keys=[f"k{i}" for i in range(8)])


@pytest.mark.parametrize("rev", [9007199254740992, -1, True, "0", 1.0])
def test_expected_revision_is_a_strict_safe_integer(rev):
    with pytest.raises(ValidationError):
        memory.MemoryConfigure(**BASE, expected_revision=rev, enabled=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        _capture(expected_revision=rev)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        memory.MemoryForget(**BASE, expected_revision=rev, idempotency_key="k", node_id="n")  # type: ignore[arg-type]


def test_expected_revision_max_minus_one_ok():
    top = MAX_PLAYBACK_GENERATION - 1
    memory.MemoryConfigure(**BASE, expected_revision=top, enabled=True)
    _capture(expected_revision=top)
    memory.MemoryForget(**BASE, expected_revision=top, idempotency_key="k", node_id="n")


def test_final_revision_can_be_named_for_native_noop_reconciliation():
    # The store reserves the final increment for disable. The wire still needs
    # to name that state when reconciling an already-applied/no-op request.
    top = MAX_PLAYBACK_GENERATION
    memory.MemoryConfigure(**BASE, expected_revision=top, enabled=False)
    _capture(expected_revision=top)
    memory.MemoryForget(**BASE, expected_revision=top, idempotency_key="k", node_id="n")


@pytest.mark.parametrize("rev", [-1, 9007199254740992, True, "1", 1.0])
def test_state_revision_bounds(rev):
    with pytest.raises(ValidationError):
        _state(revision=rev)  # type: ignore[arg-type]


@pytest.mark.parametrize("count", [-1, MAX_PLAYBACK_GENERATION + 1, True, "0", 1.0])
def test_state_node_count_bounds(count):
    with pytest.raises(ValidationError):
        _state(node_count=count)  # type: ignore[arg-type]


def test_quarantined_state_reports_actual_over_quota_metadata_count():
    state = _state(node_count=257, status="quarantined")
    assert state.node_count == 257


@pytest.mark.parametrize("status", ["bogus", "", "disabled"])
def test_state_status_literal(status):
    with pytest.raises(ValidationError):
        _state(status=status)  # type: ignore[arg-type]


@pytest.mark.parametrize("epoch", [0, -1, True, "1", 1.0, 9007199254740992])
def test_binding_epoch_strict_positive(epoch):
    with pytest.raises(ValidationError):
        memory.MemoryBinding(session_id="s", thread_id="t", epoch=epoch)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["ordinal", "source_revision", "source_sequence"])
def test_node_positive_server_ints(field):
    with pytest.raises(ValidationError):
        _node(**{field: 0})
    with pytest.raises(ValidationError):
        _node(**{field: True})


def test_results_wrap_state_and_nodes():
    st = _state()
    nd = _node()
    cap = memory.MemoryCaptureResult(state=st, node=nd)
    assert cap.state.revision == 0
    assert cap.node.node_id == "n1"
    q = memory.MemoryQueryResult(state=st, nodes=[nd])
    assert len(q.nodes) == 1
    f = memory.MemoryForgetResult(state=st, forgotten_ids=["n1"])
    assert f.forgotten_ids == ["n1"]
    with pytest.raises(ValidationError):
        memory.MemoryQueryResult(state=st, nodes=[nd] * 65)
    with pytest.raises(ValidationError):
        memory.MemoryForgetResult(state=st, forgotten_ids=[f"n{i}" for i in range(257)])
