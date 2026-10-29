"""Pure selected-recall projection tests; all entries are synthetic native-shaped data."""

import json
from types import SimpleNamespace

from packages.contracts.models import JournalEntry, ProviderInfo, SpeechWindow
from services.converse.context_projection import ContextProjection
from services.converse.delivery_context import (
    window_context,
    window_context_projection,
    window_context_scoped,
)
from services.converse.history import typed_history, typed_history_projection, typed_history_scoped
from services.converse.memory_history import (
    resolve_current_pin,
    tagged_assistant_eligible,
    validate_pin,
)

SOURCE = SimpleNamespace(session_id="session", thread_id="thread", epoch=1)
LOCAL = ProviderInfo(provider="synthetic", model="local-fixture", local=True)
CONTRIBUTOR = ProviderInfo(provider="meta", model="synthetic-contributor", local=False)
RUNTIME_ID = "runtime-1"

PIN = {
    "schema_version": "1.0",
    "implementation_id": "korina-dag-v1",
    "use_generation": 1,
    "namespace_revision": 0,
    "closure_sha256": "a" * 64,
    "selector_ids": ["seed-1"],
}


def pin(**overrides):
    base = dict(PIN)
    base.update(overrides)
    return base


def user_entry(sequence, text=None, scope="public_synthetic", **extra):
    payload = {"text": text or f"Synthetic user {sequence}", "revision": 1}
    if scope is not None:
        payload["content_scope"] = scope
    payload.update(extra)
    return JournalEntry(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        sequence=sequence,
        event_id=f"user-{sequence}",
        kind="conversation.user",
        origin="authenticated-user",
        payload=payload,
        idempotency_key=f"user-key-{sequence}",
    )


def assistant_entry(
    sequence, text=None, scope="local_only", runtime_id=RUNTIME_ID, tag=None, has_tag=False, **extra
):
    payload = {
        "text": text or f"Synthetic assistant {sequence}",
        "content_scope": scope,
        "runtime_id": runtime_id,
        "chunk_id": f"chunk-{sequence}",
        "generation_epoch": 1,
    }
    payload.update(extra)
    if has_tag:
        payload["memory_context"] = tag
    # Allow explicit scope=None to mean missing key for local_only tests.
    if scope is None and "content_scope" in payload:
        del payload["content_scope"]
    if runtime_id is None and "runtime_id" in payload:
        # Keep explicit None to test missing/invalid runtime handling.
        payload["runtime_id"] = None
    return JournalEntry(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        sequence=sequence,
        event_id=f"assistant-{sequence}",
        kind="conversation.converse",
        origin="converse",
        payload=payload,
        idempotency_key=f"assistant-key-{sequence}",
    )


def tagged_assistant(sequence, tag=None, **kwargs):
    return assistant_entry(sequence, has_tag=True, tag=PIN if tag is None else tag, **kwargs)


def window_source(sequence=100, utterance_id="current"):
    window = SpeechWindow(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        utterance_id=utterance_id,
        window_index=0,
        text="Synthetic current window",
        start_ms=0,
        end_ms=1000,
        user_speaking=False,
        capture_ended=True,
        content_scope="public_synthetic",
    )
    return JournalEntry(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        sequence=sequence,
        event_id=f"window-{utterance_id}",
        kind="conversation.window",
        origin="authenticated-user",
        payload=window.model_dump(mode="json"),
        idempotency_key=f"window-key-{utterance_id}",
    )


def window_assistant(
    sequence,
    text=None,
    scope="public_synthetic",
    runtime_id=RUNTIME_ID,
    tag=None,
    has_tag=False,
    **extra,
):
    # Window assistants default to public scope so untagged comparison is meaningful.
    return assistant_entry(
        sequence,
        text=text,
        scope=scope,
        runtime_id=runtime_id,
        tag=tag,
        has_tag=has_tag,
        **extra,
    )


