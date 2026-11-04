"""Bounded blueprint comparison and the intentional raw-description16k limit.

Authored against frozen2dbefd41 after requirement freeze; prospective source and
Muse output were not read. Existing fixtures supply synthetic protocol metadata,
not native authority, provider readiness, model output or physical cleanup.
Only baseline symbols are imported so the original source can collect this file.
"""

import copy
import dataclasses
import hashlib
import json
import math
import sys
from contextlib import contextmanager
from types import BuiltinMethodType

import pytest
from test_decisions_media_classifier import _consumer, _fence, _ranges, _source
from test_llama_media import _model_for_mode, _spec_for_mode

from packages.providers import llama_media
from packages.providers.laya import MAX_TEXT_CHARS
from packages.providers.llama_media import LlamaMediaProtocolError, build_request
from packages.providers.text import ProviderRefusal
from services.decisions.media_classifier import project_media_state

_MODES = ("converse_chat", "decisions_systemone")


def _prepared(mode, **extra):
    return build_request(
        {"mode": mode, "model": _model_for_mode(mode), **_spec_for_mode(mode), **extra}
    )


def _validate(value):
    return llama_media._validate_prepared_request(
        value, expected_mode=value.mode, expected_model=value.model
    )


def _encoder_observation(monkeypatch, *independent_graphs):
    original = llama_media._compact_json
    forbidden_calls = []

    def observe(value):
        if any(value is graph for graph in independent_graphs):
            forbidden_calls.append("independent-graph")
        return original(value)

    monkeypatch.setattr(llama_media, "_compact_json", observe)
    return forbidden_calls


def _reject_before_independent_encoding(monkeypatch, prepared, body):
    calls = _encoder_observation(monkeypatch, body)
    error = None
    try:
        _validate(dataclasses.replace(prepared, body=body))
    except Exception as caught:
        error = caught
    # Check the actual ordering witness BEFORE the final rejection taxonomy.
    assert calls == [], "Independent caller graph reached the JSON encoder"
    assert isinstance(error, LlamaMediaProtocolError)


def _media_leaf(body, mode, replacement):
    if mode == "converse_chat":
        body["messages"][-1]["content"][0]["image_url"]["url"] = replacement
    else:
        body["files"][0] = replacement


@pytest.mark.parametrize("mode", _MODES)
def test_valid_prepared_body_uses_bounded_raw_blueprint_and_returns_detached_json(mode):
    prepared = _prepared(mode)
    body = copy.deepcopy(prepared.body)
    validated = _validate(dataclasses.replace(prepared, body=body))
    assert validated.body == json.loads(prepared.raw_body)
    assert validated.raw_body == prepared.raw_body
    assert validated.body is not body
    _media_leaf(body, mode, "caller mutation after validation")
    assert validated.body == json.loads(prepared.raw_body)
    body.clear()
    assert validated.body == json.loads(prepared.raw_body)


@pytest.mark.parametrize("mode", _MODES)
def test_small_valid_raw_rejects_oversized_independent_leaf_before_encoding(mode, monkeypatch):
    prepared = _prepared(mode)
    assert len(prepared.raw_body) < 8192  # Fixture size, not a new protocol limit.
    body = copy.deepcopy(prepared.body)
    # Finite1MiB synthetic mismatch; no32MiB allocation benchmark is needed.
    _media_leaf(body, mode, "x" * (1024 * 1024))
    _reject_before_independent_encoding(monkeypatch, prepared, body)


@pytest.mark.parametrize("mode", _MODES)
def test_nested_cycle_is_rejected_by_raw_blueprint_without_encoding_it(mode, monkeypatch):
    prepared = _prepared(mode)
    body = copy.deepcopy(prepared.body)
    _media_leaf(body, mode, body)
    _reject_before_independent_encoding(monkeypatch, prepared, body)


class _EqualityObject:
    def __init__(self, touches):
        self.touches = touches

    def __eq__(self, other):
        self.touches.append("object equality")
        raise AssertionError("Caller equality hook was invoked")


class _EqualityString(str):
    def __new__(cls, value, touches):
        instance = super().__new__(cls, value)
        instance.touches = touches
        return instance

    def __eq__(self, other):
        self.touches.append("string subclass equality")
        raise AssertionError("Caller equality hook was invoked")


@pytest.mark.parametrize("kind", ("object", "str-subclass"))
def test_nested_nonbuiltin_values_are_closed_before_encoding_or_equality(kind, monkeypatch):
    prepared = _prepared("converse_chat")
    body = copy.deepcopy(prepared.body)
    original = body["messages"][-1]["content"][0]["image_url"]["url"]
    touches = []
    replacement = (
        _EqualityObject(touches) if kind == "object" else _EqualityString(original, touches)
    )
    _media_leaf(body, "converse_chat", replacement)
    _reject_before_independent_encoding(monkeypatch, prepared, body)
    assert touches == []


