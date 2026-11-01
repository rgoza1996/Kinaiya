"""Read-only model projection over native definitions and existing brokers.

No discovery transport, pin creation, permission approval or execution belongs
here. Foreign descriptions/schemas remain untrusted metadata. Each response is
a live inspection, not an execution snapshot or a grant.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

from packages.contracts.agent_tools import NATIVE_TOOL_ORDER
from packages.contracts.native_tool_schemas import native_tool_definition, native_tool_schema
from packages.contracts.tool_catalog import (
    CATALOG_KINDS,
    MAX_CATALOG_BYTES,
    CatalogError,
    catalog_page,
    encode_catalog,
    parse_query,
    qualify,
)


class CatalogGuard:
    """Native-only disclosure stamps; never serialized into a model result."""

    def __init__(self, runtime, rec):
        self.runtime, self.rec = runtime, rec
        self.provider = runtime.provider_info_for(rec).model_dump()
        self.snapshot = runtime.tool_snapshot(rec.task if rec is not None else None)
        self.context = self._context()
        self.sources = []
        self.retained_sources = []

    def _context(self):
        runtime, rec = self.runtime, self.rec
        policy = runtime.policy
        task = rec.task if rec is not None else None
        return (
            policy.effective_write,
            policy.effective_shell,
            policy.network,
            policy.yolo,
            policy.deny_write,
            policy.deny_shell,
            runtime._permission_fingerprint(rec),
            (task.objective_revision, task.content_scope, task.harness_version, task.state.value)
            if task is not None
            else None,
            runtime.owner_provider_context() if task is None else None,
        )

    def disclosure_current(self, *, retained=True):
        try:
            runtime, rec = self.runtime, self.rec
            if runtime.provider_info_for(rec).model_dump() != self.provider:
                return False
            for kind, service, rows, query in self.retained_sources if retained else self.sources:
                if kind == "skill" and rows:
                    if service is not runtime.skill_service or not service.tool_metadata_current(
                        runtime.provider_info_for(rec),
                        rows,
                        task_id=rec.task.task_id if rec is not None else None,
                        invocation="model",
                        retained=retained,
                    ):
                        return False
                elif kind == "plugin" and rows:
                    if service is not runtime.plugin_service or not service.tool_metadata_current(
                        runtime.provider_info_for(rec),
                        rows,
                        task=runtime._plugin_task(rec) if rec is not None else None,
                        retained=retained,
                    ):
                        return False
                elif kind == "mcp":
                    if service is not runtime.mcp_host:
                        return False
                    current = runtime._model_mcp(rec).cached_tool_metadata(
                        query.server_id, tool=query.name if query.selected else None
                    )
                    if retained:
                        # Cache refresh is a new observation, not withdrawal
                        # of owner disclosure. Config/scope/transport changes
                        # still invalidate retained content.
                        keys = (
                            "id",
                            "config_sha256",
                            "enabled",
                            "access_state",
                            "transport_grant",
                            "allow_remote",
                        )
                        before = [{key: row[key] for key in keys} for row in rows]
                        identities = {row["id"] for row in rows}
                        after = [
                            {key: row[key] for key in keys}
                            for row in current
                            if row["id"] in identities
                        ]
                        if before != after:
                            return False
                    elif rows != current:
                        return False
            return True
        except Exception:
            return False

    def current(self):
        return (
            self.runtime.tool_snapshot(self.rec.task if self.rec is not None else None)
            == self.snapshot
            and self._context() == self.context
            and self.disclosure_current(retained=False)
        )

    def retain_visible(self, result):
        """Keep future checks only for metadata actually disclosed in this page."""
        visible = result.get("items", [result.get("item")])
        identities = {(row["kind"], row["name"]) for row in visible if row is not None}
        servers = {row["server_id"] for row in visible if row is not None and row["kind"] == "mcp"}
        self.retained_sources = [
            (
                kind,
                service,
                [
                    row
                    for row in rows
                    if (kind, row["id"] if kind == "skill" else row["plugin_id"]) in identities
                ]
                if kind in {"skill", "plugin"}
                else [row for row in rows if row["id"] in servers],
                query,
            )
            for kind, service, rows, query in self.sources
        ]
        self.retained_sources = [source for source in self.retained_sources if source[2]]

    def commit(self):
        # Full inventories are needed only through the initial inspection's
        # await fences. A task retains at most its disclosed page/selection.
        self.sources = self.retained_sources


def _identity(kind, name, *, server_id=None):
    row = {"kind": kind, "name": name, "qualified_id": qualify(kind, name, server_id)}
    if server_id is not None:
        row["server_id"] = server_id
    return row


def _schema(row, schema, *, output_schema=None):
    """Return the whole schema, or a closed unavailable state; never clip it."""
    try:
        encoded = encode_catalog(schema)
        row["input_schema"] = json.loads(encoded)
        row["schema_sha256"] = hashlib.sha256(encoded.encode("ascii")).hexdigest()
        if output_schema is not None:
            row["output_schema"] = json.loads(encode_catalog(output_schema))
        # Leave room for the outer response and current task context.
        encode_catalog(row, max_bytes=MAX_CATALOG_BYTES - 2048)
    except CatalogError as exc:
        row.pop("input_schema", None)
        row.pop("output_schema", None)
        row.pop("schema_sha256", None)
        row["schema_state"] = (
            "too_large" if exc.code in {"catalog_too_large", "metadata_too_large"} else "invalid"
        )
    else:
        row["schema_state"] = "available"


def _permissions(runtime, task, tool, target=None):
    policy = runtime.policy
    scoped = None
    if runtime.tool_permissions is not None and (
        target is not None or tool in {"read_file", "write_file", "shell", "search"}
    ):
        try:
            scoped = (
                runtime.tool_permissions.resolve_target(task, target)
                if target is not None
                else runtime.tool_permissions.resolve(task, tool)
            )
        except Exception:
            raise CatalogError("invalid_metadata") from None
    approval = "uncommitted_speech_only" if tool == "publish_ui" else "not_required"
    if scoped is not None:
        approval = {"deny": "denied", "ask": "per_action", "allow": "not_required"}[
            scoped["effective"]
        ]
    elif policy.denies(tool):
        approval = "denied"
    elif tool in {"write_file", "shell"}:
        approval = "per_action" if policy.needs_approval(tool) else "broad_grant"
    elif tool == "mcp_call":
        approval = "broad_grant" if policy.yolo else "per_action"
    return {
        "approval": approval,
        "write_granted": policy.effective_write,
        "shell_granted": policy.effective_shell,
        "network_granted": policy.network,
        "uncommitted_speech_approval": (
            scoped["effective"] != "deny" if scoped is not None else not policy.denies(tool)
        )
        and tool in {"write_file", "shell", "mcp_call", "publish_ui"},
        **({"tool_rule": scoped} if scoped is not None else {}),
        "task_state": task.state.value if task is not None else None,
        "contextual_checks": "required_at_dispatch",
        "readiness": "not_checked",
    }


def _native(runtime, task, query):
    snapshot = runtime.tool_snapshot(task)
    names = (query.name,) if query.selected else NATIVE_TOOL_ORDER
    rows = []
    for name in names:
        if name not in NATIVE_TOOL_ORDER:
            continue
        definition = native_tool_definition(name)
        configured = snapshot.supports(name)
        row = {
            **_identity("native", name),
            "description": definition["description"],
            "availability": "configured" if configured else "not_configured",
            "schema_state": "available",
            "schema_kind": "native_arguments",
            "protocol_version": snapshot.schema_version,
        }
        if configured:
            row["invocation"] = {"tool": name}
        if query.selected:
            row.update({key: value for key, value in definition.items() if key != "description"})
            row["permissions"] = _permissions(runtime, task, name)
        rows.append(row)
    return rows


async def _skills(runtime, rec, query, guard):
    service = runtime.skill_service
    rows = await service.tool_metadata(
        runtime.provider_info_for(rec),
        skill_id=query.name if query.selected else None,
        task_id=rec.task.task_id if rec is not None else None,
        invocation="model",
    )
    guard.sources.append(("skill", service, copy.deepcopy(rows), query))
    result = []
    for item in rows:
        row = {
            **_identity("skill", item["id"]),
            "description": item["description"],
            "availability": "configured",
            "schema_state": "available",
            "schema_kind": "gateway_arguments",
        }
        if query.selected:
            row["invocation"] = {"tool": "skill_load", "arguments": {"skill_id": item["id"]}}
            row["provenance"] = {
                key: item[key]
                for key in (
                    "version",
                    "digest",
                    "source_kind",
                    "source_revision",
                    "active_version",
                    "task_pinned_version",
                    "integrity",
                    "model_invocable",
                    "user_invocable",
                )
            }
            schema = native_tool_schema("skill_load")
            schema["properties"]["skill_id"]["const"] = item["id"]
            _schema(row, schema)
            row["permissions"] = _permissions(
                runtime, rec.task if rec is not None else None, "skill_load"
            )
            row["requirements"] = [
                "current_and_pinned_disclosure",
                "current_invocation_flags",
                "verified_immutable_task_pin_on_load",
                "skill_content_is_not_authority",
            ]
        result.append(row)
    return result


async def _plugins(runtime, rec, query, guard):
    rows = await runtime.plugin_service.tool_metadata(
        runtime.provider_info_for(rec),
        plugin_id=query.name if query.selected else None,
        task=runtime._plugin_task(rec) if rec is not None else None,
    )
    guard.sources.append(("plugin", runtime.plugin_service, copy.deepcopy(rows), query))
    result = []
    for item in rows:
        row = {
            **_identity("plugin", item["plugin_id"]),
            "description": item["description"],
            "availability": "configured",
            "schema_state": "available",
            "schema_kind": "target_arguments",
            "invocation": {"tool": "plugin_call", "arguments": {"plugin_id": item["plugin_id"]}},
            "provenance": {
                key: item[key]
                for key in ("version_id", "source_sha256", "active_version", "task_pinned_version")
            },
        }
        if query.selected:
            _schema(row, item["input_schema"], output_schema=item["output_schema"])
            row["permissions"] = _permissions(
                runtime, rec.task if rec is not None else None, "plugin_call"
            )
            row["requirements"] = [
                "approved_enabled_version",
                "current_and_pinned_disclosure",
                "exact_task_pin_on_call",
                "isolated_compute_only",
            ]
        result.append(row)
    return result


def _mcp(runtime, query, guard):
    servers = runtime._model_mcp(guard.rec).cached_tool_metadata(
        query.server_id, tool=query.name if query.selected else None
    )
    guard.sources.append(("mcp", runtime.mcp_host, copy.deepcopy(servers), query))
    result = []
    for server in servers:
        tools = list(server["tools"])
        if not query.selected:
            observed = {tool["name"] for tool in tools}
            tools.extend(
                {"name": name, "description": "", "allowed": True, "schema_state": "not_discovered"}
                for name in server["allowed_tools"]
                if name not in observed
            )
        for tool in tools:
            configured = server["enabled"] and tool["allowed"]
            row = {
                **_identity("mcp", tool["name"], server_id=server["id"]),
                "description": tool["description"],
                "availability": "configured" if configured else "unavailable",
                "schema_state": tool["schema_state"],
                "schema_kind": "target_arguments",
                "provenance": {
                    "config_sha256": server["config_sha256"],
                    "definition_sha256": server["definition_sha256"],
                    "tool_sha256": tool.get("tool_sha256"),
                    "server_version": server["server_version"],
                },
            }
            if configured:
                row["invocation"] = {
                    "tool": "mcp_call",
                    "arguments": {"server_id": server["id"], "tool": tool["name"]},
                }
            if query.selected:
                row["access"] = {
                    key: server[key]
                    for key in (
                        "transport",
                        "configured_enabled",
                        "access_state",
                        "transport_grant",
                        "allow_remote",
                    )
                }
                row["allowlisted"] = tool["allowed"]
                row["requirements"] = [
                    "enabled_server",
                    "exact_tool_allowlist",
                    "current_disclosure",
                    "transport_grants",
                    "current_exact_target_rule_or_host_approval",
                    "schema_revalidated_at_call",
                    "remote_effects_not_verified_by_response",
                ]
                row["permissions"] = _permissions(
                    runtime,
                    guard.rec.task if guard.rec is not None else None,
                    "mcp_call",
                    target={"kind": "mcp", "server_id": server["id"], "name": tool["name"]},
                )
                if "input_schema" in tool:
                    _schema(row, tool["input_schema"])
            result.append(row)
    return result


async def inspect_catalog(runtime, arguments, *, rec=None, guard_sink=None):
    """Build the same bounded projection for HTTP inspection and model calls.

    `rec`, disclosure identity, invocation and policy are supplied by native
    code, never query fields. Optional brokers fail independently with closed
    states, while cancellation always propagates.
    """
    query = parse_query(arguments)
    guard = CatalogGuard(runtime, rec)
    task = rec.task if rec is not None else None
    context = {
        "inspection_only": True,
        "history_kind": "tool_catalog_result",
        "tool": "tool_catalog",
        "ok": True,
        "next_action": (
            "Inspection complete. Continue the requested work using the returned metadata. "
            "Inspection cannot complete work. Inspect again only when another schema is needed "
            "or metadata has changed."
        ),
        "task_id": task.task_id if task is not None else None,
        "objective_revision": task.objective_revision if task is not None else None,
        "task_state": task.state.value if task is not None else None,
        "untrusted_metadata": True,
    }
    services = {
        "native": True,
        "mcp": runtime.mcp_host is not None,
        "skill": runtime.skill_service is not None,
        "plugin": runtime.plugin_service is not None,
    }
    rows, catalogs = [], []
    for kind in CATALOG_KINDS:
        if query.kind is not None and kind != query.kind:
            continue
        state = "configured" if services[kind] else "not_configured"
        if services[kind]:
            try:
                if kind == "native":
                    batch = _native(runtime, task, query)
                elif kind == "mcp":
                    batch = _mcp(runtime, query, guard)
                elif kind == "skill":
                    batch = await _skills(runtime, rec, query, guard)
                else:
                    batch = await _plugins(runtime, rec, query, guard)
                if type(batch) is not list:
                    raise CatalogError("invalid_metadata")
                rows.extend(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                state = "metadata_unavailable"
        catalogs.append({"kind": kind, "state": state})
    if not guard.current():
        # Never release a projection assembled for a different trusted model
        # or objective. No input or backend exception text reaches the caller.
        raise CatalogError("invalid_metadata")
    if query.selected:
        item = next((row for row in rows if row["name"] == query.name), None)
        result = {
            "schema_version": "1.0",
            "item": item,
            "catalogs": catalogs,
            "state": "available"
            if item is not None
            else catalogs[0]["state"]
            if catalogs[0]["state"] != "configured"
            else "not_found",
        }
    else:
        result = catalog_page(rows, query, catalogs=catalogs)
    result.update(context)
    guard.retain_visible(result)
    if guard_sink is not None:
        guard_sink.append(guard)
    # Round-trip only exact bounded JSON. The returned structure shares no
    # mutable schema/cache objects with callers, providers or owner controls.
    return json.loads(encode_catalog(result))
