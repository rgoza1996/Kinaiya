"""Replaceable text providers. Fixture behavior is explicit and never a live fallback."""

import asyncio
import json
import os
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import httpx

from packages.contracts.models import ProviderInfo


class ProviderError(RuntimeError):
    pass


class ScriptedProvider:
    info = ProviderInfo(provider="deterministic-fixture", model="scripted", local=True)

    def __init__(self, responses, delay=0):
        self.responses = deque(responses)
        self.delay = delay
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        await asyncio.sleep(self.delay)
        if not self.responses:
            raise ProviderError("Synthetic fixture exhausted")
        value = self.responses.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    async def stream(self, messages):
        yield await self.complete(messages)


def _authorize_endpoint(base_url: str, allow_cloud: bool) -> bool:
    parsed = urlparse(base_url)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ProviderError("Use an HTTP(S) endpoint without embedded credentials")
    if not local and not allow_cloud:
        raise ProviderError("Cloud disclosure is disabled for this provider profile")
    if not local and parsed.scheme != "https":
        raise ProviderError("Remote inference requires HTTPS")
    return local


class CompatibleTextProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        allow_cloud=False,
        max_tokens=2048,
        timeout=90,
        transport=None,
    ):
        local = _authorize_endpoint(base_url, allow_cloud)
        self.info = ProviderInfo(
            provider="openai-compatible", model=model, local=local, streaming=True
        )
        self.model, self.max_tokens = model, max_tokens
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    async def complete(self, messages):
        response = await self.http.post(
            "chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "stream": False,
            },
        )
        if response.is_error:
            raise ProviderError(
                f"Provider HTTP {response.status_code}; inspect provider diagnostics locally"
            )
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Provider returned no text") from exc

    async def stream(self, messages) -> AsyncIterator[str]:
        async with self.http.stream(
            "POST",
            "chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "stream": True,
            },
        ) as response:
            if response.is_error:
                raise ProviderError(f"Provider HTTP {response.status_code}")
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    event = json.loads(line[6:])
                    choices = event.get("choices", [])
                    if choices:
                        text = choices[0].get("delta", {}).get("content")
                        if text:
                            yield text

    async def close(self):
        await self.http.aclose()


class MetaResponsesProvider:
    """Meta Responses API; explicit Contributor disclosure, scoped secret reference.

    `secret_ref='muse'` deliberately reads only the existing Meta credential; no
    profile names, email, settings, private history or other provider credentials.
    The development demo uses synthetic/public data and the budget proxy.
    """

    def __init__(
        self,
        *,
        base_url="https://api.meta.ai/v1",
        api_key="",
        secret_ref=None,
        model="muse-spark-1.3-contributor",
        allow_cloud=False,
        max_tokens=2048,
        reasoning_effort="xhigh",
        transport=None,
    ):
        if not allow_cloud:
            raise ProviderError("Meta requires explicit cloud disclosure authorization")
        _authorize_endpoint(base_url, True)
        if secret_ref == "muse":
            path = (
                Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "muse/auth.json"
            )
            profile = json.loads(path.read_text())["providers"]["meta"]
            api_key = profile.get("api_key") or profile.get("access_token")
        if not api_key:
            raise ProviderError("Meta credential is unavailable")
        self.info = ProviderInfo(provider="meta", model=model, local=False, streaming=True)
        self.model, self.max_tokens, self.effort = model, max_tokens, reasoning_effort
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=120,
            headers={"Authorization": f"Bearer {api_key}", "x-tbh-contributor-mode": "true"},
            transport=transport,
        )

    def body(self, messages, stream):
        return {
            "model": self.model,
            "input": messages,
            "stream": stream,
            "max_output_tokens": self.max_tokens,
            "reasoning": {"effort": self.effort},
        }

    async def complete(self, messages):
        response = await self.http.post("responses", json=self.body(messages, False))
        if response.is_error:
            raise ProviderError(f"Meta HTTP {response.status_code}; no automatic provider fallback")
        data = response.json()
        if data.get("status") in {"failed", "incomplete"}:
            raise ProviderError("Meta response did not complete within its token budget")
        texts = [
            part["text"]
            for item in data.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        if not texts:
            raise ProviderError("Meta returned no output text")
        return "".join(texts)

    async def stream(self, messages):
        terminal = False
        async with self.http.stream(
            "POST", "responses", json=self.body(messages, True)
        ) as response:
            if response.is_error:
                raise ProviderError(f"Meta HTTP {response.status_code}")
            async for line in response.aiter_lines():
                if not line.startswith("data: ") or line[6:] == "[DONE]":
                    continue
                event = json.loads(line[6:])
                if event.get("type") == "response.output_text.delta":
                    yield event["delta"]
                elif event.get("type") == "response.completed":
                    terminal = True
                elif event.get("type") in {"response.failed", "response.incomplete", "error"}:
                    raise ProviderError("Meta streaming response failed or exhausted budget")
        if not terminal:
            raise ProviderError("Meta stream disconnected before completion")

    async def close(self):
        await self.http.aclose()
