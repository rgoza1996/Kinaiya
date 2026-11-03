"""Truthful setup completion summary and machine-readable receipt formatting."""

from __future__ import annotations

import json
import os
import re
import textwrap
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECRET_VALUE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]+(?:\s+[A-Za-z0-9._~+/=-]{8,})?|ghp_[A-Za-z0-9]{10,})"
)
_SENSITIVE_KEYS = frozenset(
    {"api_key", "secret", "token", "password", "authorization", "owner_token", "client_secret"}
)


def redact_secrets(text: str) -> str:
    """Redact inline credentials, bearer tokens, and URL userinfo/query secrets."""
    if not text:
        return ""
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    cleaned = _SECRET_VALUE_RE.sub("[REDACTED]", cleaned)
    return cleaned


def redact_endpoint(endpoint: str | None) -> str:
    """Strip any userinfo, query parameters, or fragments from an endpoint URL."""
    if not endpoint or not isinstance(endpoint, str):
        return "http://127.0.0.1:1234"
    cleaned = _ANSI_ESCAPE_RE.sub("", endpoint.strip())
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return "[REDACTED_ENDPOINT]"
    if not parsed.scheme or not parsed.hostname:
        return redact_secrets(cleaned)
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunsplit((parsed.scheme, netloc, redact_secrets(parsed.path), "", ""))


def compute_readiness_state(receipt: Mapping[str, Any]) -> tuple[str, str, bool]:
    """Return (readiness_state_code, badge_text, is_ready) truthfully from receipt evidence."""
    host_running = receipt.get("host_running") is True
    if host_running:
        return "host_running", "Host running", True

    conn = str(receipt.get("provider_connection") or "not_checked").strip().lower()
    if conn == "verified":
        detail = redact_secrets(
            str(receipt.get("connection_detail") or receipt.get("provider_detail") or "ok")
        )
        return "connection_verified", f"Connection verified ({detail})", False
    if conn == "failed":
        reason = redact_secrets(
            str(
                receipt.get("connection_error")
                or receipt.get("failure_reason")
                or "endpoint unreachable"
            )
        )
        return "connection_failed", f"Connection check failed ({reason})", False
    return "configured_untested", "Configured (not tested)", False


def _extract_agent_info(receipt: Mapping[str, Any]) -> dict[str, Any]:
    providers = receipt.get("providers")
    agent_from_providers = (
        providers.get("agent")
        if isinstance(providers, Mapping) and isinstance(providers.get("agent"), Mapping)
        else {}
    )
    agent_direct = receipt.get("agent") if isinstance(receipt.get("agent"), Mapping) else {}
    merged = {**agent_from_providers, **agent_direct}

    adapter = (
        receipt.get("adapter")
        or receipt.get("kind")
        or merged.get("adapter")
        or merged.get("kind")
        or ("meta" if (receipt.get("allow_cloud") or merged.get("allow_cloud")) else "lmstudio")
    )
    endpoint = (
        receipt.get("endpoint")
        or receipt.get("base_url")
        or merged.get("endpoint")
        or merged.get("base_url")
        or "http://127.0.0.1:1234"
    )
    model = receipt.get("model") or merged.get("model") or "unconfigured-model"
    credential_env = (
        receipt.get("credential_env")
        or receipt.get("api_key_env")
        or merged.get("credential_env")
        or merged.get("api_key_env")
    )
    return {
        "adapter": str(adapter),
        "endpoint": redact_endpoint(str(endpoint)),
        "model": redact_secrets(str(model)),
        "credential_env": str(credential_env) if credential_env else None,
    }


def _extract_converse_label(receipt: Mapping[str, Any], default_model: str) -> tuple[bool, str]:
    dist = receipt.get("distribution")
    enabled = bool(
        receipt.get("converse") is True
        or receipt.get("converse_enabled") is True
        or (isinstance(dist, Mapping) and dist.get("converse") is True)
    )
    if not enabled:
        return False, "Disabled (Agent-only)"

    providers = receipt.get("providers")
    converse_cfg = (
        providers.get("converse")
        if isinstance(providers, Mapping) and isinstance(providers.get("converse"), Mapping)
        else (
            receipt.get("converse_profile")
            if isinstance(receipt.get("converse_profile"), Mapping)
            else {}
        )
    )
    converse_model = receipt.get("converse_model") or converse_cfg.get("model") or default_model
    return True, f"Enabled ({redact_secrets(str(converse_model))})"


