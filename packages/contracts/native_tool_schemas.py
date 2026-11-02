"""Trusted canonical logical inputs for native Agent operations.

These definitions are inspection metadata, not a replacement dispatch validator.
They neither authorize an action nor describe every legacy coerced input. Native
brokers still enforce task authority, disclosure, bounds and relational checks.
Provider wire aliases and encoded argument strings deliberately do not appear.
"""

import json
from copy import deepcopy
from hashlib import sha256

_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_SAFE_INTEGER = 9_007_199_254_740_991
_UI_TEXT_PATTERN = r"^[^\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]*$"
_UTF8_PATTERN = r"^[^\ud800-\udfff]*$"
_IDENTITY_PATTERN = r"^[^\x00\ud800-\udfff]+$"
_COMPONENT_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]*(?![\s\S])"
_SKILL_PATTERN = (
    r"^(?:local|github:[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"|clawhub:[A-Za-z0-9][A-Za-z0-9._-]{0,127})"
    r"/[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?![\s\S])"
)


class NativeToolSchemaError(ValueError):
    """A constant refusal for an unknown or malformed native operation name."""

    def __init__(self):
        super().__init__("Unknown native tool schema")


def _string(minimum=0, maximum=None, *, pattern=_UTF8_PATTERN, **annotations):
    result = {"type": "string", "minLength": minimum, "pattern": pattern}
    if maximum is not None:
        result["maxLength"] = maximum
    return {**result, **annotations}


def _ui_string(minimum, maximum):
    return _string(minimum, maximum, pattern=_UI_TEXT_PATTERN)


def _integer(minimum, maximum=None, **annotations):
    result = {"type": "integer", "minimum": minimum}
    if maximum is not None:
        result["maximum"] = maximum
    return {**result, **annotations}


def _number(minimum=-1e12, maximum=1e12, **annotations):
    return {"type": "number", "minimum": minimum, "maximum": maximum, **annotations}


def _array(item, minimum=0, maximum=None, **annotations):
    result = {"type": "array", "items": item, "minItems": minimum}
    if maximum is not None:
        result["maxItems"] = maximum
    return {**result, **annotations}


def _object(properties, required=(), **annotations):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
        **annotations,
    }


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _ref(name):
    return {"$ref": f"#/$defs/{name}"}


def _schema(body):
    return {"$schema": _DRAFT, **body}


