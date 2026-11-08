"""Searchable provider/model picker consuming existing stored profiles (#390).

Consumes existing provider profiles (`providers.env` / `config.json`) without
creating a competing registry. Performs opt-in, bounded `GET /models` metadata
discovery only after cloud disclosure approval when remote, never performs paid
generation during model listing, and pins `(profile_id, model_id)` across save
and reload.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.distribution.installation import (
    SUPPORTED_ROLE_ADAPTERS,
    InstallError,
    atomic_json,
)

MAX_DISCOVERED_MODELS = 500
MAX_MODELS_RESPONSE_BYTES = 65536
DEFAULT_DISCOVERY_TIMEOUT_S = 5.0
DEFAULT_PAGE_SIZE = 15

_PROFILE_ID_RE = re.compile(r"^p([0-9]{2,6})$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,200}$")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so Bearer credentials are never forwarded across origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Redirects are disallowed during authenticated model discovery",
            headers,
            fp,
        )


def is_loopback_endpoint(endpoint: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(endpoint.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_models_url(base_url: str, models_path: str = "/models") -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise InstallError("Provider endpoint URL is required")
    raw = base_url.strip()
    if any(ord(c) <= 32 or ord(c) == 127 for c in raw):
        raise InstallError(
            "Provider endpoint URL must not contain whitespace or control characters"
        )
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InstallError("Model discovery requires an HTTP or HTTPS provider endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InstallError("Provider endpoint URL must not embed credentials, query, or fragment")

    path = (parsed.path or "").rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
    if not path:
        path = "/v1"
    elif not path.lower().endswith("/v1") and not re.search(r"/v[0-9]+[a-z0-9._-]*$", path.lower()):
        path = f"{path}/v1"

    clean_models_path = (models_path or "/models").strip()
    if not clean_models_path.startswith("/"):
        clean_models_path = f"/{clean_models_path}"
    if path.lower().endswith("/v1") and clean_models_path.lower().startswith("/v1/"):
        clean_models_path = clean_models_path[3:]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"{path}{clean_models_path}", "", "")
    )


def check_local_v1_hint(url: str) -> str | None:
    """If url is a local server endpoint without /v1, return url with /v1 appended (#503)."""
    try:
        raw = url.strip()
        if not raw or any(ord(c) <= 32 for c in raw):
            return None
        parsed = urllib.parse.urlsplit(raw)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/v1") or "/v1/" in path:
        return None
    host = parsed.hostname.lower()
    port = parsed.port
    is_local = host in {"localhost", "127.0.0.1", "::1"}
    common_local_ports = {11434, 8080, 5000, 1234, 8000, 8081}
    if is_local or port in common_local_ports:
        new_path = (path + "/v1") if path else "/v1"
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment)
        )
    return None


def get_starter_provider_presets() -> list[ProviderProfileChoice]:
    """Default starter provider profiles available on a fresh install (#503)."""
    return [
        ProviderProfileChoice(
            profile_id="p01",
            name="LM Studio (Local)",
            base_url="http://127.0.0.1:1234",
            adapter_kind="lmstudio",
            allow_cloud=False,
            models_path="/v1/models",
        ),
        ProviderProfileChoice(
            profile_id="p02",
            name="Meta / Muse Cloud",
            base_url="https://api.muse.kinaiya.io/v1",
            adapter_kind="meta",
            allow_cloud=True,
            credential_env="KINAIYA_META_API_KEY",
            models_path="/models",
        ),
    ]


@dataclass(frozen=True)
class RoleAdapterEligibility:
    role: str
    adapter_kind: str
    eligible: bool
    reason: str = ""


def check_role_adapter_eligibility(role: str, adapter_kind: str) -> RoleAdapterEligibility:
    """Check whether an adapter kind is supported for the target service role (#335)."""
    clean_role = role.strip().lower()
    clean_kind = adapter_kind.strip().lower()
    if clean_role not in SUPPORTED_ROLE_ADAPTERS:
        return RoleAdapterEligibility(
            role=clean_role,
            adapter_kind=clean_kind,
            eligible=False,
            reason=f"Unsupported provider role {clean_role!r}",
        )
    allowed = SUPPORTED_ROLE_ADAPTERS[clean_role]
    if clean_kind in allowed:
        return RoleAdapterEligibility(
            role=clean_role,
            adapter_kind=clean_kind,
            eligible=True,
            reason=f"Adapter {clean_kind!r} is eligible for {clean_role}",
        )
    if clean_role == "agent" and clean_kind == "compatible":
        return RoleAdapterEligibility(
            role=clean_role,
            adapter_kind=clean_kind,
            eligible=False,
            reason=(
                "Adapter 'compatible' does not implement exact complete-request measurement "
                "(prepare_complete) required by Agent (#335); select 'lmstudio' for local "
                "native loopback or 'meta' with explicit cloud disclosure"
            ),
        )
    return RoleAdapterEligibility(
        role=clean_role,
        adapter_kind=clean_kind,
        eligible=False,
        reason=(
            f"Adapter {clean_kind!r} is not supported for {clean_role}; "
            f"supported {clean_role} adapters: {', '.join(allowed)}"
        ),
    )


