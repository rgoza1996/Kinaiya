"""Independent frozen native-d1 protocol oracles, no running model/server."""

import copy
import importlib
import json

import httpx
import pytest

from packages.providers.laya import LayaRoutingProvider, _finalized_questions, _window_questions
from packages.providers.text import ProviderError, ProviderRefusal

MODEL = "native/d1-Omni_2B.Q8"
LABELS = {
    "conversation": ["backchannel", "unfinished", "urgent", "ready"],
    "agent_route": ["conversation", "correction", "cancellation", "new_task"],
    "route": ["conversation", "new_task", "correction", "cancellation"],
    "target": ["NONE", "t0", "t1"],
}


def native():
    assert importlib.util.find_spec("packages.providers.native_d1") is not None, (
        "UNIMPLEMENTED native_d1 adapter module, not a behavioral failure"
    )
    return importlib.import_module("packages.providers.native_d1").NativeD1RoutingProvider


def compact(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def messages(mode="window"):
    latest = (
        {"text": "Public café request Ω", "user_speaking": True, "capture_ended": False}
        if mode == "window"
        else {"text": "Public café request Ω", "kind": "conversation.user"}
    )
    return [
        {"role": "system", "content": "Native classification only"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "latest": latest,
                    "context": [{"kind": "conversation.user", "text": "Public context"}],
                    "tasks": [
                        {
                            "task_id": "task-alpha",
                            "objective_revision": 3,
                            "state": "running",
                            "objective": "SECRET-OBJECTIVE-MUST-NOT-EXPORT",
                        },
                        {"task_id": "task-beta", "objective_revision": 7, "state": "queued"},
                    ],
                }
            ),
        },
    ]


def payload(mode="window", *, route="correction", conversation="ready", target="t1"):
    picks = (
        {"conversation": conversation, "agent_route": route, "target": target}
        if mode == "window"
        else {"route": route, "target": target}
    )
    return {
        "model": MODEL,
        "answers": {
            key: {
                "type": "choice",
                "choice": choice,
                "probabilities": {label: float(label == choice) for label in LABELS[key]},
                "confidence": 1.0,
            }
            for key, choice in picks.items()
        },
        "usage": {"input_tokens": 43, "output_tokens": 0},
    }


class Wire:
    def __init__(self, value=None, *, raw=None, status=200, headers=None):
        self.value, self.raw, self.status, self.headers = value, raw, status, headers or {}
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(
            self.status,
            headers=self.headers,
            stream=httpx.ByteStream(self.raw if self.raw is not None else compact(self.value)),
        )


def provider(wire, **kwargs):
    return native()(
        "http://127.0.0.1:8870",
        MODEL,
        min_confidence=kwargs.pop("min_confidence", 0.5),
        transport=httpx.MockTransport(wire),
        **kwargs,
    )


@pytest.mark.parametrize("mode", ["window", "finalized"])
async def test_exact_native_wire_projection_and_unknown_health_without_probe(mode):
    wire = Wire(payload(mode))
    adapter = provider(wire, api_key="public-synthetic-key")
    try:
        assert adapter.info.model_dump() == {
            "provider": "native_d1",
            "model": MODEL,
            "local": True,
            "streaming": False,
        }
        health = await adapter.health()
        assert {key: health[key] for key in ("ready", "readiness", "qualification", "model")} == {
            "ready": False,
            "readiness": "unknown",
            "qualification": "not_checked",
            "model": MODEL,
        }
        assert not {"artifact_sha256", "device", "loaded"}.intersection(health)
        assert wire.requests == []
        source = messages(mode)
        await adapter.complete(source)
        assert len(wire.requests) == 1
        request = wire.requests[0]
        assert request.method == "POST" and request.url.path == "/v1/systemone"
        assert request.url.query == b"autoload=false"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["content-type"].split(";")[0] == "application/json"
        assert request.headers["authorization"] == "Bearer public-synthetic-key"
        state = json.loads(source[1]["content"])
        projected = copy.deepcopy(state)
        for task in projected["tasks"]:
            task.pop("objective", None)
        expected = {
            "model": MODEL,
            "state": LayaRoutingProvider._render_state(projected),
            "questions": (_window_questions if mode == "window" else _finalized_questions)(
                projected["tasks"]
            ),
        }
        assert json.loads(request.content) == expected
        assert request.content == compact(json.loads(request.content))
        assert len(request.content) <= 262144
        assert b"SECRET-OBJECTIVE" not in request.content and "Ω".encode() in request.content
        with pytest.raises(ProviderError):
            adapter.stream(source)
    finally:
        await adapter.close()
    assert adapter.http.is_closed


