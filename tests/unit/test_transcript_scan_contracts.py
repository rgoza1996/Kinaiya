"""Independent frozen #255 raw-scan packet oracles; synthetic data only."""

import copy
import importlib
import importlib.util
import json
from datetime import UTC, datetime

import pytest

from packages.contracts.models import JournalEntry

SAFE = 9_007_199_254_740_991


def kinds():
    name = "packages.contracts.transcript_scan"
    assert importlib.util.find_spec(name) is not None, "Frozen transcript scan packets are missing"
    module = importlib.import_module(name)
    return module.TranscriptScanRequest, module.TranscriptScanPage


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def request_body(**updates):
    return {
        "schema_version": "1.0",
        "session_id": "synthetic-session",
        "thread_id": "synthetic-thread",
        "epoch": 1,
        "after": 0,
        "through_sequence": None,
        "limit": 2,
        **updates,
    }


def entry_body(sequence=2):
    return JournalEntry(
        schema_version="1.0",
        event_id=f"synthetic-event-{sequence}",
        session_id="synthetic-session",
        thread_id="synthetic-thread",
        epoch=1,
        kind="conversation.user",
        origin="authenticated-user",
        sequence=sequence,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload={
            "text": "Synthetic transcript 🌱",
            "final": True,
            "revision": 1,
            "content_scope": "public_synthetic",
            "metadata": {"finite": 1.25, "flag": True, "empty": None, "items": [1, "x"]},
        },
        causes=[],
        task_refs=[],
        idempotency_key=f"synthetic-{sequence}",
    ).model_dump(mode="json")


def page_body(**updates):
    return {
        "schema_version": "1.0",
        "request": request_body(),
        "through_sequence": 9,
        "entries": [entry_body(2), entry_body(3)],
        "next_after": 3,
        "exhausted": False,
        **updates,
    }


def test_complete_request_and_page_roundtrip_preserve_real_entry_metadata_and_finite_json():
    Request, Page = kinds()
    original = Request.from_json(wire(request_body()))
    page = Page.from_json(wire(page_body())).validate_request(original)
    assert page.model_dump(mode="json") == page_body()
    assert page.request == original
    assert type(page.entries[0]) is JournalEntry
    assert page.entries[0].payload["metadata"]["finite"] == 1.25
    assert Request.from_json(wire(original.model_dump(mode="json"))) == original
    assert Page.from_json(wire(page.model_dump(mode="json"))) == page


@pytest.mark.parametrize("packet", ["request", "page"])
def test_exact_wire_limit_accepts_padding_and_rejects_one_more_raw_byte(packet):
    Request, Page = kinds()
    kind, body, maximum = (
        (Request, request_body(), 8192) if packet == "request" else (Page, page_body(), 262144)
    )
    raw = wire(body)
    exact = raw + b" " * (maximum - len(raw))
    assert kind.from_json(exact).model_dump(mode="json") == body
    with pytest.raises(ValueError):
        kind.from_json(exact + b" ")


@pytest.mark.parametrize("field", list(request_body()))
def test_request_requires_every_explicit_field(field):
    Request, _ = kinds()
    body = request_body()
    del body[field]
    with pytest.raises(ValueError):
        Request.from_json(wire(body))


@pytest.mark.parametrize("field", list(page_body()))
def test_page_requires_every_explicit_field(field):
    _, Page = kinds()
    body = page_body()
    del body[field]
    with pytest.raises(ValueError):
        Page.from_json(wire(body))


@pytest.mark.parametrize("field", list(entry_body()))
def test_native_entry_fields_cannot_be_regenerated_by_old_model_defaults(field):
    _, Page = kinds()
    body = page_body()
    del body["entries"][0][field]
    with pytest.raises(ValueError):
        Page.from_json(wire(body))


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "2.0"},
        {"session_id": ""},
        {"thread_id": "x" * 129},
        {"session_id": "synthetic\nsecret"},
        {"session_id": "synthetic\x00secret"},
        {"epoch": True},
        {"epoch": 1.0},
        {"epoch": "1"},
        {"epoch": 0},
        {"after": -1},
        {"after": SAFE + 1},
        {"through_sequence": False},
        {"through_sequence": SAFE + 1},
        {"limit": 0},
        {"limit": 21},
        {"limit": True},
        {"extra": "untrusted"},
    ],
)
def test_request_strict_identity_counters_and_closed_shape(change):
    Request, _ = kinds()
    with pytest.raises(ValueError):
        Request.model_validate(request_body(**change))