def _publication_schema():
    # Local references keep selected inspection complete without repeating the
    # nine data definitions in every component and update alternative.
    data = {
        "text": _object({"text": _ui_string(1, 8000)}, ("text",)),
        "status": _object(
            {
                "text": _ui_string(1, 1000),
                "tone": {"enum": ["neutral", "success", "warning", "error"]},
            },
            ("text",),
        ),
        "table": _object(
            {
                "columns": _array(_ui_string(1, 80), 1, 12),
                "rows": _array(_array(_ref("cell"), 1, 12), 0, 100),
                "selectable": {"type": "boolean", "default": True},
            },
            ("columns", "rows"),
        ),
        "chart": _object(
            {
                "labels": _array(_ui_string(0, 80), 1, 100),
                "values": _array(_number(), 1, 100),
                "unit": _ui_string(0, 40),
                "style": {"enum": ["bar", "line"], "default": "bar"},
            },
            ("labels", "values"),
        ),
        "diagram": _object(
            {
                "nodes": _array(_ref("node"), 1, 30),
                "edges": _array(_ref("edge"), 0, 60),
            },
            ("nodes", "edges"),
        ),
        "source": _object(
            {
                "label": _ui_string(1, 160),
                "url": {
                    **_ui_string(1, 2000),
                    "format": "uri",
                    "description": "Native validation requires HTTPS and rejects credentials, whitespace and secret query keys.",
                },
                "freshness": {"enum": ["static", "simulated", "delayed", "stale"]},
                "observed_at": _number(0, _SAFE_INTEGER),
            },
            ("label", "url", "freshness"),
        ),
        "number": _object(
            {
                "label": _ui_string(1, 160),
                "value": _number(),
                "min": _number(),
                "max": _number(),
                "step": {"type": "number", "exclusiveMinimum": 0, "maximum": 1e12},
            },
            ("label", "value", "min", "max"),
        ),
        "select": _object(
            {
                "label": _ui_string(1, 160),
                "options": _array(_ui_string(1, 160), 1, 30, uniqueItems=True),
                "value": _ui_string(1, 160),
            },
            ("label", "options", "value"),
        ),
        "ask": _object(
            {"label": _ui_string(1, 160), "message": _ui_string(1, 2000)},
            ("label", "message"),
        ),
    }
    definitions = {
        "cell": {
            "anyOf": [
                {"type": "null"},
                {"type": "boolean"},
                _number(),
                _ui_string(0, 1000),
            ]
        },
        "node": _object(
            {
                "id": _ui_string(1, 128),
                "label": _ui_string(0, 160),
                "detail": _ui_string(0, 1000),
            },
            ("id", "label"),
        ),
        "edge": _object(
            {
                "from": _ui_string(1, 128),
                "to": _ui_string(1, 128),
                "label": _ui_string(0, 80),
            },
            ("from", "to"),
        ),
        **{f"{kind}_data": shape for kind, shape in data.items()},
    }
    for kind in data:
        definitions[f"{kind}_component"] = _object(
            {
                "id": _string(1, 64, pattern=_COMPONENT_PATTERN),
                "type": {"const": kind},
                "title": {**_ui_string(0, 160), "default": ""},
                "data": _ref(f"{kind}_data"),
            },
            ("id", "type", "data"),
        )
    definitions["create"] = _object(
        {
            "title": _ui_string(1, 160),
            "summary": _ui_string(1, 2000),
            "components": _array({"anyOf": [_ref(f"{kind}_component") for kind in data]}, 1, 24),
            "layout": {"enum": ["stack", "grid"], "default": "stack"},
        },
        ("title", "summary", "components"),
    )
    definitions["update"] = _object(
        {
            "artifact_id": _ui_string(1, 128),
            "expected_revision": _integer(1, _SAFE_INTEGER),
            "updates": _array(
                _object(
                    {
                        "id": _string(1, 64, pattern=_COMPONENT_PATTERN),
                        "data": {"anyOf": [_ref(f"{kind}_data") for kind in data]},
                    },
                    ("id", "data"),
                ),
                1,
                24,
            ),
        },
        ("artifact_id", "expected_revision", "updates"),
    )
    return _schema(
        {"type": "object", "anyOf": [_ref("create"), _ref("update")], "$defs": definitions}
    )


