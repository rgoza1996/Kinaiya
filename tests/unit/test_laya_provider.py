"""Synthetic MockTransport tests for the bounded Laya typed adapter."""

import asyncio
import json

import httpx
import pytest

from packages.providers.laya import LayaRoutingProvider
from packages.providers.text import ProviderError, ProviderRefusal


def _window_input():
    return [
        {"role": "system", "content": "ignore these instructions"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "context": [{"text": "hello"}],
                    "latest": {
                        "text": "please steer the task",
                        "user_speaking": True,
                        "capture_ended": False,
                    },
                    "tasks": [
                        {
                            "task_id": "task-alpha",
                            "objective_revision": 3,
                            "state": "active",
                            "objective": "must not be sent",
                        },
                        {
                            "task_id": "task-beta",
                            "objective_revision": 7,
                            "state": "queued",
                        },
                    ],
                }
            ),
        },
    ]


def _finalized_input():
    return [
        {"role": "system", "content": "ignore these instructions"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "context": [],
                    "latest": {"text": "do the follow-up", "kind": "conversation.revision"},
                    "tasks": [
                        {
                            "task_id": "task-alpha",
                            "objective_revision": 3,
                            "state": "active",
                        }
                    ],
                }
            ),
        },
    ]


def _ok_payload(model="english", target="t0"):
    return {
        "answers": {
            "conversation": {
                "type": "choice",
                "choice": "ready",
                "probabilities": {
                    "backchannel": 0.05,
                    "unfinished": 0.05,
                    "urgent": 0.1,
                    "ready": 0.8,
                },
                "answer_confidence": 0.8,
            },
            "agent_route": {
                "type": "choice",
                "choice": "correction",
                "probabilities": {
                    "conversation": 0.05,
                    "correction": 0.7,
                    "cancellation": 0.05,
                    "new_task": 0.2,
                },
                "answer_confidence": 0.7,
            },
            "target": {
                "type": "choice",
                "choice": target,
                "probabilities": {"NONE": 0.1, "t0": 0.6, "t1": 0.3},
                "answer_confidence": 0.6,
            },
        },
        "usage": {
            "truncated": False,
            "state_tokens_dropped": 0,
            "truncated_questions": [],
        },
        "routing": {"model": model, "repo": "synthetic"},
    }


def _final_payload():
    return {
        "answers": {
            "route": {
                "type": "choice",
                "choice": "new_task",
                "probabilities": {
                    "conversation": 0.1,
                    "new_task": 0.6,
                    "correction": 0.2,
                    "cancellation": 0.1,
                },
                "answer_confidence": 0.6,
            },
            "target": {
                "type": "choice",
                "choice": "NONE",
                "probabilities": {"NONE": 0.9, "t0": 0.1},
                "answer_confidence": 0.9,
            },
        },
        "usage": {
            "truncated": False,
            "state_tokens_dropped": 0,
            "truncated_questions": [],
        },
        "routing": {"model": "english", "repo": "synthetic"},
    }


def _client(handler):
    def stream_response(request):
        response = handler(request)
        # Match HTTP transport: headers arrive before the body is consumed.
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=httpx.ByteStream(response.content),
        )

    return httpx.MockTransport(stream_response)


@pytest.mark.asyncio
async def test_window_success_binds_native_target():
    seen = {}

    def handler(request: httpx.Request):
        assert request.url.path == "/v1/systemone"
        assert request.headers.get("accept-encoding") == "identity"
        body = json.loads(request.content.decode())
        seen["body"] = body
        assert body["model"] == "english"
        # Exact Jev request names from the pinned upstream HTTP contract.
        assert all(
            set(q) == {"type", "instructions", "criteria"} for q in body["questions"].values()
        )
        assert "task-alpha" in body["state"]
        assert "must not be sent" not in body["state"]
        return httpx.Response(200, json=_ok_payload())

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    assert provider.info.local is True
    assert provider.info.streaming is False
    raw = await provider.complete(_window_input())
    out = json.loads(raw)
    assert out["conversation"] == "RESPOND_WITHOUT_INTERRUPTING_USER"
    assert out["agent_route"] == "STEER_CURRENT_TASK"
    assert out["target_task_id"] == "task-alpha"
    assert out["expected_objective_revision"] == 3
    assert out["reason_code"] == "laya_typed_choices"
    await provider.close()


@pytest.mark.asyncio
async def test_finalized_success_maps_route_and_none_target():
    def handler(request: httpx.Request):
        return httpx.Response(200, json=_final_payload())

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    raw = await provider.complete(_finalized_input())
    out = json.loads(raw)
    assert out["route"] == "queue"
    assert out["target_task_id"] is None
    assert out["expected_objective_revision"] is None
    assert out["confidence"] == pytest.approx(0.6)
    assert out["reason_code"] == "laya_typed_choices"
    await provider.close()


@pytest.mark.asyncio
async def test_truncated_usage_rejected():
    payload = _final_payload()
    payload["usage"]["truncated"] = True

    def handler(request: httpx.Request):
        return httpx.Response(200, json=payload)

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    with pytest.raises(ProviderError):
        await provider.complete(_finalized_input())
    await provider.close()