@pytest.mark.parametrize(
    "mutation",
    [
        "request",
        "scope",
        "thread",
        "epoch",
        "sequence_bool",
        "before",
        "after_highwater",
        "order",
        "duplicate",
        "too_many",
        "cursor",
        "empty_continuation",
        "exhausted_cursor",
        "bool",
        "highwater_bool",
        "extra",
        "entry_extra",
        "entry_counter_string",
        "unsafe_payload",
    ],
)
def test_page_refuses_wrong_request_scope_order_cursor_or_actual_entry_shape(mutation):
    Request, Page = kinds()
    original = Request.model_validate(request_body())
    body = page_body()
    if mutation == "request":
        body["request"]["limit"] = 3
    elif mutation in {"scope", "thread", "epoch"}:
        key = {"scope": "session_id", "thread": "thread_id", "epoch": "epoch"}[mutation]
        body["entries"][0][key] = 2 if key == "epoch" else "foreign"
    elif mutation == "sequence_bool":
        body["entries"][0]["sequence"] = True
    elif mutation == "before":
        body["entries"][0]["sequence"] = 0
    elif mutation == "after_highwater":
        body["through_sequence"] = 2
    elif mutation == "order":
        body["entries"].reverse()
    elif mutation == "duplicate":
        body["entries"][1]["event_id"] = body["entries"][0]["event_id"]
    elif mutation == "too_many":
        body["entries"].append(entry_body(4))
        body["next_after"] = 4
    elif mutation == "cursor":
        body["next_after"] = 4
    elif mutation == "empty_continuation":
        body["entries"] = []
    elif mutation == "exhausted_cursor":
        body["exhausted"] = True
    elif mutation == "bool":
        body["exhausted"] = "false"
    elif mutation == "highwater_bool":
        body["through_sequence"] = True
    elif mutation == "extra":
        body["extra"] = "discarded?"
    elif mutation == "entry_extra":
        body["entries"][0]["extra"] = "discarded?"
    elif mutation == "entry_counter_string":
        body["entries"][0]["epoch"] = "1"
    else:
        body["entries"][0]["payload"]["unsafe"] = SAFE + 1
    with pytest.raises(ValueError):
        Page.model_validate(body).validate_request(original)


def test_continued_page_keeps_full_original_and_fixed_native_highwater():
    Request, Page = kinds()
    body = request_body(after=1, through_sequence=9)
    request = Request.model_validate(body)
    value = page_body(request=body)
    assert Page.model_validate(value).validate_request(request).through_sequence == 9
    value["through_sequence"] = 10
    with pytest.raises(ValueError):
        Page.model_validate(value).validate_request(request)


def test_empty_exhausted_page_at_zero_highwater_is_valid():
    Request, Page = kinds()
    request = Request.model_validate(request_body())
    page = Page.model_validate(
        page_body(through_sequence=0, entries=[], next_after=None, exhausted=True)
    )
    assert page.validate_request(request).entries == []


@pytest.mark.parametrize(
    "defect",
    [
        "request_dict",
        "entry_dict",
        "entry_extra",
        "entry_bool",
        "entry_omitted",
        "page_extra",
        "request_extra",
    ],
)
def test_typed_graph_revalidation_rejects_forged_state_without_normalization(defect):
    Request, Page = kinds()
    page = Page.model_validate(page_body())
    if defect == "request_dict":
        page = page.model_copy(update={"request": page.request.model_dump(mode="json")})
    elif defect == "entry_dict":
        page = page.model_copy(update={"entries": [entry_body(2), page.entries[1]]})
    elif defect.startswith("entry_"):
        entry = copy.deepcopy(page.entries[0])
        if defect == "entry_extra":
            entry.__dict__["forged"] = "secret"
        elif defect == "entry_bool":
            entry.__dict__["sequence"] = True
        else:
            del entry.__dict__["event_id"]
        page = page.model_copy(update={"entries": [entry, page.entries[1]]})
    elif defect == "page_extra":
        page.__dict__["forged"] = "secret"
    else:
        request = Request.model_validate(request_body())
        request.__dict__["forged"] = "secret"
        page = page.model_copy(update={"request": request})
    with pytest.raises(ValueError):
        Page.model_validate(page)


