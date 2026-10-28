"""Explicit managed local dialogue transport; no optional model imports."""

import asyncio
import math
from contextlib import aclosing

import httpx

from packages.contracts.models import ProviderInfo
from packages.providers.qwen3_ipc import (
    ERROR_CODES,
    MANIFEST_SHA256,
    MAX_EVENTS,
    MAX_FRAME_BYTES,
    MAX_TEXT_BYTES,
    MODEL,
    PROFILE,
    decode_json,
    event_message,
    parse_event,
    validate_limits,
    validate_messages,
)
from packages.providers.text import ProviderError, _http_cleanup_owned, _OwnedHTTPStream
from services.resources.client import local_base

PUBLIC_ERROR_CODES = ERROR_CODES | {"busy", "paused", "unavailable", "deadline", "disconnected"}
MAX_HTTP_BYTES = 1024 * 1024


def public_event(value, seq, *, max_input_tokens, max_new_tokens):
    """Validate public fields with the shared native payload/count rules."""
    if not isinstance(value, dict) or type(seq) is not int or not 0 <= seq < MAX_EVENTS:
        raise ValueError("Invalid managed Qwen public frame")
    kind = value.get("kind")
    if not isinstance(kind, str):
        raise ValueError("Invalid managed Qwen public kind")
    if kind == "error":
        if (
            set(value) != {"kind", "code"}
            or not isinstance(value["code"], str)
            or value["code"] not in PUBLIC_ERROR_CODES
        ):
            raise ValueError("Invalid managed Qwen error")
        return dict(value)
    fields = {"text"} if kind == "delta" else {"finish_reason", "input_tokens", "output_tokens"}
    if kind not in {"delta", "done"} or set(value) != {"kind"} | fields:
        raise ValueError("Invalid managed Qwen public frame")
    native = event_message(
        kind, 1, "a" * 64, "b" * 64, seq=seq, **{key: value[key] for key in fields}
    )
    parse_event(
        native,
        1,
        "a" * 64,
        "b" * 64,
        seq=seq,
        max_input_tokens=max_input_tokens,
        max_new_tokens=max_new_tokens,
    )
    return dict(value)


def deadline(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 1 <= value <= 900
    ):
        raise ValueError("Qwen requires a finite request deadline in 1..900 seconds")
    return float(value)


class ManagedQwen3Provider:
    info = ProviderInfo(provider="managed-qwen3", model=MODEL, local=True, streaming=True)

    def __init__(
        self,
        base_url,
        token,
        *,
        max_input_tokens=2048,
        max_new_tokens=128,
        request_timeout_s=120,
        transport=None,
    ):
        self.limits = validate_limits(max_input_tokens, max_new_tokens)
        self.timeout = deadline(request_timeout_s)
        if not isinstance(base_url, str):
            raise ValueError("Qwen requires a loopback origin")
        self.base_url = local_base(base_url)
        if not isinstance(token, str) or not token or any(char in token for char in "\0\r\n"):
            raise ValueError("Qwen requires a scoped service credential")
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
            timeout=self.timeout + 5,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    @property
    def managed_binding(self):
        return {
            "provider": "managed-qwen3",
            "model": MODEL,
            "profile": PROFILE,
            "manifest_sha256": MANIFEST_SHA256,
            "resources_url": self.base_url,
            **self.limits,
        }

    async def complete(self, messages):
        parts = []
        async with aclosing(self.stream(messages)) as source:
            async for text in source:
                parts.append(text)
        return "".join(parts)

    async def stream(self, messages):
        try:
            messages = validate_messages(messages)
            total, text_bytes, sequence, terminal = 0, 0, 0, False
            pending = bytearray()
            async with asyncio.timeout(self.timeout + 5):
                async with self.http.stream(
                    "POST", "/v1/converse/completions", json={"messages": messages}
                ) as response:
                    # Protect the actual close at EOF as well as context exit.
                    if _http_cleanup_owned.get():
                        response.stream = _OwnedHTTPStream(response.stream)
                    if not response.is_success:
                        raise ProviderError(f"Managed Qwen HTTP {response.status_code}")
                    if response.headers.get("content-encoding", "identity") not in {"", "identity"}:
                        raise ProviderError("Managed Qwen requires an uncompressed bounded stream")
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_HTTP_BYTES:
                            raise ProviderError("Managed Qwen HTTP stream exceeds its byte bound")
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            event = public_event(
                                decode_json(bytes(line), MAX_FRAME_BYTES), sequence, **self.limits
                            )
                            if terminal:
                                raise ProviderError("Managed Qwen returned data after its terminal")
                            sequence += 1
                            if event["kind"] == "delta":
                                text_bytes += len(event["text"].encode("utf-8"))
                                if text_bytes > MAX_TEXT_BYTES:
                                    raise ProviderError("Managed Qwen text exceeds its byte bound")
                                yield event["text"]
                            elif event["kind"] == "error":
                                raise ProviderError("Managed Qwen refusal: " + event["code"])
                            else:
                                terminal = True
                                if event["finish_reason"] == "length":
                                    raise ProviderError(
                                        "Managed Qwen reached its output token limit"
                                    )
                        if len(pending) > MAX_FRAME_BYTES:
                            raise ProviderError("Managed Qwen HTTP frame exceeds its byte bound")
                    if pending:
                        # The broker emits complete newline-delimited frames.
                        raise ProviderError("Managed Qwen returned an incomplete HTTP frame")
                    if not terminal:
                        raise ProviderError("Managed Qwen ended without a terminal")
        except (ValueError, UnicodeError, OSError, httpx.HTTPError, TimeoutError) as exc:
            raise ProviderError("Managed Qwen transport or bounded protocol failed") from exc

    async def close(self):
        await self.http.aclose()
