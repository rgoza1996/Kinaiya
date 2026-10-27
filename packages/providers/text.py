"""Replaceable text providers. Fixture behavior is explicit and never a live fallback."""

import asyncio
import json
import os
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import httpx

from packages.contracts.models import ProviderInfo, ToolAction


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
        structured_actions=False,
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
        self.structured_actions = structured_actions
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=120,
            headers={"Authorization": f"Bearer {api_key}", "x-tbh-contributor-mode": "true"},
            transport=transport,
        )

    def body(self, messages, stream):
        if self.structured_actions:
            messages = [dict(message) for message in messages]
            wire = (
                "\nStrict response wire serialization: for mcp_call, put the configured server id "
                "in arguments.server_id, the exact tool name in arguments.mcp_tool, and JSON-encode "
                "the target argument object into arguments.mcp_arguments_json. For mcp_list, use "
                "server_id, optional after and optional mcp_tool for schema selection. "
                "The adapter converts these fields into the logical tool arguments. Set unused "
                "nullable fields to null. For finish, summary and objective_revision are required "
                "and the revision must equal the current objective revision. Continue tool work "
                "until the objective has actual verification evidence."
            )
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] += wire
            else:
                messages.insert(0, {"role": "system", "content": wire.strip()})
            for message in messages:
                if message.get("role") != "assistant":
                    continue
                try:
                    action = json.loads(message["content"])
                    args = action["arguments"]
                    if action["tool"] == "mcp_call":
                        action["arguments"] = {
                            "server_id": args["server_id"],
                            "mcp_tool": args["tool"],
                            "mcp_arguments_json": json.dumps(args["arguments"]),
                        }
                    elif action["tool"] == "mcp_list" and "tool" in args:
                        args["mcp_tool"] = args.pop("tool")
                    message["content"] = json.dumps(action)
                except (ValueError, KeyError, TypeError):
                    pass
        body = {
            "model": self.model,
            "input": messages,
            "stream": stream,
            "max_output_tokens": self.max_tokens,
            "reasoning": {"effort": self.effort},
        }
        if self.structured_actions:
            fields = {
                key: {"type": ["string", "null"]}
                for key in ("path", "content", "pattern", "command", "summary")
            }
            fields.update(
                {
                    key: {"type": ["integer", "null"]}
                    for key in ("objective_revision", "max_bytes", "after", "limit", "max_chars")
                }
            )
            fields["timeout_s"] = {"type": ["number", "null"]}
            fields["event_ids"] = {"type": ["array", "null"], "items": {"type": "string"}}
            fields["server_id"] = {"type": ["string", "null"]}
            fields["mcp_tool"] = {
                "type": ["string", "null"],
                "description": "Exact MCP tool name for mcp_call or selected-schema mcp_list.",
            }
            fields["mcp_arguments_json"] = {
                "type": ["string", "null"],
                "description": "For mcp_call: JSON-encode the target tool's full argument object in this string. The adapter decodes it into arguments.",
            }
            schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": ToolAction.model_json_schema()["properties"]["tool"]["enum"],
                    },
                    "arguments": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": fields,
                        "required": list(fields),
                    },
                },
                "required": ["tool", "arguments"],
            }
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "kinaiya_tool_action",
                    "strict": True,
                    "schema": schema,
                }
            }
        return body

    async def complete(self, messages):
        response = await self.http.post("responses", json=self.body(messages, False))
        if response.is_error:
            raise ProviderError(f"Meta HTTP {response.status_code}; no automatic provider fallback")
        data = response.json()
        if data.get("status") in {"failed", "incomplete"}:
            raise ProviderError("Meta response did not complete within its token budget")
        messages = [item for item in data.get("output", []) if item.get("type") == "message"]
        final_messages = [
            item for item in messages if item.get("phase") in {"final", "final_answer"}
        ]
        texts = [
            part["text"]
            for item in final_messages or messages
            if item.get("phase") != "commentary"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        if not texts:
            raise ProviderError("Meta returned no output text")
        text = "".join(texts)
        if self.structured_actions:
            action = json.loads(text)
            action["arguments"] = {
                key: value for key, value in action["arguments"].items() if value is not None
            }
            args = action["arguments"]
            if action.get("tool") == "mcp_call":
                encoded = args.get("mcp_arguments_json")
                if not isinstance(encoded, str) or len(encoded) > 65536:
                    raise ProviderError("MCP arguments require bounded JSON object text")
                try:
                    decoded = json.loads(encoded)
                except (ValueError, RecursionError) as exc:
                    raise ProviderError("MCP arguments are invalid JSON") from exc
                if not isinstance(decoded, dict):
                    raise ProviderError("MCP arguments must decode to an object")
                action["arguments"] = {
                    "server_id": args.get("server_id"),
                    "tool": args.get("mcp_tool"),
                    "arguments": decoded,
                }
            elif action.get("tool") == "mcp_list":
                action["arguments"] = {
                    key: value for key, value in args.items() if key in {"server_id", "after"}
                }
                if "mcp_tool" in args:
                    action["arguments"]["tool"] = args["mcp_tool"]
            return json.dumps(action)
        return text

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
