"""Regressions from independent pin and adapter mutation probes."""

from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.memory import MemoryBinding
from packages.contracts.memory_context import MemoryContextPin
from packages.contracts.models import JournalEntry, ProviderInfo, SpeechWindow
from services.converse.delivery_context import window_context_projection
from services.converse.history import typed_history_projection
from services.converse.memory_adapter import RecallSnapshot, SessionMemoryAdapter
from services.converse.memory_history import validate_pin

BINDING = MemoryBinding(session_id="synthetic", thread_id="thread", epoch=1)
PIN = {
    "schema_version": "1.0",
    "implementation_id": "korina-dag-v1",
    "use_generation": 1,
    "namespace_revision": 0,
    "closure_sha256": "a" * 64,
    "selector_ids": ["node"],
}


@pytest.mark.parametrize(
    "field,value",
    [
        ("use_generation", True),
        ("use_generation", 1.0),
        ("namespace_revision", False),
        ("namespace_revision", 0.0),
        ("selector_ids", ("node",)),
    ],
)
def test_mutated_model_is_strictly_revalidated_before_history_or_snapshot(field, value):
    current = MemoryContextPin.model_validate(PIN)
    setattr(current, field, value)
    assert validate_pin(current) is None
    with pytest.raises(ValueError):
        RecallSnapshot(BINDING, current, {"role": "system", "content": "Synthetic data"})
    assistant = JournalEntry(
        **BINDING.model_dump(),
        event_id="assistant",
        sequence=1,
        kind="conversation.converse",
        origin="converse",
        idempotency_key="assistant",
        payload={
            "text": "Synthetic recalled reply.",
            "content_scope": "local_only",
            "runtime_id": "runtime",
            "memory_context": dict(PIN),
        },
    )
    info = ProviderInfo(provider="synthetic", model="fixture", local=True)
    source = SimpleNamespace(**BINDING.model_dump(), sequence=2)
    projected = typed_history_projection(
        [assistant], source, info, 12, memory_context=current, runtime_id="runtime"
    )
    assert projected.messages == [] and not projected.consumed_memory
    window = SpeechWindow(
        **BINDING.model_dump(),
        utterance_id="current",
        window_index=0,
        revision=1,
        text="Synthetic current speech.",
        start_ms=0,
        end_ms=1000,
        user_speaking=False,
    )
    source = JournalEntry(
        **BINDING.model_dump(),
        event_id="window",
        sequence=2,
        kind="conversation.window",
        origin="authenticated-user",
        idempotency_key="window",
        payload=window.model_dump(mode="json"),
    )
    projected = window_context_projection(
        [assistant, source], source, info, memory_context=current, runtime_id="runtime"
    )
    assert projected.messages == [] and not projected.consumed_memory


@pytest.mark.parametrize("operation", ["recall", "validate"])
@pytest.mark.parametrize("origin", ["http://foreign.invalid", "https://foreign.invalid"])
async def test_changed_origin_is_refused_before_credential_or_memory_dispatch(operation, origin):
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(503, json={})

    adapter = SessionMemoryAdapter(
        "http://127.0.0.1",
        "synthetic-credential-0000000000",
        transport=httpx.MockTransport(response),
    )
    adapter._http.base_url = origin
    try:
        if operation == "recall":
            assert await adapter.recall(BINDING, ("node",), 1) is None
        else:
            snapshot = RecallSnapshot(
                BINDING,
                MemoryContextPin.model_validate(PIN),
                {"role": "system", "content": "Synthetic data"},
            )
            assert await adapter.validate(snapshot) is False
        assert requests == []
    finally:
        await adapter.close()


async def test_mutated_binding_is_not_normalized_into_an_authorized_recall():
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(503, json={})

    adapter = SessionMemoryAdapter(
        "http://127.0.0.1",
        "synthetic-credential-0000000000",
        transport=httpx.MockTransport(response),
    )
    malformed = BINDING.model_copy(update={"epoch": True})
    try:
        with pytest.raises(ValueError):
            RecallSnapshot(
                malformed,
                MemoryContextPin.model_validate(PIN),
                {"role": "system", "content": "Synthetic data"},
            )
        assert await adapter.recall(malformed, ("node",), 1) is None
        assert requests == []
    finally:
        await adapter.close()
