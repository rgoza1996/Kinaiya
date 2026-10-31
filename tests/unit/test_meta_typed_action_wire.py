"""Pure typed-v2 projection checks; native execution remains independently authoritative."""

import copy
import itertools
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from packages.contracts.agent_tools import NATIVE_TOOL_ORDER, native_tool_snapshot
from packages.providers.meta_action_wire import (
    TypedWireError,
    action_schema,
    decode_action,
    encode_action,
)
from services.agent.artifacts import ArtifactConflict, ArtifactInvalid, ArtifactStore

_FLAGS = (
    "selector_available",
    "mcp_available",
    "skills_available",
    "plugins_available",
    "artifacts_available",
    "auxiliary_available",
    "catalog_available",
)
_SNAPSHOTS = tuple(
    native_tool_snapshot(**dict(zip(_FLAGS, flags, strict=True)))
    for flags in itertools.product((False, True), repeat=7)
)
_ALL_TOOLS = _SNAPSHOTS[-1].available_tools
_BASE_TOOLS = _SNAPSHOTS[0].available_tools
_UI_TYPES = ("text", "status", "table", "chart", "diagram", "source", "number", "select", "ask")
_MARKER = "synthetic-untrusted-wire-marker"
_UNSET = object()


def _component(kind):
    data = {
        "text": {"text": "Fictional example"},
        "status": {"text": "Simulated"},
        "table": {
            "columns": ["Null", "Bool", "Integer", "Float", "Text"],
            "rows": [[None, False, 0, 0.0, ""], [None, True, 3, 1.25, "fictional"]],
            "selectable": False,
        },
        "chart": {"labels": ["", "Fictional"], "values": [0, 1.25], "unit": ""},
        "diagram": {
            "nodes": [{"id": "customer", "label": ""}, {"id": "order", "label": "Order"}],
            "edges": [{"from": "customer", "to": "order", "label": ""}],
        },
        "source": {
            "label": "Synthetic",
            "url": "https://example.invalid/data",
            "freshness": "simulated",
        },
        "number": {"label": "Amount", "value": 0, "min": 0, "max": 3.5},
        "select": {"label": "Pick", "options": ["First", "Second"], "value": "First"},
        "ask": {"label": "Ask", "message": "Explain this fictional example"},
    }
    return {"id": f"demo_{kind}", "type": kind, "data": copy.deepcopy(data[kind])}


def _create(*kinds):
    return {
        "tool": "publish_ui",
        "arguments": {
            "title": "Synthetic UI",
            "summary": "Fictional codec test data",
            "components": [_component(kind) for kind in kinds or _UI_TYPES],
        },
    }


def _update(kind):
    component = _component(kind)
    return {
        "tool": "publish_ui",
        "arguments": {
            "artifact_id": "synthetic-artifact",
            "expected_revision": 1,
            "updates": [{"id": component["id"], "data": component["data"]}],
        },
    }


def _actions():
    return {
        "tool_catalog": {"kind": "native", "name": "write_file"},
        "read_file": {"path": "fictional.txt"},
        "write_file": {"path": "fictional.txt", "content": ""},
        "search": {"pattern": ""},
        "shell": {"command": "true"},
        "read_transcript": {"event_ids": []},
        "select_context": {"path": "."},
        "mcp_list": {},
        "mcp_call": {
            "server_id": "synthetic-server",
            "mcp_tool": "count",
            "mcp_arguments_json": '{ "count": 0, "nullable": null, "enabled": false }',
        },
        "skills_list": {},
        "skill_load": {"skill_id": "synthetic:skill"},
        "plugins_list": {},
        "plugin_call": {
            "plugin_id": "synthetic-plugin",
            "plugin_arguments_json": '{ "count": 0, "nullable": null, "enabled": false }',
        },
        "publish_ui": _create("text")["arguments"],
        "consult_model": {"purpose": "analysis", "prompt": "Synthetic task text"},
        "finish": {"summary": "Synthetic evidence was inspected", "objective_revision": 1},
    }


