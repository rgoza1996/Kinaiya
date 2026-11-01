"""Owner-only lifecycle and direct non-LLM invocation of compute plugins."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from packages.contracts.models import PluginControl


def require_plugins(runtime):
    if runtime.plugin_service is None:
        raise ValueError("No compute-plugin host configured")
    return runtime.plugin_service


async def apply_plugin_control(runtime, control: PluginControl):
    return await runtime.owned_workspace_operation(
        lambda: _apply_plugin_control(runtime, control),
        require_root=control.operation in {"register", "evaluate", "run"},
    )


async def _apply_plugin_control(runtime, control):
    service = require_plugins(runtime)
    return await service.owner_control(
        control.idempotency_key,
        control.model_dump(mode="json"),
        lambda: _apply(runtime, service, control),
    )


async def _apply(runtime, service, control):
    registry = service.registry
    op = control.operation
    fields = control.model_dump(exclude_none=True)
    allowed = {
        "register": {"path", "manifest"},
        "inspect": {"version_id"},
        "evaluate": {"version_id"},
        "promote": {"version_id", "report_id", "reviewed"},
        "rollback": {"plugin_id", "reviewed"},
        "disable": {"plugin_id"},
        "remove": {"version_id"},
        "scope": {"version_id", "content_scope", "reviewed"},
        "run": {"version_id", "arguments"},
    }[op]
    supplied = set(fields) - {"schema_version", "operation", "idempotency_key"}
    if control.reviewed is False:
        supplied.discard("reviewed")
    if supplied - allowed or not (allowed - {"reviewed"}) <= supplied:
        raise ValueError(f"{op} requires only {sorted(allowed)}")
    if op == "register":
        raw = runtime.policy.read_bytes(control.path, maximum=65_537)
        version_id = await asyncio.to_thread(registry.register_bytes, raw, control.manifest)
        return service.inspect(version_id)
    if op == "inspect":
        return service.inspect(control.version_id)
    if op == "evaluate":
        return await service.evaluate(control.version_id)
    if op == "promote":
        return {
            "active": await asyncio.to_thread(
                registry.promote,
                control.version_id,
                control.report_id,
                actor="owner",
                reviewed=control.reviewed,
            )
        }
    if op == "rollback":
        return {
            "active": await asyncio.to_thread(
                registry.rollback, control.plugin_id, actor="owner", reviewed=control.reviewed
            )
        }
    if op == "disable":
        await asyncio.to_thread(registry.disable, control.plugin_id, actor="owner")
        return {"plugin_id": control.plugin_id, "enabled": False}
    if op == "remove":
        if runtime.plugin_dependents(control.version_id):
            raise ValueError("A live or recoverable task still references this plugin version")
        await asyncio.to_thread(registry.remove, control.version_id, actor="owner")
        return {"removed": control.version_id}
    if op == "scope":
        return await service.set_scope(
            control.version_id, control.content_scope, reviewed=control.reviewed
        )
    return await service.run(control.version_id, control.arguments)


def router(runtime, auth):
    routes = APIRouter(prefix="/v1/plugins", dependencies=[Depends(auth.require("owner"))])

    @routes.get("")
    async def list_plugins(after: int = 0, limit: int = 8):
        try:
            return require_plugins(runtime).list_owner(after, limit)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post("/control")
    async def control(body: PluginControl):
        try:
            return await apply_plugin_control(runtime, body)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.get("/task-pins/{task_id}")
    async def task_pins(task_id: str):
        try:
            return {"task_id": task_id, "plugin_versions": runtime.plugin_pins(task_id)}
        except KeyError as exc:
            raise HTTPException(404, "Unknown task") from exc

    return routes
