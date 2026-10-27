"""Native MCP host/client for Kinaiya (Roadmap 31.5, ADR-0006).

The host brokers a small set of owner-configured MCP servers through the
same permission layer as native tools. Model output, tool names,
descriptions, and server results are untrusted input: discovery never
grants call authority, and only an exact ``allowed_tools`` entry on an
enabled server authorizes a call. Configuration replacement is owner-only.

Isolation: local stdio servers run only with an explicit shell grant and
only inside the existing bubblewrap sandbox from
``services.agent.tools.build_bwrap_args`` (no fallback). Remote HTTP
servers need both the policy network grant and per-server ``allow_remote``,
HTTPS outside loopback, strict URL rules, and secret-ref headers only.
Secrets travel as resolver references, never as embedded values, and never
appear in metadata, errors, or reprs.

Concurrency: every server is owned by a dedicated actor task. All MCP SDK
async contexts (stdio/HTTP transports and ClientSession) are entered,
used, and exited on that same actor task, so ``close()`` from any request
task never exits a cancel scope in a different task. Public ``discover`` /
``call`` enqueue to the owner; caller cancellation is isolated with
``asyncio.shield`` and bounded. Timeouts/cancels of a call never auto-
replay a possibly-executed side effect: the session is dropped (stdio
process group killed) and the outcome reported as uncertain.
"""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import os
import re
import shlex
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import anyio
import httpx
import jsonschema
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.policy import PolicyDenied, ToolPolicy
from services.agent.tools import build_bwrap_args

from . import _stdio_relay

SecretResolver = Callable[[str], str | None]

MAX_LISTED_TOOLS = 128
MAX_TOOL_NAME_CHARS = 128
MAX_DESCRIPTION_CHARS = 2000
MAX_SCHEMA_CHARS = 32768
MAX_ARGS_CHARS = 65536
MAX_ARG_KEYS = 64
MAX_ALLOWED_TOOLS = 64
MAX_CONFIG_ARGS = 32
MAX_SECRET_REFS = 16
MAX_REF_CHARS = 512
MAX_URL_CHARS = 2048
FRAME_CAP = _stdio_relay.MAX_STDOUT_FRAME_BYTES
INPUT_FRAME_CAP = _stdio_relay.MAX_INPUT_FRAME_BYTES
STDERR_CAP = _stdio_relay.MAX_STDERR_BYTES
TERMINATION_TIMEOUT = 2.0
MAX_STDIO_MEMORY_BYTES = 512 * 1024 * 1024
SECRET_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "api_key",
        "apikey",
        "key",
        "secret",
        "auth",
        "authorization",
        "password",
        "credential",
    }
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|0-9A-Za-z]+$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class MCPError(Exception):
    """Base failure for MCP host/transport/tool problems."""


class MCPTransportError(MCPError):
    """Connection, timeout, crash, bound, or protocol failure. Host survives."""


class MCPToolError(MCPError):
    """The server reported an error, or arguments/results failed host bounds."""


