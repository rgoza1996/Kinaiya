"""Real local API/CLI/MCP transport probe with no Converse or model inference.

Use a new owned data directory. --keep-serving leaves the two owner services
available for browser verification until interrupted. All peers are synthetic
and bind loopback; this is not external-server or OAuth conformance evidence.
"""

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import uvicorn

from core.policy import ToolPolicy
from packages.contracts.models import ProviderInfo
from services.agent import AgentRuntime, create_app
from services.common.auth import Auth, load_or_create_tokens
from services.session.api import create_app as session_app
from services.session.store import LocalJournal, Store


class NoInferenceFixture:
    info = ProviderInfo(
        provider="synthetic-fixture", model="management-without-inference", local=True
    )
    calls = 0

    async def complete(self, messages):
        self.calls += 1
        raise RuntimeError("Management verification must never invoke inference")


async def run(args):
    from packages.ecosystem.mcp_control import MCPController

    root = args.data_dir.absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    repository = Path(__file__).resolve().parents[1]
    peer = repository / "evaluation/fixtures/mcp_management_server.py"
    shutil.copyfile(peer, workspace / "mcp_probe_server.py")
    tokens = load_or_create_tokens(root)
    login = root / "owner-login.txt"
    fd = os.open(login, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(tokens["owner"])
    configuration = {
        "session_url": f"http://127.0.0.1:{args.session_port}",
        "agent_url": f"http://127.0.0.1:{args.agent_port}",
    }
    (root / "config.json").write_text(json.dumps(configuration))
    auth = Auth(tokens, allowed_origins=("http://127.0.0.1:8783",))
    store = Store(root / "journal.sqlite")
    policy = ToolPolicy(workspace, allow_write=True, allow_shell=True, network=True)
    controller = MCPController(root / "mcp", workspace, policy, initial_configs=[])
    provider = NoInferenceFixture()
    runtime = AgentRuntime(
        LocalJournal(store, "agent"), provider, workspace, policy, mcp_controller=controller
    )
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                session_app(store, auth),
                host="127.0.0.1",
                port=args.session_port,
                log_level="warning",
            )
        ),
        uvicorn.Server(
            uvicorn.Config(
                create_app(runtime, auth),
                host="127.0.0.1",
                port=args.agent_port,
                log_level="warning",
            )
        ),
    ]
    jobs = [asyncio.create_task(server.serve()) for server in servers]
    http_log = (root / "http-peer.log").open("w")
    child = subprocess.Popen(
        [
            sys.executable,
            str(peer),
            "--port",
            str(args.peer_port),
            "--calls-log",
            str(root / "http-methods.jsonl"),
        ],
        stdout=http_log,
        stderr=subprocess.STDOUT,
    )
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + tokens["owner"]}, timeout=40
        ) as client:
            agent = configuration["agent_url"]
            for _ in range(100):
                try:
                    response = await client.get(agent + "/health")
                    if response.status_code == 200 and all(server.started for server in servers):
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Probe services did not start")
            for _ in range(100):
                try:
                    await client.get(f"http://127.0.0.1:{args.peer_port}/mcp")
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)
            records = []

            async def control(operation, *, server_id=None, config=None, reviewed=False):
                snapshot = (await client.get(agent + "/v1/mcp/management")).json()
                body = {
                    "operation": operation,
                    "idempotency_key": f"probe-{len(records)}",
                    "expected_revision": snapshot["revision"],
                    "reviewed": reviewed,
                }
                if server_id is not None:
                    body["server_id"] = server_id
                if config is not None:
                    body["config"] = config
                response = await client.post(agent + "/v1/mcp/control", json=body)
                response.raise_for_status()
                result = response.json()
                replay = await client.post(agent + "/v1/mcp/control", json=body)
                assert replay.status_code == 200 and replay.json() == result
                records.append({"operation": operation, "result": result, "replay_equal": True})
                return result

            stdio = {
                "id": "probe-stdio",
                "transport": "stdio",
                "command": "/usr/bin/python3",
                "args": [
                    "-u",
                    "/workspace/mcp_probe_server.py",
                    "--calls-log",
                    "/workspace/stdio-methods.jsonl",
                ],
                "enabled": False,
                "allowed_tools": ["echo"],
                "content_scope": "public_synthetic",
            }
            remote = {
                "id": "probe-http",
                "transport": "http",
                "allow_remote": True,
                "url": f"http://127.0.0.1:{args.peer_port}/mcp",
                "enabled": False,
                "allowed_tools": ["echo"],
                "content_scope": "public_synthetic",
            }
            for definition in (stdio, remote):
                identity = definition["id"]
                await control("configure", config=definition, reviewed=True)
                assert (
                    not (workspace / "stdio-methods.jsonl").exists()
                    if identity == "probe-stdio"
                    else True
                )
                tested = await control("test", server_id=identity)
                assert tested["status"] == "connected"
                await control("enable", server_id=identity, reviewed=True)
                assert (await control("reconnect", server_id=identity))["status"] == "connected"
                await control("disable", server_id=identity)
                await control("remove", server_id=identity)
            cli_args = [
                sys.executable,
                "-m",
                "services.launcher",
                "--data-dir",
                str(root),
                "agent",
                "/mcp list",
            ]
            cli = await asyncio.create_subprocess_exec(
                *cli_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repository,
                env={**os.environ, "PYTHONPATH": str(repository)},
            )
            stdout, stderr = await cli.communicate()
            assert cli.returncode == 0, stderr.decode()[-1000:]
            cli_result = json.loads(stdout)
            assert cli_result["status"] == "ok" and provider.calls == 0
            methods = []
            for path in (workspace / "stdio-methods.jsonl", root / "http-methods.jsonl"):
                methods.extend(json.loads(line)["method"] for line in path.read_text().splitlines())
            assert (
                "initialize" in methods and "tools/list" in methods and "tools/call" not in methods
            )
            # Leave a disabled HTTP definition for optional browser setup/test/enable/remove.
            await control("configure", config={**remote, "id": "browser-http"}, reviewed=True)
            report = {
                "synthetic_only": True,
                "converse_constructed": False,
                "model_calls": provider.calls,
                "owner_api": records,
                "actual_cli": cli_result,
                "peer_methods": methods,
                "repository_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repository, text=True
                ).strip(),
                "working_tree_clean": not bool(
                    subprocess.check_output(
                        ["git", "status", "--porcelain"], cwd=repository, text=True
                    ).strip()
                ),
            }
            (root / "evidence.json").write_text(json.dumps(report, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "evidence": str(root / "evidence.json"),
                        "owner_login_file": str(login),
                        "model_calls": provider.calls,
                        "operations": len(records),
                        **configuration,
                    }
                ),
                flush=True,
            )
            if args.keep_serving:
                # The owning verifier creates this file after browser QA.
                while not (root / "stop").exists() and all(not job.done() for job in jobs):
                    await asyncio.sleep(0.5)
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*jobs, return_exceptions=True)
        await runtime.close()
        store.close()
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        http_log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--session-port", type=int, default=8870)
    parser.add_argument("--agent-port", type=int, default=8871)
    parser.add_argument("--peer-port", type=int, default=8876)
    parser.add_argument("--keep-serving", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
