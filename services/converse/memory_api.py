"""Owner-only, bounded process-local Converse memory controls."""

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from packages.contracts.memory import MemoryBinding
from packages.contracts.memory_context import MemoryUseConfigure, MemoryUseState


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("Invalid number")


def install_memory_controls(app, runtime, auth):
    authorize = auth.require("owner")

    class MemoryRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def handle(request: Request):
                try:
                    if any(
                        len(request.headers.getlist(name)) > 1
                        for name in ("authorization", "origin")
                    ):
                        raise HTTPException(401, "Ambiguous control headers")
                    await authorize(request, request.headers.get("authorization", ""))
                    if any(
                        len(request.headers.getlist(name)) > 1
                        for name in ("content-type", "content-length", "content-encoding")
                    ):
                        raise HTTPException(422, "Ambiguous memory control headers")
                    if request.headers.get("content-encoding", "identity").strip().lower() not in {
                        "",
                        "identity",
                    }:
                        raise HTTPException(415, "Memory controls require uncompressed JSON")
                    if request.query_params:
                        raise HTTPException(422, "Memory controls accept no query")
                    if (
                        request.headers.get("content-type", "").split(";")[0].strip()
                        != "application/json"
                    ):
                        raise HTTPException(415, "Memory controls require JSON")
                    raw = bytearray()
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > 16 * 1024:
                            raise HTTPException(413, "Memory control exceeds its bound")
                        raw.extend(chunk)
                    request._body = bytes(raw)
                    try:
                        decoded = raw.decode("utf-8")
                        request._json = json.loads(
                            decoded, object_pairs_hook=_pairs, parse_constant=_constant
                        )
                        # json.loads accepts escaped lone surrogates; the wire does not.
                        json.dumps(request._json, ensure_ascii=False).encode("utf-8")
                    except (ValueError, RecursionError):
                        raise HTTPException(422, "Invalid memory control JSON") from None
                    response = await original(request)
                    if len(response.body) > 4096:
                        raise HTTPException(503, "Memory control response exceeds its bound")
                    response.headers["Cache-Control"] = "no-store"
                    return response
                except HTTPException as exc:
                    status, detail = exc.status_code, exc.detail
                except RequestValidationError:
                    status, detail = 422, "Invalid memory control"
                except (ValueError, KeyError):
                    status, detail = 409, "Memory control scope or generation changed"
                except Exception:
                    status, detail = 503, "Memory control is unavailable"
                return JSONResponse(
                    {"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"}
                )

            return handle

    router = APIRouter(prefix="/v1/memory", route_class=MemoryRoute)

    @router.post("/state", response_model=MemoryUseState)
    async def state(request: MemoryBinding):
        return await runtime.memory_state(request)

    @router.post("/use", response_model=MemoryUseState)
    async def use(request: MemoryUseConfigure):
        return await runtime.memory_use(request)

    app.include_router(router)