_SCHEMAS = {
    "consult_model": _schema(
        _object(
            {
                "purpose": {
                    "enum": [
                        "analysis",
                        "compression",
                        "title_generation",
                        "delegation",
                        "harness_design",
                    ]
                },
                "prompt": _string(1, 8000, description="UTF-8 text, at most 16000 encoded bytes."),
            },
            ("purpose", "prompt"),
        )
    ),
    "read_file": _schema(
        _object(
            {
                "path": _string(1, pattern=_IDENTITY_PATTERN),
                "max_bytes": _integer(
                    1,
                    262144,
                    default=65536,
                    description="Complete-file UTF-8 byte bound; oversized files are refused, never clipped.",
                ),
            },
            ("path",),
        )
    ),
    "write_file": _schema(
        _object(
            {
                "path": _string(1, pattern=_IDENTITY_PATTERN),
                "content": _string(description="Valid UTF-8 text, at most 262144 encoded bytes."),
                "objective_revision": _integer(1),
            },
            ("path", "content"),
        )
    ),
    "search": _schema(
        _object(
            {"pattern": _string(), "path": _nullable(_string(pattern=_IDENTITY_PATTERN))},
            ("pattern",),
        )
    ),
    "shell": _schema(
        _object(
            {
                "command": _string(1, allOf=[{"pattern": r"\S"}]),
                "timeout_s": _number(1, 120, default=60),
            },
            ("command",),
        )
    ),
    "read_transcript": _schema(
        _object(
            {
                "event_ids": _array(_string(1), 0, 20, default=[]),
                "after": _integer(0, default=0),
                "limit": _integer(1, 20, default=8),
                "paged": {"type": "boolean"},
                "entry_view": _nullable({"enum": ["resolved", "immutable"]}),
                "offset": _integer(0, _SAFE_INTEGER, default=0),
                "max_chars": _integer(1, 12000, default=2000),
                "expected_revision": _nullable(_integer(1, _SAFE_INTEGER)),
                "expected_entry_sha256": _nullable(_string(64, 64, pattern=r"^[0-9a-f]{64}$")),
                "scope_token": _nullable(_string(64, 64, pattern=r"^[0-9a-f]{64}$")),
                "through_sequence": _nullable(_integer(0, _SAFE_INTEGER)),
            }
        )
    ),
    "select_context": _schema(
        _object(
            {
                "path": _string(pattern=_IDENTITY_PATTERN, default="."),
                "max_chars": _integer(1, 8000, default=4000),
            }
        )
    ),
    "mcp_list": _schema(
        _object(
            {
                "server_id": _nullable(
                    _string(1, 64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?![\s\S])")
                ),
                "after": _integer(0, 128, default=0),
                "tool": _nullable(_string(1, 128, pattern=r"^[A-Za-z0-9_.-]+(?![\s\S])")),
            }
        )
    ),
    "mcp_call": _schema(
        _object(
            {
                "server_id": _string(1, 64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?![\s\S])"),
                "tool": _string(1, 128, pattern=r"^[A-Za-z0-9_.-]+(?![\s\S])"),
                "arguments": {"type": "object", "maxProperties": 64},
            },
            ("server_id", "tool", "arguments"),
        )
    ),
    "skills_list": _schema(_object({"after": _integer(0, 1_000_000, default=0)})),
    "skill_load": _schema(
        _object(
            {
                "skill_id": _string(1, 265, pattern=_SKILL_PATTERN),
                "resource": _nullable(
                    _string(1, 1024, description="Relative regular UTF-8 resource in the task pin.")
                ),
            },
            ("skill_id",),
        )
    ),
    "plugins_list": _schema(
        _object(
            {
                "plugin_id": _nullable(_string(1, 64, pattern=r"^[a-z][a-z0-9_-]{0,63}(?![\s\S])")),
                "after": _integer(0, 128, default=0),
            }
        )
    ),
    "plugin_call": _schema(
        _object(
            {
                "plugin_id": _string(1, 64, pattern=r"^[a-z][a-z0-9_-]{0,63}(?![\s\S])"),
                "arguments": {"type": "object"},
            },
            ("plugin_id", "arguments"),
        )
    ),
    "publish_ui": _publication_schema(),
    "finish": _schema(
        _object(
            {"summary": _string(), "objective_revision": _integer(1)},
            ("summary", "objective_revision"),
        )
    ),
    "tool_catalog": _schema(
        _object(
            {
                "kind": {"enum": ["native", "mcp", "skill", "plugin"]},
                "name": _string(1, 512, pattern=_IDENTITY_PATTERN),
                "server_id": _string(1, 128, pattern=_IDENTITY_PATTERN),
                "query": _string(0, 80),
                "after": _integer(0, 1_000_000, default=0),
            }
        )
    ),
}

_COMMON_REQUIREMENTS = (
    "native_task_authority",
    "current_session_epoch",
    "current_objective_revision",
    "provider_disclosure",
    "cancellation_fence",
)
_INTEGER_KIND = "Native strict integer checks also reject integral floats; standard JSON Schema integer does not distinguish 1 from 1.0."

