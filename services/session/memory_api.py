"""Opt-in owner administration for local Converse memory; no prompt integration."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from packages.contracts.memory import (
    MemoryBinding,
    MemoryCapture,
    MemoryCaptureResult,
    MemoryConfigure,
    MemoryForget,
    MemoryForgetResult,
    MemoryInspect,
    MemoryQuery,
    MemoryQueryResult,
    MemoryState,
)
from services.session.memory import MemoryError, MemoryStore

BODY_LIMIT = 16 * 1024
RESPONSE_LIMIT = 256 * 1024


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("Non-finite JSON number")


def install_memory(app, store, auth):
    try:
        memory = MemoryStore(store)
    except MemoryError:
        # Optional memory storage cannot take the ordinary journal offline.
        memory = None
    app.state.converse_memory = memory
    authorize = auth.require("owner")

    def backend():
        if memory is None:
            raise MemoryError(503, "Converse memory is unavailable")
        return memory

    class MemoryRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def handle(request: Request):
                try:
                    if len(request.headers.getlist("authorization")) > 1:
                        raise HTTPException(401, "Ambiguous bearer header")
                    await authorize(request, request.headers.get("authorization", ""))
                    if request.query_params:
                        raise HTTPException(422, "Memory routes do not accept query parameters")
                    raw = bytearray()
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > BODY_LIMIT:
                            raise HTTPException(413, "Memory request exceeds16KiB")
                        raw.extend(chunk)
                    request._body = bytes(raw)
                    if raw:
                        if (
                            request.headers.get("content-type", "").split(";")[0].strip()
                            != "application/json"
                        ):
                            raise HTTPException(415, "Memory requests require JSON")
                        try:
                            request._json = json.loads(
                                raw.decode("utf-8"),
                                object_pairs_hook=_pairs,
                                parse_constant=_constant,
                            )
                        except (ValueError, RecursionError):
                            raise HTTPException(422, "Invalid memory JSON") from None
                    response = await original(request)
                    if len(response.body) > RESPONSE_LIMIT:
                        raise HTTPException(503, "Memory response exceeds its bound")
                    response.headers["Cache-Control"] = "no-store"
                    return response
                except RequestValidationError:
                    status, detail = 422, "Invalid memory request"
                except MemoryError as exc:
                    status, detail = exc.status, str(exc)
                except KeyError:
                    status, detail = 404, "Unknown memory reference"
                except HTTPException as exc:
                    status, detail = exc.status_code, exc.detail
                return JSONResponse(
                    status_code=status,
                    content={"detail": detail},
                    headers={"Cache-Control": "no-store"},
                )

            return handle

    router = APIRouter(
        prefix="/v1/converse-memory",
        route_class=MemoryRoute,
        dependencies=[Depends(auth.require("owner"))],
    )

    @router.post("/state", response_model=MemoryState)
    def state(request: MemoryBinding):
        return backend().state(request)

    @router.post("/configure", response_model=MemoryState)
    def configure(request: MemoryConfigure):
        return backend().configure(request)

    @router.post("/capture", response_model=MemoryCaptureResult)
    def capture(request: MemoryCapture):
        return backend().capture(request)

    @router.post("/inspect", response_model=MemoryCaptureResult)
    def inspect(request: MemoryInspect):
        return backend().inspect(request)

    @router.post("/query", response_model=MemoryQueryResult)
    def query(request: MemoryQuery):
        return backend().query(request)

    @router.post("/forget", response_model=MemoryForgetResult)
    def forget(request: MemoryForget):
        return backend().forget(request)

    app.include_router(router)
