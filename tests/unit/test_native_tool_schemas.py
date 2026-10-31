"""Canonical inspection schemas stay bounded and defer native authority checks."""

import ast
import copy
import inspect
import json
import re
from hashlib import sha256

import pytest
from jsonschema import Draft202012Validator

from packages.contracts import native_tool_schemas as registry
from packages.contracts.agent_tools import NATIVE_TOOL_ORDER
from packages.contracts.native_tool_schemas import (
    NativeToolSchemaError,
    native_tool_definition,
    native_tool_schema,
)
from services.agent.artifacts import ArtifactInvalid, validate_components
from services.agent.tools import ToolFailure, validate_write_arguments

_NAMES = tuple(dict.fromkeys((*NATIVE_TOOL_ORDER, "tool_catalog")))
_UI_TYPES = ("text", "status", "table", "chart", "diagram", "source", "number", "select", "ask")


def _component(kind):
    data = {
        "text": {"text": "Fictional example\nwith a tab\t"},
        "status": {"text": "Simulated"},
        "table": {
            "columns": ["Null", "Bool", "Integer", "Float", "Text"],
            "rows": [[None, False, 0, 0.0, ""], [None, True, 3, 1.25, "Fictional"]],
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
            "observed_at": 0,
        },
        "number": {"label": "Amount", "value": 0, "min": 0, "max": 3.5},
        "select": {"label": "Pick", "options": ["First", "Second"], "value": "First"},
        "ask": {"label": "Ask", "message": "Explain this fictional example"},
    }
    return {"id": f"demo_{kind}", "type": kind, "data": copy.deepcopy(data[kind])}


def _create(*kinds):
    return {
        "title": "Synthetic UI",
        "summary": "Fictional schema test data",
        "components": [_component(kind) for kind in kinds or _UI_TYPES],
    }


def _update(kind):
    component = _component(kind)
    return {
        "artifact_id": "synthetic-artifact",
        "expected_revision": 1,
        "updates": [{"id": component["id"], "data": component["data"]}],
    }


def _arguments(name):
    return {
        "read_file": {"path": "fictional.txt"},
        "write_file": {"path": "fictional.txt", "content": ""},
        "search": {"pattern": ""},
        "shell": {"command": "true", "timeout_s": 1.25},
        "read_transcript": {},
        "select_context": {},
        "mcp_list": {},
        "mcp_call": {
            "server_id": "synthetic-server",
            "tool": "count",
            "arguments": {"count": 0, "nullable": None, "enabled": False},
        },
        "skills_list": {},
        "skill_load": {"skill_id": "local/synthetic"},
        "plugins_list": {},
        "plugin_call": {
            "plugin_id": "synthetic-plugin",
            "arguments": {"count": 0, "nullable": None, "enabled": False},
        },
        "publish_ui": _create(),
        "consult_model": {"purpose": "analysis", "prompt": "Synthetic task text"},
        "finish": {"summary": "", "objective_revision": 1},
        "tool_catalog": {},
    }[name]


def _validator(name):
    return Draft202012Validator(native_tool_schema(name))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _walk(value, depth=0):
    yield value, depth
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, depth + 1
            yield from _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, depth + 1)


@pytest.mark.parametrize("name", _NAMES)
def test_every_advertised_or_catalog_operation_has_a_complete_valid_schema(name):
    schema = native_tool_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    _validator(name).validate(_arguments(name))
    for node, _ in _walk(schema):
        if isinstance(node, dict) and "$ref" in node:
            assert node["$ref"].startswith("#/$defs/")
            assert node["$ref"][len("#/$defs/") :] in schema["$defs"]


@pytest.mark.parametrize("name", _NAMES)
def test_definitions_and_nested_schemas_are_fresh_with_reproducible_digests(name):
    expected = native_tool_definition(name)
    schema = native_tool_schema(name)
    assert schema == expected["input_schema"]
    assert expected["schema_sha256"] == sha256(_canonical(schema)).hexdigest()
    assert re.fullmatch("[a-f0-9]{64}", expected["schema_sha256"])

    definition = native_tool_definition(name)
    nested = next(
        node for node, _ in _walk(schema) if isinstance(node, dict) and node is not schema
    )
    nested["untrusted-mutation"] = True
    definition["source"].append("untrusted-mutation")
    definition["requirements"].clear()
    definition["constraints"].append("untrusted-mutation")
    definition["legacy_behavior"].append("untrusted-mutation")
    definition["input_schema"].clear()
    assert native_tool_definition(name) == expected
    assert native_tool_schema(name) == expected["input_schema"]