@pytest.mark.parametrize("mode", ["window", "finalized"])
@pytest.mark.parametrize(
    "route,expected",
    [
        ("conversation", "ignore"),
        ("new_task", "queue"),
        ("correction", "steer"),
        ("cancellation", "interrupt"),
    ],
)
async def test_all_semantic_routes_use_captured_target_and_ignore_clears_it(mode, route, expected):
    wire = Wire(payload(mode, route=route))
    adapter = provider(wire)
    try:
        result = json.loads(await adapter.complete(messages(mode)))
        assert result["agent_route" if mode == "window" else "route"] == (
            {
                "ignore": "IGNORE_FOR_IMMEDIATE_ROUTING",
                "queue": "QUEUE_UNTIL_TASK_DONE",
                "steer": "STEER_CURRENT_TASK",
                "interrupt": "INTERRUPT_CURRENT_TASK",
            }[expected]
            if mode == "window"
            else expected
        )
        assert result["target_task_id"] == (None if expected == "ignore" else "task-beta")
        assert result["expected_objective_revision"] == (None if expected == "ignore" else 7)
        assert result["reason_code"] == "native_d1_typed_choices"
        if mode == "finalized":
            assert result["confidence"] == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("backchannel", "IGNORE"),
        ("unfinished", "WAIT_FOR_NEXT_WINDOW"),
        ("urgent", "RESPOND_INTERRUPT_USER"),
        ("ready", "RESPOND_WITHOUT_INTERRUPTING_USER"),
    ],
)
async def test_four_conversation_timing_labels(choice, expected):
    adapter = provider(Wire(payload(conversation=choice)))
    try:
        assert json.loads(await adapter.complete(messages()))["conversation"] == expected
    finally:
        await adapter.close()


@pytest.mark.parametrize("bad", [True, False, -0.01, 1.01, float("nan"), float("inf"), "0.5", None])
def test_owner_confidence_floor_is_required_strict_finite_numeric(bad):
    with pytest.raises((ProviderError, ValueError)):
        native()("http://127.0.0.1:8870", MODEL, min_confidence=bad)


def test_floor_has_no_implicit_default():
    with pytest.raises(TypeError):
        native()("http://127.0.0.1:8870", MODEL)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.invalid:8870",
        "https://remote.invalid:8870",
        "http://localhost",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://user@127.0.0.1:8870",
        "http://127.0.0.1:8870/v1",
        "http://127.0.0.1:8870/?x=1",
        "http://127.0.0.1:8870/#x",
        " http://127.0.0.1:8870",
        "http://127.0.0.1:8870\\",
        "http://localhоst:8870",
        "http://127.0.0.1:8870\n",
    ],
)
def test_endpoint_must_be_explicit_loopback_root(url):
    with pytest.raises((ProviderError, ValueError)):
        native()(url, MODEL, min_confidence=0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allow_cloud": True},
        {"allow_cloud": 0},
        {"model": ""},
        {"model": "bad alias"},
        {"model": "é"},
        {"model": "a" * 129},
        {"api_key": "x\nsecret"},
        {"api_key": "x\x00secret"},
        {"api_key": "Ω" * 8193},
    ],
)
def test_alias_key_and_exact_false_policy(kwargs):
    arguments = {"model": MODEL, "min_confidence": 0.5, **kwargs}
    with pytest.raises((ProviderError, ValueError)):
        native()("http://127.0.0.1:8870", **arguments)


