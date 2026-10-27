"""Independent service launcher and direct Agent CLI. Optional imports stay lazy."""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from packages.contracts.models import TaskRequest, new_id
from services.common.auth import Auth, load_or_create_tokens

ROOT = Path(__file__).resolve().parents[1]
PORTS = {"session": 8760, "agent": 8761, "converse": 8762, "decisions": 8763}


def load_config(path):
    if not path.exists():
        raise SystemExit(f"Missing configuration: {path}. Run kinaiya init first.")
    return json.loads(path.read_text())


def provider(config, role):
    from packages.providers.text import CompatibleTextProvider, MetaResponsesProvider

    profile = dict(config["providers"][role])
    kind = profile.pop("kind")
    if kind == "meta":
        return MetaResponsesProvider(**profile)
    if kind == "compatible":
        key_ref = profile.pop("api_key_env", None)
        if key_ref:
            profile["api_key"] = os.environ.get(key_ref, "")
        return CompatibleTextProvider(**profile)
    raise SystemExit(
        f"Unsupported {role} provider kind {kind}; configure a live provider explicitly"
    )


def initialize(args):
    directory = args.data_dir.resolve()
    load_or_create_tokens(directory)
    config_path = args.config or directory / "config.json"
    if not config_path.exists():
        profile = {
            "kind": "compatible",
            "base_url": "http://127.0.0.1:8080/v1",
            "model": "configure-your-local-model",
            "allow_cloud": False,
            "max_tokens": 2048,
        }
        config_path.write_text(
            json.dumps(
                {
                    "providers": {key: profile for key in ("agent", "converse", "decisions")},
                    "session_url": "http://127.0.0.1:8760",
                    "agent_url": "http://127.0.0.1:8761",
                    "voice": {"url": "http://127.0.0.1:8765", "profile": "Kokoro 82M"},
                    "origins": ["http://localhost:8780", "http://127.0.0.1:8780"],
                    "policy": {
                        "allow_write": False,
                        "allow_shell": False,
                        "network": False,
                        "yolo": False,
                    },
                    "decisions_default_target": "laya-needs-setup",
                    "agent_enabled": True,
                },
                indent=2,
            )
            + "\n"
        )
    print(
        json.dumps(
            {
                "config": str(config_path),
                "credentials": str(directory / "service-tokens.json"),
                "next": "Configure providers, then kinaiya serve all; keys stay outside chat.",
            }
        )
    )


