"""Owner-only Options metadata and revision-bound native settings."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from core.extensions import HarnessError
from core.policy import PolicyDenied
from services.agent.model_settings import (
    BoundModelSettingsChange,
    ModelSettingsChange,
    ModelSettingsConflict,
    ModelSettingsUnavailable,
)
from services.agent.runtime_settings import (
    BoundRuntimeSettingsChange,
    RuntimeSettingsChange,
    RuntimeSettingsConflict,
    RuntimeSettingsUnavailable,
    execution_metadata,
)


class _NoStoreRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request):
            try:
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


def router(runtime, auth):
    routes = APIRouter(
        prefix="/v1/agent/options",
        dependencies=[Depends(auth.require("owner"))],
        route_class=_NoStoreRoute,
    )

    @routes.get("")
    async def inspect_options(response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            models = (
                runtime.model_settings.snapshot() if runtime.model_settings is not None else None
            )
            settings = (
                runtime.runtime_settings.snapshot()
                if runtime.runtime_settings is not None
                else None
            )
            execution = execution_metadata(runtime)
            provider = (
                runtime.model_settings.default_info()
                if runtime.model_settings is not None
                else runtime.provider.info.model_dump()
            )
        except (
            OSError,
            ValueError,
            ModelSettingsUnavailable,
            RuntimeSettingsUnavailable,
            sqlite3.Error,
        ) as exc:
            raise HTTPException(503, "Agent settings are unavailable") from exc
        return {
            "schema_version": "1.0",
            "models": models,
            "runtime_settings": settings,
            "execution": execution,
            "provider": provider,
            "permissions": {
                key: getattr(runtime.policy, key)
                for key in ("allow_write", "allow_shell", "network", "yolo")
            },
            "max_steps": runtime.max_steps,
            "tools": list(runtime.tool_snapshot().available_tools),
        }

    @routes.post("/runtime")
    async def change_runtime(
        request: Annotated[
            RuntimeSettingsChange | BoundRuntimeSettingsChange,
            Body(discriminator="schema_version"),
        ],
        response: Response,
    ):
        response.headers["Cache-Control"] = "no-store"
        if runtime.runtime_settings is None:
            raise HTTPException(503, "Execution settings are not enabled on this host")
        async with runtime.integration_lock:
            try:
                return await runtime.runtime_settings.change(request)
            except RuntimeSettingsConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (PolicyDenied, ValueError) as exc:
                raise HTTPException(
                    400, "Execution settings refused; check the workspace and bounds"
                ) from exc
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                raise HTTPException(
                    503, "Execution settings outcome is unconfirmed; inspect the exact receipt"
                ) from exc

    @routes.get("/runtime-changes/{key}")
    async def runtime_change_receipt(key: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if len(key) > 128 or runtime.runtime_settings is None:
            raise HTTPException(404, "Execution settings receipt is unavailable")
        try:
            receipt = runtime.runtime_settings.receipt(key)
        except (RuntimeSettingsUnavailable, OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(503, "Execution settings receipt is unavailable") from exc
        if receipt is None:
            raise HTTPException(404, "No committed execution settings receipt is visible")
        return receipt

    @routes.post("/runtime-changes/lookup")
    async def runtime_change_lookup(
        request: Annotated[
            RuntimeSettingsChange | BoundRuntimeSettingsChange,
            Body(discriminator="schema_version"),
        ],
    ):
        if runtime.runtime_settings is None:
            raise HTTPException(503, "Execution settings are not enabled on this host")
        async with runtime.integration_lock:
            try:
                return runtime.runtime_settings.lookup(request)
            except RuntimeSettingsConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (RuntimeSettingsUnavailable, OSError, ValueError, sqlite3.Error) as exc:
                raise HTTPException(
                    503, "Execution settings outcome is unconfirmed; inspect the exact receipt"
                ) from exc

    @routes.post("/models")
    async def change_models(
        request: Annotated[
            ModelSettingsChange | BoundModelSettingsChange, Body(discriminator="schema_version")
        ],
    ):
        if runtime.model_settings is None:
            raise HTTPException(503, "Agent model settings are not enabled on this host")
        async with runtime.integration_lock:
            try:
                receipt, applied = runtime.model_settings.change_with_status(
                    request, validate_harness=runtime.session_harness
                )
                # Legacy health and /model inspection report the Agent default.
                # Accepted tasks use their own provider object and native pin.
                if applied:
                    try:
                        runtime.provider = runtime.model_settings.selection(None)[0]
                    except ValueError:
                        # A removed default profile stays visibly unavailable; an
                        # independent valid harness override can still be saved.
                        pass
                return receipt
            except ModelSettingsConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (ModelSettingsUnavailable, OSError, sqlite3.Error) as exc:
                raise HTTPException(
                    503, "Model settings outcome is unconfirmed; inspect the exact receipt"
                ) from exc
            except (HarnessError, ValueError, RuntimeError) as exc:
                raise HTTPException(
                    400, "Model change refused; refresh the available profiles"
                ) from exc

    @routes.post("/model-changes/lookup")
    async def model_change_lookup(request: BoundModelSettingsChange):
        if runtime.model_settings is None:
            raise HTTPException(503, "Agent model settings are not enabled on this host")
        async with runtime.integration_lock:
            try:
                return runtime.model_settings.lookup(request)
            except ModelSettingsConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (ModelSettingsUnavailable, OSError, ValueError, sqlite3.Error) as exc:
                raise HTTPException(
                    503, "Model settings outcome is unconfirmed; inspect the exact receipt"
                ) from exc

    @routes.get("/model-selection")
    async def model_selection(
        harness_version: str | None = Query(default=None, pattern=r"^(seed-v1|v-[a-f0-9]{24})$"),
    ):
        try:
            version = runtime.session_harness(harness_version)
            if runtime.model_settings is None:
                return {
                    "schema_version": "1.0",
                    "harness_version": version,
                    "provider": runtime.provider.info.model_dump(),
                    "pin": None,
                }
            provider, pin = runtime.model_settings.selection(version)
            return {
                "schema_version": "1.0",
                "harness_version": version,
                "provider": provider.info.model_dump(),
                "pin": pin.model_dump(),
            }
        except (ModelSettingsUnavailable, sqlite3.Error) as exc:
            raise HTTPException(503, "Agent settings are unavailable") from exc
        except (HarnessError, OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, "Harness model selection is unavailable") from exc

    @routes.get("/model-changes/{key}")
    async def model_change_receipt(key: str):
        if len(key) > 128 or runtime.model_settings is None:
            raise HTTPException(404, "Model change receipt is unavailable")
        try:
            receipt = runtime.model_settings.receipt(key)
        except (ModelSettingsUnavailable, OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(
                503, "Model settings outcome is unconfirmed; inspect the exact receipt"
            ) from exc
        if receipt is None:
            raise HTTPException(404, "Model change has no committed receipt yet")
        return receipt

    return routes
