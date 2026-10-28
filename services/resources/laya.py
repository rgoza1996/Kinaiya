"""Trusted owner configuration for a managed pinned local Laya worker."""

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.providers.laya import LayaRoutingProvider
from packages.providers.laya_artifact import valid_artifact_sha256
from packages.resources.gpu import read_amd_memory, read_amd_process_vram, valid_pci_bus_id
from packages.resources.processes import WorkerSpec
from services.resources.client import local_base

CHILD_SECRET_ENV = "KINAIYA_LAYA_WORKER_SECRET"


class ManagedLayaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    python: str = Field(min_length=1, max_length=4096)
    model_dir: str = Field(min_length=1, max_length=4096)
    manifest: str = Field(min_length=1, max_length=4096)
    package_path: str | None = Field(default=None, min_length=1, max_length=4096)
    port: int = Field(default=8871, ge=1024, le=65535)
    device: Literal["cpu", "cuda"] = "cpu"
    pci_bus_id: str | None = None
    gpu_index: int = Field(default=0, ge=0, le=15)
    host_memory_reserve_bytes: int = Field(default=2 * 1024**3, ge=128 * 1024**2, le=1 << 46)
    gpu_memory_reserve_bytes: int = Field(default=4 * 1024**3, ge=128 * 1024**2, le=1 << 46)
    startup_timeout_s: float = Field(default=45, ge=1, le=120)

    @model_validator(mode="after")
    def gpu_binding(self):
        if self.device == "cuda" and not valid_pci_bus_id(self.pci_bus_id):
            raise ValueError("Managed GPU Laya requires an explicit lowercase PCI bus ID")
        if self.device == "cpu" and self.pci_bus_id is not None:
            raise ValueError("A CPU worker cannot claim GPU ownership")
        return self


def laya_worker(settings, profile, root, workspace, *, service_tokens=()):
    """Return a fixed WorkerSpec and bounded authenticated readiness adapter."""
    url = f"http://127.0.0.1:{settings.port}"
    if profile.get("kind") != "laya" or local_base(profile.get("base_url", "")) != url:
        raise ValueError("Managed Laya must match the configured loopback Decisions provider")
    if profile.get("allow_cloud", False) is not False or "api_key" in profile:
        raise ValueError("Managed Laya uses a separate local secret reference")
    pin = profile.get("expected_artifact_sha256")
    alias = profile.get("model")
    key_ref = profile.get("api_key_env")
    if (
        not valid_artifact_sha256(pin)
        or not isinstance(alias, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", alias)
    ):
        raise ValueError("Managed Laya requires an explicit artifact pin and model alias")
    if not isinstance(key_ref, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", key_ref):
        raise ValueError("Managed Laya requires its separate API secret environment reference")
    key = os.environ.get(key_ref, "")
    if not key or len(key.encode()) > 4096 or any(c in key for c in "\r\n"):
        raise ValueError("The configured separate Laya API secret is unavailable")
    if key in service_tokens:
        raise ValueError("Laya must not reuse a Kinaiya owner or service credential")
    for value in [settings.python, settings.model_dir, settings.manifest, settings.package_path]:
        if value is None:
            continue
        path = Path(value)
        if not path.is_absolute() or not path.exists():
            raise ValueError("Managed Laya paths must be absolute existing owner inputs")
        for location in (path, path.resolve()):
            if (
                location == workspace
                or location.is_relative_to(workspace)
                or workspace.is_relative_to(location)
            ):
                raise ValueError("Managed Laya inputs must be outside the Agent workspace")
    env = {
        "HOME": str(Path.home()),
        "PYTHONPATH": str(root)
        + (os.pathsep + settings.package_path if settings.package_path else ""),
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "TRANSFORMERS_OFFLINE": "1",
        CHILD_SECRET_ENV: key,
    }
    if settings.device == "cuda":
        env["ROCR_VISIBLE_DEVICES"] = str(settings.gpu_index)
    spec = WorkerSpec(
        id="laya",
        argv=(
            settings.python,
            "-m",
            "services.decisions.laya_worker",
            "--model-dir",
            settings.model_dir,
            "--manifest",
            settings.manifest,
            "--expected-artifact-sha256",
            pin,
            "--api-key-env",
            CHILD_SECRET_ENV,
            "--device",
            settings.device,
            "--port",
            str(settings.port),
        ),
        cwd=root,
        env=env,
        startup_timeout_s=settings.startup_timeout_s,
        stop_timeout_s=5,
    )
    probe = LayaRoutingProvider(
        url, model=alias, api_key=key, expected_artifact_sha256=pin, timeout=1
    )
    return spec, probe


async def laya_ready(settings, probe, process):
    health = await probe.health()
    if (
        health.get("ready") is not True
        or health.get("worker_pid") != process.pid
        or health.get("device", "").split(":")[0] != settings.device
    ):
        return False
    if settings.device == "cuda":
        memory = read_amd_memory(settings.pci_bus_id)
        used = read_amd_process_vram(process.pid, settings.pci_bus_id)
        if memory is None or used is None or not 0 < used <= memory["total_bytes"]:
            return False
    return True


def laya_gpu_sample(settings, manager):
    """Driver telemetry only; WorkerManager alone establishes process ownership."""
    memory = read_amd_memory(settings.pci_bus_id)
    if memory is None:
        return None
    worker = manager.status()["workers"]["laya"]
    if worker["state"] == "stopped":
        evidence = worker.get("stop_evidence")
        if worker.get("owned_pid") is not None and (
            not evidence or not evidence.get("reaped") or not evidence.get("group_empty")
        ):
            return None
        used = 0  # No child started, or its retained ownership handles proved release.
    elif worker["state"] == "running" and worker["ready"] and worker["rss_bytes"] is not None:
        used = read_amd_process_vram(worker["owned_pid"], settings.pci_bus_id)
        if used is None or used > memory["total_bytes"]:
            return None
    else:
        return None
    return {**memory, "worker_resident_bytes": used}