def _resolve(schema, reference):
    assert reference.startswith("#/"), "Only local schema references are supported"
    value = schema
    for part in reference[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def _expand(schema, value=_UNSET, active=()):
    value = schema if value is _UNSET else value
    if isinstance(value, list):
        return [_expand(schema, item, active) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        assert reference not in active, "Recursive strict schemas are forbidden"
        resolved = _expand(schema, _resolve(schema, reference), (*active, reference))
        siblings = {key: item for key, item in value.items() if key != "$ref"}
        assert not set(resolved).intersection(siblings), "Conflicting reference siblings"
        return {**resolved, **_expand(schema, siblings, active)}
    return {
        key: _expand(schema, item, active)
        for key, item in value.items()
        if key not in {"$defs", "definitions"}
    }


def _schema_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _schema_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _schema_dicts(item)


def _schema_depth(value):
    if not isinstance(value, dict):
        return 0
    types = value.get("type", [])
    types = [types] if isinstance(types, str) else types
    here = int("object" in types or "array" in types)
    children = list(value.get("properties", {}).values())
    if isinstance(value.get("items"), dict):
        children.append(value["items"])
    children.extend(value.get("anyOf", []))
    return here + max((_schema_depth(child) for child in children), default=0)


def _node_count(value):
    if isinstance(value, dict):
        return 1 + sum(_node_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_node_count(item) for item in value)
    return 1


def _schema_stats(schema):
    properties = enum_count = string_chars = 0
    largest_enum = largest_enum_chars = 0
    for node in _schema_dicts(schema):
        names = list(node.get("properties", {}))
        names.extend(node.get("$defs", {}))
        names.extend(node.get("definitions", {}))
        properties += len(node.get("properties", {}))
        string_chars += sum(len(name) for name in names)
        values = node.get("enum", [])
        enum_count += len(values)
        enum_chars = sum(len(value) for value in values if isinstance(value, str))
        string_chars += enum_chars
        largest_enum = max(largest_enum, len(values))
        if len(values) > 250:
            largest_enum_chars = max(largest_enum_chars, enum_chars)
        if isinstance(node.get("const"), str):
            string_chars += len(node["const"])
    expanded = _expand(schema)
    return {
        "depth": _schema_depth(expanded),
        "properties": properties,
        "enum_count": enum_count,
        "string_chars": string_chars,
        "largest_enum": largest_enum,
        "large_enum_chars": largest_enum_chars,
        "expanded_nodes": _node_count(expanded),
    }


@pytest.mark.parametrize(
    "snapshot", _SNAPSHOTS, ids=lambda snapshot: "-".join(snapshot.available_tools)
)
def test_all_128_native_selections_emit_strict_closed_bounded_schemas(snapshot):
    schema = action_schema(snapshot.available_tools)
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"action"}
    assert schema["required"] == ["action"]
    assert schema["additionalProperties"] is False
    assert "anyOf" not in schema and "oneOf" not in schema
    expanded = _expand(schema)
    alternatives = expanded["properties"]["action"]["anyOf"]
    names = []
    for branch in alternatives:
        assert set(branch["properties"]) == {"tool", "arguments"}
        tool = branch["properties"]["tool"]
        literals = tool.get("enum", [tool.get("const")])
        assert len(literals) == 1 and type(literals[0]) is str
        names.append(literals[0])
    assert names == list(snapshot.available_tools)
    forbidden = {
        "oneOf",
        "allOf",
        "discriminator",
        "$recursiveRef",
        "$dynamicRef",
        "patternProperties",
    }
    for node in _schema_dicts(schema):
        assert not forbidden.intersection(node)
        if "properties" in node or node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
    stats = _schema_stats(schema)
    assert stats["depth"] <= 10, stats
    assert stats["properties"] <= 5000, stats
    assert stats["enum_count"] <= 1000, stats
    assert stats["string_chars"] <= 120000, stats
    assert stats["large_enum_chars"] <= 15000, stats
    assert stats["expanded_nodes"] <= 200000, stats
    validator = Draft202012Validator(schema)
    for name in snapshot.available_tools:
        flat = {"tool": name, "arguments": _actions()[name]}
        wire = encode_action(flat)
        validator.validate(wire)
        assert decode_action(wire, snapshot.available_tools) == flat


def test_all_sixteen_native_operations_are_covered_without_foreign_schema_decoding():
    assert _ALL_TOOLS == NATIVE_TOOL_ORDER
    assert set(_actions()) == set(_ALL_TOOLS)
    for name in ("mcp_call", "plugin_call"):
        flat = {"tool": name, "arguments": _actions()[name]}
        decoded = decode_action(encode_action(flat), _ALL_TOOLS)
        assert decoded == flat
        field = "mcp_arguments_json" if name == "mcp_call" else "plugin_arguments_json"
        assert type(decoded["arguments"][field]) is str
        assert decoded["arguments"][field] == flat["arguments"][field]


def test_schema_and_codec_results_are_defensive_fresh_copies():
    baseline = action_schema(_ALL_TOOLS)
    first = action_schema(_ALL_TOOLS)
    first["properties"]["action"].clear()
    first["properties"][_MARKER] = {"type": "string"}
    assert action_schema(_ALL_TOOLS) == baseline
    flat = _create()
    saved_flat = copy.deepcopy(flat)
    wire = encode_action(flat)
    assert flat == saved_flat
    saved_wire = copy.deepcopy(wire)
    decoded = decode_action(wire, _ALL_TOOLS)
    assert wire == saved_wire
    decoded["arguments"]["components"][0]["data"]["text"] = _MARKER
    assert wire == saved_wire and flat == saved_flat
    wire["action"]["arguments"]["components"][0]["data"]["text"] = _MARKER
    assert flat == saved_flat


@pytest.mark.parametrize("kind", _UI_TYPES)
@pytest.mark.parametrize("operation", ["create", "update"])
def test_all_nine_ui_data_types_roundtrip_through_create_and_update(kind, operation):
    flat = _create(kind) if operation == "create" else _update(kind)
    wire = encode_action(flat)
    Draft202012Validator(action_schema(_ALL_TOOLS)).validate(wire)
    assert decode_action(wire, _ALL_TOOLS) == flat
    assert json.loads(json.dumps(wire, allow_nan=False)) == wire


def test_only_declared_optional_nulls_are_omitted_with_real_values_preserved():
    flat = _create()
    wire = encode_action(flat)
    args = wire["action"]["arguments"]
    assert args["layout"] is None
    components = {component["type"]: component for component in args["components"]}
    assert all(component["title"] is None for component in components.values())
    assert components["status"]["data"]["tone"] is None
    assert components["chart"]["data"]["style"] is None
    assert components["source"]["data"]["observed_at"] is None
    assert components["number"]["data"]["step"] is None
    assert components["diagram"]["data"]["nodes"][0]["detail"] is None
    decoded = decode_action(wire, _ALL_TOOLS)
    assert decoded == flat
    data = {
        component["type"]: component["data"] for component in decoded["arguments"]["components"]
    }
    cells = data["table"]["rows"][0]
    assert cells == [None, False, 0, 0.0, ""]
    assert [type(cell) for cell in cells] == [type(None), bool, int, float, str]
    assert data["table"]["selectable"] is False
    assert data["chart"]["unit"] == ""
    assert type(data["chart"]["values"][0]) is int
    assert type(data["chart"]["values"][1]) is float
    assert type(data["number"]["value"]) is int
    assert type(data["number"]["max"]) is float


def test_explicit_optional_values_and_empty_lists_strings_zero_are_not_defaulted():
    flat = _create("table", "diagram", "source", "number", "status", "chart")
    flat["arguments"]["layout"] = "grid"
    components = {component["type"]: component for component in flat["arguments"]["components"]}
    components["table"]["title"] = ""
    components["table"]["data"]["rows"] = []
    components["diagram"]["data"]["edges"] = []
    components["diagram"]["data"]["nodes"][0]["detail"] = ""
    components["source"]["data"]["observed_at"] = 0
    components["number"]["data"]["step"] = 0.25
    components["status"]["data"]["tone"] = "neutral"
    components["chart"]["data"]["style"] = "line"
    assert decode_action(encode_action(flat), _ALL_TOOLS) == flat
    transcript = {"tool": "read_transcript", "arguments": {"event_ids": [], "after": 0, "limit": 1}}
    assert decode_action(encode_action(transcript), _ALL_TOOLS) == transcript


def test_numeric_looking_table_strings_remain_strings():
    flat = _create("table")
    flat["arguments"]["components"][0]["data"] = {
        "columns": ["Literal"],
        "rows": [["1.25"], ["false"], ["null"]],
    }
    decoded = decode_action(encode_action(flat), _ALL_TOOLS)
    assert decoded == flat
    assert all(type(row[0]) is str for row in decoded["arguments"]["components"][0]["data"]["rows"])


@pytest.mark.parametrize("kind", _UI_TYPES)
def test_component_data_must_match_the_selected_ui_type(kind):
    wire = encode_action(_create(kind))
    wire["action"]["arguments"]["components"][0]["data"] = {"not_native_data": _MARKER}
    assert not Draft202012Validator(action_schema(_ALL_TOOLS)).is_valid(wire)
    with pytest.raises(TypedWireError):
        decode_action(wire, _ALL_TOOLS)


def test_unknown_component_types_and_cross_tool_argument_fields_are_rejected():
    wire = encode_action(_create("text"))
    wire["action"]["arguments"]["components"][0]["type"] = _MARKER
    with pytest.raises(TypedWireError):
        decode_action(wire, _ALL_TOOLS)
    for name in _ALL_TOOLS:
        wire = encode_action({"tool": name, "arguments": _actions()[name]})
        wire["action"]["arguments"][_MARKER] = _MARKER
        with pytest.raises(TypedWireError):
            decode_action(wire, _ALL_TOOLS)


@pytest.mark.parametrize("name", _ALL_TOOLS)
def test_every_declared_property_is_required_on_the_strict_wire(name):
    flat = {"tool": name, "arguments": _actions()[name]}
    complete = encode_action(flat)
    for field in complete["action"]["arguments"]:
        broken = copy.deepcopy(complete)
        del broken["action"]["arguments"][field]
        assert not Draft202012Validator(action_schema(_ALL_TOOLS)).is_valid(broken)
        with pytest.raises(TypedWireError):
            decode_action(broken, _ALL_TOOLS)


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("action",),
        ("action", "arguments"),
        ("action", "arguments", "components", 0),
        ("action", "arguments", "components", 0, "data"),
        ("action", "arguments", "components", 4, "data", "nodes", 0),
        ("action", "arguments", "components", 4, "data", "edges", 0),
    ],
)
def test_unknown_fields_are_rejected_at_every_ui_object_boundary(path):
    wire = encode_action(_create())
    target = wire
    for part in path:
        target = target[part]
    target[_MARKER] = _MARKER
    assert not Draft202012Validator(action_schema(_ALL_TOOLS)).is_valid(wire)
    with pytest.raises(TypedWireError) as caught:
        decode_action(wire, _ALL_TOOLS)
    assert _MARKER not in str(caught.value)


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("field", ["objective_revision", "session_id", "task_id", "evidence"])
def test_ui_publication_never_accepts_native_metadata_or_unrelated_active_arguments(
    operation, field
):
    flat = _create("text") if operation == "create" else _update("text")
    bad_history = copy.deepcopy(flat)
    bad_history["arguments"][field] = 1 if field == "objective_revision" else _MARKER
    with pytest.raises(TypedWireError):
        encode_action(bad_history)
    wire = encode_action(flat)
    wire["action"]["arguments"][field] = bad_history["arguments"][field]
    with pytest.raises(TypedWireError):
        decode_action(wire, _ALL_TOOLS)