@dataclass(frozen=True)
class ProviderProfileChoice:
    profile_id: str
    name: str
    base_url: str
    adapter_kind: str = "lmstudio"
    credential_env: str | None = None
    allow_cloud: bool = False
    enabled: bool = True
    selected_model: str = ""
    discovered_models: tuple[str, ...] = ()
    models_path: str = "/models"
    api_key: str = field(default="", repr=False)

    def eligibility_for(self, role: str) -> RoleAdapterEligibility:
        return check_role_adapter_eligibility(role, self.adapter_kind)


@dataclass(frozen=True)
class ModelDiscoveryResult:
    profile_id: str
    models_url: str
    status: str
    models: tuple[str, ...]
    message: str
    http_status: int | None = None
    truncated: bool = False
    manual_fallback_required: bool = False


@dataclass(frozen=True)
class SelectedModel:
    """Pinned `(profile_id, model_id)` selection preserving profile provenance."""

    profile_id: str
    model_id: str
    role: str = "agent"
    endpoint: str = ""
    adapter_kind: str = "lmstudio"
    credential_env: str | None = None
    allow_cloud: bool = False
    selection_source: str = "discovered"
    reasoning_effort: str = "none"

    @property
    def identity(self) -> tuple[str, str]:
        return (self.profile_id, self.model_id)

    @property
    def qualified_id(self) -> str:
        return f"{self.profile_id}:{self.model_id}"


def _unquote_dotenv_value(raw_value: str) -> str:
    val = raw_value.strip()
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        inner = val[1:-1]
        out: list[str] = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner) and inner[i + 1] in ('"', "\\"):
                out.append(inner[i + 1])
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    return val