def _is_loopback(host: str) -> bool:
    if host.lower() in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _sanitize_url(url: str) -> str:
    """Endpoint for metadata/errors: scheme+host+path only, no userinfo/query."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "http://[invalid-url]"
    netloc = parts.hostname or "[no-host]"
    if parts.port:
        netloc += f":{parts.port}"
    path = parts.path or ""
    return f"{parts.scheme}://{netloc}{path}"


def _scrub(text: str, secrets: list[str]) -> str:
    """Remove secret values, query strings, and userinfo from an error detail."""
    for value in secrets:
        if value and value in text:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"\?[^\s\"']*", "?[query-redacted]", text)
    text = re.sub(r"://[^/\s@]+@", "://[redacted]@", text)
    return " ".join(text.split())[:300]


def _scrub_any(obj: Any, secrets: list[str]) -> Any:
    """Deep-scrub resolved secret values out of untrusted tool content."""
    if not secrets:
        return obj
    if isinstance(obj, str):
        for value in secrets:
            if value and value in obj:
                obj = obj.replace(value, "[redacted]")
        return obj
    if isinstance(obj, list):
        return [_scrub_any(item, secrets) for item in obj]
    if isinstance(obj, dict):
        scrubbed: dict[Any, Any] = {}
        for key, val in obj.items():
            if isinstance(key, str):
                for value in secrets:
                    if value and value in key:
                        key = key.replace(value, "[redacted]")
            scrubbed[key] = _scrub_any(val, secrets)
        return scrubbed
    return obj


def _scrubbed_stderr_tail(holder: list[bytes], secrets: list[str]) -> str:
    """Last stderr bytes for error context: bounded, decoded, secret-scrubbed."""
    raw = bytes(holder[0]) if holder and holder[0] else b""
    if not raw:
        return ""
    tail = raw[-512:]
    return _scrub(tail.decode("utf-8", errors="replace"), secrets)


class MCPConfig(BaseModel):
    """Owner configuration for one MCP server. Secrets are references only."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    enabled: bool = False
    allowed_tools: list[str] = Field(default_factory=list)
    env_secret_refs: dict[str, str] = Field(default_factory=dict)
    header_secret_refs: dict[str, str] = Field(default_factory=dict)
    allow_remote: bool = False
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    max_result_chars: int = Field(default=16000, ge=1024, le=262144)

    def __repr__(self) -> str:
        return f"MCPConfig(id={self.id!r}, transport={self.transport!r}, enabled={self.enabled!r})"

    @field_validator("command")
    @classmethod
    def _non_empty_command(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("command must be non-empty")
        return value

    @field_validator("args")
    @classmethod
    def _bound_args(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_CONFIG_ARGS:
            raise ValueError(f"args lists at most {MAX_CONFIG_ARGS} entries")
        for item in value:
            if not isinstance(item, str) or len(item) > 4096:
                raise ValueError("each arg must be a string of at most 4096 chars")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def _exact_tool_names(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_ALLOWED_TOOLS:
            raise ValueError(f"allowlist holds at most {MAX_ALLOWED_TOOLS} tools")
        for name in value:
            if (
                not isinstance(name, str)
                or not _TOOL_NAME.fullmatch(name)
                or len(name) > MAX_TOOL_NAME_CHARS
            ):
                raise ValueError(f"allowlist entries must be exact tool names, got {name!r}")
        return value

    @field_validator("env_secret_refs")
    @classmethod
    def _env_refs(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_SECRET_REFS:
            raise ValueError(f"at most {MAX_SECRET_REFS} secret refs")
        for name, ref in value.items():
            if not _ENV_NAME.fullmatch(name) or not isinstance(ref, str) or not ref.strip():
                raise ValueError(f"env_secret_refs needs ENV-NAME -> non-empty ref, got {name!r}")
            if len(ref) > MAX_REF_CHARS:
                raise ValueError("secret ref exceeds bounds")
        return value

    @field_validator("header_secret_refs")
    @classmethod
    def _header_refs(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_SECRET_REFS:
            raise ValueError(f"at most {MAX_SECRET_REFS} secret refs")
        for name, ref in value.items():
            if not _HEADER_NAME.fullmatch(name) or not isinstance(ref, str) or not ref.strip():
                raise ValueError(
                    f"header_secret_refs needs header-name -> non-empty ref, got {name!r}"
                )
            if len(ref) > MAX_REF_CHARS:
                raise ValueError("secret ref exceeds bounds")
        return value

    @model_validator(mode="after")
    def _transport_rules(self) -> MCPConfig:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio servers require command")
            if self.url is not None:
                raise ValueError("stdio servers must not set url")
            if self.header_secret_refs:
                raise ValueError("header_secret_refs are http-only")
        else:
            if not self.url:
                raise ValueError("http servers require url")
            if self.command is not None or self.args:
                raise ValueError("http servers must not set command/args")
            if self.env_secret_refs:
                raise ValueError("env_secret_refs are stdio-only")
            self._check_url(self.url)
        return self

    @staticmethod
    def _check_url(url: str) -> None:
        if len(url) > MAX_URL_CHARS:
            raise ValueError("url exceeds 2048 chars")
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise ValueError(f"unparseable url: {exc}") from exc
        if parts.scheme not in ("http", "https"):
            raise ValueError("url scheme must be http or https")
        if not parts.hostname:
            raise ValueError("url needs a host")
        _ = parts.port
        if parts.username or parts.password or "@" in (parts.netloc or ""):
            raise ValueError("url must not carry userinfo/credentials")
        if parts.fragment:
            raise ValueError("url must not carry a fragment")
        for key, _ in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in SECRET_QUERY_KEYS:
                raise ValueError(f"url query must not carry secret key {key!r}")

    def sanitized_endpoint(self) -> str:
        if self.transport == "stdio":
            return f"stdio:{self.command}"
        return _sanitize_url(self.url or "")


def _bwrap_base_argv(config: MCPConfig, policy: ToolPolicy) -> list[str]:
    """Bubblewrap argv without secrets. Fails closed without the sandbox."""
    if not policy.effective_shell:
        raise PolicyDenied(f"mcp stdio server {config.id!r} requires the shell grant")
    joined = shlex.join([config.command or "", *config.args])
    argv, _isolated = build_bwrap_args(policy, joined, policy.network)
    if len(argv) < 4 or argv[-3:-1] != ["/bin/bash", "-c"]:
        raise MCPTransportError("unexpected bubblewrap argv shape; refusing to launch")
    if not Path("/usr/bin/prlimit").is_file():
        raise MCPTransportError("MCP stdio needs /usr/bin/prlimit for process limits")
    return [
        "/usr/bin/prlimit",
        f"--as={MAX_STDIO_MEMORY_BYTES}:{MAX_STDIO_MEMORY_BYTES}",
        "--cpu=120:120",
        "--nofile=128:128",
        "--fsize=16777216:16777216",
        "--",
        *argv,
    ]


def _secure_spawn_argv(
    base_argv: list[str], secret_env: dict[str, str]
) -> tuple[list[str], int | None]:
    """Hide ``--setenv`` secrets in an anonymous memfd read via ``--args FD``.

    Returns ``(argv_for_spawn, memfd)``. ``memfd`` is None when there are no
    secrets; otherwise the caller must pass it in ``pass_fds`` and close the
    parent copy right after spawn. The returned argv never contains a raw
    secret value (verified before return).

    The ``--args FD`` triplet is inserted immediately before the trailing
    ``/bin/bash -c <command>`` so the memfd ``--setenv`` pairs are applied
    AFTER the base ``--clearenv``. Inserting them at the front would let
    ``--clearenv`` wipe every secret (silent empty delivery).
    """
    if not secret_env:
        return list(base_argv), None
    if len(base_argv) < 3 or base_argv[-3:-1] != ["/bin/bash", "-c"]:
        raise MCPTransportError("unexpected bubblewrap argv shape; refusing to launch")
    fd = _stdio_relay.create_args_memfd(secret_env)
    try:
        argv = [*base_argv[:-3], "--args", str(fd), *base_argv[-3:]]
    except Exception:
        os.close(fd)
        raise
    leaked = _stdio_relay.argv_contains_secret(argv, list(secret_env.values()))
    if leaked is not None:
        os.close(fd)
        raise MCPTransportError("refusing to spawn: secret value present in argv")
    return argv, fd


def _check_caps(tools: list[Any], server_id: str) -> dict[str, dict[str, Any]]:
    """Validate tool list/schema bounds on discovery AND call refresh."""
    if len(tools) > MAX_LISTED_TOOLS:
        raise MCPTransportError(
            f"mcp {server_id!r} lists {len(tools)} tools, limit is {MAX_LISTED_TOOLS}"
        )
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools:
        name = getattr(tool, "name", "")
        if (
            not isinstance(name, str)
            or not _TOOL_NAME.fullmatch(name)
            or len(name) > MAX_TOOL_NAME_CHARS
            or name in schemas
        ):
            raise MCPTransportError(f"mcp {server_id!r} published an unusable tool name")
        schema = getattr(tool, "inputSchema", None) or {}
        if not isinstance(schema, dict):
            raise MCPTransportError(f"mcp {server_id!r} tool {name!r} has no object schema")
        if len(json.dumps(schema)) > MAX_SCHEMA_CHARS:
            raise MCPTransportError(f"mcp {server_id!r} tool {name!r} schema exceeds bounds")
        _check_schema_profile(schema)
        schemas[name] = schema
    return schemas


def _check_schema_profile(schema: dict) -> None:
    # No retrieval or untrusted regular expressions in the initial MCP profile.
    budget = 512
    blocked = {"$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties"}
    schema_maps = {"properties", "$defs", "definitions", "dependentSchemas"}
    schema_lists = {"allOf", "anyOf", "oneOf", "prefixItems"}
    schema_values = {
        "items",
        "additionalProperties",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
    }

    def visit(node, depth=0, is_schema=True):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 12:
            raise MCPTransportError("MCP schema exceeds structural bounds")
        if isinstance(node, dict):
            if is_schema and blocked.intersection(node):
                raise MCPTransportError(
                    "MCP schema references and regular expressions are unsupported"
                )
            for key, value in node.items():
                if is_schema and key in schema_maps and isinstance(value, dict):
                    for child in value.values():
                        visit(child, depth + 1)
                elif is_schema and key in schema_lists and isinstance(value, list):
                    for child in value:
                        visit(child, depth + 1)
                else:
                    visit(value, depth + 1, is_schema and key in schema_values)
        elif isinstance(node, list):
            for value in node:
                visit(value, depth + 1, is_schema)

    visit(schema)


def _kill_group(pid: int) -> None:
    """Best-effort SIGTERM then SIGKILL to a spawned process group."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return


def _kill_group_force(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return


async def _reap_with_timeout(process: Any) -> None:
    """Wait for exit; escalate SIGTERM -> SIGKILL on the process group."""
    try:
        with anyio.fail_after(TERMINATION_TIMEOUT):
            await process.wait()
            return
    except TimeoutError:
        pass
    try:
        _kill_group(process.pid)
    except (AttributeError, ValueError):
        pass
    try:
        with anyio.fail_after(TERMINATION_TIMEOUT):
            await process.wait()
            return
    except TimeoutError:
        pass
    try:
        _kill_group_force(process.pid)
    except (AttributeError, ValueError):
        pass
    with anyio.fail_after(TERMINATION_TIMEOUT):
        await process.wait()


def _http_body_cap(config: MCPConfig) -> int:
    """Pre-decode byte cap for one HTTP response body.

    Must comfortably cover the worst-case encoding of a max-size result
    (JSON escaping can expand text several-fold) while staying bounded.
    """
    return max(FRAME_CAP, 8 * config.max_result_chars)


class _BodyCapStream(httpx.AsyncByteStream):
    """Byte-counting response stream: fail closed before SDK decode."""

    def __init__(self, inner: httpx.AsyncByteStream, cap: int, fatal: list[MCPError]) -> None:
        self._inner = inner
        self._cap = cap
        self._fatal = fatal

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > self._cap:
                err = MCPTransportError(
                    f"http response body exceeds {self._cap} bytes; refused without decoding"
                )
                self._fatal.append(err)
                raise err
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _BodyCapTransport(httpx.AsyncHTTPTransport):
    """HTTP transport wrapper enforcing a pre-decode response byte cap."""

    def __init__(self, cap: int, fatal: list[MCPError]) -> None:
        super().__init__(limits=httpx.Limits(max_connections=4, max_keepalive_connections=0))
        self._cap = cap
        self._fatal = fatal

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            error = MCPTransportError("encoded MCP HTTP bodies are unsupported")
            self._fatal.append(error)
            raise error
        response.stream = _BodyCapStream(response.stream, self._cap, self._fatal)
        return response


def _http_client_for(
    config: MCPConfig,
    headers: dict[str, str] | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Explicit HTTP client: no redirects, no ambient env, bounded timeouts."""
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(config.timeout_seconds),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
        headers={"Accept-Encoding": "identity", **(headers or {})},
    )


@asynccontextmanager
async def _bounded_stdio_transport(
    server_id: str,
    argv: list[str],
    secrets: list[str],
    timeout_seconds: float,
    memfd: int | None,
    pass_fds: tuple[int, ...],
    fatal: list[MCPError] | None = None,
):
    """Spawn bwrap with capped JSONL framing in both directions.

    All framing happens here, BEFORE the MCP SDK decodes anything: stdout
    frames longer than FRAME_CAP (or unterminated past the cap) kill the
    process group, record a fatal error, and close the stream without
    forwarding a single byte. The owner surfaces the recorded fatal instead
    of a generic timeout. Stderr is hard-capped. No unbounded
    readline/communicate buffers are used. Fatal errors are recorded rather
    than raised inside the transport task group so session teardown stays on
    the owner task and is never masked by a cross-task cancel-scope exit.
    """
    leaked = _stdio_relay.argv_contains_secret(argv, secrets)
    if leaked is not None:
        if memfd is not None:
            os.close(memfd)
        raise MCPTransportError("refusing to spawn: secret value present in argv")
    try:
        process = await anyio.open_process(
            argv,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
            pass_fds=pass_fds,
        )
    finally:
        if memfd is not None:
            try:
                os.close(memfd)
            except OSError:
                pass
    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)
    err_holder: list[bytes] = [b""]

    fatal_box: list[MCPError] = fatal if fatal is not None else []

    async def stdout_reader() -> None:
        assert process.stdout is not None
        buffer = bytearray()
        try:
            async with read_writer:
                while True:
                    try:
                        chunk = await process.stdout.receive(65536)
                    except anyio.EndOfStream:
                        break
                    buffer += chunk
                    frames, over = _stdio_relay.frame_complete(buffer, FRAME_CAP)
                    if over:
                        _kill_group(process.pid)
                        fatal_box.append(
                            MCPTransportError(
                                f"mcp {server_id!r} stdout frame exceeds "
                                f"{FRAME_CAP} bytes; killed without decoding"
                            )
                        )
                        break
                    for frame in frames:
                        try:
                            message = JSONRPCMessage.model_validate_json(frame)
                        except Exception as exc:
                            await read_writer.send(exc)
                            continue
                        await read_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def stdin_writer() -> None:
        assert process.stdin is not None
        try:
            async with write_reader:
                while True:
                    try:
                        session_message = await write_reader.receive()
                    except anyio.EndOfStream:
                        break
                    raw = (
                        session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        ).encode("utf-8")
                        + b"\n"
                    )
                    if len(raw) > INPUT_FRAME_CAP:
                        _kill_group(process.pid)
                        fatal_box.append(
                            MCPTransportError(
                                f"mcp {server_id!r} outbound frame exceeds bounds; refused"
                            )
                        )
                        break
                    await process.stdin.send(raw)
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def stderr_drain() -> None:
        assert process.stderr is not None
        collected = bytearray()
        try:
            while True:
                try:
                    chunk = await process.stderr.receive(32768)
                except anyio.EndOfStream:
                    break
                room = STDERR_CAP - len(collected)
                if room > 0:
                    collected += chunk[:room]
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()
        finally:
            err_holder[0] = bytes(collected)

    async with process:
        async with anyio.create_task_group() as tg:
            tg.start_soon(stdout_reader)
            tg.start_soon(stdin_writer)
            tg.start_soon(stderr_drain)
            try:
                yield read_stream, write_stream, err_holder
            finally:
                if process.stdin is not None:
                    try:
                        await process.stdin.aclose()
                    except Exception:
                        pass
                try:
                    await _reap_with_timeout(process)
                finally:
                    tg.cancel_scope.cancel()
                await read_writer.aclose()
                await write_stream.aclose()


def _consume_future(fut: asyncio.Future) -> None:
    try:
        fut.exception()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


class _ServerActor:
    """Dedicated owner task for one server. Owns every SDK context end-to-end."""

    def __init__(
        self,
        config: MCPConfig,
        policy: ToolPolicy,
        resolver: SecretResolver | None,
    ) -> None:
        self.config = config.model_copy(deep=True)
        self._policy = policy
        self._resolver = resolver
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._current: asyncio.Future | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name=f"mcp-actor-{self.config.id}"
            )

    async def submit(self, op: dict[str, Any]) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        op["future"] = fut
        try:
            self._queue.put_nowait(op)
        except asyncio.QueueFull as exc:
            raise MCPTransportError("mcp request queue is full") from exc
        return fut

    def cancel_operation(self, future: asyncio.Future) -> None:
        if self._current is future and self._task is not None:
            self._task.cancel()
        else:
            future.cancel()

    async def stop(self) -> None:
        if self._stopped:
            task = self._task
            if task is not None:
                try:
                    await asyncio.shield(asyncio.wait_for(asyncio.shield(task), timeout=30))
                except (TimeoutError, asyncio.CancelledError, Exception):
                    pass
            return
        self._stopped = True
        if self._task is None:
            return
        while not self._queue.empty():
            pending = self._queue.get_nowait()["future"]
            if not pending.done():
                pending.set_exception(MCPTransportError("MCP host closed before request started"))
        if self._current is not None:
            self._task.cancel()
        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()
        await self._queue.put({"kind": "stop", "future": done})
        try:
            await asyncio.wait_for(asyncio.shield(done), timeout=30)
        finally:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=30)
            except (TimeoutError, asyncio.CancelledError, Exception):
                if not self._task.done():
                    self._task.cancel()

    async def _run(self) -> None:
        stack = AsyncExitStack()
        session: ClientSession | None = None
        schemas: dict[str, dict[str, Any]] = {}
        secrets: list[str] = []
        stderr_holder: list[bytes] = [b""]
        fatal: list[MCPError] = []
        config = self.config

        def current_secrets() -> list[str]:
            return list(secrets)

        def take_fatal() -> list[MCPError]:
            """Snapshot transport-recorded pre-decode failures (never raised in-situ)."""
            return list(fatal)

        async def reset() -> None:
            nonlocal session, schemas, secrets, stderr_holder, fatal, stack
            session = None
            schemas = {}
            # Retain the previous connection secrets until error formatting has
            # finished. ensure() replaces them before the next connection.
            stderr_holder = [b""]
            fatal = []
            old = stack
            stack = AsyncExitStack()
            await old.aclose()

        async def ensure() -> ClientSession:
            nonlocal session, secrets, stderr_holder, fatal
            if session is not None:
                return session
            resolved: dict[str, str] = {}
            if config.env_secret_refs or config.header_secret_refs:
                if self._resolver is None:
                    raise MCPError(
                        f"mcp {config.id!r} needs secret refs but no resolver is configured"
                    )
                for name, ref in {**config.env_secret_refs, **config.header_secret_refs}.items():
                    value = self._resolver(ref)
                    if (
                        not isinstance(value, str)
                        or not value
                        or len(value) > 8192
                        or "\x00" in value
                        or (config.transport == "http" and ("\r" in value or "\n" in value))
                    ):
                        raise MCPTransportError(
                            f"mcp {config.id!r} secret ref {ref!r} did not resolve"
                        )
                    resolved[name] = value
            secrets = list(resolved.values())
            if config.transport == "stdio":
                base = _bwrap_base_argv(config, self._policy)
                secret_env = {
                    name: resolved[name] for name in config.env_secret_refs if name in resolved
                }
                argv, memfd = _secure_spawn_argv(base, secret_env)
                pass_fds: tuple[int, ...] = (memfd,) if memfd is not None else ()
                fatal = []
                entered = await stack.enter_async_context(
                    _bounded_stdio_transport(
                        config.id,
                        argv,
                        list(resolved.values()),
                        config.timeout_seconds,
                        memfd,
                        pass_fds,
                        fatal,
                    )
                )
                read, write, err_holder = entered
                stderr_holder = err_holder
            else:
                if not self._policy.network:
                    raise PolicyDenied(f"mcp http server {config.id!r} requires the network grant")
                if not config.allow_remote:
                    raise PolicyDenied(
                        f"mcp http server {config.id!r} requires explicit allow_remote"
                    )
                parts = urlsplit(config.url or "")
                host = parts.hostname or ""
                if parts.scheme != "https" and not _is_loopback(host):
                    raise PolicyDenied(
                        f"mcp http server {config.id!r} needs https outside loopback"
                    )
                headers = {
                    name: resolved[name] for name in config.header_secret_refs if name in resolved
                }
                fatal = []
                transport = _BodyCapTransport(_http_body_cap(config), fatal)
                client = _http_client_for(config, headers or None, transport)
                await stack.enter_async_context(client)
                streams = await stack.enter_async_context(
                    streamable_http_client(config.url or "", http_client=client)
                )
                read, write = streams[0], streams[1]
            sess = await stack.enter_async_context(ClientSession(read, write))
            try:
                await asyncio.wait_for(sess.initialize(), timeout=config.timeout_seconds)
            except asyncio.CancelledError:
                await reset()
                raise
            except Exception:
                pending = take_fatal()
                tail = _scrubbed_stderr_tail(stderr_holder, current_secrets())
                await reset()
                if pending:
                    raise pending[0] from None
                if tail:
                    raise MCPTransportError(
                        f"mcp {config.id!r} handshake failed via "
                        f"{config.sanitized_endpoint()}: server stderr tail {tail!r}"
                    ) from None
                raise
            session = sess
            return sess

        async def do_discover(fut: asyncio.Future) -> None:
            nonlocal schemas
            try:
                sess = await ensure()
            except (PolicyDenied, MCPError, MCPTransportError) as exc:
                if not fut.done():
                    fut.set_exception(exc)
                return
            except asyncio.CancelledError:
                await reset()
                if not fut.done():
                    fut.cancel()
                return
            except Exception as exc:  # noqa: BLE001 - surface handshake faults, scrubbed
                err = MCPTransportError(
                    f"mcp {config.id!r} handshake failed via {config.sanitized_endpoint()}: "
                    f"{type(exc).__name__} {_scrub(str(exc), current_secrets())}"
                )
                try:
                    await reset()
                except Exception as reset_exc:  # noqa: BLE001 - never claim clean on failure
                    err = MCPTransportError(
                        f"mcp {config.id!r} handshake failed and cleanup failed: "
                        f"{_scrub(str(reset_exc), current_secrets())}"
                    )
                if not fut.done():
                    fut.set_exception(err)
                return
            last: Exception | None = None
            for _ in range(2):
                try:
                    result = await asyncio.wait_for(
                        sess.list_tools(), timeout=config.timeout_seconds
                    )
                    tools = result.tools if hasattr(result, "tools") else []
                    try:
                        schemas = _check_caps(list(tools), config.id)
                    except MCPTransportError as exc:
                        await reset()
                        if not fut.done():
                            fut.set_exception(exc)
                        return
                    out = []
                    for tool in tools:
                        description = str(getattr(tool, "description", "") or "")
                        if len(description) > MAX_DESCRIPTION_CHARS:
                            description = (
                                description[:MAX_DESCRIPTION_CHARS] + "…[truncated metadata]"
                            )
                        out.append(
                            {
                                "name": tool.name,
                                "description": _scrub_any(description, current_secrets()),
                                "inputSchema": _scrub_any(schemas[tool.name], current_secrets()),
                                "allowed": tool.name in config.allowed_tools,
                            }
                        )
                    if not fut.done():
                        fut.set_result(out)
                    return
                except (MCPTransportError, TimeoutError) as exc:
                    last = exc
                    pending = take_fatal()
                    tail = _scrubbed_stderr_tail(stderr_holder, current_secrets())
                    try:
                        await reset()
                    except Exception as reset_exc:  # noqa: BLE001
                        if not fut.done():
                            fut.set_exception(
                                MCPTransportError(
                                    f"mcp {config.id!r} listing failed and cleanup failed: "
                                    f"{_scrub(str(reset_exc), current_secrets())}"
                                )
                            )
                        return
                    if pending:
                        # Pre-decode transport refusal is deterministic: no reconnect.
                        if not fut.done():
                            fut.set_exception(pending[0])
                        return
                    if isinstance(last, TimeoutError) and tail:
                        if not fut.done():
                            fut.set_exception(
                                MCPTransportError(
                                    f"mcp {config.id!r} listing timed out; "
                                    f"server stderr tail {tail!r}"
                                )
                            )
                        return
                    try:
                        sess = await ensure()
                    except Exception as exc2:  # noqa: BLE001
                        if not fut.done():
                            fut.set_exception(exc2)
                        return
                except asyncio.CancelledError:
                    try:
                        await reset()
                    finally:
                        if not fut.done():
                            fut.cancel()
                    return
                except Exception as exc:  # noqa: BLE001
                    pending = take_fatal()
                    tail = _scrubbed_stderr_tail(stderr_holder, current_secrets())
                    try:
                        await reset()
                    finally:
                        if not fut.done():
                            if pending:
                                fut.set_exception(pending[0])
                            else:
                                note = f"; server stderr tail {tail!r}" if tail else ""
                                fut.set_exception(
                                    MCPTransportError(
                                        f"mcp {config.id!r} listing failed: "
                                        f"{type(exc).__name__} "
                                        f"{_scrub(str(exc), current_secrets())}{note}"
                                    )
                                )
                    return
            if not fut.done():
                if isinstance(last, TimeoutError):
                    fut.set_exception(MCPTransportError(f"mcp {config.id!r} listing timed out"))
                elif last is not None:
                    fut.set_exception(last)
                else:
                    fut.set_exception(MCPTransportError(f"mcp {config.id!r} listing failed"))

        async def do_call(tool: str, args: dict[str, Any] | None, fut: asyncio.Future) -> None:
            nonlocal schemas
            if tool not in config.allowed_tools:
                if not fut.done():
                    fut.set_exception(
                        PolicyDenied(f"mcp tool {tool!r} on {config.id!r} is not allowlisted")
                    )
                return
            if not isinstance(args, dict):
                if not fut.done():
                    fut.set_exception(
                        MCPToolError(
                            f"mcp tool {tool!r} needs an object argument, got {type(args).__name__}"
                        )
                    )
                return
            if len(args) > MAX_ARG_KEYS:
                if not fut.done():
                    fut.set_exception(MCPToolError(f"mcp tool {tool!r} too many argument keys"))
                return
            try:
                raw_args = json.dumps(args)
            except (TypeError, ValueError) as exc:
                if not fut.done():
                    fut.set_exception(MCPToolError(f"mcp tool {tool!r} arguments not JSON: {exc}"))
                return
            if len(raw_args) > MAX_ARGS_CHARS:
                if not fut.done():
                    fut.set_exception(
                        MCPToolError(f"mcp tool {tool!r} arguments exceed {MAX_ARGS_CHARS} chars")
                    )
                return
            try:
                sess = await ensure()
            except Exception as exc:  # noqa: BLE001 - PolicyDenied/MCPError pass through
                if not fut.done():
                    fut.set_exception(exc)
                return
            schema = schemas.get(tool)
            if schema is None:
                try:
                    result = await asyncio.wait_for(
                        sess.list_tools(), timeout=config.timeout_seconds
                    )
                    schemas = _check_caps(list(result.tools or []), config.id)
                    schema = schemas.get(tool)
                except asyncio.CancelledError:
                    try:
                        await reset()
                    finally:
                        if not fut.done():
                            fut.cancel()
                    return
                except Exception as exc:  # noqa: BLE001
                    pending = take_fatal()
                    try:
                        await reset()
                    finally:
                        if not fut.done():
                            if pending:
                                fut.set_exception(pending[0])
                            else:
                                fut.set_exception(
                                    MCPTransportError(
                                        f"mcp {config.id!r} listing failed: "
                                        f"{type(exc).__name__} "
                                        f"{_scrub(str(exc), current_secrets())}"
                                    )
                                )
                    return
            if schema is None:
                if not fut.done():
                    fut.set_exception(
                        MCPToolError(f"mcp {config.id!r} does not offer tool {tool!r}")
                    )
                return
            try:
                jsonschema.validate(args, schema)
            except jsonschema.ValidationError as exc:
                path = "/".join(str(part) for part in exc.absolute_path) or "(root)"
                if not fut.done():
                    fut.set_exception(
                        MCPToolError(
                            f"mcp tool {tool!r} arguments failed schema: "
                            f"{exc.validator!r} at {path}"
                        )
                    )
                return
            except jsonschema.SchemaError:
                if not fut.done():
                    fut.set_exception(
                        MCPToolError(f"mcp {config.id!r} tool {tool!r} schema is invalid")
                    )
                return
            try:
                result = await asyncio.wait_for(
                    sess.call_tool(
                        tool,
                        arguments=args,
                        read_timeout_seconds=timedelta(seconds=config.timeout_seconds),
                    ),
                    timeout=config.timeout_seconds,
                )
            except TimeoutError:
                pending = take_fatal()
                try:
                    await reset()
                finally:
                    if not fut.done():
                        if pending:
                            fut.set_exception(pending[0])
                        else:
                            fut.set_exception(
                                MCPTransportError(
                                    f"mcp {config.id!r} tool {tool!r} timed out after "
                                    f"{config.timeout_seconds:g}s; outcome uncertain, not retried"
                                )
                            )
                return
            except asyncio.CancelledError:
                try:
                    await reset()
                finally:
                    if not fut.done():
                        fut.cancel()
                return
            except Exception as exc:  # noqa: BLE001
                pending = take_fatal()
                try:
                    await reset()
                finally:
                    if not fut.done():
                        if pending:
                            fut.set_exception(pending[0])
                        else:
                            fut.set_exception(
                                MCPTransportError(
                                    f"mcp {config.id!r} tool {tool!r} failed via "
                                    f"{config.sanitized_endpoint()}: {type(exc).__name__} "
                                    f"{_scrub(str(exc), current_secrets())}"
                                )
                            )
                return
            if getattr(result, "isError", False):
                text = _scrub_any(_text_of(result), current_secrets())
                if not fut.done():
                    fut.set_exception(
                        MCPToolError(
                            f"mcp {config.id!r} tool {tool!r} reported error: {text[:500]}"
                        )
                    )
                return
            blocks = [
                _scrub_any(_block_of(block), current_secrets()) for block in (result.content or [])
            ]
            structured = _scrub_any(
                json.loads(json.dumps(result.structuredContent, default=str))
                if result.structuredContent is not None
                else None,
                current_secrets(),
            )
            payload = {
                "origin": "mcp",
                "server": config.id,
                "tool": tool,
                "untrusted": True,
                "isError": False,
                "content": blocks,
                "structured": structured,
            }
            try:
                encoded = json.dumps(payload, default=str)
            except (TypeError, ValueError) as exc:
                if not fut.done():
                    fut.set_exception(MCPToolError(f"mcp tool {tool!r} result not JSON: {exc}"))
                return
            if len(encoded) > config.max_result_chars:
                if not fut.done():
                    fut.set_exception(
                        MCPToolError(
                            f"mcp {config.id!r} tool {tool!r} result exceeds "
                            f"{config.max_result_chars} chars; refusing to truncate a tool result"
                        )
                    )
                return
            if not fut.done():
                fut.set_result(json.loads(encoded))

        while True:
            op = await self._queue.get()
            kind = op.get("kind")
            fut = op.get("future")
            if fut is not None and fut.cancelled():
                continue
            self._current = fut
            try:
                if kind == "stop":
                    try:
                        await reset()
                    except Exception as exc:  # noqa: BLE001 - report close failure honestly
                        if fut is not None and not fut.done():
                            fut.set_exception(
                                MCPTransportError(
                                    f"mcp {config.id!r} close failed: "
                                    f"{_scrub(str(exc), current_secrets())}"
                                )
                            )
                        # The stop requester awaits `fut`, so the failure is
                        # loud; return (do not raise into the queue loop, which
                        # would leave a stopped actor parked on the queue and
                        # stall close() on its 30s task-watch fallback).
                        return
                    if fut is not None and not fut.done():
                        fut.set_result(None)
                    return
                if kind == "discover":
                    assert fut is not None
                    await do_discover(fut)
                elif kind == "call":
                    assert fut is not None
                    await do_call(op["tool"], op.get("args"), fut)
                else:
                    if fut is not None and not fut.done():
                        fut.set_exception(MCPError(f"unknown op {kind!r}"))
            except asyncio.CancelledError:
                try:
                    await reset()
                finally:
                    if fut is not None and not fut.done():
                        fut.cancel()
                    raise
            except Exception as exc:  # noqa: BLE001 - actor survives; op gets the fault
                if fut is not None and not fut.done():
                    fut.set_exception(exc)
            finally:
                self._current = None


