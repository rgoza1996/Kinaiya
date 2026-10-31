"""Issue #169: bounded constant Meta decode diagnostics, synthetic HTTP only."""

import json
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.agent_tools import agent_tool_scope, native_tool_snapshot
from packages.providers import whistle_ipc
from packages.providers.text import ActionDecodeError, MetaResponsesProvider, ProviderError

MARKER = "SYNTHETIC_PRIVATE_PAYLOAD_169"
UI_LIMIT = 48 * 1024
WIRE_LIMIT = 6 * UI_LIMIT + 8192
JSON_REASONS = (
    "text_type",
    "size_limit",
    "json_syntax",
    "duplicate_key",
    "non_finite",
    "invalid_unicode",
    "nesting_limit",
    "not_object",
    "decode_limit",
)
COMMON_MESSAGES = {
    "ui_arguments": "UI publication arguments require a bounded UTF-8 JSON object",
    "ui_wire": "UI publication wire fields require a bounded UTF-8 JSON object",
    "unavailable_action": "Unavailable native tool response require a bounded UTF-8 JSON object",
}
SPECIAL_MESSAGES = {
    ("ui_arguments", "field_shape"): (
        "UI publication arguments require exactly the create or update fields"
    ),
    ("ui_wire", "field_shape"): "UI publication requires only tool and arguments wire fields",
    ("ui_wire", "unknown_fields"): "UI publication forbids undeclared wire fields",
    ("ui_wire", "argument_fields"): "UI publication requires only the ui_spec_json argument",
    ("ui_wire", "unexpected_ui_field"): "ui_spec_json is only valid for publish_ui",
    ("unavailable_action", "field_shape"): "Unavailable tool response has undeclared fields",
}
PAIRS = tuple((stage, reason) for stage in COMMON_MESSAGES for reason in JSON_REASONS) + tuple(
    SPECIAL_MESSAGES
)
OPTIONAL_TOOLS = (
    "select_context",
    "mcp_list",
    "mcp_call",
    "skills_list",
    "skill_load",
    "plugins_list",
    "plugin_call",
    "publish_ui",
)


def spec():
    return {
        "title": "Synthetic UI 🌱",
        "summary": "Explicitly simulated fixture.",
        "components": [{"id": "value", "type": "text", "data": {"text": "Synthetic 42"}}],
    }


def encoded_spec():
    return json.dumps(spec(), ensure_ascii=False, separators=(",", ":"))


def wire(body, encoded, *, tool="publish_ui", extra=None, top=None):
    fields = body["text"]["format"]["schema"]["properties"]["arguments"]["properties"]
    args = {**dict.fromkeys(fields), "ui_spec_json": encoded, **(extra or {})}
    return json.dumps({"tool": tool, "arguments": args, **(top or {})}, ensure_ascii=True)


@pytest.fixture
async def exchange():
    state = SimpleNamespace(requests=[], reply=None)

    def send(request):
        assert request.method == "POST"
        assert str(request.url) == "https://synthetic.invalid/v1/responses"
        body = json.loads(request.content)
        state.requests.append(body)
        raw = state.reply(body) if callable(state.reply) else state.reply
        assert type(raw) is str
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": raw}],
                    }
                ],
            },
        )

    state.provider = MetaResponsesProvider(
        base_url="https://synthetic.invalid/v1",
        api_key="synthetic-fixture-key",
        allow_cloud=True,
        structured_actions=True,
        transport=httpx.MockTransport(send),
    )
    try:
        yield state
    finally:
        await state.provider.close()


async def request(exchange, reply, *, snapshot=None):
    exchange.reply = reply
    before = len(exchange.requests)
    try:
        with agent_tool_scope(
            snapshot if snapshot is not None else native_tool_snapshot(artifacts_available=True)
        ):
            return await exchange.provider.complete(
                [{"role": "user", "content": "Public synthetic decode fixture"}]
            )
    finally:
        assert len(exchange.requests) == before + 1, "Decode failures must not retry inference"


