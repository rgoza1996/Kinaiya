"""Passive native identities, never discovery, probes, or remote code attestations."""

import hashlib
import json

from packages.contracts.agent_tools import NATIVE_TOOL_ORDER
from packages.contracts.native_tool_schemas import native_tool_schema
from packages.contracts.tool_permissions import NATIVE_PERMISSION_TOOLS


def identity(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def target_id(target):
    return identity(target)


def registry(runtime, selected=None):
    """At most 16 native + 16*(128 observed + 64 allowlisted) targets."""
    rows = {}
    snapshot = runtime.tool_snapshot()
    for name in NATIVE_TOOL_ORDER:
        if selected is not None and (selected["kind"] != "native" or selected["name"] != name):
            continue
        target = {"kind": "native", "name": name}
        legacy = name in NATIVE_PERMISSION_TOOLS
        editable = legacy or name == "search"
        rows[target_id(target)] = {
            "target": target,
            "binding": {
                "protocol_version": "1.0",
                "schema_sha256": identity(native_tool_schema(name)),
            },
            "available": snapshot.supports(name),
            "editable": editable,
            "rule_api": "legacy" if legacy else "target" if editable else None,
            "host_mode": "allow",
            "hard_denied": False,
            "reason": "Native operation rule"
            if editable
            else "Inventory only; no scoped rule broker",
        }
    host = runtime.mcp_host
    if host is None or (selected is not None and selected["kind"] == "native"):
        return rows
    try:
        servers = (
            host.cached_tool_metadata(selected["server_id"], tool=selected["name"])
            if selected is not None
            else host.cached_tool_metadata()
        )
    except Exception:
        raise RuntimeError("Passive MCP target metadata is unavailable") from None
    for server in servers:
        observed = {entry["name"]: entry for entry in server["tools"]}
        names = set(server["allowed_tools"]) | set(observed)
        for name in sorted(names):
            if selected is not None and selected["name"] != name:
                continue
            item = observed.get(name)
            tool_sha = item.get("tool_sha256") if item else None
            target = {"kind": "mcp", "server_id": server["id"], "name": name}
            allowed = name in server["allowed_tools"]
            available = bool(
                server["enabled"] and server["transport_grant"] and allowed and tool_sha
            )
            rows[target_id(target)] = {
                "target": target,
                "binding": {"config_sha256": server["config_sha256"], "tool_sha256": tool_sha},
                "available": available,
                "editable": True,
                "rule_api": "target",
                "host_mode": "allow" if runtime.policy.yolo else "ask",
                "hard_denied": not (server["enabled"] and server["transport_grant"] and allowed),
                "reason": "Observed schema; remote behavior unverified"
                if available
                else "Requires enabled transport, allowlist and observed schema",
            }
    if len(rows) > 3088:
        raise ValueError("Permission target inventory exceeds its native bound")
    return rows
