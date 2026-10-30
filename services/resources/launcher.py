"""Opt-in first-party conversation worker host; arbitrary argv is not an API."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.client import JournalClient
from packages.providers.qwen3_ipc import MANIFEST_SHA256, MODEL, PROFILE
from packages.providers.tts_backends import profile_settings, validate_tts_device, validate_voice
from packages.resources.processes import WorkerManager, WorkerSpec
from services.resources.api import create_app
from services.resources.client import ConverseControl, local_base
from services.resources.coordinator import ResourceCoordinator
from services.resources.gguf import (
    GGUFProtocol,
    ManagedGGUFConfig,
    configured_binding,
    configured_manifest,
    gguf_worker,
)
from services.resources.laya import ManagedLayaConfig, laya_gpu_sample, laya_ready, laya_worker
from services.resources.managed_text import ManagedTextBroker
from services.resources.qwen3 import (
    ManagedQwen3Config,
    Qwen3Broker,
    qwen3_worker,
    validate_qwen3_inputs,
)
from services.resources.state import ResourceRecord, ResourceState
from services.resources.whistle import ManagedWhistleConfig, WhistleBroker, whistle_worker


class LocalResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    voice_python: str = Field(min_length=1, max_length=4096)
    voice_port: int = Field(default=8765, ge=1024, le=65535)
    stt_model: Literal["tiny", "tiny.en"] = "tiny.en"
    voice_device: Literal["cpu", "cuda"] = "cpu"
    voice_stt_backend: Literal["faster-whisper", "openai-whisper"] = "faster-whisper"
    voice_stt_checkpoint: str | None = Field(default=None, min_length=1, max_length=4096)
    tts_model_dir: str | None = Field(default=None, min_length=1, max_length=4096)
    tts_voice: (
        Literal[
            "af_heart", "F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5", "en-Carter_man"
        ]
        | None
    ) = None
    profile: str = "conversation_priority"
    minimum_free_bytes: int = Field(default=128 * 1024 * 1024, ge=64 * 1024 * 1024, le=1 << 46)
    voice_memory_reserve_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024, ge=128 * 1024 * 1024, le=1 << 46
    )
    laya: ManagedLayaConfig | None = None
    whistle: ManagedWhistleConfig | None = None
    qwen3: ManagedQwen3Config | None = None
    gguf: ManagedGGUFConfig | None = None

    @model_validator(mode="after")
    def one_text_worker(self):
        if self.qwen3 is not None and self.gguf is not None:
            raise ValueError("Select one managed Converse text worker")
        return self

    @model_validator(mode="after")
    def voice_backend_selection(self):
        if self.voice_device == "cuda":
            if self.voice_stt_backend != "openai-whisper" or self.voice_stt_checkpoint is None:
                raise ValueError("GPU voice requires its configured OpenAI Whisper checkpoint")
        elif self.voice_stt_backend != "faster-whisper" or self.voice_stt_checkpoint is not None:
            raise ValueError("CPU voice requires its existing faster-whisper backend")
        return self

    @field_validator("voice_stt_checkpoint")
    @classmethod
    def absolute_stt_checkpoint(cls, value):
        if value is not None and (
            not Path(value).is_absolute() or any(char in value for char in "\0\r\n")
        ):
            raise ValueError("STT checkpoint must be an absolute owner-configured path")
        return value

    @field_validator("tts_model_dir")
    @classmethod
    def absolute_tts_directory(cls, value):
        if value is not None and (
            not Path(value).is_absolute() or any(char in value for char in "\0\r\n")
        ):
            raise ValueError("TTS model directory must be an absolute owner-configured path")
        return value


def resource_app(args, config, auth, tokens, root):
    settings = LocalResourceConfig.model_validate(config.get("resources"))
    if not settings.enabled:
        raise ValueError(
            "Configure resources.enabled and a local voice Python before serving resources"
        )
    ResourceRecord(profile=settings.profile)
    data = args.data_dir.resolve()
    state_path = data / "resources-state.json"
    voice_url = f"http://127.0.0.1:{settings.voice_port}"
    voice = config.get("voice")
    if not isinstance(voice, dict):
        raise ValueError("Configure the managed local voice endpoint")
    tts_profile = voice.get("profile", "Kokoro 82M")
    if not isinstance(tts_profile, str):
        raise ValueError("Select a configured retained TTS profile")
    tts_voice = validate_voice(tts_profile, settings.tts_voice)
    validate_tts_device(tts_profile, settings.voice_device)
    if settings.voice_device == "cuda" and voice.get("stt") is not None:
        raise ValueError("The GPU voice profile cannot select the CPU Whistle target")
    tts_settings = profile_settings(tts_profile)
    if tts_settings.requires_model_directory and settings.tts_model_dir is None:
        raise ValueError(f"{tts_profile} requires its owner-configured local model directory")
    if not tts_settings.requires_model_directory and settings.tts_model_dir is not None:
        raise ValueError("Kokoro does not accept an external model directory")
    tts_revision = tts_settings.artifact_revision
    synthesis_mode = tts_settings.mode
    if settings.tts_model_dir is not None:
        workspace = args.workspace.resolve()
        model_path = Path(settings.tts_model_dir)
        for location in (model_path, model_path.resolve()):
            if (
                location == workspace
                or location.is_relative_to(workspace)
                or workspace.is_relative_to(location)
            ):
                raise ValueError("Managed TTS model inputs must be outside the Agent workspace")
    if settings.voice_stt_checkpoint is not None:
        workspace = args.workspace.resolve()
        checkpoint = Path(settings.voice_stt_checkpoint)
        for location in (checkpoint, checkpoint.resolve()):
            if (
                location == workspace
                or location.is_relative_to(workspace)
                or workspace.is_relative_to(location)
            ):
                raise ValueError("Managed STT checkpoint must be outside the Agent workspace")
    if local_base(voice.get("url", "")) != voice_url:
        raise ValueError(
            "The configured Converse voice endpoint must match its managed local worker"
        )
    spec = WorkerSpec(
        id="voice",
        argv=(
            settings.voice_python,
            "-m",
            "packages.providers.voice_worker",
            "--data-dir",
            str(data),
            "--port",
            str(settings.voice_port),
            "--stt-model",
            settings.stt_model,
            "--device",
            settings.voice_device,
            "--stt-backend",
            settings.voice_stt_backend,
            *(
                ("--stt-checkpoint", settings.voice_stt_checkpoint)
                if settings.voice_stt_checkpoint
                else ()
            ),
            "--tts-profile",
            tts_profile,
            "--tts-voice",
            tts_voice,
            *(("--tts-model-dir", settings.tts_model_dir) if settings.tts_model_dir else ()),
            "--resource-policy",
            str(state_path),
        ),
        cwd=root,
        env={
            "HOME": str(Path.home()),
            "PYTHONPATH": str(root),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "OMP_NUM_THREADS": "4",
            "TOKENIZERS_PARALLELISM": "false",
        },
    )
    specs = [spec]
    resources_url = f"http://127.0.0.1:{getattr(args, 'port', None) or 8764}"
    whistle_endpoint = None
    selected_stt = voice.get("stt")
    if selected_stt is not None or settings.whistle is not None:
        if (
            not isinstance(selected_stt, dict)
            or set(selected_stt) != {"provider", "url"}
            or selected_stt["provider"] != "whistle"
            or settings.whistle is None
            or local_base(selected_stt["url"]) != resources_url
            or local_base(config.get("resources_url", "http://127.0.0.1:8764")) != resources_url
        ):
            raise ValueError("Select Whistle STT at the configured managed resources endpoint")
        whistle_spec, whistle_endpoint = whistle_worker(
            settings.whistle, root, args.workspace.resolve()
        )
        specs.append(whistle_spec)
    laya_probe = None
    if settings.laya is not None:
        if settings.laya.port in {settings.voice_port, 8760, 8761, 8762, 8763, 8764}:
            raise ValueError("Managed Laya port must be separate from core/voice services")
        laya_spec, laya_probe = laya_worker(
            settings.laya,
            config["providers"]["decisions"],
            root,
            args.workspace.resolve(),
            service_tokens=tuple(tokens.values()),
        )
        specs.append(laya_spec)
    qwen3_endpoint, gguf_endpoint, managed_text_binding = None, None, None
    selected_text = config.get("providers", {}).get("converse", {})
    if selected_text.get("kind") == "qwen3" or settings.qwen3 is not None:
        if (
            settings.qwen3 is None
            or selected_text.get("kind") != "qwen3"
            or set(selected_text)
            - {"kind", "base_url", "max_input_tokens", "max_new_tokens", "request_timeout_s"}
            or local_base(selected_text.get("base_url", "")) != resources_url
            or local_base(config.get("resources_url", "http://127.0.0.1:8764")) != resources_url
        ):
            raise ValueError("Select Qwen3 dialogue at the configured managed resources endpoint")
        for name in ("max_input_tokens", "max_new_tokens", "request_timeout_s"):
            expected = getattr(settings.qwen3, name)
            value = selected_text.get(name, expected)
            valid_type = (
                type(value) in (int, float) if name == "request_timeout_s" else type(value) is int
            )
            if not valid_type or value != expected:
                raise ValueError("Qwen3 provider limits must match its managed worker settings")
        qwen3_spec, qwen3_endpoint = qwen3_worker(settings.qwen3, root, args.workspace.resolve())
        specs.append(qwen3_spec)
        managed_text_binding = {
            "provider": "managed-qwen3",
            "model": MODEL,
            "profile": PROFILE,
            "manifest_sha256": MANIFEST_SHA256,
            "resources_url": resources_url,
            "max_input_tokens": settings.qwen3.max_input_tokens,
            "max_new_tokens": settings.qwen3.max_new_tokens,
        }
    if selected_text.get("kind") == "gguf" or settings.gguf is not None:
        if (
            settings.gguf is None
            or selected_text.get("kind") != "gguf"
            or set(selected_text) != {"kind", "base_url"}
            or local_base(selected_text.get("base_url", "")) != resources_url
            or local_base(config.get("resources_url", "http://127.0.0.1:8764")) != resources_url
        ):
            raise ValueError("Select GGUF dialogue at the configured managed resources endpoint")
        gguf_spec, gguf_endpoint = gguf_worker(settings.gguf, root, args.workspace.resolve())
        specs.append(gguf_spec)
        managed_text_binding = configured_binding(
            settings.gguf, gguf_endpoint.manifest, resources_url
        )
    state = ResourceState(state_path, profile=settings.profile)
    if not state_path.exists():
        state.save()
    health_http = httpx.AsyncClient(
        base_url=voice_url, timeout=1, follow_redirects=False, trust_env=False
    )
    agent_http = httpx.AsyncClient(
        base_url=local_base(config["agent_url"]),
        headers={"Authorization": "Bearer " + tokens["owner"]},
        timeout=5,
        follow_redirects=False,
        trust_env=False,
    )
    voice_health = {}

    async def readiness(worker_spec, process):
        if worker_spec.id == "gguf":
            return await gguf_endpoint.probe(worker_spec, process)
        if worker_spec.id == "qwen3":
            return await qwen3_endpoint.probe(worker_spec, process)
        if worker_spec.id == "whistle":
            return await whistle_endpoint.probe(worker_spec, process)
        if worker_spec.id == "laya":
            return await laya_ready(settings.laya, laya_probe, process)
        voice_health.clear()
        try:
            response = await health_http.get("/health")
            payload = response.json()
            required = {
                "service",
                "pid",
                "device",
                "loaded",
                "tts_profile",
                "tts_voice",
                "synthesis_mode",
                "tts_artifact_revision",
                "assets_state",
            }
            if settings.voice_device == "cuda":
                required |= {"stt_backend", "stt_device", "tts_device"}
            if not isinstance(payload, dict) or not required.issubset(payload):
                return False
            loaded = payload["loaded"]
            if not isinstance(loaded, list):
                return False
            actual_device = "cuda:0" if settings.voice_device == "cuda" else "cpu"
            stt_device = payload.get("stt_device", "cpu" if "stt" in loaded else None)
            tts_device = payload.get("tts_device", "cpu" if "tts" in loaded else None)
            if (
                response.status_code != 200
                or payload["service"] != "local-voice"
                or type(payload["pid"]) is not int
                or payload["pid"] != process.pid
                or payload["device"] != settings.voice_device
                or payload.get("stt_backend", "faster-whisper") != settings.voice_stt_backend
                or stt_device != (actual_device if "stt" in loaded else None)
                or tts_device != (actual_device if "tts" in loaded else None)
                or payload["tts_profile"] != tts_profile
                or payload["tts_voice"] != tts_voice
                or payload["synthesis_mode"] != synthesis_mode
                or payload["tts_artifact_revision"] != tts_revision
                or payload["assets_state"] not in ("not_checked", "verified")
                or any(not isinstance(name, str) or name not in {"stt", "tts"} for name in loaded)
                or len(loaded) != len(set(loaded))
                or (
                    tts_settings.requires_model_directory
                    and "tts" in loaded
                    and payload["assets_state"] != "verified"
                )
                or payload.get("synthesis_quarantined", False) is not False
            ):
                return False
            voice_health.clear()
            voice_health.update(
                {
                    "pid": process.pid,
                    "device": payload.get("device"),
                    "loaded": list(loaded),
                    "tts_profile": tts_profile,
                    "tts_voice": tts_voice,
                    "synthesis_mode": synthesis_mode,
                    "tts_artifact_revision": tts_revision,
                    "assets_state": payload["assets_state"],
                }
            )
            voice_health.update(
                {
                    key: payload[key]
                    for key in ("stt_backend", "stt_device", "tts_device")
                    if key in payload
                }
            )
            return True
        except (httpx.HTTPError, ValueError):
            return False

    manager = WorkerManager(specs, readiness=readiness)

    async def before_start(worker_id):
        current = manager.status()["workers"][worker_id]
        if current["state"] == "running" and current["ready"]:
            return
        if worker_id == "qwen3":
            validate_qwen3_inputs(settings.qwen3, args.workspace.resolve())
            return
        if worker_id == "gguf":
            configured_manifest(settings.gguf, args.workspace.resolve())
            return
        if worker_id == "whistle":
            from packages.providers.whistle_assets import validate_assets

            validate_assets(
                settings.whistle.weights, settings.whistle.library, args.workspace.resolve()
            )
            return
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    "127.0.0.1",
                    settings.laya.port if worker_id == "laya" else settings.voice_port,
                ),
                timeout=1,
            )
        except ConnectionRefusedError:
            return
        writer.close()
        await writer.wait_closed()
        raise RuntimeError(
            "Worker endpoint is occupied by an unverified process; it will not be adopted or stopped"
        )

    async def task_status(task_id):
        response = await agent_http.get("/v1/agent/tasks/" + task_id)
        response.raise_for_status()
        return response.json()

    coordinator = ResourceCoordinator(
        manager,
        state,
        ConverseControl(config.get("converse_url", "http://127.0.0.1:8762"), tokens["resources"]),
        journal=JournalClient(config["session_url"], tokens["resources"]),
        task_status=task_status,
        minimum_free_bytes=settings.minimum_free_bytes,
        before_start=before_start,
        restore_reserve_bytes=settings.voice_memory_reserve_bytes
        + (settings.laya.host_memory_reserve_bytes if settings.laya else 0)
        + (settings.whistle.host_memory_reserve_bytes if settings.whistle else 0)
        + (settings.qwen3.host_memory_reserve_bytes if settings.qwen3 else 0)
        + (settings.gguf.host_memory_reserve_bytes if settings.gguf else 0),
        text_worker_id="gguf" if settings.gguf else "qwen3",
        text_restore_reserve_bytes=(
            settings.gguf.host_memory_reserve_bytes if settings.gguf else None
        ),
        qwen3_restore_reserve_bytes=(
            settings.qwen3.host_memory_reserve_bytes if settings.qwen3 else 0
        ),
        managed_text_binding=managed_text_binding,
        whistle_restore_reserve_bytes=(
            settings.whistle.host_memory_reserve_bytes if settings.whistle else 0
        ),
        gpu_probe=(
            (lambda: laya_gpu_sample(settings.laya, manager))
            if settings.laya is not None and settings.laya.device == "cuda"
            else None
        ),
        gpu_restore_reserve_bytes=(settings.laya.gpu_memory_reserve_bytes if settings.laya else 0),
    )
    whistle = (
        WhistleBroker(
            coordinator, whistle_endpoint, request_timeout_s=settings.whistle.request_timeout_s
        )
        if whistle_endpoint is not None
        else None
    )
    qwen3 = (
        Qwen3Broker(coordinator, qwen3_endpoint, request_timeout_s=settings.qwen3.request_timeout_s)
        if qwen3_endpoint is not None
        else None
    )
    gguf = (
        ManagedTextBroker(
            coordinator,
            gguf_endpoint,
            GGUFProtocol(gguf_endpoint, settings.gguf.manifest_sha256),
            request_timeout_s=settings.gguf.request_timeout_s,
        )
        if gguf_endpoint is not None
        else None
    )
    selected_broker = gguf or qwen3
    selected_endpoint = gguf_endpoint or qwen3_endpoint
    if selected_broker is not None and callable(
        getattr(selected_endpoint, "release_after_stop", None)
    ):
        coordinator.register_text_lifecycle(
            abort=selected_broker.abort_active,
            drain=selected_broker.drain,
            release=selected_endpoint.release_after_stop,
        )
    app = create_app(coordinator, auth, whistle, qwen3, gguf)

    @asynccontextmanager
    async def lifespan(app):
        for attempt in range(30):
            try:
                await coordinator.converse.health()
                break
            except httpx.HTTPError:
                if attempt < 29:
                    await asyncio.sleep(0.1)
        await coordinator.initialize()

        async def maintain_control():
            while True:
                await asyncio.sleep(0.25)
                try:
                    await coordinator.reconcile_owner_intent()
                except (OSError, ValueError):
                    # Invalid owner policy blocks Converse at its own boundary;
                    # never replace it with an enabled default here.
                    pass
                if coordinator.publication_pending:
                    try:
                        await asyncio.wait_for(coordinator._publish(), timeout=1)
                        coordinator.publication_pending = False
                    except Exception:
                        pass

        publication = asyncio.create_task(maintain_control())
        try:
            yield
        finally:
            publication.cancel()
            await asyncio.gather(publication, return_exceptions=True)
            try:
                failures = []
                for broker in (whistle, qwen3, gguf):
                    if broker is not None:
                        try:
                            await broker.close()
                        except Exception as exc:
                            failures.append(exc)
                # Always reconcile the actual owned processes, including when
                # a transport close failed. Endpoint pruning follows only a
                # successful manager close that confirmed group release.
                await coordinator.close()
                if whistle_endpoint is not None:
                    whistle_endpoint.close()
                if qwen3_endpoint is not None:
                    qwen3_endpoint.close()
                if gguf_endpoint is not None:
                    gguf_endpoint.close()
                if failures:
                    raise failures[0]
            finally:
                await health_http.aclose()
                await agent_http.aclose()
                if laya_probe is not None:
                    await laya_probe.close()

    app.router.lifespan_context = lifespan
    app.state.coordinator = coordinator
    app.state.voice_health = voice_health
    app.state.whistle = whistle
    app.state.qwen3 = qwen3
    app.state.gguf = gguf
    return app
