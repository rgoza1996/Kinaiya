"""Strict output-budget metadata only; public synthetic bytes, no HTTP or models."""

import copy
import json

import pytest

from packages.providers.response_diagnostics import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_TOKEN_COUNT,
    meta_failure_diagnostic,
    settled_output_budget_diagnostic,
    validate_meta_diagnostic,
)

REQUEST_SHA = "a" * 64
MARKER = "SYNTHETIC_PRIVATE_PARTIAL_OUTPUT_484"
MAX_RAW_BYTES = 8 * 1024 * 1024


def response(**changes):
    return {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "error": None,
        "usage": {"input_tokens": 17, "output_tokens": 32, "total_tokens": 49},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": MARKER}]}],
        **changes,
    }


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def diagnostic(value=None, *, request_sha256=REQUEST_SHA, max_output_tokens=32):
    return settled_output_budget_diagnostic(
        encoded(response() if value is None else value), request_sha256, max_output_tokens
    )


def test_exact_exhaustion_returns_only_validated_closed_metadata():
    raw = encoded(response())
    before = bytes(raw)
    result = settled_output_budget_diagnostic(raw, REQUEST_SHA, 32)
    assert result == {
        "schema_version": "1.0",
        "provider": "meta",
        "status": "incomplete",
        "reason": "max_output_tokens",
        "error_code": "missing",
        "request_sha256": REQUEST_SHA,
        "usage": {"input_tokens": 17, "output_tokens": 32, "total_tokens": 49},
    }
    assert validate_meta_diagnostic(result) == result
    assert len(encoded(result)) <= MAX_DIAGNOSTIC_BYTES
    assert MARKER not in json.dumps(result)
    assert raw == before


def test_absent_error_optional_total_and_ignored_metadata_do_not_leak():
    value = response(usage={"input_tokens": 17, "output_tokens": 32, "debug": MARKER}, debug=MARKER)
    del value["error"]
    before = copy.deepcopy(value)
    result = diagnostic(value)
    assert result["usage"] == {"input_tokens": 17, "output_tokens": 32}
    assert result["error_code"] == "missing"
    assert MARKER not in json.dumps(result)
    assert value == before


@pytest.mark.parametrize("status", [None, True, 1, [], {}, "completed", "failed", "queued", MARKER])
def test_other_statuses_never_authorize_recovery(status):
    assert diagnostic(response(status=status)) is None


@pytest.mark.parametrize(
    "details",
    [
        None,
        {},
        [],
        "max_output_tokens",
        {"reason": None},
        {"reason": True},
        {"reason": "content_filter"},
        {"reason": "MAX_OUTPUT_TOKENS"},
        {"reason": MARKER},
        {"reason": "max_output_tokens", "debug": MARKER},
    ],
)
def test_reason_requires_the_exact_closed_details_object(details):
    assert diagnostic(response(incomplete_details=details)) is None


@pytest.mark.parametrize(
    "error", [{}, [], False, "", MARKER, {"code": "server_error"}, {"message": MARKER}]
)
def test_any_competing_error_is_terminal(error):
    assert diagnostic(response(error=error)) is None


@pytest.mark.parametrize(
    "usage",
    [
        None,
        [],
        {},
        {"input_tokens": 17},
        {"output_tokens": 32},
        {"input_tokens": 0, "output_tokens": 32},
        {"input_tokens": -1, "output_tokens": 32},
        {"input_tokens": True, "output_tokens": 32},
        {"input_tokens": 17.0, "output_tokens": 32},
        {"input_tokens": "17", "output_tokens": 32},
        {"input_tokens": MAX_TOKEN_COUNT + 1, "output_tokens": 32},
        {"input_tokens": 17, "output_tokens": True},
        {"input_tokens": 17, "output_tokens": 32.0},
        {"input_tokens": 17, "output_tokens": 0},
        {"input_tokens": 17, "output_tokens": 31},
        {"input_tokens": 17, "output_tokens": 33},
        {"input_tokens": 17, "output_tokens": 32, "total_tokens": 48},
        {"input_tokens": 17, "output_tokens": 32, "total_tokens": True},
        {"input_tokens": MAX_TOKEN_COUNT, "output_tokens": 32},
        {"input_tokens": 17, "output_tokens": 32, **{f"debug{i}": MARKER for i in range(15)}},
    ],
)
def test_missing_malformed_or_inconsistent_usage_never_authorizes_recovery(usage):
    assert diagnostic(response(usage=usage)) is None


