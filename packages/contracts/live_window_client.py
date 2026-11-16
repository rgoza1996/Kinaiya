"""Retained HTTP exchanges; a missing original receipt is never a no-write fence."""

from packages.contracts.live_windows import (
    MAX_CANDIDATE_BYTES,
    MAX_RESULT_BYTES,
    LiveWindowCandidate,
    LiveWindowPublication,
    LiveWindowPublicationProof,
    LiveWindowPublicationRef,
    LiveWindowPublicationState,
    LiveWindowScope,
    live_window_json,
)

BASE = "/v2/transcript/live-windows"


async def _exchange(client, path, request, model, maximum):
    raw = await client._owned_metadata(
        "POST", BASE + path, json=request.model_dump(mode="json"), maximum=maximum, raw=True
    )
    # Parse bounds/duplicate keys before allowing JSON-native datetime decoding.
    live_window_json(raw, maximum)
    return model.model_validate_json(raw, strict=True)


async def publish(client, original):
    # Frozen v2 envelopes contain an unchanged, mutable v1 AppendRequest. Detach
    # that full original before any HTTP effect or suspension can expose it.
    if isinstance(original, LiveWindowPublication):
        original = original.model_dump(mode="json")
    original = LiveWindowPublication.model_validate(original, strict=True)
    result = await _exchange(client, "", original, LiveWindowPublicationProof, MAX_RESULT_BYTES)
    if result.original != original.reference():
        raise ValueError("Live publication changed its original request")
    return result


async def lookup(client, reference):
    reference = LiveWindowPublicationRef.model_validate(reference, strict=True)
    result = await _exchange(
        client, "/lookup", reference, LiveWindowPublicationState, MAX_RESULT_BYTES
    )
    if result.original != reference:
        raise ValueError("Live lookup changed its original request")
    return result


async def latest(client, scope):
    scope = LiveWindowScope.model_validate(scope, strict=True)
    raw = await client._owned_metadata(
        "POST",
        BASE + "/latest",
        json=scope.model_dump(mode="json"),
        maximum=MAX_CANDIDATE_BYTES,
        raw=True,
    )
    if raw == b"null":
        return None
    live_window_json(raw, MAX_CANDIDATE_BYTES)
    result = LiveWindowCandidate.model_validate_json(raw, strict=True)
    source = result.latest.original
    if (source.session_id, source.thread_id, source.epoch, source.utterance_id) != (
        scope.session_id,
        scope.thread_id,
        scope.epoch,
        scope.utterance_id,
    ):
        raise ValueError("Live candidate changed its original scope")
    return result
