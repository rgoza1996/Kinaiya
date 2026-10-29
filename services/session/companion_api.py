"""Foreground invitation routes with a separate companion bearer boundary."""

import ipaddress
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from packages.contracts.companion import (
    CallAction,
    CallEnd,
    CallNote,
    CallPage,
    CallRequest,
    CompanionCall,
    CompanionDevice,
    CompanionWire,
    DeviceIssued,
    DevicePage,
    PairingIssued,
    PairingRedeem,
    PairingRequest,
)
from packages.contracts.models import MAX_PLAYBACK_GENERATION
from services.session.companion import CompanionError, CompanionStore

BODY_LIMIT = 16 * 1024
# JSON escaping can double each permitted note's UTF-8 byte count.
RESPONSE_LIMIT = 256 * 1024
RouteID = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("Non-finite JSON number")


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def install_companion(app, store, auth, *, clock=None):
    companion = CompanionStore(store, **({"clock": clock} if clock is not None else {}))
    app.state.companion = companion

    class CompanionRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def handle(request: Request):
                try:
                    origin = request.headers.get("origin")
                    if origin and origin not in auth.allowed_origins:
                        raise HTTPException(403, "Browser origin is not permitted")
                    if len(request.headers.getlist("authorization")) > 1:
                        raise HTTPException(401, "Ambiguous bearer header")
                    # Use actual ASGI peer/scheme and Host, never forwarded headers.
                    if request.scope.get("scheme") != "https" and not (
                        request.client is not None
                        and _loopback(request.client.host)
                        and _loopback(request.url.hostname or "")
                    ):
                        raise HTTPException(403, "Companion control requires HTTPS or loopback")
                    allowed = {"after", "limit", "session_id", "device_id"}
                    if any(k not in allowed for k in request.query_params) or len(
                        request.query_params.multi_items()
                    ) != len(request.query_params):
                        raise HTTPException(422, "Unsupported companion query parameters")
                    raw = bytearray()
                    async for chunk in request.stream():
                        if len(raw) + len(chunk) > BODY_LIMIT:
                            raise HTTPException(413, "Companion request exceeds16KiB")
                        raw.extend(chunk)
                    request._body = bytes(raw)
                    if raw:
                        if (
                            request.headers.get("content-type", "").split(";")[0].strip()
                            != "application/json"
                        ):
                            raise HTTPException(415, "Companion requests require JSON")
                        try:
                            request._json = json.loads(
                                raw.decode("utf-8"),
                                object_pairs_hook=_pairs,
                                parse_constant=_constant,
                            )
                        except (ValueError, RecursionError):
                            raise HTTPException(422, "Invalid companion JSON") from None
                    response = await original(request)
                    if len(response.body) > RESPONSE_LIMIT:
                        raise HTTPException(503, "Companion response exceeds its bound")
                    response.headers["Cache-Control"] = "no-store"
                    return response
                except RequestValidationError:
                    # Validation errors must never echo a redemption secret.
                    return JSONResponse(
                        status_code=422,
                        content={"detail": "Invalid companion request"},
                        headers={"Cache-Control": "no-store"},
                    )
                except CompanionError as exc:
                    return JSONResponse(
                        status_code=exc.status,
                        content={"detail": str(exc)},
                        headers={"Cache-Control": "no-store"},
                    )
                except HTTPException as exc:
                    return JSONResponse(
                        status_code=exc.status_code,
                        content={"detail": exc.detail},
                        headers={"Cache-Control": "no-store"},
                    )

            return handle

    router = APIRouter(prefix="/v1/companion", route_class=CompanionRoute)
    owner = Depends(auth.require("owner"))

    def device_token(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer ") or len(authorization) != 50:
            raise HTTPException(401, "Invalid companion bearer")
        return authorization[7:]

    companion_auth = Depends(device_token)

    @router.post("/pairings", response_model=PairingIssued, dependencies=[owner])
    def pair(request: PairingRequest):
        return companion.pair(request)

    @router.post("/pairings/redeem", response_model=DeviceIssued)
    def redeem(request: PairingRedeem, authorization: str = Header(default="")):
        if authorization:
            raise HTTPException(401, "Redemption does not accept a bearer")
        return companion.redeem(request)

    @router.get("/devices", response_model=DevicePage, dependencies=[owner])
    def devices():
        return {"devices": companion.devices()}

    @router.post(
        "/devices/{device_id}/revoke", response_model=CompanionDevice, dependencies=[owner]
    )
    def revoke(device_id: RouteID, request: CompanionWire):
        return companion.revoke(device_id)

    @router.get("/self", response_model=CompanionDevice)
    def self_device(token=companion_auth):
        return companion.self_device(token)

    @router.post("/calls", response_model=CompanionCall, dependencies=[owner])
    def invite(request: CallRequest):
        return companion.invite(request)

    @router.get("/calls", response_model=CallPage, dependencies=[owner])
    def owner_calls(
        session_id: str = Query(min_length=1, max_length=128),
        device_id: str | None = Query(default=None, min_length=1, max_length=128),
        after: int = Query(default=0, ge=0, le=MAX_PLAYBACK_GENERATION),
        limit: int = Query(default=20, ge=1, le=20),
    ):
        return companion.calls(session_id=session_id, device_id=device_id, after=after, limit=limit)

    @router.get("/calls/{call_id}", response_model=CompanionCall, dependencies=[owner])
    def owner_call(call_id: RouteID):
        return companion.get_call(call_id)

    @router.post("/calls/{call_id}/end", response_model=CompanionCall, dependencies=[owner])
    def owner_end(call_id: RouteID, request: CallEnd):
        return companion.action(call_id, request)

    @router.post("/calls/{call_id}/note", response_model=CompanionCall, dependencies=[owner])
    def note(call_id: RouteID, request: CallNote):
        return companion.note(call_id, request)

    @router.get("/self/calls", response_model=CallPage)
    def device_calls(
        token=companion_auth,
        after: int = Query(default=0, ge=0, le=MAX_PLAYBACK_GENERATION),
        limit: int = Query(default=20, ge=1, le=20),
    ):
        return companion.calls(token=token, after=after, limit=limit)

    @router.get("/self/calls/{call_id}", response_model=CompanionCall)
    def device_call(call_id: RouteID, token=companion_auth):
        return companion.get_call(call_id, token=token)

    @router.post("/self/calls/{call_id}/actions", response_model=CompanionCall)
    def action(call_id: RouteID, request: CallAction, token=companion_auth):
        return companion.action(call_id, request, token=token)

    app.include_router(router)
