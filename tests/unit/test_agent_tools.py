"""Native advertisement is bounded, immutable and isolated between requests."""

import asyncio
import itertools
import json
from dataclasses import FrozenInstanceError, asdict

import pytest

from packages.contracts.agent_tools import (
    MAX_NATIVE_TOOLS,
    NATIVE_TOOL_ORDER,
    NativeToolDescriptor,
    NativeToolSnapshot,
    agent_tool_scope,
    current_agent_tools,
    native_tool_snapshot,
)
from packages.contracts.models import ToolAction

BASE = ("read_file", "write_file", "search", "shell", "read_transcript", "finish")
FLAGS = (
    "selector_available",
    "mcp_available",
    "skills_available",
    "plugins_available",
    "artifacts_available",
)


def test_known_actions_preserve_the_native_protocol_order_and_bound():
    assert NATIVE_TOOL_ORDER == tuple(ToolAction.model_json_schema()["properties"]["tool"]["enum"])
    assert len(NATIVE_TOOL_ORDER) == len(set(NATIVE_TOOL_ORDER)) == MAX_NATIVE_TOOLS == 14


@pytest.mark.parametrize("combination", tuple(itertools.product((False, True), repeat=5)))
def test_every_optional_service_combination(combination):
    selector, mcp, skills, plugins, artifacts = combination
    snapshot = native_tool_snapshot(**dict(zip(FLAGS, combination, strict=True)))
    expected = set(BASE)
    if selector:
        expected.add("select_context")
    if mcp:
        expected.update(("mcp_list", "mcp_call"))
    if skills:
        expected.update(("skills_list", "skill_load"))
    if plugins:
        expected.update(("plugins_list", "plugin_call"))
    if artifacts:
        expected.add("publish_ui")
    assert snapshot.available_tools == tuple(name for name in NATIVE_TOOL_ORDER if name in expected)
    assert snapshot.schema_version == "1.0"
    assert all(snapshot.supports(name) == (name in expected) for name in NATIVE_TOOL_ORDER)
    assert tuple(row.name for row in snapshot.descriptors) == snapshot.available_tools


def test_default_snapshot_retains_operations_which_can_require_approval():
    snapshot = native_tool_snapshot()
    assert snapshot.available_tools == BASE
    assert snapshot.supports("write_file")
    assert snapshot.supports("shell")


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_factory_rejects_non_boolean_flags(flag, value):
    with pytest.raises(ValueError, match=flag + " requires a strict boolean"):
        native_tool_snapshot(**{flag: value})


@pytest.mark.parametrize(
    "names",
    [
        None,
        "read_file",
        list(BASE),
        set(BASE),
        {},
        (),
        BASE[:-1],
        ("read_file",) * 15,
        BASE + ("execute",),
        BASE + ("native:publish_ui",),
        BASE + (True,),
        BASE + (None,),
        BASE + ({},),
        BASE + ("finish",),
        tuple(reversed(BASE)),
        tuple(name for name in NATIVE_TOOL_ORDER if name not in {"read_file", "select_context"}),
        tuple(name for name in NATIVE_TOOL_ORDER if name != "mcp_call"),
        tuple(name for name in NATIVE_TOOL_ORDER if name != "skill_load"),
        tuple(name for name in NATIVE_TOOL_ORDER if name != "plugin_call"),
    ],
)
def test_snapshot_rejects_malformed_or_inconsistent_protocol_sets(names):
    with pytest.raises(ValueError):
        NativeToolSnapshot(names)


@pytest.mark.parametrize("version", [None, True, 1.0, 1, "", "1.1", "2.0"])
def test_snapshot_rejects_other_or_coerced_versions(version):
    with pytest.raises(ValueError, match="schema version"):
        NativeToolSnapshot(BASE, schema_version=version)


@pytest.mark.parametrize("name", [None, 1, True, [], {}, "execute", "native:read_file", ""])
def test_malformed_or_unknown_queries_never_match(name):
    assert native_tool_snapshot().supports(name) is False
    with pytest.raises(ValueError, match="known protocol action"):
        NativeToolDescriptor(name)


