"""Measured loopback chat adapter; owner-declared endpoint trust, no inference tuning.

The endpoint address, model alias and context limit are explicit owner
configuration. Trust in the endpoint is a declaration, not proof of a model
artifact or server incarnation. This module performs no download, training,
tokenizer inference or alias discovery; it only counts and generates against
the exact frozen request body.
"""

import asyncio
import json
import math
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from packages.contracts.agent_tools import (
    LEGACY_NATIVE_TOOL_ORDER,
    agent_export_permitted,
    capture_agent_export_admission,
    capture_agent_export_check,
    current_agent_tools,
)
from packages.contracts.models import ProviderInfo
from packages.providers.complete_http import RequestBoundAsyncClient, complete_stream
from packages.providers.completion_budget import PreparedComplete
from packages.providers.prompt_budget import (
    MAX_SAFE_COUNT,
    PromptCountUnavailable,
    PromptTokenCount,
    request_digest,
)
from packages.providers.text import (
    CompatibleTextProvider,
    ProviderCleanupError,
    ProviderError,
    ProviderRefusal,
    _iter_compatible_stream,
    _OwnedHTTPStream,
    _reject_compressed_response,
    _validate_complete_payload,
)

_COUNT_PATH = "chat/completions/input_tokens"
_GENERATION_PATH = "chat/completions"
_COUNT_TIMEOUT_S = 10
_REQUEST_MAX_BYTES = 512 * 1024
_COUNT_MAX_BYTES = 4096
_COUNT_OBJECT = "response.input_tokens"

_MEASURE_UNAVAILABLE = "Measured chat prompt measurement is unavailable"
_MEASURE_STALE = "Measured chat prompt measurement is invalid or stale"
_GENERATION_STALE = "Measured chat generation measurement is missing or stale"
_ADMISSION_CHANGED = "Measured chat complete prompt admission changed"
_ADMISSION_INVALID = "Measured chat complete admission is invalid"


@dataclass(frozen=True, repr=False)
class _MeasuredChatTicket:
    client: RequestBoundAsyncClient
    headers_sha256: str
    binding: tuple
    measurement: PromptTokenCount
    transport: object
    mounts: tuple
    configuration: tuple
    source: list
    generation: int


def _configuration_of(client):
    return (
        client.follow_redirects,
        client.trust_env,
        client.timeout.as_dict(),
        client._auth,
        tuple(client.params.multi_items()),
        tuple(
            (
                cookie.domain,
                cookie.path,
                cookie.name,
                cookie.value,
                cookie.secure,
                cookie.expires,
            )
            for cookie in client.cookies.jar
        ),
        tuple((kind, tuple(hooks)) for kind, hooks in sorted(client.event_hooks.items())),
    )


