"""Owner-only Options metadata and revision-bound native settings."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from core.extensions import HarnessError
from core.policy import PolicyDenied
from packages.contracts.tool_permissions import ToolPermissionChange, ToolPermissionTargetChange
from packages.contracts.workspace_settings import WorkspaceChange
from services.agent.model_settings import (
    BoundModelSettingsChange,
    ModelSettingsChange,
    ModelSettingsConflict,
    ModelSettingsUnavailable,
)
from services.agent.runtime_settings import (
    RuntimeSettingsChange,
    RuntimeSettingsConflict,
    execution_metadata,
)
from services.agent.tool_permissions import ToolPermissionConflict
from services.agent.workspace_settings import WorkspaceConflict


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
    from services.agent.auxiliary_routes_api import router as auxiliary_routes_router

    routes = APIRouter(
        prefix="/v1/agent/options",
        dependencies=[Depends(auth.require("owner"))],
        route_class=_NoStoreRoute,
    )

    def workspace_settings():
        if runtime.workspace_settings is None:
            raise HTTPException(503, "Native workspace selection is unavailable")
        return runtime.workspace_settings

    @routes.get("/workspace-settings")
    async def inspect_workspace_settings():
        try:
            return workspace_settings().snapshot()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Native workspace authority is unavailable") from exc

    @routes.post("/workspace-settings")
    async def change_workspace_settings(request: WorkspaceChange):
        async with runtime.integration_lock:
            try:
                return await workspace_settings().change(request)
            except WorkspaceConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (PolicyDenied, ValueError) as exc:
                raise HTTPException(
                    400, "Workspace selection refused; inspect its paths and native owners"
                ) from exc
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                raise HTTPException(
                    503, "Workspace outcome is unconfirmed; inspect the exact request"
                ) from exc

    @routes.get("/workspace-changes/{key}")
    async def workspace_change_receipt(key: str):
        try:
            receipt = workspace_settings().receipt(key)
            if receipt is None:
                raise HTTPException(404, "No exact committed workspace receipt is visible")
            return receipt
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Workspace receipt is unavailable") from exc

    @routes.post("/workspace-changes/lookup")
    async def lookup_workspace_change(request: WorkspaceChange):
        try:
            return workspace_settings().lookup(request)
        except WorkspaceConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Exact workspace outcome is unavailable") from exc

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
        except (OSError, ValueError, ModelSettingsUnavailable, sqlite3.Error) as exc:
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
    async def change_runtime(request: RuntimeSettingsChange, response: Response):
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

    def tool_permissions():
        if runtime.tool_permissions is None:
            raise HTTPException(503, "Scoped native tool permissions are unavailable")
        return runtime.tool_permissions

    @routes.get("/tool-permissions")
    async def inspect_tool_permissions(
        response: Response,
        scope: str = Query(default="default", pattern=r"^(default|workspace|session|task)$"),
        scope_id: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
        session_id: str | None = Query(default=None, min_length=1, max_length=128),
        task_id: str | None = Query(default=None, min_length=1, max_length=128),
    ):
        response.headers["Cache-Control"] = "no-store"
        if scope_id is not None and (session_id is not None or task_id is not None):
            raise HTTPException(400, "Choose one native permission scope")
        try:
            return await tool_permissions().select(scope, session_id, task_id, scope_id)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, "Native permission scope is invalid or unavailable") from exc
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Native permission state is unavailable") from exc

    @routes.get("/tool-permission-scopes")
    async def saved_tool_permission_scopes(
        response: Response,
        after: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
        limit: int = Query(default=20, ge=1, le=20),
    ):
        response.headers["Cache-Control"] = "no-store"
        try:
            return await tool_permissions().page_saved(after, limit)
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Saved permission scopes are unavailable") from exc

    @routes.get("/tool-permission-targets")
    async def inspect_permission_targets(
        scope: str = Query(default="default", pattern=r"^(default|workspace|session|task)$"),
        scope_id: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
        session_id: str | None = Query(default=None, min_length=1, max_length=128),
        task_id: str | None = Query(default=None, min_length=1, max_length=128),
        after: str | None = Query(default=None, pattern=r"^[a-f0-9]{64}$"),
        limit: int = Query(default=20, ge=1, le=20),
    ):
        if scope_id is not None and (session_id is not None or task_id is not None):
            raise HTTPException(400, "Choose one native permission scope")
        try:
            return await tool_permissions().select_targets(
                scope, session_id, task_id, scope_id, after, limit
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, "Permission target scope or cursor is unavailable") from exc
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Permission target inventory is unavailable") from exc

    @routes.post("/tool-permission-targets")
    async def change_permission_target(request: ToolPermissionTargetChange):
        try:
            return await tool_permissions().change_target(request)
        except ToolPermissionConflict as exc:
            raise HTTPException(
                409, "Permission target changed; inspect its exact receipt"
            ) from exc
        except ValueError as exc:
            raise HTTPException(400, "Permission target change was refused") from exc
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Permission target save outcome is unconfirmed") from exc

    @routes.post("/tool-permissions")
    async def change_tool_permission(request: ToolPermissionChange, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            return await tool_permissions().change(request)
        except ToolPermissionConflict as exc:
            raise HTTPException(409, "Permission state changed; inspect the exact receipt") from exc
        except ValueError as exc:
            raise HTTPException(400, "Native permission change was refused") from exc
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Permission save outcome is unconfirmed") from exc

    @routes.get("/tool-permission-changes/{key}")
    async def tool_permission_receipt(key: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not 1 <= len(key) <= 128:
            raise HTTPException(404, "Permission receipt is unavailable")
        try:
            result = tool_permissions().receipt(key)
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(503, "Permission receipt is unavailable") from exc
        if result is None:
            raise HTTPException(404, "No committed permission receipt is visible")
        return result

    @routes.get("/runtime-changes/{key}")
    async def runtime_change_receipt(key: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if len(key) > 128 or runtime.runtime_settings is None:
            raise HTTPException(404, "Execution settings receipt is unavailable")
        try:
            receipt = runtime.runtime_settings.receipt(key)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(503, "Execution settings receipt is unavailable") from exc
        if receipt is None:
            raise HTTPException(404, "No committed execution settings receipt is visible")
        return receipt

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
                receipt, _applied = runtime.model_settings.change_with_status(
                    request, validate_harness=runtime.session_harness
                )
                # Metadata reads the current selection without constructing an
                # adapter; each accepted task captures its actual provider lease.
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
            info, pin = runtime.model_settings.selection_info(version)
            return {
                "schema_version": "1.0",
                "harness_version": version,
                "provider": info.model_dump(),
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

    routes.include_router(auxiliary_routes_router(runtime, auth, _NoStoreRoute))
    return routes