def test_mixed_create_update_and_unknown_update_fields_are_rejected():
    create = _create("text")
    update = _update("text")
    mixed = encode_action(create)
    mixed["action"]["arguments"].update(update["arguments"])
    with pytest.raises(TypedWireError):
        decode_action(mixed, _ALL_TOOLS)
    with pytest.raises(TypedWireError):
        encode_action({"tool": "publish_ui", "arguments": mixed["action"]["arguments"]})
    for path in [("updates", 0), ("updates", 0, "data")]:
        wire = encode_action(update)
        target = wire["action"]["arguments"]
        for part in path:
            target = target[part]
        target[_MARKER] = True
        with pytest.raises(TypedWireError):
            decode_action(wire, _ALL_TOOLS)


@pytest.mark.parametrize("value", [True, False, "1", 1.5, None])
@pytest.mark.parametrize("operation", ["finish", "update"])
def test_revision_fields_are_strict_integers_without_coercion(value, operation):
    flat = (
        {"tool": "finish", "arguments": _actions()["finish"]}
        if operation == "finish"
        else _update("text")
    )
    field = "objective_revision" if operation == "finish" else "expected_revision"
    wire = encode_action(flat)
    wire["action"]["arguments"][field] = value
    with pytest.raises(TypedWireError):
        decode_action(wire, _ALL_TOOLS)
    flat["arguments"][field] = value
    with pytest.raises(TypedWireError):
        encode_action(flat)


