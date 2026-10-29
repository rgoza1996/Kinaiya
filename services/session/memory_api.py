"""Opt-in owner administration and scoped read-only Converse recall bridge."""

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
from packages.contracts.memory_catalog import MemoryCatalogResult
from packages.contracts.memory_context import (
    MemoryContextValidate,
    MemoryContextValidation,
    MemoryRecallRequest,
    MemoryRecallResult,
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
        store.register_converse_context_validator("korina-dag-v1", memory.validate_publication)
    except (MemoryError, ValueError):
        # Optional memory storage cannot take the ordinary journal offline.
        memory = None
    app.state.converse_memory = memory

    def backend():
        if memory is None:
            raise MemoryError(503, "Converse memory is unavailable")
        return memory

    class MemoryRoute(APIRoute):
        roles = ("owner",)
        response_limit = RESPONSE_LIMIT
        bridge = False

        def get_route_handler(self):
            original = super().get_route_handler()
            authorize = auth.require(*self.roles)

            async def handle(request: Request):
                try:
                    if len(request.headers.getlist("authorization")) > 1:
                        raise HTTPException(401, "Ambiguous bearer header")
                    if self.bridge and len(request.headers.getlist("origin")) > 1:
                        raise HTTPException(403, "Ambiguous browser origin")
                    await authorize(request, request.headers.get("authorization", ""))
                    if request.query_params:
                        raise HTTPException(422, "Memory routes do not accept query parameters")
                    if self.bridge:
                        for header in ("content-type", "content-length", "content-encoding"):
                            if len(request.headers.getlist(header)) > 1:
                                raise HTTPException(422, "Ambiguous memory request header")
                        if request.headers.get(
                            "content-encoding", "identity"
                        ).strip().lower() not in {"", "identity"}:
                            raise HTTPException(415, "Memory requests require uncompressed JSON")
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
                    if len(response.body) > self.response_limit:
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
                except Exception:
                    if not self.bridge:
                        raise
                    status, detail = 503, "Memory context is unavailable"
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

    class CatalogRoute(MemoryRoute):
        bridge = True

    def catalog(request: MemoryBinding):
        return backend().catalog(request)

    router.add_api_route(
        "/catalog",
        catalog,
        methods=["POST"],
        response_model=MemoryCatalogResult,
        route_class_override=CatalogRoute,
    )

    app.include_router(router)

    class RecallRoute(MemoryRoute):
        roles = ("owner", "converse")
        response_limit = 17 * 1024
        bridge = True

    class ValidateRoute(RecallRoute):
        response_limit = 4 * 1024

    def recall(request: MemoryRecallRequest):
        return backend().recall(request)

    def validate_context(request: MemoryContextValidate):
        return backend().validate_context(request)

    bridge = APIRouter(
        prefix="/v1/converse-memory",
        dependencies=[Depends(auth.require("owner", "converse"))],
    )
    for path, endpoint, model, route in (
        ("/recall", recall, MemoryRecallResult, RecallRoute),
        ("/validate", validate_context, MemoryContextValidation, ValidateRoute),
    ):
        bridge.add_api_route(
            path, endpoint, methods=["POST"], response_model=model, route_class_override=route
        )
    app.include_router(bridge)
