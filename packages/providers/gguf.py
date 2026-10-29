"""Converse-only client for the protected managed GGUF resource endpoint."""

import re
from contextlib import aclosing

from packages.contracts.models import ProviderInfo
from packages.providers.qwen3_ipc import validate_messages
from packages.providers.text import CompatibleTextProvider


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