@pytest.mark.parametrize("name", _NAMES)
def test_complete_definition_has_bounded_json_and_room_for_contextual_projection(name):
    definition = native_tool_definition(name)
    assert definition["description"]
    assert definition["source"] and all(
        type(reference) is str and not reference.startswith("/") and ":" in reference
        for reference in definition["source"]
    )
    requirements = definition["requirements"]
    assert len(requirements) == len(set(requirements))
    assert all(re.fullmatch("[a-z][a-z_]+", code) for code in requirements)
    assert "native_task_authority" in requirements
    assert "provider_disclosure" in requirements
    assert len(_canonical(definition)) + 2048 < 32768
    assert max(depth for _, depth in _walk(definition)) <= 24
    assert sum(1 for _ in _walk(definition)) <= 16384


class _Hostile:
    def __str__(self):
        raise AssertionError("Untrusted string conversion ran")

    def __repr__(self):
        raise AssertionError("Untrusted repr ran")

    def __hash__(self):
        raise AssertionError("Untrusted hash ran")

    def __eq__(self, other):
        raise AssertionError("Untrusted equality ran")


class _HostileString(str):
    def __hash__(self):
        raise AssertionError("Untrusted string-subclass hash ran")


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(None, id="none"),
        pytest.param(False, id="bool"),
        pytest.param(1, id="integer"),
        pytest.param([], id="list"),
        pytest.param({}, id="dict"),
        pytest.param("", id="empty"),
        pytest.param("untrusted-secret-marker", id="unknown"),
        pytest.param("publish_ui\x00", id="nul"),
        pytest.param("\ud800", id="surrogate"),
        pytest.param(_Hostile(), id="hostile"),
        pytest.param(_HostileString("read_file"), id="string-subclass"),
    ],
)
@pytest.mark.parametrize("function", [native_tool_schema, native_tool_definition])
def test_unknown_names_have_a_constant_payload_free_error_without_user_methods(function, name):
    with pytest.raises(NativeToolSchemaError) as caught:
        function(name)
    assert caught.value.args == ("Unknown native tool schema",)
    assert str(caught.value) == "Unknown native tool schema"


@pytest.mark.parametrize("name", _NAMES)
def test_canonical_outer_input_objects_reject_unknown_authority_fields(name):
    args = _arguments(name)
    args["owner_approval"] = True
    assert not _validator(name).is_valid(args)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("read_file", {"path": False}),
        ("read_file", {"path": "a", "max_bytes": "1"}),
        ("read_file", {"path": "a", "max_bytes": False}),
        ("read_file", {"path": "a", "max_bytes": 0}),
        ("read_file", {"path": "a", "max_bytes": 262145}),
        ("write_file", {"path": "a", "content": None}),
        ("write_file", {"path": "a", "content": "", "objective_revision": True}),
        ("write_file", {"path": "a\x00", "content": ""}),
        ("search", {"pattern": 12}),
        ("shell", {"command": " \t\n"}),
        ("shell", {"command": "true", "timeout_s": "1"}),
        ("shell", {"command": "true", "timeout_s": 0}),
        ("shell", {"command": "true", "timeout_s": 121}),
        ("read_transcript", {"event_ids": [1]}),
        ("read_transcript", {"event_ids": ["id"] * 21}),
        ("read_transcript", {"after": -1}),
        ("read_transcript", {"limit": True}),
        ("read_transcript", {"limit": 21}),
        ("select_context", {"max_chars": 0}),
        ("select_context", {"max_chars": 8001}),
        ("select_context", {"max_chars": False}),
        ("mcp_list", {"after": 129}),
        ("mcp_list", {"after": True}),
        ("mcp_call", {"server_id": "s", "tool": "t", "arguments": "{}"}),
        ("mcp_call", {"server_id": "s", "mcp_tool": "t", "mcp_arguments_json": "{}"}),
        ("skills_list", {"after": 1000001}),
        ("skills_list", {"after": "1"}),
        ("skill_load", {"skill_id": "local/synthetic", "resource": ""}),
        ("skill_load", {"skill_id": "local/synthetic", "invocation": "user"}),
        ("plugins_list", {"after": 129}),
        ("plugin_call", {"plugin_id": "synthetic", "arguments": "{}"}),
        ("plugin_call", {"plugin_id": "synthetic", "plugin_arguments_json": "{}"}),
        ("finish", {"summary": "", "objective_revision": True}),
        ("finish", {"summary": 1, "objective_revision": 1}),
        ("tool_catalog", {"kind": "Native"}),
        ("tool_catalog", {"name": ""}),
        ("tool_catalog", {"name": "x" * 513}),
        ("tool_catalog", {"server_id": "x" * 129}),
        ("tool_catalog", {"query": "x" * 81}),
        ("tool_catalog", {"after": False}),
        ("tool_catalog", {"after": "1"}),
        ("tool_catalog", {"after": -1}),
        ("tool_catalog", {"after": 1000001}),
    ],
)
def test_canonical_shapes_reject_wrong_types_and_enforced_field_bounds(name, args):
    assert not _validator(name).is_valid(args)


