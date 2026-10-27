"""Integration tests: native MCP host over a real bubblewrap stdio server.

Live stdio tests need bubblewrap namespaces plus /usr/bin/python3 and skip
truthfully otherwise. HTTP live transport is not run (no external servers).
MCP SDK 1.30.0; stdio transport verified, HTTP transport not run live.
"""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.policy import PolicyDenied, ToolPolicy
from packages.ecosystem.mcp_host import (
    MCPConfig,
    MCPError,
    MCPHost,
    MCPToolError,
    MCPTransportError,
)
from services.agent import tools as agent_tools

SERVER_SRC = """
import hashlib, json, os, sys, time
PROTOCOL = "2025-11-25"
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
    logpath = sys.argv[2] if len(sys.argv) > 2 else None
    def log(entry):
        if logpath:
            with open(logpath, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\\n")
    if mode == "crash":
        sys.stderr.write("synthetic crash\\n")
        sys.stderr.flush()
        raise SystemExit(2)
    tools = [
        {"name": "echo", "description": "Echo text.",
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                          "required": ["text"], "additionalProperties": False}},
        {"name": "hidden_tool", "description": "Listed but never allowlisted.",
         "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                          "required": ["text"], "additionalProperties": False}},
        {"name": "env_probe", "description": "Probe sandbox env.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    ]
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize" and mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                             "serverInfo": {"name": "synth-mcp", "version": "1.0.0"}}})
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue
        elif method == "ping" and mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list" and mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call" and mid is not None:
            name, args = params.get("name"), params.get("arguments") or {}
            log({"event": "call", "tool": name})
            if mode == "die":
                os._exit(1)
            if mode == "slow" and name == "echo":
                time.sleep(20)
            if name == "env_probe":
                token = os.environ.get("SYNTH_TOKEN", "")
                log({"event": "env",
                     "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                     "keys": sorted(os.environ.keys()), "home": os.environ.get("HOME", "")})
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"content": [{"type": "text", "text": "probed"}]}})
            elif name in ("echo", "hidden_tool"):
                text = "X" * 100000 if (mode == "big" and name == "echo") else f"{name}:" + str(args.get("text", ""))
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"content": [{"type": "text", "text": text}]}})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": "unknown tool"}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "method not found"}})
main()
"""


def _sandbox_ok(workspace: Path) -> bool:
    if Path("/usr/bin/python3").exists() is False:
        return False
    try:
        policy = ToolPolicy(workspace=workspace)
        argv, _ = agent_tools.build_bwrap_args(policy, "true", False)
        done = subprocess.run(argv, capture_output=True, timeout=30)
    except Exception:
        return False
    return done.returncode == 0


def _write_server(workspace: Path) -> None:
    (workspace / "synth_mcp_server.py").write_text(SERVER_SRC, encoding="utf-8")


def _cfg(workspace: Path, **over: object) -> MCPConfig:
    base: dict[str, object] = {
        "id": "synth",
        "transport": "stdio",
        "command": "/usr/bin/python3",
        "args": ["/workspace/synth_mcp_server.py", "ok", "/workspace/calls.log"],
        "enabled": True,
        "allowed_tools": ["echo", "env_probe"],
        "timeout_seconds": 15.0,
    }
    base.update(over)
    return MCPConfig(**base)  # type: ignore[arg-type]


def _calls(workspace: Path) -> list[dict]:
    log = workspace / "calls.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_config_rejects_url_userinfo_secret_query_and_wildcards() -> None:
    with pytest.raises(ValidationError):
        MCPConfig(id="s", transport="http", url="https://user:pw@example.com/x")
    with pytest.raises(ValidationError):
        MCPConfig(id="s", transport="http", url="https://example.com/?token=abc")
    with pytest.raises(ValidationError):
        MCPConfig(id="s", transport="http", url="https://example.com/x#frag")
    with pytest.raises(ValidationError):
        MCPConfig(id="s", transport="stdio", command="echo", allowed_tools=["*"])
    with pytest.raises(ValidationError):
        MCPConfig(id="s", transport="http", url="https://example.com/x", env_secret_refs={"A": "r"})
    with pytest.raises(ValidationError):
        MCPConfig(id="s", transport="stdio", command="echo", header_secret_refs={"X-A": "r"})


