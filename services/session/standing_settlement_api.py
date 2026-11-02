"""Agent-only native settlement transport on the existing shared owner bound."""

import asyncio
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from packages.contracts.standing_agent_settlement import (
    StandingAgentRetirementRegistrationReceipt,
    StandingAgentRetirementRegistrationRequest,
    StandingAgentSettlementQuery,
    StandingAgentSettlementReceipt,
    StandingAgentSettlementRequest,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.session.standing_resource_finalization_api import (
    StandingResourceFinalizationOperations,
)
from services.session.standing_settlement import _fresh
from services.session.standing_work_api import STANDING_INGRESS_SECONDS

_INVALID = "Invalid standing Agent settlement request"
_HEADER = b"x-kinaiya-standing-settlement-capability"
_BODIES = {
    "register_agent": StandingAgentRetirementRegistrationRequest,
    "registration_lookup": StandingAgentRetirementRegistrationRequest,
    "settle": StandingAgentSettlementRequest,
    "lookup": StandingAgentSettlementQuery,
}
_RESULTS = {
    "register_agent": StandingAgentRetirementRegistrationReceipt,
    "registration_lookup": StandingAgentRetirementRegistrationReceipt,
    "settle": StandingAgentSettlementReceipt,
    "lookup": StandingAgentSettlementReceipt,
}


class StandingSettlementOperations(StandingResourceFinalizationOperations):
    async def _body(self, request, event, deadline, started, *, kind):
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Standing Agent settlement requires identity JSON")
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

    @staticmethod
    def _settlement_capability(request, action):
        headers = [
            (key.lower(), value)
            for key, value in request.scope["headers"]
            if key.lower().startswith(b"x-kinaiya-") and key.lower().endswith(b"capability")
        ]
        if action != "settle":
            if headers:
                raise HTTPException(422, _INVALID)
            return None
        if (
            len(headers) != 1
            or headers[0][0] != _HEADER
            or re.fullmatch(rb"[0-9a-f]{64}", headers[0][1]) is None
        ):
            raise HTTPException(422, _INVALID)
        return bytes.fromhex(headers[0][1].decode("ascii"))

    def _call(self, action, argument, query, event, deadline, *, capability=None):
        self._guard(event, deadline)
        try:
            if query or action not in _BODIES:
                raise StandingUnavailable
            argument = _fresh(argument, _BODIES[action])
            method = getattr(self.owner, action)
            if action == "settle":
                result = method(argument, capability, deadline=deadline, cancelled=event.is_set)
            elif action == "register_agent":
                if capability is not None:
                    raise StandingUnavailable
                result = method(argument, deadline=deadline, cancelled=event.is_set)
            else:
                if capability is not None:
                    raise StandingUnavailable
                result = method(argument)
            self._guard(event, deadline)
            if result is None and action in {"registration_lookup", "lookup"}:
                return b"null"
            value = _fresh(result, _RESULTS[action])
            value = (
                value.validate_query(argument)
                if action == "lookup"
                else value.validate_request(argument)
            )
            raw = value.canonical_bytes()
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
            if action not in _BODIES:
                raise HTTPException(422, _INVALID)
            capability = self._settlement_capability(request, action)
            argument = await self._body(request, event, deadline, started, kind=_BODIES[action])
            self._guard(event, min(deadline, started + STANDING_INGRESS_SECONDS))
            ingress_done.set()
            task = asyncio.create_task(
                asyncio.to_thread(
                    self._call, action, argument, query, event, deadline, capability=capability
                )
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


def install_standing_settlement_routes(app, owner, auth, *, shared):
    operations = StandingSettlementOperations(owner, shared=shared)
    router = APIRouter(prefix="/v1/standing/tasks", dependencies=[Depends(auth.require("agent"))])

    async def respond(request, action):
        try:
            raw = await operations.run(request, action)
        except StandingConflict:
            raise HTTPException(409, str(StandingConflict())) from None
        except StandingUnavailable:
            raise HTTPException(503, str(StandingUnavailable())) from None
        return Response(raw, media_type="application/json", headers={"Cache-Control": "no-store"})

    @router.post("/retirement/register")
    async def register(request: Request):
        return await respond(request, "register_agent")

    @router.post("/retirement/registration/lookup")
    async def registration_lookup(request: Request):
        return await respond(request, "registration_lookup")

    @router.post("/settle")
    async def settle(request: Request):
        return await respond(request, "settle")

    @router.post("/settlement/lookup")
    async def lookup(request: Request):
        return await respond(request, "lookup")

    app.include_router(router)
    return operations
