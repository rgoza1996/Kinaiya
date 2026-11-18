"""Bounded JSON ingress shared by external Agent control endpoints."""

import asyncio

from fastapi import HTTPException

from packages.contracts.external_agent import MAX_EXTERNAL_NATIVE_REQUEST_BYTES
from packages.contracts.transcript_scan import transcript_json


async def external_agent_body(incoming, model, limit=MAX_EXTERNAL_NATIVE_REQUEST_BYTES):
    if incoming.scope.get("query_string"):
        raise HTTPException(422, "External Agent native operations take a JSON body")
    raw = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in incoming.stream():
                if len(raw) + len(chunk) > limit:
                    raise HTTPException(413, "External Agent request exceeds its bound")
                raw.extend(chunk)
        bounded = bytes(raw)
        transcript_json(bounded, limit)
        return model.model_validate_json(bounded, strict=True)
    except TimeoutError:
        raise HTTPException(408, "External Agent request body is unavailable") from None
    except (ValueError, RecursionError):
        raise HTTPException(422, "Invalid external Agent request") from None