@pytest.mark.parametrize(
    "raw", [b"[]", b"null", b'{"x":NaN}', b'{"x":Infinity}', b"\xff", b'{"session_id":"\\ud800"}']
)
def test_malformed_raw_input_is_refused(raw):
    Request, _ = kinds()
    with pytest.raises(ValueError):
        Request.from_json(raw)


def test_materialized_legacy_entry_keeps_generated_identity_with_subset_fields_set():
    Request, Page = kinds()
    original = Request.model_validate(request_body())
    body = entry_body(2)
    body.pop("event_id")
    body.pop("created_at")
    native = JournalEntry.model_validate(body)
    assert "event_id" not in native.model_fields_set and "created_at" not in native.model_fields_set
    page = Page.model_validate(
        {**page_body(entries=[native], next_after=None, exhausted=True), "request": original}
    )
    assert page.entries[0].event_id == native.event_id
    assert page.entries[0].created_at == native.created_at


def test_typed_legacy_entry_fields_set_may_not_contain_foreign_or_aliased_names():
    _, Page = kinds()

    class Alias(str):
        pass

    for member in ("foreign", Alias("event_id"), 1):
        page = Page.model_validate(page_body())
        entry = copy.deepcopy(page.entries[0])
        entry.__pydantic_fields_set__ = {member}
        forged = page.model_copy(update={"entries": [entry, page.entries[1]]})
        with pytest.raises(ValueError):
            Page.model_validate(forged)


def test_duplicate_keys_at_top_and_inside_entry_are_refused():
    Request, Page = kinds()
    with pytest.raises(ValueError):
        Request.from_json(b'{"epoch":1,' + wire(request_body())[1:])
    raw = wire(page_body()).replace(b'"text":', b'"text":"ambiguous","text":', 1)
    with pytest.raises(ValueError):
        Page.from_json(raw)


def test_public_parser_errors_do_not_echo_input_or_chain_private_causes():
    Request, _ = kinds()
    errors = []
    for secret in ("SYNTHETIC_SENTINEL_ALPHA", "SYNTHETIC_SENTINEL_BETA"):
        with pytest.raises(ValueError) as caught:
            Request.from_json(wire(request_body(epoch=secret)))
        assert secret not in str(caught.value)
        assert caught.value.__cause__ is None
        errors.append(str(caught.value))
    assert errors[0] == errors[1]


def test_legitimate_20000_scalar_metadata_and_nested_native_values_roundtrip_in_full():
    _, Page = kinds()
    body = page_body()
    body["entries"][0]["payload"]["native_scalars"] = [0] * 20000
    nested = "synthetic-leaf"
    for _ in range(80):
        nested = [nested]
    body["entries"][0]["payload"]["nested"] = nested
    assert len(wire(body)) < 262144
    page = Page.from_json(wire(body))
    assert page.entries[0].payload["native_scalars"] == [0] * 20000
    assert page.entries[0].payload["nested"] == nested
    assert Page.from_json(wire(page.model_dump(mode="json"))) == page


def test_isolated_scan_depth_and_node_overflow_refuse_without_recursive_failure():
    _, Page = kinds()
    nested = 0
    for _ in range(600):
        nested = [nested]
    body = page_body()
    body["entries"][0]["payload"]["too_deep"] = nested
    with pytest.raises(ValueError):
        Page.from_json(wire(body))
    page = Page.model_validate(page_body())
    entry = page.entries[0].model_copy(update={"payload": {"nodes": [0] * 262145}})
    with pytest.raises(ValueError):
        Page.model_validate(page.model_copy(update={"entries": [entry, page.entries[1]]}))
