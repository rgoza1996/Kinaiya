"""Issue453: actual Meta status normalization with public synthetic HTTP only."""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from packages.providers.prompt_budget import request_digest
from packages.providers.response_diagnostics import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_TOKEN_COUNT,
    validate_meta_diagnostic,
)
from packages.providers.text import MetaResponseError, MetaResponsesProvider, ProviderError

MARKER = "SYNTHETIC_SECRET_RESPONSE_453"


@pytest.fixture
async def exchange():
    state = SimpleNamespace(reply=None, counts=[], calls=[])

    def send(request):
        body = json.loads(request.content)
        if request.url.path.endswith("input_tokens"):
            state.counts.append(body)
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 17})
        state.calls.append(body)
        return httpx.Response(200, json=state.reply)

    state.provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-only-key",
        allow_cloud=True,
        context_tokens=32768,
        transport=httpx.MockTransport(send),
    )
    yield state
    await state.provider.close()


async def rejected(exchange, response, *, prepared=False):
    exchange.reply = response
    messages = [{"role": "user", "content": "Public synthetic request453"}]
    if prepared:
        operation = await exchange.provider.prepare_complete(messages)
        complete = operation.complete
    else:
        operation = None

        async def complete():
            return await exchange.provider.complete(messages)

    with pytest.raises(MetaResponseError) as raised:
        await complete()
    assert len(exchange.calls) == 1, "Terminal provider statuses must not retry"
    error = raised.value
    diagnostic = error.diagnostic
    assert diagnostic["request_sha256"] == request_digest(exchange.calls[0])
    if prepared:
        assert exchange.calls == exchange.counts
        assert operation.measurement.request_sha256 == diagnostic["request_sha256"]
        with pytest.raises(ProviderError):
            await operation.complete()
    assert len(json.dumps(diagnostic).encode()) <= MAX_DIAGNOSTIC_BYTES
    assert MARKER not in str(error) + json.dumps(diagnostic)
    return error


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize(
    "response,message,reason,code",
    [
        ({"status": "failed"}, "Meta response failed", "not_applicable", "missing"),
        (
            {"status": "failed", "error": {"code": "server_error", "message": MARKER}},
            "Meta response failed",
            "not_applicable",
            "server_error",
        ),
        (
            {"status": "failed", "incomplete_details": {"reason": "max_output_tokens"}},
            "Meta response failed",
            "not_applicable",
            "missing",
        ),
        ({"status": "incomplete"}, "Meta response did not complete", "missing", "missing"),
        (
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            "Meta response did not complete: output token budget exhausted",
            "max_output_tokens",
            "missing",
        ),
        (
            {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
            "Meta response did not complete",
            "content_filter",
            "missing",
        ),
    ],
)
async def test_native_status_and_only_supported_output_exhaustion(
    exchange, prepared, response, message, reason, code
):
    error = await rejected(exchange, response, prepared=prepared)
    assert str(error) == message
    assert error.diagnostic["reason"] == reason
    assert error.diagnostic["error_code"] == code


@pytest.mark.parametrize(
    "details,label",
    [
        (None, "missing"),
        ({}, "missing"),
        ({"reason": None}, "missing"),
        ({"reason": MARKER}, "unknown"),
        ({"reason": "MAX_OUTPUT_TOKENS"}, "unknown"),
        ({"reason": 3}, "malformed"),
        ({"reason": True}, "malformed"),
        ({"reason": ["max_output_tokens"]}, "malformed"),
        ({"reason": "max_output_tokens" + "x" * 10000}, "malformed"),
        ({"reason": "max_output_tokens", **{f"k{i}": MARKER for i in range(16)}}, "malformed"),
        ([MARKER], "malformed"),
    ],
)
async def test_missing_malformed_unknown_or_oversized_reason_is_never_budget_evidence(
    exchange, details, label
):
    error = await rejected(exchange, {"status": "incomplete", "incomplete_details": details})
    assert error.diagnostic["reason"] == label
    assert "budget" not in str(error)


@pytest.mark.parametrize(
    "error,label",
    [
        (None, "missing"),
        ({"message": MARKER}, "missing"),
        ({"code": MARKER}, "unknown"),
        ({"code": "server_error", "message": MARKER}, "server_error"),
        ({"code": "rate_limit_exceeded"}, "rate_limit_exceeded"),
        ({"code": True}, "malformed"),
        ({"code": {MARKER: MARKER}}, "malformed"),
        ({"code": "x" * 10000}, "malformed"),
        ({"code": "server_error", **{f"k{i}": MARKER for i in range(16)}}, "malformed"),
    ],
)
async def test_error_code_is_an_allowlisted_label_only(exchange, error, label):
    value = await rejected(exchange, {"status": "failed", "error": error})
    assert value.diagnostic["error_code"] == label


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"input_tokens": 7, "output_tokens": 3}, {"input_tokens": 7, "output_tokens": 3}),
        (
            {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10, "debug": MARKER},
            {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        ),
        (
            {"input_tokens": MAX_TOKEN_COUNT, "output_tokens": 0},
            {"input_tokens": MAX_TOKEN_COUNT, "output_tokens": 0},
        ),
        (None, None),
        ([], None),
        ({"input_tokens": 1}, None),
        ({"input_tokens": True, "output_tokens": 1}, None),
        ({"input_tokens": -1, "output_tokens": 1}, None),
        ({"input_tokens": 1.0, "output_tokens": 1}, None),
        ({"input_tokens": MARKER, "output_tokens": 1}, None),
        ({"input_tokens": 7, "output_tokens": 3, "total_tokens": 11}, None),
        ({"input_tokens": MAX_TOKEN_COUNT + 1, "output_tokens": 0}, None),
        ({"input_tokens": MAX_TOKEN_COUNT, "output_tokens": 1}, None),
        (
            {"input_tokens": 7, "output_tokens": 3, **{f"k{i}": MARKER for i in range(15)}},
            None,
        ),
    ],
)
async def test_usage_retains_only_valid_bounded_counters(exchange, usage, expected):
    error = await rejected(exchange, {"status": "failed", "usage": usage})
    assert error.diagnostic.get("usage") == expected


