"""Converse-only client for the protected managed GGUF resource endpoint."""

import asyncio
import json
import re
from contextlib import aclosing

import httpx

from packages.contracts.models import ProviderInfo
from packages.providers.prompt_budget import (
    MAX_COUNT_BYTES,
    PromptCountUnavailable,
    PromptTokenCount,
    binding_digest,
    request_digest,
)
from packages.providers.qwen3_ipc import MAX_REQUEST_BYTES, decode_json, validate_messages
from packages.providers.text import (
    CompatibleTextProvider,
    ProviderCleanupError,
    ProviderError,
    _http_cleanup_owned,
    _OwnedHTTPStream,
    _reject_compressed_response,
)


class ManagedGGUFProvider(CompatibleTextProvider):
    def __init__(
        self,
        base_url,
        token,
        *,
        model,
        alias,
        profile,
        manifest_sha256,
        context_tokens=2048,
        max_new_tokens=128,
        request_timeout_s=120,
        transport=None,
    ):
        from packages.providers.qwen3 import deadline
        from services.resources.client import local_base

        self.resources_url = local_base(base_url)
        for value in (model, profile):
            if (
                not isinstance(value, str)
                or not 0 < len(value.encode("utf-8")) <= 128
                or any(char in value for char in "\0\r\n")
            ):
                raise ValueError("Invalid managed GGUF identity")
        if not isinstance(alias, str) or re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", alias) is None:
            raise ValueError("Invalid managed GGUF alias")
        if (
            not isinstance(manifest_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
        ):
            raise ValueError("Invalid managed GGUF manifest pin")
        if (
            type(context_tokens) is not int
            or not 64 <= context_tokens <= 32768
            or type(max_new_tokens) is not int
            or not 16 <= max_new_tokens < context_tokens
            or max_new_tokens > 2048
        ):
            raise ValueError("Invalid managed GGUF token limits")
        if (
            not isinstance(token, str)
            or not token.strip()
            or len(token) > 4096
            or any(char in token for char in "\0\r\n")
        ):
            raise ValueError("GGUF requires a scoped service credential")
        timeout = deadline(request_timeout_s)
        self._count_timeout_s = min(timeout, 10) + 5
        super().__init__(
            self.resources_url + "/v1",
            alias,
            api_key=token,
            max_tokens=max_new_tokens,
            timeout=timeout + 5,
            transport=transport,
        )
        self.info = ProviderInfo(provider="managed-gguf", model=model, local=True, streaming=True)
        self._binding = {
            "provider": self.info.provider,
            "model": model,
            "profile": profile,
            "manifest_sha256": manifest_sha256,
            "resources_url": self.resources_url,
            "context_tokens": context_tokens,
            "max_new_tokens": max_new_tokens,
        }

    @property
    def managed_binding(self):
        return dict(self._binding)

    def _body(self, messages, stream):
        if stream is not True:
            raise ValueError("Managed GGUF uses one streaming protocol")
        return super()._body(validate_messages(messages), True)

    async def complete(self, messages):
        # The same native SSE/EOF/real-close criteria apply to both callers.
        parts = []
        async with aclosing(self.stream(messages)) as source:
            async for text in source:
                parts.append(text)
        return "".join(parts)

    async def measure_prompt(self, messages):
        """Count the exact generation body, without admitting or requesting generation."""
        try:
            body, binding = self._body(messages, True), self.managed_binding
            encoded = json.dumps(
                body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if (
                len(encoded) > MAX_REQUEST_BYTES
                or type(body["max_tokens"]) is not int
                or body["max_tokens"] != binding["max_new_tokens"]
            ):
                raise PromptCountUnavailable("Prompt measurement is unavailable")
            request_sha256, binding_sha256 = request_digest(body), binding_digest(binding)
            http, endpoint = self.http, str(self.http.base_url)
            async with asyncio.timeout(self._count_timeout_s):
                async with http.stream(
                    "POST",
                    "chat/completions/input_tokens",
                    json=body,
                    timeout=self._count_timeout_s,
                ) as response:
                    if _http_cleanup_owned.get():
                        response.stream = _OwnedHTTPStream(response.stream)
                    if response.status_code != 200:
                        raise PromptCountUnavailable("Prompt measurement is unavailable")
                    _reject_compressed_response(response)
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw) + len(chunk) > MAX_COUNT_BYTES:
                            raise PromptCountUnavailable("Prompt measurement is unavailable")
                        raw.extend(chunk)
                # Real response close must precede either validation or success.
                # The original caller's request and selected configuration must
                # still describe the measurement that was actually requested.
                if (
                    self.http is not http
                    or str(http.base_url) != endpoint
                    or self.managed_binding != binding
                    or binding_digest(self.managed_binding) != binding_sha256
                    or request_digest(self._body(messages, True)) != request_sha256
                ):
                    raise PromptCountUnavailable("Prompt measurement is unavailable")
                return PromptTokenCount.from_wire(
                    decode_json(bytes(raw), MAX_COUNT_BYTES), body=body, binding=binding
                )
        except ProviderCleanupError:
            # Retained HTTP cleanup must stay distinguishable to its owner.
            raise
        except (
            ProviderError,
            ValueError,
            TypeError,
            OSError,
            httpx.HTTPError,
            TimeoutError,
        ) as exc:
            raise PromptCountUnavailable("Prompt measurement is unavailable") from exc