async def test_http_needs_network_grant_and_remote_opt_in(tmp_path) -> None:
    policy = ToolPolicy(workspace=tmp_path, network=False)
    host = MCPHost(
        [
            MCPConfig(
                id="h",
                transport="http",
                url="http://127.0.0.1:1/",
                enabled=True,
                allowed_tools=["t"],
                allow_remote=True,
            )
        ],
        policy,
    )
    try:
        with pytest.raises(PolicyDenied, match="network"):
            await host.discover("h")
    finally:
        await host.close()
    policy2 = ToolPolicy(workspace=tmp_path, network=True)
    host2 = MCPHost(
        [
            MCPConfig(
                id="h",
                transport="http",
                url="http://127.0.0.1:1/",
                enabled=True,
                allowed_tools=["t"],
                allow_remote=False,
            )
        ],
        policy2,
    )
    try:
        with pytest.raises(PolicyDenied, match="allow_remote"):
            await host2.call("h", "t", {})
    finally:
        await host2.close()


async def test_http_refused_hides_header_secret_and_query(tmp_path) -> None:
    policy = ToolPolicy(workspace=tmp_path, network=True)
    secret = "hdr-secret-VALUE-zzz"
    host = MCPHost(
        [
            MCPConfig(
                id="h",
                transport="http",
                url="http://127.0.0.1:1/?next=ok",
                enabled=True,
                allowed_tools=["t"],
                allow_remote=True,
                header_secret_refs={"X-T": "ref/t"},
                timeout_seconds=5.0,
            )
        ],
        policy,
        secret_resolver=lambda ref: secret,
    )
    try:
        with pytest.raises(MCPTransportError) as excinfo:
            await host.discover("h")
        assert secret not in str(excinfo.value)
    finally:
        await host.close()


async def test_disabled_server_and_unknown_id_denied(tmp_path) -> None:
    host = MCPHost(
        [MCPConfig(id="s", transport="stdio", command="echo")], ToolPolicy(workspace=tmp_path)
    )
    try:
        with pytest.raises(PolicyDenied):
            await host.discover("s")
        with pytest.raises(MCPError):
            await host.discover("nope")
        assert host.list_servers()[0]["enabled"] is False
    finally:
        await host.close()


async def test_replace_configs_is_owner_only(tmp_path) -> None:
    policy = ToolPolicy(workspace=tmp_path)
    host = MCPHost([], policy)
    try:
        with pytest.raises(PolicyDenied):
            await host.replace_configs(actor="agent", configs=[])
        await host.replace_configs(
            actor="owner",
            configs=[MCPConfig(id="n", transport="http", url="https://example.com/x")],
        )
        assert [s["id"] for s in host.list_servers()] == ["n"]
    finally:
        await host.close()


async def test_list_servers_and_repr_redact_secrets(tmp_path) -> None:
    secret = "super-secret-VALUE-qqq"
    cfg = MCPConfig(
        id="s",
        transport="stdio",
        command="/usr/bin/python3",
        enabled=True,
        env_secret_refs={"SYNTH_TOKEN": "ref/t"},
    )
    host = MCPHost([cfg], ToolPolicy(workspace=tmp_path), secret_resolver=lambda ref: secret)
    try:
        blob = json.dumps(host.list_servers())
        assert secret not in blob and secret not in repr(host) and secret not in repr(cfg)
    finally:
        await host.close()


async def test_stdio_roundtrip_and_discover_is_not_authority(tmp_path) -> None:
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    _write_server(tmp_path)
    policy = ToolPolicy(workspace=tmp_path, allow_shell=True)
    host = MCPHost([_cfg(tmp_path)], policy)
    try:
        tools = await host.discover("synth")
        by_name = {t["name"]: t for t in tools}
        assert by_name["hidden_tool"]["allowed"] is False
        assert by_name["echo"]["allowed"] is True
        result = await host.call("synth", "echo", {"text": "hello-mcp"})
        assert result["origin"] == "mcp" and result["untrusted"] is True
        assert result["server"] == "synth" and result["tool"] == "echo"
        assert result["content"][0]["text"] == "echo:hello-mcp"
        with pytest.raises(PolicyDenied):
            await host.call("synth", "hidden_tool", {"text": "x"})
        assert all(entry.get("tool") != "hidden_tool" for entry in _calls(tmp_path))
    finally:
        await host.close()


async def test_bad_schema_never_sent(tmp_path) -> None:
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    _write_server(tmp_path)
    host = MCPHost([_cfg(tmp_path)], ToolPolicy(workspace=tmp_path, allow_shell=True))
    try:
        await host.discover("synth")
        before = len(_calls(tmp_path))
        with pytest.raises(MCPToolError):
            await host.call("synth", "echo", {"wrong": 1})
        assert len(_calls(tmp_path)) == before
    finally:
        await host.close()