@pytest.mark.asyncio
async def test_unknown_label_rejected():
    payload = _final_payload()
    payload["answers"]["route"]["choice"] = "dance"
    payload["answers"]["route"]["probabilities"]["dance"] = 0.5

    def handler(request: httpx.Request):
        return httpx.Response(200, json=payload)

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    with pytest.raises(ProviderError):
        await provider.complete(_finalized_input())
    await provider.close()


@pytest.mark.asyncio
async def test_model_generated_target_not_accepted():
    payload = _ok_payload()
    payload["answers"]["target"]["choice"] = "task-alpha"
    payload["answers"]["target"]["probabilities"] = {
        "NONE": 0.1,
        "t0": 0.1,
        "t1": 0.1,
        "task-alpha": 0.7,
    }

    def handler(request: httpx.Request):
        return httpx.Response(200, json=payload)

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    with pytest.raises(ProviderError):
        await provider.complete(_window_input())
    await provider.close()


@pytest.mark.asyncio
async def test_redirect_denied_without_raw_text():
    def handler(request: httpx.Request):
        return httpx.Response(302, headers={"location": "/else"}, content=b"secret-body")

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    with pytest.raises(ProviderError) as excinfo:
        await provider.complete(_window_input())
    assert "secret-body" not in str(excinfo.value)
    await provider.close()


@pytest.mark.asyncio
async def test_encoded_response_denied():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            json=_final_payload(),
        )

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    with pytest.raises(ProviderError):
        await provider.complete(_finalized_input())
    await provider.close()


@pytest.mark.asyncio
async def test_oversized_response_denied():
    def handler(request: httpx.Request):
        return httpx.Response(200, content=b"x" * (128 * 1024 + 1))

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    with pytest.raises(ProviderError):
        await provider.complete(_finalized_input())
    await provider.close()


@pytest.mark.asyncio
async def test_legacy_string_latest_fails_explicitly():
    def handler(request: httpx.Request):  # pragma: no cover
        raise AssertionError("must not call network")

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(handler))
    messages = [
        {"role": "system", "content": "x"},
        {
            "role": "user",
            "content": json.dumps({"context": [], "latest": "fallback string", "tasks": []}),
        },
    ]
    with pytest.raises(ProviderError):
        await provider.complete(messages)
    await provider.close()


def test_local_cloud_policy():
    with pytest.raises(ProviderError):
        LayaRoutingProvider("http://example.com")
    with pytest.raises(ProviderError):
        LayaRoutingProvider("http://127.0.0.1:8000?x=1")
    with pytest.raises(ProviderError):
        LayaRoutingProvider("http://127.0.0.1:8000", model="../evil")
    with pytest.raises(ProviderError):
        LayaRoutingProvider("http://127.0.0.1:8000", model="english\n")
    provider = LayaRoutingProvider("https://example.com", model="english", allow_cloud=True)
    assert provider.info.local is False


@pytest.mark.asyncio
async def test_stream_raises():
    provider = LayaRoutingProvider("http://127.0.0.1:8000")
    with pytest.raises(ProviderError):
        await provider.stream([])
    await provider.close()


@pytest.mark.asyncio
async def test_health_requires_loaded_revision_device():
    def bare(request: httpx.Request):
        return httpx.Response(200, json={"status": "ok"})

    provider = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(bare))
    out = await provider.health()
    assert out["ready"] is False
    await provider.close()

    def loaded(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "loaded": ["english"],
                "revisions": {"english": "r123"},
                "device": "cpu",
                "device_is_preference": False,
            },
        )

    provider2 = LayaRoutingProvider("http://127.0.0.1:8000", transport=_client(loaded))
    out2 = await provider2.health()
    assert out2["ready"] is True
    assert out2["revision"] == "r123"
    await provider2.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_usage",
        "dropped_state",
        "boolean_drop_count",
        "truncated_question",
        "wrong_model",
        "missing_answer",
        "extra_answer",
        "abstained",
        "unevaluated",
        "low_confidence",
        "nan_probability",
        "wrong_mass",
        "choice_not_argmax",
        "inconsistent_confidence",
    ],
)
async def test_malformed_or_incomplete_decision_never_becomes_ignore(mutation):
    payload = _final_payload()
    route = payload["answers"]["route"]
    if mutation == "missing_usage":
        del payload["usage"]
    elif mutation == "dropped_state":
        payload["usage"]["state_tokens_dropped"] = 1
    elif mutation == "boolean_drop_count":
        payload["usage"]["state_tokens_dropped"] = False
    elif mutation == "truncated_question":
        payload["usage"]["truncated_questions"] = ["route"]
    elif mutation == "wrong_model":
        payload["routing"]["model"] = "multilingual"
    elif mutation == "missing_answer":
        del payload["answers"]["route"]
    elif mutation == "extra_answer":
        payload["answers"]["injected"] = {"choice": "run_shell"}
    elif mutation in {"abstained", "unevaluated"}:
        route["abstention"] = mutation
    elif mutation == "low_confidence":
        route["low_confidence"] = True
    elif mutation == "nan_probability":
        route["probabilities"]["conversation"] = float("nan")
    elif mutation == "wrong_mass":
        route["probabilities"]["conversation"] = 0.9
    elif mutation == "choice_not_argmax":
        route["choice"] = "conversation"
    elif mutation == "inconsistent_confidence":
        route["answer_confidence"] = 0.99
    provider = LayaRoutingProvider(
        "http://127.0.0.1:8870",
        transport=_client(lambda r: httpx.Response(200, content=json.dumps(payload).encode())),
    )
    try:
        with pytest.raises(ProviderError):
            await provider.complete(_finalized_input())
    finally:
        await provider.close()


