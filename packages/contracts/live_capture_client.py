"""Original bounded HTTP capture metadata; no retries, startup or physical claims."""

from packages.contracts.live_captures import (
    MAX_RESULT_BYTES,
    LiveCaptureBackend,
    LiveCaptureFence,
    LiveCaptureRegistration,
    LiveCaptureSourceState,
    LiveCaptureState,
    capture_json,
)
from packages.contracts.live_windows import LiveWindowPublicationRef

BASE = "/v2/transcript/live-captures"


async def backend(client):
    raw = await client._owned_metadata("GET", BASE + "/backend", maximum=MAX_RESULT_BYTES, raw=True)
    return LiveCaptureBackend.model_validate(capture_json(raw, MAX_RESULT_BYTES), strict=True)


async def _exchange(client, path, request, model):
    raw = await client._owned_metadata(
        "POST",
        BASE + path,
        json=request.model_dump(mode="json"),
        maximum=MAX_RESULT_BYTES,
        raw=True,
    )
    return model.model_validate(capture_json(raw, MAX_RESULT_BYTES), strict=True)


async def state(client, method, original):
    original = LiveCaptureRegistration.model_validate(original, strict=True)
    result = await _exchange(client, "/" + method, original, LiveCaptureState)
    if result.original != original or (
        method == "register" and result.receipt is None and result.fence is None
    ):
        raise ValueError("Native capture response did not settle its original request")
    return result


async def fence(client, request):
    request = LiveCaptureFence.model_validate(request, strict=True)
    result = await _exchange(client, "/fence", request, LiveCaptureState)
    if (
        result.original != request.original
        or result.fence is None
        or result.fence.request != request
    ):
        raise ValueError("Native capture response did not settle its original fence")
    return result


async def source(client, reference):
    reference = LiveWindowPublicationRef.model_validate(reference, strict=True)
    result = await _exchange(client, "/source", reference, LiveCaptureSourceState)
    if result.original != reference:
        raise ValueError("Native capture response changed its original source")
    return result