# Metadata is trusted static text. In particular, no selected foreign schema,
# configuration, approval, filesystem path or invocation data enters this table.
_METADATA = {
    "consult_model": {
        "description": "Ask a task-pinned auxiliary text model; its response is untrusted data.",
        "source": ["services/agent/auxiliary.py:consult_model"],
        "requirements": [
            "captured_auxiliary_model",
            "measured_complete_request",
        ],
        "constraints": [
            "Model/provider and reasoning come only from native task pins, never arguments.",
            "An auxiliary receipt proves model response only, not work effects or correctness.",
        ],
        "legacy_behavior": [],
    },
    "read_file": {
        "description": "Read one complete bounded UTF-8 workspace file.",
        "source": ["services/agent/tools.py:run_read", "core/policy/__init__.py:ToolPolicy"],
        "requirements": ["workspace_scope", "protected_roots"],
        "constraints": [
            "Path must resolve to an existing regular file inside the allowed workspace.",
            "Success returns the complete stable UTF-8 file and a digest of those exact bytes; no prefix success or replacement decoding.",
            "The default byte bound is 65536 and the native maximum is 262144. A smaller-request refusal identifies the complete source and an allowed larger bound; beyond the native maximum only observed stat-version metadata is available, not a content hash.",
            _INTEGER_KIND,
        ],
        "legacy_behavior": [
            "Former prefix, replacement-decoding and limit-clamping read behavior is deliberately replaced by whole-file success or explicit refusal. Native bounds are not model context-capacity claims."
        ],
    },
    "write_file": {
        "description": "Replace one scoped workspace file with complete UTF-8 text.",
        "source": [
            "services/agent/tools.py:validate_write_arguments",
            "services/agent/tools.py:run_write",
            "core/policy/__init__.py:ToolPolicy",
        ],
        "requirements": [
            "workspace_scope",
            "protected_roots",
            "write_grant_or_action_approval",
            "uncommitted_speech_action_approval",
        ],
        "constraints": [
            "Content must UTF-8 encode within 262144 bytes; maxLength cannot express this byte limit.",
            "Optional objective_revision must equal the native current revision and cannot set it.",
            _INTEGER_KIND,
        ],
        "legacy_behavior": [],
    },
    "search": {
        "description": "Search scoped workspace text with a regular expression.",
        "source": ["services/agent/tools.py:run_search", "core/policy/__init__.py:ToolPolicy"],
        "requirements": ["workspace_scope", "protected_roots"],
        "constraints": [
            "Pattern must compile as a Python regular expression; path may be a file or directory.",
            "Output is capped at 50 matches and 500 characters per line; there is no native regex-time or traversal-count bound.",
        ],
        "legacy_behavior": [
            "Dispatch defaults a missing pattern to empty text and stringifies it; extra keys are ignored."
        ],
    },
    "shell": {
        "description": "Run a bounded command in the required workspace OS sandbox.",
        "source": [
            "services/agent/tools.py:run_shell",
            "services/agent/tools.py:build_bwrap_args",
            "core/policy/__init__.py:ToolPolicy",
        ],
        "requirements": [
            "workspace_scope",
            "protected_roots",
            "shell_grant_or_action_approval",
            "required_os_isolation",
            "network_grant_if_enabled",
            "uncommitted_speech_action_approval",
        ],
        "constraints": [
            "Sandbox availability and transport isolation remain native checks; shell permission can write the workspace without a separate write grant."
        ],
        "legacy_behavior": [
            "Dispatch stringifies command and float-coerces timeout_s; zero selects 60, other limits clamp to 1..120; fractional timeouts work; extra keys are ignored."
        ],
    },
    "read_transcript": {
        "description": "Read complete transcript text through bounded authorized pages for the task session epoch.",
        "source": [
            "services/agent/runtime.py:AgentRuntime._read_transcript",
            "services/agent/transcript_pages.py:read_transcript",
        ],
        "requirements": ["transcript_session_epoch", "entry_disclosure"],
        "constraints": [
            "Prefer paged:true. One event_id selects resolved or immutable entry_view; offset>0 requires expected_revision, expected_entry_sha256 and scope_token from the prior page.",
            "Discovery pages use immutable views and native fixed through_sequence; continue with both through_sequence and scope_token plus next_after until scan_exhausted.",
            "Paged max_chars defaults to 2000 and is at most 12000; responses contain at most 12000 text characters and 131072 UTF-8 JSON bytes. Metadata, provenance and current disclosure are native checks.",
            "New continuation arguments require paged:true. ID mode requires after=0, limit=1 and no through_sequence. Discovery forbids resolved view, nonzero offset and expected entry fields.",
            "Retained context stays bounded to 128 roots and 256 provenance nodes; repeated slices reuse the same root.",
        ],
        "legacy_behavior": [
            "Without paged:true the result remains a list; nonempty event_ids select resolved references independently of after/limit. Legacy limit/after coercions remain. Clipped entries explicitly carry entry_view, truncated, total_chars and a paged continuation hint; aggregate omission refuses."
        ],
    },
    "select_context": {
        "description": "Use the task-pinned selector to identify bounded workspace reference files.",
        "source": ["services/agent/context.py:select_context"],
        "requirements": ["workspace_scope", "protected_roots", "task_pinned_executable_selector"],
        "constraints": [
            "Path must identify a workspace directory; scan visits at most 512 names, samples at most 32 files and 2048 bytes per file.",
            _INTEGER_KIND,
        ],
        "legacy_behavior": [
            "Path defaults to the workspace root although older hints require it; extra keys are ignored."
        ],
    },
    "mcp_list": {
        "description": "List configured MCP metadata or explicitly discover one server's tools.",
        "source": [
            "services/agent/mcp_tools.py:run_mcp_list",
            "services/agent/mcp_tools.py:ModelScopedMCP",
        ],
        "requirements": [
            "mcp_host_configured",
            "provider_target_disclosure",
            "transport_grants_if_discovering",
        ],
        "constraints": [
            "Selecting tool requires a nonnull server_id; after remains validated for selected queries.",
            "Server discovery may initialize transport; metadata-only tool_catalog inspection must not call this operation.",
            _INTEGER_KIND,
        ],
        "legacy_behavior": [
            "Extra outer keys are ignored; pagination and selected-schema context bounds remain native."
        ],
    },
    "mcp_call": {
        "description": "Call one exact owner-allowlisted tool on a configured MCP server.",
        "source": [
            "services/agent/mcp_tools.py:run_mcp_call",
            "packages/ecosystem/mcp_host.py:MCPHost.call",
        ],
        "requirements": [
            "mcp_host_configured",
            "server_enabled",
            "exact_tool_allowlist",
            "provider_target_disclosure",
            "mcp_action_approval_unless_yolo",
            "transport_grants",
            "uncommitted_speech_action_approval",
        ],
        "constraints": [
            "Arguments must satisfy the current selected tool schema and fit the host's 65536-character serialized argument limit.",
            "Stdio requires the broad shell transport grant; remote HTTP requires network grant and configured remote allowance.",
            "A response receipt does not prove remote side effects; definition digests are not remote implementation hashes or task pins.",
        ],
        "legacy_behavior": [
            "Missing arguments defaults to an empty object and extra outer keys are ignored; MCP value bounds differ from the compute-plugin profile."
        ],
    },
    "skills_list": {
        "description": "List enabled model-eligible skill metadata under current disclosure policy.",
        "source": [
            "services/agent/skill_service.py:SkillService.catalog",
            "services/agent/runtime.py:AgentRuntime._skill_result",
        ],
        "requirements": [
            "skill_controller_configured",
            "skill_enabled_ready",
            "provider_target_disclosure",
            "author_model_invocation_allowed",
        ],
        "constraints": [
            "Disclosure and eligibility filtering precede pagination; listing creates no skill pin.",
            _INTEGER_KIND,
        ],
        "legacy_behavior": [],
    },
    "skill_load": {
        "description": "Load untrusted instructions or one UTF-8 resource from the task's verified skill pin.",
        "source": [
            "services/agent/skill_service.py:SkillService.load",
            "services/agent/runtime.py:AgentRuntime._skill_result",
        ],
        "requirements": [
            "skill_controller_configured",
            "skill_enabled_ready",
            "provider_target_disclosure",
            "author_invocation_allowed",
            "task_skill_version_pin",
        ],
        "constraints": [
            "Current and pinned invocation flags and disclosure scope are checked; only trusted explicit selection chooses user invocation.",
            "Resource must be a relative regular UTF-8 file in the verified package, at most 262144 bytes; the runtime response must fit 11500 serialized characters. Native normalization and pin bounds also apply.",
            "Loading instructions is not execution, an authority grant or task-effect evidence.",
        ],
        "legacy_behavior": [
            "PurePosixPath normalizes redundant separators and dot segments before the native resource path check."
        ],
    },
    "plugins_list": {
        "description": "Inspect bounded active approved compute-plugin metadata and selected input schemas.",
        "source": [
            "services/agent/plugin_service.py:PluginService.catalog",
            "services/agent/runtime.py:AgentRuntime._plugin_result",
        ],
        "requirements": [
            "plugin_host_configured",
            "owner_approved_enabled_plugin",
            "provider_target_disclosure",
        ],
        "constraints": [
            "The active version may differ from a task pin; selected task inspection must distinguish those schemas.",
            _INTEGER_KIND,
        ],
        "legacy_behavior": [],
    },
    "plugin_call": {
        "description": "Run an owner-approved exact task-pinned compute plugin in isolation.",
        "source": [
            "services/agent/plugin_service.py:PluginService.call_task",
            "core/plugins/registry.py:PluginRegistry.run",
            "core/plugins/schemas.py:validate_against",
        ],
        "requirements": [
            "plugin_host_configured",
            "owner_approved_enabled_plugin",
            "provider_target_disclosure",
            "task_plugin_version_pin",
            "required_os_isolation",
        ],
        "constraints": [
            "The selected pinned input schema validates arguments; finite JSON values are bounded to 65536 UTF-8 bytes, depth 32 and 4096 visited values/keys.",
            "Disablement and disclosure revocation remain effective; plugin computation cannot grant authority or prove external effects.",
        ],
        "legacy_behavior": [],
    },
    "publish_ui": {
        "description": "Publish or update a bounded native interactive result for Converse.",
        "source": [
            "services/agent/artifacts.py:ArtifactStore.publish",
            "services/agent/artifacts.py:validate_components",
            "services/agent/runtime.py:AgentRuntime._publish_ui",
        ],
        "requirements": [
            "artifact_store_configured",
            "active_originating_task_revision",
            "artifact_ownership_scope",
            "expected_artifact_revision_if_update",
            "publication_bounds_and_quotas",
            "uncommitted_speech_action_approval",
        ],
        "constraints": [
            "Both proposal and complete stored envelope must fit 49152 UTF-8 bytes, depth 12 and 8192 visits; maps have at most 128 entries and lists 1200. Server metadata consumes part of the envelope budget.",
            "Component and update IDs must be unique; update data must completely replace the existing component type while its title/type remain fixed.",
            "Table row widths equal columns; chart value count equals label count; diagram node IDs are unique and edge references exist; select value belongs to options; min <= value <= max.",
            "All numbers are finite. Native source URL validation enforces HTTPS, no credentials/whitespace/secret query keys; timestamps and simulated labels are not independently fact-verified.",
            "Controls change presentation only; Ask prefills the ordinary composer; a publication receipt proves publication only.",
            _INTEGER_KIND,
        ],
        "legacy_behavior": [],
    },
    "finish": {
        "description": "Propose successful completion for native current-revision evidence verification.",
        "source": [
            "services/agent/runtime.py:AgentRuntime._finish",
            "services/agent/runtime.py:AgentRuntime._succeed",
        ],
        "requirements": [
            "current_revision_evidence",
            "no_failed_required_effects",
            "native_artifact_file_verification",
            "durable_outcome_confirmation",
        ],
        "constraints": [
            "Revision must equal the current native objective revision; completion remains subject to cancellation/steering and durable outcome confirmation.",
            "When external or extension receipts are present, independent native effect/artifact evidence or a file-hash read is required. The new tool_catalog receipt is inspection only and cannot clear failed-effect taint or authorize completion.",
        ],
        "legacy_behavior": [
            "Native revision comparison uses equality, so integral floats and bool can compare equal; summary is optional, string-coerced, stripped and defaults to done; extra keys are ignored."
        ],
    },
    "tool_catalog": {
        "description": "Inspect bounded disclosure-filtered tool metadata or one selected logical schema without execution or pinning.",
        "source": [
            "docs/progressive-tool-catalog.md:query-and-response-contract",
            "packages/contracts/tool_catalog.py:parse_query",
        ],
        "requirements": [
            "catalog_feature_advertised",
            "disclosure_before_match_and_page",
            "metadata_only_no_execution_or_pinning",
        ],
        "constraints": [
            "The native parser enforces query combinations: name requires kind; MCP selection also requires server_id; server_id is only legal for kind=mcp.",
            "Selection cannot also search or paginate; empty query equals no search. No input chooses provider, scope, invocation, grants or pin creation.",
            "Pages contain at most 4 rows, each 2048 canonical ASCII bytes, page 16384 bytes; selected response and retained history unit are at most 32768 bytes.",
            _INTEGER_KIND,
        ],
        "legacy_behavior": [],
    },
}


def _known_name(name):
    # Exact type checking avoids invoking user-defined hashing/equality on a
    # malformed query. Error messages never include untrusted supplied values.
    if type(name) is not str or name not in _SCHEMAS:
        raise NativeToolSchemaError()
    return name


def native_tool_schema(name) -> dict:
    """Return a fresh complete canonical logical input JSON Schema."""
    return deepcopy(_SCHEMAS[_known_name(name)])


def native_tool_definition(name) -> dict:
    """Return fresh trusted schema/provenance/requirements inspection metadata.

    The digest covers only the schema's compact sorted ASCII JSON. Static
    requirements describe checks, not current grants or execution readiness.
    """
    checked = _known_name(name)
    schema = native_tool_schema(checked)
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    metadata = deepcopy(_METADATA[checked])
    return {
        **metadata,
        "input_schema": schema,
        "schema_sha256": sha256(encoded.encode("ascii")).hexdigest(),
        "requirements": [*_COMMON_REQUIREMENTS, *metadata["requirements"]],
    }
