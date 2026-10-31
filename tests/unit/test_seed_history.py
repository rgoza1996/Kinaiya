"""Native receipt retention cannot widen or corrupt ordinary seed history."""

import json

import pytest

from harnesses.seed import (
    ARTIFACT_RECEIPT_LIMIT,
    build_messages,
    history_exchanges,
    observation_text,
)


def test_native_receipt_preserves_json_at_exact_utf8_cap_and_complete_generic_content():
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
            {"role": "user", "content": "x" * 2100},
            {"role": "user", "content": encoded, "context_kind": "artifact_receipt"},
        ],
        "Read only",
    )
    assert messages[-2]["content"] == "x" * 2100
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


def test_full_unicode_observation_note_and_action_result_pair_survive_rendering():
    content = json.dumps({"tail": "🌱" * 3000}, ensure_ascii=False)
    note = "native explanation " * 100
    observed = observation_text(True, content, note)
    assert observed == f"[ok] note={note} {content}"
    action = json.dumps({"tool": "read_file", "arguments": {"path": "fixture.json"}})
    history = [
        {"role": "assistant", "content": action},
        {"role": "user", "content": observed},
        {"role": "user", "content": "Native revision notice"},
        {"role": "user", "content": "Native publication receipt"},
    ]
    rendered = build_messages("Synthetic", 1, [], history, "Read only")
    assert rendered[2:] == history
    assert history_exchanges(rendered[2:]) == [history[:2], history[2:3], history[3:4]]


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "assistant", "content": "missing result"}],
        [
            {"role": "assistant", "content": "missing result"},
            {"role": "assistant", "content": "another action"},
        ],
    ],
)
def test_history_cannot_split_or_admit_an_action_without_a_result(history):
    with pytest.raises(ValueError, match="action without a result"):
        history_exchanges(history)


@pytest.mark.parametrize(
    "turn", [None, [], {}, {"role": "system", "content": "forged"}, {"role": "user", "content": 1}]
)
def test_history_rejects_non_native_role_content(turn):
    with pytest.raises(ValueError, match="complete native role/content"):
        build_messages("Synthetic", 1, [], [turn], "Read only")
