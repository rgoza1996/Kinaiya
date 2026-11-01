"""Independent service launcher and direct Agent CLI. Optional imports stay lazy."""

import argparse
import asyncio
import json
import math
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


def converse_speech(config, token, *, resource_state=None):
    """Select STT and TTS independently, keeping service and cloud secrets apart."""
    from packages.providers.speech import SpeechClient

    voice = config.get("voice")
    if not voice:
        return None
    choice, settings, cloud = voice.get("stt"), {}, None
    tts_choice, cloud_tts = voice.get("tts"), None
    tts_settings = None
    if tts_choice is not None:
        from packages.providers.openrouter_speech import openrouter_tts_settings

        tts_settings = openrouter_tts_settings(tts_choice)
    if isinstance(choice, dict) and choice.get("provider") == "meta":
        from packages.providers.meta_speech import MetaSTTClient, meta_stt_settings

        cloud = MetaSTTClient(**meta_stt_settings(choice))
    elif choice is not None:
        from services.resources.client import local_base
        from services.resources.whistle import ManagedWhistleConfig

        if (
            config.get("resources", {}).get("enabled") is not True
            or not isinstance(choice, dict)
            or set(choice) != {"provider", "url"}
            or choice["provider"] != "whistle"
            or local_base(choice["url"])
            != local_base(config.get("resources_url", "http://127.0.0.1:8764"))
        ):
            raise ValueError("Whistle STT must select the managed resources endpoint")
        managed = ManagedWhistleConfig.model_validate(config["resources"].get("whistle"))
        settings = {
            "stt_url": choice["url"],
            "stt_provider": "whistle",
            "stt_timeout_s": managed.request_timeout_s,
        }
    local = None
    if cloud is None or tts_settings is None:
        local = SpeechClient(
            voice["url"], token, profile=voice.get("profile", "Kokoro 82M"), **settings
        )
    if tts_settings is not None:
        from packages.providers.openrouter_speech import OpenRouterTTSClient

        cloud_tts = OpenRouterTTSClient(**tts_settings)
    if cloud is not None or cloud_tts is not None:
        from packages.providers.meta_speech import CloudSTTLocalTTS

        return CloudSTTLocalTTS(
            local,
            cloud if cloud is not None else local,
            tts=cloud_tts,
            resource_state=resource_state,
        )
    return local


