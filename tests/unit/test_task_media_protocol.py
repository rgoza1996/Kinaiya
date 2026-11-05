"""Conditional native media inputs; synthetic bytes are not decoder evidence."""

import copy
import hashlib
import json
from functools import reduce
from operator import or_
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from core.policy import ToolPolicy
from harnesses.seed import build_messages
from packages.contracts import native_tool_schemas as logical
from packages.contracts.agent_tools import native_tool_snapshot
from packages.contracts.tool_catalog import CatalogQuery
from packages.providers import meta_action_wire as typed
from services.agent.artifacts import ArtifactInvalid, ArtifactStore
from services.agent.media_assets import NativeMediaAssets
from services.agent.tool_catalog import _native

LEGACY = native_tool_snapshot(artifacts_available=True)
ENABLED = native_tool_snapshot(artifacts_available=True, task_harness_available=True)
ASSET = "media_" + "a" * 32


def create(asset=ASSET):
    return {
        "title": "Synthetic media",
        "summary": "Fixture only",
        "components": [{"id": "clip", "type": "media", "data": {"asset_id": asset}}],
    }


def update(asset=ASSET):
    return {
        "artifact_id": "synthetic-artifact",
        "expected_revision": 1,
        "updates": [{"id": "clip", "data": {"asset_id": asset}}],
    }


@pytest.mark.parametrize("arguments", [create(), update()])
def test_media_alternative_exists_only_with_native_harness_capability(arguments):
    base = logical.native_tool_schema("publish_ui")
    enabled = logical.native_tool_schema("publish_ui", media_available=True)
    assert not Draft202012Validator(base).is_valid(arguments)
    Draft202012Validator(enabled).validate(arguments)
    action = {"tool": "publish_ui", "arguments": arguments}
    envelope = typed.encode_action(action, tools=ENABLED.available_tools)
    Draft202012Validator(typed.action_schema(ENABLED.available_tools)).validate(envelope)
    assert typed.decode_action(envelope, ENABLED.available_tools) == action
    with pytest.raises(typed.TypedWireError):
        typed.decode_action(envelope, LEGACY.available_tools)
    with pytest.raises(typed.TypedWireError):
        typed.encode_action(action)


def test_default_logical_and_typed_branches_keep_original_nine_type_bytes():
    assert logical.native_tool_schema("publish_ui") == logical._SCHEMAS["publish_ui"]
    adapter = TypeAdapter(reduce(or_, (typed._ACTIONS[name] for name in LEGACY.available_tools)))
    expected = adapter.json_schema()
    definitions = expected.pop("$defs", {})
    expected = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"action": expected},
        "required": ["action"],
        "$defs": definitions,
    }

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    assert canonical(typed.action_schema(LEGACY.available_tools)) == canonical(expected)
    assert "ComponentNativeMedia" not in canonical(expected)
    default = logical.native_tool_definition("publish_ui")
    enabled = logical.native_tool_definition("publish_ui", media_available=True)
    assert default["schema_sha256"] != enabled["schema_sha256"]
    assert "native_media_asset_task_revision_scope_binding" not in default["requirements"]
    assert "native_media_asset_task_revision_scope_binding" in enabled["requirements"]


@pytest.mark.parametrize(
    "data",
    [
        {"asset_id": "https://example.invalid/video.mp4"},
        {"asset_id": "../source.mp4"},
        {"asset_id": "media_" + "A" * 32},
        {"asset_id": ASSET + "\n"},
        {"asset_id": ASSET, "url": "https://example.invalid"},
        {"asset_id": ASSET, "path": "private.mp4"},
        {"asset_id": ASSET, "bytes": "AA=="},
        {"asset_id": 0},
        {"asset_id": None},
        {},
    ],
)
def test_media_data_cannot_supply_paths_urls_uploads_or_extra_authority(data):
    arguments = create()
    arguments["components"][0]["data"] = data
    assert not Draft202012Validator(
        logical.native_tool_schema("publish_ui", media_available=True)
    ).is_valid(arguments)
    with pytest.raises(typed.TypedWireError):
        typed.encode_action(
            {"tool": "publish_ui", "arguments": arguments}, tools=ENABLED.available_tools
        )


def test_seed_and_selected_catalog_describe_only_the_enabled_native_media_shape(tmp_path):
    args = ("Synthetic task", 1, [], [], "write=False")
    default = build_messages(*args, tool_snapshot=LEGACY)[0]["content"]
    enabled = build_messages(*args, tool_snapshot=ENABLED)[0]["content"]
    assert "Native media:" not in default
    assert '"select"|"ask"|"media"' in enabled
    assert 'data exactly {"asset_id"' in enabled
    policy = ToolPolicy(tmp_path)
    runtime = SimpleNamespace(tool_snapshot=lambda _: ENABLED, policy=policy, tool_permissions=None)
    row = _native(runtime, None, CatalogQuery(kind="native", name="publish_ui"))[0]
    assert row["input_schema"] == logical.native_tool_schema("publish_ui", media_available=True)


def test_decoded_media_publication_remains_bound_to_actual_native_scope(tmp_path):
    owner_dir = tmp_path / "owner"
    owner_dir.mkdir(mode=0o700)
    assets = NativeMediaAssets(owner_dir / "media.sqlite")
    store = ArtifactStore(tmp_path / "artifacts.sqlite", media_assets=assets)
    binding = {
        "task_id": "synthetic-task",
        "objective_revision": 1,
        "session_id": "synthetic-session",
        "thread_id": "synthetic-thread",
        "epoch": 1,
    }
    scope = "private_cloud_authorized"
    source = b"\x00\x00\x00\x18ftypmp42synthetic"
    try:
        receipt = assets.register(
            binding=binding,
            content_scope=scope,
            source_bytes=source,
            source_sha256=hashlib.sha256(source).hexdigest(),
            rendered_mp4=source + b"highlighted",
            overlay_sha256="b" * 64,
            width=2,
            height=2,
            duration_seconds=1,
            fps_num=1,
            fps_den=1,
            frame_count=1,
        )
        action = {"tool": "publish_ui", "arguments": create(receipt["asset_id"])}
        envelope = typed.encode_action(action, tools=ENABLED.available_tools)
        arguments = typed.decode_action(envelope, ENABLED.available_tools)["arguments"]
        task = SimpleNamespace(**binding, content_scope=scope)
        publication = store.publish(task, arguments)
        assert publication["content_scope"] == scope
        wrong = SimpleNamespace(**binding, content_scope="public_synthetic")
        with pytest.raises(ArtifactInvalid, match="does not match this task scope"):
            store.publish(wrong, arguments)
        duplicate = copy.deepcopy(arguments)
        duplicate["components"].append({**duplicate["components"][0], "id": "other_clip"})
        with pytest.raises(ArtifactInvalid, match="at most one media"):
            store.publish(task, duplicate)
    finally:
        store.close()
        assets.close()
