"""One owned metadata exchange per original; no automatic consumption replay."""

import httpx

from packages.contracts.live_agent_sources import LiveAgentSourceQuery, LiveAgentSourceState
from packages.contracts.live_decisions_attempts import live_attempt_json
from packages.contracts.live_effect_evidence import LiveEffectEvidenceState
from packages.contracts.live_effect_pending import (
    MAX_PAGE_BYTES,
    LiveEffectPendingPage,
    LiveEffectPendingQuery,
)
from packages.contracts.live_effects import (
    MAX_RESPONSE_BYTES,
    LiveEffectAdmit,
    LiveEffectFinish,
    LiveEffectQuery,
    LiveEffectReceipt,
    LiveEffectRecord,
    LiveEffectRefused,
    LiveEffectStart,
    LiveEffectState,
    effect_json,
)

BASE = "/v2/transcript/live-effects"


async def _exchange(client, path, request, model):
    try:
        raw = await client._owned_metadata(
            "POST",
            BASE + path,
            json=request.model_dump(mode="json"),
            maximum=MAX_RESPONSE_BYTES,
            raw=True,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            raise LiveEffectRefused("Original live metadata request was refused") from exc
        raise
    return model.model_validate(effect_json(raw, MAX_RESPONSE_BYTES), strict=True)


async def lookup(client, request):
    request = LiveEffectQuery.model_validate(request, strict=True)
    result = await _exchange(client, "/lookup", request, LiveEffectState)
    if result.original != request:
        raise ValueError("Live consumer lookup changed its original query")
    return result


async def pending(client, request):
    request = LiveEffectPendingQuery.model_validate(request, strict=True)
    raw = await client._owned_metadata(
        "POST",
        BASE + "/pending",
        json=request.model_dump(mode="json"),
        maximum=MAX_PAGE_BYTES,
        raw=True,
    )
    result = LiveEffectPendingPage.model_validate(
        live_attempt_json(raw, maximum=MAX_PAGE_BYTES),
        strict=True,
    )
    if result.original != request:
        raise ValueError("Pending consumer page changed its original query")
    return result


async def evidence(client, request):
    request = LiveEffectReceipt.model_validate(request, strict=True)
    result = await _exchange(client, "/evidence", request, LiveEffectEvidenceState)
    if result.original != request:
        raise ValueError("Agent evidence changed its original reservation")
    return result


async def source_authority(client, request):
    request = LiveAgentSourceQuery.model_validate(request, strict=True)
    result = await _exchange(client, "/source-authority", request, LiveAgentSourceState)
    if result.original != request:
        raise ValueError("Native source authority changed its original query")
    return result


async def admit(client, request):
    request = LiveEffectAdmit.model_validate(request, strict=True)
    result = await _exchange(client, "/admit", request, LiveEffectRecord)
    if result.receipt.original != request:
        raise ValueError("Live consumer admission changed its original operation")
    return result


async def finish(client, request):
    request = LiveEffectFinish.model_validate(request, strict=True)
    result = await _exchange(client, "/finish", request, LiveEffectRecord)
    if result.receipt != request.original or result.completion != request:
        raise ValueError("Live consumer completion changed its original operation")
    return result


async def start(client, request):
    request = LiveEffectStart.model_validate(request, strict=True)
    result = await _exchange(client, "/start", request, LiveEffectRecord)
    if (
        result.receipt != request.original
        or result.started is None
        or result.started.original != request
    ):
        raise ValueError("Live consumer start changed its original executor")
    return result