def serve(args):
    import uvicorn

    from packages.contracts.client import JournalClient

    config_path = args.config or args.data_dir / "config.json"
    config = load_config(config_path)
    tokens = load_or_create_tokens(args.data_dir)
    auth = Auth(tokens, tuple(config.get("origins", [])))
    service = args.service
    if service == "all":
        children = []
        try:
            for name in ("session", "agent", "converse", "decisions"):
                children.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "services.launcher",
                            "--data-dir",
                            str(args.data_dir.resolve()),
                            "--config",
                            str(config_path.resolve()),
                            "--workspace",
                            str(args.workspace.resolve()),
                            "serve",
                            name,
                        ]
                    )
                )
            print(
                "Independent session/Agent/Converse/Decisions processes started; Ctrl-C stops all.",
                flush=True,
            )
            while all(child.poll() is None for child in children):
                for child in children:
                    try:
                        child.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
        except KeyboardInterrupt:
            pass
        finally:
            for child in children:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
            for child in children:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
        return
    if service == "session":
        from services.session.api import create_app
        from services.session.store import Store

        app = create_app(Store(args.data_dir / "journal.sqlite"), auth)
    elif service == "agent":
        from core.policy import ToolPolicy

        from services.agent import AgentRuntime, create_app

        args.workspace.mkdir(parents=True, exist_ok=True)
        journal = JournalClient(config["session_url"], tokens["agent"])
        policy = ToolPolicy(
            args.workspace,
            **config.get("policy", {}),
            protected_roots=(
                ROOT / "core",
                ROOT / "services/agent",
                ROOT / "services/session",
                ROOT / "packages/contracts",
                ROOT / "evaluation",
                args.data_dir.resolve(),
            ),
        )
        runtime = AgentRuntime(journal, provider(config, "agent"), args.workspace, policy)

        @asynccontextmanager
        async def agent_lifespan(app):
            # Bounded retries handle supervisor startup order; no work is executed by recovery.
            for attempt in range(30):
                try:
                    await runtime.recover()
                    break
                except httpx.ConnectError:
                    if attempt == 29:
                        raise
                    await asyncio.sleep(0.1)
            yield
            await runtime.close()
            await journal.close()

        app = create_app(runtime, auth)
        app.router.lifespan_context = agent_lifespan
    elif service == "converse":
        from packages.providers.speech import SpeechClient
        from services.converse.api import create_app
        from services.converse.runtime import ConverseRuntime
        from services.decisions.runtime import OutcomeWorker

        journal = JournalClient(config["session_url"], tokens["converse"])
        runtime = ConverseRuntime(journal, provider(config, "converse"))
        voice = config.get("voice")
        speech = (
            SpeechClient(voice["url"], tokens["converse"], profile=voice["profile"])
            if voice
            else None
        )
        app = create_app(runtime, auth, speech)

        @asynccontextmanager
        async def converse_lifespan(app):
            async def observe():
                worker = OutcomeWorker(journal, runtime)
                while True:
                    try:
                        await worker.run()
                    except httpx.HTTPError:
                        await asyncio.sleep(1)

            observer = asyncio.create_task(observe())
            yield
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            await journal.close()
            if speech:
                await speech.close()

        app.router.lifespan_context = converse_lifespan
    else:
        from packages.agent_adapters.http import AgentClient
        from services.decisions.api import create_app
        from services.decisions.runtime import DecisionsWorker, ModelClassifier

        worker = DecisionsWorker(
            JournalClient(config["session_url"], tokens["decisions"]),
            AgentClient(config["agent_url"], tokens["decisions"]),
            ModelClassifier(provider(config, "decisions")),
            enabled=config.get("agent_enabled", True),
        )
        app = create_app(worker, auth)
    uvicorn.run(app, host="127.0.0.1", port=args.port or PORTS[service], log_level="warning")


async def direct_agent(args):
    config = load_config(args.config or args.data_dir / "config.json")
    tokens = load_or_create_tokens(args.data_dir)
    headers = {"Authorization": f"Bearer {tokens['owner']}"}
    async with httpx.AsyncClient(headers=headers, timeout=30) as http:
        response = await http.post(
            config["session_url"] + "/v1/sessions", json={"channel": "agent"}
        )
        response.raise_for_status()
        session = response.json()
        request = TaskRequest(
            session_id=session["session_id"],
            thread_id=session["thread_id"],
            epoch=session["epoch"],
            objective=args.objective,
            idempotency_key=new_id(),
        )
        response = await http.post(
            config["agent_url"] + "/v1/agent/tasks", json=request.model_dump()
        )
        response.raise_for_status()
        task = response.json()
        print(json.dumps({"accepted_task_id": task["task_id"]}), flush=True)
        while task["state"] not in {
            "succeeded",
            "failed",
            "cancelled",
            "blocked",
            "awaiting_approval",
            "paused",
        }:
            await asyncio.sleep(0.1)
            response = await http.get(config["agent_url"] + "/v1/agent/tasks/" + task["task_id"])
            response.raise_for_status()
            task = response.json()
        print(json.dumps(task, indent=2))
        return 0 if task["state"] == "succeeded" else 1


def main():
    parser = argparse.ArgumentParser(description="Kinaiya independent runtimes and Agent CLI")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    server = commands.add_parser("serve")
    server.add_argument("service", choices=[*PORTS, "all"])
    server.add_argument("--port", type=int)
    agent = commands.add_parser("agent", help="Submit real work directly, without Converse")
    agent.add_argument("objective")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args)
    elif args.command == "serve":
        serve(args)
    else:
        raise SystemExit(asyncio.run(direct_agent(args)))


if __name__ == "__main__":
    main()
