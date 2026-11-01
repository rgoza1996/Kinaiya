"""Bounded owner-only installed capability administration."""

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.capability_settings import CapabilityChange
from services.agent.capability_settings import CapabilityConflict, decode


def router(runtime, auth):
    routes = APIRouter(prefix="/v1/agent/options", dependencies=[Depends(auth.require("owner"))])

    def error(code, message):
        return HTTPException(code, message, headers={"Cache-Control": "no-store"})

    def owner():
        value = getattr(runtime, "capability_settings", None)
        if value is None:
            raise error(503, "Native capability owner is unavailable")
        return value

    def response(value):
        result = JSONResponse(value, headers={"Cache-Control": "no-store"})
        if len(result.body) > 65536:
            raise error(503, "Capability response exceeds its bound")
        return result

    async def body(request):
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > 16384:
                        raise error(413, "Capability request exceeds its bound")
                    raw.extend(chunk)
        except TimeoutError:
            raise error(408, "Capability request deadline expired") from None
        try:
            return CapabilityChange.model_validate(decode(raw.decode("utf-8")))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise error(422, "Capability request requires its strict versioned shape") from None

    @routes.get("/capabilities")
    async def inspect(request: Request):
        pairs = list(request.query_params.multi_items())
        if len(pairs) != len({key for key, _ in pairs}) or any(
            key not in {"section", "after", "limit"} for key, _ in pairs
        ):
            raise error(400, "Invalid capability page")
        query = dict(pairs)
        for key in ("after", "limit"):
            if key in query:
                if (
                    not 1 <= len(query[key]) <= 7
                    or not query[key].isascii()
                    or not query[key].isdigit()
                ):
                    raise error(400, "Invalid capability cursor")
                query[key] = int(query[key])
        try:
            return response(await owner().snapshot(query.pop("section", "tools"), **query))
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            raise error(503, "Capability metadata is unavailable") from None
        except Exception:
            raise error(503, "Capability metadata is unavailable") from None

    @routes.post("/capabilities")
    async def change(request: Request):
        settings = owner()
        original = await body(request)
        try:
            return response(await settings.change(original))
        except CapabilityConflict:
            raise error(409, "Capability authority changed; inspect the original request") from None
        except ValueError:
            raise error(400, "Capability action refused; inspect the native requirements") from None
        except (RuntimeError, OSError, sqlite3.Error):
            raise error(
                503, "Capability outcome is unconfirmed; check the original request"
            ) from None
        except Exception:
            raise error(
                503, "Capability outcome is unconfirmed; check the original request"
            ) from None

    @routes.post("/capability-changes/lookup")
    async def lookup(request: Request):
        settings = owner()
        original = await body(request)
        try:
            return response(settings.lookup(original))
        except CapabilityConflict:
            raise error(409, "Original capability backend or identity conflicts") from None
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            raise error(503, "Original capability outcome is unavailable") from None
        except Exception:
            raise error(503, "Original capability outcome is unavailable") from None

    return routes