def receipt_for(assistant, status="played", **fields):
    payload = {
        "event_id": assistant.event_id,
        "chunk_id": assistant.payload["chunk_id"],
        "generation_epoch": assistant.payload["generation_epoch"],
        "status": status,
        "alignment": "conservative_estimate",
        "played_seconds": 0,
        **fields,
    }
    return JournalEntry(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        sequence=assistant.sequence + 1000,
        event_id=f"receipt-{assistant.event_id}-{status}",
        kind="delivery.updated",
        origin="converse",
        payload=payload,
        causes=[assistant.event_id],
        idempotency_key=f"receipt-key-{assistant.event_id}",
    )


def test_projections_return_shared_context_projection_shape():
    recent = [user_entry(1)]
    typed = typed_history_projection(recent, SOURCE, LOCAL, 5)
    window_src = window_source()
    windowed = window_context_projection(
        [user_entry(1, scope="public_synthetic")], window_src, LOCAL
    )
    assert isinstance(typed, ContextProjection)
    assert isinstance(windowed, ContextProjection)
    assert typed.delivery is None
    assert typed.consumed_memory is False


def test_typed_untagged_matches_compat_byte_for_byte():
    recent = [
        user_entry(1, text="hello"),
        assistant_entry(2, text="hi", scope="public_synthetic", runtime_id="other"),
        user_entry(3, text="bye"),
    ]
    proj = typed_history_projection(recent, SOURCE, CONTRIBUTOR, 10)
    compat_messages, compat_scopes = typed_history_scoped(recent, SOURCE, CONTRIBUTOR, 10)
    assert proj.messages == compat_messages == typed_history(recent, SOURCE, CONTRIBUTOR, 10)
    assert proj.consumed_scopes == compat_scopes
    assert proj.consumed_memory is False
    assert proj.delivery is None


def test_window_untagged_matches_compat_byte_for_byte():
    source = window_source(sequence=50)
    first = window_assistant(1, text="first")
    second = window_assistant(2, text="second")
    recent = [first, second, source]
    proj = window_context_projection(recent, source, CONTRIBUTOR)
    compat_messages, compat_delivery, compat_scopes = window_context_scoped(
        recent, source, CONTRIBUTOR
    )
    legacy_messages, legacy_delivery = window_context(recent, source, CONTRIBUTOR)
    assert proj.messages == compat_messages == legacy_messages
    assert proj.delivery == compat_delivery == legacy_delivery
    assert proj.consumed_scopes == compat_scopes
    assert proj.consumed_memory is False


def test_compat_paths_omit_tagged_by_default():
    tagged = tagged_assistant(2, text="tagged must hide")
    recent = [user_entry(1), tagged, user_entry(3)]
    assert typed_history(recent, SOURCE, LOCAL, 10) == [
        {"role": "user", "content": "Synthetic user 1"},
        {"role": "user", "content": "Synthetic user 3"},
    ]
    messages, scopes = typed_history_scoped(recent, SOURCE, LOCAL, 10)
    assert all(m["content"] != "tagged must hide" for m in messages)
    assert scopes == ["public_synthetic", "public_synthetic"]

    source = window_source(sequence=50)
    w_tagged = window_assistant(
        2, text="tagged window hide", scope="local_only", has_tag=True, tag=PIN
    )
    recent_w = [window_assistant(1, text="kept"), w_tagged, source]
    messages_w, delivery_w = window_context(recent_w, source, LOCAL)
    assert all(m["content"] != "tagged window hide" for m in messages_w)
    messages_s, delivery_s, _ = window_context_scoped(recent_w, source, LOCAL)
    assert messages_w == messages_s and delivery_w == delivery_s