def assert_diagnostic(error, stage, reason):
    assert isinstance(error, ProviderError)
    assert error.stage == stage and error.reason == reason
    expected = {"stage": stage, "reason": reason}
    assert error.diagnostic == expected
    assert str(error) == SPECIAL_MESSAGES.get((stage, reason), COMMON_MESSAGES[stage])
    first = error.diagnostic
    first.update(stage=MARKER, reason=MARKER, raw=MARKER)
    assert error.diagnostic == expected
    assert first is not error.diagnostic
    native_facing = json.dumps({"error": str(error), "diagnostic": error.diagnostic})
    assert MARKER not in native_facing
    for leaked in ("JSONDecodeError", "UnicodeEncodeError", "ValueError(", "line 1 column"):
        assert leaked not in native_facing
    assert len(native_facing) < 300


async def rejected(exchange, reply, stage, reason, *, snapshot=None):
    with pytest.raises(ActionDecodeError) as raised:
        await request(exchange, reply, snapshot=snapshot)
    assert_diagnostic(raised.value, stage, reason)


@pytest.mark.parametrize("stage,reason", PAIRS)
def test_diagnostic_pairs_are_constant_readonly_and_return_fresh_data(stage, reason):
    error = ActionDecodeError(stage, reason)
    assert_diagnostic(error, stage, reason)
    for attribute in ("stage", "reason", "diagnostic"):
        with pytest.raises(AttributeError):
            setattr(error, attribute, MARKER)
    assert error.diagnostic == {"stage": stage, "reason": reason}


class StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "stage,reason",
    [
        (MARKER, "field_shape"),
        ("ui_arguments", MARKER),
        ("ui_arguments", "unknown_fields"),
        ("unavailable_action", "unexpected_ui_field"),
        (None, "field_shape"),
        (True, "field_shape"),
        ({MARKER: MARKER}, "field_shape"),
        ("ui_wire", [MARKER]),
        ("ui_wire", 1),
        (StringSubclass("ui_arguments"), "field_shape"),
        ("ui_arguments", StringSubclass("field_shape")),
    ],
)
def test_unknown_or_coerced_diagnostic_pairs_cannot_echo_supplied_values(stage, reason):
    with pytest.raises(ValueError) as raised:
        ActionDecodeError(stage, reason)
    assert str(raised.value) == "Unknown structured action diagnostic"
    assert MARKER not in str(raised.value)


@pytest.mark.parametrize("encoded", [False, 0, 1, [], {MARKER: MARKER}])
async def test_inner_non_text_ui_argument_has_a_typed_constant_reason(exchange, encoded):
    await rejected(exchange, lambda body: wire(body, encoded), "ui_arguments", "text_type")


@pytest.mark.parametrize(
    "encoded,reason",
    [
        pytest.param("", "size_limit", id="empty"),
        pytest.param(" ", "json_syntax", id="whitespace"),
        pytest.param('{"' + MARKER + '":', "json_syntax", id="syntax"),
        pytest.param(
            '{"title":"' + MARKER + '","title":"duplicate","summary":"S","components":[]}',
            "duplicate_key",
            id="duplicate-root-key",
        ),
        pytest.param(
            '{"title":"T","summary":"S","components":[{"' + MARKER + '":1,"' + MARKER + '":2}]}',
            "duplicate_key",
            id="duplicate-nested-key",
        ),
        pytest.param('{"' + MARKER + '":NaN}', "non_finite", id="nan"),
        pytest.param('{"' + MARKER + '":Infinity}', "non_finite", id="infinity"),
        pytest.param('{"' + MARKER + '":1e999}', "non_finite", id="exponent-overflow"),
        pytest.param('{"' + MARKER + '":"\\ud800"}', "invalid_unicode", id="escaped-surrogate"),
        pytest.param(
            '{"components":' + "[" * 10000 + "0" + "]" * 10000 + "}",
            "nesting_limit",
            id="actual-nesting-limit",
        ),
        pytest.param("[]", "not_object", id="array"),
        pytest.param("null", "not_object", id="null-json"),
        pytest.param("true", "not_object", id="boolean-json"),
        pytest.param("42", "not_object", id="number-json"),
        pytest.param(json.dumps(MARKER), "not_object", id="string-json"),
        pytest.param(json.dumps(encoded_spec()), "not_object", id="double-encoded-object"),
        pytest.param(
            json.dumps({**spec(), MARKER: MARKER}), "field_shape", id="unknown-logical-field"
        ),
        pytest.param(
            json.dumps({**spec(), "artifact_id": MARKER}), "field_shape", id="mixed-create-update"
        ),
        pytest.param(json.dumps({"title": MARKER}), "field_shape", id="incomplete-create"),
        pytest.param(
            json.dumps(
                {"artifact_id": MARKER, "expected_revision": 1, "updates": [], "title": "T"}
            ),
            "field_shape",
            id="mixed-update-create",
        ),
        pytest.param(
            json.dumps({**spec(), "summary": MARKER + "🌱" * 13000}, ensure_ascii=False),
            "size_limit",
            id="utf8-byte-overflow",
        ),
    ],
)
async def test_inner_ui_decode_reasons_preserve_original_prefix_and_redact_payload(
    exchange, encoded, reason
):
    await rejected(exchange, lambda body: wire(body, encoded), "ui_arguments", reason)


