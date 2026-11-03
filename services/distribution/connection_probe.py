"""Explicit bounded provider connection verification with distinct readiness states (#391).

Separates configuration persistence, role adapter eligibility (#335), endpoint
reachability, credential acceptance, `/models` metadata listing, and optional
minimal inference probing. Never claims a successful first task or
`inference_verified` readiness from `GET /models` alone, never retries failed
requests automatically, never forwards credentials across HTTP redirects, and
never leaks API keys, headers, or raw response bodies into terminal output.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from services.distribution.installation import SUPPORTED_ROLE_ADAPTERS, InstallError

DEFAULT_PROBE_TIMEOUT_S = 5.0
MAX_PROBE_RESPONSE_BYTES = 65536

EndpointReachableState = Literal["verified", "failed", "not_checked"]
CredentialsAcceptedState = Literal["verified", "rejected", "not_required", "not_checked"]
ModelListedState = Literal["verified", "not_listed", "unsupported_endpoint", "not_checked"]
InferenceProbeState = Literal["verified", "failed", "skipped"]
OverallReadinessState = Literal[
    "unverified_saved",
    "metadata_verified",
    "inference_verified",
    "failed",
]
FailureKind = Literal[
    "auth",
    "timeout",
    "rate_limit",
    "billing",
    "bad_response",
    "unsupported_adapter",
    "unavailable_endpoint",
]

REMEDIATION_MESSAGES: dict[str, str] = {
    "unsupported_adapter": (
        "Configuration limitation (not a model-quality failure): the selected adapter "
        "does not implement exact complete-request measurement (prepare_complete) required "
        "by Agent (#335). Select 'lmstudio' for local native loopback or 'meta' with "
        "explicit cloud disclosure."
    ),
    "auth": (
        "Authentication rejected (HTTP 401/403): verify or rotate the API key in your "
        "private local credentials (.env) or environment variable and confirm endpoint permissions."
    ),
    "billing": (
        "Billing or quota limit reached: check your provider account balance, spending limit, "
        "or plan quota before retrying manually."
    ),
    "rate_limit": (
        "Provider rate limit reached (HTTP 429): wait before retrying manually; automatic "
        "retries are disabled to prevent unintended spend."
    ),
    "timeout": (
        "Connection timed out: verify the provider server is responsive and check network "
        "or firewall settings before retrying."
    ),
    "bad_response": (
        "Provider returned a malformed, oversized, or redirected response: verify the base URL "
        "points directly to the API endpoint without redirects and returns bounded JSON."
    ),
    "unavailable_endpoint": (
        "Endpoint unavailable: verify the local server is running on the configured port or "
        "confirm the remote HTTPS URL and explicit cloud disclosure approval."
    ),
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse all HTTP redirects so Authorization headers are never forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Redirects are refused during provider connection verification",
            headers,
            fp,
        )


def _is_loopback(endpoint: str) -> bool:
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


def _normalize_probe_urls(
    endpoint: str,
    *,
    adapter_kind: str,
) -> tuple[str, str]:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("Endpoint URL is required")
    raw = endpoint.strip()
    if any(ord(c) <= 32 or ord(c) == 127 for c in raw):
        raise ValueError("Endpoint URL must not contain whitespace or control characters")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Endpoint URL must use http or https with a valid hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Endpoint URL must not embed credentials, query, or fragment")

    path = (parsed.path or "").rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/responses", "/models"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
    if not path:
        path = "/v1"
    elif not path.lower().endswith("/v1") and not re.search(r"/v[0-9]+[a-z0-9._-]*$", path.lower()):
        path = f"{path}/v1"

    models_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, f"{path}/models", "", ""))
    inference_suffix = "/responses" if adapter_kind == "meta" else "/chat/completions"
    inference_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"{path}{inference_suffix}", "", "")
    )
    return models_url, inference_url


def _check_role_adapter(
    role: str,
    *,
    adapter_kind: str,
    endpoint: str,
    model: str,
    allow_cloud: bool,
    credential_env: str | None,
) -> tuple[bool, str | None]:
    """Validate role adapter eligibility using #335's exact `prepare_complete` contract."""
    from services.launcher import validate_setup_provider

    clean_role = role.strip().lower()
    clean_kind = adapter_kind.strip().lower()
    if clean_role not in SUPPORTED_ROLE_ADAPTERS:
        return False, REMEDIATION_MESSAGES["unsupported_adapter"]
    if clean_kind not in SUPPORTED_ROLE_ADAPTERS[clean_role]:
        return False, REMEDIATION_MESSAGES["unsupported_adapter"]

    synthetic_cloud = clean_kind == "meta"
    synthetic_url = "https://probe.example/v1" if synthetic_cloud else "http://127.0.0.1:1234"
    probe_profile: dict[str, Any] = {
        "kind": clean_kind,
        "base_url": synthetic_url,
        "model": model or "probe-model",
        "allow_cloud": synthetic_cloud,
    }
    if clean_kind == "lmstudio":
        probe_profile["max_tokens"] = 128
        probe_profile["reasoning_effort"] = "none"
    elif clean_kind == "meta":
        probe_profile["max_tokens"] = 2048
        probe_profile["context_tokens"] = 32768
        if clean_role == "agent":
            probe_profile["structured_actions"] = True
            probe_profile["action_format"] = "flat-v1"
        probe_profile["api_key_env"] = credential_env or "KINAIYA_PROBE_PLACEHOLDER_KEY"
    else:
        probe_profile["max_tokens"] = 2048
        if credential_env:
            probe_profile["api_key_env"] = credential_env

    try:
        validate_setup_provider(clean_role, probe_profile)
    except (ValueError, RuntimeError, TypeError, KeyError):
        return False, REMEDIATION_MESSAGES["unsupported_adapter"]
    return True, None


