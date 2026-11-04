"""Issue #391: Explicit bounded connection verification with distinct readiness states."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from services.distribution import installation, wizard
from services.distribution.connection_probe import (
    MAX_PROBE_RESPONSE_BYTES,
    format_probe_summary,
    probe_provider_connection,
    run_interactive_connection_check,
)


class _FakeTTY:
    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)
        self.output = ""

    def write(self, text: str) -> None:
        self.output += text

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        try:
            return next(self._answers) + "\n"
        except StopIteration:
            return ""


class _FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._stream = io.BytesIO(body)
        self.status = status

    def read(self, amt: int | None = None) -> bytes:
        return self._stream.read(amt)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def test_metadata_verified_does_not_claim_inference_verified_or_ready() -> None:
    calls: list[tuple[str, str]] = []

    def opener(req: Any, timeout: float = 5.0) -> _FakeHTTPResponse:
        calls.append((req.get_method(), req.full_url))
        return _FakeHTTPResponse(b'{"data": [{"id": "qwen2.5-coder-7b"}]}', 200)

    res = probe_provider_connection(
        endpoint="http://127.0.0.1:1234/v1",
        model="qwen2.5-coder-7b",
        role="agent",
        adapter_kind="lmstudio",
        allow_cloud=False,
        run_inference_probe=False,
        opener=opener,
    )
    assert calls == [("GET", "http://127.0.0.1:1234/v1/models")]
    assert res.config_saved is True
    assert res.role_adapter_eligible is True
    assert res.endpoint_reachable == "verified"
    assert res.credentials_accepted == "not_required"
    assert res.model_listed == "verified"
    assert res.inference_probe == "skipped"
    assert res.overall_readiness == "metadata_verified"
    assert res.ready is False
    assert res.failure_kind is None


def test_explicit_opt_in_inference_verified_shows_billable_warning_and_verifies_inference() -> None:
    calls: list[tuple[str, str]] = []
    secret = "sk-cloud-meta-secret-never-leak-999"

    def opener(req: Any, timeout: float = 5.0) -> _FakeHTTPResponse:
        calls.append((req.get_method(), req.full_url))
        assert req.headers.get("Authorization") == f"Bearer {secret}"
        if req.get_method() == "GET":
            return _FakeHTTPResponse(b'{"data": [{"id": "muse-spark-1.3"}]}', 200)
        assert req.get_method() == "POST"
        return _FakeHTTPResponse(
            b'{"status": "completed", "output": [{"type": "message", "content": []}]}',
            200,
        )

    tty = _FakeTTY(["inference", "yes"])
    res = run_interactive_connection_check(
        tty,
        endpoint="https://api.meta.ai/v1",
        model="muse-spark-1.3",
        role="agent",
        adapter_kind="meta",
        allow_cloud=True,
        api_key=secret,
        opener=opener,
    )
    assert calls == [
        ("GET", "https://api.meta.ai/v1/models"),
        ("POST", "https://api.meta.ai/v1/responses"),
    ]
    assert "Planned connection check destination: https://api.meta.ai/v1/models" in tty.output
    assert "Cloud disclosure applies: yes" in tty.output
    assert "may be billable" in tty.output
    assert res.billable_warning_shown is True
    assert res.endpoint_reachable == "verified"
    assert res.credentials_accepted == "verified"
    assert res.model_listed == "verified"
    assert res.inference_probe == "verified"
    assert res.overall_readiness == "inference_verified"
    assert res.ready is True
    assert res.failure_kind is None
    assert secret not in tty.output
    assert secret not in format_probe_summary(res)
    assert secret not in repr(res)


def test_unsupported_adapter_compatible_for_agent_reports_configuration_limitation_without_network() -> (
    None
):
    def forbid_network(req: Any, timeout: float = 5.0) -> _FakeHTTPResponse:
        raise AssertionError("unsupported_adapter must fail before any network request")

    res = probe_provider_connection(
        endpoint="http://127.0.0.1:8080/v1",
        model="qwen2.5-coder",
        role="agent",
        adapter_kind="compatible",
        opener=forbid_network,
    )
    assert res.role_adapter_eligible is False
    assert res.endpoint_reachable == "not_checked"
    assert res.credentials_accepted == "not_checked"
    assert res.model_listed == "not_checked"
    assert res.inference_probe == "skipped"
    assert res.overall_readiness == "failed"
    assert res.failure_kind == "unsupported_adapter"
    assert res.remediation is not None
    assert "Configuration limitation (not a model-quality failure)" in res.remediation
    assert "prepare_complete" in res.remediation


@pytest.mark.parametrize(
    "case,expected_failure,expected_reach,expected_cred,expected_snippet",
    [
        ("auth_401", "auth", "verified", "rejected", "Authentication rejected"),
        ("auth_403", "auth", "verified", "rejected", "Authentication rejected"),
        ("timeout", "timeout", "failed", "not_checked", "timed out"),
        ("rate_limit_429", "rate_limit", "verified", "verified", "rate limit"),
        ("billing_402", "billing", "verified", "verified", "Billing or quota"),
        ("billing_quota_429", "billing", "verified", "verified", "Billing or quota"),
        (
            "malformed_json",
            "bad_response",
            "verified",
            "verified",
            "malformed, oversized, or redirected",
        ),
        (
            "oversized_body",
            "bad_response",
            "verified",
            "verified",
            "malformed, oversized, or redirected",
        ),
        (
            "redirect_302",
            "bad_response",
            "verified",
            "verified",
            "malformed, oversized, or redirected",
        ),
        ("conn_refused", "unavailable_endpoint", "failed", "not_checked", "Endpoint unavailable"),
    ],
)
def test_all_failure_kinds_have_specific_remediation_zero_retries_and_zero_leakage(
    case: str,
    expected_failure: str,
    expected_reach: str,
    expected_cred: str,
    expected_snippet: str,
) -> None:
    attempts = 0
    secret_key = "sk-sensitive-probe-token-8888"
    raw_sensitive_body = "RAW_INTERNAL_PROVIDER_STACKTRACE_DO_NOT_ECHO_98765"

    def mock_opener(req: Any, timeout: float = 5.0) -> _FakeHTTPResponse:
        nonlocal attempts
        attempts += 1
        if case == "auth_401":
            raise urllib.error.HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(raw_sensitive_body.encode("utf-8")),
            )
        if case == "auth_403":
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                {},
                io.BytesIO(raw_sensitive_body.encode("utf-8")),
            )
        if case == "timeout":
            raise TimeoutError("socket timed out")
        if case == "rate_limit_429":
            raise urllib.error.HTTPError(
                req.full_url,
                429,
                "Too Many Requests",
                {},
                io.BytesIO(b'{"error": {"message": "rate limit"}}'),
            )
        if case == "billing_402":
            raise urllib.error.HTTPError(
                req.full_url,
                402,
                "Payment Required",
                {},
                io.BytesIO(b'{"error": {"message": "payment required"}}'),
            )
        if case == "billing_quota_429":
            raise urllib.error.HTTPError(
                req.full_url,
                429,
                "Quota Exceeded",
                {},
                io.BytesIO(
                    f'{{"error": {{"code": "insufficient_quota", "detail": "{raw_sensitive_body}"}}}}'.encode()
                ),
            )
        if case == "malformed_json":
            return _FakeHTTPResponse(f"{{not-json {raw_sensitive_body}".encode(), 200)
        if case == "oversized_body":
            return _FakeHTTPResponse(b"A" * (MAX_PROBE_RESPONSE_BYTES + 100), 200)
        if case == "redirect_302":
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, None)
        if case == "conn_refused":
            raise urllib.error.URLError("Connection refused")
        raise AssertionError(f"Unhandled case {case}")

    res = probe_provider_connection(
        endpoint="https://api.meta.ai/v1",
        model="muse-spark-1.3",
        role="agent",
        adapter_kind="meta",
        allow_cloud=True,
        api_key=secret_key,
        opener=mock_opener,
    )
    # Strictly zero automatic retries
    assert attempts == 1
    assert res.overall_readiness == "failed"
    assert res.ready is False
    assert res.failure_kind == expected_failure
    assert res.endpoint_reachable == expected_reach
    assert res.credentials_accepted == expected_cred
    assert res.remediation is not None
    assert expected_snippet in res.remediation

    summary = format_probe_summary(res)
    assert secret_key not in summary
    assert raw_sensitive_body not in summary
    assert secret_key not in repr(res)
    assert raw_sensitive_body not in repr(res)


def test_skip_and_failed_probe_preserve_existing_saved_config_and_cloud_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import launcher

    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    layout = installation.Layout.for_home(tmp_path)
    setup_res = installation.setup(
        layout,
        endpoint="http://127.0.0.1:1234/v1",
        model="qwen2.5-coder-7b",
        adapter="lmstudio",
    )
    assert setup_res["provider_connection"] == "not_checked"
    assert setup_res["ready"] is False

    saved_config_bytes = layout.config.read_bytes()
    saved_config_json = json.loads(layout.config.read_text(encoding="utf-8"))
    assert saved_config_json["providers"]["agent"]["allow_cloud"] is False

    # 1. User skips interactive connection check -> unverified_saved, config untouched.
    tty_skip = _FakeTTY(["no"])
    res_skip = run_interactive_connection_check(
        tty_skip,
        endpoint="http://127.0.0.1:1234/v1",
        model="qwen2.5-coder-7b",
        role="agent",
        adapter_kind="lmstudio",
        allow_cloud=False,
        config_saved=True,
        opener=lambda *a, **kw: pytest.fail("skip must not make network calls"),
    )
    assert res_skip.overall_readiness == "unverified_saved"
    assert res_skip.provider_connection == "not_checked"
    assert res_skip.ready is False
    assert res_skip.endpoint_reachable == "not_checked"
    assert res_skip.credentials_accepted == "not_checked"
    assert res_skip.model_listed == "not_checked"
    assert res_skip.inference_probe == "skipped"
    assert layout.config.read_bytes() == saved_config_bytes

    # 2. Failed connection check (or unapproved remote cloud check) never mutates saved config
    # or broadens cloud disclosure permissions.
    tty_fail = _FakeTTY(["metadata"])
    res_fail = run_interactive_connection_check(
        tty_fail,
        endpoint="https://unapproved.cloud.example/v1",
        model="cloud-model",
        role="agent",
        adapter_kind="meta",
        allow_cloud=False,  # Cloud disclosure NOT granted
        config_saved=True,
        opener=lambda *a, **kw: pytest.fail("unapproved cloud must not make network calls"),
    )
    assert res_fail.overall_readiness == "failed"
    assert res_fail.failure_kind == "unavailable_endpoint"
    assert layout.config.read_bytes() == saved_config_bytes
    after_cfg = json.loads(layout.config.read_text(encoding="utf-8"))
    assert after_cfg["providers"]["agent"]["allow_cloud"] is False
    assert after_cfg["providers"]["agent"]["base_url"] == "http://127.0.0.1:1234/v1"


def test_wizard_with_opt_in_connection_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import launcher

    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    layout = installation.Layout.for_home(tmp_path)
    tty = _FakeTTY(
        [
            "",  # workspace
            "",  # data
            "http://127.0.0.1:1234",  # endpoint
            "qwen2.5-coder-7b",  # model
            "no",  # allow_cloud
            "",  # credential_env
            "no",  # converse
            "yes",  # confirm write
            "metadata",  # run GET /models check only
        ]
    )
    result = wizard.run_wizard(
        layout,
        tty=tty,
        check_connection=True,
        opener=lambda req, timeout=5.0: _FakeHTTPResponse(
            b'{"data": [{"id": "qwen2.5-coder-7b"}]}', 200
        ),
    )
    assert result["provider_connection"] == "metadata_verified"
    assert result["ready"] is False
    assert result["connection_probe"]["overall_readiness"] == "metadata_verified"
    assert result["connection_probe"]["inference_probe"] == "skipped"