def _block_of(block: Any) -> Any:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", by_alias=True, exclude_none=True)
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return {"type": "unknown", "text": text[:2000]}
    return {"type": "unknown", "text": str(block)[:2000]}


def _text_of(result: Any) -> str:
    texts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            texts.append(text)
    return "\n".join(texts)


class MCPHost:
    """Brokers owner-configured MCP servers. Untrusted results, bounded I/O."""

    def __init__(
        self,
        configs: list[MCPConfig],
        policy: ToolPolicy,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = secret_resolver
        self._configs: dict[str, MCPConfig] = {}
        self._actors: dict[str, _ServerActor] = {}
        self._host_lock = asyncio.Lock()
        self._closed = False
        if len(configs) > 16:
            raise ValueError("at most 16 MCP servers are supported")
        for config in configs:
            if config.id in self._configs:
                raise ValueError(f"duplicate MCP server id {config.id!r}")
            frozen = MCPConfig.model_validate(config.model_dump())
            self._configs[frozen.id] = frozen

    def __repr__(self) -> str:
        return f"MCPHost(servers={sorted(self._configs)!r})"

    def _actor_for(self, server_id: str) -> _ServerActor:
        actor = self._actors.get(server_id)
        if actor is None:
            actor = _ServerActor(self._configs[server_id], self._policy, self._resolver)
            actor.start()
            self._actors[server_id] = actor
        return actor

    async def replace_configs(self, *, actor: str, configs: list[MCPConfig]) -> None:
        """Owner/lead-only reconfiguration; closes every live session first."""
        if actor != "owner":
            raise PolicyDenied(f"permission change requires owner, got {actor!r}")
        if len(configs) > 16:
            raise ValueError("at most 16 MCP servers are supported")
        async with self._host_lock:
            for server_actor in list(self._actors.values()):
                await server_actor.stop()
            self._actors = {}
            fresh: dict[str, MCPConfig] = {}
            for config in configs:
                if config.id in fresh:
                    raise ValueError(f"duplicate MCP server id {config.id!r}")
                fresh[config.id] = MCPConfig.model_validate(config.model_dump())
            self._configs = fresh
            self._closed = False

    def list_servers(self) -> list[dict[str, Any]]:
        """Metadata only: never secret values, never credentials."""
        out = []
        for config in self._configs.values():
            out.append(
                {
                    "id": config.id,
                    "transport": config.transport,
                    "enabled": config.enabled,
                    "allowed_tools": sorted(config.allowed_tools),
                    "endpoint": config.sanitized_endpoint(),
                    "timeout_seconds": config.timeout_seconds,
                    "allow_remote": config.allow_remote,
                    "secret_refs": sorted([*config.env_secret_refs, *config.header_secret_refs]),
                }
            )
        return copy.deepcopy(out)

    async def _submit(self, server_id: str, op: dict[str, Any], timeout: float) -> Any:
        async with self._host_lock:
            if self._closed:
                raise MCPTransportError("mcp host is closed")
            config = self._configs.get(server_id)
            if config is None:
                raise MCPError(f"unknown mcp server {server_id!r}")
            if not config.enabled:
                raise PolicyDenied(f"mcp server {server_id!r} is disabled")
            actor = self._actor_for(server_id)
            fut = await actor.submit(op)
        bound = timeout + 30.0
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=bound)
        except TimeoutError as exc:
            actor.cancel_operation(fut)
            fut.add_done_callback(_consume_future)
            raise MCPTransportError(
                f"mcp {server_id!r} op timed out after {bound:g}s; outcome uncertain, not retried"
            ) from exc
        except asyncio.CancelledError:
            actor.cancel_operation(fut)
            fut.add_done_callback(_consume_future)
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=10)
            except (Exception, asyncio.CancelledError):
                pass
            raise

    async def discover(self, server_id: str) -> list[dict[str, Any]]:
        """Bounded tool metadata. Discovery grants no call authority."""
        async with self._host_lock:
            if self._closed:
                raise MCPTransportError("mcp host is closed")
            config = self._configs.get(server_id)
            if config is None:
                raise MCPError(f"unknown mcp server {server_id!r}")
            if not config.enabled:
                raise PolicyDenied(f"mcp server {server_id!r} is disabled")
            timeout = config.timeout_seconds
        return await self._submit(server_id, {"kind": "discover"}, timeout)

    async def call(self, server_id: str, tool: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """Call one allowlisted tool. Denied or invalid calls are never sent."""
        async with self._host_lock:
            if self._closed:
                raise MCPTransportError("mcp host is closed")
            config = self._configs.get(server_id)
            if config is None:
                raise MCPError(f"unknown mcp server {server_id!r}")
            if not config.enabled:
                raise PolicyDenied(f"mcp server {server_id!r} is disabled")
            if tool not in config.allowed_tools:
                raise PolicyDenied(
                    f"mcp tool {tool!r} on {server_id!r} is not in the owner allowlist"
                )
            timeout = config.timeout_seconds
        if args is not None and not isinstance(args, dict):
            raise MCPToolError("MCP arguments must be an object")
        try:
            if len(json.dumps(args)) > MAX_ARGS_CHARS:
                raise MCPToolError("MCP arguments exceed bounds")
        except (ValueError, TypeError) as exc:
            raise MCPToolError("MCP arguments must be JSON") from exc
        return await self._submit(
            server_id, {"kind": "call", "tool": tool, "args": copy.deepcopy(args)}, timeout
        )

    async def close(self) -> None:
        async with self._host_lock:
            if self._closed and not self._actors:
                return
            actors = list(self._actors.values())
            self._actors = {}
            self._closed = True
        for actor in actors:
            await actor.stop()