@dataclass(frozen=True)
class ConnectionProbeResult:
    """Structured readiness report separating config, metadata, and inference verification."""

    config_saved: bool
    role_adapter_eligible: bool
    endpoint_reachable: EndpointReachableState
    credentials_accepted: CredentialsAcceptedState
    model_listed: ModelListedState
    inference_probe: InferenceProbeState
    overall_readiness: OverallReadinessState
    failure_kind: FailureKind | None = None
    remediation: str | None = None
    planned_models_url: str = ""
    planned_inference_url: str = ""
    cloud_disclosure_applies: bool = False
    billable_warning_shown: bool = False

    @property
    def ready(self) -> bool:
        return self.overall_readiness == "inference_verified"

    @property
    def provider_connection(self) -> str:
        if self.overall_readiness == "unverified_saved":
            return "not_checked"
        return self.overall_readiness

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ready"] = self.ready
        payload["provider_connection"] = self.provider_connection
        return payload


def _classify_http_error(exc: urllib.error.HTTPError) -> FailureKind:
    code = exc.code
    error_body = ""
    try:
        if exc.fp is not None:
            raw = exc.fp.read(4096)
            if isinstance(raw, bytes):
                error_body = raw.decode("utf-8", errors="ignore").lower()
    except Exception:
        error_body = ""

    if code == 402 or any(
        token in error_body
        for token in (
            "insufficient_quota",
            "billing_hard_limit",
            "quota_exceeded",
            "credit balance",
            "billing",
        )
    ):
        return "billing"
    if code in (401, 403):
        return "auth"
    if code == 429:
        return "rate_limit"
    if code in (301, 302, 303, 307, 308):
        return "bad_response"
    return "unavailable_endpoint"