@pytest.mark.parametrize("missing", [False, True])
async def test_null_or_missing_ui_text_is_a_wire_argument_failure(exchange, missing):
    def reply(body):
        value = json.loads(wire(body, None))
        if missing:
            value["arguments"].pop("ui_spec_json")
        return json.dumps(value)

    await rejected(exchange, reply, "ui_wire", "argument_fields")


@pytest.mark.parametrize("field", ["path", "summary", "objective_revision"])
async def test_extra_active_known_argument_is_a_wire_failure(exchange, field):
    value = 1 if field == "objective_revision" else MARKER
    await rejected(
        exchange,
        lambda body: wire(body, encoded_spec(), extra={field: value}),
        "ui_wire",
        "argument_fields",
    )


@pytest.mark.parametrize("value", [None, MARKER])
async def test_unknown_wire_argument_does_not_leak_its_key_or_value(exchange, value):
    await rejected(
        exchange,
        lambda body: wire(body, encoded_spec(), extra={MARKER: value}),
        "ui_wire",
        "unknown_fields",
    )


async def test_native_metadata_is_not_a_top_level_ui_wire_field(exchange):
    await rejected(
        exchange,
        lambda body: wire(body, encoded_spec(), top={MARKER: MARKER}),
        "ui_wire",
        "field_shape",
    )


async def test_literal_surrogate_in_inner_text_is_rejected_at_the_earlier_wire_boundary(exchange):
    # Escaped inner JSON survives this boundary and is covered above. An
    # actual surrogate in the wire string is invalid UTF-8 before inner decode.
    await rejected(
        exchange,
        lambda body: wire(body, '{"' + MARKER + '":"\ud800"}'),
        "ui_wire",
        "invalid_unicode",
    )


@pytest.mark.parametrize("duplicate", ["tool", "ui_spec_json"])
async def test_wire_duplicate_fields_are_distinct_from_inner_json_duplicates(exchange, duplicate):
    encoded = json.dumps(encoded_spec())
    if duplicate == "tool":
        raw = (
            '{"tool":"publish_ui","tool":"publish_ui","arguments":{"ui_spec_json":' + encoded + "}}"
        )
    else:
        raw = (
            '{"tool":"publish_ui","arguments":{"ui_spec_json":'
            + encoded
            + ',"ui_spec_json":'
            + json.dumps(MARKER)
            + "}}"
        )
    await rejected(exchange, raw, "ui_wire", "duplicate_key")


async def test_nonpublication_with_ui_text_has_a_wire_only_diagnostic(exchange):
    await rejected(
        exchange,
        lambda body: wire(
            body, encoded_spec(), tool="write_file", extra={"path": MARKER, "content": MARKER}
        ),
        "ui_wire",
        "unexpected_ui_field",
    )