def _read_env_map_safe(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    if env_path.is_symlink() or env_path.resolve() != Path(os.path.abspath(env_path)):
        raise InstallError("Provider .env path cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(env_path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 1024 * 1024:
            raise InstallError("Provider .env file must be a bounded regular file")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            content = stream.read()
    finally:
        os.close(fd)
    env_map: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        key = k.strip()
        if _ENV_KEY_RE.match(key):
            env_map[key] = _unquote_dotenv_value(v)
    return env_map


def load_stored_profiles(
    *,
    env_path: Path | None = None,
    config_path: Path | None = None,
    profiles: list[dict[str, Any] | ProviderProfileChoice] | None = None,
    include_starter_presets: bool = False,
) -> list[ProviderProfileChoice]:
    """Load existing provider profiles without inventing a competing registry."""
    result: list[ProviderProfileChoice] = []
    seen_ids: set[str] = set()

    if profiles is not None:
        for idx, item in enumerate(profiles, start=1):
            if isinstance(item, ProviderProfileChoice):
                if item.profile_id not in seen_ids:
                    seen_ids.add(item.profile_id)
                    result.append(item)
                continue
            if not isinstance(item, dict):
                raise InstallError("Profile entry must be a mapping or ProviderProfileChoice")
            pid = str(item.get("profile_id") or f"p{idx:02d}").strip().lower()
            if not _PROFILE_ID_RE.match(pid):
                raise InstallError(f"Invalid profile_id {pid!r}")
            base_url = str(item.get("base_url") or item.get("endpoint") or "").strip()
            if not base_url:
                raise InstallError(f"Profile {pid!r} is missing base_url/endpoint")
            allow_cloud = bool(item.get("allow_cloud", not is_loopback_endpoint(base_url)))
            default_kind = "meta" if allow_cloud else "lmstudio"
            adapter_kind = (
                str(
                    item.get("adapter_kind")
                    or item.get("kind")
                    or item.get("adapter")
                    or default_kind
                )
                .strip()
                .lower()
            )
            cred_env = item.get("credential_env") or item.get("api_key_env")
            if not cred_env and item.get("api_key"):
                cred_env = f"KINAIYA_PROVIDER_{pid.upper()}_API_KEY"
            disc_raw = item.get("discovered_models") or ()
            discovered = tuple(str(m).strip() for m in disc_raw if str(m).strip())
            choice = ProviderProfileChoice(
                profile_id=pid,
                name=str(item.get("name") or pid.upper()).strip(),
                base_url=base_url,
                adapter_kind=adapter_kind,
                credential_env=str(cred_env) if cred_env else None,
                allow_cloud=allow_cloud,
                enabled=bool(item.get("enabled", True)),
                selected_model=str(item.get("selected_model") or item.get("model") or "").strip(),
                discovered_models=discovered,
                models_path=str(item.get("models_path") or "/models").strip(),
                api_key=str(item.get("api_key") or ""),
            )
            if pid not in seen_ids:
                seen_ids.add(pid)
                result.append(choice)

    if env_path is not None and env_path.exists():
        env_map = _read_env_map_safe(env_path)
        raw_ids = env_map.get("KINAIYA_CUSTOM_PROVIDER_IDS", "")
        for tok in raw_ids.split(","):
            pid = tok.strip().lower()
            if not pid or not _PROFILE_ID_RE.match(pid) or pid in seen_ids:
                continue
            prefix = f"KINAIYA_PROVIDER_{pid.upper()}_"
            base_url = env_map.get(f"{prefix}BASE_URL", "").strip()
            if not base_url:
                continue
            name = env_map.get(f"{prefix}NAME", "").strip() or pid.upper()
            api_key = env_map.get(f"{prefix}API_KEY", "")
            enabled_raw = env_map.get(f"{prefix}ENABLED", "true").strip().lower()
            enabled = enabled_raw not in ("false", "0", "no", "off")
            allow_cloud = not is_loopback_endpoint(base_url)
            default_kind = "meta" if allow_cloud else "lmstudio"
            adapter_kind = (
                (
                    env_map.get(f"{prefix}ADAPTER", "")
                    or env_map.get(f"{prefix}KIND", "")
                    or default_kind
                )
                .strip()
                .lower()
            )
            disc_csv = env_map.get(f"{prefix}DISCOVERED_MODELS", "")
            discovered = tuple(m.strip() for m in disc_csv.split(",") if m.strip())
            cred_env = f"{prefix}API_KEY" if api_key else None
            seen_ids.add(pid)
            result.append(
                ProviderProfileChoice(
                    profile_id=pid,
                    name=name,
                    base_url=base_url,
                    adapter_kind=adapter_kind,
                    credential_env=cred_env,
                    allow_cloud=allow_cloud,
                    enabled=enabled,
                    selected_model=env_map.get(f"{prefix}MODEL", "").strip(),
                    discovered_models=discovered,
                    models_path=env_map.get(f"{prefix}MODELS_PATH", "/models").strip() or "/models",
                    api_key=api_key,
                )
            )

    if config_path is not None and config_path.exists():
        if config_path.is_symlink():
            raise InstallError("Configuration path cannot be a symlink")
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        providers_cfg = cfg.get("providers", {})
        if isinstance(providers_cfg, dict):
            seq = len(result) + 1
            for role_name, role_prof in providers_cfg.items():
                if not isinstance(role_prof, dict):
                    continue
                pid = str(role_prof.get("profile_id") or f"p{seq:02d}").strip().lower()
                if pid in seen_ids:
                    continue
                base_url = str(role_prof.get("base_url") or "").strip()
                if not base_url:
                    continue
                seen_ids.add(pid)
                seq += 1
                result.append(
                    ProviderProfileChoice(
                        profile_id=pid,
                        name=str(role_prof.get("profile_name") or f"{role_name}-{pid}"),
                        base_url=base_url,
                        adapter_kind=str(role_prof.get("kind") or "lmstudio"),
                        credential_env=role_prof.get("api_key_env"),
                        allow_cloud=bool(role_prof.get("allow_cloud", False)),
                        enabled=True,
                        selected_model=str(role_prof.get("model") or ""),
                    )
                )

    if include_starter_presets and not result:
        for preset in get_starter_provider_presets():
            if preset.profile_id not in seen_ids:
                seen_ids.add(preset.profile_id)
                result.append(preset)

    return result


def _truncate_line(text: str, width: int) -> str:
    bound = max(20, int(width))
    if len(text) <= bound:
        return text
    if bound <= 3:
        return text[:bound]
    return text[: bound - 3] + "..."


def _wrap_block(text: str, width: int) -> str:
    bound = max(20, int(width))
    wrapped = textwrap.wrap(
        text,
        width=bound,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return "\n".join(wrapped) if wrapped else ""


def format_profile_choices(
    profiles: list[ProviderProfileChoice],
    *,
    role: str = "agent",
    width: int = 80,
) -> str:
    """Format stored provider profiles with endpoint, ID, and role adapter eligibility."""
    lines: list[str] = []
    lines.append(_wrap_block(f"Stored profiles for role '{role}':", width))
    for idx, prof in enumerate(profiles, start=1):
        elig = prof.eligibility_for(role)
        badge = (
            f"[{role}: {prof.adapter_kind} OK]"
            if elig.eligible
            else f"[{role}: {prof.adapter_kind} UNSUPPORTED (#335)]"
        )
        row = f"{idx}. {prof.profile_id} {badge} ({prof.name}) {prof.base_url}"
        lines.append(_wrap_block(row, width))
        if not elig.eligible:
            lines.append(_wrap_block(f"   ! {elig.reason}", width))
    return "\n".join(lines) + "\n"


def select_profile_interactively(
    tty,
    profiles: list[ProviderProfileChoice],
    *,
    role: str = "agent",
    width: int = 80,
) -> ProviderProfileChoice:
    """Select an eligible stored provider profile for the requested role."""
    if tty is None or not hasattr(tty, "readline") or not hasattr(tty, "write"):
        raise InstallError("No controlling terminal; interactive profile selection requires a TTY")
    if not profiles:
        raise InstallError("No stored provider profiles available to select")

    tty.write(format_profile_choices(profiles, role=role, width=width))
    tty.write(_truncate_line(f"Select profile [1-{len(profiles)}] (or :cancel):", width) + "\n")
    tty.flush()
    try:
        raw = tty.readline()
    except (KeyboardInterrupt, EOFError):
        tty.write("\n")
        tty.flush()
        raise InstallError("Setup cancelled; no configuration written") from None
    if not raw:
        raise InstallError("Setup cancelled: terminal input closed")
    choice = raw.strip()
    if choice.lower() in (":cancel", "q", "quit"):
        raise InstallError("Setup cancelled: profile selection aborted")

    selected: ProviderProfileChoice | None = None
    if choice == "" and len(profiles) == 1:
        selected = profiles[0]
    elif choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(profiles):
            selected = profiles[idx - 1]
    else:
        for prof in profiles:
            if prof.profile_id == choice.lower():
                selected = prof
                break

    if selected is None:
        raise InstallError(f"Invalid profile selection {choice!r}")

    elig = selected.eligibility_for(role)
    if not elig.eligible:
        raise InstallError(elig.reason)
    if not selected.enabled:
        raise InstallError(
            f"Provider profile {selected.profile_id!r} ({selected.name}) is disabled"
        )
    return selected


def discover_profile_models(
    profile: ProviderProfileChoice | dict[str, Any],
    *,
    allow_cloud_disclosed: bool = False,
    timeout_s: float = DEFAULT_DISCOVERY_TIMEOUT_S,
    max_bytes: int = MAX_MODELS_RESPONSE_BYTES,
    max_models: int = MAX_DISCOVERED_MODELS,
    opener: Callable[..., Any] | None = None,
) -> ModelDiscoveryResult:
    """Opt-in bounded `GET /models` metadata check; never invokes paid generation."""
    if isinstance(profile, dict):
        loaded = load_stored_profiles(profiles=[profile])
        prof = loaded[0]
    else:
        prof = profile

    models_url = resolve_models_url(prof.base_url, prof.models_path)
    loopback = is_loopback_endpoint(prof.base_url)
    parsed = urllib.parse.urlsplit(models_url)

    if not loopback:
        if parsed.scheme != "https":
            return ModelDiscoveryResult(
                profile_id=prof.profile_id,
                models_url=models_url,
                status="insecure_remote",
                models=(),
                message=(
                    f"Refusing non-HTTPS remote endpoint {models_url}. "
                    "Use HTTPS with explicit cloud disclosure or enter a manual model ID."
                ),
                manual_fallback_required=True,
            )
        if not (allow_cloud_disclosed and prof.allow_cloud):
            return ModelDiscoveryResult(
                profile_id=prof.profile_id,
                models_url=models_url,
                status="disclosure_required",
                models=(),
                message=(
                    f"Cloud disclosure approval is required before contacting remote endpoint {models_url}. "
                    "Enter a manual model ID or approve cloud disclosure first."
                ),
                manual_fallback_required=True,
            )

    headers: dict[str, str] = {"Accept": "application/json"}
    api_key = prof.api_key or (
        os.environ.get(prof.credential_env, "") if prof.credential_env else ""
    )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(models_url, headers=headers, method="GET")
    if opener is None:
        built = urllib.request.build_opener(_NoRedirectHandler())
        opener = built.open

    http_status: int | None = None
    try:
        with opener(req, timeout=timeout_s) as resp:
            http_status = getattr(resp, "status", 200)
            raw_body = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        http_status = exc.code
        if http_status in (301, 302, 303, 307, 308):
            return ModelDiscoveryResult(
                profile_id=prof.profile_id,
                models_url=models_url,
                status="redirect_blocked",
                models=(),
                http_status=http_status,
                message=(
                    f"Redirect (HTTP {http_status}) from {models_url} was blocked so credentials "
                    "are never forwarded. Enter a manual model ID."
                ),
                manual_fallback_required=True,
            )
        if http_status in (401, 403):
            return ModelDiscoveryResult(
                profile_id=prof.profile_id,
                models_url=models_url,
                status="unauthorized",
                models=(),
                http_status=http_status,
                message=(
                    f"Authentication rejected (HTTP {http_status}) at {models_url}. "
                    "Verify your API key or enter a manual model ID."
                ),
                manual_fallback_required=True,
            )
        if http_status in (404, 405, 501):
            return ModelDiscoveryResult(
                profile_id=prof.profile_id,
                models_url=models_url,
                status="unsupported_endpoint",
                models=(),
                http_status=http_status,
                message=(
                    f"Endpoint {models_url} does not support model listing (HTTP {http_status}). "
                    "Enter a manual model ID."
                ),
                manual_fallback_required=True,
            )
        return ModelDiscoveryResult(
            profile_id=prof.profile_id,
            models_url=models_url,
            status="connection_error",
            models=(),
            http_status=http_status,
            message=(
                f"Model discovery at {models_url} failed with HTTP {http_status}. "
                "Enter a manual model ID."
            ),
            manual_fallback_required=True,
        )
    except TimeoutError:
        return ModelDiscoveryResult(
            profile_id=prof.profile_id,
            models_url=models_url,
            status="timeout",
            models=(),
            http_status=None,
            message=(
                f"Timed out waiting for {models_url} after {timeout_s:.1f}s. "
                "Enter a manual model ID."
            ),
            manual_fallback_required=True,
        )
    except (urllib.error.URLError, OSError):
        return ModelDiscoveryResult(
            profile_id=prof.profile_id,
            models_url=models_url,
            status="connection_error",
            models=(),
            http_status=None,
            message=(
                f"Could not connect to {models_url}. "
                "Verify the server is running or enter a manual model ID."
            ),
            manual_fallback_required=True,
        )

    if len(raw_body) > max_bytes:
        return ModelDiscoveryResult(
            profile_id=prof.profile_id,
            models_url=models_url,
            status="malformed_response",
            models=(),
            http_status=http_status,
            message=(
                f"Response from {models_url} exceeded {max_bytes} byte safety bound. "
                "Enter a manual model ID."
            ),
            manual_fallback_required=True,
        )

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return ModelDiscoveryResult(
            profile_id=prof.profile_id,
            models_url=models_url,
            status="malformed_response",
            models=(),
            http_status=http_status,
            message=(f"Endpoint {models_url} returned malformed JSON. Enter a manual model ID."),
            manual_fallback_required=True,
        )

    raw_items: list[Any] | None = None
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            raw_items = payload["data"]
        elif isinstance(payload.get("models"), list):
            raw_items = payload["models"]
    elif isinstance(payload, list):
        raw_items = payload

    if raw_items is None:
        return ModelDiscoveryResult(
            profile_id=prof.profile_id,
            models_url=models_url,
            status="malformed_response",
            models=(),
            http_status=http_status,
            message=(
                f"Endpoint {models_url} JSON did not contain a 'data' or 'models' array. "
                "Enter a manual model ID."
            ),
            manual_fallback_required=True,
        )

    discovered: list[str] = []
    seen: set[str] = set()
    truncated = False
    for entry in raw_items:
        candidate = ""
        if isinstance(entry, dict):
            raw_id = entry.get("id") or entry.get("name")
            if isinstance(raw_id, str):
                candidate = raw_id.strip()
        elif isinstance(entry, str):
            candidate = entry.strip()
        if not candidate or not _MODEL_ID_RE.match(candidate) or candidate in seen:
            continue
        if len(discovered) >= max_models:
            truncated = True
            break
        seen.add(candidate)
        discovered.append(candidate)

    if not discovered:
        return ModelDiscoveryResult(
            profile_id=prof.profile_id,
            models_url=models_url,
            status="empty",
            models=(),
            http_status=http_status,
            message=(
                f"Endpoint {models_url} returned 0 models. "
                "Load a model on the server or enter a manual model ID."
            ),
            manual_fallback_required=True,
        )

    suffix = f" (capped at first {max_models})" if truncated else ""
    return ModelDiscoveryResult(
        profile_id=prof.profile_id,
        models_url=models_url,
        status="ok",
        models=tuple(discovered),
        http_status=http_status,
        truncated=truncated,
        message=f"Discovered {len(discovered)} model(s) from {models_url}{suffix}.",
        manual_fallback_required=False,
    )


def _validate_manual_model_id(raw_model: str) -> str:
    cleaned = raw_model.strip()
    if (
        not cleaned
        or not (1 <= len(cleaned) <= 200)
        or any(ord(c) < 32 or ord(c) == 127 for c in cleaned)
        or not _MODEL_ID_RE.match(cleaned)
    ):
        raise InstallError("Select an explicit bounded model name")
    return cleaned


def _prompt_manual_model_id(
    tty,
    *,
    profile_id: str,
    role: str,
    endpoint: str,
    adapter_kind: str,
    credential_env: str | None,
    allow_cloud: bool,
    width: int,
) -> SelectedModel:
    tty.write(_wrap_block(f"Enter exact model ID for {profile_id} ({role}):", width) + "\n")
    tty.flush()
    try:
        line = tty.readline()
    except (KeyboardInterrupt, EOFError):
        tty.write("\n")
        tty.flush()
        raise InstallError("Setup cancelled; no configuration written") from None
    if not line:
        raise InstallError("Setup cancelled: terminal input closed")
    cleaned = line.strip()
    if cleaned.lower() in (":cancel", "q", "quit"):
        raise InstallError("Setup cancelled: manual model entry aborted")
    valid_model = _validate_manual_model_id(cleaned)
    return SelectedModel(
        profile_id=profile_id,
        model_id=valid_model,
        role=role,
        endpoint=endpoint,
        adapter_kind=adapter_kind,
        credential_env=credential_env,
        allow_cloud=allow_cloud,
        selection_source="manual",
    )


def select_model_interactively(
    tty,
    models: list[str] | tuple[str, ...] | list[dict[str, Any]] | ModelDiscoveryResult,
    *,
    profile_id: str = "p01",
    role: str = "agent",
    adapter_kind: str | None = None,
    endpoint: str = "http://127.0.0.1:1234",
    credential_env: str | None = None,
    allow_cloud: bool = False,
    width: int = 80,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_models: int = MAX_DISCOVERED_MODELS,
) -> SelectedModel:
    """Interactively select or filter a model on a TTY, pinning `(profile_id, model_id)`.

    - If 1 model is available: pressing Enter selects it immediately without retyping.
    - If multiple models are available: supports `/<query>` substring search, `1..N`
      numbered selection, `n`/`p` pagination, and `m`/`:manual` manual entry.
    - If discovery failed or returned 0 models: displays a truthful warning and falls
      back cleanly to manual model ID entry without fabricating models.
    """
    if tty is None or not hasattr(tty, "readline") or not hasattr(tty, "write"):
        raise InstallError("No controlling terminal; interactive model selection requires a TTY")

    pid = profile_id.strip().lower()
    if not _PROFILE_ID_RE.match(pid):
        raise InstallError(f"Invalid provider profile ID {profile_id!r}")

    effective_kind = (
        adapter_kind.strip().lower()
        if adapter_kind is not None
        else (("meta" if allow_cloud else "lmstudio") if role == "agent" else "compatible")
    )
    elig = check_role_adapter_eligibility(role, effective_kind)
    if not elig.eligible:
        tty.write(_wrap_block(f"Refused {role} adapter: {elig.reason}", width) + "\n")
        tty.flush()
        raise InstallError(elig.reason)

    discovery_warning: str | None = None
    raw_list: list[Any]
    if isinstance(models, ModelDiscoveryResult):
        raw_list = list(models.models)
        if models.status != "ok" or not raw_list:
            discovery_warning = models.message
        elif models.truncated:
            tty.write(_wrap_block(models.message, width) + "\n")
    else:
        raw_list = list(models)

    normalized_models: list[str] = []
    seen: set[str] = set()
    truncated = False
    for item in raw_list:
        cand = ""
        if isinstance(item, dict):
            cand = str(item.get("model_id") or item.get("id") or "").strip()
        elif isinstance(item, str):
            cand = item.strip()
        if not cand or not _MODEL_ID_RE.match(cand) or cand in seen:
            continue
        if len(normalized_models) >= max_models:
            truncated = True
            break
        seen.add(cand)
        normalized_models.append(cand)

    if truncated:
        tty.write(
            _wrap_block(
                f"Notice: model list capped at {max_models} entries; use /query or :manual.",
                width,
            )
            + "\n"
        )

    if not normalized_models:
        warn = discovery_warning or f"No models discovered for profile {pid}."
        tty.write(_wrap_block(f"Warning: {warn}", width) + "\n")
        tty.write(_wrap_block("Falling back to manual model ID entry.", width) + "\n")
        tty.flush()
        return _prompt_manual_model_id(
            tty,
            profile_id=pid,
            role=role,
            endpoint=endpoint,
            adapter_kind=effective_kind,
            credential_env=credential_env,
            allow_cloud=allow_cloud,
            width=width,
        )

    if len(normalized_models) == 1:
        only_model = normalized_models[0]
        tty.write(_truncate_line(f"Available model for {pid}: 1. {only_model}", width) + "\n")
        tty.write(
            _wrap_block(
                f"Press Enter to select '{only_model}' (or :manual / :cancel):",
                width,
            )
            + "\n"
        )
        tty.flush()
        try:
            line = tty.readline()
        except (KeyboardInterrupt, EOFError):
            tty.write("\n")
            tty.flush()
            raise InstallError("Setup cancelled; no configuration written") from None
        if not line:
            raise InstallError("Setup cancelled: terminal input closed")
        ans = line.strip()
        if ans == "" or ans == "1":
            return SelectedModel(
                profile_id=pid,
                model_id=only_model,
                role=role,
                endpoint=endpoint,
                adapter_kind=effective_kind,
                credential_env=credential_env,
                allow_cloud=allow_cloud,
                selection_source="discovered",
            )
        if ans.lower() in ("m", ":manual"):
            return _prompt_manual_model_id(
                tty,
                profile_id=pid,
                role=role,
                endpoint=endpoint,
                adapter_kind=effective_kind,
                credential_env=credential_env,
                allow_cloud=allow_cloud,
                width=width,
            )
        if ans.lower() in (":cancel", "q", "quit"):
            raise InstallError("Setup cancelled: model selection aborted")
        valid_direct = _validate_manual_model_id(ans)
        return SelectedModel(
            profile_id=pid,
            model_id=valid_direct,
            role=role,
            endpoint=endpoint,
            adapter_kind=effective_kind,
            credential_env=credential_env,
            allow_cloud=allow_cloud,
            selection_source="manual",
        )

    filtered = list(normalized_models)
    active_query = ""
    page = 0
    eff_page_size = max(1, int(page_size))

    while True:
        total_pages = max(1, (len(filtered) + eff_page_size - 1) // eff_page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * eff_page_size
        end = min(len(filtered), start + eff_page_size)

        header = (
            f"Models for {pid} ({len(filtered)} match"
            + (f" for /{active_query}" if active_query else "")
            + f", page {page + 1}/{total_pages}):"
        )
        tty.write(_truncate_line(header, width) + "\n")
        for idx in range(start, end):
            row = f"  {idx + 1}. {filtered[idx]}"
            tty.write(_truncate_line(row, width) + "\n")
        prompt_msg = (
            "Select [1-"
            + str(len(filtered))
            + "], /search, "
            + ("n/p page, " if total_pages > 1 else "")
            + ":manual, or :cancel:"
        )
        tty.write(_truncate_line(prompt_msg, width) + "\n")
        tty.flush()

        try:
            line = tty.readline()
        except (KeyboardInterrupt, EOFError):
            tty.write("\n")
            tty.flush()
            raise InstallError("Setup cancelled; no configuration written") from None
        if not line:
            raise InstallError("Setup cancelled: terminal input closed")
        cmd = line.strip()
        lower = cmd.lower()

        if lower in (":cancel", "q", "quit"):
            raise InstallError("Setup cancelled: model selection aborted")
        if lower in ("m", ":manual"):
            return _prompt_manual_model_id(
                tty,
                profile_id=pid,
                role=role,
                endpoint=endpoint,
                adapter_kind=effective_kind,
                credential_env=credential_env,
                allow_cloud=allow_cloud,
                width=width,
            )
        if lower == "n" and total_pages > 1:
            page = (page + 1) % total_pages
            continue
        if lower == "p" and total_pages > 1:
            page = (page - 1) % total_pages
            continue
        if cmd.startswith("/") or cmd.startswith("?"):
            query = cmd[1:].strip()
            if not query or query.lower() == "all":
                filtered = list(normalized_models)
                active_query = ""
                page = 0
                continue
            matches = [m for m in normalized_models if query.lower() in m.lower()]
            if not matches:
                tty.write(
                    _truncate_line(
                        f"No models matched /{query}. Showing previous list (or type :manual).",
                        width,
                    )
                    + "\n"
                )
                continue
            filtered = matches
            active_query = query
            page = 0
            continue
        if cmd == "" and len(filtered) == 1:
            return SelectedModel(
                profile_id=pid,
                model_id=filtered[0],
                role=role,
                endpoint=endpoint,
                adapter_kind=effective_kind,
                credential_env=credential_env,
                allow_cloud=allow_cloud,
                selection_source="discovered",
            )
        if cmd.isdigit():
            choice_num = int(cmd)
            if 1 <= choice_num <= len(filtered):
                return SelectedModel(
                    profile_id=pid,
                    model_id=filtered[choice_num - 1],
                    role=role,
                    endpoint=endpoint,
                    adapter_kind=effective_kind,
                    credential_env=credential_env,
                    allow_cloud=allow_cloud,
                    selection_source="discovered",
                )
            tty.write(
                _truncate_line(f"Invalid number {choice_num}; choose 1..{len(filtered)}.", width)
                + "\n"
            )
            continue
        if cmd in filtered:
            return SelectedModel(
                profile_id=pid,
                model_id=cmd,
                role=role,
                endpoint=endpoint,
                adapter_kind=effective_kind,
                credential_env=credential_env,
                allow_cloud=allow_cloud,
                selection_source="discovered",
            )
        tty.write(
            _truncate_line(
                "Enter a model number, /query to filter, :manual, or :cancel.",
                width,
            )
            + "\n"
        )


def save_role_model_selection(
    config_or_layout: Any,
    selection: SelectedModel,
) -> dict[str, Any]:
    """Persist a pinned `(profile_id, model_id)` selection for a service role in `config.json`."""
    elig = check_role_adapter_eligibility(selection.role, selection.adapter_kind)
    if not elig.eligible:
        raise InstallError(elig.reason)

    config_path = (
        Path(config_or_layout.config)
        if hasattr(config_or_layout, "config")
        else Path(config_or_layout)
    )
    if config_path.is_symlink():
        raise InstallError("Configuration path cannot be a symlink")
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        data = {"providers": {}, "distribution": {"converse": False}}

    providers = data.setdefault("providers", {})
    role_entry: dict[str, Any] = {
        "kind": selection.adapter_kind,
        "base_url": selection.endpoint,
        "model": selection.model_id,
        "profile_id": selection.profile_id,
        "qualified_model_id": selection.qualified_id,
        "allow_cloud": bool(selection.allow_cloud),
        "selection_source": selection.selection_source,
    }
    if selection.credential_env:
        role_entry["api_key_env"] = selection.credential_env
    if hasattr(selection, "reasoning_effort") and selection.reasoning_effort:
        role_entry["reasoning_effort"] = selection.reasoning_effort
    elif selection.adapter_kind == "lmstudio":
        role_entry.setdefault("max_tokens", 128)
        role_entry.setdefault("reasoning_effort", "none")
    elif selection.adapter_kind == "meta":
        role_entry.setdefault("max_tokens", 2048)
        role_entry.setdefault("context_tokens", 32768)
        if selection.role == "agent":
            role_entry.setdefault("structured_actions", True)
            role_entry.setdefault("action_format", "flat-v1")
    else:
        role_entry.setdefault("max_tokens", 2048)

    providers[selection.role] = role_entry
    atomic_json(config_path, data)
    return role_entry


def load_role_model_selection(
    config_or_layout: Any,
    role: str = "agent",
) -> SelectedModel:
    """Reload a saved `(profile_id, model_id)` selection for a service role from `config.json`."""
    config_path = (
        Path(config_or_layout.config)
        if hasattr(config_or_layout, "config")
        else Path(config_or_layout)
    )
    if not config_path.exists() or config_path.is_symlink():
        raise InstallError(f"Missing or unsafe configuration file: {config_path}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    role_entry = data.get("providers", {}).get(role)
    if not isinstance(role_entry, dict):
        raise InstallError(f"No provider configuration saved for role {role!r}")

    profile_id = str(role_entry.get("profile_id") or "").strip().lower()
    model_id = str(role_entry.get("model") or "").strip()
    qualified = str(role_entry.get("qualified_model_id") or "").strip()
    if not profile_id and ":" in qualified:
        profile_id, _ = qualified.split(":", 1)
    if not profile_id:
        profile_id = "p01"

    return SelectedModel(
        profile_id=profile_id,
        model_id=model_id,
        role=role,
        endpoint=str(role_entry.get("base_url") or ""),
        adapter_kind=str(role_entry.get("kind") or "lmstudio"),
        credential_env=role_entry.get("api_key_env"),
        allow_cloud=bool(role_entry.get("allow_cloud", False)),
        selection_source=str(role_entry.get("selection_source") or "discovered"),
    )