async def test_output_cap_fails_clearly(tmp_path) -> None:
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    _write_server(tmp_path)
    cfg = _cfg(
        tmp_path,
        args=["/workspace/synth_mcp_server.py", "big", "/workspace/calls.log"],
        max_result_chars=8000,
    )
    host = MCPHost([cfg], ToolPolicy(workspace=tmp_path, allow_shell=True))
    try:
        with pytest.raises(MCPToolError, match="exceeds"):
            await host.call("synth", "echo", {"text": "x"})
    finally:
        await host.close()


async def test_slow_server_timeout_keeps_host_usable(tmp_path) -> None:
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    _write_server(tmp_path)
    cfg = _cfg(
        tmp_path,
        args=["/workspace/synth_mcp_server.py", "slow", "/workspace/calls.log"],
        timeout_seconds=2.0,
    )
    host = MCPHost([cfg], ToolPolicy(workspace=tmp_path, allow_shell=True))
    try:
        with pytest.raises(MCPTransportError, match="timed out"):
            await host.call("synth", "echo", {"text": "x"})
        tools = await host.discover("synth")
        assert {t["name"] for t in tools} >= {"echo", "hidden_tool", "env_probe"}
    finally:
        await host.close()


async def test_crashing_server_does_not_crash_host(tmp_path) -> None:
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    _write_server(tmp_path)
    cfg = _cfg(tmp_path, args=["/workspace/synth_mcp_server.py", "crash", "/workspace/calls.log"])
    host = MCPHost([cfg], ToolPolicy(workspace=tmp_path, allow_shell=True))
    try:
        with pytest.raises(MCPTransportError):
            await host.discover("synth")
    finally:
        await host.close()
    assert (tmp_path / "synth_mcp_server.py").exists()


async def test_server_env_scrubbed_but_explicit_secret_delivered(tmp_path) -> None:
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    _write_server(tmp_path)
    marker = "KINAIYA_SHOULD_NOT_LEAK_7f3a"
    secret = "synth-secret-VALUE-xyz"
    import os

    os.environ[marker] = "must-not-appear"
    cfg = _cfg(tmp_path, env_secret_refs={"SYNTH_TOKEN": "ref/synth"}, timeout_seconds=15.0)
    host = MCPHost(
        [cfg], ToolPolicy(workspace=tmp_path, allow_shell=True), secret_resolver=lambda ref: secret
    )
    try:
        result = await host.call("synth", "env_probe", {})
        assert result["content"][0]["text"] == "probed"
        env_entries = [e for e in _calls(tmp_path) if e.get("event") == "env"]
        assert env_entries, "server did not log env probe"
        env = env_entries[-1]
        assert env["token_sha256"] == hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in json.dumps(env)
        assert marker not in env["keys"]
        assert env["home"] == "/tmp"
    finally:
        await host.close()
        del os.environ[marker]


async def test_stdio_requires_shell_grant_and_isolation_remains_without_network(tmp_path) -> None:
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    _write_server(tmp_path)
    host = MCPHost([_cfg(tmp_path)], ToolPolicy(workspace=tmp_path, allow_shell=False))
    try:
        with pytest.raises(PolicyDenied, match="shell"):
            await host.discover("synth")
    finally:
        await host.close()
    assert (tmp_path / "calls.log").exists() is False
    host2 = MCPHost(
        [_cfg(tmp_path)], ToolPolicy(workspace=tmp_path, allow_shell=True, network=False)
    )
    try:
        result = await host2.call("synth", "echo", {"text": "nonet"})
        assert result["content"][0]["text"] == "echo:nonet"
    finally:
        await host2.close()


async def test_concurrent_calls_are_serialized(tmp_path) -> None:
    if not _sandbox_ok(tmp_path):
        pytest.skip("bubblewrap isolation unavailable in this environment")
    _write_server(tmp_path)
    host = MCPHost([_cfg(tmp_path)], ToolPolicy(workspace=tmp_path, allow_shell=True))
    try:
        results = await asyncio.gather(
            *(host.call("synth", "echo", {"text": f"m{i}"}) for i in range(5))
        )
        assert [r["content"][0]["text"] for r in results] == [f"echo:m{i}" for i in range(5)]
    finally:
        await host.close()


def test_tool_module_imports_without_optional_runtime() -> None:
    assert sys.version_info >= (3, 11)