@pytest.mark.parametrize("kind", _UI_TYPES)
def test_each_ui_create_and_complete_update_data_shape_is_available(kind):
    validator = _validator("publish_ui")
    validator.validate(_create(kind))
    validator.validate(_update(kind))
    validate_components(_create(kind)["components"])


def test_combined_ui_preserves_null_bool_zero_empty_strings_and_optional_omission():
    args = _create()
    before = copy.deepcopy(args)
    _validator("publish_ui").validate(args)
    normalized = validate_components(args["components"])
    assert args == before
    cells = next(c for c in normalized if c["type"] == "table")["data"]["rows"][0]
    assert cells == [None, False, 0, 0.0, ""]
    assert [type(cell) for cell in cells] == [type(None), bool, int, float, str]
    assert "tone" not in next(c for c in normalized if c["type"] == "status")["data"]
    assert next(c for c in normalized if c["type"] == "chart")["data"]["style"] == "bar"


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("text", "text", ""),
        ("text", "text", None),
        ("text", "text", "x" * 8001),
        ("text", "text", "bad\rcontrol"),
        ("text", "text", "bad\ud800unicode"),
        ("status", "text", "x" * 1001),
        ("status", "tone", "unrecognized"),
        ("status", "tone", None),
        ("table", "columns", []),
        ("table", "columns", ["C"] * 13),
        ("table", "rows", [["C"]] * 101),
        ("table", "rows", [["x" * 1001]]),
        ("table", "rows", [[1e12 + 1]]),
        ("table", "selectable", 0),
        ("chart", "labels", []),
        ("chart", "labels", ["C"] * 101),
        ("chart", "values", [True]),
        ("chart", "values", []),
        ("chart", "unit", "x" * 41),
        ("chart", "style", "pie"),
        ("diagram", "nodes", []),
        ("diagram", "nodes", [{"id": "x", "label": ""}] * 31),
        ("diagram", "edges", [{"from": "customer", "to": "order"}] * 61),
        ("source", "freshness", "live"),
        ("source", "observed_at", -1),
        ("source", "observed_at", True),
        ("source", "observed_at", 9007199254740992),
        ("number", "value", True),
        ("number", "step", 0),
        ("number", "step", 1e12 + 1),
        ("select", "options", []),
        ("select", "options", ["Same", "Same"]),
        ("select", "options", [str(i) for i in range(31)]),
        ("ask", "message", ""),
        ("ask", "message", "x" * 2001),
    ],
)
def test_ui_data_bounds_and_optional_values_are_not_coerced(kind, field, value):
    args = _create(kind)
    args["components"][0]["data"][field] = value
    assert not _validator("publish_ui").is_valid(args)


@pytest.mark.parametrize(
    "mutation",
    [
        {"title": "x" * 161},
        {"summary": "x" * 2001},
        {"layout": "arbitrary-code"},
        {"components": []},
        {"components": [_component("text")] * 25},
        {"objective_revision": 1},
        {"artifact_id": "mixed-update"},
        {"ui_spec_json": "{}"},
    ],
)
def test_publication_envelopes_remain_closed_and_bounded(mutation):
    args = {**_create("text"), **mutation}
    assert not _validator("publish_ui").is_valid(args)


@pytest.mark.parametrize("invalid_id", ["x" * 65, "0invalid", "valid\n", "bad id"])
def test_component_ids_match_native_full_string_grammar(invalid_id):
    args = _create("text")
    args["components"][0]["id"] = invalid_id
    assert not _validator("publish_ui").is_valid(args)


@pytest.mark.parametrize(
    "mutation",
    [
        {"artifact_id": ""},
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"expected_revision": 0},
        {"expected_revision": 9007199254740992},
        {"updates": []},
        {"updates": [{"id": "demo_text", "type": "text", "data": {"text": "x"}}]},
        {"title": "cannot-change-title"},
    ],
)
def test_update_shape_excludes_create_metadata_and_component_type_mutation(mutation):
    assert not _validator("publish_ui").is_valid({**_update("text"), **mutation})


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        ("table", {"rows": [["one"]]}),
        ("chart", {"values": [1]}),
        ("diagram", {"nodes": [{"id": "same", "label": "A"}, {"id": "same", "label": "B"}]}),
        ("diagram", {"edges": [{"from": "missing", "to": "order"}]}),
        ("number", {"value": 5}),
        ("select", {"value": "Absent"}),
        ("source", {"url": "https://user:password@example.invalid/"}),
        ("source", {"url": "https://example.invalid/?access_token=synthetic"}),
        ("source", {"url": "http://example.invalid/"}),
    ],
)
def test_relational_and_uri_rules_are_explicit_native_constraints(kind, changes):
    args = _create(kind)
    args["components"][0]["data"].update(changes)
    _validator("publish_ui").validate(args)
    with pytest.raises(ArtifactInvalid):
        validate_components(args["components"])
    constraints = " ".join(native_tool_definition("publish_ui")["constraints"])
    assert "Table row widths" in constraints
    assert "Native source URL validation" in constraints


