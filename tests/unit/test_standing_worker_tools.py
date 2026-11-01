"""Independent standing task restriction oracles, frozen before implementation."""

from dataclasses import FrozenInstanceError

import pytest

from packages.contracts.agent_tools import (
    NATIVE_TOOL_ORDER,
    NativeToolSnapshot,
    agent_tool_scope,
    current_agent_tools,
    native_tool_snapshot,
)

FILES = ("read_file", "write_file", "finish")
BASE = ("read_file", "write_file", "search", "shell", "read_transcript", "finish")


def restricted(names=FILES, **changes):
    assert "task_subset" in NativeToolSnapshot.__dataclass_fields__, (
        "Frozen native task-subset capability is absent"
    )
    return NativeToolSnapshot(names, task_subset="native-files-v1", **changes)


def test_existing_default_snapshot_keeps_all_original_base_operations():
    snapshot = native_tool_snapshot()
    assert snapshot.available_tools == BASE
    assert snapshot.owner_disabled is None and snapshot.owner_revision is None
    assert all(snapshot.supports(name) for name in BASE)


@pytest.mark.parametrize("disabled", [None, ("write_file",), ("read_file", "write_file")])
def test_standing_subset_intersects_actual_owner_mask_without_inventing_an_owner(disabled):
    changes = {} if disabled is None else {"owner_disabled": disabled, "owner_revision": 7}
    names = tuple(name for name in FILES if disabled is None or name not in disabled)
    snapshot = restricted(names, **changes)
    assert snapshot.available_tools == names and snapshot.task_subset == "native-files-v1"
    assert snapshot.owner_disabled == disabled
    assert snapshot.owner_revision == (None if disabled is None else 7)
    assert tuple(item.name for item in snapshot.descriptors) == names
    assert all(snapshot.supports(name) == (name in names) for name in NATIVE_TOOL_ORDER)


@pytest.mark.parametrize("value", [True, "files", ["native-files-v1"]])
def test_subset_requires_the_exact_strict_literal(value):
    restricted()
    with pytest.raises(ValueError):
        NativeToolSnapshot(FILES, task_subset=value)


@pytest.mark.parametrize(
    "names",
    [BASE, FILES[:-1], tuple(reversed(FILES)), ("read_file", "read_file", "finish")],
)
def test_restriction_cannot_advertise_extra_actions_or_omit_finish(names):
    restricted()
    with pytest.raises(ValueError):
        NativeToolSnapshot(names, task_subset="native-files-v1")


def test_actual_owner_mask_cannot_disable_finish_or_reenable_a_disabled_file_tool():
    restricted()
    for names, disabled in [(FILES, ("write_file",)), (("read_file",), ("finish",))]:
        with pytest.raises(ValueError):
            NativeToolSnapshot(
                names,
                task_subset="native-files-v1",
                owner_disabled=disabled,
                owner_revision=1,
            )


def test_subset_is_immutable_and_nested_scope_restores_original_identity():
    original, standing = native_tool_snapshot(), restricted()
    with agent_tool_scope(original):
        with agent_tool_scope(standing):
            assert current_agent_tools() is standing
            with pytest.raises(FrozenInstanceError):
                standing.task_subset = None
        assert current_agent_tools() is original
    assert current_agent_tools() is None