def test_body_dictionary_order_is_checked_recursively_without_encoding_caller(monkeypatch):
    prepared = _prepared("converse_chat")
    body = copy.deepcopy(prepared.body)
    user = body["messages"][-1]
    body["messages"][-1] = dict(reversed(tuple(user.items())))
    assert tuple(body["messages"][-1]) != tuple(user)
    _reject_before_independent_encoding(monkeypatch, prepared, body)


def test_float_signed_zero_in_independent_body_is_not_equal_to_raw_positive_zero(monkeypatch):
    prepared = _prepared("converse_chat", temperature=0.0)
    assert b'"temperature":0.0' in prepared.raw_body
    body = copy.deepcopy(prepared.body)
    body["temperature"] = -0.0
    assert math.copysign(1.0, body["temperature"]) == -1.0
    _reject_before_independent_encoding(monkeypatch, prepared, body)


def _reverse_objects(value):
    if type(value) is dict:
        return {key: _reverse_objects(value[key]) for key in reversed(value)}
    if type(value) is list:
        return [_reverse_objects(item) for item in value]
    return value


def test_systemone_independent_questions_keep_recursive_unordered_matching():
    prepared = _prepared("decisions_systemone")
    body = copy.deepcopy(prepared.body)
    questions = _reverse_objects(prepared.questions)
    assert tuple(questions) != tuple(prepared.questions)
    assert tuple(questions["route"]["criteria"]) != tuple(prepared.questions["route"]["criteria"])
    validated = _validate(dataclasses.replace(prepared, body=body, questions=questions))
    expected = json.loads(prepared.raw_body)["questions"]
    assert validated.questions == expected
    questions["route"]["criteria"].clear()
    assert validated.questions == expected


def test_raw_canonicality_still_refuses_whitespace_with_matching_raw_digest(monkeypatch):
    prepared = _prepared("converse_chat")
    raw = b" " + prepared.raw_body
    body = copy.deepcopy(prepared.body)
    changed = dataclasses.replace(
        prepared, raw_body=raw, request_sha256=hashlib.sha256(raw).hexdigest()
    )
    _reject_before_independent_encoding(monkeypatch, changed, body)


def _project(description):
    return project_media_state(
        purpose="converse_live",
        allowed_operation="live_routing",
        speaker_posture="unknown_speech",
        source=_source(),
        fence=_fence(),
        consumer=_consumer(),
        ranges=_ranges(),
        tasks=(
            {
                "task_id": "before",
                "objective_revision": 1,
                "state": "running",
                "description": "Before",
            },
            {
                "task_id": "target",
                "objective_revision": 2,
                "state": "queued",
                "description": description,
            },
            {
                "task_id": "after",
                "objective_revision": 3,
                "state": "queued",
                "description": "After",
            },
        ),
    )


def _assert_task_associations(projected, expected):
    assert tuple(t.task_id for t in projected.tasks) == ("before", "target", "after")
    assert tuple(t.objective_revision for t in projected.tasks) == (1, 2, 3)
    assert tuple(t.description for t in projected.tasks) == ("Before", expected, "After")


def test_description_none_empty_and_ordinary_whitespace_keep_old_projection_semantics():
    for description, expected in (
        (None, ""),
        ("", ""),
        (" \t\n ", ""),
        ("  Draft\n\t the   table  ", "Draft the table"),
    ):
        _assert_task_associations(_project(description), expected)


def test_description_exact_raw16000_whitespace_heavy_input_preserves360_prefix():
    assert MAX_TEXT_CHARS == 16000
    description = "a " * 8000
    assert len(description) == MAX_TEXT_CHARS
    _assert_task_associations(_project(description), "a " * 180)


@contextmanager
def _actual_raw_split_calls(raw):
    previous = sys.getprofile()
    calls = []
    probe = "profile calibration"
    calibrated = []

    def observe(frame, event, function):
        if (
            event == "c_call"
            and type(function) is BuiltinMethodType
            and function.__name__ == "split"
        ):
            if function.__self__ is raw:
                calls.append("raw.split")
            elif function.__self__ is probe:
                calibrated.append("probe.split")
        if previous is not None:
            previous(frame, event, function)

    sys.setprofile(observe)
    try:
        probe.split()
        assert calibrated == ["probe.split"]
        yield calls
    finally:
        sys.setprofile(previous)


def test_over_raw_description_limit_refuses_whole_projection_before_actual_split():
    description = "a " * 8000 + "a"
    assert len(description) == MAX_TEXT_CHARS + 1
    previous = sys.getprofile()
    error = None
    projected = None
    with _actual_raw_split_calls(description) as calls:
        try:
            projected = _project(description)
        except ProviderRefusal as caught:
            error = caught
    assert sys.getprofile() is previous
    # The original source splits/truncates this input and therefore fails HERE,
    # rather than only failing a later expectation of a final refusal.
    assert calls == [], "Over-limit raw task description was split before refusal"
    assert isinstance(error, ProviderRefusal)
    assert projected is None


def test_within_raw_limit_invalid_suffix_discarded_after360_remains_ignored():
    description = "x" * 360 + "\ud800\x00"
    assert len(description) < MAX_TEXT_CHARS
    _assert_task_associations(_project(description), "x" * 360)