@pytest.mark.parametrize("value", [True, "1.25", None, float("nan"), float("inf")])
@pytest.mark.parametrize("kind", ["number", "chart"])
def test_numeric_data_never_coerces_boolean_string_null_or_nonfinite_values(value, kind):
    wire = encode_action(_create(kind))
    data = wire["action"]["arguments"]["components"][0]["data"]
    if kind == "number":
        data["value"] = value
    else:
        data["values"][0] = value
    with pytest.raises(TypedWireError):
        decode_action(wire, _ALL_TOOLS)


@pytest.mark.parametrize("value", [0, 1, "false", [], {}])
def test_table_selectable_requires_an_actual_boolean(value):
    wire = encode_action(_create("table"))
    wire["action"]["arguments"]["components"][0]["data"]["selectable"] = value
    with pytest.raises(TypedWireError):
        decode_action(wire, _ALL_TOOLS)


@pytest.mark.parametrize("value", [None, [], {"nested": "text"}, 0, True])
def test_required_nonnullable_ui_values_cannot_disappear(value):
    wire = encode_action(_create("text"))
    wire["action"]["arguments"]["components"][0]["data"]["text"] = value
    with pytest.raises(TypedWireError):
        decode_action(wire, _ALL_TOOLS)


@pytest.mark.parametrize("tool", tuple(name for name in _ALL_TOOLS if name not in _BASE_TOOLS))
def test_known_unavailable_action_keeps_only_native_name_without_decoding_arguments(tool):
    envelope = {
        "action": {
            "tool": tool,
            "arguments": {"malformed": [_MARKER], "objective_revision": True, "ui_spec_json": 0},
        }
    }
    saved = copy.deepcopy(envelope)
    assert decode_action(envelope, _BASE_TOOLS) == {"tool": tool, "arguments": {}}
    assert envelope == saved


