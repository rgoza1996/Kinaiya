"""Owner control plane; candidates have no route or credential to activation."""

import asyncio
from functools import wraps

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from core.extensions import HarnessError
from core.policy import PolicyDenied
from packages.contracts.models import WireModel


class Registration(WireModel):
    path: str = Field(min_length=1, max_length=1024)
    manifest: dict = Field(default_factory=dict)


class Version(WireModel):
    version_id: str = Field(pattern=r"^v-[a-f0-9]{24}$")


class Promotion(Version):
    report_id: str = Field(pattern=r"^rep-[a-f0-9]{16}$")
    reviewed: bool = False


def router(runtime, auth):
    routes = APIRouter(prefix="/v1/harness", dependencies=[Depends(auth.require("owner"))])
    gate = asyncio.Semaphore(1)

    def owned(handler):
        @wraps(handler)
        async def operation(*args, **kwargs):
            return await runtime.owned_workspace_operation(
                lambda: handler(*args, **kwargs),
                require_root=handler.__name__ in {"register", "evaluate"},
            )

        return operation

    def configured():
        if runtime.selector_registry is None:
            raise HTTPException(503, "Configure an owner-held suite and protected registry first")
        return runtime.selector_registry

    @routes.get("")
    async def status():
        registry = runtime.selector_registry
        return {
            "schema_version": "1.0",
            "configured": registry is not None,
            "seed_runtime": "seed-v1",
            "contract": "context_selector.v1",
            "active": registry.active if registry else None,
            "versions": registry.versions() if registry else [],
        }

    @routes.post("/register")
    @owned
    async def register(request: Registration):
        registry = configured()
        try:
            raw = runtime.policy.read_bytes(request.path, maximum=65_537)
            version = await asyncio.to_thread(
                registry.register_bytes, raw, request.manifest or None
            )
            return {"schema_version": "1.0", "version_id": version, "active": registry.active}
        except (HarnessError, PolicyDenied, OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post("/evaluate")
    @owned
    async def evaluate(request: Version):
        registry = configured()
        async with gate:
            try:
                report = await asyncio.to_thread(registry.evaluate, request.version_id)
                return report.model_dump()
            except (HarnessError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc

    @routes.post("/promote")
    @owned
    async def promote(request: Promotion):
        registry = configured()
        try:
            version = await asyncio.to_thread(
                registry.promote,
                request.version_id,
                request.report_id,
                actor="owner",
                reviewed=request.reviewed,
            )
            return {"schema_version": "1.0", "active": version}
        except (HarnessError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @routes.post("/rollback")
    @owned
    async def rollback():
        registry = configured()
        try:
            return {
                "schema_version": "1.0",
                "active": await asyncio.to_thread(registry.rollback, actor="owner"),
            }
        except HarnessError as exc:
            raise HTTPException(409, str(exc)) from exc

    return routes
