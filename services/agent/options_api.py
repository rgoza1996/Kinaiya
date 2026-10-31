"""Owner-only Options metadata and revision-bound model defaults."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core.extensions import HarnessError
from services.agent.model_settings import ModelSettingsChange, ModelSettingsConflict


def router(runtime, auth):
    routes = APIRouter(prefix="/v1/agent/options", dependencies=[Depends(auth.require("owner"))])

    @routes.get("")
    async def inspect_options():
        try:
            models = (
                runtime.model_settings.snapshot() if runtime.model_settings is not None else None
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(503, "Agent model settings are unavailable") from exc
        return {
            "schema_version": "1.0",
            "models": models,
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
        except (HarnessError, OSError, ValueError, RuntimeError) as exc:
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
