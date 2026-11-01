"""Owner-only ordered auxiliary route configuration and exact readback."""

import re
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from core.extensions import HarnessError
from packages.contracts.auxiliary_routes import AuxiliaryRouteChange, AuxiliaryRouteScope
from packages.contracts.models import AuxiliaryTask
from services.agent.auxiliary_routes import AuxiliaryRouteConflict, AuxiliaryRouteUnavailable


def router(runtime, auth, route_class):
    routes = APIRouter(dependencies=[Depends(auth.require("owner"))], route_class=route_class)

    def store():
        models = runtime.model_settings
        if models is None or models.auxiliary_routes is None:
            raise HTTPException(503, "Auxiliary route configuration is unavailable")
        return models.auxiliary_routes

    @routes.get("/auxiliary-routes")
    async def inspect_routes(
        purpose: Annotated[AuxiliaryTask, Query()] = "analysis",
        harness_version: str | None = Query(default=None, pattern=r"^(seed-v1|v-[a-f0-9]{24})$"),
    ):
        try:
            return store().snapshot(
                AuxiliaryRouteScope(purpose=purpose, harness_version=harness_version)
            )
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Auxiliary route configuration is unavailable") from exc

    @routes.post("/auxiliary-routes")
    async def change_route(request: AuxiliaryRouteChange):
        async with runtime.integration_lock:
            try:
                return store().change(request, validate_harness=runtime.session_harness)
            except AuxiliaryRouteConflict as exc:
                raise HTTPException(
                    409, "Auxiliary route state changed; inspect its exact receipt"
                ) from exc
            except (AuxiliaryRouteUnavailable, OSError, sqlite3.Error) as exc:
                raise HTTPException(503, "Auxiliary route save outcome is unconfirmed") from exc
            except (HarnessError, ValueError, RuntimeError) as exc:
                raise HTTPException(400, "Auxiliary route change was refused") from exc

    @routes.post("/auxiliary-route-changes/lookup")
    async def lookup_route(request: AuxiliaryRouteChange):
        async with runtime.integration_lock:
            try:
                return store().lookup(request)
            except AuxiliaryRouteConflict as exc:
                raise HTTPException(409, "Auxiliary route request identity changed") from exc
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
                raise HTTPException(503, "Auxiliary route receipt is unavailable") from exc

    @routes.get("/auxiliary-route-changes/{key}")
    async def route_receipt(key: str):
        if re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", key) is None:
            raise HTTPException(404, "Auxiliary route receipt is unavailable")
        try:
            receipt = store().receipt(key)
        except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
            raise HTTPException(503, "Auxiliary route receipt is unavailable") from exc
        if receipt is None:
            raise HTTPException(404, "No committed auxiliary route receipt is visible")
        return receipt

    return routes
