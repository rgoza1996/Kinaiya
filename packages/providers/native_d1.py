"""Bounded native SystemOne text decisions, with no model discovery or fallback."""

import asyncio
import json
import math
import re
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
from packages.providers.prompt_budget import PromptCountUnavailable
from packages.providers.text import ProviderError, ProviderRefusal, _OwnedHTTPStream

MAX_REQUEST_BYTES = 262144
MAX_RESPONSE_BYTES = 128 * 1024
REASON_CODE = "native_d1_typed_choices"
_PATH = "v1/systemone?autoload=false"
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", re.ASCII)
_MAX_JSON_DEPTH = 512


def _probability(value):
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def _endpoint(base_url):
    if (
        type(base_url) is not str
        or not base_url
        or any(ch.isspace() or ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in base_url)
        or any(ch in base_url for ch in "\\?#")
    ):
        raise ProviderError("Native d1 requires an explicit loopback root endpoint")
    try:
        parsed = urlsplit(base_url)
        host, port = parsed.hostname, parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or host not in {"localhost", "127.0.0.1", "::1"}
            or not host.isascii()
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65535
            or re.fullmatch(
                r"(?:localhost|127\.0\.0\.1|\[::1\]):[0-9]+",
                parsed.netloc,
                flags=re.ASCII | re.IGNORECASE,
            )
            is None
            or parsed.path not in {"", "/"}
        ):
            raise ValueError
    except ValueError:
        raise ProviderError("Native d1 requires an explicit loopback root endpoint") from None
    return base_url.rstrip("/") + "/"


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def _constant(_value):
    raise ValueError


def _json_object(raw, maximum):
    """Bound nesting before the recursive parser, then inspect all ignored fields too."""
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= maximum:
            raise ValueError
        text = raw.decode("utf-8", errors="strict")
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > _MAX_JSON_DEPTH:
                    raise ValueError
            elif char in "]}":
                depth -= 1
                if depth < 0:
                    raise ValueError
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        if type(value) is not dict:
            raise ValueError
        pending = [value]
        while pending:
            item = pending.pop()
            if type(item) is dict:
                pending.extend(item.keys())
                pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
            elif type(item) is str:
                item.encode("utf-8", errors="strict")
            elif type(item) is float and not math.isfinite(item):
                raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ProviderRefusal("Native d1 requires a bounded strict UTF-8 JSON object") from None


def _wire(body):
    try:
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8", errors="strict"
        )
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ProviderRefusal("Native d1 request is not bounded UTF-8 JSON") from None
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProviderRefusal("Native d1 request exceeded the bounded size")
    return raw


