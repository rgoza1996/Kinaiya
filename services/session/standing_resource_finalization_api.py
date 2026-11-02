"""Bounded new-family transport; old standing wire limits stay unchanged."""

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceBindingRequest,
    StandingResourceFinalization,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    checked_finalization,
    finalization_bytes,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.session.standing_work_api import (
    STANDING_INGRESS_SECONDS,
    StandingOwnerOperations,
)

_INVALID = "Invalid standing resource finalization request"
_BODIES = {
    "bind": StandingResourceBindingRequest,
    "binding_lookup": StandingResourceBindingRequest,
    "context": StandingResourceFinalizeRequest,
    "report": StandingResourceFinalization,
    "lookup": StandingResourceFinalizeRequest,
}
_RESULTS = {
    "bind": StandingResourceBindingReceipt,
    "binding_lookup": StandingResourceBindingReceipt,
    "context": StandingResourceFinalizeContext,
    "report": StandingResourceFinalizationReceipt,
    "lookup": StandingResourceFinalizationReceipt,
}


class StandingResourceFinalizationOperations(StandingOwnerOperations):
    """Reuse actual ingress/thread ownership, with isolated packet bounds."""

    def __init__(self, owner, *, shared=None):
        super().__init__(owner)
        self._shared = shared
        if shared is not None:
            self._operations = shared._operations

    async def run(self, request, action, argument=None):
        if self._shared is not None and self._shared._closed:
            raise StandingUnavailable
        return await super().run(request, action, argument)

    async def close(self):
        if self._shared is not None:
            self._shared._closed = True
        await super().close()

    async def _body(self, request, event, deadline, started, *, kind):
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Standing resource finalization requires identity JSON")
        ingress_deadline = min(deadline, started + STANDING_INGRESS_SECONDS)
        remaining = ingress_deadline - time.monotonic()
        if remaining <= 0:
            event.set()
            raise StandingUnavailable
        raw = bytearray()
        try:
            async with asyncio.timeout(remaining):
                async for chunk in request.stream():
                    self._guard(event, ingress_deadline)
                    if type(chunk) is not bytes or len(raw) + len(chunk) > kind.wire_bytes:
                        raise HTTPException(422, _INVALID)
                    raw.extend(chunk)
            self._guard(event, ingress_deadline)
        except TimeoutError:
            event.set()
            raise StandingUnavailable from None
        try:
            return kind.from_json(bytes(raw))
        except ValueError:
            raise HTTPException(422, _INVALID) from None

    def _call(self, action, argument, query, event, deadline, *, capability=None):
        self._guard(event, deadline)
        try:
            if capability is not None or action not in _BODIES or query:
                raise StandingUnavailable
            argument = checked_finalization(argument, _BODIES[action])
            method = getattr(self.owner, action)
            result = (
                method(argument, deadline=deadline, cancelled=event.is_set)
                if action in {"bind", "report"}
                else method(argument)
            )
            self._guard(event, deadline)
            if result is None and action in {"binding_lookup", "lookup"}:
                return b"null"
            value = checked_finalization(result, _RESULTS[action])
            original = argument.context.request if action == "report" else argument
            value = value.validate_request(original)
            if action == "report" and finalization_bytes(value.finalization) != finalization_bytes(
                argument
            ):
                raise StandingUnavailable
            raw = finalization_bytes(value)
            if len(raw) > _RESULTS[action].wire_bytes:
                raise StandingUnavailable
            self._guard(event, deadline)
            return raw
        except (StandingConflict, StandingUnavailable):
            raise
        except Exception:
            raise StandingUnavailable from None

    async def _execute(
        self, request, action, argument, event, deadline, started, ingress_done, disconnected
    ):
        monitor = None
        try:
            query = self._query(request, action)
            if action not in _BODIES or any(
                key.lower() == b"x-kinaiya-standing-capability"
                for key, _ in request.scope["headers"]
            ):
                raise HTTPException(422, _INVALID)
            argument = await self._body(request, event, deadline, started, kind=_BODIES[action])
            self._guard(event, min(deadline, started + STANDING_INGRESS_SECONDS))
            ingress_done.set()
            task = asyncio.create_task(
                asyncio.to_thread(self._call, action, argument, query, event, deadline)
            )
            monitor = asyncio.create_task(self._disconnect(request, event, disconnected))
            return await self._thread_result(task, event)
        except (HTTPException, StandingConflict, StandingUnavailable):
            raise
        except BaseException as error:
            event.set()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise StandingUnavailable from None
        finally:
            ingress_done.set()
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)


def install_standing_resource_finalization_routes(app, owner, auth, *, shared=None):
    operations = StandingResourceFinalizationOperations(owner, shared=shared)
    resources = APIRouter(
        prefix="/v1/standing/resources", dependencies=[Depends(auth.require("resources"))]
    )
    reads = APIRouter(
        prefix="/v1/standing/resources",
        dependencies=[Depends(auth.require("resources", "agent"))],
    )

    async def respond(request, action):
        try:
            raw = await operations.run(request, action)
        except StandingConflict:
            raise HTTPException(409, str(StandingConflict())) from None
        except StandingUnavailable:
            raise HTTPException(503, str(StandingUnavailable())) from None
        return Response(raw, media_type="application/json", headers={"Cache-Control": "no-store"})

    @resources.post("/binding")
    async def bind(request: Request):
        return await respond(request, "bind")

    @resources.post("/binding/lookup")
    async def binding_lookup(request: Request):
        return await respond(request, "binding_lookup")

    @resources.post("/finalization/context")
    async def context(request: Request):
        return await respond(request, "context")

    @resources.post("/finalization/report")
    async def report(request: Request):
        return await respond(request, "report")

    @reads.post("/finalization/lookup")
    async def lookup(request: Request):
        return await respond(request, "lookup")

    app.include_router(resources)
    app.include_router(reads)
    return operations
