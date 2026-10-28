"""Bounded history projection invariants; all entries are synthetic native-shaped data."""

from types import SimpleNamespace

import pytest

from packages.contracts.models import JournalEntry, ProviderInfo
from services.converse.history import MAX_INTEGER, typed_history

SOURCE = SimpleNamespace(session_id="session", thread_id="thread", epoch=1)
CONTRIBUTOR = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
LOCAL = ProviderInfo(provider="synthetic", model="local-fixture", local=True)


def entry(sequence, *, kind="conversation.user", text=None, origin=None, **payload):
    return JournalEntry(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        sequence=sequence,
        event_id=f"event-{sequence}",
        kind=kind,
        origin=origin or ("converse" if kind != "conversation.user" else "authenticated-user"),
        payload={
            "text": text or f"Synthetic text {sequence}",
            "revision": 1,
            "content_scope": "public_synthetic",
            **payload,
        },
        idempotency_key=f"key-{sequence}",
    )


def revision(sequence, target, **payload):
    return entry(
        sequence,
        kind="conversation.revision",
        target_event_id=target.event_id,
        revision=2,
        **payload,
    )


def test_revision_keeps_original_position_before_context_truncation():
    first = entry(1)
    assistant = entry(2, kind="conversation.converse")
    latest = entry(3)
    corrected = revision(4, first, text="Corrected original position")
    snapshot = [first, assistant, latest, corrected]
    assert typed_history(snapshot, SOURCE, CONTRIBUTOR, 3) == [
        {"role": "user", "content": "Corrected original position"},
        {"role": "assistant", "content": "Synthetic text 2"},
        {"role": "user", "content": "Synthetic text 3"},
    ]
    assert typed_history(snapshot, SOURCE, CONTRIBUTOR, 2) == [
        {"role": "assistant", "content": "Synthetic text 2"},
        {"role": "user", "content": "Synthetic text 3"},
    ]


def test_denied_message_is_removed_before_context_truncation():
    snapshot = [entry(i) for i in range(1, 4)]
    snapshot.append(revision(4, snapshot[2], content_scope="local_only"))
    assert typed_history(snapshot, SOURCE, CONTRIBUTOR, 2) == [
        {"role": "user", "content": "Synthetic text 1"},
        {"role": "user", "content": "Synthetic text 2"},
    ]


def test_typed_context_preserves_full_text_and_configured_turns_instead_of_window_caps():
    snapshot = [entry(i, text=f"{i}:" + "x" * 3000) for i in range(1, 16)]
    history = typed_history(snapshot, SOURCE, LOCAL, 12)
    assert len(history) == 12
    assert history[0]["content"] == "4:" + "x" * 3000
    assert history[-1]["content"] == "15:" + "x" * 3000


def test_suffix_is_contiguous_and_sliced_before_filtering_without_refilling():
    original = entry(1)
    snapshot = [original]
    snapshot.extend(
        entry(i).model_copy(update={"thread_id": "foreign-thread"}) for i in range(2, 102)
    )
    snapshot.append(revision(102, original))
    assert typed_history(snapshot, SOURCE, CONTRIBUTOR, 12) == []


@pytest.mark.parametrize(
    "field,value", [("session_id", "other"), ("thread_id", "other"), ("epoch", 2)]
)
def test_foreign_scope_cannot_change_or_supply_history(field, value):
    original = entry(1)
    foreign = revision(2, original, text="Foreign revision").model_copy(update={field: value})
    forged = entry(3, text="Foreign base").model_copy(update={field: value})
    assert typed_history([original, foreign, forged], SOURCE, CONTRIBUTOR, 12) == [
        {"role": "user", "content": "Synthetic text 1"}
    ]


@pytest.mark.parametrize(
    "kind", ["conversation.user", "conversation.converse", "conversation.revision"]
)
@pytest.mark.parametrize("origin", ["agent-supervisor", "decisions", "resources"])
def test_other_component_origins_cannot_supply_or_revise_history(kind, origin):
    original = entry(1)
    forged = entry(2, kind=kind, origin=origin, target_event_id=original.event_id, revision=2)
    assert typed_history([original, forged], SOURCE, CONTRIBUTOR, 12) == [
        {"role": "user", "content": "Synthetic text 1"}
    ]


@pytest.mark.parametrize("bad_revision", [None, True, 1.0, 0, -1, "1", {}, [], MAX_INTEGER + 1])
def test_malformed_base_revision_is_omitted_even_without_a_correction(bad_revision):
    original = entry(1, revision=bad_revision)
    assert typed_history([original], SOURCE, LOCAL, 12) == []


@pytest.mark.parametrize("bad_revision", [None, True, 2.0, 1, 3, -1, "2", {}, [], MAX_INTEGER + 1])
def test_any_unprocessable_targeted_revision_stickily_omits_base(bad_revision):
    original = entry(1)
    bad = entry(
        2, kind="conversation.revision", target_event_id=original.event_id, revision=bad_revision
    )
    later = revision(3, original, text="Later public label cannot undo malformed chain")
    assert typed_history([original, bad, later], SOURCE, LOCAL, 12) == []


@pytest.mark.parametrize("bad_text", [None, True, 2, {}, []])
def test_malformed_revision_text_does_not_fall_back_to_base(bad_text):
    original = entry(1)
    bad = revision(2, original).model_copy(
        update={"payload": {"target_event_id": original.event_id, "revision": 2, "text": bad_text}}
    )
    assert typed_history([original, bad], SOURCE, CONTRIBUTOR, 12) == []


def test_pre_base_revision_taints_target_and_orphan_cannot_create_message():
    original = entry(2)
    before = revision(1, original)
    orphan = entry(3, kind="conversation.revision", target_event_id="unknown", revision=2)
    assert typed_history([before, original, orphan], SOURCE, CONTRIBUTOR, 12) == []


def test_native_sequence_order_controls_fold_even_if_input_order_differs():
    original = entry(1)
    second = revision(2, original, text="Second")
    third = entry(
        3, kind="conversation.revision", target_event_id=original.event_id, revision=3, text="Third"
    )
    assert typed_history([third, original, second], SOURCE, CONTRIBUTOR, 12) == [
        {"role": "user", "content": "Third"}
    ]


@pytest.mark.parametrize("scope", [None, {}, [], 42, "unrecognized"])
@pytest.mark.parametrize("kind", ["conversation.user", "conversation.converse"])
def test_malformed_or_unknown_present_scope_is_denied_even_for_local(kind, scope):
    assert typed_history([entry(1, kind=kind, content_scope=scope)], SOURCE, LOCAL, 12) == []
