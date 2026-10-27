"""Bounded JSON Schema profile and JSON value bounds for compute plugins.

Mirrors the existing MCP host's safe schema profile: no refs, no regex,
no external schemas, depth<=12, nodes<=512, lengths<=16KiB. Unsupported
schema keywords are an explicit refusal, never silently ignored. Input and
output JSON values must be finite, <=64KiB, and bounded in depth/nodes.
Schema failures are native failures, never evidence of success.
"""

from __future__ import annotations

import json
import math
from typing import Any

MAX_SCHEMA_CHARS = 16 * 1024
SCHEMA_MAX_DEPTH = 12
SCHEMA_MAX_NODES = 512

VALUE_MAX_BYTES = 64 * 1024
VALUE_MAX_DEPTH = 32
VALUE_MAX_NODES = 4096

_BLOCKED_KEYS = frozenset(
    {
        "$ref",
        "$dynamicRef",
        "$recursiveRef",
        "$id",
        "$schema",
        "$anchor",
        "$dynamicAnchor",
        "$recursiveAnchor",
        "$vocabulary",
        "pattern",
        "patternProperties",
        "contentMediaType",
        "contentEncoding",
        "contentSchema",
    }
)

# Keywords the host implements. Any other schema-object key is rejected
# rather than silently ignored by the validator.
_ALLOWED_KEYS = frozenset(
    {
        "type",
        "const",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "prefixItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "dependentSchemas",
        "dependentRequired",
        "contains",
        "minContains",
        "maxContains",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
        "title",
        "description",
        "default",
        "examples",
        "format",
        "readOnly",
        "writeOnly",
        "deprecated",
        "$defs",
        "definitions",
        "$comment",
    }
)

_SCHEMA_MAP_KEYS = frozenset({"properties", "$defs", "definitions", "dependentSchemas"})
_SCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_VALUE_KEYS = frozenset(
    {
        "items",
        "additionalProperties",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)


def check_schema_profile(schema: Any) -> None:
    """Validate a manifest schema dict against the bounded safe profile."""
    from core.plugins.errors import PluginRegistrationError

    if not isinstance(schema, dict):
        raise PluginRegistrationError("schema must be an object")
    try:
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PluginRegistrationError(f"schema is not JSON-serializable: {exc}") from exc
    if len(encoded) > MAX_SCHEMA_CHARS:
        raise PluginRegistrationError(f"schema exceeds {MAX_SCHEMA_CHARS} bytes ({len(encoded)})")
    budget = SCHEMA_MAX_NODES

    def visit(node: Any, depth: int = 0, is_schema: bool = True) -> None:
        nonlocal budget
        budget -= 1
        if budget < 0:
            raise PluginRegistrationError(f"schema exceeds node bound ({SCHEMA_MAX_NODES})")
        if depth > SCHEMA_MAX_DEPTH:
            raise PluginRegistrationError(f"schema exceeds depth bound ({SCHEMA_MAX_DEPTH})")
        if isinstance(node, str) and len(node) > MAX_SCHEMA_CHARS:
            raise PluginRegistrationError("schema string exceeds length bound")
        if isinstance(node, dict):
            if is_schema:
                hit = _BLOCKED_KEYS.intersection(node)
                if hit:
                    raise PluginRegistrationError(
                        f"unsupported schema keywords (refs/regex/external): {sorted(hit)}"
                    )
                for key in node:
                    if key not in _ALLOWED_KEYS:
                        raise PluginRegistrationError(f"unsupported schema keyword: {key!r}")
            for key, value in node.items():
                if is_schema and key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
                    for child in value.values():
                        visit(child, depth + 1, True)
                elif is_schema and key in _SCHEMA_LIST_KEYS and isinstance(value, list):
                    for child in value:
                        visit(child, depth + 1, True)
                else:
                    visit(value, depth + 1, bool(is_schema and key in _SCHEMA_VALUE_KEYS))
        elif isinstance(node, list):
            for value in node:
                visit(value, depth + 1, is_schema)

    visit(schema)


def check_json_value(value: Any, *, what: str = "value") -> bytes:
    """Bound a JSON value: finite, <=64KiB, bounded depth/nodes.

    Returns the canonical encoded bytes. Raises PluginCandidateFailure.
    """
    from core.plugins.errors import PluginCandidateFailure

    nodes = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > VALUE_MAX_NODES:
            raise PluginCandidateFailure(f"{what} exceeds node bound ({VALUE_MAX_NODES})")
        if depth > VALUE_MAX_DEPTH:
            raise PluginCandidateFailure(f"{what} exceeds depth bound ({VALUE_MAX_DEPTH})")
        if isinstance(node, float) and not math.isfinite(node):
            raise PluginCandidateFailure(f"{what} must be finite JSON (no NaN/Infinity)")
        if isinstance(node, str) and len(node.encode("utf-8")) > VALUE_MAX_BYTES:
            raise PluginCandidateFailure(f"{what} string exceeds size bound")
        if isinstance(node, dict):
            for k, v in node.items():
                if not isinstance(k, str):
                    raise PluginCandidateFailure(f"{what} object keys must be strings")
                walk(k, depth + 1)
                walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(value, 0)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginCandidateFailure(f"{what} is not JSON-serializable: {exc}") from exc
    if len(encoded) > VALUE_MAX_BYTES:
        raise PluginCandidateFailure(f"{what} exceeds {VALUE_MAX_BYTES} bytes ({len(encoded)})")
    return encoded


def validate_against(schema: dict, value: Any, *, what: str) -> None:
    """Check value bounds then JSON Schema conformance.

    Schema-conformance failures raise PluginCandidateFailure (native
    failure, never success evidence).
    """
    from core.plugins.errors import PluginCandidateFailure, PluginIntegrityError

    check_json_value(value, what=what)
    try:
        import jsonschema
    except ImportError as exc:
        raise PluginIntegrityError(f"jsonschema unavailable: {exc}") from exc
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        raise PluginCandidateFailure(
            f"{what} failed schema ({exc.validator!r} at {path}): {exc.message[:300]}"
        ) from exc
    except jsonschema.SchemaError as exc:
        raise PluginIntegrityError(f"stored schema invalid: {exc.message[:300]}") from exc
