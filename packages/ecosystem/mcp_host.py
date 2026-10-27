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
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import shlex
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import jsonschema
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.policy import PolicyDenied, ToolPolicy
from services.agent.tools import build_bwrap_args

SecretResolver = Callable[[str], str | None]

MAX_LISTED_TOOLS = 128
MAX_TOOL_NAME_CHARS = 128
MAX_DESCRIPTION_CHARS = 2000
MAX_SCHEMA_CHARS = 32768
MAX_ARGS_CHARS = 65536
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
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
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
        if value and len(value) >= 4 and value in text:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"\?[^\s\"']*", "?[query-redacted]", text)
    text = re.sub(r"://[^/\s@]+@", "://[redacted]@", text)
    return " ".join(text.split())[:300]


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
        for item in value:
            if not isinstance(item, str) or len(item) > 4096:
                raise ValueError("each arg must be a string of at most 4096 chars")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def _exact_tool_names(cls, value: list[str]) -> list[str]:
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
        for name, ref in value.items():
            if not _ENV_NAME.fullmatch(name) or not isinstance(ref, str) or not ref.strip():
                raise ValueError(f"env_secret_refs needs ENV-NAME -> non-empty ref, got {name!r}")
        return value

    @field_validator("header_secret_refs")
    @classmethod
    def _header_refs(cls, value: dict[str, str]) -> dict[str, str]:
        for name, ref in value.items():
            if not _HEADER_NAME.fullmatch(name) or not isinstance(ref, str) or not ref.strip():
                raise ValueError(
                    f"header_secret_refs needs header-name -> non-empty ref, got {name!r}"
                )
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
        if len(url) > 2048:
            raise ValueError("url exceeds 2048 chars")
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise ValueError(f"unparseable url: {exc}") from exc
        if parts.scheme not in ("http", "https"):
            raise ValueError("url scheme must be http or https")
        if not parts.hostname:
            raise ValueError("url needs a host")
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
        self._locks: dict[str, asyncio.Lock] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._schemas: dict[str, dict[str, dict[str, Any]]] = {}
        self._closed = False
        for config in configs:
            if config.id in self._configs:
                raise ValueError(f"duplicate MCP server id {config.id!r}")
            self._configs[config.id] = config
            self._locks[config.id] = asyncio.Lock()

    def __repr__(self) -> str:
        return f"MCPHost(servers={sorted(self._configs)!r})"

    async def replace_configs(self, *, actor: str, configs: list[MCPConfig]) -> None:
        """Owner/lead-only reconfiguration; closes every live session first."""
        if actor != "owner":
            raise PolicyDenied(f"permission change requires owner, got {actor!r}")
        await self.close()
        fresh = MCPHost(configs, self._policy, self._resolver)
        self._configs, self._locks = fresh._configs, fresh._locks
        self._schemas = {}
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
        return out

    async def discover(self, server_id: str) -> list[dict[str, Any]]:
        """Bounded tool metadata. Discovery grants no call authority."""
        config = self._require_enabled(server_id)
        async with self._locks[server_id]:
            result = await self._with_reconnect(config, read_only=True)
            tools = result.tools if hasattr(result, "tools") else []
            if len(tools) > MAX_LISTED_TOOLS:
                raise MCPTransportError(
                    f"mcp {server_id!r} lists {len(tools)} tools, limit is {MAX_LISTED_TOOLS}"
                )
            schemas: dict[str, dict[str, Any]] = {}
            out = []
            for tool in tools:
                name = getattr(tool, "name", "")
                if not isinstance(name, str) or not name or len(name) > MAX_TOOL_NAME_CHARS:
                    raise MCPTransportError(f"mcp {server_id!r} published an unusable tool name")
                schema = getattr(tool, "inputSchema", None) or {}
                if not isinstance(schema, dict):
                    raise MCPTransportError(f"mcp {server_id!r} tool {name!r} has no object schema")
                if len(json.dumps(schema)) > MAX_SCHEMA_CHARS:
                    raise MCPTransportError(
                        f"mcp {server_id!r} tool {name!r} schema exceeds bounds"
                    )
                description = str(getattr(tool, "description", "") or "")
                if len(description) > MAX_DESCRIPTION_CHARS:
                    description = description[:MAX_DESCRIPTION_CHARS] + "…[truncated metadata]"
                schemas[name] = schema
                out.append(
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": schema,
                        "allowed": name in config.allowed_tools,
                    }
                )
            self._schemas[server_id] = schemas
            return out

    async def call(self, server_id: str, tool: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """Call one allowlisted tool. Denied or invalid calls are never sent."""
        config = self._require_enabled(server_id)
        if tool not in config.allowed_tools:
            raise PolicyDenied(f"mcp tool {tool!r} on {server_id!r} is not in the owner allowlist")
        if not isinstance(args, dict):
            raise MCPToolError(
                f"mcp tool {tool!r} needs an object argument, got {type(args).__name__}"
            )
        raw_args = json.dumps(args)
        if len(raw_args) > MAX_ARGS_CHARS:
            raise MCPToolError(f"mcp tool {tool!r} arguments exceed {MAX_ARGS_CHARS} chars")
        async with self._locks[server_id]:
            session = await self._ensure_session_locked(config, [])
            schema = self._schemas.get(server_id, {}).get(tool)
            if schema is None:
                await self._refresh_schemas_locked(config, session)
                schema = self._schemas.get(server_id, {}).get(tool)
            if schema is None:
                raise MCPToolError(f"mcp {server_id!r} does not offer tool {tool!r}")
            try:
                jsonschema.validate(args, schema)
            except jsonschema.ValidationError as exc:
                path = "/".join(str(part) for part in exc.absolute_path) or "(root)"
                raise MCPToolError(
                    f"mcp tool {tool!r} arguments failed schema: {exc.validator!r} at {path}"
                ) from exc
            except jsonschema.SchemaError as exc:
                raise MCPToolError(f"mcp {server_id!r} tool {tool!r} schema is invalid") from exc
            try:
                result = await asyncio.wait_for(
                    session.call_tool(
                        tool,
                        arguments=args,
                        read_timeout_seconds=timedelta(seconds=config.timeout_seconds),
                    ),
                    timeout=config.timeout_seconds,
                )
            except TimeoutError as exc:
                await self._drop_locked(server_id)
                raise MCPTransportError(
                    f"mcp {server_id!r} tool {tool!r} timed out after {config.timeout_seconds:g}s"
                ) from exc
            except asyncio.CancelledError:
                await self._drop_locked(server_id, shield=True)
                raise
            except Exception as exc:
                await self._drop_locked(server_id)
                raise MCPTransportError(
                    f"mcp {server_id!r} tool {tool!r} failed via {config.sanitized_endpoint()}: "
                    f"{type(exc).__name__} {_scrub(str(exc), [])}"
                ) from exc
            if getattr(result, "isError", False):
                raise MCPToolError(
                    f"mcp {server_id!r} tool {tool!r} reported error: {self._text_of(result)[:500]}"
                )
            payload = {
                "origin": "mcp",
                "server": server_id,
                "tool": tool,
                "untrusted": True,
                "isError": False,
                "content": [self._block(block) for block in (result.content or [])],
                "structured": result.structuredContent,
            }
            if len(json.dumps(payload, default=str)) > config.max_result_chars:
                raise MCPToolError(
                    f"mcp {server_id!r} tool {tool!r} result exceeds "
                    f"{config.max_result_chars} chars; refusing to truncate a tool result"
                )
            return json.loads(json.dumps(payload, default=str))

    async def close(self) -> None:
        for server_id in list(self._stacks):
            await self._drop_locked(server_id)
        self._closed = True

    def _require_enabled(self, server_id: str) -> MCPConfig:
        if self._closed:
            raise MCPTransportError("mcp host is closed")
        config = self._configs.get(server_id)
        if config is None:
            raise MCPError(f"unknown mcp server {server_id!r}")
        if not config.enabled:
            raise PolicyDenied(f"mcp server {server_id!r} is disabled")
        return config

    async def _with_reconnect(self, config: MCPConfig, *, read_only: bool = False) -> Any:
        """Read-only ops may reconnect once. Calls never auto-replay side effects."""
        last: Exception | None = None
        for _ in range(2 if read_only else 1):
            try:
                session = await self._ensure_session_locked(config, [])
                return await asyncio.wait_for(session.list_tools(), timeout=config.timeout_seconds)
            except (MCPTransportError, TimeoutError) as exc:
                last = exc
                await self._drop_locked(config.id)
        assert last is not None
        if isinstance(last, TimeoutError):
            raise MCPTransportError(f"mcp {config.id!r} listing timed out") from last
        raise last

    async def _refresh_schemas_locked(self, config: MCPConfig, session: ClientSession) -> None:
        try:
            result = await asyncio.wait_for(session.list_tools(), timeout=config.timeout_seconds)
        except TimeoutError as exc:
            await self._drop_locked(config.id)
            raise MCPTransportError(f"mcp {config.id!r} listing timed out") from exc
        except asyncio.CancelledError:
            await self._drop_locked(config.id, shield=True)
            raise
        except Exception as exc:
            await self._drop_locked(config.id)
            raise MCPTransportError(
                f"mcp {config.id!r} listing failed: {type(exc).__name__}"
            ) from exc
        schemas = {}
        for tool in result.tools or []:
            if isinstance(getattr(tool, "name", None), str) and isinstance(
                getattr(tool, "inputSchema", None), dict
            ):
                schemas[tool.name] = tool.inputSchema
        self._schemas[config.id] = schemas

    async def _ensure_session_locked(self, config: MCPConfig, secrets: list[str]) -> ClientSession:
        existing = self._sessions.get(config.id)
        if existing is not None:
            return existing
        resolved: dict[str, str] = {}
        if config.env_secret_refs or config.header_secret_refs:
            if self._resolver is None:
                raise MCPError(f"mcp {config.id!r} needs secret refs but no resolver is configured")
            for name, ref in {**config.env_secret_refs, **config.header_secret_refs}.items():
                value = self._resolver(ref)
                if not isinstance(value, str) or not value:
                    raise MCPTransportError(f"mcp {config.id!r} secret ref {ref!r} did not resolve")
                resolved[name] = value
        stack = AsyncExitStack()
        try:
            if config.transport == "stdio":
                read, write = await self._connect_stdio(config, resolved, stack)
            else:
                read, write = await self._connect_http(config, resolved, stack)
            session = await stack.enter_async_context(ClientSession(read, write))
            try:
                await asyncio.wait_for(session.initialize(), timeout=config.timeout_seconds)
            except TimeoutError as exc:
                raise MCPTransportError(f"mcp {config.id!r} handshake timed out") from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MCPTransportError(
                    f"mcp {config.id!r} handshake failed via {config.sanitized_endpoint()}: "
                    f"{type(exc).__name__} {_scrub(str(exc), list(resolved.values()))}"
                ) from exc
            self._stacks[config.id] = stack
            self._sessions[config.id] = session
            return session
        except asyncio.CancelledError:
            await stack.aclose()
            raise
        except (PolicyDenied, MCPError):
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            raise MCPTransportError(
                f"mcp {config.id!r} connect failed via {config.sanitized_endpoint()}: "
                f"{type(exc).__name__} {_scrub(str(exc), list(resolved.values()))}"
            ) from exc

    async def _connect_stdio(
        self, config: MCPConfig, resolved: dict[str, str], stack: AsyncExitStack
    ) -> tuple[Any, Any]:
        assert config.command is not None
        if not self._policy.effective_shell:
            raise PolicyDenied(f"mcp stdio server {config.id!r} requires the shell grant")
        joined = shlex.join([config.command, *config.args])
        argv, _isolated = build_bwrap_args(self._policy, joined, self._policy.network)
        if len(argv) < 4 or argv[-3:-1] != ["/bin/bash", "-c"]:
            raise MCPTransportError("unexpected bubblewrap argv shape; refusing to launch")
        secret_env = {name: resolved[name] for name in config.env_secret_refs if name in resolved}
        launch = argv[:-3]
        for name, value in secret_env.items():
            launch += ["--setenv", name, value]
        launch += argv[-3:]
        params = StdioServerParameters(
            command=launch[0], args=launch[1:], env=dict(secret_env) if secret_env else None
        )
        return await stack.enter_async_context(stdio_client(params))

    async def _connect_http(
        self, config: MCPConfig, resolved: dict[str, str], stack: AsyncExitStack
    ) -> tuple[Any, Any]:
        assert config.url is not None
        if not self._policy.network:
            raise PolicyDenied(f"mcp http server {config.id!r} requires the network grant")
        if not config.allow_remote:
            raise PolicyDenied(f"mcp http server {config.id!r} requires explicit allow_remote")
        parts = urlsplit(config.url)
        host = parts.hostname or ""
        if parts.scheme != "https" and not _is_loopback(host):
            raise PolicyDenied(f"mcp http server {config.id!r} needs https outside loopback")
        headers = {name: resolved[name] for name in config.header_secret_refs if name in resolved}
        client = await stack.enter_async_context(
            streamable_http_client(
                config.url, headers=headers or None, timeout=config.timeout_seconds
            )
        )
        return client[0], client[1]

    async def _drop_locked(self, server_id: str, *, shield: bool = False) -> None:
        self._sessions.pop(server_id, None)
        stack = self._stacks.pop(server_id, None)
        self._schemas.pop(server_id, None)
        if stack is not None:
            if shield:
                with suppress(Exception):
                    await asyncio.shield(stack.aclose())
            else:
                with suppress(Exception):
                    await stack.aclose()

    @staticmethod
    def _block(block: Any) -> Any:
        if hasattr(block, "model_dump"):
            return block.model_dump(mode="json", by_alias=True, exclude_none=True)
        return {"type": "unknown", "text": str(block)[:2000]}

    @staticmethod
    def _text_of(result: Any) -> str:
        texts = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                texts.append(text)
        return "\n".join(texts)
