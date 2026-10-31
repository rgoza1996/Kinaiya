"""MCP tool implementations for the seed Agent.

``mcp_list`` exposes bounded owner-configured metadata (ids and allowlisted
tool names, or one server's untrusted tool schemas). ``mcp_call`` brokers one
owner-allowlisted call through the configured ``MCPHost`` and authors a
broker-generated ``external`` receipt from the observed response.

Trust boundaries preserved here, not in prompts:

- No secrets, credential endpoints, or secret values ever flow into
  observations or evidence. ``mcp_list`` without an id projects only
  ``{id, enabled, allowed_tools}`` from host metadata.
- Server descriptions and results are untrusted input. Discovery never
  grants call authority; the host enforces enabled state, the exact
  allowlist, and the shell/network transport grants on every call, even
  under YOLO.
- ``mcp_call`` consumes the bound owner approval inside dispatch, after the
  runtime's epoch/revision checks and before any external bytes are sent.
  Caller cancellation propagates; nothing here replays a timed-out or
  cancelled call.
- The ``external`` receipt proves a response was received. It NEVER
  verifies remote side effects.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core.policy import ApprovalError, PolicyDenied
from core.supervision import truncate
from packages.contracts.models import Evidence
from services.agent.tools import ToolFailure

MAX_MCP_LIST_SERVERS = 16
MAX_MCP_LIST_TOOLS = 128
MAX_MCP_DESCRIPTION_CHARS = 500


class ModelScopedMCP:
    """Constrain model-facing MCP data by owner scope and actual provider."""

    def __init__(self, host, provider_info):
        self.host = host
        self.local = provider_info.local
        # The current Meta adapter always enables Contributor mode, including
        # aliases whose configured name does not contain "contributor".
        self.contributor = (
            provider_info.provider == "meta" or "contributor" in provider_info.model.lower()
        )

    def _allowed(self, entry):
        if self.local:
            return True
        scope = entry.get("content_scope", "local_only")
        if self.contributor:
            return scope == "public_synthetic" and not entry.get("credentialed", True)
        return scope in {"public_synthetic", "private_cloud_authorized"}

    def list_servers(self):
        return [entry for entry in self.host.list_servers() if self._allowed(entry)]

    def cached_tool_metadata(self, server_id=None, *, tool=None):
        """Filter passive metadata on both sides of the host projection."""
        try:
            before = {entry["id"] for entry in self.list_servers()}
            if server_id is not None and server_id not in before:
                return []
            rows = self.host.cached_tool_metadata(server_id, tool=tool)
            after = {entry["id"] for entry in self.list_servers()}
            return [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"content_scope", "credentialed"}
                }
                for row in rows
                if row["id"] in before & after and self._allowed(row)
            ]
        except Exception:
            raise ToolFailure("MCP metadata is unavailable") from None

    def _check(self, server):
        if not any(entry["id"] == server for entry in self.list_servers()):
            raise PolicyDenied("MCP server is unavailable under this model disclosure policy")

    async def discover(self, server):
        self._check(server)
        result = await self.host.discover(server)
        self._check(server)
        return result

    async def call(self, server, tool, arguments):
        self._check(server)
        result = await self.host.call(server, tool, arguments)
        self._check(server)
        return result


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _payload_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _require_host(host: Any) -> Any:
    if host is None:
        raise ToolFailure("No MCP servers configured for this task")
    return host


async def run_mcp_list(
    host: Any,
    server_id: str | None,
    objective_revision: int,
    *,
    after: int = 0,
    tool: str | None = None,
) -> tuple[str, Evidence]:
    """Return valid bounded JSON pages; full schemas load only when selected."""
    _require_host(host)
    if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after <= 128:
        raise ToolFailure("mcp_list after must be an integer cursor in 0..128")
    if tool is not None and (not isinstance(tool, str) or not tool or server_id is None):
        raise ToolFailure("mcp_list tool needs a server_id and exact tool name")
    try:
        if server_id is None:
            entries = [
                {
                    "id": item["id"],
                    "enabled": item["enabled"],
                    "allowed_tools": item["allowed_tools"][:8],
                    "more_allowed_tools": len(item["allowed_tools"]) > 8,
                }
                for item in host.list_servers()
            ]
            body = {"servers": [], "next_after": None}
            rows = body["servers"]
        else:
            if not isinstance(server_id, str) or not server_id.strip():
                raise ToolFailure("mcp_list requires a non-empty server_id string")
            entries = await host.discover(server_id)
            if tool is not None:
                selected = next((item for item in entries if item["name"] == tool), None)
                if selected is None:
                    raise ToolFailure("MCP tool is absent from current discovery")
                body = {"server": server_id, "tool": selected, "untrusted": True}
                if len(_canonical(body)) > 1800:
                    raise ToolFailure(
                        "Selected MCP schema exceeds this model-context profile; an adapter is required"
                    )
                observation = _canonical(body)
                return observation, Evidence(
                    kind="read",
                    summary=f"MCP schema {server_id}/{tool} (untrusted)",
                    sha256=_payload_hash(body),
                    output=observation,
                    objective_revision=objective_revision,
                )
            entries = [
                {
                    "name": item["name"],
                    "allowed": item["allowed"],
                    "description": item["description"][:80],
                }
                for item in entries
            ]
            body = {"server": server_id, "tools": [], "next_after": None, "untrusted": True}
            rows = body["tools"]
        for item in entries[after : after + 4]:
            rows.append(item)
            if len(_canonical(body)) > 1700:
                rows.pop()
                break
        next_after = after + len(rows)
        body["next_after"] = next_after if next_after < len(entries) else None
        observation = _canonical(body)
        return observation, Evidence(
            kind="read",
            summary="MCP metadata page (does not verify external effects)",
            sha256=_payload_hash(body),
            output=observation,
            objective_revision=objective_revision,
        )
    except ToolFailure:
        raise
    except Exception as exc:
        raise ToolFailure(f"mcp_list failed: {exc}") from exc


async def run_mcp_call(
    host: Any,
    policy: Any,
    server_id: str,
    tool: str,
    arguments: Any,
    objective_revision: int,
    *,
    approval_id: str | None = None,
    digest: str | None = None,
) -> tuple[str, Evidence]:
    """Call one allowlisted tool and author the broker-generated receipt."""
    _require_host(host)
    if not isinstance(server_id, str) or not server_id.strip():
        raise ToolFailure("mcp_call requires a non-empty server_id string")
    if not isinstance(tool, str) or not tool.strip():
        raise ToolFailure("mcp_call requires a non-empty tool string")
    if not isinstance(arguments, dict):
        raise ToolFailure("mcp_call arguments must be an object")
    expected = policy.digest_for(
        "mcp_call",
        {
            "server_id": server_id,
            "tool": tool,
            "arguments": arguments,
        },
        objective_revision,
    )
    if approval_id is None and not policy.yolo:
        raise ApprovalError("mcp_call needs a bound owner approval")
    if approval_id is not None:
        # One-use, digest- and revision-bound spend, committed here after
        # the runtime's epoch/revision checks and before external bytes.
        if digest != expected:
            raise ApprovalError("mcp_call arguments do not match the approved digest")
        policy.consume_approval(approval_id, expected, objective_revision)
    try:
        result = await host.call(server_id, tool, dict(arguments))
    except Exception as exc:
        raise ToolFailure(f"mcp_call {server_id}/{tool} failed: {exc}") from exc
    if not isinstance(result, dict):
        raise ToolFailure(f"mcp_call {server_id}/{tool} returned an unusable result")
    receipt_hash = _payload_hash(result)
    texts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"][:2000])
    structured = result.get("structured")
    detail = "\n".join(texts) if texts else _canonical(structured)
    observation = truncate(f"mcp {server_id}/{tool} response (untrusted): {detail}")
    return observation, Evidence(
        kind="external",
        summary=(
            f"mcp {server_id}/{tool} response receipt "
            "(untrusted; proves a response arrived, never remote side effects)"
        ),
        sha256=receipt_hash,
        output=truncate(_canonical(result)),
        objective_revision=objective_revision,
    )
