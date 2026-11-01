"""Shared deterministic owner controls. Optional skill imports remain lazy."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from packages.contracts.models import TaskRequest


class SkillControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal[
        "install",
        "install_archive",
        "enable",
        "disable",
        "update",
        "update_archive",
        "rollback",
        "remove",
        "scope",
        "inspect",
    ]
    skill_id: str | None = Field(default=None, min_length=1, max_length=256)
    source: str | None = Field(default=None, min_length=1, max_length=4096)
    source_ref: str | None = Field(default=None, min_length=1, max_length=1024)
    content_scope: Literal["local_only", "public_synthetic", "private_cloud_authorized"] | None = (
        None
    )


def require_skills(runtime):
    if runtime.skill_service is None:
        raise ValueError("No skill controller configured; enable skills in owner configuration")
    return runtime.skill_service


async def apply_skill_control(runtime, control: SkillControl):
    return await runtime.owned_workspace_operation(
        lambda: _apply_skill_control(runtime, control),
        require_root=control.operation
        in {"install", "install_archive", "update", "update_archive"},
    )


async def _apply_skill_control(runtime, control):
    service = require_skills(runtime)
    try:
        if control.operation == "inspect":
            if not control.skill_id or any(
                value is not None
                for value in (control.source, control.source_ref, control.content_scope)
            ):
                raise ValueError("inspect requires only skill_id")
            return await service.inspect_owner(control.skill_id)
        ref = None
        if control.source_ref is not None:
            from packages.ecosystem.skills import SourceRef

            ref = SourceRef.parse(control.source_ref).validate()
        return await service.mutate(
            control.operation,
            authority="owner",
            skill_id=control.skill_id,
            source=control.source,
            source_ref=ref,
            content_scope=control.content_scope,
        )
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def router(runtime, auth):
    routes = APIRouter(prefix="/v1/skills", dependencies=[Depends(auth.require("owner"))])

    @routes.get("")
    async def list_skills(after: int = 0, limit: int = 8):
        try:
            return await require_skills(runtime).list_owner(after=after, limit=limit)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post("/control")
    async def control(body: SkillControl):
        try:
            return await apply_skill_control(runtime, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post("/{skill_id:path}/invoke", status_code=201)
    async def invoke(skill_id: str, body: TaskRequest):
        try:
            return await runtime.submit(body, actor="owner", explicit_skill=skill_id)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc

    return routes
