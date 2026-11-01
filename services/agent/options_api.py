"""Owner-only Options metadata and revision-bound native settings."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from core.extensions import HarnessError
from core.policy import PolicyDenied
from packages.contracts.tool_permissions import ToolPermissionChange
from services.agent.model_settings import ModelSettingsChange, ModelSettingsConflict
from services.agent.runtime_settings import (
    RuntimeSettingsChange,
    RuntimeSettingsConflict,
    execution_metadata,
)
from services.agent.tool_permissions import ToolPermissionConflict


def router(runtime, auth):
    routes = APIRouter(prefix="/v1/agent/options", dependencies=[Depends(auth.require("owner"))])

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
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(503, "Agent settings are unavailable") from exc
        return {
            "schema_version": "1.0",
            "models": models,
            "runtime_settings": settings,
            "execution": execution,
            "provider": runtime.model_settings.default_info()
            if runtime.model_settings is not None
            else runtime.provider.info.model_dump(),
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
    async def change_models(request: ModelSettingsChange):
        if runtime.model_settings is None:
            raise HTTPException(503, "Agent model settings are not enabled on this host")
        async with runtime.integration_lock:
            try:
                if (
                    request.harness_version is not None
                    and runtime.model_settings.receipt(request.idempotency_key) is None
                ):
                    runtime.session_harness(request.harness_version)
                receipt = runtime.model_settings.change(request)
                # Legacy health and /model inspection report the Agent default.
                # Accepted tasks use their own provider object and native pin.
                try:
                    runtime.provider = runtime.model_settings.selection(None)[0]
                except ValueError:
                    # A removed default profile stays visibly unavailable; an
                    # independent valid harness override can still be saved.
                    pass
                return receipt
            except ModelSettingsConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except (HarnessError, OSError, ValueError, RuntimeError) as exc:
                raise HTTPException(
                    400, "Model change refused; refresh the available profiles"
                ) from exc
            except sqlite3.Error as exc:
                raise HTTPException(
                    503, "Model change outcome is unconfirmed; inspect the exact receipt"
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
        except (HarnessError, OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(409, "Harness model selection is unavailable") from exc

    @routes.get("/model-changes/{key}")
    async def model_change_receipt(key: str):
        if len(key) > 128 or runtime.model_settings is None:
            raise HTTPException(404, "Model change receipt is unavailable")
        receipt = runtime.model_settings.receipt(key)
        if receipt is None:
            raise HTTPException(404, "Model change has no committed receipt yet")
        return receipt

    return routes
