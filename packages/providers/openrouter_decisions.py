"""Bounded OpenRouter Decisions adapter for typed SystemOne choice classification.

Strictly role-limited to Decisions; never used for Converse dialogue or Agent
execution. Preserves pinned provider preferences, price ceilings, response
identity checks, and task-local inspection capture without model-accuracy
promotion gates.
"""

from __future__ import annotations

import asyncio
import json
import math
from urllib.parse import urlsplit

import httpx

from packages.contracts.models import ProviderInfo
from packages.providers.complete_http import RequestBoundAsyncClient, complete_stream
from packages.providers.laya import (
    AGENT_CHOICES,
    CONVERSATION_CHOICES,
    FINAL_CHOICES,
    MAX_TOTAL_CHARS,
    LayaRoutingProvider,
    _finalized_questions,
    _window_questions,
)
from packages.providers.native_d1 import _json_object, _probability, _wire
from packages.providers.prompt_budget import PromptCountUnavailable
from packages.providers.text import ProviderError, ProviderRefusal, _OwnedHTTPStream

MAX_REQUEST_BYTES = 262144
MAX_RESPONSE_BYTES = 128 * 1024
REASON_CODE = "openrouter_decisions_typed_choices"
_PATH = "decisions"

OPENROUTER_DECISIONS_MODELS: dict[str, dict] = {
    "typesafe/jev-1.14": {
        "provider_tag": "typesafe",
        "provider_name": "TypeSafe",
        "allowed_response_identities": ("typesafe/jev-1.14", "typesafe/jev-1.14-20261014"),
        "max_price_prompt": 0.042,
    },
    "cloudflare/clef-flash": {
        "provider_tag": "deepinfra/fp8",
        "provider_name": "DeepInfra",
        "allowed_response_identities": ("cloudflare/clef-flash",),
        "max_price_prompt": 0.019,
    },
    "microsoft/microsoft-decision-1": {
        "provider_tag": "azure",
        "provider_name": "Azure",
        "allowed_response_identities": (
            "microsoft/microsoft-decision-1",
            "microsoft/microsoft-decision-1-20261009",
        ),
        "max_price_prompt": 0.042,
    },
    "liquid/d1": {
        "provider_tag": "liquid",
        "provider_name": "Liquid",
        "allowed_response_identities": ("liquid/d1", "liquid/d1-20260930"),
        "max_price_prompt": 0.02,
    },
    "baseten/joey-1": {
        "provider_tag": "baseten",
        "provider_name": "BaseTen",
        "allowed_response_identities": ("baseten/joey-1", "baseten/joey-1-20261029"),
        "max_price_prompt": 0.01,
    },
    "cloudflare/clef": {
        "provider_tag": "deepinfra/fp8",
        "provider_name": "DeepInfra",
        "allowed_response_identities": ("cloudflare/clef",),
        "max_price_prompt": 0.192,
    },
    "liquid/d1-ultra": {
        "provider_tag": "liquid",
        "provider_name": "Liquid",
        "allowed_response_identities": ("liquid/d1-ultra", "liquid/d1-ultra-20261015"),
        "max_price_prompt": 0.042,
    },
}


def _validate_origin(base_url: str, timeout: float = 15) -> str:
    if (
        type(base_url) is not str
        or not base_url
        or any(ch.isspace() or ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in base_url)
        or any(ch in base_url for ch in "\\?#")
    ):
        raise ProviderError("OpenRouter decisions requires an explicit OpenRouter HTTPS origin")
    if (
        type(timeout) not in (int, float)
        or type(timeout) is bool
        or not 0 < timeout <= 120
        or not math.isfinite(timeout)
    ):
        raise ProviderError("OpenRouter decisions requires a bounded positive timeout")
    try:
        parsed = urlsplit(base_url)
        host, port = parsed.hostname, parsed.port
        path = parsed.path.rstrip("/")
        if (
            parsed.scheme != "https"
            or host != "openrouter.ai"
            or parsed.netloc not in {"openrouter.ai", "openrouter.ai:443"}
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or path not in {"/api/v1", "/api/alpha"}
        ):
            raise ValueError
    except ValueError:
        raise ProviderError(
            "OpenRouter decisions requires an explicit OpenRouter HTTPS origin"
        ) from None
    return f"https://openrouter.ai{path}/"


