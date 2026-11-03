"""User-local .env persistence and /models discovery for custom OpenAI-compatible APIs."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import inspect
import ipaddress
import json
import math
import os
import re
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

MASKED_API_KEY_DISPLAY = "••••••••••••"
DEFAULT_CHAT_COMPLETIONS_PATH = "/chat/completions"
DEFAULT_MODELS_PATH = "/models"
REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high")
DEFAULT_GLOBAL_REASONING_EFFORT = "medium"
CUSTOM_PROVIDER_ROLES = (
    "converse",
    "agent",
    "decisions",
    "heartbeat",
    "memory",
    "escalation",
)

MAX_ENV_FILE_BYTES = 256 * 1024
MAX_DISCOVERY_RESPONSE_BYTES = 256 * 1024
MAX_DISCOVERED_MODELS = 512
MAX_OPERATION_RECEIPTS = 32
MAX_REVISION = 9_007_199_254_740_990

_PROFILE_ID_RE = re.compile(r"^p([0-9]{2,6})$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,160}$")
_ENDPOINT_PATH_RE = re.compile(r"^/[A-Za-z0-9._:/@-]{1,159}$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")

UNVERIFIED_CUSTOM_CAPABILITIES: dict[str, Any] = {
    "chat": "unverified",
    "streaming": "unverified",
    "reasoning": "unverified",
    "multimodal": "unverified",
    "tool_use": "unverified",
    "agent_qualified": False,
}

PROPOSED_ROLE_DIAGNOSTIC = (
    "Custom role binding is saved as a proposed selection in .env pending "
    "native role-runtime admission; live runtime requests do not use it yet."
)

REMOVED_PROFILE_ROLE_DIAGNOSTIC = (
    "Referenced custom provider profile was removed; this role binding is "
    "retained as unavailable and needs reselection."
)

_ACTION_ALIASES: dict[str, str] = {
    "add": "add_custom_provider",
    "add_custom_provider": "add_custom_provider",
    "update": "update_custom_provider",
    "update_custom_provider": "update_custom_provider",
    "remove": "remove_custom_provider",
    "remove_custom_provider": "remove_custom_provider",
    "set_enabled": "set_custom_provider_enabled",
    "set_custom_provider_enabled": "set_custom_provider_enabled",
    "discover": "discover_custom_models",
    "discover_custom_models": "discover_custom_models",
    "bind_role": "bind_custom_role_model",
    "bind_custom_role_model": "bind_custom_role_model",
    "set_reasoning_effort": "set_global_reasoning_effort",
    "set_global_reasoning_effort": "set_global_reasoning_effort",
    "reconcile": "reconcile_custom_provider_operation",
    "reconcile_custom_provider_operation": "reconcile_custom_provider_operation",
}

_OPERATION_ID_KEYS = ("client_operation_id", "operation_id", "idempotency_key")
_COMMON_MUTATION_OPTIONAL_KEYS = {"expected_revision", *_OPERATION_ID_KEYS}

_ACTION_ALLOWED_KEYS: dict[str, set[str]] = {
    "add_custom_provider": {
        "action",
        "name",
        "base_url",
        "api_key",
        "enabled",
        "selected_model",
        "allow_remote_http",
        "chat_completions_path",
        "models_path",
        "custom_endpoints",
        "max_output_tokens",
        *_COMMON_MUTATION_OPTIONAL_KEYS,
    },
    "update_custom_provider": {
        "action",
        "profile_id",
        "name",
        "base_url",
        "api_key",
        "clear_api_key",
        "enabled",
        "selected_model",
        "allow_remote_http",
        "chat_completions_path",
        "models_path",
        "custom_endpoints",
        "max_output_tokens",
        "clear_max_output_tokens",
        *_COMMON_MUTATION_OPTIONAL_KEYS,
    },
    "remove_custom_provider": {
        "action",
        "profile_id",
        "confirm_referenced",
        *_COMMON_MUTATION_OPTIONAL_KEYS,
    },
    "set_custom_provider_enabled": {
        "action",
        "profile_id",
        "enabled",
        *_COMMON_MUTATION_OPTIONAL_KEYS,
    },
    "discover_custom_models": {
        "action",
        "profile_id",
        *_COMMON_MUTATION_OPTIONAL_KEYS,
    },
    "bind_custom_role_model": {
        "action",
        "role",
        "profile_id",
        "model_id",
        "reasoning_effort",
        *_COMMON_MUTATION_OPTIONAL_KEYS,
    },
    "set_global_reasoning_effort": {
        "action",
        "reasoning_effort",
        *_COMMON_MUTATION_OPTIONAL_KEYS,
    },
    "reconcile_custom_provider_operation": {
        "action",
        *_OPERATION_ID_KEYS,
    },
}

_ACTION_REQUIRED_KEYS: dict[str, set[str]] = {
    "add_custom_provider": {"action", "name", "base_url"},
    "update_custom_provider": {"action", "profile_id"},
    "remove_custom_provider": {"action", "profile_id"},
    "set_custom_provider_enabled": {"action", "profile_id", "enabled"},
    "discover_custom_models": {"action", "profile_id"},
    "bind_custom_role_model": {"action", "role"},
    "set_global_reasoning_effort": {"action", "reasoning_effort"},
    "reconcile_custom_provider_operation": {"action"},
}


class CustomProviderError(ValueError):
    """Raised when a custom provider operation fails validation or execution."""


class CustomProviderValidationError(CustomProviderError):
    """Raised when a custom provider request violates strict wire type or schema bounds."""


class CustomProviderConflictError(CustomProviderError):
    """Raised when a custom provider mutation conflicts with current revision or operation identity."""

    def __init__(self, message: str, *, error_code: str = "custom_provider_conflict") -> None:
        super().__init__(message)
        self.error_code = error_code


class CustomProviderStaleDiscoveryError(CustomProviderConflictError):
    """Raised when in-flight model discovery completes after the target profile or key changed."""

    def __init__(self, profile_id: str, reason: str) -> None:
        super().__init__(
            f"Stale model discovery for {profile_id!r} refused: {reason}",
            error_code="custom_provider_stale_discovery",
        )
        self.profile_id = profile_id
        self.reason = reason


class CustomProviderReferencedError(CustomProviderConflictError):
    """Raised when removing a custom provider that is still referenced without confirmation."""

    def __init__(self, profile_id: str, referenced_by: list[str]) -> None:
        super().__init__(
            f"Custom provider {profile_id!r} is referenced by {', '.join(referenced_by)}; "
            "confirmation is required before removal.",
            error_code="custom_provider_referenced",
        )
        self.profile_id = profile_id
        self.referenced_by = list(referenced_by)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject HTTP redirects so Bearer credentials are never forwarded across origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Redirects are disallowed for authenticated model discovery.",
            headers,
            fp,
        )


def _require_bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        raise CustomProviderValidationError(f"{field_name} must be an exact boolean.")
    return value


def _require_optional_bool(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    return _require_bool(value, field_name)


def _require_str(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise CustomProviderValidationError(f"{field_name} must be a string.")
    return value


def _require_optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, field_name)


def _validate_expected_revision(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > MAX_REVISION:
        raise CustomProviderValidationError(
            "expected_revision must be a non-negative integer within bounds."
        )
    return value


def _validate_operation_id(value: Any, field_name: str = "client_operation_id") -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise CustomProviderValidationError(f"{field_name} must be a string.")
    cleaned = value.strip()
    if not cleaned or cleaned != value or not _OPERATION_ID_RE.match(cleaned):
        raise CustomProviderValidationError(
            f"{field_name} must be 1..128 characters matching [A-Za-z0-9:._-]."
        )
    return cleaned


def _resolve_operation_id(
    *,
    client_operation_id: Any = None,
    operation_id: Any = None,
    idempotency_key: Any = None,
) -> str | None:
    candidates: list[str] = []
    for name, raw in (
        ("client_operation_id", client_operation_id),
        ("operation_id", operation_id),
        ("idempotency_key", idempotency_key),
    ):
        validated = _validate_operation_id(raw, name)
        if validated is not None:
            candidates.append(validated)
    if not candidates:
        return None
    first = candidates[0]
    if any(c != first for c in candidates[1:]):
        raise CustomProviderValidationError("Conflicting operation ID fields were supplied.")
    return first


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def normalize_openai_base_url(base_url: str, *, allow_remote_http: bool = False) -> str:
    """Validate and normalize an OpenAI-compatible base URL.

    Ensures the returned base URL ends with `/v1` (unless a custom versioned path
    already ends with `/v1` or another explicit version segment) and never leaves
    trailing `/models` or `/chat/completions` suffixes that would duplicate paths.
    """
    _require_bool(allow_remote_http, "allow_remote_http")
    if type(base_url) is not str or not base_url.strip():
        raise CustomProviderValidationError("Base URL is required.")
    raw = base_url.strip()
    if any(ch in raw for ch in ("\r", "\n", "\x00", " ")):
        raise CustomProviderValidationError(
            "Base URL must not contain whitespace or control characters."
        )
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise CustomProviderValidationError("Base URL scheme must be https or loopback http.")
    if parsed.username or parsed.password:
        raise CustomProviderValidationError("Base URL must not embed credentials.")
    if not parsed.hostname:
        raise CustomProviderValidationError("Base URL must include a valid host.")
    if parsed.query or parsed.fragment:
        raise CustomProviderValidationError(
            "Base URL must not include query parameters or fragments."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise CustomProviderValidationError("Base URL port is invalid.") from exc

    is_loopback = _is_loopback_host(parsed.hostname)
    if scheme == "http" and not is_loopback and not allow_remote_http:
        raise CustomProviderValidationError(
            "Remote HTTP base URLs are disallowed by default; use HTTPS or loopback HTTP."
        )

    # Normalize path: strip trailing slashes, accidental endpoint suffixes, and duplicate /v1
    path = parsed.path or ""
    while path.endswith("/") and len(path) > 1:
        path = path[:-1]
    if path == "/":
        path = ""

    for suffix in ("/chat/completions", "/completions", "/models"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            while path.endswith("/") and len(path) > 1:
                path = path[:-1]

    # Collapse repeated /v1/v1 segments at the tail
    while path.lower().endswith("/v1/v1"):
        path = path[:-3]

    if not path:
        path = "/v1"
    elif not path.lower().endswith("/v1") and not re.search(r"/v[0-9]+[a-z0-9._-]*$", path.lower()):
        path = f"{path}/v1"

    host_str = parsed.hostname
    if ":" in host_str and not host_str.startswith("["):
        host_str = f"[{host_str}]"
    netloc = f"{host_str}:{port}" if port is not None else host_str
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))


def _validate_single_line_text(value: str, field_name: str, *, max_len: int = 256) -> str:
    if type(value) is not str:
        raise CustomProviderValidationError(f"{field_name} must be a string.")
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise CustomProviderValidationError(
            f"{field_name} must not contain newlines or null bytes."
        )
    cleaned = value.strip()
    if not cleaned:
        raise CustomProviderValidationError(f"{field_name} must not be empty.")
    if len(cleaned) > max_len:
        raise CustomProviderValidationError(f"{field_name} exceeds maximum length ({max_len}).")
    return cleaned


def _validate_model_id(model_id: str) -> str:
    cleaned = _validate_single_line_text(model_id, "Model ID", max_len=160)
    if not _MODEL_ID_RE.match(cleaned):
        raise CustomProviderValidationError(
            "Model ID contains unsupported characters (allowed: letters, digits, ., _, :, /, @, -)."
        )
    return cleaned


def _validate_endpoint_path(
    path_value: str | None,
    field_name: str,
    *,
    default_path: str,
) -> str:
    if path_value is None:
        return default_path
    if type(path_value) is not str:
        raise CustomProviderValidationError(f"{field_name} must be a string.")
    if any(ch in path_value for ch in ("\r", "\n", "\x00", " ")):
        raise CustomProviderValidationError(
            f"{field_name} must not contain whitespace or control characters."
        )
    cleaned = path_value.strip()
    if not cleaned:
        return default_path
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    while len(cleaned) > 1 and cleaned.endswith("/"):
        cleaned = cleaned[:-1]
    if ".." in cleaned or not _ENDPOINT_PATH_RE.match(cleaned):
        raise CustomProviderValidationError(
            f"{field_name} must be a valid relative endpoint path starting with '/' (e.g. {default_path!r})."
        )
    return cleaned


def _validate_custom_endpoints(raw_value: str | None) -> str:
    """Validate optional comma-separated custom endpoint paths (e.g. '/v1/systemone' for llama.cpp)."""
    if raw_value is None:
        return ""
    if type(raw_value) is not str:
        raise CustomProviderValidationError("Custom endpoints must be a string.")
    if any(ch in raw_value for ch in ("\r", "\n", "\x00")):
        raise CustomProviderValidationError(
            "Custom endpoints must not contain newlines or null bytes."
        )
    stripped = raw_value.strip()
    if not stripped:
        return ""
    tokens = [tok.strip() for tok in re.split(r"[,;]+", stripped) if tok.strip()]
    normalized: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        path = _validate_endpoint_path(token, "Custom endpoint path", default_path="")
        if path and path not in seen:
            seen.add(path)
            normalized.append(path)
    joined = ", ".join(normalized)
    if len(joined) > 256:
        raise CustomProviderValidationError("Custom endpoints exceed maximum length (256).")
    return joined


def _validate_max_output_tokens(value: Any) -> int | None:
    """Validate optional max_output_tokens with strict int | None wire typing."""
    if value is None:
        return None
    if type(value) is not int:
        raise CustomProviderValidationError("Maximum output tokens must be an integer or None.")
    if value < 1 or value > 2_000_000:
        raise CustomProviderValidationError(
            "Maximum output tokens must be between 1 and 2,000,000 (or None for model default)."
        )
    return value


def _parse_env_max_output_tokens(raw_value: str) -> int | None:
    """Parse max_output_tokens string stored in .env file."""
    cleaned = raw_value.strip()
    if not cleaned:
        return None
    if not re.fullmatch(r"[0-9]{1,8}", cleaned):
        return None
    parsed = int(cleaned)
    if 1 <= parsed <= 2_000_000:
        return parsed
    return None


def _validate_reasoning_effort(value: str | None, *, allow_empty: bool = False) -> str:
    if value is None:
        return "" if allow_empty else DEFAULT_GLOBAL_REASONING_EFFORT
    if type(value) is not str:
        raise CustomProviderValidationError("Reasoning effort must be a string.")
    cleaned = value.strip().lower()
    if allow_empty and cleaned in ("", "global", "default"):
        return ""
    if cleaned not in REASONING_EFFORT_VALUES:
        allowed = ", ".join(REASONING_EFFORT_VALUES)
        raise CustomProviderValidationError(f"Reasoning effort must be one of: {allowed}.")
    return cleaned


def _resolve_endpoint_url(normalized_base: str, endpoint_path: str) -> str:
    """Resolve an endpoint path against a normalized OpenAI base URL without duplicating /v1."""
    clean_base = normalized_base.rstrip("/")
    clean_path = (
        endpoint_path.strip() if endpoint_path and endpoint_path.strip() else DEFAULT_MODELS_PATH
    )
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    if clean_base.lower().endswith("/v1") and clean_path.lower().startswith("/v1/"):
        clean_path = clean_path[3:]
    elif clean_base.lower().endswith("/v1") and clean_path.lower() == "/v1":
        clean_path = ""
    return f"{clean_base}{clean_path}"


def _escape_dotenv_value(value: str, *, quote: bool = True) -> str:
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise CustomProviderValidationError(
            "Environment value must not contain newlines or null bytes."
        )
    if not quote:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote_dotenv_value(raw_value: str) -> str:
    val = raw_value.strip()
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        inner = val[1:-1]
        out: list[str] = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt in ('"', "\\"):
                    out.append(nxt)
                    i += 2
                    continue
            out.append(ch)
            i += 1
        return "".join(out)
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    # Strip trailing unquoted comment if preceded by whitespace
    for idx, ch in enumerate(val):
        if ch == "#" and (idx == 0 or val[idx - 1].isspace()):
            val = val[:idx].rstrip()
            break
    return val


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _compute_request_sha256(action: str, params: dict[str, Any]) -> str:
    sanitized: dict[str, Any] = {"action": action}
    for key, val in sorted(params.items()):
        if key in _OPERATION_ID_KEYS or key == "action":
            continue
        if key == "api_key" and isinstance(val, str):
            sanitized["api_key_sha256"] = _hash_secret(val)
        else:
            sanitized[key] = val
    return hashlib.sha256(_canonical_json(sanitized).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _EnvLine:
    raw: str
    key: str | None = None
    value: str | None = None


def _parse_dotenv_lines(content: str) -> list[_EnvLine]:
    lines: list[_EnvLine] = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(_EnvLine(raw=raw_line))
            continue
        candidate = stripped
        if candidate.startswith("export "):
            candidate = candidate[len("export ") :].lstrip()
        if "=" not in candidate:
            lines.append(_EnvLine(raw=raw_line))
            continue
        key_part, val_part = candidate.split("=", 1)
        key = key_part.strip()
        if not _ENV_KEY_RE.match(key):
            lines.append(_EnvLine(raw=raw_line))
            continue
        lines.append(_EnvLine(raw=raw_line, key=key, value=_unquote_dotenv_value(val_part)))
    return lines


def _is_managed_key(key: str) -> bool:
    if key in (
        "KINAIYA_CUSTOM_PROVIDER_IDS",
        "KINAIYA_CUSTOM_PROVIDER_LAST_SEQ",
        "KINAIYA_CUSTOM_PROVIDER_REVISION",
        "KINAIYA_CUSTOM_PROVIDER_RECEIPTS_JSON",
        "KINAIYA_GLOBAL_REASONING_EFFORT",
    ):
        return True
    if key.startswith("KINAIYA_PROVIDER_P") and re.match(
        r"^KINAIYA_PROVIDER_P[0-9]{2,6}_(NAME|BASE_URL|API_KEY|ENABLED|MODEL|DISCOVERED_MODELS|DISCOVERY_STATUS|ALLOW_REMOTE_HTTP|CHAT_PATH|MODELS_PATH|CUSTOM_ENDPOINTS|MAX_OUTPUT_TOKENS)$",
        key,
    ):
        return True
    if key.startswith("KINAIYA_CUSTOM_ROLE_"):
        role_suffix = key[len("KINAIYA_CUSTOM_ROLE_") :].lower()
        if role_suffix in CUSTOM_PROVIDER_ROLES:
            return True
        if role_suffix.endswith("_reasoning"):
            base_role = role_suffix[: -len("_reasoning")]
            return base_role in CUSTOM_PROVIDER_ROLES
    return False


@dataclass
class _StoredCustomProfile:
    profile_id: str
    name: str
    base_url: str
    api_key: str
    enabled: bool = True
    selected_model: str = ""
    discovered_models: list[str] | None = None
    discovery_status: str = "unprobed"
    allow_remote_http: bool = False
    chat_completions_path: str = DEFAULT_CHAT_COMPLETIONS_PATH
    models_path: str = DEFAULT_MODELS_PATH
    custom_endpoints: str = ""
    max_output_tokens: int | None = None

    @property
    def api_key_sha256(self) -> str:
        return _hash_secret(self.api_key) if self.api_key else ""

    def to_public_dict(self, *, referenced_by: list[str]) -> dict[str, Any]:
        has_key = bool(self.api_key)
        discovered = list(self.discovered_models or [])
        return {
            "profile_id": self.profile_id,
            "provider_type": "custom_openai_compatible",
            "name": self.name,
            "base_url": self.base_url,
            "chat_completions_path": self.chat_completions_path or DEFAULT_CHAT_COMPLETIONS_PATH,
            "models_path": self.models_path or DEFAULT_MODELS_PATH,
            "custom_endpoints": self.custom_endpoints,
            "max_output_tokens": self.max_output_tokens,
            "enabled": bool(self.enabled),
            "has_api_key": has_key,
            "masked_api_key": MASKED_API_KEY_DISPLAY if has_key else "",
            "selected_model": self.selected_model,
            "qualified_model_id": (
                f"{self.profile_id}:{self.selected_model}" if self.selected_model else ""
            ),
            "discovered_models": discovered,
            "discovery_status": self.discovery_status or "unprobed",
            "allow_remote_http": bool(self.allow_remote_http),
            "referenced_by": list(referenced_by),
            "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
        }


@dataclass(frozen=True)
class _DiscoveryFingerprint:
    profile_id: str
    provider_name: str
    base_url: str
    models_path: str
    models_url: str
    api_key_sha256: str
    enabled: bool
    allow_remote_http: bool
    pre_revision: int


@dataclass(frozen=True)
class _RawDiscoveryOutcome:
    status: str
    http_status: int | None
    discovered_models: list[str]
    message: str


@dataclass
class _LoadedState:
    lines: list[_EnvLine]
    profiles: list[_StoredCustomProfile]
    role_bindings: dict[str, str]
    role_reasonings: dict[str, str]
    global_reasoning_effort: str
    max_seq: int
    revision: int
    receipts: dict[str, dict[str, Any]]


async def _shielded_aclose(resource: Any, *, cleanup_timeout_s: float = 2.0) -> None:
    """Ensure async resource close settles even if the caller is cancelled or timed out."""
    if resource is None:
        return
    close_fn = getattr(resource, "aclose", None)
    if close_fn is None:
        return
    task = asyncio.create_task(close_fn())
    interrupted = False
    try:
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=cleanup_timeout_s)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                break
    finally:
        if not task.done():
            task.cancel()
        else:
            try:
                task.result()
            except (Exception, asyncio.CancelledError):
                pass
    if interrupted:
        raise asyncio.CancelledError


def _parse_bounded_models_json(raw_body: bytes, models_url: str) -> _RawDiscoveryOutcome:
    """Parse and bound a /models JSON response body without unbounded recursion or size."""
    try:
        text = raw_body.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _RawDiscoveryOutcome(
            status="invalid_response",
            http_status=200,
            discovered_models=[],
            message=(
                f"Endpoint {models_url} did not return valid JSON. "
                "Enter a manual model ID if this server uses a non-standard /models response."
            ),
        )

    # Structural depth and node walk bound
    pending: list[tuple[Any, int]] = [(payload, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 16384:
            return _RawDiscoveryOutcome(
                status="invalid_response",
                http_status=200,
                discovered_models=[],
                message=(
                    f"Endpoint {models_url} returned an overly complex JSON structure. "
                    "Enter a manual model ID below."
                ),
            )
        if isinstance(item, dict):
            for k, v in item.items():
                pending.append((k, depth + 1))
                pending.append((v, depth + 1))
        elif isinstance(item, list):
            for elem in item:
                pending.append((elem, depth + 1))
        elif isinstance(item, float) and not math.isfinite(item):
            return _RawDiscoveryOutcome(
                status="invalid_response",
                http_status=200,
                discovered_models=[],
                message=f"Endpoint {models_url} returned non-finite numbers in JSON.",
            )

    items: list[Any] | None = None
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("models"), list):
            items = payload["models"]
    elif isinstance(payload, list):
        items = payload

    if items is None:
        return _RawDiscoveryOutcome(
            status="invalid_response",
            http_status=200,
            discovered_models=[],
            message=(
                f"Endpoint {models_url} JSON did not contain a 'data' or 'models' list. "
                "Enter a manual model ID below."
            ),
        )

    discovered: list[str] = []
    seen_ids: set[str] = set()
    for entry in items:
        if len(discovered) >= MAX_DISCOVERED_MODELS:
            break
        candidate_id = ""
        if isinstance(entry, dict):
            raw_id = entry.get("id") if isinstance(entry.get("id"), str) else entry.get("name")
            if isinstance(raw_id, str):
                candidate_id = raw_id.strip()
        elif isinstance(entry, str):
            candidate_id = entry.strip()
        if not candidate_id or not _MODEL_ID_RE.match(candidate_id):
            continue
        if candidate_id not in seen_ids:
            seen_ids.add(candidate_id)
            discovered.append(candidate_id)

    if not discovered:
        return _RawDiscoveryOutcome(
            status="empty",
            http_status=200,
            discovered_models=[],
            message=(
                f"Endpoint {models_url} returned 0 models. "
                "Load a model on the server or enter a manual model ID."
            ),
        )

    return _RawDiscoveryOutcome(
        status="ok",
        http_status=200,
        discovered_models=discovered,
        message=f"Discovered {len(discovered)} model(s) from {models_url}.",
    )


def _classify_http_status(status_code: int, models_url: str) -> _RawDiscoveryOutcome:
    if status_code in (301, 302, 303, 307, 308):
        return _RawDiscoveryOutcome(
            status="redirect_blocked",
            http_status=status_code,
            discovered_models=[],
            message=(
                f"Redirect (HTTP {status_code}) was blocked so Bearer credentials "
                "are never forwarded. Update the Base URL to the final endpoint."
            ),
        )
    if status_code in (401, 403):
        return _RawDiscoveryOutcome(
            status="auth_error",
            http_status=status_code,
            discovered_models=[],
            message=(
                f"Authentication failed (HTTP {status_code}) when querying {models_url}. "
                "Check or rotate the API key, or enter a manual model ID."
            ),
        )
    if status_code in (404, 405, 501):
        return _RawDiscoveryOutcome(
            status="unsupported",
            http_status=status_code,
            discovered_models=[],
            message=(
                f"Endpoint {models_url} returned HTTP {status_code} (/models not supported). "
                "You can still enter a manual model ID below."
            ),
        )
    return _RawDiscoveryOutcome(
        status="connection_error",
        http_status=status_code,
        discovered_models=[],
        message=(
            f"Endpoint {models_url} returned HTTP {status_code}. "
            "Verify the Base URL or enter a manual model ID."
        ),
    )


class CustomProvidersEnvStore:
    """User-local .env store for unlimited custom OpenAI-compatible provider profiles."""

    def __init__(
        self,
        env_path: Path,
        *,
        workspace: Path | None = None,
        http_opener: Callable[..., Any] | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
        async_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        discovery_timeout_s: float = 5.0,
    ) -> None:
        raw_env = Path(env_path)
        abs_env = Path(os.path.abspath(raw_env))
        if (
            raw_env.is_symlink()
            or abs_env.is_symlink()
            or abs_env.parent.is_symlink()
            or abs_env.parent != abs_env.parent.resolve()
            or (abs_env.exists() and abs_env != abs_env.resolve())
        ):
            raise CustomProviderError("Custom provider .env path must not contain symlinks.")

        resolved_workspace: Path | None = None
        if workspace is not None:
            raw_ws = Path(workspace)
            abs_ws = Path(os.path.abspath(raw_ws))
            if raw_ws.is_symlink() or abs_ws != abs_ws.resolve():
                raise CustomProviderError("Workspace path must not contain symlinks.")
            if abs_env == abs_ws or abs_env.is_relative_to(abs_ws):
                raise CustomProviderError(
                    "Custom provider .env path must be outside the agent workspace."
                )
            resolved_workspace = abs_ws

        self._env_path = abs_env
        self._lock_path = abs_env.parent / f".{abs_env.name}.lock"
        self._workspace = resolved_workspace
        self._http_opener = http_opener
        self._async_transport = async_transport
        self._async_client_factory = async_client_factory
        self._discovery_timeout_s = float(discovery_timeout_s)
        self._thread_lock = threading.RLock()
        self._active_discoveries: set[asyncio.Task[Any]] = set()

    @property
    def env_path(self) -> Path:
        return self._env_path

    async def aclose(self) -> None:
        """Cancel and await any in-flight discovery tasks so transport cleanup settles."""
        tasks = list(self._active_discoveries)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _verify_parent_dir(self, *, create: bool = False) -> Path:
        parent = self._env_path.parent
        if self._workspace is not None and (
            self._env_path == self._workspace or self._env_path.is_relative_to(self._workspace)
        ):
            raise CustomProviderError(
                "Custom provider .env path must be outside the agent workspace."
            )
        for ancestor in (parent, *parent.parents):
            if ancestor.is_symlink():
                raise CustomProviderError(
                    "Custom provider .env parent directory must not be a symlink."
                )
        if parent != parent.resolve():
            raise CustomProviderError(
                "Custom provider .env parent directory must not contain symlinks."
            )
        if create:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            st = os.lstat(parent)
        except FileNotFoundError:
            return parent
        except OSError as exc:
            raise CustomProviderError(
                f"Custom provider .env parent directory is inaccessible: {exc}"
            ) from exc
        if not stat.S_ISDIR(st.st_mode):
            raise CustomProviderError("Custom provider .env parent path must be a directory.")
        if st.st_uid != os.getuid():
            raise CustomProviderError(
                "Custom provider .env parent directory must be owned by the current user."
            )
        return parent

    @contextmanager
    def _exclusive_lock(self):
        with self._thread_lock:
            self._verify_parent_dir(create=True)
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            try:
                fd = os.open(self._lock_path, flags, 0o600)
            except OSError as exc:
                raise CustomProviderError(
                    f"Could not open custom provider lock file: {exc}"
                ) from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
                    raise CustomProviderError(
                        "Custom provider lock file must be an owner-only regular file."
                    )
                if (st.st_mode & 0o077) != 0:
                    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)

    def _read_protected_bytes(self, target_path: Path, *, max_bytes: int) -> bytes | None:
        self._verify_parent_dir(create=False)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(target_path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CustomProviderError(
                f"Custom provider .env path {target_path.name!r} could not be opened safely: {exc}"
            ) from exc

        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise CustomProviderError(
                    f"Custom provider file {target_path.name!r} must be a regular file."
                )
            if st.st_nlink != 1:
                raise CustomProviderError(
                    f"Custom provider file {target_path.name!r} must not have hard links."
                )
            if st.st_uid != os.getuid():
                raise CustomProviderError(
                    f"Custom provider file {target_path.name!r} must be owned by the current user."
                )
            if (st.st_mode & 0o077) != 0:
                raise CustomProviderError(
                    f"Custom provider file {target_path.name!r} must have 0600 permissions."
                )
            if st.st_size > max_bytes:
                raise CustomProviderError(
                    f"Custom provider file {target_path.name!r} exceeds maximum size ({max_bytes} bytes)."
                )

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise CustomProviderError(
                        f"Custom provider file {target_path.name!r} exceeds maximum size ({max_bytes} bytes)."
                    )

            try:
                actual_st = os.lstat(target_path)
            except OSError as exc:
                raise CustomProviderError(
                    f"Custom provider file {target_path.name!r} disappeared during read."
                ) from exc
            if (actual_st.st_dev, actual_st.st_ino) != (st.st_dev, st.st_ino):
                raise CustomProviderError(
                    f"Custom provider file {target_path.name!r} identity changed during read."
                )
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _write_protected_bytes(self, target_path: Path, payload: bytes, *, max_bytes: int) -> None:
        if len(payload) > max_bytes:
            raise CustomProviderError(
                f"Serialized {target_path.name!r} exceeds maximum size ({max_bytes} bytes)."
            )
        parent = self._verify_parent_dir(create=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            dir=str(parent),
        )
        tmp_path = Path(tmp_name)
        fd_closed = False
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or st.st_nlink != 1
                or (st.st_mode & 0o077) != 0
            ):
                raise CustomProviderError("Temporary custom provider file failed safety check.")
            view = memoryview(payload)
            written = 0
            while written < len(payload):
                n = os.write(fd, view[written:])
                if n <= 0:
                    raise OSError("Short write to temporary custom provider file.")
                written += n
            os.fsync(fd)
            os.close(fd)
            fd_closed = True

            os.replace(tmp_path, target_path)
            dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if not fd_closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _read_raw_env(self) -> tuple[list[_EnvLine], dict[str, str]]:
        raw_bytes = self._read_protected_bytes(self._env_path, max_bytes=MAX_ENV_FILE_BYTES)
        if raw_bytes is None:
            return [], {}
        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CustomProviderError("Custom provider .env file must be valid UTF-8.") from exc
        lines = _parse_dotenv_lines(content)
        env_map: dict[str, str] = {}
        for item in lines:
            if item.key is not None and item.value is not None:
                env_map[item.key] = item.value
        return lines, env_map

    def _load_state(self) -> _LoadedState:
        lines, env_map = self._read_raw_env()
        raw_ids = env_map.get("KINAIYA_CUSTOM_PROVIDER_IDS", "").strip()
        profile_ids: list[str] = []
        seen: set[str] = set()
        max_seq = 0
        if raw_ids:
            for token in raw_ids.split(","):
                pid = token.strip().lower()
                if not pid:
                    continue
                m = _PROFILE_ID_RE.match(pid)
                if not m or pid in seen:
                    continue
                seen.add(pid)
                profile_ids.append(pid)
                max_seq = max(max_seq, int(m.group(1)))

        try:
            stored_seq = int(env_map.get("KINAIYA_CUSTOM_PROVIDER_LAST_SEQ", "0").strip() or "0")
            if stored_seq > max_seq:
                max_seq = stored_seq
        except ValueError:
            pass

        # Also inspect any KINAIYA_PROVIDER_Pxx_* keys to avoid ever reusing a historical ID
        for key in env_map:
            m = re.match(r"^KINAIYA_PROVIDER_P([0-9]{2,6})_", key)
            if m:
                max_seq = max(max_seq, int(m.group(1)))

        raw_revision = env_map.get("KINAIYA_CUSTOM_PROVIDER_REVISION", "0").strip() or "0"
        try:
            revision = int(raw_revision)
        except ValueError as exc:
            raise CustomProviderError(
                "Stored KINAIYA_CUSTOM_PROVIDER_REVISION is invalid."
            ) from exc
        if revision < 0 or revision > MAX_REVISION:
            raise CustomProviderError("Stored KINAIYA_CUSTOM_PROVIDER_REVISION is out of bounds.")

        receipts: dict[str, dict[str, Any]] = {}
        raw_receipts = env_map.get("KINAIYA_CUSTOM_PROVIDER_RECEIPTS_JSON", "").strip()
        if raw_receipts:
            try:
                parsed_receipts = json.loads(raw_receipts)
                if isinstance(parsed_receipts, dict):
                    for k, v in parsed_receipts.items():
                        if isinstance(k, str) and _OPERATION_ID_RE.match(k) and isinstance(v, dict):
                            receipts[k] = v
            except (json.JSONDecodeError, RecursionError):
                raise CustomProviderError(
                    "Stored KINAIYA_CUSTOM_PROVIDER_RECEIPTS_JSON is corrupted."
                ) from None

        raw_global_effort = (
            env_map.get("KINAIYA_GLOBAL_REASONING_EFFORT", DEFAULT_GLOBAL_REASONING_EFFORT)
            .strip()
            .lower()
        )
        global_reasoning_effort = (
            raw_global_effort
            if raw_global_effort in REASONING_EFFORT_VALUES
            else DEFAULT_GLOBAL_REASONING_EFFORT
        )

        profiles: list[_StoredCustomProfile] = []
        for pid in profile_ids:
            prefix = f"KINAIYA_PROVIDER_{pid.upper()}_"
            name = env_map.get(f"{prefix}NAME", "").strip() or pid.upper()
            base_url = env_map.get(f"{prefix}BASE_URL", "").strip()
            if not base_url:
                continue
            api_key = env_map.get(f"{prefix}API_KEY", "")
            enabled_raw = env_map.get(f"{prefix}ENABLED", "true").strip().lower()
            enabled = enabled_raw not in ("false", "0", "no", "off")
            selected_model = env_map.get(f"{prefix}MODEL", "").strip()
            allow_http_raw = env_map.get(f"{prefix}ALLOW_REMOTE_HTTP", "false").strip().lower()
            allow_remote_http = allow_http_raw in ("true", "1", "yes", "on")
            discovered_raw = env_map.get(f"{prefix}DISCOVERED_MODELS", "").strip()
            discovered_models = [m.strip() for m in discovered_raw.split(",") if m.strip()]
            discovery_status = env_map.get(f"{prefix}DISCOVERY_STATUS", "").strip() or (
                "ok" if discovered_models else "unprobed"
            )
            chat_path_raw = env_map.get(f"{prefix}CHAT_PATH", "").strip()
            try:
                chat_completions_path = _validate_endpoint_path(
                    chat_path_raw,
                    "Chat completions endpoint path",
                    default_path=DEFAULT_CHAT_COMPLETIONS_PATH,
                )
            except CustomProviderError:
                chat_completions_path = DEFAULT_CHAT_COMPLETIONS_PATH

            models_path_raw = env_map.get(f"{prefix}MODELS_PATH", "").strip()
            try:
                models_path = _validate_endpoint_path(
                    models_path_raw,
                    "Models endpoint path",
                    default_path=DEFAULT_MODELS_PATH,
                )
            except CustomProviderError:
                models_path = DEFAULT_MODELS_PATH

            custom_endpoints_raw = env_map.get(f"{prefix}CUSTOM_ENDPOINTS", "").strip()
            try:
                custom_endpoints = _validate_custom_endpoints(custom_endpoints_raw)
            except CustomProviderError:
                custom_endpoints = ""

            max_tokens_raw = env_map.get(f"{prefix}MAX_OUTPUT_TOKENS", "").strip()
            max_output_tokens = _parse_env_max_output_tokens(max_tokens_raw)

            profiles.append(
                _StoredCustomProfile(
                    profile_id=pid,
                    name=name,
                    base_url=base_url,
                    api_key=api_key,
                    enabled=enabled,
                    selected_model=selected_model,
                    discovered_models=discovered_models,
                    discovery_status=discovery_status,
                    allow_remote_http=allow_remote_http,
                    chat_completions_path=chat_completions_path,
                    models_path=models_path,
                    custom_endpoints=custom_endpoints,
                    max_output_tokens=max_output_tokens,
                )
            )

        role_bindings: dict[str, str] = {}
        role_reasonings: dict[str, str] = {}
        for role in CUSTOM_PROVIDER_ROLES:
            raw_binding = env_map.get(f"KINAIYA_CUSTOM_ROLE_{role.upper()}", "").strip()
            if ":" in raw_binding:
                pid_part, model_part = raw_binding.split(":", 1)
                pid_clean = pid_part.strip().lower()
                model_clean = model_part.strip()
                # Retain role binding even if pid_clean was removed so r56
                # unavailable_needs_reselection state is preserved across reloads!
                if _PROFILE_ID_RE.match(pid_clean) and model_clean:
                    role_bindings[role] = f"{pid_clean}:{model_clean}"
            raw_role_reasoning = (
                env_map.get(f"KINAIYA_CUSTOM_ROLE_{role.upper()}_REASONING", "").strip().lower()
            )
            if raw_role_reasoning in REASONING_EFFORT_VALUES:
                role_reasonings[role] = raw_role_reasoning

        return _LoadedState(
            lines=lines,
            profiles=profiles,
            role_bindings=role_bindings,
            role_reasonings=role_reasonings,
            global_reasoning_effort=global_reasoning_effort,
            max_seq=max_seq,
            revision=revision,
            receipts=receipts,
        )

    @staticmethod
    def _compute_state_sha256(
        *,
        revision: int,
        global_reasoning_effort: str,
        profiles: list[_StoredCustomProfile],
        role_bindings: dict[str, str],
        role_reasonings: dict[str, str],
    ) -> str:
        canonical = {
            "revision": revision,
            "global_reasoning_effort": global_reasoning_effort,
            "profiles": [
                {
                    "profile_id": p.profile_id,
                    "name": p.name,
                    "base_url": p.base_url,
                    "api_key_sha256": p.api_key_sha256,
                    "enabled": bool(p.enabled),
                    "selected_model": p.selected_model,
                    "discovered_models": list(p.discovered_models or []),
                    "discovery_status": p.discovery_status or "unprobed",
                    "allow_remote_http": bool(p.allow_remote_http),
                    "chat_completions_path": p.chat_completions_path
                    or DEFAULT_CHAT_COMPLETIONS_PATH,
                    "models_path": p.models_path or DEFAULT_MODELS_PATH,
                    "custom_endpoints": p.custom_endpoints,
                    "max_output_tokens": p.max_output_tokens,
                }
                for p in profiles
            ],
            "role_bindings": dict(sorted(role_bindings.items())),
            "role_reasonings": dict(sorted(role_reasonings.items())),
        }
        return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()

    def _write_state(
        self,
        existing_lines: list[_EnvLine],
        profiles: list[_StoredCustomProfile],
        role_bindings: dict[str, str],
        max_seq: int,
        *,
        revision: int,
        receipts: dict[str, dict[str, Any]] | None = None,
        role_reasonings: dict[str, str] | None = None,
        global_reasoning_effort: str = DEFAULT_GLOBAL_REASONING_EFFORT,
    ) -> None:
        preserved_lines: list[str] = []
        for item in existing_lines:
            if item.key is not None and _is_managed_key(item.key):
                continue
            preserved_lines.append(item.raw)

        # Strip trailing blank lines from preserved section before appending managed keys
        while preserved_lines and not preserved_lines[-1].strip():
            preserved_lines.pop()

        managed_lines: list[str] = []
        ids_csv = ",".join(p.profile_id for p in profiles)
        managed_lines.append(f"KINAIYA_CUSTOM_PROVIDER_IDS={ids_csv}")
        managed_lines.append(f"KINAIYA_CUSTOM_PROVIDER_REVISION={revision}")
        if max_seq > 0:
            managed_lines.append(f"KINAIYA_CUSTOM_PROVIDER_LAST_SEQ={max_seq}")
        clean_global_effort = (
            global_reasoning_effort
            if global_reasoning_effort in REASONING_EFFORT_VALUES
            else DEFAULT_GLOBAL_REASONING_EFFORT
        )
        managed_lines.append(
            f"KINAIYA_GLOBAL_REASONING_EFFORT={_escape_dotenv_value(clean_global_effort, quote=True)}"
        )

        for profile in profiles:
            prefix = f"KINAIYA_PROVIDER_{profile.profile_id.upper()}_"
            managed_lines.append(f"{prefix}NAME={_escape_dotenv_value(profile.name, quote=True)}")
            managed_lines.append(
                f"{prefix}BASE_URL={_escape_dotenv_value(profile.base_url, quote=True)}"
            )
            managed_lines.append(
                f"{prefix}CHAT_PATH={_escape_dotenv_value(profile.chat_completions_path or DEFAULT_CHAT_COMPLETIONS_PATH, quote=True)}"
            )
            managed_lines.append(
                f"{prefix}MODELS_PATH={_escape_dotenv_value(profile.models_path or DEFAULT_MODELS_PATH, quote=True)}"
            )
            if profile.custom_endpoints:
                managed_lines.append(
                    f"{prefix}CUSTOM_ENDPOINTS={_escape_dotenv_value(profile.custom_endpoints, quote=True)}"
                )
            if profile.max_output_tokens is not None:
                managed_lines.append(f"{prefix}MAX_OUTPUT_TOKENS={profile.max_output_tokens}")
            managed_lines.append(
                f"{prefix}API_KEY={_escape_dotenv_value(profile.api_key, quote=True)}"
            )
            managed_lines.append(f"{prefix}ENABLED={'true' if profile.enabled else 'false'}")
            if profile.selected_model:
                managed_lines.append(
                    f"{prefix}MODEL={_escape_dotenv_value(profile.selected_model, quote=True)}"
                )
            if profile.discovered_models:
                csv_models = ",".join(profile.discovered_models)
                managed_lines.append(
                    f"{prefix}DISCOVERED_MODELS={_escape_dotenv_value(csv_models, quote=True)}"
                )
            if profile.discovery_status and profile.discovery_status != "unprobed":
                managed_lines.append(
                    f"{prefix}DISCOVERY_STATUS={_escape_dotenv_value(profile.discovery_status, quote=True)}"
                )
            if profile.allow_remote_http:
                managed_lines.append(f"{prefix}ALLOW_REMOTE_HTTP=true")

        reasonings_map = role_reasonings or {}
        for role in CUSTOM_PROVIDER_ROLES:
            binding = role_bindings.get(role, "").strip()
            if binding:
                managed_lines.append(
                    f"KINAIYA_CUSTOM_ROLE_{role.upper()}={_escape_dotenv_value(binding, quote=True)}"
                )
            role_effort = reasonings_map.get(role, "").strip().lower()
            if role_effort in REASONING_EFFORT_VALUES:
                managed_lines.append(
                    f"KINAIYA_CUSTOM_ROLE_{role.upper()}_REASONING={_escape_dotenv_value(role_effort, quote=True)}"
                )

        if receipts:
            # Keep only the most recent MAX_OPERATION_RECEIPTS receipts
            items = list(receipts.items())
            if len(items) > MAX_OPERATION_RECEIPTS:
                items = items[-MAX_OPERATION_RECEIPTS:]
            bounded_receipts = dict(items)
            receipts_json = _canonical_json(bounded_receipts)
            managed_lines.append(
                f"KINAIYA_CUSTOM_PROVIDER_RECEIPTS_JSON={_escape_dotenv_value(receipts_json, quote=True)}"
            )

        all_lines = preserved_lines + ([""] if preserved_lines else []) + managed_lines
        serialized = "\n".join(all_lines) + "\n"
        self._write_protected_bytes(
            self._env_path,
            serialized.encode("utf-8"),
            max_bytes=MAX_ENV_FILE_BYTES,
        )

    @staticmethod
    def _references_for_profile(
        profile: _StoredCustomProfile,
        role_bindings: dict[str, str],
    ) -> list[str]:
        refs: list[str] = []
        prefix = f"{profile.profile_id}:"
        for role in CUSTOM_PROVIDER_ROLES:
            binding = role_bindings.get(role, "")
            if binding.startswith(prefix):
                refs.append(role)
        return refs

    def _build_snapshot_from_state(
        self,
        *,
        profiles: list[_StoredCustomProfile],
        role_bindings: dict[str, str],
        role_reasonings: dict[str, str],
        global_reasoning_effort: str,
        revision: int,
        reconciled: bool = False,
        client_operation_id: str | None = None,
    ) -> dict[str, Any]:
        profile_dicts = [
            p.to_public_dict(referenced_by=self._references_for_profile(p, role_bindings))
            for p in profiles
        ]
        by_id = {p.profile_id: p for p in profiles}
        resolved_roles: dict[str, dict[str, Any] | None] = {}
        for role in CUSTOM_PROVIDER_ROLES:
            binding = role_bindings.get(role, "")
            if ":" in binding:
                pid, model_id = binding.split(":", 1)
                role_override = role_reasonings.get(role)
                effective_effort = (
                    role_override
                    if role_override in REASONING_EFFORT_VALUES
                    else global_reasoning_effort
                )
                prof = by_id.get(pid)
                if prof is not None:
                    resolved_roles[role] = {
                        "profile_id": prof.profile_id,
                        "provider_name": prof.name,
                        "base_url": prof.base_url,
                        "enabled": prof.enabled,
                        "model_id": model_id,
                        "qualified_model_id": f"{prof.profile_id}:{model_id}",
                        "reasoning_effort": role_override,
                        "effective_reasoning_effort": effective_effort,
                        "status": "proposed_unavailable",
                        "active": False,
                        "unavailable_reason": (
                            "referenced_profile_disabled"
                            if not prof.enabled
                            else "native_role_runtime_not_wired"
                        ),
                        "diagnostic": PROPOSED_ROLE_DIAGNOSTIC,
                        "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
                    }
                    continue
                # Profile was removed with confirm_referenced=True: retain role reference
                # in explicit unavailable_needs_reselection state per Penpot r56.
                resolved_roles[role] = {
                    "profile_id": pid,
                    "provider_name": f"{pid} (removed)",
                    "base_url": "",
                    "enabled": False,
                    "model_id": model_id,
                    "qualified_model_id": f"{pid}:{model_id}",
                    "reasoning_effort": role_override,
                    "effective_reasoning_effort": effective_effort,
                    "status": "unavailable_needs_reselection",
                    "active": False,
                    "unavailable_reason": "referenced_profile_removed",
                    "needs_reselection": True,
                    "diagnostic": REMOVED_PROFILE_ROLE_DIAGNOSTIC,
                    "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
                }
                continue
            resolved_roles[role] = None

        state_sha256 = self._compute_state_sha256(
            revision=revision,
            global_reasoning_effort=global_reasoning_effort,
            profiles=profiles,
            role_bindings=role_bindings,
            role_reasonings=role_reasonings,
        )
        snap: dict[str, Any] = {
            "schema_version": 1,
            "env_file_name": self._env_path.name,
            "unlimited": True,
            "revision": revision,
            "state_sha256": state_sha256,
            "reconciled": bool(reconciled),
            "role_runtime_status": "proposed_unavailable",
            "role_runtime_diagnostic": PROPOSED_ROLE_DIAGNOSTIC,
            "global_reasoning_effort": global_reasoning_effort,
            "profiles": profile_dicts,
            "role_bindings": resolved_roles,
        }
        if client_operation_id is not None:
            snap["client_operation_id"] = client_operation_id
        return snap

    def snapshot(self) -> dict[str, Any]:
        """Return the public snapshot of custom providers and role bindings."""
        with self._exclusive_lock():
            state = self._load_state()
            return self._build_snapshot_from_state(
                profiles=state.profiles,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
                revision=state.revision,
            )

    def _check_or_replay_receipt(
        self,
        *,
        state: _LoadedState,
        op_id: str | None,
        action: str,
        request_sha256: str,
        expected_revision: int | None,
    ) -> dict[str, Any] | None:
        if op_id is not None and op_id in state.receipts:
            prior = state.receipts[op_id]
            if prior.get("action") != action or prior.get("request_sha256") != request_sha256:
                raise CustomProviderConflictError(
                    f"Operation ID {op_id!r} belongs to a different custom provider request."
                )
            stored_response = prior.get("response")
            if isinstance(stored_response, dict):
                replayed = json.loads(_canonical_json(stored_response))
                replayed["reconciled"] = True
                replayed["client_operation_id"] = op_id
                replayed["committed_revision"] = prior.get("revision", replayed.get("revision"))
                if isinstance(replayed.get("snapshot"), dict):
                    replayed["snapshot"]["reconciled"] = True
                    replayed["snapshot"]["client_operation_id"] = op_id
                return replayed
            snap = self._build_snapshot_from_state(
                profiles=state.profiles,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
                revision=state.revision,
                reconciled=True,
                client_operation_id=op_id,
            )
            snap["committed_revision"] = prior.get("revision", state.revision)
            return snap

        if expected_revision is not None and expected_revision != state.revision:
            raise CustomProviderConflictError(
                f"Custom provider revision conflict: expected_revision={expected_revision} "
                f"does not match current revision={state.revision}."
            )
        return None

    def _record_receipt(
        self,
        *,
        receipts: dict[str, dict[str, Any]],
        op_id: str | None,
        action: str,
        request_sha256: str,
        expected_revision: int | None,
        revision: int,
        state_sha256: str,
        target_profile_id: str | None,
        response: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        updated = dict(receipts)
        if op_id is None:
            return updated
        # Store compact copy without recursive receipt chains
        compact_response = json.loads(_canonical_json(response))
        updated[op_id] = {
            "client_operation_id": op_id,
            "action": action,
            "request_sha256": request_sha256,
            "expected_revision": expected_revision,
            "revision": revision,
            "state_sha256": state_sha256,
            "target_profile_id": target_profile_id,
            "response": compact_response,
        }
        if len(updated) > MAX_OPERATION_RECEIPTS:
            keys = list(updated.keys())[-MAX_OPERATION_RECEIPTS:]
            updated = {k: updated[k] for k in keys}
        return updated

    def reconcile(
        self,
        client_operation_id: str | None = None,
        *,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile an operation by its client_operation_id / idempotency_key."""
        op_id = _resolve_operation_id(
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )
        if op_id is None:
            raise CustomProviderValidationError(
                "client_operation_id is required to reconcile a custom provider operation."
            )
        with self._exclusive_lock():
            state = self._load_state()
            current_snap = self._build_snapshot_from_state(
                profiles=state.profiles,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
                revision=state.revision,
            )
            prior = state.receipts.get(op_id)
            if prior is None:
                return {
                    **current_snap,
                    "status": "not_found",
                    "found": False,
                    "reconciled": False,
                    "client_operation_id": op_id,
                    "operation_id": op_id,
                    "idempotency_key": op_id,
                    "current_revision": current_snap["revision"],
                    "current_state_sha256": current_snap["state_sha256"],
                    "snapshot": current_snap,
                }
            stored_resp = prior.get("response")
            base_resp = (
                json.loads(_canonical_json(stored_resp))
                if isinstance(stored_resp, dict)
                else dict(current_snap)
            )
            return {
                **base_resp,
                "status": "committed",
                "found": True,
                "reconciled": True,
                "client_operation_id": op_id,
                "operation_id": op_id,
                "idempotency_key": op_id,
                "action": prior.get("action"),
                "target_profile_id": prior.get("target_profile_id"),
                "committed_revision": prior.get("revision"),
                "committed_state_sha256": prior.get("state_sha256"),
                "current_revision": current_snap["revision"],
                "current_state_sha256": current_snap["state_sha256"],
                "result": base_resp,
                "snapshot": base_resp.get("snapshot", base_resp),
            }

    def set_global_reasoning_effort(
        self,
        reasoning_effort: str,
        *,
        expected_revision: int | None = None,
        client_operation_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        clean_effort = _validate_reasoning_effort(reasoning_effort, allow_empty=False)
        exp_rev = _validate_expected_revision(expected_revision)
        op_id = _resolve_operation_id(
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )
        req_sha = _compute_request_sha256(
            "set_global_reasoning_effort",
            {"reasoning_effort": clean_effort},
        )
        with self._exclusive_lock():
            state = self._load_state()
            replayed = self._check_or_replay_receipt(
                state=state,
                op_id=op_id,
                action="set_global_reasoning_effort",
                request_sha256=req_sha,
                expected_revision=exp_rev,
            )
            if replayed is not None:
                return replayed

            new_revision = state.revision + 1
            snap = self._build_snapshot_from_state(
                profiles=state.profiles,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=clean_effort,
                revision=new_revision,
                reconciled=False,
                client_operation_id=op_id,
            )
            updated_receipts = self._record_receipt(
                receipts=state.receipts,
                op_id=op_id,
                action="set_global_reasoning_effort",
                request_sha256=req_sha,
                expected_revision=exp_rev,
                revision=new_revision,
                state_sha256=snap["state_sha256"],
                target_profile_id=None,
                response=snap,
            )
            self._write_state(
                state.lines,
                state.profiles,
                state.role_bindings,
                state.max_seq,
                revision=new_revision,
                receipts=updated_receipts,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=clean_effort,
            )
            return snap

    def add_profile(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str = "",
        enabled: bool = True,
        selected_model: str = "",
        allow_remote_http: bool = False,
        chat_completions_path: str | None = None,
        models_path: str | None = None,
        custom_endpoints: str | None = None,
        max_output_tokens: int | None = None,
        expected_revision: int | None = None,
        client_operation_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _require_bool(enabled, "enabled")
        _require_bool(allow_remote_http, "allow_remote_http")
        _require_str(api_key, "API key")
        _require_str(selected_model, "Selected model")
        exp_rev = _validate_expected_revision(expected_revision)
        op_id = _resolve_operation_id(
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )

        clean_name = _validate_single_line_text(name, "Provider name", max_len=96)
        clean_url = normalize_openai_base_url(
            base_url,
            allow_remote_http=allow_remote_http,
        )
        clean_chat_path = _validate_endpoint_path(
            chat_completions_path,
            "Chat completions endpoint path",
            default_path=DEFAULT_CHAT_COMPLETIONS_PATH,
        )
        clean_models_path = _validate_endpoint_path(
            models_path,
            "Models endpoint path",
            default_path=DEFAULT_MODELS_PATH,
        )
        clean_custom_endpoints = _validate_custom_endpoints(custom_endpoints)
        clean_max_tokens = _validate_max_output_tokens(max_output_tokens)
        clean_key = ""
        if api_key.strip():
            clean_key = _validate_single_line_text(api_key, "API key", max_len=512)
        clean_model = _validate_model_id(selected_model) if selected_model.strip() else ""

        req_sha = _compute_request_sha256(
            "add_custom_provider",
            {
                "name": clean_name,
                "base_url": clean_url,
                "api_key": clean_key,
                "enabled": enabled,
                "selected_model": clean_model,
                "allow_remote_http": allow_remote_http,
                "chat_completions_path": clean_chat_path,
                "models_path": clean_models_path,
                "custom_endpoints": clean_custom_endpoints,
                "max_output_tokens": clean_max_tokens,
            },
        )

        with self._exclusive_lock():
            state = self._load_state()
            replayed = self._check_or_replay_receipt(
                state=state,
                op_id=op_id,
                action="add_custom_provider",
                request_sha256=req_sha,
                expected_revision=exp_rev,
            )
            if replayed is not None:
                return replayed

            next_seq = state.max_seq + 1
            new_id = f"p{next_seq:02d}"
            existing_ids = {p.profile_id for p in state.profiles}
            while new_id in existing_ids:
                next_seq += 1
                new_id = f"p{next_seq:02d}"

            created = _StoredCustomProfile(
                profile_id=new_id,
                name=clean_name,
                base_url=clean_url,
                api_key=clean_key,
                enabled=enabled,
                selected_model=clean_model,
                discovered_models=[clean_model] if clean_model else [],
                discovery_status="unprobed",
                allow_remote_http=allow_remote_http,
                chat_completions_path=clean_chat_path,
                models_path=clean_models_path,
                custom_endpoints=clean_custom_endpoints,
                max_output_tokens=clean_max_tokens,
            )
            profiles = [*state.profiles, created]
            new_revision = state.revision + 1
            snap = self._build_snapshot_from_state(
                profiles=profiles,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
                revision=new_revision,
                reconciled=False,
                client_operation_id=op_id,
            )
            snap["created_profile_id"] = new_id
            updated_receipts = self._record_receipt(
                receipts=state.receipts,
                op_id=op_id,
                action="add_custom_provider",
                request_sha256=req_sha,
                expected_revision=exp_rev,
                revision=new_revision,
                state_sha256=snap["state_sha256"],
                target_profile_id=new_id,
                response=snap,
            )
            self._write_state(
                state.lines,
                profiles,
                state.role_bindings,
                next_seq,
                revision=new_revision,
                receipts=updated_receipts,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
            )
            return snap

    def update_profile(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
        enabled: bool | None = None,
        selected_model: str | None = None,
        allow_remote_http: bool | None = None,
        chat_completions_path: str | None = None,
        models_path: str | None = None,
        custom_endpoints: str | None = None,
        max_output_tokens: int | None = None,
        clear_max_output_tokens: bool = False,
        expected_revision: int | None = None,
        client_operation_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _require_str(profile_id, "profile_id")
        _require_optional_str(name, "Provider name")
        _require_optional_str(base_url, "Base URL")
        _require_optional_str(api_key, "API key")
        _require_bool(clear_api_key, "clear_api_key")
        _require_optional_bool(enabled, "enabled")
        _require_optional_str(selected_model, "Selected model")
        _require_optional_bool(allow_remote_http, "allow_remote_http")
        _require_optional_str(chat_completions_path, "Chat completions endpoint path")
        _require_optional_str(models_path, "Models endpoint path")
        _require_optional_str(custom_endpoints, "Custom endpoints")
        _require_bool(clear_max_output_tokens, "clear_max_output_tokens")
        clean_max_tokens = _validate_max_output_tokens(max_output_tokens)
        exp_rev = _validate_expected_revision(expected_revision)
        op_id = _resolve_operation_id(
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )

        pid = profile_id.strip().lower()
        if not _PROFILE_ID_RE.match(pid):
            raise CustomProviderValidationError(f"Unknown custom provider ID: {profile_id!r}")

        clean_name = (
            _validate_single_line_text(name, "Provider name", max_len=96)
            if name is not None
            else None
        )
        clean_chat_path = (
            _validate_endpoint_path(
                chat_completions_path,
                "Chat completions endpoint path",
                default_path=DEFAULT_CHAT_COMPLETIONS_PATH,
            )
            if chat_completions_path is not None
            else None
        )
        clean_models_path = (
            _validate_endpoint_path(
                models_path,
                "Models endpoint path",
                default_path=DEFAULT_MODELS_PATH,
            )
            if models_path is not None
            else None
        )
        clean_custom_endpoints = (
            _validate_custom_endpoints(custom_endpoints) if custom_endpoints is not None else None
        )
        clean_model = (
            (_validate_model_id(selected_model) if selected_model.strip() else "")
            if selected_model is not None
            else None
        )
        clean_key: str | None = None
        if api_key is not None and api_key.strip() and api_key.strip() != MASKED_API_KEY_DISPLAY:
            clean_key = _validate_single_line_text(api_key, "API key", max_len=512)

        req_sha = _compute_request_sha256(
            "update_custom_provider",
            {
                "profile_id": pid,
                "name": clean_name,
                "base_url": base_url,
                "api_key": clean_key or "",
                "clear_api_key": clear_api_key,
                "enabled": enabled,
                "selected_model": clean_model,
                "allow_remote_http": allow_remote_http,
                "chat_completions_path": clean_chat_path,
                "models_path": clean_models_path,
                "custom_endpoints": clean_custom_endpoints,
                "max_output_tokens": clean_max_tokens,
                "clear_max_output_tokens": clear_max_output_tokens,
            },
        )

        with self._exclusive_lock():
            state = self._load_state()
            replayed = self._check_or_replay_receipt(
                state=state,
                op_id=op_id,
                action="update_custom_provider",
                request_sha256=req_sha,
                expected_revision=exp_rev,
            )
            if replayed is not None:
                return replayed

            target = next((p for p in state.profiles if p.profile_id == pid), None)
            if target is None:
                raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")

            if allow_remote_http is not None:
                target.allow_remote_http = allow_remote_http
            if clean_name is not None:
                target.name = clean_name
            if base_url is not None:
                target.base_url = normalize_openai_base_url(
                    base_url,
                    allow_remote_http=target.allow_remote_http,
                )
            elif allow_remote_http is False:
                # Re-validate existing base_url if allow_remote_http was turned off
                target.base_url = normalize_openai_base_url(
                    target.base_url,
                    allow_remote_http=False,
                )
            if clean_chat_path is not None:
                target.chat_completions_path = clean_chat_path
            if clean_models_path is not None:
                target.models_path = clean_models_path
            if clean_custom_endpoints is not None:
                target.custom_endpoints = clean_custom_endpoints
            if clear_max_output_tokens:
                target.max_output_tokens = None
            elif clean_max_tokens is not None:
                target.max_output_tokens = clean_max_tokens
            if clear_api_key:
                target.api_key = ""
            elif clean_key is not None:
                target.api_key = clean_key
            if enabled is not None:
                target.enabled = enabled
            if clean_model is not None:
                target.selected_model = clean_model
                if clean_model and clean_model not in (target.discovered_models or []):
                    target.discovered_models = [*(target.discovered_models or []), clean_model]

            new_revision = state.revision + 1
            snap = self._build_snapshot_from_state(
                profiles=state.profiles,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
                revision=new_revision,
                reconciled=False,
                client_operation_id=op_id,
            )
            updated_receipts = self._record_receipt(
                receipts=state.receipts,
                op_id=op_id,
                action="update_custom_provider",
                request_sha256=req_sha,
                expected_revision=exp_rev,
                revision=new_revision,
                state_sha256=snap["state_sha256"],
                target_profile_id=pid,
                response=snap,
            )
            self._write_state(
                state.lines,
                state.profiles,
                state.role_bindings,
                state.max_seq,
                revision=new_revision,
                receipts=updated_receipts,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
            )
            return snap

    def set_enabled(
        self,
        profile_id: str,
        *,
        enabled: bool,
        expected_revision: int | None = None,
        client_operation_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _require_str(profile_id, "profile_id")
        _require_bool(enabled, "enabled")
        return self.update_profile(
            profile_id,
            enabled=enabled,
            expected_revision=expected_revision,
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )

    def bind_role_model(
        self,
        *,
        role: str,
        profile_id: str | None,
        model_id: str | None,
        reasoning_effort: str | None = None,
        expected_revision: int | None = None,
        client_operation_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _require_str(role, "role")
        _require_optional_str(profile_id, "profile_id")
        _require_optional_str(model_id, "model_id")
        _require_optional_str(reasoning_effort, "reasoning_effort")
        exp_rev = _validate_expected_revision(expected_revision)
        op_id = _resolve_operation_id(
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )

        clean_role = role.strip().lower()
        if clean_role not in CUSTOM_PROVIDER_ROLES:
            raise CustomProviderValidationError(f"Unsupported role: {role!r}")

        clean_role_effort: str | None = None
        if reasoning_effort is not None:
            clean_role_effort = _validate_reasoning_effort(reasoning_effort, allow_empty=True)

        clearing = not profile_id or not profile_id.strip() or not model_id or not model_id.strip()
        pid = profile_id.strip().lower() if profile_id and profile_id.strip() else ""
        clean_model = _validate_model_id(model_id) if not clearing and model_id else ""

        req_sha = _compute_request_sha256(
            "bind_custom_role_model",
            {
                "role": clean_role,
                "profile_id": pid if not clearing else None,
                "model_id": clean_model if not clearing else None,
                "reasoning_effort": clean_role_effort,
            },
        )

        with self._exclusive_lock():
            state = self._load_state()
            replayed = self._check_or_replay_receipt(
                state=state,
                op_id=op_id,
                action="bind_custom_role_model",
                request_sha256=req_sha,
                expected_revision=exp_rev,
            )
            if replayed is not None:
                return replayed

            if clearing:
                state.role_bindings.pop(clean_role, None)
                state.role_reasonings.pop(clean_role, None)
            else:
                target = next((p for p in state.profiles if p.profile_id == pid), None)
                if target is None:
                    raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")
                if not target.enabled:
                    raise CustomProviderError(
                        f"Custom provider {target.name!r} ({pid}) is disabled and cannot be bound to {clean_role}."
                    )
                target.selected_model = clean_model
                if clean_model not in (target.discovered_models or []):
                    target.discovered_models = [*(target.discovered_models or []), clean_model]
                state.role_bindings[clean_role] = f"{pid}:{clean_model}"
                if clean_role_effort is not None:
                    if clean_role_effort:
                        state.role_reasonings[clean_role] = clean_role_effort
                    else:
                        state.role_reasonings.pop(clean_role, None)

            new_revision = state.revision + 1
            snap = self._build_snapshot_from_state(
                profiles=state.profiles,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
                revision=new_revision,
                reconciled=False,
                client_operation_id=op_id,
            )
            updated_receipts = self._record_receipt(
                receipts=state.receipts,
                op_id=op_id,
                action="bind_custom_role_model",
                request_sha256=req_sha,
                expected_revision=exp_rev,
                revision=new_revision,
                state_sha256=snap["state_sha256"],
                target_profile_id=pid or None,
                response=snap,
            )
            self._write_state(
                state.lines,
                state.profiles,
                state.role_bindings,
                state.max_seq,
                revision=new_revision,
                receipts=updated_receipts,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
            )
            return snap

    def remove_profile(
        self,
        profile_id: str,
        *,
        confirm_referenced: bool = False,
        expected_revision: int | None = None,
        client_operation_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _require_str(profile_id, "profile_id")
        _require_bool(confirm_referenced, "confirm_referenced")
        exp_rev = _validate_expected_revision(expected_revision)
        op_id = _resolve_operation_id(
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )

        pid = profile_id.strip().lower()
        if not _PROFILE_ID_RE.match(pid):
            raise CustomProviderValidationError(f"Unknown custom provider ID: {profile_id!r}")

        req_sha = _compute_request_sha256(
            "remove_custom_provider",
            {"profile_id": pid, "confirm_referenced": confirm_referenced},
        )

        with self._exclusive_lock():
            state = self._load_state()
            replayed = self._check_or_replay_receipt(
                state=state,
                op_id=op_id,
                action="remove_custom_provider",
                request_sha256=req_sha,
                expected_revision=exp_rev,
            )
            if replayed is not None:
                return replayed

            target = next((p for p in state.profiles if p.profile_id == pid), None)
            if target is None:
                raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")

            refs = self._references_for_profile(target, state.role_bindings)
            if refs and not confirm_referenced:
                raise CustomProviderReferencedError(pid, refs)

            remaining = [p for p in state.profiles if p.profile_id != pid]
            # Penpot r56: do NOT delete referencing KINAIYA_CUSTOM_ROLE_<ROLE> entries!
            # Retain the role reference so snapshot() reports status="unavailable_needs_reselection"
            # with unavailable_reason="referenced_profile_removed".

            new_revision = state.revision + 1
            snap = self._build_snapshot_from_state(
                profiles=remaining,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
                revision=new_revision,
                reconciled=False,
                client_operation_id=op_id,
            )
            updated_receipts = self._record_receipt(
                receipts=state.receipts,
                op_id=op_id,
                action="remove_custom_provider",
                request_sha256=req_sha,
                expected_revision=exp_rev,
                revision=new_revision,
                state_sha256=snap["state_sha256"],
                target_profile_id=pid,
                response=snap,
            )
            self._write_state(
                state.lines,
                remaining,
                state.role_bindings,
                state.max_seq,
                revision=new_revision,
                receipts=updated_receipts,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
            )
            return snap

    def _prepare_discovery(
        self,
        profile_id: str,
        *,
        expected_revision: int | None,
        op_id: str | None,
        req_sha: str,
    ) -> tuple[_DiscoveryFingerprint, dict[str, str], dict[str, Any] | None]:
        pid = profile_id.strip().lower()
        if not _PROFILE_ID_RE.match(pid):
            raise CustomProviderValidationError(f"Unknown custom provider ID: {profile_id!r}")

        with self._exclusive_lock():
            state = self._load_state()
            replayed = self._check_or_replay_receipt(
                state=state,
                op_id=op_id,
                action="discover_custom_models",
                request_sha256=req_sha,
                expected_revision=expected_revision,
            )
            if replayed is not None:
                return (
                    _DiscoveryFingerprint(
                        profile_id=pid,
                        provider_name="",
                        base_url="",
                        models_path=DEFAULT_MODELS_PATH,
                        models_url="",
                        api_key_sha256="",
                        enabled=False,
                        allow_remote_http=False,
                        pre_revision=state.revision,
                    ),
                    {},
                    replayed,
                )

            target = next((p for p in state.profiles if p.profile_id == pid), None)
            if target is None:
                raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")
            # Refuse disabled profiles BEFORE touching or disclosing any credential!
            if not target.enabled:
                raise CustomProviderError(
                    f"Custom provider {target.name!r} ({pid}) is disabled; enable it before model discovery."
                )

            normalized_base = normalize_openai_base_url(
                target.base_url,
                allow_remote_http=target.allow_remote_http,
            )
            models_path = target.models_path or DEFAULT_MODELS_PATH
            models_url = _resolve_endpoint_url(normalized_base, models_path)
            headers: dict[str, str] = {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
            if target.api_key:
                headers["Authorization"] = f"Bearer {target.api_key}"

            fingerprint = _DiscoveryFingerprint(
                profile_id=target.profile_id,
                provider_name=target.name,
                base_url=normalized_base,
                models_path=models_path,
                models_url=models_url,
                api_key_sha256=target.api_key_sha256,
                enabled=bool(target.enabled),
                allow_remote_http=bool(target.allow_remote_http),
                pre_revision=state.revision,
            )
            return fingerprint, headers, None

    def _commit_discovery_outcome(
        self,
        *,
        fingerprint: _DiscoveryFingerprint,
        outcome: _RawDiscoveryOutcome,
        expected_revision: int | None,
        op_id: str | None,
        req_sha: str,
    ) -> dict[str, Any]:
        pid = fingerprint.profile_id
        with self._exclusive_lock():
            state = self._load_state()
            replayed = self._check_or_replay_receipt(
                state=state,
                op_id=op_id,
                action="discover_custom_models",
                request_sha256=req_sha,
                expected_revision=expected_revision,
            )
            if replayed is not None:
                return replayed

            current_target = next((p for p in state.profiles if p.profile_id == pid), None)
            if current_target is None:
                raise CustomProviderStaleDiscoveryError(
                    pid, "profile was removed while model discovery was in flight."
                )
            if not current_target.enabled:
                raise CustomProviderStaleDiscoveryError(
                    pid, "profile was disabled while model discovery was in flight."
                )
            current_norm_base = normalize_openai_base_url(
                current_target.base_url,
                allow_remote_http=current_target.allow_remote_http,
            )
            current_models_path = current_target.models_path or DEFAULT_MODELS_PATH
            if (
                current_norm_base != fingerprint.base_url
                or current_models_path != fingerprint.models_path
                or bool(current_target.allow_remote_http) != fingerprint.allow_remote_http
            ):
                raise CustomProviderStaleDiscoveryError(
                    pid,
                    "profile endpoint configuration changed while model discovery was in flight.",
                )
            if current_target.api_key_sha256 != fingerprint.api_key_sha256:
                raise CustomProviderStaleDiscoveryError(
                    pid, "profile API key changed while model discovery was in flight."
                )

            current_target.discovery_status = outcome.status
            if outcome.status in ("ok", "empty"):
                current_target.discovered_models = list(outcome.discovered_models)
                if outcome.discovered_models and not current_target.selected_model:
                    current_target.selected_model = outcome.discovered_models[0]

            new_revision = state.revision + 1
            snap = self._build_snapshot_from_state(
                profiles=state.profiles,
                role_bindings=state.role_bindings,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
                revision=new_revision,
                reconciled=False,
                client_operation_id=op_id,
            )
            response = self._format_discovery_response(
                target=current_target,
                models_url=fingerprint.models_url,
                outcome=outcome,
                snapshot=snap,
                reconciled=False,
                client_operation_id=op_id,
            )
            updated_receipts = self._record_receipt(
                receipts=state.receipts,
                op_id=op_id,
                action="discover_custom_models",
                request_sha256=req_sha,
                expected_revision=expected_revision,
                revision=new_revision,
                state_sha256=snap["state_sha256"],
                target_profile_id=pid,
                response=response,
            )
            self._write_state(
                state.lines,
                state.profiles,
                state.role_bindings,
                state.max_seq,
                revision=new_revision,
                receipts=updated_receipts,
                role_reasonings=state.role_reasonings,
                global_reasoning_effort=state.global_reasoning_effort,
            )
            return response

    @staticmethod
    def _format_discovery_response(
        *,
        target: _StoredCustomProfile,
        models_url: str,
        outcome: _RawDiscoveryOutcome,
        snapshot: dict[str, Any],
        reconciled: bool = False,
        client_operation_id: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "profile_id": target.profile_id,
            "provider_name": target.name,
            "models_url": models_url,
            "status": outcome.status,
            "http_status": outcome.http_status,
            "models": [
                {
                    "model_id": mid,
                    "qualified_model_id": f"{target.profile_id}:{mid}",
                    "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
                }
                for mid in outcome.discovered_models
            ],
            "manual_fallback_allowed": True,
            "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
            "message": outcome.message,
            "revision": snapshot["revision"],
            "state_sha256": snapshot["state_sha256"],
            "reconciled": bool(reconciled),
            "snapshot": snapshot,
        }
        if client_operation_id is not None:
            result["client_operation_id"] = client_operation_id
        return result

    def _execute_sync_http_discovery(
        self,
        *,
        models_url: str,
        headers: dict[str, str],
        timeout_s: float,
    ) -> _RawDiscoveryOutcome:
        req = urllib.request.Request(models_url, headers=headers, method="GET")
        opener = self._http_opener
        if opener is None:
            built_opener = urllib.request.build_opener(_NoRedirectHandler())
            opener = built_opener.open

        try:
            with opener(req, timeout=timeout_s) as resp:
                status_code = int(getattr(resp, "status", 200))
                if status_code != 200:
                    return _classify_http_status(status_code, models_url)
                raw_body = resp.read(MAX_DISCOVERY_RESPONSE_BYTES + 1)
                if len(raw_body) > MAX_DISCOVERY_RESPONSE_BYTES:
                    return _RawDiscoveryOutcome(
                        status="invalid_response",
                        http_status=status_code,
                        discovered_models=[],
                        message=(
                            f"Endpoint {models_url} response exceeded maximum size "
                            f"({MAX_DISCOVERY_RESPONSE_BYTES} bytes)."
                        ),
                    )
        except urllib.error.HTTPError as exc:
            return _classify_http_status(exc.code, models_url)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", str(exc))
            return _RawDiscoveryOutcome(
                status="connection_error",
                http_status=None,
                discovered_models=[],
                message=(
                    f"Could not reach {models_url} ({reason}). "
                    "Verify the server is running or enter a manual model ID."
                ),
            )

        return _parse_bounded_models_json(raw_body, models_url)

    async def _execute_async_http_discovery(
        self,
        *,
        models_url: str,
        headers: dict[str, str],
        timeout_s: float,
    ) -> _RawDiscoveryOutcome:
        if (
            self._http_opener is not None
            and self._async_transport is None
            and self._async_client_factory is None
        ):
            if inspect.iscoroutinefunction(self._http_opener):
                async with asyncio.timeout(timeout_s):
                    return await self._http_opener(models_url, headers, timeout_s)
            async with asyncio.timeout(timeout_s):
                return await asyncio.to_thread(
                    self._execute_sync_http_discovery,
                    models_url=models_url,
                    headers=headers,
                    timeout_s=timeout_s,
                )

        client: httpx.AsyncClient | None = None
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(timeout_s):
                if self._async_client_factory is not None:
                    client = self._async_client_factory()
                else:
                    client = httpx.AsyncClient(
                        follow_redirects=False,
                        timeout=httpx.Timeout(timeout_s),
                        limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
                        transport=self._async_transport,
                    )
                request = client.build_request("GET", models_url, headers=headers)
                response = await client.send(request, stream=True)
                status_code = response.status_code
                if status_code != 200:
                    return _classify_http_status(status_code, models_url)

                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_DISCOVERY_RESPONSE_BYTES:
                            return _RawDiscoveryOutcome(
                                status="invalid_response",
                                http_status=status_code,
                                discovered_models=[],
                                message=(
                                    f"Endpoint {models_url} response exceeded maximum size "
                                    f"({MAX_DISCOVERY_RESPONSE_BYTES} bytes)."
                                ),
                            )
                    except ValueError:
                        pass

                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    if len(buf) + len(chunk) > MAX_DISCOVERY_RESPONSE_BYTES:
                        return _RawDiscoveryOutcome(
                            status="invalid_response",
                            http_status=status_code,
                            discovered_models=[],
                            message=(
                                f"Endpoint {models_url} response exceeded maximum size "
                                f"({MAX_DISCOVERY_RESPONSE_BYTES} bytes)."
                            ),
                        )
                    buf.extend(chunk)
                raw_body = bytes(buf)
        except (TimeoutError, httpx.TimeoutException):
            return _RawDiscoveryOutcome(
                status="connection_error",
                http_status=None,
                discovered_models=[],
                message=(
                    f"Timed out after {timeout_s:.1f}s when querying {models_url}. "
                    "Verify the server is responsive or enter a manual model ID."
                ),
            )
        except httpx.HTTPError as exc:
            return _RawDiscoveryOutcome(
                status="connection_error",
                http_status=None,
                discovered_models=[],
                message=(
                    f"Could not reach {models_url} ({exc}). "
                    "Verify the server is running or enter a manual model ID."
                ),
            )
        finally:
            try:
                await _shielded_aclose(response)
            finally:
                await _shielded_aclose(client)

        return _parse_bounded_models_json(raw_body, models_url)

    def discover_models(
        self,
        profile_id: str,
        *,
        timeout_s: float | None = None,
        expected_revision: int | None = None,
        client_operation_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Discover models synchronously while releasing the file lock during network I/O."""
        _require_str(profile_id, "profile_id")
        exp_rev = _validate_expected_revision(expected_revision)
        op_id = _resolve_operation_id(
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )
        pid = profile_id.strip().lower()
        req_sha = _compute_request_sha256("discover_custom_models", {"profile_id": pid})

        fingerprint, headers, replayed = self._prepare_discovery(
            profile_id,
            expected_revision=exp_rev,
            op_id=op_id,
            req_sha=req_sha,
        )
        if replayed is not None:
            return replayed

        effective_timeout = self._discovery_timeout_s if timeout_s is None else float(timeout_s)
        if self._http_opener is None and (
            self._async_transport is not None or self._async_client_factory is not None
        ):
            outcome = asyncio.run(
                self._execute_async_http_discovery(
                    models_url=fingerprint.models_url,
                    headers=headers,
                    timeout_s=effective_timeout,
                )
            )
        else:
            outcome = self._execute_sync_http_discovery(
                models_url=fingerprint.models_url,
                headers=headers,
                timeout_s=effective_timeout,
            )

        return self._commit_discovery_outcome(
            fingerprint=fingerprint,
            outcome=outcome,
            expected_revision=exp_rev,
            op_id=op_id,
            req_sha=req_sha,
        )

    async def discover_models_async(
        self,
        profile_id: str,
        *,
        timeout_s: float | None = None,
        expected_revision: int | None = None,
        client_operation_id: str | None = None,
        operation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Discover models asynchronously off the file lock with owned transport cleanup and CAS commit."""
        _require_str(profile_id, "profile_id")
        exp_rev = _validate_expected_revision(expected_revision)
        op_id = _resolve_operation_id(
            client_operation_id=client_operation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
        )
        pid = profile_id.strip().lower()
        req_sha = _compute_request_sha256("discover_custom_models", {"profile_id": pid})

        fingerprint, headers, replayed = self._prepare_discovery(
            profile_id,
            expected_revision=exp_rev,
            op_id=op_id,
            req_sha=req_sha,
        )
        if replayed is not None:
            return replayed

        effective_timeout = self._discovery_timeout_s if timeout_s is None else float(timeout_s)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_discoveries.add(current_task)
        try:
            outcome = await self._execute_async_http_discovery(
                models_url=fingerprint.models_url,
                headers=headers,
                timeout_s=effective_timeout,
            )
        finally:
            if current_task is not None:
                self._active_discoveries.discard(current_task)

        return self._commit_discovery_outcome(
            fingerprint=fingerprint,
            outcome=outcome,
            expected_revision=exp_rev,
            op_id=op_id,
            req_sha=req_sha,
        )

    @staticmethod
    def _validate_action_envelope(payload: Any) -> tuple[str, int | None, str | None]:
        if type(payload) is not dict:
            raise CustomProviderValidationError("Request body must be a JSON object.")
        if any(type(k) is not str for k in payload):
            raise CustomProviderValidationError("Request field names must be strings.")
        raw_action = payload.get("action")
        if type(raw_action) is not str or not raw_action.strip():
            raise CustomProviderValidationError("Request action must be a non-empty string.")
        canonical = _ACTION_ALIASES.get(raw_action.strip())
        if canonical is None:
            raise CustomProviderValidationError(
                f"Unsupported custom provider action: {raw_action!r}"
            )
        allowed = _ACTION_ALLOWED_KEYS[canonical]
        unknown = set(payload.keys()) - allowed
        if unknown:
            raise CustomProviderValidationError(
                f"Unknown or unsupported field(s) for {canonical}: {', '.join(sorted(unknown))}"
            )
        required = _ACTION_REQUIRED_KEYS[canonical]
        missing = [k for k in sorted(required) if k not in payload or payload[k] is None]
        if missing:
            raise CustomProviderValidationError(
                f"Missing required field(s) for {canonical}: {', '.join(missing)}"
            )
        exp_rev = _validate_expected_revision(payload.get("expected_revision"))
        op_id = _resolve_operation_id(
            client_operation_id=payload.get("client_operation_id"),
            operation_id=payload.get("operation_id"),
            idempotency_key=payload.get("idempotency_key"),
        )
        return canonical, exp_rev, op_id

    def _dispatch_validated_sync_action(
        self,
        canonical: str,
        payload: dict[str, Any],
        exp_rev: int | None,
        op_id: str | None,
    ) -> dict[str, Any]:
        if canonical == "add_custom_provider":
            return self.add_profile(
                name=_require_str(payload["name"], "name"),
                base_url=_require_str(payload["base_url"], "base_url"),
                api_key=_require_str(payload.get("api_key", ""), "api_key"),
                enabled=_require_bool(payload.get("enabled", True), "enabled"),
                selected_model=_require_str(payload.get("selected_model", ""), "selected_model"),
                allow_remote_http=_require_bool(
                    payload.get("allow_remote_http", False), "allow_remote_http"
                ),
                chat_completions_path=_require_optional_str(
                    payload.get("chat_completions_path"), "chat_completions_path"
                ),
                models_path=_require_optional_str(payload.get("models_path"), "models_path"),
                custom_endpoints=_require_optional_str(
                    payload.get("custom_endpoints"), "custom_endpoints"
                ),
                max_output_tokens=_validate_max_output_tokens(payload.get("max_output_tokens")),
                expected_revision=exp_rev,
                client_operation_id=op_id,
            )
        if canonical == "update_custom_provider":
            max_tokens_present = "max_output_tokens" in payload
            raw_max_tokens = payload.get("max_output_tokens")
            validated_max_tokens = _validate_max_output_tokens(raw_max_tokens)
            explicit_clear_max = _require_bool(
                payload.get("clear_max_output_tokens", False), "clear_max_output_tokens"
            )
            return self.update_profile(
                _require_str(payload["profile_id"], "profile_id"),
                name=_require_optional_str(payload.get("name"), "name"),
                base_url=_require_optional_str(payload.get("base_url"), "base_url"),
                api_key=_require_optional_str(payload.get("api_key"), "api_key"),
                clear_api_key=_require_bool(payload.get("clear_api_key", False), "clear_api_key"),
                enabled=_require_optional_bool(payload.get("enabled"), "enabled"),
                selected_model=_require_optional_str(
                    payload.get("selected_model"), "selected_model"
                ),
                allow_remote_http=_require_optional_bool(
                    payload.get("allow_remote_http"), "allow_remote_http"
                ),
                chat_completions_path=_require_optional_str(
                    payload.get("chat_completions_path"), "chat_completions_path"
                ),
                models_path=_require_optional_str(payload.get("models_path"), "models_path"),
                custom_endpoints=_require_optional_str(
                    payload.get("custom_endpoints"), "custom_endpoints"
                ),
                max_output_tokens=validated_max_tokens,
                clear_max_output_tokens=bool(
                    explicit_clear_max or (max_tokens_present and validated_max_tokens is None)
                ),
                expected_revision=exp_rev,
                client_operation_id=op_id,
            )
        if canonical == "set_custom_provider_enabled":
            return self.set_enabled(
                _require_str(payload["profile_id"], "profile_id"),
                enabled=_require_bool(payload["enabled"], "enabled"),
                expected_revision=exp_rev,
                client_operation_id=op_id,
            )
        if canonical == "remove_custom_provider":
            return self.remove_profile(
                _require_str(payload["profile_id"], "profile_id"),
                confirm_referenced=_require_bool(
                    payload.get("confirm_referenced", False), "confirm_referenced"
                ),
                expected_revision=exp_rev,
                client_operation_id=op_id,
            )
        if canonical == "bind_custom_role_model":
            return self.bind_role_model(
                role=_require_str(payload["role"], "role"),
                profile_id=_require_optional_str(payload.get("profile_id"), "profile_id"),
                model_id=_require_optional_str(payload.get("model_id"), "model_id"),
                reasoning_effort=_require_optional_str(
                    payload.get("reasoning_effort"), "reasoning_effort"
                ),
                expected_revision=exp_rev,
                client_operation_id=op_id,
            )
        if canonical == "set_global_reasoning_effort":
            return self.set_global_reasoning_effort(
                _require_str(payload["reasoning_effort"], "reasoning_effort"),
                expected_revision=exp_rev,
                client_operation_id=op_id,
            )
        if canonical == "reconcile_custom_provider_operation":
            return self.reconcile(client_operation_id=op_id)
        raise CustomProviderValidationError(f"Unsupported custom provider action: {canonical!r}")

    def apply_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and dispatch a custom provider action synchronously."""
        canonical, exp_rev, op_id = self._validate_action_envelope(payload)
        if canonical == "discover_custom_models":
            return self.discover_models(
                _require_str(payload["profile_id"], "profile_id"),
                expected_revision=exp_rev,
                client_operation_id=op_id,
            )
        return self._dispatch_validated_sync_action(canonical, payload, exp_rev, op_id)

    async def apply_action_async(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and dispatch a custom provider action asynchronously."""
        canonical, exp_rev, op_id = self._validate_action_envelope(payload)
        if canonical == "discover_custom_models":
            return await self.discover_models_async(
                _require_str(payload["profile_id"], "profile_id"),
                expected_revision=exp_rev,
                client_operation_id=op_id,
            )
        return self._dispatch_validated_sync_action(canonical, payload, exp_rev, op_id)