def sanitize_completion_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return a pure, secret-free, ANSI-free dictionary suitable for JSON output."""
    state_code, badge, is_ready = compute_readiness_state(receipt)
    agent = _extract_agent_info(receipt)
    converse_enabled, converse_label = _extract_converse_label(receipt, agent["model"])
    conn = str(receipt.get("provider_connection") or "not_checked").strip().lower()
    next_cmd = str(receipt.get("next_command") or receipt.get("next") or "kinaiya serve host")

    sanitized: dict[str, Any] = {
        "ready": is_ready,
        "provider_connection": conn,
        "readiness_state": state_code,
        "readiness_badge": badge,
        "install": str(receipt.get("install") or ""),
        "launcher": str(receipt.get("launcher") or "kinaiya"),
        "config": str(receipt.get("config") or ""),
        "data": str(receipt.get("data") or ""),
        "workspace": str(receipt.get("workspace") or ""),
        "agent": {
            "adapter": agent["adapter"],
            "endpoint": agent["endpoint"],
            "model": agent["model"],
        },
        "converse": converse_enabled,
        "converse_status": converse_label,
        "next": next_cmd,
        "next_command": next_cmd,
    }
    if agent["credential_env"]:
        sanitized["credential_env"] = agent["credential_env"]
    if "web_assets_available" in receipt:
        sanitized["web_assets_available"] = bool(receipt["web_assets_available"])
    return sanitized


def format_json_receipt(receipt: Mapping[str, Any]) -> str:
    """Serialize a sanitized machine-readable completion receipt to JSON without secrets or ANSI."""
    return json.dumps(sanitize_completion_receipt(receipt), indent=2)


def _wrap_line(text: str, width: int) -> list[str]:
    effective_width = max(20, width)
    if len(text) <= effective_width:
        return [text]
    return textwrap.wrap(
        text,
        width=effective_width,
        subsequent_indent="  ",
        break_long_words=True,
        break_on_hyphens=False,
    )


def render_completion_summary(
    receipt: Mapping[str, Any],
    *,
    is_tty: bool = True,
    no_color: bool = False,
    width: int = 80,
    env: Mapping[str, str] | None = None,
) -> str:
    """Render a concise, truthful terminal completion summary without side effects."""
    effective_env = os.environ if env is None else env
    use_color = bool(is_tty and not no_color and not effective_env.get("NO_COLOR"))
    effective_width = max(24, int(width) if width else 80)

    state_code, badge_text, _ = compute_readiness_state(receipt)
    agent = _extract_agent_info(receipt)
    _, converse_label = _extract_converse_label(receipt, agent["model"])

    install_loc = str(receipt.get("install") or receipt.get("launcher") or "local-layout")
    config_loc = str(receipt.get("config") or "unconfigured")
    data_loc = str(receipt.get("data") or "unconfigured")
    workspace_loc = str(receipt.get("workspace") or "unconfigured")
    next_cmd = str(receipt.get("next_command") or receipt.get("next") or "kinaiya serve host")

    rule = "-" * min(effective_width, 60)
    raw_lines: list[str] = [
        rule,
        "Kinaiya Setup Summary",
        rule,
        f"Status: {badge_text}",
        f"Install: {install_loc}",
        f"Agent: {agent['adapter']} @ {agent['endpoint']} (model: {agent['model']})",
        f"Converse: {converse_label}",
        f"Config: {config_loc}",
        f"Data: {data_loc}",
        f"Workspace: {workspace_loc}",
    ]

    if receipt.get("web_assets_available") is False:
        raw_lines.append(
            "Web UI assets: missing (run 'flutter build web' in apps/kinaiya_flutter "
            "or install a verified release bundle)"
        )

    raw_lines.append(rule)
    raw_lines.append("Next steps:")

    credential_env = agent["credential_env"]
    credential_saved = bool(
        receipt.get("credential_saved")
        or receipt.get("credential_status") in {"saved", "masked", "present"}
    )
    env_already_set = bool(credential_env and effective_env.get(credential_env))
    if credential_env and not credential_saved and not env_already_set:
        raw_lines.append(f"  1. export {credential_env}=<your-api-key>")
        raw_lines.append(f"  2. Start host: {next_cmd} (or kinaiya start)")
    else:
        raw_lines.append(f"  1. Start host: {next_cmd} (or kinaiya start)")

    raw_lines.append("  Diagnose or edit: kinaiya doctor | kinaiya setup model")
    raw_lines.append(rule)

    wrapped: list[str] = []
    for line in raw_lines:
        if line == rule:
            wrapped.append(rule)
        else:
            wrapped.extend(_wrap_line(line, effective_width))

    if use_color:
        color_code = {
            "host_running": "\x1b[32m",
            "connection_verified": "\x1b[32m",
            "configured_untested": "\x1b[33m",
            "connection_failed": "\x1b[31m",
        }.get(state_code, "\x1b[33m")
        styled: list[str] = []
        for line in wrapped:
            if line.startswith("Status: "):
                styled.append(f"{color_code}[*]\x1b[0m {line}")
            else:
                styled.append(line)
        return "\n".join(styled) + "\n"

    return "\n".join(wrapped) + "\n"
