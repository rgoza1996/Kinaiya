"""Independent service launcher and direct Agent CLI. Optional imports stay lazy."""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from packages.contracts.models import TaskRequest, new_id
from services.common.auth import Auth, load_or_create_tokens

ROOT = Path(__file__).resolve().parents[1]
PORTS = {"session": 8760, "agent": 8761, "converse": 8762, "decisions": 8763, "resources": 8764}


def supervise(commands, *, popen=subprocess.Popen, sleep=time.sleep):
    """Failed/unloaded components do not stop their healthy peers."""
    children = {name: [popen(command), command, 0] for name, command in commands.items()}
    try:
        while children:
            for name, (child, command, attempts) in list(children.items()):
                code = child.poll()
                if code is None:
                    continue
                if code != 0 and attempts < 3:
                    sleep(0.1 * (attempts + 1))
                    children[name] = [popen(command), command, attempts + 1]
                    print(
                        json.dumps(
                            {"service": name, "event": "restarted", "attempt": attempts + 1}
                        ),
                        flush=True,
                    )
                else:
                    children.pop(name)
                    print(
                        json.dumps({"service": name, "event": "unavailable", "exit_code": code}),
                        flush=True,
                    )
            sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        for child, _, _ in children.values():
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
        for child, _, _ in children.values():
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


def load_config(path):
    if not path.exists():
        raise SystemExit(f"Missing configuration: {path}. Run kinaiya init first.")
    return json.loads(path.read_text())


def provider(config, role, *, resource_state=None):
    from packages.providers.text import CompatibleTextProvider, MetaResponsesProvider

    profile = dict(config["providers"][role])
    kind = profile.pop("kind")
    if kind == "meta":
        if role == "agent":
            profile.setdefault("structured_actions", True)
        result = MetaResponsesProvider(**profile)
    elif kind == "compatible":
        key_ref = profile.pop("api_key_env", None)
        if key_ref:
            profile["api_key"] = os.environ.get(key_ref, "")
        result = CompatibleTextProvider(**profile)
    else:
        raise SystemExit(
            f"Unsupported {role} provider kind {kind}; configure a live provider explicitly"
        )
    if resource_state is not None:
        from services.resources.providers import ResourceBoundProvider

        result = ResourceBoundProvider(result, resource_state, role)
    return result


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
    resources_enabled = (
        isinstance(config.get("resources"), dict) and config["resources"].get("enabled") is True
    )
    resource_state = args.data_dir.resolve() / "resources-state.json" if resources_enabled else None
    if service == "all":
        commands = {
            name: [
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
            for name in PORTS
            if name != "resources" or resources_enabled
        }
        print(
            "Independent processes started; failed components restart at most three times.",
            flush=True,
        )
        supervise(commands)
        return
    if service == "resources":
        from services.resources.launcher import resource_app

        app = resource_app(args, config, auth, tokens, ROOT)
    elif service == "session":
        from services.session.api import create_app
        from services.session.store import Store

        app = create_app(Store(args.data_dir / "journal.sqlite"), auth)
    elif service == "agent":
        from core.policy import ToolPolicy
        from packages.commands import Registry
        from services.agent import AgentRuntime, create_app

        args.workspace.mkdir(parents=True, exist_ok=True)
        journal = JournalClient(config["session_url"], tokens["agent"])
        protected = (
            ROOT / "core",
            ROOT / "services/agent",
            ROOT / "services/session",
            ROOT / "packages/contracts",
            ROOT / "packages/resources",
            ROOT / "services/resources",
            ROOT / "packages/providers",
            ROOT / "evaluation",
            ROOT / "Roadmap.md",
            args.data_dir.resolve(),
            Path.home() / ".config/muse",
        )
        workspace = args.workspace.resolve()
        if workspace in {Path("/"), Path("/tmp"), Path("/run"), Path("/proc"), Path.home()} or any(
            workspace == root.resolve()
            or workspace.is_relative_to(root.resolve())
            or root.resolve().is_relative_to(workspace)
            for root in protected
        ):
            raise SystemExit(
                "Use a dedicated worker workspace disjoint from core, evaluation, settings and credentials"
            )
        policy = ToolPolicy(
            args.workspace,
            **config.get("policy", {}),
            protected_roots=protected,
        )
        selector = None
        if harness := config.get("harness"):
            from core.extensions import Registry as HarnessRegistry

            suite = Path(harness["suite"]).resolve()
            if suite.is_relative_to(workspace):
                raise SystemExit("Heldout evaluation suite must be outside the worker workspace")
            selector = HarnessRegistry(
                args.data_dir / "harness",
                suite,
                model_profile={
                    "provider": config["providers"]["agent"]["kind"],
                    "model": config["providers"]["agent"]["model"],
                    "evaluation_model_calls": 0,
                },
            )
        mcp_host = None
        mcp_controller = None
        mcp_entries = config.get("mcp_servers", [])
        mcp_settings = config.get("mcp")
        mcp_enabled = bool(mcp_entries)
        if mcp_settings is not None:
            if (
                not isinstance(mcp_settings, dict)
                or set(mcp_settings) != {"enabled"}
                or type(mcp_settings["enabled"]) is not bool
            ):
                raise SystemExit("mcp accepts an explicit boolean enabled flag")
            mcp_enabled = mcp_settings["enabled"]
        if mcp_enabled:
            # Optional MCP host activates only with explicit owner-authored
            # server configs. The module import stays lazy so the Agent runs
            # without the MCP dependency when nothing is configured.
            from packages.ecosystem.mcp_control import MCPController
            from packages.ecosystem.mcp_host import MCPConfig

            if not isinstance(mcp_entries, list):
                raise SystemExit("mcp_servers must be a list of owner-authored server configs")
            try:
                mcp_configs = [MCPConfig(**entry) for entry in mcp_entries]
            except Exception as exc:
                raise SystemExit(f"Invalid mcp_servers configuration: {exc}") from exc

            def _mcp_secret(ref: str) -> str | None:
                # Secrets resolve only from env:NAME references against the
                # process environment. Never arbitrary files, Muse helpers,
                # or model-provided values.
                if isinstance(ref, str) and ref.startswith("env:"):
                    name = ref[len("env:") :]
                    if name:
                        return os.environ.get(name)
                return None

            mcp_controller = MCPController(
                args.data_dir.resolve() / "mcp",
                workspace,
                policy,
                initial_configs=mcp_configs,
                secret_resolver=_mcp_secret,
            )
            mcp_host = mcp_controller.host
        skill_service = None
        if skill_config := config.get("skills"):
            from services.agent.skill_service import SkillService

            if not isinstance(skill_config, dict) or set(skill_config) - {"enabled"}:
                raise SystemExit("skills accepts only an explicit enabled flag")
            if type(skill_config.get("enabled")) is not bool:
                raise SystemExit("skills.enabled must be a boolean")
            if skill_config["enabled"]:
                skill_service = SkillService(args.data_dir.resolve() / "skills", workspace)
        plugin_service = None
        if plugin_config := config.get("plugins"):
            from services.agent.plugin_service import PluginService

            if not isinstance(plugin_config, dict) or set(plugin_config) - {"enabled", "suites"}:
                raise SystemExit("plugins accepts enabled and owner-held suites only")
            if type(plugin_config.get("enabled")) is not bool:
                raise SystemExit("plugins.enabled must be a boolean")
            suites = plugin_config.get("suites", {})
            if not isinstance(suites, dict) or len(suites) > 128:
                raise SystemExit("plugins.suites must be a bounded owner mapping")
            if plugin_config["enabled"]:
                plugin_service = PluginService(
                    args.data_dir.resolve() / "plugins",
                    workspace,
                    {key: Path(value) for key, value in suites.items()},
                )
        runtime = AgentRuntime(
            journal,
            provider(config, "agent", resource_state=resource_state),
            args.workspace,
            policy,
            selector_registry=selector,
            mcp_host=mcp_host,
            mcp_controller=mcp_controller,
            skill_service=skill_service,
            plugin_service=plugin_service,
        )
        if resources_enabled:
            from services.resources.client import ResourceClient

            runtime.resource_client = ResourceClient(
                config.get("resources_url", "http://127.0.0.1:8764"), tokens["owner"]
            )
        owner_journal = JournalClient(config["session_url"], tokens["owner"])
        profiles = {
            name: (
                lambda profile=profile: provider(
                    {"providers": {"agent": profile}}, "agent", resource_state=resource_state
                )
            )
            for name, profile in config.get("agent_profiles", {}).items()
        }
        commands = Registry(
            runtime, owner_journal, path=args.data_dir / "commands.sqlite", profiles=profiles
        )

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
            await owner_journal.close()
            commands.close()

        app = create_app(runtime, auth, commands)
        app.router.lifespan_context = agent_lifespan
    elif service == "converse":
        from packages.providers.speech import SpeechClient
        from services.converse.api import create_app
        from services.converse.runtime import ConverseRuntime
        from services.decisions.runtime import OutcomeWorker

        journal = JournalClient(config["session_url"], tokens["converse"])
        runtime = ConverseRuntime(
            journal, provider(config, "converse", resource_state=resource_state)
        )
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
            ModelClassifier(provider(config, "decisions", resource_state=resource_state)),
            enabled=config.get("agent_enabled", True),
        )
        app = create_app(worker, auth)
    uvicorn.run(app, host="127.0.0.1", port=args.port or PORTS[service], log_level="warning")