@pytest.mark.parametrize(
    "maximum",
    [None, True, False, 0, -1, "32", 32.0, float("inf"), float("nan"), MAX_TOKEN_COUNT + 1],
)
def test_native_output_limit_is_a_strict_positive_safe_integer(maximum):
    assert diagnostic(max_output_tokens=maximum) is None


@pytest.mark.parametrize("maximum", [1, 32, 32768, MAX_TOKEN_COUNT - 1])
def test_native_output_limit_boundaries_are_exact(maximum):
    value = response(
        usage={"input_tokens": 1, "output_tokens": maximum, "total_tokens": maximum + 1}
    )
    assert diagnostic(value, max_output_tokens=maximum)["usage"]["output_tokens"] == maximum


def test_safe_counter_sum_cannot_overflow_even_at_allowed_native_limit():
    value = response(usage={"input_tokens": 1, "output_tokens": MAX_TOKEN_COUNT})
    assert diagnostic(value, max_output_tokens=MAX_TOKEN_COUNT) is None


@pytest.mark.parametrize(
    "request_sha", [None, True, 0, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, MARKER]
)
def test_request_digest_requires_exact_lowercase_sha256(request_sha):
    assert diagnostic(request_sha256=request_sha) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "{}",
        bytearray(b"{}"),
        b"",
        b" ",
        b"[]",
        b"null",
        b"true",
        b"1",
        b'"text"',
        b"{",
        b'{"status":',
        b"\xff",
        b"\xef\xbb\xbf{}",
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_malformed_or_nonobject_raw_input_is_refused_without_content(raw):
    assert settled_output_budget_diagnostic(raw, REQUEST_SHA, 32) is None


@pytest.mark.parametrize(
    "fragment",
    [
        b'"status":"incomplete","status":"incomplete"',
        b'"ignored":{"duplicate":1,"duplicate":2}',
        b'"ignored":NaN',
        b'"ignored":Infinity',
        b'"ignored":-Infinity',
        b'"ignored":1e999',
        b'"ignored":-1e999',
        b'"ignored":"\\ud800"',
    ],
)
def test_ambiguous_or_unsafe_ignored_fields_are_also_refused(fragment):
    body = encoded(response())
    raw = b"{" + fragment + b"," + body[1:]
    assert settled_output_budget_diagnostic(raw, REQUEST_SHA, 32) is None


def test_finite_ignored_fraction_is_not_an_usage_counter():
    assert diagnostic(response(ignored={"latency": 0.125}))["usage"]["output_tokens"] == 32


def test_raw_size_boundary_is_inclusive_and_larger_body_is_refused():
    small = encoded(response(padding=""))
    at_bound = encoded(response(padding="x" * (MAX_RAW_BYTES - len(small))))
    assert len(at_bound) == MAX_RAW_BYTES
    assert (
        settled_output_budget_diagnostic(at_bound, REQUEST_SHA, 32)["reason"] == "max_output_tokens"
    )
    assert settled_output_budget_diagnostic(at_bound + b" ", REQUEST_SHA, 32) is None


def test_existing_general_failure_projection_is_unchanged():
    # General terminal diagnostics still allow missing usage/reasons. They
    # supply no narrower recovery authority through this additive parser.
    value = {"status": "incomplete", "output": [{"text": MARKER}]}
    assert meta_failure_diagnostic(value, REQUEST_SHA) == {
        "schema_version": "1.0",
        "provider": "meta",
        "status": "incomplete",
        "reason": "missing",
        "error_code": "missing",
        "request_sha256": REQUEST_SHA,
    }
    assert diagnostic(value) is None
