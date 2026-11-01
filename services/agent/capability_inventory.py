"""Passive owner projections; availability is never an execution grant."""

import hashlib
import json

from packages.contracts.agent_tools import NATIVE_TOOL_ORDER
from packages.contracts.native_tool_schemas import native_tool_definition

GROUPS = {
    "files": ("read_file", "write_file", "search"),
    "execution": ("shell",),
    "transcript": ("read_transcript",),
    "harness": ("select_context",),
    "mcp": ("mcp_list", "mcp_call"),
    "skills": ("skills_list", "skill_load"),
    "plugins": ("plugins_list", "plugin_call"),
    "presentation": ("publish_ui",),
    "auxiliary": ("consult_model",),
    "discovery": ("tool_catalog",),
    "core": ("finish",),
}


def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(encode(value).encode("ascii")).hexdigest()


def _text(value, maximum=512):
    if type(value) is not str or len(value.encode("utf-8")) > maximum or "\x00" in value:
        raise ValueError("Installed metadata is unavailable")
    return value


def _authorization(runtime, scope, *, enabled, invocable=True, tool):
    if not enabled or not invocable or not runtime.tool_snapshot().supports(tool):
        return "blocked"
    try:
        info = runtime.provider_info_for(None)
        from packages.providers.disclosure import scope_allowed

        return "requires_action_checks" if scope_allowed(info, scope) else "blocked"
    except Exception:
        return "unknown"


def _native_authorization(runtime, name):
    try:
        if runtime.policy.denies(name):
            return "blocked"
        decision = runtime._permission_decision(None, name)
        if decision is not None and decision["effective"] == "deny":
            return "blocked"
        return "requires_action_checks"
    except Exception:
        return "unknown"


async def inventory(runtime, section, disabled):
    if section == "tools":
        host = runtime.host_tool_snapshot()
        rows = []
        for name in NATIVE_TOOL_ORDER:
            rows.append(
                {
                    "kind": "native",
                    "name": name,
                    "qualified_id": "native:" + name,
                    "group": next(group for group, names in GROUPS.items() if name in names),
                    "description": native_tool_definition(name)["description"],
                    "protocol_version": "1.0",
                    "protected": name == "finish",
                    "requested_enabled": name not in disabled,
                    "host_available": host.supports(name),
                    "effective_available": runtime.tool_snapshot().supports(name),
                    "authorization": _native_authorization(runtime, name),
                }
            )
        return "configured", rows
    if section == "skills":
        service = runtime.skill_service
        if service is None:
            return "not_configured", []
        from packages.ecosystem.skills import (
            FRONTMATTER_MAX_BYTES,
            REGISTRY_MAX_BYTES,
            SourceRef,
            _check_digest_format,
        )
        from services.agent.skill_service import MAX_PINS, TASK_ID_RE

        rows = []
        for record in await service.installed_inventory():
            source = record["source"]
            if type(source) is not dict:
                raise ValueError("Installed source metadata is unavailable")
            ref = SourceRef(
                kind=source.get("kind"),
                namespace=source.get("namespace"),
                name=record["name"],
                repository=source.get("repository"),
                ref=source.get("ref"),
                subdir=source.get("subdir"),
            ).validate()
            if ref.namespace != record["namespace"]:
                raise ValueError("Installed source identity differs")
            for value in (ref.namespace, ref.repository, ref.ref, ref.subdir):
                if value is not None:
                    _text(value, REGISTRY_MAX_BYTES)
            notes, pins = record["compatibility_notes"], record["dependent_task_ids"]
            if type(notes) is not list or len(encode(notes)) > REGISTRY_MAX_BYTES:
                raise ValueError("Installed compatibility metadata exceeds its native bound")
            for note in notes:
                _text(note, REGISTRY_MAX_BYTES)
            if (
                type(pins) is not list
                or len(pins) > MAX_PINS
                or any(type(pin) is not str or TASK_ID_RE.fullmatch(pin) is None for pin in pins)
            ):
                raise ValueError("Installed task pins are malformed")
            if any(
                type(record[key]) is not bool
                for key in ("installed", "enabled", "model_invocable", "user_invocable")
            ) or record["status"] not in {
                "ready",
                "disabled",
                "needs_setup",
                "needs_adapter",
                "unsupported",
            }:
                raise ValueError("Installed skill state is malformed")
            _check_digest_format(record["digest"])
            row = {
                "kind": "skill",
                "skill_id": _text(record["id"], 265),
                "name": _text(record["name"], 128),
                "version": _text(record["version"], 64),
                "digest": record["digest"],
                "description": _text(record["description"], FRONTMATTER_MAX_BYTES),
                "source": {
                    key: source.get(key)
                    for key in ("kind", "namespace", "repository", "ref", "subdir")
                },
                "installed": record["installed"],
                "enabled": record["enabled"],
                "status": record["status"],
                "compatibility_notes": record["compatibility_notes"],
                "model_invocable": record["model_invocable"],
                "user_invocable": record["user_invocable"],
                "scope": record["scope"],
                "integrity": "registered_only",
                "dependent_task_ids": record["dependent_task_ids"],
                "authorization": _authorization(
                    runtime,
                    record["scope"],
                    enabled=record["enabled"] and record["status"] == "ready",
                    invocable=record["model_invocable"],
                    tool="skill_load",
                ),
            }
            row["target_sha256"] = digest(row)
            rows.append(row)
        return "configured", rows
    if section != "plugins":
        raise ValueError("Unknown capability section")
    service = runtime.plugin_service
    if service is None:
        return "not_configured", []
    rows = []
    scopes = service._scopes()
    for record in service.registry.owner_inventory():
        manifest = record["manifest"]
        enabled = (
            record["enabled"]
            and record["active_version"] == record["version_id"]
            and record["approved"]
        )
        row = {
            "kind": "plugin",
            "plugin_id": record["plugin_id"],
            "version_id": record["version_id"],
            "source_sha256": record["source_sha256"],
            "manifest_sha256": record["manifest_sha256"],
            "description": manifest["description"],
            "runtime": manifest["runtime"],
            "author": manifest["author"],
            "license": manifest["license"],
            "installed": True,
            "enabled": enabled,
            "active_version": record["active_version"],
            "approved": record["approved"],
            "suite_configured": record["suite_configured"],
            "rollback_eligible": record["rollback_eligible"],
            "reports": record["reports"],
            "reports_total": record["reports_total"],
            "scope": scopes.get(record["version_id"], "local_only"),
            "integrity": "verified_current",
            "dependent_task_ids": record["dependent_task_ids"],
            "authorization": _authorization(
                runtime,
                scopes.get(record["version_id"], "local_only"),
                enabled=enabled,
                tool="plugin_call",
            ),
        }
        row["target_sha256"] = digest(row)
        rows.append(row)
    return "configured", rows