def probe_provider_connection(
    *,
    endpoint: str,
    model: str,
    role: str = "agent",
    adapter_kind: str | None = None,
    allow_cloud: bool = False,
    credential_env: str | None = None,
    api_key: str | None = None,
    config_saved: bool = True,
    skip_probe: bool = False,
    run_inference_probe: bool = False,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    max_bytes: int = MAX_PROBE_RESPONSE_BYTES,
    opener: Callable[..., Any] | None = None,
) -> ConnectionProbeResult:
    """Execute a bounded, zero-retry connection check with distinct readiness states."""
    loopback = _is_loopback(endpoint)
    cloud_applies = not loopback
    effective_kind = (
        adapter_kind.strip().lower()
        if adapter_kind is not None
        else (("meta" if allow_cloud else "lmstudio") if role == "agent" else "compatible")
    )

    try:
        models_url, inference_url = _normalize_probe_urls(endpoint, adapter_kind=effective_kind)
    except ValueError:
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=False,
            endpoint_reachable="not_checked",
            credentials_accepted="not_checked",
            model_listed="not_checked",
            inference_probe="skipped",
            overall_readiness="failed",
            failure_kind="unavailable_endpoint",
            remediation=REMEDIATION_MESSAGES["unavailable_endpoint"],
            cloud_disclosure_applies=cloud_applies,
        )

    eligible, _ = _check_role_adapter(
        role,
        adapter_kind=effective_kind,
        endpoint=endpoint,
        model=model,
        allow_cloud=allow_cloud,
        credential_env=credential_env,
    )
    if not eligible:
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=False,
            endpoint_reachable="not_checked",
            credentials_accepted="not_checked",
            model_listed="not_checked",
            inference_probe="skipped",
            overall_readiness="failed",
            failure_kind="unsupported_adapter",
            remediation=REMEDIATION_MESSAGES["unsupported_adapter"],
            planned_models_url=models_url,
            planned_inference_url=inference_url if run_inference_probe else "",
            cloud_disclosure_applies=cloud_applies,
        )

    if skip_probe:
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="not_checked",
            credentials_accepted="not_checked",
            model_listed="not_checked",
            inference_probe="skipped",
            overall_readiness="unverified_saved",
            failure_kind=None,
            remediation=None,
            planned_models_url=models_url,
            planned_inference_url="",
            cloud_disclosure_applies=cloud_applies,
        )

    parsed = urllib.parse.urlsplit(models_url)
    if cloud_applies and (not allow_cloud or parsed.scheme != "https"):
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="not_checked",
            credentials_accepted="not_checked",
            model_listed="not_checked",
            inference_probe="skipped",
            overall_readiness="failed",
            failure_kind="unavailable_endpoint",
            remediation=REMEDIATION_MESSAGES["unavailable_endpoint"],
            planned_models_url=models_url,
            planned_inference_url=inference_url if run_inference_probe else "",
            cloud_disclosure_applies=True,
        )

    resolved_secret = api_key or (os.environ.get(credential_env, "") if credential_env else "")
    has_credentials = bool(resolved_secret)
    headers: dict[str, str] = {"Accept": "application/json"}
    if has_credentials:
        headers["Authorization"] = f"Bearer {resolved_secret}"

    if opener is None:
        built = urllib.request.build_opener(_NoRedirectHandler())
        opener = built.open

    # Step 1: Bounded GET /models metadata check (never performs paid generation)
    models_req = urllib.request.Request(models_url, headers=dict(headers), method="GET")
    model_listed: ModelListedState = "not_checked"
    try:
        with opener(models_req, timeout=timeout_s) as resp:
            raw_models_body = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 405, 501):
            model_listed = "unsupported_endpoint"
            raw_models_body = None
        else:
            fkind = _classify_http_error(exc)
            cred_state: CredentialsAcceptedState = (
                "rejected"
                if fkind == "auth"
                else ("verified" if has_credentials else "not_required")
            )
            reach_state: EndpointReachableState = (
                "failed" if fkind == "unavailable_endpoint" else "verified"
            )
            return ConnectionProbeResult(
                config_saved=config_saved,
                role_adapter_eligible=True,
                endpoint_reachable=reach_state,
                credentials_accepted=cred_state,
                model_listed="not_checked",
                inference_probe="skipped",
                overall_readiness="failed",
                failure_kind=fkind,
                remediation=REMEDIATION_MESSAGES[fkind],
                planned_models_url=models_url,
                planned_inference_url=inference_url if run_inference_probe else "",
                cloud_disclosure_applies=cloud_applies,
                billable_warning_shown=bool(run_inference_probe),
            )
    except TimeoutError:
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="failed",
            credentials_accepted="not_checked",
            model_listed="not_checked",
            inference_probe="skipped",
            overall_readiness="failed",
            failure_kind="timeout",
            remediation=REMEDIATION_MESSAGES["timeout"],
            planned_models_url=models_url,
            planned_inference_url=inference_url if run_inference_probe else "",
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=bool(run_inference_probe),
        )
    except (urllib.error.URLError, OSError):
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="failed",
            credentials_accepted="not_checked",
            model_listed="not_checked",
            inference_probe="skipped",
            overall_readiness="failed",
            failure_kind="unavailable_endpoint",
            remediation=REMEDIATION_MESSAGES["unavailable_endpoint"],
            planned_models_url=models_url,
            planned_inference_url=inference_url if run_inference_probe else "",
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=bool(run_inference_probe),
        )

    cred_accepted: CredentialsAcceptedState = "verified" if has_credentials else "not_required"

    if raw_models_body is not None:
        if len(raw_models_body) > max_bytes:
            return ConnectionProbeResult(
                config_saved=config_saved,
                role_adapter_eligible=True,
                endpoint_reachable="verified",
                credentials_accepted=cred_accepted,
                model_listed="not_checked",
                inference_probe="skipped",
                overall_readiness="failed",
                failure_kind="bad_response",
                remediation=REMEDIATION_MESSAGES["bad_response"],
                planned_models_url=models_url,
                planned_inference_url=inference_url if run_inference_probe else "",
                cloud_disclosure_applies=cloud_applies,
                billable_warning_shown=bool(run_inference_probe),
            )
        try:
            parsed_json = json.loads(raw_models_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return ConnectionProbeResult(
                config_saved=config_saved,
                role_adapter_eligible=True,
                endpoint_reachable="verified",
                credentials_accepted=cred_accepted,
                model_listed="not_checked",
                inference_probe="skipped",
                overall_readiness="failed",
                failure_kind="bad_response",
                remediation=REMEDIATION_MESSAGES["bad_response"],
                planned_models_url=models_url,
                planned_inference_url=inference_url if run_inference_probe else "",
                cloud_disclosure_applies=cloud_applies,
                billable_warning_shown=bool(run_inference_probe),
            )

        items: list[Any] | None = None
        if isinstance(parsed_json, dict):
            if isinstance(parsed_json.get("data"), list):
                items = parsed_json["data"]
            elif isinstance(parsed_json.get("models"), list):
                items = parsed_json["models"]
        elif isinstance(parsed_json, list):
            items = parsed_json

        if items is None:
            return ConnectionProbeResult(
                config_saved=config_saved,
                role_adapter_eligible=True,
                endpoint_reachable="verified",
                credentials_accepted=cred_accepted,
                model_listed="not_checked",
                inference_probe="skipped",
                overall_readiness="failed",
                failure_kind="bad_response",
                remediation=REMEDIATION_MESSAGES["bad_response"],
                planned_models_url=models_url,
                planned_inference_url=inference_url if run_inference_probe else "",
                cloud_disclosure_applies=cloud_applies,
                billable_warning_shown=bool(run_inference_probe),
            )

        listed_ids: set[str] = set()
        for entry in items:
            if isinstance(entry, dict):
                cand = entry.get("id") or entry.get("name")
                if isinstance(cand, str) and cand.strip():
                    listed_ids.add(cand.strip())
            elif isinstance(entry, str) and entry.strip():
                listed_ids.add(entry.strip())

        model_listed = "verified" if model in listed_ids else "not_listed"

    # If the user did NOT explicitly opt in to the minimal inference probe, stop here:
    # NEVER claim `inference_verified` based solely on GET /models!
    if not run_inference_probe:
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="verified",
            credentials_accepted=cred_accepted,
            model_listed=model_listed,
            inference_probe="skipped",
            overall_readiness="metadata_verified",
            failure_kind=None,
            remediation=None,
            planned_models_url=models_url,
            planned_inference_url="",
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=False,
        )

    # Step 2: Explicit opt-in minimal 1-token inference probe
    inf_headers = dict(headers)
    inf_headers["Content-Type"] = "application/json"
    if effective_kind == "meta":
        inf_body = json.dumps(
            {
                "model": model,
                "input": [{"role": "user", "content": "ping"}],
                "max_output_tokens": 1,
                "stream": False,
            }
        ).encode("utf-8")
    else:
        inf_body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            }
        ).encode("utf-8")

    inf_req = urllib.request.Request(
        inference_url,
        data=inf_body,
        headers=inf_headers,
        method="POST",
    )
    try:
        with opener(inf_req, timeout=timeout_s) as resp:
            raw_inf_body = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        fkind = _classify_http_error(exc)
        if fkind == "auth":
            cred_accepted = "rejected"
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="verified",
            credentials_accepted=cred_accepted,
            model_listed=model_listed,
            inference_probe="failed",
            overall_readiness="failed",
            failure_kind=fkind,
            remediation=REMEDIATION_MESSAGES[fkind],
            planned_models_url=models_url,
            planned_inference_url=inference_url,
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=True,
        )
    except TimeoutError:
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="verified",
            credentials_accepted=cred_accepted,
            model_listed=model_listed,
            inference_probe="failed",
            overall_readiness="failed",
            failure_kind="timeout",
            remediation=REMEDIATION_MESSAGES["timeout"],
            planned_models_url=models_url,
            planned_inference_url=inference_url,
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=True,
        )
    except (urllib.error.URLError, OSError):
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="verified",
            credentials_accepted=cred_accepted,
            model_listed=model_listed,
            inference_probe="failed",
            overall_readiness="failed",
            failure_kind="unavailable_endpoint",
            remediation=REMEDIATION_MESSAGES["unavailable_endpoint"],
            planned_models_url=models_url,
            planned_inference_url=inference_url,
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=True,
        )

    if len(raw_inf_body) > max_bytes:
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="verified",
            credentials_accepted=cred_accepted,
            model_listed=model_listed,
            inference_probe="failed",
            overall_readiness="failed",
            failure_kind="bad_response",
            remediation=REMEDIATION_MESSAGES["bad_response"],
            planned_models_url=models_url,
            planned_inference_url=inference_url,
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=True,
        )

    try:
        inf_payload = json.loads(raw_inf_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="verified",
            credentials_accepted=cred_accepted,
            model_listed=model_listed,
            inference_probe="failed",
            overall_readiness="failed",
            failure_kind="bad_response",
            remediation=REMEDIATION_MESSAGES["bad_response"],
            planned_models_url=models_url,
            planned_inference_url=inference_url,
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=True,
        )

    valid_inf = isinstance(inf_payload, dict) and (
        isinstance(inf_payload.get("choices"), list)
        or isinstance(inf_payload.get("output"), list)
        or inf_payload.get("status") == "completed"
    )
    if not valid_inf:
        return ConnectionProbeResult(
            config_saved=config_saved,
            role_adapter_eligible=True,
            endpoint_reachable="verified",
            credentials_accepted=cred_accepted,
            model_listed=model_listed,
            inference_probe="failed",
            overall_readiness="failed",
            failure_kind="bad_response",
            remediation=REMEDIATION_MESSAGES["bad_response"],
            planned_models_url=models_url,
            planned_inference_url=inference_url,
            cloud_disclosure_applies=cloud_applies,
            billable_warning_shown=True,
        )

    return ConnectionProbeResult(
        config_saved=config_saved,
        role_adapter_eligible=True,
        endpoint_reachable="verified",
        credentials_accepted=cred_accepted,
        model_listed=model_listed,
        inference_probe="verified",
        overall_readiness="inference_verified",
        failure_kind=None,
        remediation=None,
        planned_models_url=models_url,
        planned_inference_url=inference_url,
        cloud_disclosure_applies=cloud_applies,
        billable_warning_shown=True,
    )


