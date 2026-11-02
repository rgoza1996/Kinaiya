"""Persisted confidence must not broaden other catalog JSON number permissions."""

import json

import pytest

from services.settings.provider_catalog import _stored_json


@pytest.mark.parametrize("confidence", [0.0, -0.0, 1.0, 0.6, 1e-7])
def test_only_native_profile_confidence_round_trips_fractional_json(confidence):
    value = {"kind": "native_d1", "min_confidence": confidence}
    assert _stored_json(json.dumps(value), 8192, profile_config=True) == value
    with pytest.raises(ValueError):
        _stored_json(json.dumps(value), 8192)


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "laya", "min_confidence": 0.6},
        {"kind": "compatible", "timeout_s": 4.0},
        {"kind": "native_d1", "timeout_s": 4.0},
        {"kind": "native_d1", "credential": {"min_confidence": 0.6}},
        {"kind": "native_d1", "min_confidence": {"value": 0.6}},
        {"kind": "native_d1", "min_confidence": [0.6]},
        {"nested": {"kind": "native_d1", "min_confidence": 0.6}},
        [{"kind": "native_d1", "min_confidence": 0.6}],
    ],
)
def test_fractional_permission_does_not_escape_exact_profile_field(value):
    with pytest.raises(ValueError):
        _stored_json(json.dumps(value), 8192, profile_config=True)


@pytest.mark.parametrize("number", ["-0.1", "1.1", "1e999", "NaN", "Infinity", "-Infinity"])
def test_persisted_native_confidence_rejects_out_of_range_or_nonfinite_number(number):
    raw = '{"kind":"native_d1","min_confidence":' + number + "}"
    with pytest.raises(ValueError):
        _stored_json(raw, 8192, profile_config=True)
