"""One owned metadata exchange per original; no automatic consumption replay."""

from packages.contracts.live_effects import (
    MAX_RESPONSE_BYTES,
    LiveEffectAdmit,
    LiveEffectFinish,
    LiveEffectQuery,
    LiveEffectRecord,
    LiveEffectStart,
    LiveEffectState,
    effect_json,
)

BASE = "/v2/transcript/live-effects"


async def _exchange(client, path, request, model):
    raw = await client._owned_metadata(
        "POST",
        BASE + path,
        json=request.model_dump(mode="json"),
        maximum=MAX_RESPONSE_BYTES,
        raw=True,
    )
    return model.model_validate(effect_json(raw, MAX_RESPONSE_BYTES), strict=True)


async def lookup(client, request):
    request = LiveEffectQuery.model_validate(request, strict=True)
    result = await _exchange(client, "/lookup", request, LiveEffectState)
    if result.original != request:
        raise ValueError("Live consumer lookup changed its original query")
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