@pytest.mark.parametrize(
    "fault",
    [
        "extra-top",
        "missing-model",
        "foreign-model",
        "usage-extra",
        "input-bool",
        "input-zero",
        "input-unsafe",
        "output-bool",
        "output-nonzero",
        "missing-question",
        "extra-question",
        "answer-extra",
        "answer-type",
        "unknown-choice",
        "prob-extra",
        "prob-bool",
        "prob-nan",
        "prob-sum",
        "not-argmax",
        "confidence-mismatch",
        "confidence-bool",
        "legacy-confidence",
    ],
)
async def test_native_shape_and_numbers_never_accept_legacy_or_forged_success(fault):
    body = payload()
    answer = body["answers"]["agent_route"]
    if fault == "extra-top":
        body["routing"] = {"model": MODEL}
    elif fault == "missing-model":
        body.pop("model")
    elif fault == "foreign-model":
        body["model"] = "discovered-synonym"
    elif fault == "usage-extra":
        body["usage"]["truncated"] = False
    elif fault.startswith("input-"):
        body["usage"]["input_tokens"] = {
            "input-bool": True,
            "input-zero": 0,
            "input-unsafe": 2**53,
        }[fault]
    elif fault.startswith("output-"):
        body["usage"]["output_tokens"] = fault == "output-bool" or 1
    elif fault == "missing-question":
        body["answers"].pop("target")
    elif fault == "extra-question":
        body["answers"]["invented"] = copy.deepcopy(answer)
    elif fault == "answer-extra":
        answer["abstention"] = "passed"
    elif fault == "answer-type":
        answer["type"] = "noul"
    elif fault == "unknown-choice":
        answer["choice"] = "task-invented"
    elif fault == "prob-extra":
        answer["probabilities"]["invented"] = 0
    elif fault == "prob-bool":
        answer["probabilities"]["conversation"] = False
    elif fault == "prob-nan":
        answer["probabilities"]["conversation"] = float("nan")
    elif fault == "prob-sum":
        answer["probabilities"]["conversation"] = 0.2
    elif fault == "not-argmax":
        answer["choice"], answer["confidence"] = "conversation", 0
    elif fault == "confidence-mismatch":
        answer["confidence"] = 0.5
    elif fault == "confidence-bool":
        answer["confidence"] = True
    elif fault == "legacy-confidence":
        answer["answer_confidence"] = answer.pop("confidence")
    wire = Wire(raw=json.dumps(body, ensure_ascii=False).encode())
    adapter = provider(wire)
    try:
        with pytest.raises(ProviderRefusal):
            await adapter.complete(messages())
        assert len(wire.requests) == 1
    finally:
        await adapter.close()


@pytest.mark.parametrize("raw", [b'{"model":"a","model":"b"}', b"\xff", b"NaN", b"[" * 1200])
async def test_raw_response_ambiguity_utf8_and_depth_refuse(raw):
    adapter = provider(Wire(raw=raw))
    try:
        with pytest.raises(ProviderRefusal):
            await adapter.complete(messages())
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "channel", ["route", "conversation", "agent_route", "target", "ignored-target"]
)
async def test_floor_applies_to_selected_actions_not_irrelevant_target(channel):
    mode = "finalized" if channel == "route" else "window"
    body = payload(mode, route="conversation" if channel == "ignored-target" else "correction")
    answer = body["answers"]["target" if channel == "ignored-target" else channel]
    choice, labels = answer["choice"], answer["probabilities"]
    answer["probabilities"] = {k: 0.7 if k == choice else 0.3 / (len(labels) - 1) for k in labels}
    answer["confidence"] = 0.7
    adapter = provider(Wire(body), min_confidence=0.8)
    try:
        if channel == "ignored-target":
            result = json.loads(await adapter.complete(messages(mode)))
            assert result["target_task_id"] is None
        else:
            with pytest.raises(ProviderRefusal):
                await adapter.complete(messages(mode))
    finally:
        await adapter.close()


@pytest.mark.parametrize("bad", ["duplicate", "nonfinite", "invalid-unicode", "current-overflow"])
async def test_input_is_strict_before_projection_and_network(bad):
    source = messages()
    if bad == "duplicate":
        source[1]["content"] = '{"latest":{},"latest":{}}'
    elif bad == "nonfinite":
        source[1]["content"] = '{"latest":NaN}'
    elif bad == "invalid-unicode":
        source[1]["content"] = source[1]["content"].replace("Public", "\\ud800")
    else:
        state = json.loads(source[1]["content"])
        state["latest"]["text"] = "x" * 16001
        source[1]["content"] = json.dumps(state)
    wire = Wire(payload())
    adapter = provider(wire)
    try:
        with pytest.raises(ProviderRefusal):
            await adapter.complete(source)
        assert wire.requests == []
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "status,headers,raw,refusal",
    [
        (503, {}, b"private backend error", False),
        (302, {"location": "http://other.invalid"}, b"", False),
        (200, {"content-encoding": "gzip"}, b"not a gzip body", True),
        (200, {}, b"x" * (128 * 1024 + 1), False),
    ],
)
async def test_http_overflow_and_encoding_errors_have_no_retry(status, headers, raw, refusal):
    wire = Wire(status=status, headers=headers, raw=raw)
    adapter = provider(wire)
    try:
        with pytest.raises(ProviderRefusal if refusal else ProviderError) as error:
            await adapter.complete(messages())
        if not refusal:
            assert not isinstance(error.value, ProviderRefusal)
        assert len(wire.requests) == 1
    finally:
        await adapter.close()