def owner_login(args):
    """Export only the owner credential to a new private file, never stdout."""
    tokens = load_or_create_tokens(args.data_dir)
    destination = args.output or args.data_dir / "owner-login.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(tokens["owner"])
    print(
        json.dumps(
            {
                "path": str(destination.resolve()),
                "mode": "0600",
                "scope": "owner-only browser sign-in",
            }
        )
    )


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
        from packages.commands import parse

        if parse(args.objective) is not None:
            response = await http.post(
                config["agent_url"] + "/v1/commands/execute",
                json={
                    "schema_version": "1.0",
                    **{k: session[k] for k in ("session_id", "thread_id", "epoch")},
                    "input": args.objective,
                    "idempotency_key": new_id(),
                },
            )
            response.raise_for_status()
            result = response.json()
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "ok" else 1
        objective = args.objective[1:] if args.objective.startswith("\\/") else args.objective
        request = TaskRequest(
            session_id=session["session_id"],
            thread_id=session["thread_id"],
            epoch=session["epoch"],
            objective=objective,
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
    login = commands.add_parser("owner-login", help="Export an owner-only browser credential file")
    login.add_argument("--output", type=Path)
    server = commands.add_parser("serve")
    server.add_argument("service", choices=[*PORTS, "all"])
    server.add_argument("--port", type=int)
    agent = commands.add_parser("agent", help="Submit real work directly, without Converse")
    agent.add_argument("objective")
    args = parser.parse_args()
    if args.command == "init":
        initialize(args)
    elif args.command == "owner-login":
        owner_login(args)
    elif args.command == "serve":
        serve(args)
    else:
        raise SystemExit(asyncio.run(direct_agent(args)))


if __name__ == "__main__":
    main()