def provider(config, role, *, resource_state=None, service_token=None, workspace=None):
    from packages.providers.text import CompatibleTextProvider, MetaResponsesProvider

    profile = dict(config["providers"][role])
    kind = profile.pop("kind")
    if kind == "meta":
        if role == "agent":
            profile.setdefault("structured_actions", True)
        result = MetaResponsesProvider(**profile)
    elif kind == "lmstudio":
        if role not in {"converse", "agent", "auxiliary"}:
            raise SystemExit("Native LM Studio supports Converse, Agent and auxiliary text only")
        from packages.providers.lmstudio import LMStudioProvider

        # The adapter owns exact native counting, prediction and cleanup. Each
        # model factory call constructs a fresh instance; auxiliary requests
        # must not share the primary Agent's active operation/cleanup state.
        # Its constructor permits only reasoning_effort="none".
        result = LMStudioProvider(**profile)
    elif kind in {"compatible", "laya"}:
        if kind == "laya" and role != "decisions":
            raise SystemExit("Laya is a typed Decisions provider, not an Agent or Converse model")
        key_ref = profile.pop("api_key_env", None)
        if key_ref:
            profile["api_key"] = os.environ.get(key_ref, "")
        if kind == "laya":
            from packages.providers.laya import LayaRoutingProvider

            result = LayaRoutingProvider(**profile)
        else:
            result = CompatibleTextProvider(**profile)
    elif kind == "qwen3":
        if role != "converse":
            raise SystemExit(
                "Qwen3 is a managed Converse provider, not an Agent or Decisions model"
            )
        from packages.providers.qwen3_ipc import validate_limits
        from services.resources.client import local_base

        resources = config.get("resources")
        if (
            set(profile) - {"base_url", "max_input_tokens", "max_new_tokens", "request_timeout_s"}
            or not isinstance(resources, dict)
            or resources.get("enabled") is not True
            or not isinstance(resources.get("qwen3"), dict)
            or resource_state is None
            or not isinstance(service_token, str)
            or not service_token.strip()
            or any(char in service_token for char in "\0\r\n")
        ):
            raise ValueError("Qwen3 requires explicit managed resources and a service credential")
        base_url = profile.get("base_url")
        resources_url = config.get("resources_url", "http://127.0.0.1:8764")
        if (
            not isinstance(base_url, str)
            or not isinstance(resources_url, str)
            or local_base(base_url) != local_base(resources_url)
        ):
            raise ValueError("Qwen3 must select the configured managed resources endpoint")
        managed = resources["qwen3"]
        limits = validate_limits(
            managed.get("max_input_tokens", 2048), managed.get("max_new_tokens", 128)
        )
        selected = validate_limits(
            profile.get("max_input_tokens", limits["max_input_tokens"]),
            profile.get("max_new_tokens", limits["max_new_tokens"]),
        )
        timeout = managed.get("request_timeout_s", 120)
        selected_timeout = profile.get("request_timeout_s", timeout)
        if (
            selected != limits
            or type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or timeout <= 0
            or type(selected_timeout) not in {int, float}
            or not math.isfinite(selected_timeout)
            or selected_timeout != timeout
        ):
            raise ValueError("Qwen3 provider limits must match its managed worker settings")
        from packages.providers.qwen3 import ManagedQwen3Provider

        result = ManagedQwen3Provider(
            local_base(base_url), service_token, **limits, request_timeout_s=timeout
        )
    elif kind == "gguf":
        if role != "converse":
            raise SystemExit("Managed GGUF is selected independently for Converse")
        from packages.providers.gguf import ManagedGGUFProvider
        from services.resources.client import local_base
        from services.resources.gguf import ManagedGGUFConfig, configured_manifest

        resources = config.get("resources")
        if (
            set(profile) - {"base_url"}
            or not isinstance(resources, dict)
            or resources.get("enabled") is not True
            or resources.get("qwen3") is not None
            or not isinstance(resources.get("gguf"), dict)
            or resource_state is None
            or workspace is None
        ):
            raise ValueError("GGUF requires protected managed resources and an Agent workspace")
        base_url = local_base(profile.get("base_url", ""))
        if base_url != local_base(config.get("resources_url", "http://127.0.0.1:8764")):
            raise ValueError("GGUF must select the configured resources origin")
        settings = ManagedGGUFConfig.model_validate(resources["gguf"])
        manifest = configured_manifest(settings, workspace)
        result = ManagedGGUFProvider(
            base_url,
            service_token,
            model=manifest["model"],
            alias=manifest["alias"],
            profile=manifest["profile"],
            manifest_sha256=settings.manifest_sha256,
            context_tokens=settings.context_tokens,
            max_new_tokens=settings.max_new_tokens,
            request_timeout_s=settings.request_timeout_s,
        )
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
    if getattr(args, "start_paused", False) and args.service != "converse":
        raise SystemExit("--start-paused is only supported by serve converse")
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
        if getattr(args, "converse_memory", False):
            commands["session"].append("--converse-memory")
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

        app = create_app(
            Store(args.data_dir / "journal.sqlite"),
            auth,
            converse_memory=getattr(args, "converse_memory", False),
        )
    elif service == "agent":
        from core.policy import ToolPolicy
        from packages.commands import Registry
        from services.agent import AgentRuntime, create_app
        from services.agent.artifacts import ArtifactStore

        args.workspace.mkdir(parents=True, exist_ok=True)
        journal = JournalClient(config["session_url"], tokens["agent"])
        protected = (
            ROOT / "core",
            ROOT / "services/agent",
            ROOT / "services/session",
            ROOT / "services/decisions",
            ROOT / "services/converse",
            ROOT / "packages/contracts",
            ROOT / "packages/resources",
            ROOT / "services/resources",
            ROOT / "services/settings",
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
        configured_providers = config.get("providers")
        host_profile = (
            configured_providers.get("agent") if isinstance(configured_providers, dict) else None
        )
        if not isinstance(host_profile, dict):
            host_profile = {}
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
                    "provider": host_profile["kind"],
                    "model": host_profile["model"],
                    "evaluation_model_calls": 0,
                }
                if type(host_profile.get("kind")) is str and type(host_profile.get("model")) is str
                else None,
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
        artifact_path = args.data_dir.resolve() / "artifacts.sqlite3"
        if artifact_path.resolve().is_relative_to(workspace):
            raise SystemExit("UI artifact storage must stay outside the worker workspace")
        from services.settings.provider_factory import UnavailableHostProvider

        try:
            host_provider = provider(config, "agent", resource_state=resource_state)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError, SystemExit):
            # This is an unavailable host profile, never a fallback. Keep the
            # owner catalog/Options control plane available so a valid saved
            # catalog selection can serve new work without the host credential.
            host_provider = UnavailableHostProvider()
        runtime = AgentRuntime(
            journal,
            host_provider,
            args.workspace,
            policy,
            selector_registry=selector,
            mcp_host=mcp_host,
            mcp_controller=mcp_controller,
            skill_service=skill_service,
            plugin_service=plugin_service,
            artifact_store=ArtifactStore(artifact_path),
        )
        if resources_enabled:
            from services.resources.client import ResourceClient

            runtime.resource_client = ResourceClient(
                config.get("resources_url", "http://127.0.0.1:8764"), tokens["owner"]
            )
        owner_journal = JournalClient(config["session_url"], tokens["owner"])
        from services.agent.projects import Projects

        project_path = args.data_dir.resolve() / "projects.sqlite"
        if project_path.is_relative_to(workspace):
            raise SystemExit("Owner project storage must stay outside the worker workspace")
        runtime.projects = Projects(project_path)
        from services.agent.runtime_settings import AgentRuntimeSettings

        runtime.runtime_settings = AgentRuntimeSettings(
            runtime, path=args.data_dir / "agent-runtime-settings.sqlite"
        )
        from services.agent.tool_permissions import AgentToolPermissions

        runtime.tool_permissions = AgentToolPermissions(
            runtime, path=args.data_dir / "agent-tool-permissions.sqlite"
        )
        profiles = {
            name: (
                lambda profile=profile: provider(
                    {"providers": {"agent": profile}}, "agent", resource_state=resource_state
                )
            )
            for name, profile in config.get("agent_profiles", {}).items()
        }
        import sqlite3

        from services.agent.model_settings import AgentModelSettings
        from services.settings.provider_catalog import ProviderCatalog

        # Create the shared protected owner early enough for task model
        # selection. The owner router/lifespan below reuse this connection.
        try:
            catalog = ProviderCatalog(
                args.data_dir.resolve() / "providers.sqlite",
                args.workspace.resolve(),
                env_refs=config.get("provider_credential_references", {}),
            )
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            catalog = None

        def model_factory(profile, *, auxiliary=False):
            def create(effort):
                selected = dict(profile)
                if effort is not None:
                    selected["reasoning_effort"] = effort
                role = "auxiliary" if auxiliary else "agent"
                if auxiliary and selected.get("kind") == "meta":
                    selected["structured_actions"] = False
                    selected["action_format"] = "flat-v1"
                result = provider(
                    {"providers": {role: selected}},
                    role,
                    resource_state=None if auxiliary else resource_state,
                )
                if auxiliary and resource_state is not None:
                    from services.resources.providers import ResourceBoundProvider

                    result = ResourceBoundProvider(result, resource_state, "agent")
                return result

            return create

        runtime.model_settings = AgentModelSettings(
            runtime.provider,
            {
                name: model_factory(profile)
                for name, profile in config.get("agent_profiles", {}).items()
            },
            path=args.data_dir / "agent-models.sqlite",
            reasoning_levels=config.get("agent_reasoning_options", {}),
            host_factory=model_factory(host_profile),
            auxiliary_factories={
                name: model_factory(profile, auxiliary=True)
                for name, profile in config.get("agent_profiles", {}).items()
            },
            auxiliary_host_factory=model_factory(host_profile, auxiliary=True),
            catalog=catalog,
            resource_state=resource_state,
            workspace=runtime.policy.workspace,
        )
        try:
            runtime.provider = runtime.model_settings.selection(None)[0]
        except ValueError:
            # Preserve the control plane so the owner can repair a removed or
            # unavailable saved profile. Task acceptance still refuses it.
            pass
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
        import sqlite3

        from services.converse.api import create_app
        from services.converse.intent import ConverseIntent
        from services.converse.memory_adapter import SessionMemoryAdapter
        from services.converse.model_settings import ConverseModelSettings
        from services.converse.runtime import ConverseRuntime
        from services.converse.setup import safe_host_setup
        from services.converse.windows import WindowConverseController
        from services.decisions.runtime import OutcomeWorker
        from services.settings.provider_catalog import ProviderCatalog
        from services.settings.provider_factory import UnavailableHostProvider

        try:
            catalog = ProviderCatalog(
                args.data_dir.resolve() / "providers.sqlite",
                args.workspace.resolve(),
                env_refs=config.get("provider_credential_references", {}),
            )
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            catalog = None

        journal = JournalClient(config["session_url"], tokens["converse"])
        initial_enabled = True
        if resource_state is not None and resource_state.exists():
            from services.resources.state import read_record

            initial_enabled = read_record(resource_state).desired_converse
        intent = ConverseIntent(
            args.data_dir.resolve() / "converse-intent.json", initial_enabled=initial_enabled
        )
        try:
            memory_adapters = {
                "korina-dag-v1": SessionMemoryAdapter(config["session_url"], tokens["converse"])
            }
        except ValueError:
            memory_adapters = {}
        try:
            host_provider = provider(
                config,
                "converse",
                resource_state=resource_state,
                service_token=tokens["converse"],
                workspace=args.workspace.resolve(),
            )
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError, SystemExit):
            host_provider = UnavailableHostProvider("Converse")
        runtime = ConverseRuntime(
            journal,
            host_provider,
            intent=intent,
            resources_managed=resources_enabled,
            memory_adapters=memory_adapters,
            context_budgeted=True,
        )
        if getattr(args, "start_paused", False):
            # A launch-time availability fence, installed before observers or
            # the listener exist. It must not rewrite the durable owner choice.
            runtime.enabled = False
        setup = safe_host_setup(config)
        try:
            runtime.model_settings = ConverseModelSettings(
                runtime,
                catalog,
                args.data_dir.resolve() / "converse-model.sqlite",
                host_reasoning=getattr(setup, "reasoning", None),
                resource_state=resource_state,
                workspace=args.workspace.resolve(),
            )
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            # A missing/corrupt saved role is unavailable. Never silently use
            # the host profile in place of the owner's persisted selection.
            runtime._model_options_unavailable = True
        try:
            speech = converse_speech(config, tokens["converse"], resource_state=resource_state)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError, SystemExit):
            speech = None
            runtime.speech_unavailable = True
        windows = (
            WindowConverseController(runtime)
            if config.get("streaming_windows", {}).get("enabled", True)
            else None
        )
        app = create_app(runtime, auth, speech, windows, setup=setup)

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
            window_observer = asyncio.create_task(windows.run()) if windows else None
            yield
            observer.cancel()
            if window_observer:
                window_observer.cancel()
            await asyncio.gather(
                observer, *([window_observer] if window_observer else []), return_exceptions=True
            )
            await runtime.close()
            if runtime.model_settings is not None:
                await runtime.model_settings.close()
            await journal.close()
            if speech:
                await speech.close()

        app.router.lifespan_context = converse_lifespan
    else:
        import sqlite3

        from packages.agent_adapters.http import AgentClient
        from packages.providers.text import ProviderError
        from services.decisions.api import create_app
        from services.decisions.model_settings import (
            DecisionsModelSettings,
            DecisionsModelUnavailable,
            UnavailableDecisionsModels,
        )
        from services.decisions.runtime import DecisionsWorker, ModelClassifier
        from services.decisions.window_routing import WindowAgentDispatcher, WindowClassifier
        from services.decisions.window_worker import WindowDecisionsWorker
        from services.settings.provider_catalog import ProviderCatalog
        from services.settings.provider_factory import UnavailableHostProvider

        window_chars = config.get("decisions_window_chars", 36000)
        if type(window_chars) is not int or not 256 <= window_chars <= 36000:
            raise SystemExit("decisions_window_chars must be an integer from 256 to 36000")
        try:
            catalog = ProviderCatalog(
                args.data_dir.resolve() / "providers.sqlite",
                args.workspace.resolve(),
                env_refs=config.get("provider_credential_references", {}),
            )
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            catalog = None
        try:
            host_provider = provider(config, "decisions", resource_state=resource_state)
        except (OSError, ValueError, ProviderError, KeyError, TypeError, ImportError, SystemExit):
            host_provider = UnavailableHostProvider("Decisions")
        try:
            decision_models = DecisionsModelSettings(
                host_provider,
                catalog,
                path=args.data_dir.resolve() / "decisions-model.sqlite",
                workspace=args.workspace.resolve(),
                resource_state=resource_state,
            )
        except (DecisionsModelUnavailable, OSError, ValueError, RuntimeError, sqlite3.Error):
            decision_models = UnavailableDecisionsModels(host_provider)
        worker = DecisionsWorker(
            JournalClient(config["session_url"], tokens["decisions"]),
            AgentClient(config["agent_url"], tokens["decisions"]),
            ModelClassifier(
                host_provider,
                window_chars=window_chars,
            ),
            enabled=config.get("agent_enabled", True),
            model_settings=decision_models,
        )
        windows_enabled = config.get("streaming_windows", {}).get("enabled", True)
        windows = (
            WindowDecisionsWorker(
                worker.journal,
                worker.agent,
                WindowClassifier(worker.classifier.provider, window_chars=window_chars),
                agent_enabled=worker.enabled,
                timeout=config.get("streaming_windows", {}).get("classification_timeout_s", 5),
                model_settings=decision_models,
            )
            if windows_enabled
            else None
        )
        dispatcher = (
            WindowAgentDispatcher(worker.journal, worker.agent, enabled=worker.enabled)
            if windows_enabled
            else None
        )
        app = create_app(worker, auth, windows, dispatcher, model_settings=decision_models)
    if service in {"session", "agent", "converse", "decisions"}:
        import sqlite3

        from services.settings.provider_api import router as provider_router
        from services.settings.provider_catalog import ProviderCatalog

        # Each independent owner service can manage the same protected catalog.
        # Saving a declaration never hot-swaps any role or starts inference.
        if service not in {"agent", "converse", "decisions"}:
            try:
                catalog = ProviderCatalog(
                    args.data_dir.resolve() / "providers.sqlite",
                    args.workspace.resolve(),
                    env_refs=config.get("provider_credential_references", {}),
                )
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                # An unavailable catalog cannot stop an independent host role.
                catalog = None
        app.include_router(provider_router(catalog, auth))
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def catalog_lifespan(app):
            try:
                async with original_lifespan(app):
                    yield
            finally:
                if catalog is not None:
                    catalog.close()

        app.router.lifespan_context = catalog_lifespan
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
                    "content_scope": getattr(args, "content_scope", "local_only"),
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
            content_scope=getattr(args, "content_scope", "local_only"),
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
    parser.add_argument(
        "--content-scope",
        choices=("local_only", "public_synthetic", "private_cloud_authorized"),
        default="local_only",
        help="Disclosure grant for this Agent input; defaults to local-only",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    login = commands.add_parser("owner-login", help="Export an owner-only browser credential file")
    login.add_argument("--output", type=Path)
    server = commands.add_parser("serve")
    server.add_argument("service", choices=[*PORTS, "all"])
    server.add_argument("--port", type=int)
    server.add_argument(
        "--start-paused",
        action="store_true",
        help="Converse only: close temporary admission before startup; preserve owner intent",
    )
    server.add_argument(
        "--converse-memory",
        action="store_true",
        help="Install experimental owner-managed local Converse memory API (no automatic recall)",
    )
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