def format_probe_summary(result: ConnectionProbeResult) -> str:
    """Format a human-readable summary of all distinct readiness states without secrets."""
    lines = [
        "Connection Verification Report:",
        f"  config_saved:          {result.config_saved}",
        f"  role_adapter_eligible: {result.role_adapter_eligible}",
        f"  endpoint_reachable:    {result.endpoint_reachable}",
        f"  credentials_accepted:  {result.credentials_accepted}",
        f"  model_listed:          {result.model_listed}",
        f"  inference_probe:       {result.inference_probe}",
        f"  overall_readiness:     {result.overall_readiness}",
    ]
    if result.failure_kind is not None:
        lines.append(f"  failure_kind:          {result.failure_kind}")
    if result.remediation:
        lines.append(f"  remediation:           {result.remediation}")
    return "\n".join(lines) + "\n"


def run_interactive_connection_check(
    tty,
    *,
    endpoint: str,
    model: str,
    role: str = "agent",
    adapter_kind: str | None = None,
    allow_cloud: bool = False,
    credential_env: str | None = None,
    api_key: str | None = None,
    config_saved: bool = True,
    timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    max_bytes: int = MAX_PROBE_RESPONSE_BYTES,
    opener: Callable[..., Any] | None = None,
) -> ConnectionProbeResult:
    """Interactive terminal prompt for opt-in connection verification.

    Shows the planned destination URL, cloud disclosure state, and billable
    inference warning before making any network request. Never mutates saved
    configuration or cloud disclosure permissions.
    """
    if tty is None or not hasattr(tty, "readline") or not hasattr(tty, "write"):
        raise InstallError(
            "No controlling terminal; interactive connection verification requires a TTY"
        )

    effective_kind = (
        adapter_kind.strip().lower()
        if adapter_kind is not None
        else (("meta" if allow_cloud else "lmstudio") if role == "agent" else "compatible")
    )
    loopback = _is_loopback(endpoint)
    cloud_applies = not loopback
    try:
        models_url, inference_url = _normalize_probe_urls(endpoint, adapter_kind=effective_kind)
    except ValueError:
        models_url, inference_url = endpoint, endpoint

    tty.write(f"Planned connection check destination: {models_url}\n")
    tty.write(
        f"Cloud disclosure applies: {'yes' if cloud_applies else 'no'} "
        f"(approved: {'yes' if allow_cloud else 'no'})\n"
    )
    tty.write(
        "Run bounded connection check now? "
        "[no = skip (unverified_saved) / metadata = GET /models only / inference = include 1-token probe] [no]: "
    )
    tty.flush()
    line = tty.readline()
    if not line:
        raise InstallError("Setup cancelled: terminal input closed")
    choice = line.strip().lower() or "no"

    if choice in ("no", "n", "skip"):
        res = probe_provider_connection(
            endpoint=endpoint,
            model=model,
            role=role,
            adapter_kind=effective_kind,
            allow_cloud=allow_cloud,
            credential_env=credential_env,
            api_key=api_key,
            config_saved=config_saved,
            skip_probe=True,
            opener=opener,
        )
        tty.write(format_probe_summary(res))
        tty.flush()
        return res

    run_inference = False
    if choice in ("inference", "full", "yes-inference"):
        tty.write(
            f"Warning: Minimal 1-token inference probe to {inference_url} may be billable "
            "on metered cloud providers. Confirm minimal inference probe? yes/no [no]: "
        )
        tty.flush()
        confirm_line = tty.readline()
        if not confirm_line:
            raise InstallError("Setup cancelled: terminal input closed")
        run_inference = confirm_line.strip().lower() == "yes"

    res = probe_provider_connection(
        endpoint=endpoint,
        model=model,
        role=role,
        adapter_kind=effective_kind,
        allow_cloud=allow_cloud,
        credential_env=credential_env,
        api_key=api_key,
        config_saved=config_saved,
        skip_probe=False,
        run_inference_probe=run_inference,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        opener=opener,
    )
    tty.write(format_probe_summary(res))
    tty.flush()
    return res