@pytest.mark.parametrize("tool", OPTIONAL_TOOLS)
async def test_base_snapshot_malformed_unavailable_envelope_never_decodes_arguments(exchange, tool):
    await rejected(
        exchange,
        json.dumps({"tool": tool, "arguments": {"ui_spec_json": MARKER}, MARKER: MARKER}),
        "unavailable_action",
        "field_shape",
        snapshot=native_tool_snapshot(),
    )


@pytest.mark.parametrize(
    "raw,reason",
    [
        (
            '{"tool":"publish_ui","tool":"publish_ui","arguments":{"' + MARKER + '":1}}',
            "duplicate_key",
        ),
        ('{"tool":"publish_ui","arguments":{"' + MARKER + '":NaN}}', "non_finite"),
        ('{"tool":"publish_ui","arguments":{"' + MARKER + '":1e999}}', "non_finite"),
        ('{"tool":"publish_ui","arguments":{"' + MARKER + '":"\\ud800"}}', "invalid_unicode"),
        (json.dumps({"tool": "publish_ui", "arguments": {MARKER: "x" * WIRE_LIMIT}}), "size_limit"),
    ],
    ids=["duplicate-key", "nan", "exponent-overflow", "surrogate", "size-overflow"],
)
async def test_unavailable_envelope_json_errors_keep_the_unavailable_stage(exchange, raw, reason):
    await rejected(exchange, raw, "unavailable_action", reason, snapshot=native_tool_snapshot())


@pytest.mark.parametrize("tool", OPTIONAL_TOOLS)
async def test_closed_unavailable_response_still_only_returns_the_native_refusal_name(
    exchange, tool
):
    result = await request(
        exchange,
        json.dumps({"tool": tool, "arguments": {"ui_spec_json": MARKER}}),
        snapshot=native_tool_snapshot(),
    )
    assert json.loads(result) == {"tool": tool, "arguments": {}}
    assert MARKER not in result


@pytest.mark.parametrize("stage", ["ui_arguments", "ui_wire", "unavailable_action"])
async def test_generic_decoder_failure_maps_to_constant_decode_limit(exchange, monkeypatch, stage):
    decode = whistle_ipc.decode_json
    failed = []
    inner = encoded_spec()

    def reply(body):
        raw = wire(body, inner)
        target = inner.encode("utf-8") if stage == "ui_arguments" else raw.encode("utf-8")

        def fail_selected(value, limit):
            if value == target:
                failed.append(True)
                raise ValueError(MARKER + " parser repr at line 1 column 17")
            return decode(value, limit)

        monkeypatch.setattr(whistle_ipc, "decode_json", fail_selected)
        return raw

    await rejected(
        exchange,
        reply,
        stage,
        "decode_limit",
        snapshot=native_tool_snapshot() if stage == "unavailable_action" else None,
    )
    assert failed == [True]


@pytest.mark.parametrize(
    "value",
    [
        spec(),
        {**spec(), "layout": "grid"},
        {
            "artifact_id": "synthetic-native-artifact",
            "expected_revision": 1,
            "updates": [{"id": "value", "data": {"text": "Synthetic update"}}],
        },
    ],
)
async def test_valid_create_and_update_are_unchanged(exchange, value):
    result = await request(exchange, lambda body: wire(body, json.dumps(value, ensure_ascii=False)))
    assert json.loads(result) == {"tool": "publish_ui", "arguments": value}


async def test_exact_utf8_byte_boundary_still_decodes_and_one_more_byte_is_typed(exchange):
    encoded = encoded_spec()
    encoded += " " * (UI_LIMIT - len(encoded.encode("utf-8")))
    assert len(encoded.encode("utf-8")) == UI_LIMIT
    assert len(encoded) < UI_LIMIT
    result = await request(exchange, lambda body: wire(body, encoded))
    assert json.loads(result) == {"tool": "publish_ui", "arguments": spec()}
    await rejected(exchange, lambda body: wire(body, encoded + " "), "ui_arguments", "size_limit")