class NativeD1RoutingProvider:
    """Local typed classifier; its confidence floor is an owner refusal policy."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        min_confidence: float,
        api_key: str = "",
        allow_cloud: bool = False,
        timeout: float = 5,
        transport=None,
    ):
        origin = _endpoint(base_url)
        if type(model) is not str or _MODEL.fullmatch(model) is None:
            raise ProviderError("Native d1 requires a canonical native model alias")
        if allow_cloud is not False:
            raise ProviderError("Native d1 currently permits only local endpoints")
        if not _probability(min_confidence):
            raise ProviderError("Native d1 requires a finite minimum confidence from zero to one")
        if (
            type(timeout) not in (int, float)
            or not 0 < timeout <= 120
            or not math.isfinite(timeout)
        ):
            raise ProviderError("Native d1 requires a bounded positive timeout")
        try:
            if (
                type(api_key) is not str
                or any(ch in api_key for ch in "\0\r\n")
                or len(api_key.encode("utf-8", errors="strict")) > 16384
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ProviderError("Native d1 credential is malformed or oversized") from None
        self.base_url, self.model = base_url, model
        self.min_confidence, self.allow_cloud, self.timeout = min_confidence, False, timeout
        self.info = ProviderInfo(provider="native_d1", model=model, local=True, streaming=False)
        headers = [(b"Accept-Encoding", b"identity"), (b"Content-Type", b"application/json")]
        if api_key:
            headers.append((b"Authorization", b"Bearer " + api_key.encode("utf-8")))
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
            # A cancelled facade cannot replace or retry the original dependency close.
            self._cleanup = asyncio.create_task(self._original_http.aclose())
            self._cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self._cleanup)

    def stream(self, messages):
        raise ProviderError("Native d1 routing does not stream dialogue")

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
            raise ProviderRefusal("Native d1 needs exactly system and user text messages")
        raw = messages[1]["content"]
        if len(raw) > MAX_TOTAL_CHARS:
            raise ProviderRefusal("Native d1 input exceeded the bounded size")
        try:
            messages[0]["content"].encode("utf-8", errors="strict")
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ProviderRefusal("Native d1 input must be strict UTF-8 text") from None
        _json_object(encoded, MAX_TOTAL_CHARS * 4)
        # Only this pure projection and rendering/question helpers are shared.
        # No legacy client, response parser or fabricated Laya proof is involved.
        try:
            return LayaRoutingProvider._parse_messages(self, messages)
        except ProviderRefusal:
            raise ProviderRefusal("Native d1 input violates the bounded routing grammar") from None

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
                        provider="native_d1", model=body["model"], local=True, streaming=False
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
                raise ProviderRefusal("Native d1 capture or transport binding changed")

        check()
        # The shared transport helper freezes HTTPX's actual body. Refuse any
        # codec drift rather than capturing bytes other than those dispatched.
        if client.build_request("POST", _PATH, json=body).content != wire:
            raise ProviderRefusal("Native d1 HTTP encoding does not match its frozen request")
        await capture_request(self, body, "native_d1_systemone")
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

        try:
            async with asyncio.timeout(self.timeout):
                async with complete_stream(
                    client, _PATH, body, check, _OwnedHTTPStream
                ) as response:
                    if not 200 <= response.status_code < 300:
                        await capture_error_response(response, MAX_RESPONSE_BYTES)
                        raise ProviderError("Native d1 endpoint returned an HTTP error")
                    if response.headers.get("content-encoding", "").strip().lower() not in {
                        "",
                        "identity",
                    }:
                        raise ProviderRefusal("Native d1 requires an identity-encoded response")
                    raw = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=4096):
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ProviderError("Native d1 response exceeded the bounded size")
                        raw.extend(chunk)
                    capture_response(bytes(raw))
        except asyncio.CancelledError:
            raise
        except PromptCountUnavailable:
            raise ProviderRefusal("Native d1 HTTP request binding changed") from None
        except TimeoutError:
            raise ProviderError("Native d1 request exceeded its total deadline") from None
        except httpx.HTTPError:
            raise ProviderError("Native d1 endpoint is unavailable") from None
        return _json_object(bytes(raw), MAX_RESPONSE_BYTES)

    def _map_answers(self, payload, state, mode, questions, *, model, min_confidence):
        if set(payload) != {"model", "answers", "usage"} or payload["model"] != model:
            raise ProviderRefusal("Native d1 response shape or model identity did not match")
        usage = payload["usage"]
        if (
            type(usage) is not dict
            or set(usage) != {"input_tokens", "output_tokens"}
            or type(usage["input_tokens"]) is not int
            or not 1 <= usage["input_tokens"] <= 9007199254740991
            or type(usage["output_tokens"]) is not int
            or usage["output_tokens"] != 0
        ):
            raise ProviderRefusal("Native d1 token usage was malformed")
        answers = payload["answers"]
        if type(answers) is not dict or set(answers) != set(questions):
            raise ProviderRefusal("Native d1 answer questions did not match")
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
            raise ProviderRefusal("Native d1 request questions did not match")
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
                raise ProviderRefusal("Native d1 answer was malformed")
            probabilities, choice = entry["probabilities"], entry["choice"]
            selected = probabilities[choice]
            if (
                abs(sum(probabilities.values()) - 1) > len(labels) * 0.0001 + 1e-6
                or selected + 0.0001 < max(probabilities.values())
                or abs(entry["confidence"] - selected) > 0.001
            ):
                raise ProviderRefusal("Native d1 answer probabilities were inconsistent")
            picked[name], confidence[name] = choice, selected
        action = picked["agent_route" if mode == "window" else "route"]
        required = ["conversation", "agent_route"] if mode == "window" else ["route"]
        if action != "conversation" and state["tasks"]:
            required.append("target")
        if any(confidence[name] < min_confidence for name in required):
            raise ProviderRefusal("Native d1 answer was below the owner confidence floor")
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