async def test_old_laya_protocol_is_not_rewritten_to_native_shape():
    native_wire = Wire(payload())
    legacy = LayaRoutingProvider(
        "http://127.0.0.1:8870", "english", transport=httpx.MockTransport(native_wire)
    )
    try:
        with pytest.raises(ProviderRefusal):
            await legacy.complete(messages())
        valid = payload()
        for answer in valid["answers"].values():
            answer["answer_confidence"] = answer.pop("confidence")
        valid.pop("model")
        valid["routing"] = {"model": "english"}
        valid["usage"] = {"truncated": False, "state_tokens_dropped": 0, "truncated_questions": []}
        native_wire.value = valid
        result = json.loads(await legacy.complete(messages()))
        assert result["reason_code"] == "laya_typed_choices"
    finally:
        await legacy.close()


@pytest.mark.parametrize("url,floor", [("https://localhost:443/", 0), ("http://[::1]:8870", 1)])
async def test_explicit_loopback_variants_and_floor_endpoints_are_supported(url, floor):
    wire = Wire(payload())
    adapter = native()(url, MODEL, min_confidence=floor, transport=httpx.MockTransport(wire))
    try:
        assert json.loads(await adapter.complete(messages()))["target_task_id"] == "task-beta"
        assert len(wire.requests) == 1
    finally:
        await adapter.close()


async def test_irrelevant_target_still_requires_structurally_valid_native_answer():
    body = payload(route="conversation")
    body["answers"]["target"]["probabilities"].pop("t0")
    adapter = provider(Wire(body))
    try:
        with pytest.raises(ProviderRefusal):
            await adapter.complete(messages())
    finally:
        await adapter.close()


@pytest.mark.parametrize("fault", ["total", "context-count", "context-entry", "tasks"])
async def test_preserved_old_input_caps_refuse_before_capture_or_network(fault):
    source = messages()
    state = json.loads(source[1]["content"])
    if fault == "total":
        state["unused"] = "x" * 36000
    elif fault == "context-count":
        state["context"] = [{"text": "public", "kind": "conversation.user"}] * 13
    elif fault == "context-entry":
        state["context"][0]["text"] = "x" * 2001
    else:
        state["tasks"] *= 5
    source[1]["content"] = json.dumps(state)
    wire = Wire(payload())
    adapter = provider(wire)
    try:
        with pytest.raises(ProviderRefusal):
            await adapter.complete(source)
        assert wire.requests == []
    finally:
        await adapter.close()


async def test_full_current_latest_is_preserved_at_old_limit():
    source = messages()
    state = json.loads(source[1]["content"])
    state["latest"]["text"] = "Ω" * 15995 + "TAIL!"
    source[1]["content"] = json.dumps(state, ensure_ascii=False)
    wire = Wire(payload())
    adapter = provider(wire)
    try:
        await adapter.complete(source)
        exported = json.loads(wire.requests[0].content)["state"]
        assert json.dumps(state["latest"]["text"], ensure_ascii=False) in exported
        assert len(wire.requests[0].content) <= 262144
    finally:
        await adapter.close()


@pytest.mark.parametrize("mode", ["window", "finalized"])
async def test_non_onehot_probability_at_floor_and_safe_usage_max_are_valid(mode):
    body = payload(mode)
    key = "agent_route" if mode == "window" else "route"
    answer = body["answers"][key]
    answer["probabilities"] = {
        label: 0.7 if label == "correction" else 0.1 for label in LABELS[key]
    }
    answer["confidence"] = 0.7005
    body["usage"]["input_tokens"] = 2**53 - 1
    adapter = provider(Wire(body), min_confidence=0.7)
    try:
        result = json.loads(await adapter.complete(messages(mode)))
        assert result["target_task_id"] == "task-beta"
        if mode == "finalized":
            assert result["confidence"] == 0.7
    finally:
        await adapter.close()
