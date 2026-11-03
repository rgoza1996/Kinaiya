"""User-local .env persistence and /models discovery for custom OpenAI-compatible APIs."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

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

_PROFILE_ID_RE = re.compile(r"^p([0-9]{2,6})$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,160}$")
_ENDPOINT_PATH_RE = re.compile(r"^/[A-Za-z0-9._:/@-]{1,159}$")

UNVERIFIED_CUSTOM_CAPABILITIES: dict[str, Any] = {
    "chat": "unverified",
    "streaming": "unverified",
    "reasoning": "unverified",
    "multimodal": "unverified",
    "tool_use": "unverified",
    "agent_qualified": False,
}


class CustomProviderError(ValueError):
    """Raised when a custom provider operation fails validation."""


class CustomProviderReferencedError(CustomProviderError):
    """Raised when removing a custom provider that is still referenced without confirmation."""

    def __init__(self, profile_id: str, referenced_by: list[str]) -> None:
        super().__init__(
            f"Custom provider {profile_id!r} is referenced by {', '.join(referenced_by)}; "
            "confirmation is required before removal."
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
    if not isinstance(base_url, str) or not base_url.strip():
        raise CustomProviderError("Base URL is required.")
    raw = base_url.strip()
    if any(ch in raw for ch in ("\r", "\n", "\x00", " ")):
        raise CustomProviderError("Base URL must not contain whitespace or control characters.")
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise CustomProviderError("Base URL scheme must be https or loopback http.")
    if parsed.username or parsed.password:
        raise CustomProviderError("Base URL must not embed credentials.")
    if not parsed.hostname:
        raise CustomProviderError("Base URL must include a valid host.")
    if parsed.query or parsed.fragment:
        raise CustomProviderError("Base URL must not include query parameters or fragments.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CustomProviderError("Base URL port is invalid.") from exc

    is_loopback = _is_loopback_host(parsed.hostname)
    if scheme == "http" and not is_loopback and not allow_remote_http:
        raise CustomProviderError(
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
    if not isinstance(value, str):
        raise CustomProviderError(f"{field_name} must be a string.")
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise CustomProviderError(f"{field_name} must not contain newlines or null bytes.")
    cleaned = value.strip()
    if not cleaned:
        raise CustomProviderError(f"{field_name} must not be empty.")
    if len(cleaned) > max_len:
        raise CustomProviderError(f"{field_name} exceeds maximum length ({max_len}).")
    return cleaned


def _validate_model_id(model_id: str) -> str:
    cleaned = _validate_single_line_text(model_id, "Model ID", max_len=160)
    if not _MODEL_ID_RE.match(cleaned):
        raise CustomProviderError(
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
    if not isinstance(path_value, str):
        raise CustomProviderError(f"{field_name} must be a string.")
    if any(ch in path_value for ch in ("\r", "\n", "\x00", " ")):
        raise CustomProviderError(f"{field_name} must not contain whitespace or control characters.")
    cleaned = path_value.strip()
    if not cleaned:
        return default_path
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    while len(cleaned) > 1 and cleaned.endswith("/"):
        cleaned = cleaned[:-1]
    if ".." in cleaned or not _ENDPOINT_PATH_RE.match(cleaned):
        raise CustomProviderError(
            f"{field_name} must be a valid relative endpoint path starting with '/' (e.g. {default_path!r})."
        )
    return cleaned


def _validate_custom_endpoints(raw_value: str | None) -> str:
    """Validate optional comma-separated custom endpoint paths (e.g. '/v1/systemone' for llama.cpp)."""
    if raw_value is None:
        return ""
    if not isinstance(raw_value, str):
        raise CustomProviderError("Custom endpoints must be a string.")
    if any(ch in raw_value for ch in ("\r", "\n", "\x00")):
        raise CustomProviderError("Custom endpoints must not contain newlines or null bytes.")
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
        raise CustomProviderError("Custom endpoints exceed maximum length (256).")
    return joined


def _validate_max_output_tokens(value: Any) -> int | None:
    """Validate optional max_output_tokens; blank or None leaves maximum output tokens up to the model."""
    if value is None or isinstance(value, bool):
        if isinstance(value, bool):
            raise CustomProviderError("Maximum output tokens must be a positive integer or blank.")
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            parsed = int(cleaned)
        except ValueError as exc:
            raise CustomProviderError(
                "Maximum output tokens must be a positive integer or left blank for model default."
            ) from exc
    elif isinstance(value, int):
        parsed = value
    else:
        raise CustomProviderError(
            "Maximum output tokens must be a positive integer or left blank for model default."
        )
    if parsed < 1 or parsed > 2_000_000:
        raise CustomProviderError(
            "Maximum output tokens must be between 1 and 2,000,000 (or blank for model default)."
        )
    return parsed


def _validate_reasoning_effort(value: str | None, *, allow_empty: bool = False) -> str:
    if value is None:
        return "" if allow_empty else DEFAULT_GLOBAL_REASONING_EFFORT
    if not isinstance(value, str):
        raise CustomProviderError("Reasoning effort must be a string.")
    cleaned = value.strip().lower()
    if allow_empty and cleaned in ("", "global", "default"):
        return ""
    if cleaned not in REASONING_EFFORT_VALUES:
        allowed = ", ".join(REASONING_EFFORT_VALUES)
        raise CustomProviderError(f"Reasoning effort must be one of: {allowed}.")
    return cleaned


def _resolve_endpoint_url(normalized_base: str, endpoint_path: str) -> str:
    """Resolve an endpoint path against a normalized OpenAI base URL without duplicating /v1."""
    clean_base = normalized_base.rstrip("/")
    clean_path = endpoint_path.strip() if endpoint_path and endpoint_path.strip() else DEFAULT_MODELS_PATH
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    if clean_base.lower().endswith("/v1") and clean_path.lower().startswith("/v1/"):
        clean_path = clean_path[3:]
    elif clean_base.lower().endswith("/v1") and clean_path.lower() == "/v1":
        clean_path = ""
    return f"{clean_base}{clean_path}"


def _escape_dotenv_value(value: str, *, quote: bool = True) -> str:
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise CustomProviderError("Environment value must not contain newlines or null bytes.")
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
        "KINAIYA_GLOBAL_REASONING_EFFORT",
    ):
        return True
    if key.startswith("KINAIYA_PROVIDER_P") and re.match(
        r"^KINAIYA_PROVIDER_P[0-9]{2,6}_(NAME|BASE_URL|API_KEY|ENABLED|MODEL|DISCOVERED_MODELS|ALLOW_REMOTE_HTTP|CHAT_PATH|MODELS_PATH|CUSTOM_ENDPOINTS|MAX_OUTPUT_TOKENS)$",
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
    allow_remote_http: bool = False
    chat_completions_path: str = DEFAULT_CHAT_COMPLETIONS_PATH
    models_path: str = DEFAULT_MODELS_PATH
    custom_endpoints: str = ""
    max_output_tokens: int | None = None

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
            "allow_remote_http": bool(self.allow_remote_http),
            "referenced_by": list(referenced_by),
            "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
        }


class CustomProvidersEnvStore:
    """User-local .env store for unlimited custom OpenAI-compatible provider profiles."""

    def __init__(
        self,
        env_path: Path,
        *,
        workspace: Path | None = None,
        http_opener: Callable[..., Any] | None = None,
    ) -> None:
        abs_env = Path(os.path.abspath(env_path))
        if abs_env.is_symlink() or abs_env != abs_env.resolve():
            raise CustomProviderError("Custom provider .env path must not contain symlinks.")
        self._env_path = abs_env
        self._workspace = Path(workspace).resolve() if workspace is not None else abs_env.parent
        self._http_opener = http_opener

    @property
    def env_path(self) -> Path:
        return self._env_path

    def _read_raw_env(self) -> tuple[list[_EnvLine], dict[str, str]]:
        if not self._env_path.exists():
            return [], {}
        if self._env_path.is_symlink():
            raise CustomProviderError("Custom provider .env path must not be a symlink.")
        content = self._env_path.read_text(encoding="utf-8")
        lines = _parse_dotenv_lines(content)
        env_map: dict[str, str] = {}
        for item in lines:
            if item.key is not None and item.value is not None:
                env_map[item.key] = item.value
        return lines, env_map

    def _load_state(
        self,
    ) -> tuple[
        list[_EnvLine],
        list[_StoredCustomProfile],
        dict[str, str],
        dict[str, str],
        str,
        int,
    ]:
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

        raw_global_effort = env_map.get(
            "KINAIYA_GLOBAL_REASONING_EFFORT", DEFAULT_GLOBAL_REASONING_EFFORT
        ).strip().lower()
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
            discovered_models = [
                m.strip() for m in discovered_raw.split(",") if m.strip()
            ]
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
            try:
                max_output_tokens = _validate_max_output_tokens(max_tokens_raw)
            except CustomProviderError:
                max_output_tokens = None

            profiles.append(
                _StoredCustomProfile(
                    profile_id=pid,
                    name=name,
                    base_url=base_url,
                    api_key=api_key,
                    enabled=enabled,
                    selected_model=selected_model,
                    discovered_models=discovered_models,
                    allow_remote_http=allow_remote_http,
                    chat_completions_path=chat_completions_path,
                    models_path=models_path,
                    custom_endpoints=custom_endpoints,
                    max_output_tokens=max_output_tokens,
                )
            )

        role_bindings: dict[str, str] = {}
        role_reasonings: dict[str, str] = {}
        valid_ids = {p.profile_id for p in profiles}
        for role in CUSTOM_PROVIDER_ROLES:
            raw_binding = env_map.get(f"KINAIYA_CUSTOM_ROLE_{role.upper()}", "").strip()
            if ":" in raw_binding:
                pid_part, model_part = raw_binding.split(":", 1)
                pid_clean = pid_part.strip().lower()
                model_clean = model_part.strip()
                if pid_clean in valid_ids and model_clean:
                    role_bindings[role] = f"{pid_clean}:{model_clean}"
            raw_role_reasoning = (
                env_map.get(f"KINAIYA_CUSTOM_ROLE_{role.upper()}_REASONING", "").strip().lower()
            )
            if raw_role_reasoning in REASONING_EFFORT_VALUES:
                role_reasonings[role] = raw_role_reasoning

        return (
            lines,
            profiles,
            role_bindings,
            role_reasonings,
            global_reasoning_effort,
            max_seq,
        )

    def _write_state(
        self,
        existing_lines: list[_EnvLine],
        profiles: list[_StoredCustomProfile],
        role_bindings: dict[str, str],
        max_seq: int,
        *,
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
            managed_lines.append(
                f"{prefix}NAME={_escape_dotenv_value(profile.name, quote=True)}"
            )
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
            managed_lines.append(
                f"{prefix}ENABLED={'true' if profile.enabled else 'false'}"
            )
            if profile.selected_model:
                managed_lines.append(
                    f"{prefix}MODEL={_escape_dotenv_value(profile.selected_model, quote=True)}"
                )
            if profile.discovered_models:
                csv_models = ",".join(profile.discovered_models)
                managed_lines.append(
                    f"{prefix}DISCOVERED_MODELS={_escape_dotenv_value(csv_models, quote=True)}"
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

        all_lines = preserved_lines + ([""] if preserved_lines else []) + managed_lines
        serialized = "\n".join(all_lines) + "\n"

        self._env_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".custom_providers_",
            suffix=".tmp",
            dir=str(self._env_path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._env_path)
            os.chmod(self._env_path, stat.S_IRUSR | stat.S_IWUSR)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

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

    def snapshot(self) -> dict[str, Any]:
        """Return the public snapshot of custom providers and role bindings."""
        (
            _,
            profiles,
            role_bindings,
            role_reasonings,
            global_reasoning_effort,
            _,
        ) = self._load_state()
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
                prof = by_id.get(pid)
                if prof is not None:
                    role_override = role_reasonings.get(role)
                    effective_effort = (
                        role_override
                        if role_override in REASONING_EFFORT_VALUES
                        else global_reasoning_effort
                    )
                    resolved_roles[role] = {
                        "profile_id": prof.profile_id,
                        "provider_name": prof.name,
                        "base_url": prof.base_url,
                        "enabled": prof.enabled,
                        "model_id": model_id,
                        "qualified_model_id": f"{prof.profile_id}:{model_id}",
                        "reasoning_effort": role_override,
                        "effective_reasoning_effort": effective_effort,
                        "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
                    }
                    continue
            resolved_roles[role] = None

        return {
            "schema_version": 1,
            "env_file_name": self._env_path.name,
            "unlimited": True,
            "global_reasoning_effort": global_reasoning_effort,
            "profiles": profile_dicts,
            "role_bindings": resolved_roles,
        }

    def set_global_reasoning_effort(self, reasoning_effort: str) -> dict[str, Any]:
        clean_effort = _validate_reasoning_effort(reasoning_effort, allow_empty=False)
        (
            lines,
            profiles,
            role_bindings,
            role_reasonings,
            _,
            max_seq,
        ) = self._load_state()
        self._write_state(
            lines,
            profiles,
            role_bindings,
            max_seq,
            role_reasonings=role_reasonings,
            global_reasoning_effort=clean_effort,
        )
        return self.snapshot()

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
        max_output_tokens: int | str | None = None,
    ) -> dict[str, Any]:
        clean_name = _validate_single_line_text(name, "Provider name", max_len=96)
        clean_url = normalize_openai_base_url(
            base_url,
            allow_remote_http=bool(allow_remote_http),
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
        if api_key and api_key.strip():
            clean_key = _validate_single_line_text(api_key, "API key", max_len=512)
        clean_model = _validate_model_id(selected_model) if selected_model.strip() else ""

        (
            lines,
            profiles,
            role_bindings,
            role_reasonings,
            global_reasoning_effort,
            max_seq,
        ) = self._load_state()
        next_seq = max_seq + 1
        new_id = f"p{next_seq:02d}"
        existing_ids = {p.profile_id for p in profiles}
        while new_id in existing_ids:
            next_seq += 1
            new_id = f"p{next_seq:02d}"

        created = _StoredCustomProfile(
            profile_id=new_id,
            name=clean_name,
            base_url=clean_url,
            api_key=clean_key,
            enabled=bool(enabled),
            selected_model=clean_model,
            discovered_models=[clean_model] if clean_model else [],
            allow_remote_http=bool(allow_remote_http),
            chat_completions_path=clean_chat_path,
            models_path=clean_models_path,
            custom_endpoints=clean_custom_endpoints,
            max_output_tokens=clean_max_tokens,
        )
        profiles.append(created)
        self._write_state(
            lines,
            profiles,
            role_bindings,
            next_seq,
            role_reasonings=role_reasonings,
            global_reasoning_effort=global_reasoning_effort,
        )
        return self.snapshot()

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
        max_output_tokens: int | str | None = None,
        clear_max_output_tokens: bool = False,
    ) -> dict[str, Any]:
        pid = profile_id.strip().lower()
        if not _PROFILE_ID_RE.match(pid):
            raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")

        (
            lines,
            profiles,
            role_bindings,
            role_reasonings,
            global_reasoning_effort,
            max_seq,
        ) = self._load_state()
        target: _StoredCustomProfile | None = None
        for prof in profiles:
            if prof.profile_id == pid:
                target = prof
                break
        if target is None:
            raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")

        if allow_remote_http is not None:
            target.allow_remote_http = bool(allow_remote_http)
        if name is not None:
            target.name = _validate_single_line_text(name, "Provider name", max_len=96)
        if base_url is not None:
            target.base_url = normalize_openai_base_url(
                base_url,
                allow_remote_http=target.allow_remote_http,
            )
        if chat_completions_path is not None:
            target.chat_completions_path = _validate_endpoint_path(
                chat_completions_path,
                "Chat completions endpoint path",
                default_path=DEFAULT_CHAT_COMPLETIONS_PATH,
            )
        if models_path is not None:
            target.models_path = _validate_endpoint_path(
                models_path,
                "Models endpoint path",
                default_path=DEFAULT_MODELS_PATH,
            )
        if custom_endpoints is not None:
            target.custom_endpoints = _validate_custom_endpoints(custom_endpoints)
        if clear_max_output_tokens:
            target.max_output_tokens = None
        elif max_output_tokens is not None:
            target.max_output_tokens = _validate_max_output_tokens(max_output_tokens)
        if clear_api_key:
            target.api_key = ""
        elif api_key is not None and api_key.strip():
            # Ignore accidental submission of the UI mask string
            if api_key.strip() != MASKED_API_KEY_DISPLAY:
                target.api_key = _validate_single_line_text(api_key, "API key", max_len=512)
        if enabled is not None:
            target.enabled = bool(enabled)
        if selected_model is not None:
            cleaned_model = (
                _validate_model_id(selected_model) if selected_model.strip() else ""
            )
            target.selected_model = cleaned_model
            if cleaned_model and cleaned_model not in (target.discovered_models or []):
                target.discovered_models = list(target.discovered_models or []) + [
                    cleaned_model
                ]

        self._write_state(
            lines,
            profiles,
            role_bindings,
            max_seq,
            role_reasonings=role_reasonings,
            global_reasoning_effort=global_reasoning_effort,
        )
        return self.snapshot()

    def set_enabled(self, profile_id: str, *, enabled: bool) -> dict[str, Any]:
        return self.update_profile(profile_id, enabled=enabled)

    def bind_role_model(
        self,
        *,
        role: str,
        profile_id: str | None,
        model_id: str | None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        clean_role = role.strip().lower()
        if clean_role not in CUSTOM_PROVIDER_ROLES:
            raise CustomProviderError(f"Unsupported role: {role!r}")

        (
            lines,
            profiles,
            role_bindings,
            role_reasonings,
            global_reasoning_effort,
            max_seq,
        ) = self._load_state()
        if not profile_id or not model_id or not model_id.strip():
            role_bindings.pop(clean_role, None)
            role_reasonings.pop(clean_role, None)
            self._write_state(
                lines,
                profiles,
                role_bindings,
                max_seq,
                role_reasonings=role_reasonings,
                global_reasoning_effort=global_reasoning_effort,
            )
            return self.snapshot()

        pid = profile_id.strip().lower()
        target = next((p for p in profiles if p.profile_id == pid), None)
        if target is None:
            raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")
        if not target.enabled:
            raise CustomProviderError(
                f"Custom provider {target.name!r} ({pid}) is disabled and cannot be bound to {clean_role}."
            )
        clean_model = _validate_model_id(model_id)
        target.selected_model = clean_model
        if clean_model not in (target.discovered_models or []):
            target.discovered_models = list(target.discovered_models or []) + [clean_model]
        role_bindings[clean_role] = f"{pid}:{clean_model}"
        if reasoning_effort is not None:
            clean_role_effort = _validate_reasoning_effort(reasoning_effort, allow_empty=True)
            if clean_role_effort:
                role_reasonings[clean_role] = clean_role_effort
            else:
                role_reasonings.pop(clean_role, None)
        self._write_state(
            lines,
            profiles,
            role_bindings,
            max_seq,
            role_reasonings=role_reasonings,
            global_reasoning_effort=global_reasoning_effort,
        )
        return self.snapshot()

    def remove_profile(
        self,
        profile_id: str,
        *,
        confirm_referenced: bool = False,
    ) -> dict[str, Any]:
        pid = profile_id.strip().lower()
        (
            lines,
            profiles,
            role_bindings,
            role_reasonings,
            global_reasoning_effort,
            max_seq,
        ) = self._load_state()
        target = next((p for p in profiles if p.profile_id == pid), None)
        if target is None:
            raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")

        refs = self._references_for_profile(target, role_bindings)
        if refs and not confirm_referenced:
            raise CustomProviderReferencedError(pid, refs)

        remaining = [p for p in profiles if p.profile_id != pid]
        for role in refs:
            role_bindings.pop(role, None)
            role_reasonings.pop(role, None)

        self._write_state(
            lines,
            remaining,
            role_bindings,
            max_seq,
            role_reasonings=role_reasonings,
            global_reasoning_effort=global_reasoning_effort,
        )
        return self.snapshot()

    def discover_models(
        self,
        profile_id: str,
        *,
        timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        """Discover models from GET {normalized_base_url}{models_path} for a specific profile."""
        pid = profile_id.strip().lower()
        (
            lines,
            profiles,
            role_bindings,
            role_reasonings,
            global_reasoning_effort,
            max_seq,
        ) = self._load_state()
        target = next((p for p in profiles if p.profile_id == pid), None)
        if target is None:
            raise CustomProviderError(f"Unknown custom provider ID: {profile_id!r}")

        normalized_base = normalize_openai_base_url(
            target.base_url,
            allow_remote_http=target.allow_remote_http,
        )
        models_url = _resolve_endpoint_url(normalized_base, target.models_path)
        headers: dict[str, str] = {"Accept": "application/json"}
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"

        req = urllib.request.Request(models_url, headers=headers, method="GET")
        opener = self._http_opener
        if opener is None:
            built_opener = urllib.request.build_opener(_NoRedirectHandler())
            opener = built_opener.open

        status_code: int | None = None
        raw_body = b""
        try:
            with opener(req, timeout=timeout_s) as resp:
                status_code = getattr(resp, "status", 200)
                raw_body = resp.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            if status_code in (301, 302, 303, 307, 308):
                return self._discovery_result(
                    target=target,
                    models_url=models_url,
                    status="redirect_blocked",
                    http_status=status_code,
                    models=[],
                    message=(
                        f"Redirect (HTTP {status_code}) was blocked so Bearer credentials "
                        "are never forwarded. Update the Base URL to the final endpoint."
                    ),
                )
            if status_code in (401, 403):
                return self._discovery_result(
                    target=target,
                    models_url=models_url,
                    status="auth_error",
                    http_status=status_code,
                    models=[],
                    message=(
                        f"Authentication failed (HTTP {status_code}) when querying {models_url}. "
                        "Check or rotate the API key, or enter a manual model ID."
                    ),
                )
            if status_code in (404, 405, 501):
                return self._discovery_result(
                    target=target,
                    models_url=models_url,
                    status="unsupported",
                    http_status=status_code,
                    models=[],
                    message=(
                        f"Endpoint {models_url} returned HTTP {status_code} (/models not supported). "
                        "You can still enter a manual model ID below."
                    ),
                )
            return self._discovery_result(
                target=target,
                models_url=models_url,
                status="connection_error",
                http_status=status_code,
                models=[],
                message=(
                    f"Endpoint {models_url} returned HTTP {status_code}. "
                    "Verify the Base URL or enter a manual model ID."
                ),
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", str(exc))
            return self._discovery_result(
                target=target,
                models_url=models_url,
                status="connection_error",
                http_status=None,
                models=[],
                message=(
                    f"Could not reach {models_url} ({reason}). "
                    "Verify the server is running or enter a manual model ID."
                ),
            )

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._discovery_result(
                target=target,
                models_url=models_url,
                status="invalid_response",
                http_status=status_code,
                models=[],
                message=(
                    f"Endpoint {models_url} did not return valid JSON. "
                    "Enter a manual model ID if this server uses a non-standard /models response."
                ),
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
            return self._discovery_result(
                target=target,
                models_url=models_url,
                status="invalid_response",
                http_status=status_code,
                models=[],
                message=(
                    f"Endpoint {models_url} JSON did not contain a 'data' or 'models' list. "
                    "Enter a manual model ID below."
                ),
            )

        discovered: list[str] = []
        seen_ids: set[str] = set()
        for entry in items:
            candidate_id = ""
            if isinstance(entry, dict):
                raw_id = entry.get("id") or entry.get("name")
                if isinstance(raw_id, str):
                    candidate_id = raw_id.strip()
            elif isinstance(entry, str):
                candidate_id = entry.strip()
            if not candidate_id or not _MODEL_ID_RE.match(candidate_id):
                continue
            if candidate_id not in seen_ids:
                seen_ids.add(candidate_id)
                discovered.append(candidate_id)

        target.discovered_models = discovered
        if discovered and not target.selected_model:
            target.selected_model = discovered[0]
        self._write_state(
            lines,
            profiles,
            role_bindings,
            max_seq,
            role_reasonings=role_reasonings,
            global_reasoning_effort=global_reasoning_effort,
        )

        if not discovered:
            return self._discovery_result(
                target=target,
                models_url=models_url,
                status="empty",
                http_status=status_code,
                models=[],
                message=(
                    f"Endpoint {models_url} returned 0 models. "
                    "Load a model on the server or enter a manual model ID."
                ),
            )

        return self._discovery_result(
            target=target,
            models_url=models_url,
            status="ok",
            http_status=status_code,
            models=discovered,
            message=f"Discovered {len(discovered)} model(s) from {models_url}.",
        )

    def _discovery_result(
        self,
        *,
        target: _StoredCustomProfile,
        models_url: str,
        status: str,
        http_status: int | None,
        models: list[str],
        message: str,
    ) -> dict[str, Any]:
        return {
            "profile_id": target.profile_id,
            "provider_name": target.name,
            "models_url": models_url,
            "status": status,
            "http_status": http_status,
            "models": [
                {
                    "model_id": mid,
                    "qualified_model_id": f"{target.profile_id}:{mid}",
                    "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
                }
                for mid in models
            ],
            "manual_fallback_allowed": True,
            "capabilities": dict(UNVERIFIED_CUSTOM_CAPABILITIES),
            "message": message,
            "snapshot": self.snapshot(),
        }

    def apply_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a custom provider mutation action from the HTTP API."""
        if not isinstance(payload, dict):
            raise CustomProviderError("Request body must be a JSON object.")
        action = str(payload.get("action", "")).strip()
        if action == "add_custom_provider":
            return self.add_profile(
                name=str(payload.get("name", "")),
                base_url=str(payload.get("base_url", "")),
                api_key=str(payload.get("api_key", "")),
                enabled=bool(payload.get("enabled", True)),
                selected_model=str(payload.get("selected_model", "")),
                allow_remote_http=bool(payload.get("allow_remote_http", False)),
                chat_completions_path=(
                    str(payload["chat_completions_path"])
                    if payload.get("chat_completions_path") is not None
                    else None
                ),
                models_path=(
                    str(payload["models_path"])
                    if payload.get("models_path") is not None
                    else None
                ),
                custom_endpoints=(
                    str(payload["custom_endpoints"])
                    if payload.get("custom_endpoints") is not None
                    else None
                ),
                max_output_tokens=payload.get("max_output_tokens"),
            )
        if action == "update_custom_provider":
            return self.update_profile(
                str(payload.get("profile_id", "")),
                name=(
                    str(payload["name"])
                    if "name" in payload and payload["name"] is not None
                    else None
                ),
                base_url=(
                    str(payload["base_url"])
                    if "base_url" in payload and payload["base_url"] is not None
                    else None
                ),
                api_key=(
                    str(payload["api_key"])
                    if "api_key" in payload and payload["api_key"] is not None
                    else None
                ),
                clear_api_key=bool(payload.get("clear_api_key", False)),
                enabled=(
                    bool(payload["enabled"])
                    if "enabled" in payload and payload["enabled"] is not None
                    else None
                ),
                selected_model=(
                    str(payload["selected_model"])
                    if "selected_model" in payload and payload["selected_model"] is not None
                    else None
                ),
                allow_remote_http=(
                    bool(payload["allow_remote_http"])
                    if "allow_remote_http" in payload
                    and payload["allow_remote_http"] is not None
                    else None
                ),
                chat_completions_path=(
                    str(payload["chat_completions_path"])
                    if "chat_completions_path" in payload
                    and payload["chat_completions_path"] is not None
                    else None
                ),
                models_path=(
                    str(payload["models_path"])
                    if "models_path" in payload and payload["models_path"] is not None
                    else None
                ),
                custom_endpoints=(
                    str(payload["custom_endpoints"])
                    if "custom_endpoints" in payload
                    and payload["custom_endpoints"] is not None
                    else None
                ),
                max_output_tokens=payload.get("max_output_tokens"),
                clear_max_output_tokens=bool(
                    payload.get("clear_max_output_tokens", False)
                    or (
                        "max_output_tokens" in payload
                        and (
                            payload["max_output_tokens"] is None
                            or str(payload["max_output_tokens"]).strip() == ""
                        )
                    )
                ),
            )
        if action == "set_custom_provider_enabled":
            return self.set_enabled(
                str(payload.get("profile_id", "")),
                enabled=bool(payload.get("enabled", True)),
            )
        if action == "remove_custom_provider":
            return self.remove_profile(
                str(payload.get("profile_id", "")),
                confirm_referenced=bool(payload.get("confirm_referenced", False)),
            )
        if action == "bind_custom_role_model":
            return self.bind_role_model(
                role=str(payload.get("role", "")),
                profile_id=(
                    str(payload["profile_id"])
                    if payload.get("profile_id") is not None
                    else None
                ),
                model_id=(
                    str(payload["model_id"])
                    if payload.get("model_id") is not None
                    else None
                ),
                reasoning_effort=(
                    str(payload["reasoning_effort"])
                    if "reasoning_effort" in payload
                    and payload["reasoning_effort"] is not None
                    else None
                ),
            )
        if action == "set_global_reasoning_effort":
            return self.set_global_reasoning_effort(
                str(payload.get("reasoning_effort", ""))
            )
        if action == "discover_custom_models":
            return self.discover_models(str(payload.get("profile_id", "")))
        raise CustomProviderError(f"Unsupported custom provider action: {action!r}")