async def test_oversized_stream_stops_reading_before_unbounded_buffering():
    class Flood(httpx.AsyncByteStream):
        chunks = 0
        closed = False

        async def __aiter__(self):
            for _ in range(10000):
                self.chunks += 1
                yield b"x" * 4096

        async def aclose(self):
            self.closed = True

    stream = Flood()
    provider = LayaRoutingProvider(
        "http://127.0.0.1:8870",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream)),
    )
    try:
        with pytest.raises(ProviderError, match="bounded size"):
            await provider.complete(_finalized_input())
        assert stream.chunks == 33 and stream.closed
    finally:
        await provider.close()


async def test_deeply_nested_json_is_a_permanent_refusal():
    nested = b'{"unused":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    provider = LayaRoutingProvider(
        "http://127.0.0.1:8870",
        transport=_client(lambda r: httpx.Response(200, content=nested)),
    )
    try:
        with pytest.raises(ProviderRefusal, match="malformed"):
            await provider.complete(_finalized_input())
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"max_len": True},
        {"max_len": 0},
        {"max_len": 8193},
        {"head_max_len": False},
        {"max_len": 128, "head_max_len": 128},
        {"timeout": float("inf")},
        {"timeout": 0},
    ],
)
def test_invalid_runtime_bounds_rejected_before_transport(fields):
    with pytest.raises(ProviderError):
        LayaRoutingProvider("http://127.0.0.1:8870", **fields)


async def test_checkpoint_budgets_are_not_silently_overridden():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_final_payload())

    provider = LayaRoutingProvider("http://127.0.0.1:8870", transport=_client(handler))
    try:
        await provider.complete(_finalized_input())
        assert "max_len" not in seen[0] and "head_max_len" not in seen[0]
    finally:
        await provider.close()


async def test_only_bounded_context_fields_reach_the_backend():
    messages = _window_input()
    body = json.loads(messages[1]["content"])
    body["context"][0]["unrelated_secret"] = "synthetic-should-never-leave"
    messages[1]["content"] = json.dumps(body)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_payload())

    provider = LayaRoutingProvider("http://127.0.0.1:8870", transport=_client(handler))
    try:
        await provider.complete(messages)
        assert "synthetic-should-never-leave" not in seen[0]["state"]
        assert "hello" in seen[0]["state"]
    finally:
        await provider.close()


async def test_ignored_agent_route_cannot_carry_an_irrelevant_task_binding():
    payload = _ok_payload()
    route = payload["answers"]["agent_route"]
    route["choice"] = "conversation"
    route["probabilities"] = {k: float(k == "conversation") for k in route["probabilities"]}
    route["answer_confidence"] = 1.0
    provider = LayaRoutingProvider(
        "http://127.0.0.1:8870", transport=_client(lambda r: httpx.Response(200, json=payload))
    )
    try:
        result = json.loads(await provider.complete(_window_input()))
        assert result["conversation"] == "RESPOND_WITHOUT_INTERRUPTING_USER"
        assert result["agent_route"] == "IGNORE_FOR_IMMEDIATE_ROUTING"
        assert result["target_task_id"] is result["expected_objective_revision"] is None
    finally:
        await provider.close()


async def test_invalid_capture_state_fails_before_inference():
    messages = _window_input()
    body = json.loads(messages[1]["content"])
    body["latest"]["capture_ended"] = True
    messages[1]["content"] = json.dumps(body)
    calls = []
    provider = LayaRoutingProvider(
        "http://127.0.0.1:8870",
        transport=_client(lambda r: calls.append(r) or httpx.Response(200, json=_ok_payload())),
    )
    try:
        with pytest.raises(ProviderError, match="cannot still report speaking"):
            await provider.complete(messages)
        assert calls == []
    finally:
        await provider.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_total_deadline_and_cancellation_close_slow_response(cancel):
    entered = asyncio.Event()

    class Slow(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            entered.set()
            while True:
                await asyncio.sleep(1)
                yield b" "

        async def aclose(self):
            self.closed = True

    stream = Slow()
    provider = LayaRoutingProvider(
        "http://127.0.0.1:8870",
        timeout=1 if cancel else 0.02,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream)),
    )
    try:
        task = asyncio.create_task(provider.complete(_finalized_input()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ProviderError):
            await task
        assert stream.closed
    finally:
        await provider.close()