@pytest.mark.parametrize(
    "envelope",
    [
        None,
        [],
        {},
        {"tool": "finish", "arguments": {}},
        {"action": None},
        {"action": []},
        {"action": {"tool": "unknown", "arguments": {}}},
        {"action": {"tool": True, "arguments": {}}},
        {"action": {"tool": "publish_ui"}},
        {"action": {"tool": "publish_ui", "arguments": None}},
        {"action": {"tool": "publish_ui", "arguments": []}},
        {"action": {"tool": "publish_ui", "arguments": "{}"}},
        {"action": {"tool": "publish_ui", "arguments": {}, "extra": _MARKER}},
        {"action": {"tool": "publish_ui", "arguments": {}}, "extra": _MARKER},
    ],
)
def test_closed_root_and_action_are_required_even_for_unavailable_operations(envelope):
    with pytest.raises(TypedWireError):
        decode_action(envelope, _BASE_TOOLS)


@pytest.mark.parametrize(
    "history",
    [
        None,
        [],
        {},
        {"action": {"tool": "finish", "arguments": {}}},
        {"tool": "unknown", "arguments": {}},
        {"tool": "finish"},
        {"tool": "finish", "arguments": None},
        {"tool": "finish", "arguments": {}},
        {"tool": "finish", "arguments": {"summary": "Synthetic"}},
        {"tool": "finish", "arguments": {"objective_revision": 1}},
        {"tool": "read_file", "arguments": {}},
        {"tool": "publish_ui", "arguments": {"ui_spec_json": "{}"}},
        {"tool": "publish_ui", "arguments": {"title": "Synthetic", "components": []}},
        {
            "tool": "finish",
            "arguments": {"summary": "Synthetic", "objective_revision": 1},
            "success": True,
        },
    ],
)
def test_malformed_history_is_never_filled_into_a_successful_action(history):
    with pytest.raises(TypedWireError):
        encode_action(history)


@pytest.mark.parametrize(
    "tools",
    [
        None,
        [],
        list(_BASE_TOOLS),
        (),
        ("finish",),
        tuple(reversed(_BASE_TOOLS)),
        (*_BASE_TOOLS, "finish"),
        (*_BASE_TOOLS, _MARKER),
        tuple(name for name in _ALL_TOOLS if name != "mcp_call"),
        tuple(name for name in _ALL_TOOLS if name != "plugin_call"),
        tuple(name for name in _ALL_TOOLS if name != "skill_load"),
    ],
)
def test_malformed_native_tool_selections_are_never_coerced_or_partially_advertised(tools):
    with pytest.raises(ValueError):
        action_schema(tools)
    valid = encode_action({"tool": "finish", "arguments": _actions()["finish"]})
    with pytest.raises(ValueError):
        decode_action(valid, tools)


