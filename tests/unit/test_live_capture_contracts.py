"""Independent capture wire pin and refusal of forged permission/stop claims."""

import hashlib
import json
from pathlib import Path

import pytest

from packages.contracts.live_captures import (
    PROTOCOL,
    LiveCaptureBackend,
    LiveCaptureRegistration,
    LiveCaptureState,
    capture_json,
)

NAMES = {
    "LiveCaptureBackend",
    "LiveCaptureRegistration",
    "LiveCaptureReceipt",
    "LiveCaptureFence",
    "LiveCaptureFenceReceipt",
    "LiveCaptureState",
    "LiveCaptureSourceState",
}


def request():
    return LiveCaptureRegistration(
        schema_version="2.0",
        protocol=PROTOCOL,
        backend=LiveCaptureBackend(
            schema_version="2.0", protocol=PROTOCOL, backend_id="a" * 32, controller_id="b" * 32
        ),
        owner_instance="c" * 32,
        capture_id="d" * 32,
        session_id="public-session",
        thread_id="public-thread",
        epoch=1,
    )


def test_independent_schema_bytes_and_closed_required_native_roots():
    root = Path(__file__).resolve().parents[2] / "packages/contracts/schemas"
    body = b"".join(
        (name + ".json").encode() + b"\0" + (root / (name + ".json")).read_bytes() + b"\0"
        for name in sorted(NAMES)
    )
    assert (
        hashlib.sha256(body).hexdigest()
        == "80eff6408bf5a3562cf4a4c05f8e9fdc7c50e73447010e2d1aea287f91d52dfc"
    )
    for name in NAMES:
        schema = json.loads((root / (name + ".json")).read_bytes())
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["properties"]["schema_version"]["const"] == "2.0"


@pytest.mark.parametrize(
    "change",
    [
        {"epoch": True},
        {"epoch": 1.0},
        {"epoch": 0},
        {"schema_version": "1.0"},
        {"all_work_stopped": True},
        {"provider_authorized": True},
    ],
)
def test_capture_request_cannot_coerce_scope_or_assert_physical_and_provider_authority(change):
    with pytest.raises(ValueError):
        LiveCaptureRegistration.model_validate(request().model_dump(mode="json") | change)


def test_missing_receipt_is_unknown_and_cannot_be_reported_open():
    original = request()
    body = dict(
        schema_version="2.0",
        protocol=PROTOCOL,
        original=original.model_dump(mode="json"),
        backend=original.backend.model_dump(mode="json"),
        receipt=None,
        fence=None,
        scope_current=True,
        capture_open=False,
    )
    assert LiveCaptureState.model_validate(body).receipt is None
    with pytest.raises(ValueError):
        LiveCaptureState.model_validate(body | {"capture_open": True})


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":1.0}',
        b" " * 4097,
        b'{"x":' + b"[" * 10 + b"1" + b"]" * 10 + b"}",
    ],
)
def test_capture_parser_bounds_unknown_untrusted_wire_data(raw):
    with pytest.raises(ValueError):
        capture_json(raw)
