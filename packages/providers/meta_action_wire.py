"""Strict provider-wire grammar; native tools remain the execution authority."""

from functools import lru_cache, reduce
from operator import or_
from types import UnionType
from typing import Annotated, Literal, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    create_model,
)

from packages.contracts.agent_tools import NATIVE_TOOL_ORDER, NativeToolSnapshot


class TypedWireError(ValueError):
    def __init__(self):
        super().__init__("Invalid typed action wire")


_CONFIG = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _object(model_name, **fields):
    # No defaults: even nullable optional fields are required in the wire.
    return create_model(model_name, __config__=_CONFIG, **{k: (v, ...) for k, v in fields.items()})


def _string(minimum=0, maximum=None, pattern=None):
    return Annotated[StrictStr, Field(min_length=minimum, max_length=maximum, pattern=pattern)]


def _array(item, minimum=0, maximum=None):
    return Annotated[list[item], Field(min_length=minimum, max_length=maximum)]


def _number(minimum=-1e12, maximum=1e12):
    # StrictFloat alone converts an integer to float. The exact integer branch
    # preserves the JSON scalar kind, including inside table cells.
    return (
        Annotated[StrictInt, Field(ge=minimum, le=maximum)]
        | Annotated[StrictFloat, Field(ge=minimum, le=maximum, allow_inf_nan=False)]
    )


_NUMBER = _number()
_COMPONENT_ID = _string(1, 64, r"^[A-Za-z][A-Za-z0-9_-]*$")
_CELL = StrictBool | _NUMBER | _string(0, 1000) | None
_NODE = _object("UiNode", id=_string(1, 128), label=_string(0, 160), detail=_string(0, 1000) | None)
_EDGE = _object(
    "UiEdge", **{"from": _string(1, 128), "to": _string(1, 128), "label": _string(0, 80) | None}
)
_DATA = {
    "text": _object("UiText", text=_string(1, 8000)),
    "status": _object(
        "UiStatus",
        text=_string(1, 1000),
        tone=Literal["neutral", "success", "warning", "error"] | None,
    ),
    "table": _object(
        "UiTable",
        columns=_array(_string(1, 80), 1, 12),
        rows=_array(_array(_CELL), 0, 100),
        selectable=StrictBool | None,
    ),
    "chart": _object(
        "UiChart",
        labels=_array(_string(0, 80), 1, 100),
        values=_array(_NUMBER, 1, 100),
        unit=_string(0, 40) | None,
        style=Literal["bar", "line"] | None,
    ),
    "diagram": _object("UiDiagram", nodes=_array(_NODE, 1, 30), edges=_array(_EDGE, 0, 60)),
    "source": _object(
        "UiSource",
        label=_string(1, 160),
        url=_string(1, 2000),
        freshness=Literal["static", "simulated", "delayed", "stale"],
        observed_at=_number(0, 9_007_199_254_740_991) | None,
    ),
    "number": _object(
        "UiNumber",
        label=_string(1, 160),
        value=_NUMBER,
        min=_NUMBER,
        max=_NUMBER,
        step=(
            Annotated[StrictInt, Field(gt=0, le=1e12)]
            | Annotated[StrictFloat, Field(gt=0, le=1e12, allow_inf_nan=False)]
            | None
        ),
    ),
    "select": _object(
        "UiSelect",
        label=_string(1, 160),
        options=_array(_string(1, 160), 1, 30),
        value=_string(1, 160),
    ),
    "ask": _object("UiAsk", label=_string(1, 160), message=_string(1, 2000)),
}
_COMPONENTS = tuple(
    _object(
        "Component" + kind.title(),
        id=_COMPONENT_ID,
        type=Literal[kind],
        title=_string(0, 160) | None,
        data=model,
    )
    for kind, model in _DATA.items()
)
_CREATE = _object(
    "UiCreate",
    title=_string(1, 160),
    summary=_string(1, 2000),
    components=_array(reduce(or_, _COMPONENTS), 1, 24),
    layout=Literal["stack", "grid"] | None,
)
_UPDATE = _object(
    "UiUpdate",
    artifact_id=_string(1, 128),
    expected_revision=Annotated[StrictInt, Field(ge=1, le=9_007_199_254_740_991)],
    updates=_array(_object("UiPatch", id=_COMPONENT_ID, data=reduce(or_, _DATA.values())), 1, 24),
)
_ARGUMENTS = {
    "read_file": _object("ReadFile", path=StrictStr, max_bytes=StrictInt | None),
    "write_file": _object(
        "WriteFile", path=StrictStr, content=StrictStr, objective_revision=StrictInt | None
    ),
    "search": _object("Search", pattern=StrictStr, path=StrictStr | None),
    "shell": _object("Shell", command=StrictStr, timeout_s=StrictInt | StrictFloat | None),
    "read_transcript": _object(
        "ReadTranscript",
        event_ids=list[StrictStr] | None,
        after=StrictInt | None,
        limit=StrictInt | None,
    ),
    "select_context": _object("SelectContext", path=StrictStr | None, max_chars=StrictInt | None),
    "mcp_list": _object(
        "McpList", server_id=StrictStr | None, after=StrictInt | None, mcp_tool=StrictStr | None
    ),
    "mcp_call": _object(
        "McpCall", server_id=StrictStr, mcp_tool=StrictStr, mcp_arguments_json=_string(0, 65536)
    ),
    "skills_list": _object("SkillsList", after=StrictInt | None),
    "skill_load": _object("SkillLoad", skill_id=StrictStr, resource=StrictStr | None),
    "plugins_list": _object("PluginsList", plugin_id=StrictStr | None, after=StrictInt | None),
    "plugin_call": _object(
        "PluginCall", plugin_id=StrictStr, plugin_arguments_json=_string(0, 65536)
    ),
    "publish_ui": _CREATE | _UPDATE,
    "tool_catalog": _object(
        "ToolCatalog",
        kind=Literal["native", "mcp", "skill", "plugin"] | None,
        name=_string(1, 512) | None,
        server_id=_string(1, 128) | None,
        query=_string(0, 80) | None,
        after=Annotated[StrictInt, Field(ge=0, le=1_000_000)] | None,
    ),
    "finish": _object("Finish", summary=StrictStr, objective_revision=StrictInt),
}
_ACTIONS = {
    name: _object("Action" + name.title().replace("_", ""), tool=Literal[name], arguments=args)
    for name, args in _ARGUMENTS.items()
}


