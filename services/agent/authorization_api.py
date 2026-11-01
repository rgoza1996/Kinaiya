"""Bounded owner-only redacted one-use authorization activity."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from packages.contracts.agent_submit import agent_submit_json
from packages.contracts.authorizations import AuthorizationRevoke
from services.agent.authorizations import AuthorizationConflict


def router(runtime, auth, route_class):
    routes = APIRouter(
        prefix="/authorizations",
        dependencies=[Depends(auth.require("owner"))],
        route_class=route_class,
    )

    async def body(request):
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 8192:
                        raise HTTPException(413, "Authorization identity exceeds its bound")
                    raw.extend(chunk)
            return AuthorizationRevoke.model_validate(agent_submit_json(bytes(raw), maximum=8192))
        except TimeoutError:
            raise HTTPException(408, "Authorization body deadline expired") from None
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid exact authorization request") from None

    @routes.get("")
    async def inventory(
        after: str | None = Query(
            default=None, min_length=20, max_length=20, pattern=r"^apr_[a-f0-9]{16}$"
        ),
        limit: int = Query(default=50, ge=1, le=50),
    ):
        try:
            return runtime.authorizations.snapshot(after, limit)
        except (ValueError, RuntimeError):
            raise HTTPException(503, "Authorization inventory is unavailable") from None

    async def run(invoke):
        owner = runtime.authorizations
        if runtime._closed or len(owner.tasks) >= owner.MAX_PUBLICATIONS:
            raise HTTPException(503, "Authorization metadata capacity is unavailable")
        task = asyncio.create_task(invoke())
        owner.tasks.add(task)
        runtime._retain_workspace_worker(task)
        task.add_done_callback(owner.tasks.discard)
        try:
            done, _ = await asyncio.wait({task}, timeout=5)
            if not done:
                raise HTTPException(503, "Authorization outcome is unconfirmed")
            return task.result()
        except AuthorizationConflict:
            raise HTTPException(409, "Original authorization changed or is unavailable") from None
        except (ValueError, RuntimeError):
            raise HTTPException(503, "Exact authorization outcome is unavailable") from None

    @routes.post("/revoke")
    async def revoke(request: Request):
        original = await body(request)
        return await run(lambda: runtime.authorizations.revoke(original))

    @routes.post("/revocations/lookup")
    async def lookup(request: Request):
        original = await body(request)
        return await run(lambda: runtime.authorizations.lookup(original))

    return routes
