import pytest
from pydantic import ValidationError

from packages.contracts.memory import MemoryNode, MemoryState
from packages.contracts.memory_context import (
    MemoryContextPin,
    MemoryRecallRequest,
    MemoryRecallResult,
    MemoryUseConfigure,
    recall_digest,
)

SCOPE = dict(session_id="session", thread_id="thread", epoch=1)


def node(ordinal=1, **changes):
    return MemoryNode(
        **SCOPE,
        **{
            "node_id": f"node-{ordinal}",
            "ordinal": ordinal,
            "kind": "assertion",
            "source_event_id": f"source-{ordinal}",
            "source_revision": 1,
            "source_sequence": ordinal,
            "prerequisite_ids": [],
            "body": "Public owner-curated note 🌙",
            "fact": {"key": f"fact-{ordinal}", "value": True},
            "state": "active",
            "content_scope": "local_only",
            "provenance": "owner_curated",
            **changes,
        },
    )


def result(nodes):
    state = MemoryState(**SCOPE, enabled=True, revision=3, node_count=len(nodes), status="ready")
    return dict(state=state, nodes=nodes, closure_sha256=recall_digest(state, nodes))


def pin(**changes):
    return {
        "schema_version": "1.0",
        "implementation_id": "korina-dag-v1",
        "use_generation": 1,
        "namespace_revision": 3,
        "closure_sha256": "a" * 64,
        "selector_ids": ["node-1"],
        **changes,
    }


def test_complete_ordered_snapshot_and_explicit_pin_round_trip():
    nodes = [node(), node(2, kind="inference", fact=None, prerequisite_ids=["node-1"])]
    snapshot = MemoryRecallResult(**result(nodes))
    assert snapshot.implementation_id == "korina-dag-v1"
    assert MemoryRecallResult.model_validate_json(snapshot.model_dump_json()) == snapshot
    value = MemoryContextPin.model_validate(pin())
    assert MemoryContextPin.model_validate_json(value.model_dump_json()) == value
    assert len(value.model_dump_json().encode()) < 2048
    missing = pin()
    del missing["schema_version"]
    with pytest.raises(ValidationError):
        MemoryContextPin.model_validate(missing)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "2.0"},
        {"use_generation": True},
        {"use_generation": 0},
        {"namespace_revision": False},
        {"namespace_revision": 2**53},
        {"closure_sha256": "A" * 64},
        {"selector_ids": []},
        {"selector_ids": ["node-1", "node-1"]},
        {"extra": "refused"},
    ],
)
def test_pin_rejects_unversioned_unsafe_or_ambiguous_metadata(change):
    with pytest.raises(ValidationError):
        MemoryContextPin.model_validate(pin(**change))


@pytest.mark.parametrize(
    "nodes",
    [
        [node(), node()],
        [node(2), node()],
        [node(2, kind="inference", fact=None, prerequisite_ids=["absent"])],
        [node(state="tainted")],
    ],
)
def test_snapshot_rejects_incomplete_order_duplicate_or_inactive_dependencies(nodes):
    with pytest.raises(ValidationError):
        MemoryRecallResult(**result(nodes))


def test_snapshot_checks_native_depth_and_immutable_digest():
    chain = [node()]
    for ordinal in range(2, 18):
        chain.append(
            node(ordinal, kind="inference", fact=None, prerequisite_ids=[f"node-{ordinal - 1}"])
        )
    assert MemoryRecallResult(**result(chain[:16]))
    with pytest.raises(ValidationError):
        MemoryRecallResult(**result(chain))
    value = result([node()])
    value["closure_sha256"] = "b" * 64
    with pytest.raises(ValidationError):
        MemoryRecallResult(**value)


def test_use_control_distinguishes_off_from_replacement_and_requires_selection_name():
    assert MemoryUseConfigure(
        **SCOPE, expected_runtime_id="runtime", expected_generation=0, enabled=False
    )
    assert MemoryUseConfigure(
        **SCOPE,
        expected_runtime_id="runtime",
        expected_generation=0,
        enabled=True,
        implementation_id="compatible-replacement",
        selector_ids=[],
    )
    for change in [
        dict(enabled=True),
        dict(enabled=False, implementation_id="korina-dag-v1"),
        dict(enabled=False, selector_ids=["node-1"]),
    ]:
        with pytest.raises(ValidationError):
            MemoryUseConfigure(
                **SCOPE, expected_runtime_id="runtime", expected_generation=0, **change
            )
    with pytest.raises(ValidationError):
        MemoryUseConfigure(**SCOPE, expected_generation=0, enabled=False)
    with pytest.raises(ValidationError):
        MemoryRecallRequest(**SCOPE, seed_ids=["node-1"] * 2)
