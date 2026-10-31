"""Replaceable text providers. Fixture behavior is explicit and never a live fallback."""

import asyncio
import copy
import json
import os
import stat
from collections import deque
from collections.abc import AsyncIterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from packages.contracts.models import ProviderInfo, ToolAction


class ProviderError(RuntimeError):
    pass


class ProviderRefusal(ProviderError):
    """A rejected input/result must be surfaced rather than retried as a decision."""


class ProviderCleanupError(ProviderError):
    """The provider's HTTP response did not confirm resource cleanup."""


def _meta_credential():
    """One bounded protected credential reference, never other user state."""
    from packages.providers.whistle_ipc import decode_json

    path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "muse/auth.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > 65536
            ):
                raise ValueError("Unsafe credential reference")
            profile = decode_json(os.read(fd, 65537), 65536)["providers"]["meta"]
            key = profile.get("api_key") or profile.get("access_token")
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 16384
                or any(c in key for c in "\0\r\n")
            ):
                raise ValueError("Invalid credential")
            return key
        finally:
            os.close(fd)
    except Exception as exc:
        raise ProviderError("Meta credential is unavailable") from exc


_http_cleanup_owned = ContextVar("http_cleanup_owned", default=False)


@contextmanager
def owned_http_stream_cleanup():
    """Opt in only while a caller retains bounded, unconfirmed stream ownership."""
    token = _http_cleanup_owned.set(True)
    try:
        yield
    finally:
        _http_cleanup_owned.reset(token)


class _OwnedHTTPStream(httpx.AsyncByteStream):
    """Keep HTTP response cleanup owned through cancellation at EOF or error."""

    def __init__(self, stream):
        self.stream = stream
        self.cleanup = None

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self.stream.aclose())
        interrupted = False
        while not self.cleanup.done():
            try:
                await asyncio.shield(self.cleanup)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                break
        # This owner remains pending until real close succeeds or fails. The
        # caller's bounded cancellation policy must retain that ownership.
        try:
            self.cleanup.result()
        except (Exception, asyncio.CancelledError) as exc:
            raise ProviderCleanupError("HTTP response cleanup is unconfirmed") from exc
        if interrupted:
            raise asyncio.CancelledError


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


# Inclusive entity-body, SSE framing and UTF-8 text limits. Comments and metadata
# consume the wire/line bounds; every data event, including DONE, consumes a slot.
_COMPAT_MAX_RAW_BYTES = 1024 * 1024
_COMPAT_MAX_LINE_BYTES = 64 * 1024
_COMPAT_MAX_EVENT_BYTES = 64 * 1024
_COMPAT_MAX_TEXT_BYTES = 256 * 1024
_COMPAT_MAX_DATA_EVENTS = 8192


def _refusal(reason):
    return ProviderRefusal("Compatible provider response refused: " + reason)


def _reject_compressed_response(response):
    if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
        raise _refusal("unsupported encoding")


