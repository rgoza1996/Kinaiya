"""Owner-only provider catalog routes; no adapter construction or discovery."""

import json
import math
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from services.settings.provider_catalog import ProviderCatalogConflict, ProviderChange

_MAX_REQUEST_BYTES = 65536


def _error(status, detail):
    return HTTPException(status, detail, headers={"Cache-Control": "no-store"})


async def _change_body(request):
    """Bound the body and redact validation errors before handling any secret.

    FastAPI's ordinary 422 details can include rejected input values. A raw
    credential must never be reflected even in an owner's malformed request.
    """
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_REQUEST_BYTES:
            raise _error(413, "Provider change exceeds its request bound")
        raw.extend(chunk)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Nonfinite number")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)
        pending, nodes = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 32 or nodes > 4096:
                raise ValueError("JSON bound")
            if type(item) is str:
                item.encode("utf-8")
            elif type(item) is float and not math.isfinite(item):
                raise ValueError("Nonfinite number")
            elif type(item) is dict:
                pending.extend((key, depth + 1) for key in item)
                pending.extend((child, depth + 1) for child in item.values())
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item)
        return ProviderChange.model_validate(value)
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise _error(422, "Provider change requires the strict versioned operation shape") from None


def router(catalog, auth):
    routes = APIRouter(prefix="/v1/providers", dependencies=[Depends(auth.require("owner"))])

    @routes.get("")
    async def inspect(response: Response):
        response.headers["Cache-Control"] = "no-store"
        if catalog is None:
            raise _error(503, "Provider catalog is not enabled on this host")
        try:
            return catalog.snapshot()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            raise _error(503, "Provider catalog inspection is unavailable") from None

    @routes.post("/changes")
    async def change(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if catalog is None:
            raise _error(503, "Provider catalog is not enabled on this host")
        body = await _change_body(request)
        try:
            return catalog.change(body)
        except ProviderCatalogConflict as exc:
            raise _error(409, str(exc)) from None
        except ValueError:
            raise _error(
                400, "Provider change refused; inspect the declaration and catalog bounds"
            ) from None
        except (OSError, RuntimeError, sqlite3.Error):
            raise _error(
                503, "Provider change outcome is unconfirmed; inspect its exact receipt"
            ) from None

    @routes.get("/changes/{key}")
    async def receipt(key: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if catalog is None or re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", key) is None:
            raise _error(404, "Provider change receipt is unavailable")
        try:
            value = catalog.receipt(key)
        except (OSError, ValueError, sqlite3.Error):
            raise _error(503, "Provider change receipt is unavailable") from None
        if value is None:
            # Another accepted request can still commit after this read.
            raise _error(404, "No committed provider change receipt is currently visible")
        return value

    return routes
