"""Retained original live ledger/publication exchanges, without replay."""

from packages.contracts.decisions_attempts import MAX_ATTEMPT_BYTES
from packages.contracts.live_decisions_attempts import (
    LiveDecisionsAttempt,
    LiveDecisionsAttemptAdmit,
    LiveDecisionsAttemptFinish,
    LiveDecisionsAttemptLookup,
    LiveDecisionsAttemptState,
    LiveDecisionsPublication,
    LiveDecisionsPublicationState,
    live_attempt_json,
)

BASE = "/v2/transcript/live-decisions"


async def exchange(client, path, original, model):
    raw = await client._owned_metadata(
        "POST",
        BASE + path,
        json=original.model_dump(mode="json"),
        maximum=MAX_ATTEMPT_BYTES,
        raw=True,
    )
    live_attempt_json(raw)
    return model.model_validate_json(raw, strict=True)


async def lookup(client, request):
    request = LiveDecisionsAttemptLookup.model_validate(request, strict=True)
    result = await exchange(client, "/attempts/lookup", request, LiveDecisionsAttemptState)
    if result.source != request.source:
        raise ValueError("Live inference lookup changed its original source")
    return result


async def admit(client, request):
    request = LiveDecisionsAttemptAdmit.model_validate(request, strict=True)
    result = await exchange(client, "/attempts/admit", request, LiveDecisionsAttempt)
    if result.admission != request:
        raise ValueError("Live inference admission changed its original request")
    return result


async def finish(client, request):
    request = LiveDecisionsAttemptFinish.model_validate(request, strict=True)
    result = await exchange(client, "/attempts/finish", request, LiveDecisionsAttempt)
    if result.completion != request:
        raise ValueError("Live inference completion changed its original result")
    return result


async def publication(client, method, request):
    request = LiveDecisionsPublication.model_validate(request, strict=True)
    result = await exchange(client, "/" + method, request, LiveDecisionsPublicationState)
    if result.original != request or (method == "publish" and result.receipt is None):
        raise ValueError("Live publication did not confirm its exact original")
    return result