def test_byte_bounds_and_native_integer_kind_are_not_misrepresented_as_schema_guarantees():
    validate_write_arguments({"path": "fictional.txt", "content": "x" * 262144}, 1)
    args = {"path": "fictional.txt", "content": "\U0001f642" * 65537}
    _validator("write_file").validate(args)
    with pytest.raises(ToolFailure, match="256 KiB"):
        validate_write_arguments(args, 1)
    integral_float = {"path": "fictional.txt", "content": "", "objective_revision": 1.0}
    _validator("write_file").validate(integral_float)
    with pytest.raises(ToolFailure, match="must be an int"):
        validate_write_arguments(integral_float, 1)
    assert "maxLength cannot express this byte limit" in " ".join(
        native_tool_definition("write_file")["constraints"]
    )
    assert "integral floats" in " ".join(native_tool_definition("tool_catalog")["constraints"])


@pytest.mark.parametrize("kind", _UI_TYPES)
def test_ui_component_and_data_objects_reject_undeclared_fields(kind):
    args = _create(kind)
    args["components"][0]["owner_approval"] = True
    assert not _validator("publish_ui").is_valid(args)
    args = _create(kind)
    args["components"][0]["data"]["executable_code"] = "untrusted-marker"
    assert not _validator("publish_ui").is_valid(args)
    update = _update(kind)
    update["updates"][0]["data"]["executable_code"] = "untrusted-marker"
    assert not _validator("publish_ui").is_valid(update)


@pytest.mark.parametrize("namespace", ["github", "clawhub"])
def test_existing_long_skill_identities_remain_canonically_selectable(namespace):
    identity = f"{namespace}:{'a' * 128}/{'b' * 128}"
    assert len(identity) == (264 if namespace == "github" else 265)
    _validator("skill_load").validate({"skill_id": identity})
    _validator("tool_catalog").validate({"kind": "skill", "name": identity})
    assert not _validator("skill_load").is_valid({"skill_id": identity + "x"})


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("skill_load", {"skill_id": "local/synthetic\n"}),
        ("plugin_call", {"plugin_id": "synthetic\n", "arguments": {}}),
        ("mcp_call", {"server_id": "s\n", "tool": "t", "arguments": {}}),
        ("mcp_call", {"server_id": "s", "tool": "t\n", "arguments": {}}),
    ],
)
def test_identity_patterns_do_not_accept_a_final_newline(name, args):
    assert not _validator(name).is_valid(args)


def test_catalog_has_exact_field_shapes_and_leaves_combination_authority_in_native_parser():
    validator = _validator("tool_catalog")
    validator.validate({"kind": "skill", "name": "\U0001f642" * 512})
    validator.validate({"kind": "mcp", "server_id": "s" * 128, "query": "x" * 80, "after": 1000000})
    # Field schemas alone do not establish selected-query relationships.
    validator.validate({"name": "read_file"})
    validator.validate({"kind": "native", "name": "read_file", "after": 1})
    constraints = " ".join(native_tool_definition("tool_catalog")["constraints"])
    assert "name requires kind" in constraints
    assert "Selection cannot also search or paginate" in constraints
    for args in ({"name": "bad\x00name"}, {"server_id": "\ud800"}, {"query": "\ud800"}):
        assert not validator.is_valid(args)


def test_permission_requirements_separate_grants_targets_and_completion_evidence():
    requirements = {name: set(native_tool_definition(name)["requirements"]) for name in _NAMES}
    assert "write_grant_or_action_approval" in requirements["write_file"]
    assert "shell_grant_or_action_approval" in requirements["shell"]
    assert "mcp_action_approval_unless_yolo" in requirements["mcp_call"]
    assert "transport_grants" in requirements["mcp_call"]
    assert "transport_grants_if_discovering" in requirements["mcp_list"]
    assert "task_skill_version_pin" in requirements["skill_load"]
    assert "task_plugin_version_pin" in requirements["plugin_call"]
    assert "active_originating_task_revision" in requirements["publish_ui"]
    assert "write_grant_or_action_approval" not in requirements["publish_ui"]
    assert "durable_outcome_confirmation" in requirements["finish"]
    assert "metadata_only_no_execution_or_pinning" in requirements["tool_catalog"]
    for name in ("write_file", "shell", "mcp_call", "publish_ui"):
        assert "uncommitted_speech_action_approval" in requirements[name]


def test_registry_has_no_runtime_broker_provider_or_io_imports():
    imports = set()
    for node in ast.walk(ast.parse(inspect.getsource(registry))):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module)
    assert imports <= {"copy", "hashlib", "json"}