def _compatible_json(raw):
    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw, parse_constant=invalid_constant, object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as exc:
        raise _refusal("malformed UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise _refusal("malformed payload")
    if value.get("error") is not None:
        raise _refusal("provider error payload")
    return value


def _text_size(text):
    try:
        return len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise _refusal("invalid UTF-8 text") from exc


def _compatible_choice(payload):
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise _refusal("unsupported choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise _refusal("malformed choice")
    if "index" in choice and (type(choice["index"]) is not int or choice["index"] != 0):
        raise _refusal("unsupported choice index")
    return choice


def _compatible_content(value):
    if not isinstance(value, dict):
        raise _refusal("malformed text message")
    role = value.get("role")
    if role is not None and role != "assistant":
        raise _refusal("malformed text message")
    if any(value.get(key) is not None for key in ("tool_calls", "function_call", "refusal")):
        raise _refusal("unsupported tool output or refusal")
    content = value.get("content")
    if content is not None and not isinstance(content, str):
        raise _refusal("non-string content")
    return content


def _validate_complete_payload(raw):
    payload = _compatible_json(raw)
    choice = _compatible_choice(payload)
    if any(choice.get(key) is not None for key in ("tool_calls", "function_call", "refusal")):
        raise _refusal("unsupported tool output or refusal")
    if choice.get("finish_reason") != "stop":
        raise _refusal("incomplete or unsupported finish")
    content = _compatible_content(choice.get("message"))
    if content is None:
        raise _refusal("non-string content")
    if _text_size(content) > _COMPAT_MAX_TEXT_BYTES:
        raise _refusal("text bound")
    return content


@dataclass(frozen=True)
class _CompatibleStreamEvent:
    """Validated SSE data; DONE alone does not confirm EOF or HTTP cleanup."""

    data: str
    text: str | None
    done: bool


async def _iter_compatible_events(response) -> AsyncIterator[_CompatibleStreamEvent]:
    total = events = out_bytes = data_bytes = 0
    line_buf, data_lines = bytearray(), []
    seen_stop = seen_done = False

    def dispatch():
        nonlocal events, out_bytes, seen_stop, seen_done
        events += 1
        if events > _COMPAT_MAX_DATA_EVENTS:
            raise _refusal("event count bound")
        assembled = "\n".join(data_lines)
        if assembled == "[DONE]":
            if seen_done or not seen_stop:
                raise _refusal("out-of-order terminal")
            seen_done = True
            return _CompatibleStreamEvent(assembled, None, True)
        if seen_done:
            raise _refusal("data after DONE")
        payload = _compatible_json(assembled)
        if payload.get("choices") == [] and isinstance(payload.get("usage"), dict):
            return _CompatibleStreamEvent(assembled, None, False)
        choice = _compatible_choice(payload)
        if seen_stop:
            raise _refusal("choice after stop")
        if any(choice.get(key) is not None for key in ("tool_calls", "function_call", "refusal")):
            raise _refusal("unsupported tool output or refusal")
        content = _compatible_content(choice.get("delta"))
        finish = choice.get("finish_reason")
        # Validate the entire event before yielding, including a final delta
        # carrying both text and a finish reason.
        if finish is not None:
            if finish != "stop":
                raise _refusal("incomplete or unsupported finish")
            seen_stop = True
        if content:
            size = _text_size(content)
            if out_bytes + size > _COMPAT_MAX_TEXT_BYTES:
                raise _refusal("text bound")
            out_bytes += size
        return _CompatibleStreamEvent(assembled, content, False)

    def feed_line(raw_line):
        nonlocal data_bytes
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _refusal("invalid UTF-8") from exc
        if not line:
            event = dispatch() if data_lines else None
            data_lines.clear()
            data_bytes = 0
            return event
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            size = _text_size(value) + int(bool(data_lines))
            if data_bytes + size > _COMPAT_MAX_EVENT_BYTES:
                raise _refusal("event bound")
            data_bytes += size
            data_lines.append(value)
        return None

    # Encoding is checked before this iterator. With identity encoding these
    # are entity-body bytes; aiter_bytes also supports prebuffered transports.
    async for chunk in response.aiter_bytes():
        if total + len(chunk) > _COMPAT_MAX_RAW_BYTES:
            raise _refusal("wire bound")
        total += len(chunk)
        start = 0
        while start < len(chunk):
            index = chunk.find(b"\n", start)
            end = len(chunk) if index < 0 else index
            size = len(line_buf) + end - start
            last_cr = (chunk[end - 1] == 13) if end > start else line_buf[-1:] == b"\r"
            # Keep at most one extra trailing CR until its LF arrives. CRLF
            # delimiters do not count toward the raw line's inclusive bound.
            if size - int(last_cr) > _COMPAT_MAX_LINE_BYTES:
                raise _refusal("line bound")
            line_buf.extend(chunk[start:end])
            if index < 0:
                break
            if line_buf[-1:] == b"\r":
                line_buf.pop()
            event = feed_line(bytes(line_buf))
            line_buf.clear()
            if event is not None:
                yield event
            start = index + 1
    if line_buf:
        # An EOF comment is harmless; an unfinished data event is not a
        # dispatched terminal. Never turn EOF into an implicit blank line.
        if len(line_buf) > _COMPAT_MAX_LINE_BYTES:
            raise _refusal("line bound")
        feed_line(bytes(line_buf))
    if data_lines or not (seen_stop and seen_done):
        raise _refusal("incomplete stream")


async def _iter_compatible_stream(response) -> AsyncIterator[str]:
    async for event in _iter_compatible_events(response):
        if event.text:
            yield event.text


class CompatibleTextProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        allow_cloud=False,
        max_tokens=2048,
        reasoning_effort=None,
        timeout=90,
        transport=None,
    ):
        if reasoning_effort is not None and (
            type(reasoning_effort) is not str
            or reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}
        ):
            raise ProviderError("Unsupported compatible provider reasoning effort")
        local = _authorize_endpoint(base_url, allow_cloud)
        self.info = ProviderInfo(
            provider="openai-compatible", model=model, local=local, streaming=True
        )
        self.model, self.max_tokens = model, max_tokens
        self.reasoning_effort = reasoning_effort
        headers = {"Accept-Encoding": "identity"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def _body(self, messages, stream):
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    async def complete(self, messages):
        from packages.providers.decision_capture import (
            capture_error_response,
            capture_request,
            capture_response,
        )

        body = copy.deepcopy(self._body(messages, False))
        client, info, headers = self.http, self.info.model_dump(), dict(self.http.headers)
        origin = str(client.base_url)
        await capture_request(self, body, "chat_completions")
        if (
            self.http is not client
            or self.info.model_dump() != info
            or dict(client.headers) != headers
            or str(client.base_url) != origin
        ):
            raise ProviderRefusal("Compatible capture transport binding changed")
        try:
            async with self.http.stream("POST", "chat/completions", json=body) as response:
                if _http_cleanup_owned.get():
                    response.stream = _OwnedHTTPStream(response.stream)
                if not response.is_success:
                    await capture_error_response(response, _COMPAT_MAX_RAW_BYTES)
                    raise ProviderError(f"Provider HTTP {response.status_code}")
                _reject_compressed_response(response)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > _COMPAT_MAX_RAW_BYTES:
                        raise _refusal("wire bound")
                    raw.extend(chunk)
                capture_response(bytes(raw))
                return _validate_complete_payload(bytes(raw))
        except httpx.HTTPError as exc:
            raise ProviderError("Compatible provider transport failure") from exc

    async def stream(self, messages) -> AsyncIterator[str]:
        try:
            async with self.http.stream(
                "POST", "chat/completions", json=self._body(messages, True)
            ) as response:
                if _http_cleanup_owned.get():
                    response.stream = _OwnedHTTPStream(response.stream)
                if not response.is_success:
                    raise ProviderError(f"Provider HTTP {response.status_code}")
                _reject_compressed_response(response)
                async for text in _iter_compatible_stream(response):
                    yield text
        except httpx.HTTPError as exc:
            raise ProviderError("Compatible provider transport failure") from exc

    async def close(self):
        await self.http.aclose()


@dataclass(frozen=True)
class _MetaPromptTicket:
    client: httpx.AsyncClient
    headers_sha256: str
    binding: tuple
    measurement: object


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
        context_tokens=None,
        transport=None,
    ):
        if not allow_cloud:
            raise ProviderError("Meta requires explicit cloud disclosure authorization")
        _authorize_endpoint(base_url, True)
        if secret_ref == "muse":
            api_key = _meta_credential()
        elif secret_ref is not None:
            raise ProviderError("Unknown Meta credential reference")
        if not api_key:
            raise ProviderError("Meta credential is unavailable")
        self.info = ProviderInfo(provider="meta", model=model, local=False, streaming=True)
        self.model, self.max_tokens, self.effort = model, max_tokens, reasoning_effort
        self.structured_actions = structured_actions
        if context_tokens is not None and (
            type(context_tokens) is not int
            or not 64 <= context_tokens <= 2**53 - 1
            or type(max_tokens) is not int
            or not 16 <= max_tokens <= 2048
            or max_tokens >= context_tokens
        ):
            raise ProviderError("Invalid explicit Meta context budget")
        # An owner-provided/catalog-verified limit, never inferred from /models ids.
        self.context_tokens = context_tokens
        self._count_cleanup_failed = False
        self._measurement_required = False
        self._measurement = None
        self._count_generation = 0
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=120,
            headers={"Authorization": f"Bearer {api_key}", "x-tbh-contributor-mode": "true"},
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    @property
    def cloud_prompt_measurement(self):
        return self.context_tokens is not None

    def _count_binding(self):
        from packages.providers.prompt_budget import request_digest

        return (
            str(self.http.base_url),
            self.model,
            self.max_tokens,
            self.effort,
            self.structured_actions,
            self.context_tokens,
            request_digest(self.info.model_dump()),
        )

    async def measure_prompt(self, messages):
        """Count this exact Responses request; this operation never generates."""
        from packages.providers.prompt_budget import (
            MAX_SAFE_COUNT,
            PromptCountUnavailable,
            PromptTokenCount,
            request_digest,
        )
        from packages.providers.whistle_ipc import decode_json

        # A failed/replaced count cannot leave a prior ticket usable. Ordinary
        # Agent calls that never opted into measurement retain their interface.
        self._measurement_required = True
        self._measurement = None
        self._count_generation += 1
        generation = self._count_generation
        if self.context_tokens is None or self._count_cleanup_failed:
            raise PromptCountUnavailable("Explicit Meta context limit is required")
        try:
            # The count schema accepts the generation fields, including stream,
            # reasoning and structured text. Detach before the first await.
            body = json.loads(json.dumps(self.body(messages, True), allow_nan=False))
            stamp = request_digest(body)
            client, headers = self.http, dict(self.http.headers)
            binding = self._count_binding()
            async with asyncio.timeout(10):
                async with client.stream("POST", "responses/input_tokens", json=body) as response:
                    response.stream = _OwnedHTTPStream(response.stream)
                    if response.status_code != 200:
                        raise PromptCountUnavailable("Meta prompt measurement is unavailable")
                    _reject_compressed_response(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 4096:
                            raise PromptCountUnavailable("Meta prompt measurement is invalid")
                        raw.extend(chunk)
            value = decode_json(bytes(raw), 4096)
            if (
                type(value) is not dict
                or set(value) != {"object", "input_tokens"}
                or value["object"] != "response.input_tokens"
                or type(value["input_tokens"]) is not int
                or not 1 <= value["input_tokens"] <= MAX_SAFE_COUNT
                or self.http is not client
                or client.is_closed
                or dict(client.headers) != headers
                or self._count_generation != generation
                or binding != self._count_binding()
                or request_digest(self.body(messages, True)) != stamp
            ):
                raise PromptCountUnavailable("Meta prompt measurement is invalid or stale")
            measured = PromptTokenCount(
                value["input_tokens"],
                self.context_tokens,
                self.max_tokens,
                stamp,
                request_digest(
                    {"provider": "meta", "contract": "responses-count-v1", "configuration": binding}
                ),
            )
            self._measurement = _MetaPromptTicket(
                client, request_digest(headers), binding, measured
            )
            return measured
        except ProviderCleanupError:
            self._count_cleanup_failed = True
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PromptCountUnavailable("Meta prompt measurement is unavailable") from exc

    def _generation_request(self, messages, stream):
        if not self._measurement_required:
            return self.http, self.body(messages, stream)
        from packages.providers.prompt_budget import PromptCountUnavailable, request_digest

        # Consume once before the first transport await, including on refusal.
        # Context assembly performs native awaits after its final count; only
        # this synchronous check binds the actual outgoing generation to it.
        ticket, self._measurement = self._measurement, None
        try:
            body = json.loads(json.dumps(self.body(messages, stream), allow_nan=False))
            if (
                ticket is None
                or self._count_cleanup_failed
                or self.http is not ticket.client
                or ticket.client.is_closed
                or request_digest(dict(ticket.client.headers)) != ticket.headers_sha256
                or self._count_binding() != ticket.binding
                or request_digest(body) != ticket.measurement.request_sha256
                or not ticket.measurement.fits
            ):
                raise PromptCountUnavailable("Meta generation measurement is missing or stale")
            return ticket.client, body
        except PromptCountUnavailable:
            raise
        except Exception as exc:
            raise PromptCountUnavailable("Meta generation measurement is missing or stale") from exc

    def body(self, messages, stream):
        if self.structured_actions:
            messages = [dict(message) for message in messages]
            wire = (
                "\nFor plugin_call, put the approved id in plugin_id and JSON-encode the input object in plugin_arguments_json. "
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
                    elif action["tool"] == "plugin_call":
                        action["arguments"] = {
                            "plugin_id": args["plugin_id"],
                            "plugin_arguments_json": json.dumps(args["arguments"], allow_nan=False),
                        }
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
                for key in (
                    "path",
                    "content",
                    "pattern",
                    "command",
                    "summary",
                    "skill_id",
                    "resource",
                    "plugin_id",
                    "plugin_arguments_json",
                )
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
        from packages.providers.decision_capture import capture_request, capture_response

        client, body = self._generation_request(messages, False)
        body = copy.deepcopy(body)
        binding, headers, origin = self._count_binding(), dict(client.headers), str(client.base_url)
        await capture_request(self, body, "responses")
        if (
            self.http is not client
            or self._count_binding() != binding
            or dict(client.headers) != headers
            or str(client.base_url) != origin
        ):
            raise ProviderRefusal("Meta capture transport binding changed")
        response = await client.post("responses", json=body)
        capture_response(response.content)
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
            elif action.get("tool") == "plugin_call":
                encoded = args.get("plugin_arguments_json")
                if not isinstance(encoded, str) or len(encoded) > 65536:
                    raise ProviderError("Plugin arguments require bounded JSON object text")
                try:
                    decoded = json.loads(encoded)
                except (ValueError, RecursionError) as exc:
                    raise ProviderError("Plugin arguments are invalid JSON") from exc
                if not isinstance(decoded, dict):
                    raise ProviderError("Plugin arguments must decode to an object")
                action["arguments"] = {"plugin_id": args.get("plugin_id"), "arguments": decoded}
            elif action.get("tool") == "mcp_list":
                action["arguments"] = {
                    key: value for key, value in args.items() if key in {"server_id", "after"}
                }
                if "mcp_tool" in args:
                    action["arguments"]["tool"] = args["mcp_tool"]
            return json.dumps(action)
        return text

    async def stream(self, messages):
        client, body = self._generation_request(messages, True)
        terminal = False
        async with client.stream("POST", "responses", json=body) as response:
            if _http_cleanup_owned.get():
                response.stream = _OwnedHTTPStream(response.stream)
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