def _selection(tools):
    try:
        return NativeToolSnapshot(tools).available_tools
    except (ValueError, TypeError):
        raise TypedWireError() from None


@lru_cache(maxsize=32)
def _adapter(tools):
    return TypeAdapter(reduce(or_, (_ACTIONS[name] for name in tools)))


def action_schema(tools: tuple[str, ...]) -> dict:
    """A fresh closed schema; no mutable schema cache or foreign catalog input."""
    tools = _selection(tools)
    schema = _adapter(tools).json_schema()
    definitions = schema.pop("$defs", {})
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"action": schema},
        "required": ["action"],
        "$defs": definitions,
    }


def _base(annotation):
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _nullable(annotation):
    annotation = _base(annotation)
    return get_origin(annotation) in {Union, UnionType} and type(None) in get_args(annotation)


def _plain(value):
    if isinstance(value, BaseModel):
        result = {}
        for name, field in type(value).model_fields.items():
            item = getattr(value, name)
            if item is None and _nullable(field.annotation):
                continue
            result[name] = _plain(item)
        return result
    if isinstance(value, list):
        # None here is a data value, never an omitted object field.
        return [_plain(item) for item in value]
    return value


def decode_action(envelope: dict, tools: tuple[str, ...]) -> dict:
    tools = _selection(tools)
    if type(envelope) is not dict or set(envelope) != {"action"}:
        raise TypedWireError()
    action = envelope["action"]
    if (
        type(action) is not dict
        or set(action) != {"tool", "arguments"}
        or type(action["tool"]) is not str
        or action["tool"] not in NATIVE_TOOL_ORDER
        or type(action["arguments"]) is not dict
    ):
        raise TypedWireError()
    if action["tool"] not in tools:
        # Only the known name reaches the supervisor's membership refusal.
        # Do not traverse or validate an unavailable operation's arguments.
        return {"tool": action["tool"], "arguments": {}}
    try:
        return _plain(_adapter(tools).validate_python(action, strict=True))
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise TypedWireError() from None


def _fill_model(model, value):
    if type(value) is not dict:
        raise TypedWireError()
    result = dict(value)
    for name, field in model.model_fields.items():
        if name not in result:
            if _nullable(field.annotation):
                result[name] = None
        else:
            result[name] = _fill_value(field.annotation, result[name])
    # Unknown fields are deliberately retained for strict validation to reject.
    return result


def _fill_value(annotation, value):
    annotation = _base(annotation)
    if value is None:
        return value
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _fill_model(annotation, value)
    origin = get_origin(annotation)
    if origin is list and type(value) is list:
        return [_fill_value(get_args(annotation)[0], item) for item in value]
    if origin in {Union, UnionType}:
        models = [
            _base(option)
            for option in get_args(annotation)
            if isinstance(_base(option), type) and issubclass(_base(option), BaseModel)
        ]
        if models:
            for model in models:
                try:
                    filled = _fill_model(model, value)
                    model.model_validate(filled, strict=True)
                    return filled
                except (TypedWireError, ValidationError):
                    continue
            raise TypedWireError()
    return value


def encode_action(flat_wire_action: dict) -> dict:
    """Fill declared missing optional nulls, never missing required arguments."""
    if (
        type(flat_wire_action) is not dict
        or set(flat_wire_action) != {"tool", "arguments"}
        or type(flat_wire_action["tool"]) is not str
        or flat_wire_action["tool"] not in NATIVE_TOOL_ORDER
        or type(flat_wire_action["arguments"]) is not dict
    ):
        raise TypedWireError()
    try:
        model = _ACTIONS[flat_wire_action["tool"]]
        filled = _fill_model(model, flat_wire_action)
        return {"action": model.model_validate(filled, strict=True).model_dump(mode="python")}
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise TypedWireError() from None