def test_typed_validation_errors_have_one_constant_payload_free_message():
    invalid = [
        {"action": {"tool": _MARKER, "arguments": {}}},
        {"action": {"tool": "finish", "arguments": {"summary": _MARKER}}},
        {"action": {"tool": "publish_ui", "arguments": {"title": _MARKER}}},
        {"action": {"tool": "read_file", "arguments": {"path": _MARKER, "max_bytes": True}}},
    ]
    messages = []
    for envelope in invalid:
        with pytest.raises(TypedWireError) as caught:
            decode_action(envelope, _ALL_TOOLS)
        error = caught.value
        assert isinstance(error, ValueError)
        assert _MARKER not in str(error) and _MARKER not in repr(error)
        assert _MARKER not in json.dumps({"error": str(error)})
        messages.append(str(error))
    assert len(set(messages)) == 1 and messages[0]


@pytest.fixture
def native_store(tmp_path):
    store = ArtifactStore(tmp_path / "synthetic-typed-wire.sqlite3")
    try:
        yield store
    finally:
        store.close()


def _task():
    return SimpleNamespace(
        session_id="synthetic-session",
        thread_id="synthetic-thread",
        epoch=1,
        task_id="synthetic-task",
        objective_revision=1,
        content_scope="public_synthetic",
        objective="Synthetic codec test",
    )


def test_all_ui_types_create_and_update_at_the_actual_native_store_boundary(native_store):
    task = _task()
    decoded = decode_action(encode_action(_create()), _ALL_TOOLS)
    created = native_store.publish(task, decoded["arguments"])
    assert created["revision"] == 1 and created["layout"] == "stack"
    assert created["task_id"] == task.task_id
    assert {component["type"] for component in created["components"]} == set(_UI_TYPES)
    update = {
        "tool": "publish_ui",
        "arguments": {
            "artifact_id": created["artifact_id"],
            "expected_revision": created["revision"],
            "updates": [
                {"id": component["id"], "data": component["data"]}
                for component in decoded["arguments"]["components"]
            ],
        },
    }
    updated = native_store.publish(
        task, decode_action(encode_action(update), _ALL_TOOLS)["arguments"]
    )
    assert updated["revision"] == 2 and updated["artifact_id"] == created["artifact_id"]
    table = next(component for component in updated["components"] if component["type"] == "table")
    assert table["data"]["selectable"] is False
    assert table["data"]["rows"][0] == [None, False, 0, 0.0, ""]
    with pytest.raises(ArtifactConflict):
        native_store.publish(task, decode_action(encode_action(update), _ALL_TOOLS)["arguments"])
    other_task = _task()
    other_task.task_id = "different-synthetic-task"
    update["arguments"]["expected_revision"] = 2
    with pytest.raises(ArtifactConflict):
        native_store.publish(
            other_task, decode_action(encode_action(update), _ALL_TOOLS)["arguments"]
        )


@pytest.mark.parametrize(
    ("kind", "replacement"),
    [
        ("source", {"url": "http://example.invalid/data"}),
        ("source", {"url": "https://synthetic:fake@example.invalid/data"}),
        ("diagram", {"edges": [{"from": "customer", "to": "missing-node"}]}),
        ("table", {"rows": [["wrong width"]]}),
        ("chart", {"values": [1]}),
        ("number", {"value": 9}),
        ("select", {"value": "Not listed"}),
    ],
)
def test_native_relationship_and_url_semantics_remain_authoritative(
    native_store, kind, replacement
):
    flat = _create(kind)
    flat["arguments"]["components"][0]["data"].update(replacement)
    decoded = decode_action(encode_action(flat), _ALL_TOOLS)
    with pytest.raises(ArtifactInvalid):
        native_store.publish(_task(), decoded["arguments"])
    assert (
        native_store.page(session_id="synthetic-session", thread_id="synthetic-thread", epoch=1)[
            "items"
        ]
        == []
    )
