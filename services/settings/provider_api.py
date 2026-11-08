"""Owner-only provider catalog and custom .env provider routes."""

import json
import math
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from services.settings.custom_providers_env import (
    CustomProviderConflictError,
    CustomProviderError,
    CustomProviderReferencedError,
    CustomProvidersEnvStore,
    CustomProviderValidationError,
)
from services.settings.provider_catalog import ProviderCatalogConflict, ProviderChange

_MAX_REQUEST_BYTES = 65536
_DISCOVER_ALLOWED_KEYS = {
    "expected_revision",
    "client_operation_id",
    "operation_id",
    "idempotency_key",
}


def _error(status, detail):
    return HTTPException(status, detail, headers={"Cache-Control": "no-store"})


async def _raw_body(request) -> bytes:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_REQUEST_BYTES:
            raise _error(413, "Provider change exceeds its request bound")
        raw.extend(chunk)
    return bytes(raw)


def _decode_bounded_json_object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Nonfinite number")

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
    if type(value) is not dict:
        raise ValueError("JSON root must be an object")
    return value


async def _json_body(request):
    """Read and bound a JSON object body without reflecting raw credentials."""
    raw = await _raw_body(request)
    return _decode_bounded_json_object(raw)


async def _optional_json_body(request) -> dict:
    """Read an optional JSON object body (empty body is treated as {})."""
    raw = await _raw_body(request)
    if not raw or not raw.strip():
        return {}
    return _decode_bounded_json_object(raw)


async def _change_body(request):
    """Bound the body and redact validation errors before handling any secret.

    FastAPI's ordinary 422 details can include rejected input values. A raw
    credential must never be reflected even in an owner's malformed request.
    """
    try:
        value = await _json_body(request)
        return ProviderChange.model_validate(value)
    except HTTPException:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, ValidationError):
        raise _error(422, "Provider change requires the strict versioned operation shape") from None


def _resolve_custom_store(catalog, custom_store):
    if custom_store is not None:
        return custom_store
    if catalog is not None and getattr(catalog, "path", None) is not None:
        cat_path = Path(catalog.path).resolve()
        workspace_attr = getattr(catalog, "workspace", None)
        workspace = Path(workspace_attr).resolve() if workspace_attr is not None else None
        return CustomProvidersEnvStore(
            cat_path.parent / "custom_providers.env",
            workspace=workspace,
        )
    return None


def router(catalog, auth, custom_store=None):
    routes = APIRouter(prefix="/v1/providers", dependencies=[Depends(auth.require("owner"))])
    resolved_custom = _resolve_custom_store(catalog, custom_store)

    @routes.get("")
    async def inspect(response: Response):
        response.headers["Cache-Control"] = "no-store"
        if catalog is None:
            raise _error(503, "Provider catalog is not enabled on this host")
        try:
            return catalog.snapshot()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            raise _error(503, "Provider catalog inspection is unavailable") from None

    @routes.get("/custom")
    async def inspect_custom(response: Response):
        response.headers["Cache-Control"] = "no-store"
        if resolved_custom is None:
            raise _error(503, "Custom provider .env store is not enabled on this host")
        try:
            return resolved_custom.snapshot()
        except (OSError, CustomProviderError, ValueError):
            raise _error(503, "Custom provider .env inspection is unavailable") from None

    @routes.post("/custom")
    async def change_custom(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if resolved_custom is None:
            raise _error(503, "Custom provider .env store is not enabled on this host")
        try:
            payload = await _json_body(request)
        except HTTPException:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise _error(422, "Custom provider request requires a valid JSON object") from None
        try:
            return await resolved_custom.apply_action_async(payload)
        except CustomProviderReferencedError as exc:
            return JSONResponse(
                status_code=409,
                headers={"Cache-Control": "no-store"},
                content={
                    "detail": str(exc),
                    "error": "custom_provider_referenced",
                    "profile_id": exc.profile_id,
                    "referenced_by": exc.referenced_by,
                },
            )
        except CustomProviderConflictError as exc:
            return JSONResponse(
                status_code=409,
                headers={"Cache-Control": "no-store"},
                content={
                    "detail": str(exc),
                    "error": exc.error_code,
                },
            )
        except (CustomProviderValidationError, CustomProviderError) as exc:
            raise _error(400, str(exc)) from None
        except OSError:
            raise _error(503, "Custom provider .env persistence failed") from None

    @routes.post("/custom/{profile_id}/discover")
    async def discover_custom(profile_id: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if resolved_custom is None:
            raise _error(503, "Custom provider .env store is not enabled on this host")
        try:
            body = await _optional_json_body(request)
        except HTTPException:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise _error(
                422, "Custom provider discovery request requires a valid JSON object"
            ) from None
        unknown_keys = set(body.keys()) - _DISCOVER_ALLOWED_KEYS
        if unknown_keys:
            raise _error(
                400,
                f"Unknown or unsupported field(s) for discover_custom_models: {', '.join(sorted(unknown_keys))}",
            )
        try:
            return await resolved_custom.discover_models_async(
                profile_id,
                expected_revision=body.get("expected_revision"),
                client_operation_id=body.get("client_operation_id"),
                operation_id=body.get("operation_id"),
                idempotency_key=body.get("idempotency_key"),
            )
        except CustomProviderConflictError as exc:
            return JSONResponse(
                status_code=409,
                headers={"Cache-Control": "no-store"},
                content={
                    "detail": str(exc),
                    "error": exc.error_code,
                },
            )
        except (CustomProviderValidationError, CustomProviderError) as exc:
            raise _error(400, str(exc)) from None
        except OSError:
            raise _error(503, "Custom provider model discovery failed") from None

    @routes.get("/custom/receipts/{key}")
    async def custom_receipt(key: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if resolved_custom is None:
            raise _error(503, "Custom provider .env store is not enabled on this host")
        try:
            return resolved_custom.reconcile(client_operation_id=key)
        except (CustomProviderValidationError, CustomProviderError) as exc:
            raise _error(400, str(exc)) from None
        except OSError:
            raise _error(503, "Custom provider receipt inspection is unavailable") from None

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