class MeasuredChatProvider(CompatibleTextProvider):
    """Counted loopback OpenAI-compatible chat adapter (phase 1)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        context_tokens,
        api_key: str = "",
        max_tokens: int = 2048,
        reasoning_effort=None,
        timeout=90,
        transport=None,
    ):
        try:
            parsed = urlparse(base_url)
            if (
                type(base_url) is not str
                or parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username is not None
                or parsed.password is not None
                or any(char.isspace() or ord(char) < 32 for char in base_url)
                or any(char in base_url for char in "\\?#")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("endpoint")
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProviderError(
                "Measured chat requires an explicit loopback HTTP(S) endpoint"
            ) from exc
        if type(model) is not str or not 1 <= len(model) <= 256 or "\0" in model:
            raise ProviderError("Unsupported measured chat model identity")
        if type(context_tokens) is not int or not 64 <= context_tokens <= MAX_SAFE_COUNT:
            raise ProviderError("Explicit measured chat context limit is required")
        if (
            type(max_tokens) is not int
            or not 16 <= max_tokens <= 65536
            or not max_tokens < context_tokens
        ):
            raise ProviderError("Explicit measured chat complete context limit is required")
        if reasoning_effort is not None and (
            type(reasoning_effort) is not str
            or reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}
        ):
            raise ProviderError("Unsupported compatible provider reasoning effort")
        if (
            type(api_key) is not str
            or not api_key.isascii()
            or len(api_key) > 16384
            or any(mark in api_key for mark in ("\0", "\r", "\n"))
        ):
            raise ProviderError("Measured chat credential is unavailable")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 1 <= timeout <= 900
        ):
            raise ProviderError("Unsupported measured chat timeout")

        self.info = ProviderInfo(
            provider="openai-compatible", model=model, local=True, streaming=True
        )
        self.model = model
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.context_tokens = context_tokens
        self.timeout = timeout
        headers = {"Accept-Encoding": "identity"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        # Constructed directly: this adapter never owns an abandoned superclass client.
        self.http = RequestBoundAsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )
        self._closed = False
        self._count_cleanup_failed = False
        self._measurement = None
        self._count_generation = 0

    def _count_binding(self):
        return (
            str(self.http.base_url),
            self.model,
            self.max_tokens,
            self.reasoning_effort,
            self.context_tokens,
            request_digest(self.info.model_dump()),
        )

    def _freeze(self, messages, stream):
        if type(messages) is not list:
            raise PromptCountUnavailable(_ADMISSION_INVALID)
        try:
            body = CompatibleTextProvider._body(self, messages, stream)
            # Check plain JSON and finite resource lower bounds before any
            # serializer or detached copy can duplicate owner input. Each
            # string/node contributes at least one encoded wire byte.
            characters = nodes = 0
            active = set()

            def validate(value, depth=0):
                nonlocal characters, nodes
                nodes += 1
                if nodes > _REQUEST_MAX_BYTES or depth > 512:
                    raise ValueError
                kind = type(value)
                if kind is str:
                    characters += len(value)
                    if characters > _REQUEST_MAX_BYTES:
                        raise ValueError
                elif kind in (dict, list):
                    identity = id(value)
                    if identity in active:
                        raise ValueError
                    active.add(identity)
                    if kind is dict:
                        for key, item in value.items():
                            if type(key) is not str:
                                raise ValueError
                            validate(key, depth + 1)
                            validate(item, depth + 1)
                    else:
                        for item in value:
                            validate(item, depth + 1)
                    active.remove(identity)
                elif kind is float:
                    if not math.isfinite(value):
                        raise ValueError
                elif kind not in (int, bool, type(None)):
                    raise ValueError

            validate(body)
            raw = bytearray()
            encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            for part in encoder.iterencode(body):
                encoded = part.encode("utf-8", errors="strict")
                if len(raw) + len(encoded) > _REQUEST_MAX_BYTES:
                    raise ValueError
                raw.extend(encoded)
            frozen = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise PromptCountUnavailable(_ADMISSION_INVALID) from exc
        if (
            type(frozen) is not dict
            or frozen.get("model") != self.model
            or frozen.get("max_tokens") != self.max_tokens
            or frozen.get("stream") is not stream
            or ("reasoning_effort" in frozen) is not (self.reasoning_effort is not None)
        ):
            raise PromptCountUnavailable(_ADMISSION_INVALID)
        return frozen

    def _binding_digest(self, binding):
        return request_digest(
            {
                "provider": "openai-compatible",
                "contract": "chat-complete-count-v1",
                "configuration": binding,
            }
        )

    async def _input_tokens(self, client, body, check, admission):
        from packages.providers.whistle_ipc import JsonProtocolError, decode_json

        async with asyncio.timeout(_COUNT_TIMEOUT_S) as deadline:
            async with complete_stream(
                client,
                _COUNT_PATH,
                body,
                check,
                _OwnedHTTPStream,
                admission=admission,
                purpose="complete_count",
            ) as response:
                if response.status_code != 200:
                    raise PromptCountUnavailable(_MEASURE_UNAVAILABLE)
                if response.headers.get("content-encoding", "identity").strip().lower() not in {
                    "",
                    "identity",
                }:
                    raise PromptCountUnavailable(_MEASURE_UNAVAILABLE)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > _COUNT_MAX_BYTES:
                        raise PromptCountUnavailable(_MEASURE_UNAVAILABLE)
                    raw.extend(chunk)
        if deadline.expired():
            raise PromptCountUnavailable(_MEASURE_UNAVAILABLE)
        check()
        try:
            value = decode_json(bytes(raw), _COUNT_MAX_BYTES)
        except JsonProtocolError as exc:
            raise PromptCountUnavailable(_MEASURE_UNAVAILABLE) from exc
        if (
            type(value) is not dict
            or set(value) != {"object", "input_tokens"}
            or value["object"] != _COUNT_OBJECT
            or type(value["input_tokens"]) is not int
            or not 1 <= value["input_tokens"] <= MAX_SAFE_COUNT
        ):
            raise PromptCountUnavailable(_MEASURE_UNAVAILABLE)
        return value["input_tokens"]

    async def measure_prompt(self, messages):
        # A failed/replaced count never leaves a prior ticket usable.
        self._measurement = None
        self._count_generation += 1
        generation = self._count_generation
        if self._closed or self.http.is_closed or self._count_cleanup_failed:
            raise PromptCountUnavailable(_MEASURE_UNAVAILABLE)
        try:
            body = self._freeze(messages, True)
            stamp = request_digest(body)
            binding = self._count_binding()
            binding_stamp = request_digest(binding)
            client = self.http
            headers = request_digest(dict(client.headers))
            transport, mounts = client._transport, tuple(client._mounts.items())
            client_config = _configuration_of(client)
            source = messages
        except PromptCountUnavailable:
            raise
        except Exception as exc:
            raise PromptCountUnavailable(_ADMISSION_INVALID) from exc

        def current(candidate=None):
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise PromptCountUnavailable(_MEASURE_STALE)
            if not limits_current():
                raise PromptCountUnavailable(_MEASURE_STALE)
            try:
                if (
                    task is not owner
                    or self.http is not client
                    or client.is_closed
                    or self._closed
                    or self._count_cleanup_failed
                    or client._transport is not transport
                    or tuple(client._mounts.items()) != mounts
                    or _configuration_of(client) != client_config
                    or request_digest(dict(client.headers)) != headers
                    or request_digest(self._count_binding()) != binding_stamp
                    or self._count_generation != generation
                ):
                    raise PromptCountUnavailable(_MEASURE_STALE)
            except PromptCountUnavailable:
                raise
            except Exception as exc:
                raise PromptCountUnavailable(_MEASURE_STALE) from exc
            try:
                if request_digest(self._freeze(source, True)) != stamp:
                    raise PromptCountUnavailable(_MEASURE_STALE)
                if candidate is not None and request_digest(candidate) != stamp:
                    raise PromptCountUnavailable(_MEASURE_STALE)
            except PromptCountUnavailable:
                raise
            except Exception as exc:
                raise PromptCountUnavailable(_MEASURE_STALE) from exc

        def limits_current():
            return (
                type(self.context_tokens) is int
                and 64 <= self.context_tokens <= MAX_SAFE_COUNT
                and type(self.max_tokens) is int
                and 16 <= self.max_tokens <= 65536
                and self.max_tokens < self.context_tokens
            )

        owner = asyncio.current_task()
        if not limits_current():
            raise PromptCountUnavailable(_MEASURE_UNAVAILABLE)
        try:
            tokens = await self._input_tokens(client, body, current, None)
            current(body)
            measured = PromptTokenCount(
                tokens,
                self.context_tokens,
                self.max_tokens,
                stamp,
                self._binding_digest(binding),
            )
            if self._count_generation != generation:
                raise PromptCountUnavailable(_MEASURE_STALE)
            self._measurement = _MeasuredChatTicket(
                client,
                headers,
                binding,
                measured,
                transport,
                mounts,
                client_config,
                source,
                generation,
            )
            return measured
        except ProviderCleanupError:
            self._count_cleanup_failed = True
            raise
        except asyncio.CancelledError:
            raise
        except (PromptCountUnavailable, ProviderRefusal):
            raise
        except Exception as exc:
            raise PromptCountUnavailable(_MEASURE_UNAVAILABLE) from exc

    def _consume_ticket(self, messages, stream):
        # Consume once before the first transport await, including on refusal.
        ticket, self._measurement = self._measurement, None
        try:
            body = self._freeze(messages, stream)
            if (
                ticket is None
                or self._closed
                or self._count_cleanup_failed
                or self.http is not ticket.client
                or ticket.client.is_closed
                or self._closed
                or request_digest(dict(ticket.client.headers)) != ticket.headers_sha256
                or self._count_binding() != ticket.binding
                or ticket.client._transport is not ticket.transport
                or tuple(ticket.client._mounts.items()) != ticket.mounts
                or _configuration_of(ticket.client) != ticket.configuration
                or self._count_generation != ticket.generation
                or request_digest(self._freeze(ticket.source, True))
                != ticket.measurement.request_sha256
                or request_digest(body) != ticket.measurement.request_sha256
                or not ticket.measurement.fits
            ):
                raise PromptCountUnavailable(_GENERATION_STALE)
            return ticket.client, body, ticket
        except PromptCountUnavailable:
            raise
        except Exception as exc:
            raise PromptCountUnavailable(_GENERATION_STALE) from exc

    async def stream(self, messages):
        client, body, ticket = self._consume_ticket(messages, True)
        owner = asyncio.current_task()
        captured = capture_agent_export_check()
        if not agent_export_permitted():
            raise ProviderRefusal("Agent tool metadata disclosure changed")
        try:
            async with complete_stream(
                client,
                _GENERATION_PATH,
                body,
                lambda: self._stream_current(client, body, ticket, messages, owner, captured),
                _OwnedHTTPStream,
                admission=None,
                purpose="complete_generation",
            ) as response:
                if not response.is_success:
                    raise ProviderError(f"Provider HTTP {response.status_code}")
                _reject_compressed_response(response)
                async for text in _iter_compatible_stream(response):
                    yield text
        except ProviderCleanupError:
            self._count_cleanup_failed = True
            raise
        except httpx.HTTPError as exc:
            raise ProviderError("Compatible provider transport failure") from exc

    def _stream_current(self, client, body, ticket, messages, owner, captured):
        if not captured() or not agent_export_permitted():
            raise ProviderRefusal("Agent tool metadata disclosure changed")
        task = asyncio.current_task()
        if task is not owner or (task is not None and task.cancelling() > 0):
            raise PromptCountUnavailable(_GENERATION_STALE)
        try:
            if (
                self._closed
                or client.is_closed
                or self._count_cleanup_failed
                or self.http is not client
                or client._transport is not ticket.transport
                or tuple(client._mounts.items()) != ticket.mounts
                or _configuration_of(client) != ticket.configuration
                or request_digest(dict(client.headers)) != ticket.headers_sha256
                or self._count_binding() != ticket.binding
                or self._count_generation != ticket.generation
                or request_digest(self._freeze(ticket.source, True))
                != ticket.measurement.request_sha256
                or request_digest(self._freeze(messages, True)) != ticket.measurement.request_sha256
                or request_digest(body) != ticket.measurement.request_sha256
            ):
                raise PromptCountUnavailable(_GENERATION_STALE)
        except PromptCountUnavailable:
            raise
        except Exception as exc:
            raise PromptCountUnavailable(_GENERATION_STALE) from exc

    async def prepare_complete(self, messages):
        from packages.providers.prompt_budget import request_digest as _digest

        def limits_current():
            return (
                type(self.context_tokens) is int
                and 64 <= self.context_tokens <= MAX_SAFE_COUNT
                and type(self.max_tokens) is int
                and 16 <= self.max_tokens <= 65536
                and self.max_tokens < self.context_tokens
            )

        if self._closed or self.http.is_closed or self._count_cleanup_failed:
            raise PromptCountUnavailable(_MEASURE_UNAVAILABLE)
        if not limits_current():
            raise PromptCountUnavailable(_ADMISSION_INVALID)
        owner, snapshot = asyncio.current_task(), current_agent_tools()
        captured = capture_agent_export_check()
        admission = capture_agent_export_admission()
        request_tools = (
            snapshot.available_tools if snapshot is not None else LEGACY_NATIVE_TOOL_ORDER
        )
        try:
            body = self._freeze(messages, False)
            stamp, binding = _digest(body), self._count_binding()
            binding_stamp = _digest(binding)
            client = self.http
            headers = _digest(dict(client.headers))
            transport, mounts = client._transport, tuple(client._mounts.items())
            client_config = _configuration_of(client)
            source = messages
        except PromptCountUnavailable:
            raise
        except Exception as exc:
            raise PromptCountUnavailable(_ADMISSION_INVALID) from exc

        def current(candidate=body):
            if not captured() or not agent_export_permitted():
                raise ProviderRefusal("Agent tool metadata disclosure changed")
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise PromptCountUnavailable(_ADMISSION_CHANGED)
            try:
                if (
                    task is not owner
                    or current_agent_tools() is not snapshot
                    or not limits_current()
                    or self._closed
                    or self._count_cleanup_failed
                    or self.http is not client
                    or client.is_closed
                    or client._transport is not transport
                    or tuple(client._mounts.items()) != mounts
                    or _configuration_of(client) != client_config
                    or _digest(dict(client.headers)) != headers
                    or _digest(self._count_binding()) != binding_stamp
                    or _digest(self._freeze(source, False)) != stamp
                    or _digest(candidate) != stamp
                ):
                    raise PromptCountUnavailable(_ADMISSION_CHANGED)
            except (PromptCountUnavailable, ProviderRefusal):
                raise
            except Exception as exc:
                raise PromptCountUnavailable(_ADMISSION_CHANGED) from exc

        try:
            tokens = await self._input_tokens(client, body, current, admission)
            current()
            measured = PromptTokenCount(
                tokens,
                self.context_tokens,
                self.max_tokens,
                stamp,
                self._binding_digest(binding),
            )
        except ProviderCleanupError:
            self._count_cleanup_failed = True
            raise
        except (ProviderRefusal, asyncio.CancelledError):
            raise
        except PromptCountUnavailable:
            raise
        except Exception as exc:
            raise PromptCountUnavailable(_MEASURE_UNAVAILABLE) from exc

        async def generate():
            current()
            return await self._complete_request(
                client, body, request_tools, check=current, admission=admission
            )

        return PreparedComplete(measured, generate)

    async def _complete_request(self, client, body, request_tools, *, check, admission):
        from packages.providers.decision_capture import (
            capture_error_response,
            capture_request,
            capture_response,
        )

        snapshot_body = body
        binding, headers, origin = (
            self._count_binding(),
            dict(client.headers),
            str(client.base_url),
        )
        if not agent_export_permitted():
            raise ProviderRefusal("Agent tool metadata disclosure changed")
        if check is not None:
            check(snapshot_body)
        await capture_request(self, snapshot_body, "chat_completions")
        if not agent_export_permitted():
            raise ProviderRefusal("Agent tool metadata disclosure changed")
        if (
            self.http is not client
            or self._closed
            or self._count_binding() != binding
            or dict(client.headers) != headers
            or str(client.base_url) != origin
        ):
            raise ProviderRefusal("Compatible capture transport binding changed")
        if check is not None:
            check(snapshot_body)
        task = asyncio.current_task()
        if task is not None and task.cancelling() > 0:
            raise PromptCountUnavailable(_ADMISSION_CHANGED)
        try:
            async with complete_stream(
                client,
                _GENERATION_PATH,
                snapshot_body,
                lambda: check(snapshot_body),
                _OwnedHTTPStream,
                admission=admission,
                purpose="complete_generation",
            ) as response:
                if not response.is_success:
                    await capture_error_response(response, 1024 * 1024)
                    raise ProviderError(f"Provider HTTP {response.status_code}")
                _reject_compressed_response(response)
                collected = bytearray()
                chunks = (
                    response.aiter_bytes() if response.is_stream_consumed else response.aiter_raw()
                )
                async for chunk in chunks:
                    if len(collected) + len(chunk) > 1024 * 1024:
                        raise ProviderRefusal("Compatible provider response refused: wire bound")
                    collected.extend(chunk)
                raw = bytes(collected)
            if check is not None:
                check(snapshot_body)
            capture_response(raw)
            if response.is_error:
                raise ProviderError(f"Provider HTTP {response.status_code}")
            return _validate_complete_payload(raw)
        except ProviderCleanupError:
            self._count_cleanup_failed = True
            raise
        except httpx.HTTPError as exc:
            raise ProviderError("Compatible provider transport failure") from exc

    async def complete(self, messages):
        prepared = await self.prepare_complete(messages)
        return await prepared.complete()

    async def close(self):
        self._closed = True
        await self.http.aclose()