class OpenRouterDecisionsProvider:
    """Cloud typed classifier over OpenRouter /decisions with pinned provider routing."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str,
        allow_cloud: bool = False,
        timeout: float = 15,
        min_confidence: float = 0.0,
        transport=None,
    ):
        origin = _validate_origin(base_url, timeout)
        if type(model) is not str or model not in OPENROUTER_DECISIONS_MODELS:
            raise ProviderError("OpenRouter decisions requires a registered catalog model")
        if allow_cloud is not True:
            raise ProviderError("OpenRouter decisions requires explicit cloud authorization")
        if not _probability(min_confidence):
            raise ProviderError(
                "OpenRouter decisions requires a finite minimum confidence from zero to one"
            )
        try:
            if (
                type(api_key) is not str
                or not api_key.strip()
                or any(ch in api_key for ch in "\0\r\n")
                or len(api_key.encode("utf-8", errors="strict")) > 16384
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ProviderError("OpenRouter decisions requires a valid API credential") from None
        self.base_url, self.model = base_url.rstrip("/"), model
        self.min_confidence, self.allow_cloud, self.timeout = min_confidence, True, timeout
        self.info = ProviderInfo(
            provider="openrouter_decisions", model=model, local=False, streaming=False
        )
        headers = [
            (b"Accept-Encoding", b"identity"),
            (b"Content-Type", b"application/json"),
            (b"Authorization", b"Bearer " + api_key.encode("utf-8")),
        ]
        self.http = RequestBoundAsyncClient(
            base_url=origin,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._original_http, self._original_info = self.http, self.info
        self._declaration = self._policy()
        self._headers = tuple(self.http.headers.raw)
        self._origin = str(self.http.base_url)
        self._transport = httpx.AsyncClient._transport_for_url(
            self.http, self.http.base_url.join(_PATH)
        )
        self._cleanup = None

    def _policy(self):
        return tuple(
            (type(value), value)
            for value in (
                self.base_url,
                self.model,
                self.min_confidence,
                self.allow_cloud,
                self.timeout,
            )
        )

    async def close(self):
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._original_http.aclose())
            self._cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self._cleanup)

    def stream(self, messages):
        raise ProviderError("OpenRouter decisions routing does not stream dialogue")

    async def health(self):
        return {
            "ready": False,
            "readiness": "unknown",
            "qualification": "not_checked",
            "model": self._declaration[1][1],
        }

    def _parse_messages(self, messages):
        if (
            type(messages) is not list
            or len(messages) != 2
            or any(type(message) is not dict for message in messages)
            or any(type(message.get("content")) is not str for message in messages)
        ):
            raise ProviderRefusal(
                "OpenRouter decisions needs exactly system and user text messages"
            )
        raw = messages[1]["content"]
        if len(raw) > MAX_TOTAL_CHARS:
            raise ProviderRefusal("OpenRouter decisions input exceeded the bounded size")
        try:
            messages[0]["content"].encode("utf-8", errors="strict")
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ProviderRefusal("OpenRouter decisions input must be strict UTF-8 text") from None
        _json_object(encoded, MAX_TOTAL_CHARS * 4)
        try:
            return LayaRoutingProvider._parse_messages(self, messages)
        except ProviderRefusal:
            raise ProviderRefusal(
                "OpenRouter decisions input violates the bounded routing grammar"
            ) from None

    @staticmethod
    def _provider_preferences(model: str) -> dict:
        spec = OPENROUTER_DECISIONS_MODELS[model]
        return {
            "only": [spec["provider_tag"]],
            "allow_fallbacks": False,
            "max_price": {
                "prompt": spec["max_price_prompt"],
                "completion": 0,
                "request": 0,
            },
        }

    async def complete(self, messages) -> str:
        from packages.providers.decision_capture import capture_request

        state, mode = self._parse_messages(messages)
        questions = (
            _window_questions(state["tasks"])
            if mode == "window"
            else _finalized_questions(state["tasks"])
        )
        model, min_confidence = self.model, self.min_confidence
        body = {
            "model": model,
            "state": LayaRoutingProvider._render_state(state),
            "questions": questions,
            "provider": self._provider_preferences(model),
        }
        wire = _wire(body)
        client, info = self._original_http, self._original_info
        info_body = info.model_dump()
        hooks = tuple((name, tuple(values)) for name, values in client.event_hooks.items())
        timeout = client.timeout.as_dict()

        def check():
            try:
                unchanged = (
                    self.http is client
                    and self.info is info
                    and info.model_dump() == info_body
                    and info_body
                    == ProviderInfo(
                        provider="openrouter_decisions",
                        model=body["model"],
                        local=False,
                        streaming=False,
                    ).model_dump()
                    and self._policy() == self._declaration
                    and self._cleanup is None
                    and not client.is_closed
                    and tuple(client.headers.raw) == self._headers
                    and str(client.base_url) == self._origin
                    and client.follow_redirects is False
                    and client.auth is None
                    and not client.params
                    and not client.cookies
                    and client.timeout.as_dict() == timeout
                    and tuple((name, tuple(values)) for name, values in client.event_hooks.items())
                    == hooks
                    and httpx.AsyncClient._transport_for_url(client, client.base_url.join(_PATH))
                    is self._transport
                    and _wire(body) == wire
                )
            except (AttributeError, TypeError, ValueError, ProviderError):
                unchanged = False
            if not unchanged:
                raise ProviderRefusal("OpenRouter decisions capture or transport binding changed")

        check()
        if client.build_request("POST", _PATH, json=body).content != wire:
            raise ProviderRefusal(
                "OpenRouter decisions HTTP encoding does not match its frozen request"
            )
        await capture_request(self, body, "laya_systemone")
        check()
        payload = await self._request(client, body, check)
        return json.dumps(
            self._map_answers(
                payload, state, mode, questions, model=model, min_confidence=min_confidence
            ),
            allow_nan=False,
        )

    async def _request(self, client, body, check):
        from packages.providers.decision_capture import capture_error_response, capture_response

        owner = asyncio.current_task()
        cancellations = owner.cancelling()
        deadline = asyncio.timeout(self.timeout)
        try:
            async with deadline:
                async with complete_stream(
                    client, _PATH, body, check, _OwnedHTTPStream
                ) as response:
                    if not 200 <= response.status_code < 300:
                        await capture_error_response(response, MAX_RESPONSE_BYTES)
                        raise ProviderError(
                            f"OpenRouter decisions endpoint returned HTTP {response.status_code}"
                        )
                    if response.headers.get("content-encoding", "").strip().lower() not in {
                        "",
                        "identity",
                    }:
                        raise ProviderRefusal(
                            "OpenRouter decisions requires an identity-encoded response"
                        )
                    raw = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=4096):
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ProviderError(
                                "OpenRouter decisions response exceeded the bounded size"
                            )
                        raw.extend(chunk)
                    capture_response(bytes(raw))
        except asyncio.CancelledError:
            raise
        except PromptCountUnavailable:
            raise ProviderRefusal("OpenRouter decisions HTTP request binding changed") from None
        except TimeoutError:
            raise ProviderError(
                "OpenRouter decisions request exceeded its total deadline"
            ) from None
        except httpx.HTTPError:
            raise ProviderError("OpenRouter decisions endpoint is unavailable") from None
        if owner.cancelling() > cancellations:
            raise asyncio.CancelledError
        if deadline.expired():
            raise ProviderError("OpenRouter decisions request exceeded its total deadline")
        return _json_object(bytes(raw), MAX_RESPONSE_BYTES)

    def _map_answers(self, payload, state, mode, questions, *, model, min_confidence):
        spec = OPENROUTER_DECISIONS_MODELS[model]
        keys = set(payload) - {"id"}
        if keys != {"model", "provider", "answers", "usage"}:
            raise ProviderRefusal("OpenRouter decisions response shape did not match")
        if "id" in payload and (
            type(payload["id"]) is not str or not 1 <= len(payload["id"]) <= 256
        ):
            raise ProviderRefusal("OpenRouter decisions response identifier was malformed")
        if (
            payload["model"] not in spec["allowed_response_identities"]
            or payload["provider"] != spec["provider_name"]
        ):
            raise ProviderRefusal(
                "OpenRouter decisions response model or provider identity did not match"
            )
        usage = payload["usage"]
        if (
            type(usage) is not dict
            or not {"input_tokens", "output_tokens"}
            <= set(usage)
            <= {"input_tokens", "output_tokens", "cost"}
            or type(usage["input_tokens"]) is not int
            or not 1 <= usage["input_tokens"] <= 9007199254740991
            or type(usage["output_tokens"]) is not int
            or not 0 <= usage["output_tokens"] <= 9007199254740991
        ):
            raise ProviderRefusal("OpenRouter decisions token usage was malformed")
        if "cost" in usage and (
            type(usage["cost"]) not in (int, float)
            or type(usage["cost"]) is bool
            or not math.isfinite(usage["cost"])
            or not 0 <= usage["cost"] <= 1000
        ):
            raise ProviderRefusal("OpenRouter decisions reported cost was malformed")
        answers = payload["answers"]
        if type(answers) is not dict or set(answers) != set(questions):
            raise ProviderRefusal("OpenRouter decisions answer questions did not match")
        allowed = (
            {"conversation": CONVERSATION_CHOICES, "agent_route": AGENT_CHOICES}
            if mode == "window"
            else {"route": FINAL_CHOICES}
        )
        if state["tasks"]:
            allowed["target"] = {"NONE": None} | {
                f"t{index}": task for index, task in enumerate(state["tasks"])
            }
        if set(questions) != set(allowed):
            raise ProviderRefusal("OpenRouter decisions request questions did not match")
        picked, confidence = {}, {}
        for name, labels in allowed.items():
            entry = answers[name]
            if (
                type(entry) is not dict
                or set(entry) != {"type", "choice", "probabilities", "confidence"}
                or entry["type"] != "choice"
                or type(entry["choice"]) is not str
                or entry["choice"] not in labels
                or type(entry["probabilities"]) is not dict
                or set(entry["probabilities"]) != set(labels)
                or not all(_probability(value) for value in entry["probabilities"].values())
                or not _probability(entry["confidence"])
            ):
                raise ProviderRefusal("OpenRouter decisions answer was malformed")
            probabilities, choice = entry["probabilities"], entry["choice"]
            selected = probabilities[choice]
            if abs(sum(probabilities.values()) - 1) > len(
                labels
            ) * 0.0001 + 1e-6 or selected + 0.0001 < max(probabilities.values()):
                raise ProviderRefusal("OpenRouter decisions answer probabilities were inconsistent")
            picked[name] = choice
            confidence[name] = float(entry["confidence"])
        action = picked["agent_route" if mode == "window" else "route"]
        required = ["conversation", "agent_route"] if mode == "window" else ["route"]
        if action != "conversation" and state["tasks"]:
            required.append("target")
        if any(
            min(confidence[name], float(answers[name]["probabilities"][picked[name]]))
            < min_confidence
            for name in required
        ):
            raise ProviderRefusal(
                "OpenRouter decisions answer was below the owner confidence floor"
            )
        target = (
            allowed["target"][picked["target"]]
            if action != "conversation" and state["tasks"]
            else None
        )
        result = {
            "target_task_id": target["task_id"] if target is not None else None,
            "expected_objective_revision": target["objective_revision"]
            if target is not None
            else None,
            "reason_code": REASON_CODE,
        }
        if mode == "window":
            return {
                "conversation": CONVERSATION_CHOICES[picked["conversation"]],
                "agent_route": AGENT_CHOICES[action],
                **result,
            }
        return {"route": FINAL_CHOICES[action], "confidence": float(confidence["route"]), **result}
