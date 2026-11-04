"""Deterministic unit tests for the role-limited OpenRouter Decisions adapter."""

import copy
import json

import httpx
import pytest

from packages.providers.laya import LayaRoutingProvider, _finalized_questions, _window_questions
from packages.providers.openrouter_decisions import (
    OPENROUTER_DECISIONS_MODELS,
    OpenRouterDecisionsProvider,
)
from packages.providers.text import ProviderError, ProviderRefusal

MODEL = "typesafe/jev-1.14"
LABELS = {
    "conversation": ["backchannel", "unfinished", "urgent", "ready"],
    "agent_route": ["conversation", "correction", "cancellation", "new_task"],
    "route": ["conversation", "new_task", "correction", "cancellation"],
    "target": ["NONE", "t0", "t1"],
}


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


def payload(
    mode="window",
    *,
    model=MODEL,
    route="correction",
    conversation="ready",
    target="t1",
    confidence=0.97,
    include_id=True,
):
    spec = OPENROUTER_DECISIONS_MODELS[model]
    picks = (
        {"conversation": conversation, "agent_route": route, "target": target}
        if mode == "window"
        else {"route": route, "target": target}
    )
    body = {
        "model": spec["allowed_response_identities"][-1],
        "provider": spec["provider_name"],
        "answers": {
            key: {
                "type": "choice",
                "choice": choice,
                "probabilities": {
                    label: (0.98 if label == choice else round(0.02 / (len(LABELS[key]) - 1), 6))
                    for label in LABELS[key]
                },
                "confidence": confidence,
            }
            for key, choice in picks.items()
        },
        "usage": {"input_tokens": 43, "output_tokens": 12, "cost": 0.00001806},
    }
    for answer in body["answers"].values():
        probs = answer["probabilities"]
        labels = list(probs)
        probs[labels[0]] = round(1.0 - sum(probs[k] for k in labels[1:]), 6)
    if include_id:
        body["id"] = "gen-synthetic-decisions-001"
    return body


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
    return OpenRouterDecisionsProvider(
        kwargs.pop("base_url", "https://openrouter.ai/api/alpha"),
        kwargs.pop("model", MODEL),
        api_key=kwargs.pop("api_key", "sk-or-v1-public-synthetic-key"),
        allow_cloud=kwargs.pop("allow_cloud", True),
        min_confidence=kwargs.pop("min_confidence", 0.5),
        transport=httpx.MockTransport(wire),
        **kwargs,
    )


@pytest.mark.parametrize("mode", ["window", "finalized"])
@pytest.mark.parametrize("model_name", sorted(OPENROUTER_DECISIONS_MODELS))
async def test_all_catalog_models_send_pinned_provider_preferences_and_parse_responses(
    mode, model_name
):
    wire = Wire(payload(mode, model=model_name))
    adapter = provider(wire, model=model_name)
    try:
        assert adapter.info.model_dump() == {
            "schema_version": "1.0",
            "provider": "openrouter_decisions",
            "model": model_name,
            "local": False,
            "streaming": False,
        }
        health = await adapter.health()
        assert health == {
            "ready": False,
            "readiness": "unknown",
            "qualification": "not_checked",
            "model": model_name,
        }
        assert wire.requests == []
        source = messages(mode)
        raw_out = await adapter.complete(source)
        result = json.loads(raw_out)
        assert len(wire.requests) == 1
        request = wire.requests[0]
        assert request.method == "POST"
        assert request.url.path == "/api/alpha/decisions"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["content-type"].split(";")[0] == "application/json"
        assert request.headers["authorization"] == "Bearer sk-or-v1-public-synthetic-key"
        state = json.loads(source[1]["content"])
        projected = copy.deepcopy(state)
        for task in projected["tasks"]:
            task.pop("objective", None)
        spec = OPENROUTER_DECISIONS_MODELS[model_name]
        expected = {
            "model": model_name,
            "state": LayaRoutingProvider._render_state(projected),
            "questions": (_window_questions if mode == "window" else _finalized_questions)(
                projected["tasks"]
            ),
            "provider": {
                "only": [spec["provider_tag"]],
                "allow_fallbacks": False,
                "max_price": {
                    "prompt": spec["max_price_prompt"],
                    "completion": 0,
                    "request": 0,
                },
            },
        }
        assert json.loads(request.content) == expected
        assert request.content == compact(expected)
        assert b"SECRET-OBJECTIVE" not in request.content
        assert result["target_task_id"] == "task-beta"
        assert result["expected_objective_revision"] == 7
        assert result["reason_code"] == "openrouter_decisions_typed_choices"
        if mode == "window":
            assert result["conversation"] == "RESPOND_WITHOUT_INTERRUPTING_USER"
            assert result["agent_route"] == "STEER_CURRENT_TASK"
        else:
            assert result["route"] == "steer"
            assert result["confidence"] == 0.97
        with pytest.raises(ProviderError):
            adapter.stream(source)
    finally:
        await adapter.close()
    assert adapter.http.is_closed


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"allow_cloud": False},
        {"allow_cloud": 1},
        {"api_key": ""},
        {"api_key": "   "},
        {"api_key": "bad\nkey"},
        {"model": "unknown/model"},
        {"base_url": "http://openrouter.ai/api/alpha"},
        {"base_url": "https://openrouter.ai/v1"},
        {"base_url": "https://evil.example/api/alpha"},
        {"base_url": "https://openrouter.ai/api/alpha?foo=bar"},
        {"min_confidence": -0.01},
        {"min_confidence": 1.01},
        {"timeout": 0},
        {"timeout": 121},
    ],
)
def test_constructor_enforces_origin_cloud_credential_model_and_bounds(bad_kwargs):
    with pytest.raises(ProviderError):
        provider(Wire(payload()), **bad_kwargs)


async def test_confidence_floor_and_response_identity_fences():
    high_floor = provider(Wire(payload("finalized", confidence=0.75)), min_confidence=0.8)
    try:
        with pytest.raises(ProviderRefusal, match="below the owner confidence floor"):
            await high_floor.complete(messages("finalized"))
    finally:
        await high_floor.close()

    wrong_provider_payload = payload("finalized")
    wrong_provider_payload["provider"] = "UnexpectedProvider"
    wrong_provider = provider(Wire(wrong_provider_payload))
    try:
        with pytest.raises(ProviderRefusal, match="model or provider identity did not match"):
            await wrong_provider.complete(messages("finalized"))
    finally:
        await wrong_provider.close()
