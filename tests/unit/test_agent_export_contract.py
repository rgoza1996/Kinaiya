"""Agent-only source admission binds metadata, never full prompts or permission."""

import json
from pathlib import Path

import jsonschema
import pytest

from packages.contracts.agent_export import (
    MAX_AGENT_EXPORT_BYTES,
    AgentExportAdmission,
    AgentExportProviderInfo,
    AgentExportRequest,
    agent_export_json,
)
from packages.contracts.models import ProviderInfo


def request(**changes):
    value = dict(
        task_id="task",
        objective_revision=1,
        session_id="session",
        thread_id="thread",
        epoch=1,
        source_event_ids=["source"],
        source_revisions={"source": 1},
        content_scope="public_synthetic",
        provider=ProviderInfo(provider="meta", model="public-synthetic", local=False),
        request_sha256="ab" * 32,
        purpose="complete_count",
        request_nonce="nonce-1",
    )
    value.update(changes)
    return AgentExportRequest(**value)


def admission(req):
    return AgentExportAdmission(
        **req.model_dump(),
        binding_sha256=req.binding_digest(),
        effective_scope=req.content_scope,
        journal_sequence=7,
    )


@pytest.mark.parametrize(
    "model", [AgentExportRequest, AgentExportAdmission, AgentExportProviderInfo]
)
def test_exported_schema_is_exact(model):
    stored = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "packages/contracts/schemas"
            / f"{model.__name__}.json"
        ).read_text()
    )
    assert stored == model.model_json_schema()
    jsonschema.Draft202012Validator.check_schema(stored)


@pytest.mark.parametrize(
    "change",
    [
        {"objective_revision": True},
        {"epoch": "1"},
        {"task_id": ""},
        {"task_id": "x" * 129},
        {"request_sha256": "AB" * 32},
        {"request_sha256": "a" * 63},
        {"purpose": "stream"},
        {"request_nonce": ""},
        {"request_nonce": "\ud800"},
        {"thread_id": "bad\0id"},
        {"source_event_ids": ["source", "source"]},
        {"source_revisions": {}},
        {"source_revisions": {"other": 1}},
        {"source_revisions": {"source": True}},
        {
            "source_event_ids": [str(i) for i in range(33)],
            "source_revisions": {str(i): 1 for i in range(33)},
        },
        {"source_event_ids": ["\udfff"], "source_revisions": {"\udfff": 1}},
        {"context_event_ids": ["context", "context"]},
        {"context_event_ids": [str(i) for i in range(129)]},
        {"context_event_ids": ["bad\0id"]},
        {"context_event_ids": ["\ud800"]},
        {"context_event_ids": [True]},
        {"content_scope": "cloud"},
        {"headers": {"Authorization": "forbidden"}},
        {"provider": {"provider": "meta", "model": "synthetic", "local": "false"}},
        {
            "provider": {
                "provider": "meta",
                "model": "synthetic",
                "local": False,
                "api_key": "forbidden",
            }
        },
        {"provider": {"provider": "meta", "model": "\ud800", "local": False}},
    ],
)
def test_strict_bounded_metadata(change):
    with pytest.raises(ValueError):
        request(**change)


def test_empty_sources_and_maximum_unique_bindings_are_supported():
    assert request(source_event_ids=[], source_revisions={}).source_revisions == {}
    assert request().context_event_ids == []
    assert (
        len(request(context_event_ids=[f"context-{i}" for i in range(128)]).context_event_ids)
        == 128
    )
    ids = [f"source-{i}" for i in range(32)]
    assert (
        len(request(source_event_ids=ids, source_revisions=dict.fromkeys(ids, 1)).source_event_ids)
        == 32
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "other"),
        ("objective_revision", 2),
        ("session_id", "other"),
        ("thread_id", "other"),
        ("epoch", 2),
        ("source_revisions", {"source": 2}),
        ("context_event_ids", ["retrieved-context"]),
        ("request_sha256", "cd" * 32),
        ("purpose", "complete_generation"),
        ("request_nonce", "nonce-2"),
        ("provider", {"provider": "meta", "model": "other", "local": False}),
    ],
)
def test_admission_echo_matches_every_request_field(field, value):
    req = request()
    wrong = request(**{field: value})
    with pytest.raises(ValueError):
        admission(wrong).validate_request(req)
    assert wrong.binding_digest() != req.binding_digest()


@pytest.mark.parametrize(
    "change",
    [
        {"binding_sha256": "cd" * 32},
        {"journal_sequence": True},
        {"journal_sequence": -1},
        {"effective_scope": "private_cloud_authorized"},
    ],
)
def test_copied_receipt_revalidated_not_duck_typed(change):
    req = request()
    copied = admission(req).model_copy(update=change)
    with pytest.raises(ValueError):
        copied.validate_request(req)


def test_admission_cannot_widen_a_local_request_scope():
    req = request(
        content_scope="local_only",
        provider=ProviderInfo(provider="fixture", model="local", local=True),
    )
    copied = admission(req).model_copy(update={"effective_scope": "public_synthetic"})
    with pytest.raises(ValueError):
        copied.validate_request(req)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b"\xff",
        b"[" * 2000,
        b" " * (MAX_AGENT_EXPORT_BYTES + 1),
    ],
)
def test_bounded_closed_json(raw):
    with pytest.raises(ValueError, match="^Invalid Agent export admission JSON$"):
        agent_export_json(raw)


def test_no_prompt_text_or_headers_are_admitted_or_returned():
    req = request()
    assert admission(req).validate_request(req).binding_sha256 == req.binding_digest()
    assert not {"text", "prompt", "headers", "authorization", "api_key"} & set(req.model_dump())
    assert AgentExportAdmission.model_fields["binding_sha256"].is_required()


@pytest.mark.parametrize(
    "encoding",
    ["utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"],
)
def test_json_wire_is_strict_utf8_without_bom(encoding):
    raw = request().model_dump_json().encode(encoding)
    with pytest.raises(ValueError, match="^Invalid Agent export admission JSON$"):
        agent_export_json(raw)


@pytest.mark.parametrize("raw", [b'{"x":1e999}', b'{"\\ud800":"x"}', b'{"x":"\\udfff"}'])
def test_json_gate_rejects_overflow_and_unpaired_unicode_even_in_unknown_fields(raw):
    with pytest.raises(ValueError, match="^Invalid Agent export admission JSON$"):
        agent_export_json(raw)


def test_json_gate_keeps_valid_escaped_surrogate_pair_exact():
    assert agent_export_json(b'{"x":"\\ud834\\udd1e"}') == {"x": "𝄞"}