async def test_private_payload_and_upstream_identifiers_never_enter_exception(exchange):
    error = await rejected(
        exchange,
        {
            "status": "failed",
            "id": MARKER,
            "request_id": MARKER,
            "error": {"code": "server_error", "message": MARKER, "param": MARKER},
            "output": [{"type": "message", "content": [{"text": MARKER}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2, "private": MARKER},
        },
    )
    first = error.diagnostic
    first.update(status=MARKER, raw=MARKER)
    first["usage"]["input_tokens"] = True
    assert error.diagnostic["status"] == "failed"
    assert error.diagnostic["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert MARKER not in str(error) + repr(error)


async def test_concurrent_calls_on_shared_provider_keep_distinct_request_metadata():
    both = asyncio.Event()
    calls = []

    async def send(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 2:
            both.set()
        await both.wait()
        first = body["input"][-1]["content"] == "Public first453"
        return httpx.Response(
            200,
            json={
                "status": "failed" if first else "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        )

    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-key",
        allow_cloud=True,
        transport=httpx.MockTransport(send),
    )
    try:
        async with asyncio.timeout(3):
            results = await asyncio.gather(
                *(
                    provider.complete([{"role": "user", "content": text}])
                    for text in ("Public first453", "Public second453")
                ),
                return_exceptions=True,
            )
        assert all(type(error) is MetaResponseError for error in results)
        assert [error.diagnostic["status"] for error in results] == ["failed", "incomplete"]
        assert [error.diagnostic["request_sha256"] for error in results] == [
            request_digest(body) for body in calls
        ]
        assert results[0].diagnostic["request_sha256"] != results[1].diagnostic["request_sha256"]
        assert len(calls) == 2
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "mutation",
    [
        {"raw": MARKER},
        {"status": MARKER},
        {"status": "incomplete", "reason": "not_applicable"},
        {"reason": "max_output_tokens"},
        {"request_sha256": MARKER},
        {"usage": {"input_tokens": True, "output_tokens": 2}},
        {"usage": {"input_tokens": 3, "output_tokens": 2, "private": MARKER}},
    ],
)
async def test_corrupt_optional_diagnostic_is_rejected_with_constant_error(exchange, mutation):
    error = await rejected(exchange, {"status": "failed"})
    corrupt = {**copy.deepcopy(error.diagnostic), **mutation}
    with pytest.raises(ValueError, match="^Invalid Meta response diagnostic$"):
        validate_meta_diagnostic(corrupt)


@pytest.mark.parametrize(
    "event,message",
    [
        ("response.failed", "Meta streaming response failed"),
        ("response.incomplete", "Meta streaming response did not complete"),
        ("error", "Meta streaming response error"),
    ],
)
async def test_stream_status_is_distinct_without_private_text_or_invented_budget(event, message):
    provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-key",
        allow_cloud=True,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, text="data: " + json.dumps({"type": event, "message": MARKER}) + "\n\n"
            )
        ),
    )
    try:
        with pytest.raises(ProviderError) as raised:
            _ = [part async for part in provider.stream([])]
        assert str(raised.value) == message
        assert "budget" not in str(raised.value)
        assert MARKER not in str(raised.value)
    finally:
        await provider.close()
