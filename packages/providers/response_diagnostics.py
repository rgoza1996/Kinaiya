"""Closed Meta failure metadata; no raw response, request or server text is retained."""

import json
import re

MAX_DIAGNOSTIC_BYTES = 1024
MAX_TOKEN_COUNT = 2**53 - 1
_DETAIL_KEYS = 16
_REASONS = frozenset({"max_output_tokens", "content_filter"})
_ERROR_CODES = frozenset(
    {
        "server_error",
        "rate_limit_exceeded",
        "invalid_prompt",
        "invalid_request_error",
        "context_length_exceeded",
        "content_filter",
        "model_error",
    }
)
_UNAVAILABLE = frozenset({"missing", "malformed", "unknown"})
_COUNTERS = frozenset({"input_tokens", "output_tokens", "total_tokens"})


def _label(container, key, allowed):
    if container is None:
        return "missing"
    if type(container) is not dict or len(container) > _DETAIL_KEYS:
        return "malformed"
    if key not in container or container[key] is None:
        return "missing"
    value = container[key]
    if type(value) is not str or not 1 <= len(value) <= 64:
        return "malformed"
    return value if value in allowed else "unknown"


def _usage(value):
    if type(value) is not dict or len(value) > _DETAIL_KEYS:
        return None
    result = {key: value[key] for key in _COUNTERS if key in value}
    if not {"input_tokens", "output_tokens"} <= result.keys() or any(
        type(count) is not int or not 0 <= count <= MAX_TOKEN_COUNT for count in result.values()
    ):
        return None
    total = result["input_tokens"] + result["output_tokens"]
    if total > MAX_TOKEN_COUNT or result.get("total_tokens", total) != total:
        return None
    return result


def validate_meta_diagnostic(value):
    """Rebuild only the closed scalar projection before native retention."""
    required = {"schema_version", "provider", "status", "reason", "error_code", "request_sha256"}
    if (
        type(value) is not dict
        or not 6 <= len(value) <= 7
        or not required <= value.keys()
        or value.keys() - required - {"usage"}
        or any(type(value[key]) is not str or len(value[key]) > 64 for key in required)
        or value["schema_version"] != "1.0"
        or value["provider"] != "meta"
        or value["status"] not in {"failed", "incomplete"}
        or value["reason"] not in _REASONS | _UNAVAILABLE | {"not_applicable"}
        or (value["status"] == "failed" and value["reason"] != "not_applicable")
        or (value["status"] == "incomplete" and value["reason"] == "not_applicable")
        or value["error_code"] not in _ERROR_CODES | _UNAVAILABLE
        or re.fullmatch(r"[0-9a-f]{64}", value["request_sha256"]) is None
    ):
        raise ValueError("Invalid Meta response diagnostic")
    result = {key: value[key] for key in required}
    if "usage" in value:
        usage = value["usage"]
        if (
            type(usage) is not dict
            or not 2 <= len(usage) <= 3
            or usage.keys() - _COUNTERS
            or (normalized := _usage(usage)) is None
        ):
            raise ValueError("Invalid Meta response diagnostic")
        result["usage"] = normalized
    if len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8")) > (
        MAX_DIAGNOSTIC_BYTES
    ):
        raise ValueError("Invalid Meta response diagnostic")
    return result


def meta_failure_diagnostic(response, request_sha256):
    """Inspect only selected bounded fields of an already decoded response."""
    status = response.get("status")
    result = {
        "schema_version": "1.0",
        "provider": "meta",
        "status": status,
        "reason": _label(response.get("incomplete_details"), "reason", _REASONS)
        if status == "incomplete"
        else "not_applicable",
        "error_code": _label(response.get("error"), "code", _ERROR_CODES),
        "request_sha256": request_sha256,
    }
    usage = _usage(response.get("usage"))
    if usage is not None:
        result["usage"] = usage
    return validate_meta_diagnostic(result)


def meta_failure_message(diagnostic):
    value = validate_meta_diagnostic(diagnostic)
    if value["status"] == "failed":
        return "Meta response failed"
    if value["reason"] == "max_output_tokens":
        return "Meta response did not complete: output token budget exhausted"
    return "Meta response did not complete"