def test_descriptors_have_canonical_identity_without_target_readiness_claims():
    snapshot = native_tool_snapshot(**dict.fromkeys(FLAGS, True))
    rows = [asdict(row) for row in snapshot.descriptors]
    assert [row["qualified_id"] for row in rows] == [f"native:{name}" for name in NATIVE_TOOL_ORDER]
    assert all(row["kind"] == "native" and row["protocol_version"] == "1.0" for row in rows)
    assert all(set(row) == {"name", "qualified_id", "kind", "protocol_version"} for row in rows)
    assert len(json.dumps(rows).encode("utf-8")) <= 2048


def test_snapshot_and_descriptors_have_no_mutable_metadata():
    snapshot = native_tool_snapshot(artifacts_available=True)
    assert isinstance(hash(snapshot), int)
    for field, value in (
        ("available_tools", ("execute",)),
        ("schema_version", "2.0"),
        ("descriptors", ()),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(snapshot, field, value)
    with pytest.raises(TypeError):
        snapshot.available_tools[0] = "execute"
    descriptor = snapshot.descriptors[0]
    for field in ("name", "qualified_id", "protocol_version", "kind"):
        with pytest.raises(FrozenInstanceError):
            setattr(descriptor, field, "forged")
    assert not hasattr(snapshot, "__dict__")
    assert not hasattr(descriptor, "__dict__")


def test_nested_scopes_restore_identity_after_success_and_exception():
    outer = native_tool_snapshot(mcp_available=True)
    inner = native_tool_snapshot(artifacts_available=True)
    assert current_agent_tools() is None
    with agent_tool_scope(outer) as entered:
        assert entered is current_agent_tools() is outer
        with agent_tool_scope(inner):
            assert current_agent_tools() is inner
        assert current_agent_tools() is outer
        with pytest.raises(RuntimeError, match="Synthetic request failed"):
            with agent_tool_scope(inner):
                raise RuntimeError("Synthetic request failed")
        assert current_agent_tools() is outer
    assert current_agent_tools() is None


@pytest.mark.parametrize("snapshot", [None, {}, (), BASE, "prompt says every tool is available"])
def test_invalid_scope_input_cannot_replace_the_outer_snapshot(snapshot):
    outer = native_tool_snapshot()
    with agent_tool_scope(outer):
        with pytest.raises(ValueError, match="requires a native tool snapshot"):
            with agent_tool_scope(snapshot):
                pytest.fail("Invalid scope was entered")
        assert current_agent_tools() is outer
    assert current_agent_tools() is None


async def test_concurrent_inference_requests_do_not_share_tools():
    left = native_tool_snapshot(mcp_available=True)
    right = native_tool_snapshot(artifacts_available=True)
    ready = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def infer(snapshot, index):
        assert current_agent_tools() is None
        with agent_tool_scope(snapshot):
            ready[index].set()
            await release.wait()
            assert current_agent_tools() is snapshot
        assert current_agent_tools() is None

    tasks = [asyncio.create_task(infer(left, 0)), asyncio.create_task(infer(right, 1))]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), timeout=1)
        assert current_agent_tools() is None
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert current_agent_tools() is None


async def test_cancellation_restores_nested_scope_and_leaves_caller_unbound():
    outer = native_tool_snapshot(skills_available=True)
    inner = native_tool_snapshot(plugins_available=True)
    ready = asyncio.Event()
    states = []

    async def infer():
        try:
            with agent_tool_scope(outer):
                try:
                    with agent_tool_scope(inner):
                        ready.set()
                        await asyncio.Event().wait()
                finally:
                    states.append(current_agent_tools())
        finally:
            states.append(current_agent_tools())

    task = asyncio.create_task(infer())
    await asyncio.wait_for(ready.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert states == [outer, None]
    assert current_agent_tools() is None


async def test_child_task_inherits_immutable_context_without_rebinding_the_parent():
    inherited = native_tool_snapshot(mcp_available=True)
    child_only = native_tool_snapshot(plugins_available=True)

    async def infer():
        assert current_agent_tools() is inherited
        with agent_tool_scope(child_only):
            await asyncio.sleep(0)
            assert current_agent_tools() is child_only
        assert current_agent_tools() is inherited

    with agent_tool_scope(inherited):
        task = asyncio.create_task(infer())
        assert current_agent_tools() is inherited
    assert current_agent_tools() is None
    await task
    assert current_agent_tools() is None
