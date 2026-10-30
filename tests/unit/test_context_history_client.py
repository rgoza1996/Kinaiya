"""Strict client refusal of malformed native page transport; synthetic responses."""

import json

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.context import MAX_CONTEXT_RESPONSE_BYTES
from packages.contracts.models import JournalEntry


def response_page():
    entry = JournalEntry(
        event_id="base-4",
        session_id="session",
        thread_id="thread",
        epoch=1,
        sequence=4,
        kind="conversation.user",
        origin="authenticated-user",
        payload={"text": "Synthetic public fixture", "final": True},
        idempotency_key="synthetic-4",
    )
    return {
        "schema_version": "1.0",
        "source_event_id": "source",
        "session_id": "session",
        "thread_id": "thread",
        "epoch": 1,
        "through_sequence": 10,
        "entries": [entry.model_dump(mode="json")],
        "next_before_sequence": 4,
        "exhausted": False,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "source",
        "watermark",
        "scope",
        "sequence",
        "kind",
        "epoch_bool",
        "duplicate_sequence",
        "cursor",
        "empty_continuation",
        "exhausted_cursor",
        "boolean",
    ],
)
async def test_client_refuses_invalid_or_nonprogressing_history_pages(mutation):
    page = response_page()
    if mutation == "version":
        del page["schema_version"]
    elif mutation == "source":
        page["source_event_id"] = "other"
    elif mutation == "watermark":
        page["through_sequence"] = 11
    elif mutation == "scope":
        page["entries"][0]["thread_id"] = "other"
    elif mutation == "sequence":
        page["entries"][0]["sequence"] = 8
        page["next_before_sequence"] = 8
    elif mutation == "kind":
        page["entries"][0]["kind"] = "resource.changed"
    elif mutation == "epoch_bool":
        page["entries"][0]["epoch"] = True
    elif mutation == "duplicate_sequence":
        page["entries"] *= 2
    elif mutation == "cursor":
        page["next_before_sequence"] = 5
    elif mutation == "empty_continuation":
        page["entries"] = []
    elif mutation == "exhausted_cursor":
        page["exhausted"] = True
    elif mutation == "boolean":
        page["exhausted"] = "false"
    client = JournalClient(
        "http://fixture",
        "synthetic",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page)),
    )
    try:
        with pytest.raises(ValueError):
            await client.context_history(
                source_event_id="source", through_sequence=10, before_sequence=8
            )
    finally:
        await client.close()


@pytest.mark.parametrize("mutation", ["base_order", "base_empty", "base_duplicate", "watermark"])
async def test_client_binds_qualifications_to_exact_requested_bases(mutation):
    page = response_page()
    page["entries"] = []
    page["exhausted"] = True
    page["next_before_sequence"] = None
    page["base_event_ids"] = ["base-4", "base-3"]
    if mutation == "base_order":
        page["base_event_ids"].reverse()
    elif mutation == "base_empty":
        page["base_event_ids"] = []
    elif mutation == "base_duplicate":
        page["base_event_ids"] = ["base-4", "base-4"]
    elif mutation == "watermark":
        page["through_sequence"] = 11
    client = JournalClient(
        "http://fixture",
        "synthetic",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=page)),
    )
    try:
        with pytest.raises(ValueError):
            await client.context_qualifications(
                source_event_id="source", through_sequence=10, base_event_ids=["base-4", "base-3"]
            )
    finally:
        await client.close()


@pytest.mark.parametrize("shape", ["duplicate", "oversized"])
async def test_client_rejects_ambiguous_or_oversized_native_json(shape):
    raw = json.dumps(response_page())
    raw = (
        '{"source_event_id":"other",' + raw[1:]
        if shape == "duplicate"
        else " " * (MAX_CONTEXT_RESPONSE_BYTES + 1)
    )
    client = JournalClient(
        "http://fixture",
        "synthetic",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw)),
    )
    try:
        with pytest.raises(ValueError):
            await client.context_history(source_event_id="source")
    finally:
        await client.close()
