"""Constant diagnostics preserve the existing bounded JSON decoder contract."""

import json
import math

import pytest

from packages.providers.whistle_ipc import JsonProtocolError, decode_json, encode_json

MESSAGES = {
    "size_limit": "Whistle IPC message exceeds its bound",
    "duplicate_key": "Duplicate IPC field",
    "non_finite": "Non-finite IPC number",
    "nesting_limit": "Whistle IPC nesting exceeds its bound",
    "not_object": "Whistle IPC requires an object",
}


@pytest.mark.parametrize("reason", tuple(MESSAGES))
def test_tagged_error_preserves_value_error_and_exact_fixed_message(reason):
    error = JsonProtocolError(reason)
    assert isinstance(error, ValueError)
    assert error.reason == reason
    assert error.args == (MESSAGES[reason],)
    assert str(error) == MESSAGES[reason]
    assert repr(error) == f"JsonProtocolError({MESSAGES[reason]!r})"
    assert vars(error) == {"reason": reason}


@pytest.mark.parametrize("reason", [None, True, 1, [], {}, "", "syntax", "SYNTHETIC_SECRET_MARKER"])
def test_unknown_constructor_reasons_are_rejected_without_reflection(reason):
    with pytest.raises(ValueError) as caught:
        JsonProtocolError(reason)
    assert type(caught.value) is ValueError
    assert caught.value.args == ("Unknown JSON protocol rejection reason",)
    assert vars(caught.value) == {}
    assert "SYNTHETIC_SECRET_MARKER" not in repr(caught.value)


@pytest.mark.parametrize(
    "raw,limit", [(b"", 4096), (b"{}", 1), ("{}", 4096), (None, 4096), (bytearray(b"{}"), 4096)]
)
def test_existing_byte_or_size_rejections_have_size_limit_tag(raw, limit):
    with pytest.raises(JsonProtocolError) as caught:
        decode_json(raw, limit)
    assert caught.value.reason == "size_limit"
    assert str(caught.value) == MESSAGES["size_limit"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"SYNTHETIC_KEY_MARKER":1,"SYNTHETIC_KEY_MARKER":2}',
        b'{"nested":[{"SYNTHETIC_KEY_MARKER":1,"SYNTHETIC_KEY_MARKER":2}]}',
    ],
)
def test_duplicate_tag_never_carries_the_key_or_values(raw):
    with pytest.raises(JsonProtocolError) as caught:
        decode_json(raw)
    error = caught.value
    assert error.reason == "duplicate_key"
    assert str(error) == MESSAGES["duplicate_key"]
    assert "SYNTHETIC_KEY_MARKER" not in repr(error)
    assert vars(error) == {"reason": "duplicate_key"}


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("nested", [False, True])
def test_existing_constant_rejections_have_non_finite_tag(number, nested):
    raw = ('{"value":[' + number + "]}" if nested else number).encode()
    with pytest.raises(JsonProtocolError) as caught:
        decode_json(raw)
    assert caught.value.reason == "non_finite"
    assert str(caught.value) == MESSAGES["non_finite"]
    assert vars(caught.value) == {"reason": "non_finite"}


def test_existing_interpreter_nesting_rejection_has_nesting_limit_tag():
    depth = 10000
    raw = b'{"nested":' + b"[" * depth + b"0" + b"]" * depth + b"}"
    with pytest.raises(RecursionError):
        json.loads(raw)
    with pytest.raises(JsonProtocolError) as caught:
        decode_json(raw, len(raw))
    assert caught.value.reason == "nesting_limit"
    assert str(caught.value) == MESSAGES["nesting_limit"]
    assert isinstance(caught.value.__cause__, RecursionError)


@pytest.mark.parametrize(
    "raw", [b"[]", b"null", b"true", b"false", b"0", b'"SYNTHETIC_VALUE_MARKER"', b"1e999"]
)
def test_existing_non_object_rejections_have_not_object_tag(raw):
    with pytest.raises(JsonProtocolError) as caught:
        decode_json(raw)
    assert caught.value.reason == "not_object"
    assert str(caught.value) == MESSAGES["not_object"]
    assert "SYNTHETIC_VALUE_MARKER" not in repr(caught.value)


@pytest.mark.parametrize("raw", [b"{", b'{"key":}', b"{} trailing", b"invalid"])
def test_syntax_errors_keep_the_original_json_decode_error_type(raw):
    with pytest.raises(json.JSONDecodeError) as caught:
        decode_json(raw)
    assert type(caught.value) is json.JSONDecodeError
    assert not isinstance(caught.value, JsonProtocolError)


@pytest.mark.parametrize("raw", [b"\xff", b'{"text":"\xed\xa0\x80"}'])
def test_invalid_utf8_keeps_the_original_unicode_decode_error_type(raw):
    with pytest.raises(UnicodeDecodeError) as caught:
        decode_json(raw)
    assert type(caught.value) is UnicodeDecodeError
    assert not isinstance(caught.value, JsonProtocolError)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"nested": [None, True, False, 1, -2, 1.25, {"text": "Public synthetic 🌱"}]},
        {"a": {"key": 1}, "b": {"key": 2}},
    ],
)
def test_valid_objects_still_round_trip_at_the_exact_byte_bound(value):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    assert decode_json(raw, len(raw)) == value
    assert decode_json(encode_json(value)) == value
    with pytest.raises(JsonProtocolError) as caught:
        decode_json(raw, len(raw) - 1)
    assert caught.value.reason == "size_limit"


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
def test_exponent_overflow_inside_objects_keeps_existing_decode_behavior(number):
    result = decode_json(('{"nested":[' + number + "]}").encode())
    assert math.isinf(result["nested"][0])
    assert (result["nested"][0] < 0) == number.startswith("-")


def test_decoder_does_not_add_unicode_numeric_or_tree_validation():
    assert decode_json(b'{"text":"\\ud800"}') == {"text": "\ud800"}
    number = "9" * 1000
    assert decode_json(('{"integer":' + number + "}").encode())["integer"] == int(number)
    value = {"values": [0] * 10000}
    raw = json.dumps(value).encode()
    assert decode_json(raw, len(raw)) == value


def test_other_helpers_and_existing_limit_type_behavior_are_unchanged():
    with pytest.raises(ValueError) as caught:
        encode_json({}, limit=1)
    assert type(caught.value) is ValueError
    assert str(caught.value) == MESSAGES["size_limit"]
    with pytest.raises(TypeError):
        decode_json(b"{}", limit=None)
    assert decode_json(b"{}", limit=2.5) == {}
