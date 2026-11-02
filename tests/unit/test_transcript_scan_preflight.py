"""Independent frozen-v5 parser-entry ordering, not a new codec implementation.

The raw over-node case necessarily also exceeds the byte cap. It proves only
pre-parser refusal of overlapping bounds, never an isolated raw-node ceiling.
"""

import importlib
import json

import pytest

PAGE_BYTES = 262144
MAX_NODES = 262144


def body(metadata):
    request = {
        "schema_version": "1.0",
        "session_id": "synthetic-session",
        "thread_id": "synthetic-thread",
        "epoch": 1,
        "after": 0,
        "through_sequence": None,
        "limit": 1,
    }
    return {
        "schema_version": "1.0",
        "request": request,
        "through_sequence": 1,
        "entries": [
            {
                "schema_version": "1.0",
                "event_id": "synthetic-entry",
                "session_id": request["session_id"],
                "thread_id": request["thread_id"],
                "epoch": 1,
                "sequence": 1,
                "kind": "conversation.user",
                "origin": "authenticated-user",
                "created_at": "2026-01-01T00:00:00Z",
                "payload": {
                    "text": "Synthetic native metadata",
                    "final": True,
                    "revision": 1,
                    "content_scope": "public_synthetic",
                    "native_metadata": metadata,
                },
                "causes": [],
                "task_refs": [],
                "idempotency_key": "synthetic-preflight-entry",
            }
        ],
        "next_after": None,
        "exhausted": True,
    }


def nesting(levels):
    value = 0
    for _ in range(levels):
        value = [value]
    return value


def graph_size(value):
    """Fixture analysis is iterative and independent of implementation helpers."""
    pending, nodes, maximum_depth = [(value, 0)], 0, 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        maximum_depth = max(maximum_depth, depth)
        if type(current) is dict:
            pending.extend((key, depth + 1) for key in current)
            pending.extend((child, depth + 1) for child in current.values())
        elif type(current) is list:
            pending.extend((child, depth + 1) for child in current)
    return nodes, maximum_depth


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def instrument(monkeypatch):
    module = importlib.import_module("packages.contracts.transcript_scan")
    original = json.loads
    entered = []

    def traced(value, *args, **kwargs):
        # Record only size/type; never put transcript input into test evidence.
        entered.append((type(value).__name__, len(value)))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", traced)
    # Also cover a direct stdlib import without assuming a private alias name.
    for name, value in tuple(vars(module).items()):
        if value is original:
            monkeypatch.setattr(module, name, traced)
    return module.TranscriptScanPage, entered


@pytest.mark.parametrize("levels", [513, 600])
def test_over_depth_raw_native_page_is_refused_before_recursive_parser(monkeypatch, levels):
    value = body(nesting(levels))
    nodes, depth = graph_size(value)
    encoded = raw(value)
    assert depth > 512 and nodes < MAX_NODES and len(encoded) < PAGE_BYTES
    Page, entered = instrument(monkeypatch)
    with pytest.raises(ValueError):
        Page.from_json(encoded)
    assert entered == [], "Over-depth untrusted JSON reached recursive json.loads"


@pytest.mark.parametrize("shape", ["near_depth", "20000_scalars"])
def test_valid_full_native_metadata_enters_parser_and_survives_in_full(monkeypatch, shape):
    metadata = nesting(500) if shape == "near_depth" else [0] * 20000
    value = body(metadata)
    nodes, depth = graph_size(value)
    encoded = raw(value)
    assert depth <= 512 and nodes < MAX_NODES and len(encoded) < PAGE_BYTES
    if shape == "near_depth":
        assert depth >= 500
    Page, entered = instrument(monkeypatch)
    page = Page.from_json(encoded)
    assert entered, "Parser instrumentation was not exercised by valid input"
    observed = page.entries[0].payload["native_metadata"]
    assert graph_size(observed) == graph_size(metadata)
    if shape == "20000_scalars":
        assert observed == metadata
    else:
        for _ in range(500):
            assert type(observed) is list and len(observed) == 1
            observed = observed[0]
        assert observed == 0


def test_raw_node_overflow_overlapping_byte_cap_is_refused_before_parser(monkeypatch):
    value = body([0] * (MAX_NODES + 1))
    nodes, _ = graph_size(value)
    encoded = raw(value)
    assert nodes > MAX_NODES and len(encoded) > PAGE_BYTES
    Page, entered = instrument(monkeypatch)
    with pytest.raises(ValueError):
        Page.from_json(encoded)
    assert entered == []


def test_typed_graph_node_overflow_refuses_without_recursive_json_reparse(monkeypatch):
    Page, entered = instrument(monkeypatch)
    valid = Page.model_validate(body([0] * 20000))
    entry = valid.entries[0]
    payload = {**entry.payload, "native_metadata": [0] * (MAX_NODES + 1)}
    oversized = entry.model_copy(update={"payload": payload})
    forged = valid.model_copy(update={"entries": [oversized]})
    entered.clear()
    with pytest.raises(ValueError):
        Page.model_validate(forged)
    assert entered == []
