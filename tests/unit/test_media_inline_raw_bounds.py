"""Source-informed regression for raw JSON limits after the initial DTO seal.

Root and its independent reviewer reported that a large-root allowance could
bypass the actual ordinary cap when an Anchor/Receipt/Found has no inline task.
The new production bodies remain unread by this author. Synthetic request and
receipt references below test metadata grammar, never native authority/proof.
"""

import base64
import hashlib
import json

import pytest

from packages.contracts import media

_TARGETS = ("proof_anchor", "proof_receipt", "not_found", "inline_anchor")


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _anchor(*, inline):
    # Original TaskRequest may omit its unchanged defaulted fields. Hash the
    # original canonical request bytes, never a normalized new DTO projection.
    task_raw = _canonical(
        {
            "session_id": "synthetic-session",
            "thread_id": "synthetic-thread",
            "epoch": 1,
            "objective": "Synthetic owner objective.",
            "idempotency_key": "original-submit-key",
        }
    )
    return {
        "v": 1,
        "key": "original-anchor-key",
        "owner_objective": {"kind": "owner_text", "event_ref": "original-owner-event"},
        "scope": "local_only",
        "submit": {
            "contract": "TaskRequest",
            "canonical_utf8_b64": base64.b64encode(task_raw).decode("ascii"),
            "sha256": _sha(task_raw),
        }
        if inline
        else None,
        "task_admission": None
        if inline
        else {"backend_id": "1" * 32, "receipt_id": "2" * 32, "sha256": "3" * 64},
    }


def _fixture(target):
    anchor = _anchor(inline=target == "inline_anchor")
    if target == "not_found":
        return (
            media.MediaFound[media.MediaAnchorAdmission],
            {"v": 1, "found": False, "value": None},
            65536,
        )
    if target == "proof_receipt":
        receipt = {
            "v": 1,
            "id": "4" * 32,
            "key": "original-anchor-key",
            "request_sha256": "5" * 64,
            "body": anchor,
        }
        receipt["sha256"] = _sha(b"kinaiya.media.v1/R<AnchorAdmission>\0" + _canonical(receipt))
        return media.MediaReceipt[media.MediaAnchorAdmission], receipt, 65536
    return media.MediaAnchorAdmission, anchor, 262144 if target == "inline_anchor" else 65536


def _padded(value, size):
    canonical = _canonical(value)
    padding = size - len(canonical)
    assert padding > 1
    # Both sides are legal JSON whitespace; no unknown field supplies the
    # overflow and all canonical DTO/hash bytes remain identical.
    raw = b" " * (padding // 2) + canonical + b"\n" * (padding - padding // 2)
    assert len(raw) == size
    return raw


@pytest.mark.parametrize("target", _TARGETS)
def test_actual_envelope_raw_cap_accepts_exact_boundary(target):
    kind, value, cap = _fixture(target)
    parsed = media.media_json(_padded(value, cap), kind)
    assert media.media_bytes(parsed) == _canonical(value)


@pytest.mark.parametrize("target", _TARGETS)
def test_actual_envelope_raw_cap_refuses_one_byte_over(target):
    kind, value, cap = _fixture(target)
    with pytest.raises(media.MediaContractError) as failure:
        media.media_json(_padded(value, cap + 1), kind)
    assert failure.value.code == "bound"
    assert str(failure.value) == "Invalid media contract"
