"""Opt-in native media owner wiring; configuration never grants model authority."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path


def _absolute(value):
    if type(value) is not str or not value or "\0" in value:
        raise ValueError
    path = Path(value)
    if not path.is_absolute() or path != Path(os.path.abspath(path)) or path != path.resolve():
        raise ValueError
    return path


def _profile(value):
    names = {
        "source_sha256",
        "width",
        "height",
        "fps_num",
        "fps_den",
        "frame_count",
        "duration_seconds",
    }
    if type(value) is not dict or set(value) != names:
        raise ValueError
    if type(value["source_sha256"]) is not str or not re.fullmatch(
        "[a-f0-9]{64}", value["source_sha256"]
    ):
        raise ValueError
    for name, maximum in (
        ("width", 1280),
        ("height", 720),
        ("fps_num", 120_000),
        ("fps_den", 10_000),
        ("frame_count", 1800),
    ):
        if type(value[name]) is not int or not 1 <= value[name] <= maximum:
            raise ValueError
    if not value["fps_den"] <= value["fps_num"] <= 60 * value["fps_den"]:
        raise ValueError
    duration = value["duration_seconds"]
    if type(duration) not in {int, float} or not math.isfinite(duration) or not 0 < duration <= 60:
        raise ValueError
    return {**value, "duration_seconds": float(duration)}


def _components():
    # No provider, decoder, registry, or environment lookup for default hosts.
    from packages.providers.meta_video import MetaVideoProvider
    from services.agent.media_assets import NativeMediaAssets
    from services.agent.sam_video import SamVideoProfile, SamVideoProjector
    from services.agent.task_media_factory import TaskMediaFactory

    return (
        NativeMediaAssets,
        MetaVideoProvider,
        SamVideoProfile,
        SamVideoProjector,
        TaskMediaFactory,
    )


class TaskMediaOwner:
    def __init__(self, *, assets, adapter, factory, source_sha256):
        self.media_assets, self.adapter, self.factory = assets, adapter, factory
        self.source_sha256 = source_sha256

    def bind(self, runtime):
        if (
            runtime.task_harness_broker_factory is not None
            or runtime.task_preworker_hook is not None
        ):
            raise ValueError("Native task media owner is already configured")
        runtime.task_harness_broker_factory = self.factory
        runtime.task_preworker_hook = self.factory.enroll
        # Retain the client/registry even if a later close is unconfirmed.
        runtime.task_media_owner = self

    async def close(self):
        # An uncertain provider close retains the registry for its original
        # projection. Never replace this owner or hide the cleanup exception.
        await self.adapter.close()
        self.media_assets.close()


def task_media_owner(config, *, data_dir, workspace):
    """Construct one dormant owner, with no network or provider inference.

    The plan may be written later by the native clip driver. Enrollment reads
    and verifies its actual protected file immediately before worker launch.
    """
    declaration = config.get("task_media")
    if declaration is None:
        return None
    assets = None
    try:
        if (
            type(declaration) is not dict
            or set(declaration) != {"kind", "owner_plan", "api_key_env", "video_profile"}
            or declaration["kind"] != "meta_video_v1"
        ):
            raise ValueError
        workspace, data_dir = _absolute(str(workspace)), _absolute(str(data_dir))
        plan = _absolute(declaration["owner_plan"])
        if plan.is_relative_to(workspace) or data_dir.is_relative_to(workspace):
            raise ValueError
        reference = declaration["api_key_env"]
        if (
            type(reference) is not str
            or not reference.isascii()
            or not reference.isidentifier()
            or len(reference) > 128
        ):
            raise ValueError
        profile_value = _profile(declaration["video_profile"])
        key = os.environ.get(reference)
        if type(key) is not str or not key:
            raise ValueError
        Assets, Provider, Profile, Projector, Factory = _components()
        profile = Profile(**profile_value)
        assets = Assets(data_dir / "media-assets.sqlite")
        projector = Projector(
            media_assets=assets, work_root=data_dir / "sam-render", profile=profile
        )

        async def scoped_provider(prepared, current):
            current()
            if prepared.grant.source_sha256 != profile_value["source_sha256"]:
                raise ValueError("Video source differs from the native rendering profile")
            return await adapter(prepared, current)

        factory = Factory(plan_path=plan, provider=scoped_provider)
        adapter = Provider(api_key=key, sam_projector=projector)
        return TaskMediaOwner(
            assets=assets,
            adapter=adapter,
            factory=factory,
            source_sha256=profile_value["source_sha256"],
        )
    except Exception:
        if assets is not None:
            assets.close()
        raise ValueError("Native task media configuration is unavailable") from None
