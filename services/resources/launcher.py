"""Opt-in first-party conversation worker host; arbitrary argv is not an API."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from packages.contracts.client import JournalClient
from packages.resources.processes import WorkerManager, WorkerSpec
from services.resources.api import create_app
from services.resources.client import ConverseControl, local_base
from services.resources.coordinator import ResourceCoordinator
from services.resources.laya import ManagedLayaConfig, laya_gpu_sample, laya_ready, laya_worker
from services.resources.state import ResourceRecord, ResourceState


class LocalResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    voice_python: str = Field(min_length=1, max_length=4096)
    voice_port: int = Field(default=8765, ge=1024, le=65535)
    stt_model: Literal["tiny", "tiny.en"] = "tiny.en"
    profile: str = "conversation_priority"
    minimum_free_bytes: int = Field(default=128 * 1024 * 1024, ge=64 * 1024 * 1024, le=1 << 46)
    voice_memory_reserve_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024, ge=128 * 1024 * 1024, le=1 << 46
    )
    laya: ManagedLayaConfig | None = None


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
    if local_base(config.get("voice", {}).get("url", "")) != voice_url:
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
        if worker_spec.id == "laya":
            return await laya_ready(settings.laya, laya_probe, process)
        try:
            response = await health_http.get("/health")
            payload = response.json()
            if (
                response.status_code != 200
                or payload.get("service") != "local-voice"
                or payload.get("pid") != process.pid
            ):
                return False
            voice_health.clear()
            voice_health.update(
                {
                    "pid": process.pid,
                    "device": payload.get("device"),
                    "loaded": [x for x in payload.get("loaded", []) if x in {"stt", "tts"}],
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
        + (settings.laya.host_memory_reserve_bytes if settings.laya else 0),
        gpu_probe=(
            (lambda: laya_gpu_sample(settings.laya, manager))
            if settings.laya is not None and settings.laya.device == "cuda"
            else None
        ),
        gpu_restore_reserve_bytes=(settings.laya.gpu_memory_reserve_bytes if settings.laya else 0),
    )
    app = create_app(coordinator, auth)

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
                await coordinator.close()
            finally:
                await health_http.aclose()
                await agent_http.aclose()
                if laya_probe is not None:
                    await laya_probe.close()

    app.router.lifespan_context = lifespan
    app.state.coordinator = coordinator
    app.state.voice_health = voice_health
    return app