def test_typed_exact_pin_match_is_selected_and_consumes_memory():
    tagged = tagged_assistant(2, text="recalled note")
    recent = [user_entry(1), tagged]
    proj = typed_history_projection(
        recent, SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert {"role": "assistant", "content": "recalled note"} in proj.messages
    assert proj.consumed_memory is True
    assert (
        proj.consumed_scopes[proj.messages.index({"role": "assistant", "content": "recalled note"})]
        == "local_only"
    )


def test_window_exact_pin_match_is_selected_and_consumes_memory():
    source = window_source(sequence=50)
    tagged = window_assistant(
        2, text="recalled window note", scope="local_only", has_tag=True, tag=pin()
    )
    recent = [window_assistant(1, text="plain"), tagged, source]
    proj = window_context_projection(
        recent, source, LOCAL, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert {"role": "assistant", "content": "recalled window note"} in proj.messages
    assert proj.consumed_memory is True


def test_absent_current_pin_omits_tagged():
    tagged = tagged_assistant(2)
    recent = [user_entry(1), tagged]
    for kwargs in ({}, {"memory_context": None, "runtime_id": RUNTIME_ID}):
        proj = typed_history_projection(recent, SOURCE, LOCAL, 10, **kwargs)
        assert all(
            "assistant" not in m["role"] or "Synthetic assistant 2" not in m["content"]
            for m in proj.messages
        )
        assert proj.consumed_memory is False


def test_null_tag_is_tagged_not_untagged():
    # Same disclosure would admit an untagged local_only assistant for a local provider.
    untagged = assistant_entry(2, scope="local_only")
    assert typed_history_projection([untagged], SOURCE, LOCAL, 10).consumed_memory is False
    assert len(typed_history_projection([untagged], SOURCE, LOCAL, 10).messages) == 1
    nulled = assistant_entry(2, scope="local_only", has_tag=True, tag=None)
    proj = typed_history_projection(
        [nulled], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages == []
    assert proj.consumed_memory is False


def test_missing_key_is_untagged_and_kept():
    plain = assistant_entry(2, scope="public_synthetic")
    assert "memory_context" not in plain.payload
    proj = typed_history_projection(
        [plain], SOURCE, CONTRIBUTOR, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages == [{"role": "assistant", "content": plain.payload["text"]}]
    assert proj.consumed_memory is False


def test_unknown_version_is_excluded():
    for bad in ("2.0", "1", "", 1.0, None):
        tag = pin(schema_version=bad)
        # None already covered; model_validate would drop missing, so build raw.
        if bad is None:
            tag = dict(pin())
            del tag["schema_version"]
        tagged = tagged_assistant(2, tag=tag)
        proj = typed_history_projection(
            [tagged], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
        )
        assert proj.messages == [], bad
        assert proj.consumed_memory is False


def test_malformed_tags_are_excluded():
    bad_tags = ["string", 42, 3.14, True, ["list"], ("tuple",), {"not": "a pin"}]
    for bad in bad_tags:
        tagged = tagged_assistant(2, tag=bad)
        proj = typed_history_projection(
            [tagged], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
        )
        assert proj.messages == [], bad
        assert proj.consumed_memory is False


def test_tag_mismatch_is_excluded():
    other = pin(selector_ids=["other-seed"])
    tagged = tagged_assistant(2, tag=other)
    proj = typed_history_projection(
        [tagged], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages == []
    assert proj.consumed_memory is False
    # Transitive check: tag equals an old pin but not the current one.
    assert (
        tagged_assistant_eligible(tagged, SOURCE, LOCAL, resolve_current_pin(pin()), RUNTIME_ID)
        is False
    )


def test_bad_current_pin_excludes_all_tagged():
    tagged = tagged_assistant(2)
    for bad_current in (
        {},
        {"schema_version": "1.0"},
        pin(use_generation="1"),
        pin(selector_ids=["a", "a"]),
        "string",
        42,
    ):
        proj = typed_history_projection(
            [user_entry(1), tagged],
            SOURCE,
            LOCAL,
            10,
            memory_context=bad_current,
            runtime_id=RUNTIME_ID,
        )
        assert all(m["role"] == "user" for m in proj.messages), bad_current
        assert proj.consumed_memory is False


def test_runtime_mismatch_is_excluded():
    tagged = tagged_assistant(2, runtime_id="other-runtime")
    proj = typed_history_projection(
        [tagged], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages == []
    for bad_runtime in (None, "", 42, "x" * 129, "bad id!"):
        proj = typed_history_projection(
            [tagged_assistant(3)], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=bad_runtime
        )
        assert proj.messages == [], bad_runtime
    # Entry missing runtime_id.
    missing = tagged_assistant(4, runtime_id=None)
    # Force payload without usable runtime.
    del missing.payload["runtime_id"]
    proj = typed_history_projection(
        [missing], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages == []


def test_scope_must_be_explicit_local_only():
    for scope in ("public_synthetic", "private_cloud_authorized", "unknown", 42, None):
        kwargs = {"scope": scope} if scope is not None else {}
        tagged = tagged_assistant(2, tag=pin(), **kwargs)
        if scope is None:
            # Helper deletes the key to simulate a missing scope.
            tagged = assistant_entry(2, scope=None, has_tag=True, tag=pin())
        proj = typed_history_projection(
            [tagged], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
        )
        assert proj.messages == [], scope
        assert proj.consumed_memory is False


def test_nonlocal_provider_omits_tagged():
    tagged = tagged_assistant(2, tag=pin())
    for info in (CONTRIBUTOR, SimpleNamespace(local=False, provider="x", model="y")):
        proj = typed_history_projection(
            [tagged], SOURCE, info, 10, memory_context=pin(), runtime_id=RUNTIME_ID
        )
        assert proj.messages == []
        assert proj.consumed_memory is False


def test_strict_model_values_reject_coercion():
    strict_bad = [
        pin(use_generation="1"),
        pin(use_generation=0),
        pin(use_generation=1.0),
        pin(namespace_revision=-1),
        pin(namespace_revision="0"),
        pin(implementation_id="bad id!"),
        pin(closure_sha256="z" * 64),
        pin(closure_sha256="a" * 63),
        pin(selector_ids=[]),
        pin(selector_ids=["a", "a"]),
        pin(selector_ids=["ok", 42]),
    ]
    for bad in strict_bad:
        assert validate_pin(bad) is None, bad
        tagged = tagged_assistant(2, tag=bad)
        proj = typed_history_projection(
            [tagged], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
        )
        assert proj.messages == [], bad
    # Extra fields are forbidden.
    extra = pin()
    extra["unexpected"] = "field"
    assert validate_pin(extra) is None
    # Valid pin passes.
    assert validate_pin(pin()) is not None
    assert validate_pin(dict(pin())) is not None


def test_schema_version_explicit_and_size_bound():
    missing = dict(pin())
    del missing["schema_version"]
    assert validate_pin(missing) is None
    assert resolve_current_pin(missing) is None
    assert resolve_current_pin(None) is None
    # Canonical size of a valid pin stays within the bound.
    encoded = json.dumps(
        validate_pin(pin()).model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    assert len(encoded) <= 2048
    # An oversized raw tag is fail-closed even before model checks.
    big = dict(pin())
    big["selector_ids"] = ["x" * 127 + chr(65 + i) for i in range(8)]
    # Still valid and within bound, so it passes size but must match exactly.
    assert validate_pin(big) is not None
    oversized = dict(pin())
    oversized["extra_blob"] = "y" * 3000
    assert validate_pin(oversized) is None


def test_gate_runs_before_suffix_selection():
    # Ineligible middle entry is removed first, so the earliest entry survives a 2-turn window.
    first = user_entry(1, text="first kept")
    bad_tagged = tagged_assistant(2, tag=pin(selector_ids=["other"]))
    last = user_entry(3, text="last kept")
    recent = [first, bad_tagged, last]
    proj = typed_history_projection(
        recent, SOURCE, LOCAL, 2, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages == [
        {"role": "user", "content": "first kept"},
        {"role": "user", "content": "last kept"},
    ]
    assert proj.consumed_memory is False


def test_consumed_memory_only_when_tagged_actually_selected():
    old_tagged = tagged_assistant(1, text="old recalled")
    recent = [old_tagged, user_entry(2), user_entry(3), user_entry(4)]
    # Eligible but outside the 2-turn suffix: present, not consumed.
    proj = typed_history_projection(
        recent, SOURCE, LOCAL, 2, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages == [
        {"role": "user", "content": "Synthetic user 3"},
        {"role": "user", "content": "Synthetic user 4"},
    ]
    assert proj.consumed_memory is False
    # Inside the suffix: consumed.
    proj2 = typed_history_projection(
        recent, SOURCE, LOCAL, 4, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj2.consumed_memory is True


def test_recent100_snapshot_never_backfills():
    filler = [
        user_entry(seq, scope="public_synthetic", text=f"filler {seq}") for seq in range(1, 102)
    ]
    # Old eligible tagged at sequence 1 falls outside recent[-100:].
    old = tagged_assistant(1, text="too old")
    recent = [old] + filler
    assert len(recent) == 102
    proj = typed_history_projection(
        recent, SOURCE, LOCAL, 100, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert all(m["content"] != "too old" for m in proj.messages)
    assert proj.consumed_memory is False
    # Foreign-scope entries never enter the snapshot.
    foreign = tagged_assistant(200, text="foreign")
    foreign = foreign.model_copy(update={"thread_id": "foreign"})
    proj2 = typed_history_projection(
        [user_entry(1), foreign], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert all(m["content"] != "foreign" for m in proj2.messages)


def test_user_revision_and_disclosure_still_apply_with_tags_present():
    base = user_entry(1, text="original", scope="public_synthetic")
    correction = JournalEntry(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        sequence=2,
        event_id="rev-2",
        kind="conversation.revision",
        origin="authenticated-user",
        payload={
            "target_event_id": base.event_id,
            "text": "corrected",
            "revision": 2,
            "content_scope": "public_synthetic",
        },
        idempotency_key="rev-key-2",
    )
    tagged = tagged_assistant(3, text="recalled")
    recent = [base, correction, tagged]
    proj = typed_history_projection(
        recent, SOURCE, CONTRIBUTOR, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    # Non-local provider cannot consume the local_only tagged assistant,
    # but the corrected public user text remains.
    assert proj.messages == [{"role": "user", "content": "corrected"}]
    proj_local = typed_history_projection(
        recent, SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj_local.messages == [
        {"role": "user", "content": "corrected"},
        {"role": "assistant", "content": "recalled"},
    ]
    # Private correction floor stays local.
    private_correction = correction.model_copy(
        update={"payload": dict(correction.payload, content_scope="local_only")}
    )
    proj_private = typed_history_projection([base, private_correction], SOURCE, CONTRIBUTOR, 10)
    assert proj_private.messages == []


def test_typed_keeps_full_text_and_context_turns():
    long_text = "L:" + "x" * 5000
    tagged = tagged_assistant(2, text=long_text)
    proj = typed_history_projection(
        [user_entry(1), tagged], SOURCE, LOCAL, 10, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages[-1]["content"] == long_text


def test_window_gate_runs_before_eight_row_selection():
    source = window_source(sequence=100)
    assistants = [window_assistant(seq, text=f"plain {seq}") for seq in range(1, 10)]
    # Insert an ineligible tagged entry in the middle; it must not consume a row slot.
    bad = window_assistant(
        5, text="bad tagged", scope="local_only", has_tag=True, tag=pin(selector_ids=["nope"])
    )
    recent = assistants[:4] + [bad] + assistants[4:] + [source]
    proj = window_context_projection(recent, source, CONTRIBUTOR)
    assert len(proj.messages) == 8
    assert all(m["content"] != "bad tagged" for m in proj.messages)
    assert proj.consumed_memory is False
    # Eligible tagged entries participate in the eight-row bound.
    good = window_assistant(50, text="good tagged", scope="local_only", has_tag=True, tag=pin())
    recent2 = assistants + [good, source]
    proj2 = window_context_projection(
        recent2, source, LOCAL, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert len(proj2.messages) == 8
    assert proj2.consumed_memory is True


def test_window_consumed_memory_only_for_selected_rows():
    source = window_source(sequence=100)
    old = window_assistant(1, text="old recalled", scope="local_only", has_tag=True, tag=pin())
    filler = [window_assistant(seq, text=f"plain {seq}") for seq in range(2, 12)]
    recent = [old] + filler + [source]
    proj = window_context_projection(
        recent, source, LOCAL, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert len(proj.messages) == 8
    assert all(m["content"] != "old recalled" for m in proj.messages)
    assert proj.consumed_memory is False


def test_window_recent100_never_backfills():
    source = window_source(sequence=500)
    old = window_assistant(1, text="too old", scope="local_only", has_tag=True, tag=pin())
    filler = [window_assistant(seq, text=f"filler {seq}") for seq in range(2, 102)]
    recent = [old] + filler + [source]
    proj = window_context_projection(
        recent, source, LOCAL, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert all(m["content"] != "too old" for m in proj.messages)
    assert proj.consumed_memory is False


def test_window_indices_match_final_messages_without_gaps():
    source = window_source(sequence=50)
    kept = window_assistant(1, text="kept one")
    dropped = window_assistant(
        2, text="dropped tagged", scope="local_only", has_tag=True, tag=pin(selector_ids=["nope"])
    )
    kept2 = window_assistant(3, text="kept two")
    recent = [kept, dropped, kept2, source]
    proj = window_context_projection(recent, source, CONTRIBUTOR)
    assert [m["content"] for m in proj.messages] == ["kept one", "kept two"]
    assert proj.delivery is not None
    block = json.loads(proj.delivery.split("\n", 2)[2])
    assert [row["history_index"] for row in block["history"]] == [0, 1]
    assert [row["event_id"] for row in block["history"]] == [kept.event_id, kept2.event_id]


def test_window_playback_receipts_follow_retained_assistants():
    source = window_source(sequence=200)
    first = window_assistant(10, text="first kept")
    second = window_assistant(20, text="second kept")
    dropped = window_assistant(
        15, text="dropped", scope="local_only", has_tag=True, tag=pin(selector_ids=["nope"])
    )
    first_receipt = receipt_for(first, status="played")
    dropped_receipt = receipt_for(dropped, status="played")
    second_receipt = receipt_for(second, status="generated")
    recent = [first, dropped, second, first_receipt, dropped_receipt, second_receipt, source]
    proj = window_context_projection(recent, source, CONTRIBUTOR)
    assert [m["content"] for m in proj.messages] == ["first kept", "second kept"]
    block = json.loads(proj.delivery.split("\n", 2)[2])
    assert len(block["history"]) == 2
    assert block["history"][0]["latest_status"] == "played"
    assert block["history"][0]["played_report_sequence"] == first_receipt.sequence
    assert block["history"][1]["latest_status"] == "generated"
    assert all(row["history_index"] in (0, 1) for row in block["history"])


def test_window_text_and_delivery_bounds_preserved():
    source = window_source(sequence=50)
    long_text = "W:" + "y" * 5000
    tagged = window_assistant(1, text=long_text, scope="local_only", has_tag=True, tag=pin())
    recent = [tagged, source]
    proj = window_context_projection(
        recent, source, LOCAL, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert proj.messages[0]["content"] == long_text[:2000]
    assert len(proj.delivery.encode()) <= 8192
    # Eight-row bound still holds with eligible tagged history.
    many = [window_assistant(seq, text=f"row {seq}") for seq in range(1, 12)]
    many_tagged = [
        window_assistant(
            seq + 100, text=f"tagged {seq}", scope="local_only", has_tag=True, tag=pin()
        )
        for seq in range(1, 12)
    ]
    recent_many = many + many_tagged + [window_source(sequence=1000)]
    proj_many = window_context_projection(
        recent_many, recent_many[-1], LOCAL, memory_context=pin(), runtime_id=RUNTIME_ID
    )
    assert len(proj_many.messages) <= 8
    assert proj_many.consumed_memory is True


def test_window_stop_and_scope_floors_preserved():
    source = window_source(sequence=100)
    assistant = window_assistant(10, text="generated text")
    stop = JournalEntry(
        session_id=SOURCE.session_id,
        thread_id=SOURCE.thread_id,
        epoch=SOURCE.epoch,
        sequence=60,
        event_id="stop-1",
        kind="delivery.updated",
        origin="converse",
        payload={
            "control": "generation_stop",
            "runtime_id": assistant.payload["runtime_id"],
            "status": "interrupted",
            "alignment": "delivery_unknown",
            "generation_epoch": 0,
            "next_generation_epoch": 2,
        },
        causes=[],
        idempotency_key="stop-key-1",
    )
    recent = [assistant, stop, source]
    proj = window_context_projection(recent, source, CONTRIBUTOR)
    block = json.loads(proj.delivery.split("\n", 2)[2])
    assert block["history"][0]["generation_stop"]["status"] == "stop_requested"
    assert proj.consumed_scopes == ["public_synthetic"]
