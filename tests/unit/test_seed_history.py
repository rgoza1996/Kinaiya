"""Native receipt retention cannot widen or corrupt ordinary seed history."""

import json

import pytest

from harnesses.seed import ARTIFACT_RECEIPT_LIMIT, OBSERVATION_LIMIT, build_messages


def test_native_receipt_preserves_json_at_exact_utf8_cap_and_generic_limit():
    value = {"history_kind": "ui_publication_receipt", "summary_excerpt": "🌱"}
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    value["summary_excerpt"] += "x" * (ARTIFACT_RECEIPT_LIMIT - len(encoded.encode("utf-8")))
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) == ARTIFACT_RECEIPT_LIMIT
    assert len(encoded) < ARTIFACT_RECEIPT_LIMIT
    messages = build_messages(
        "Synthetic objective",
        1,
        [],
        [
            {"role": "user", "content": "x" * (OBSERVATION_LIMIT + 100)},
            {"role": "user", "content": encoded, "context_kind": "artifact_receipt"},
        ],
        "Read only",
    )
    assert messages[-2]["content"] == "x" * OBSERVATION_LIMIT + "…[truncated 100 chars]"
    assert messages[-1] == {"role": "user", "content": encoded}
    assert json.loads(messages[-1]["content"]) == value


def test_native_receipt_rejects_utf8_over_cap_instead_of_clipping_json():
    content = json.dumps(
        {"summary_excerpt": "🌱" * (ARTIFACT_RECEIPT_LIMIT // 4)}, ensure_ascii=False
    )
    assert len(content) < ARTIFACT_RECEIPT_LIMIT < len(content.encode("utf-8"))
    with pytest.raises(ValueError, match="Native UI receipt exceeds"):
        build_messages(
            "Synthetic objective",
            1,
            [],
            [{"role": "user", "content": content, "context_kind": "artifact_receipt"}],
            "Read only",
        )
