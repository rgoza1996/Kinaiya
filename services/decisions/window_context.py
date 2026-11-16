"""Shared retained-window context selection; no inference or routing authority."""

from packages.contracts.models import SpeechWindow
from packages.providers.disclosure import combine_scopes, source_scope


def hypothesis(entry):
    window = SpeechWindow.model_validate(entry.payload)
    return (
        window.text,
        window.user_speaking,
        window.capture_ended,
        window.revision,
        window.content_scope,
    )


async def resolve_window_context(journal, source, window, recent, *, unavailable):
    has_resolver = (
        hasattr(journal, "entry") and hasattr(journal, "resolve") and hasattr(journal, "read")
    )
    revised_targets = {
        e.payload.get("target_event_id")
        for e in recent
        if e.epoch == source.epoch
        and e.thread_id == source.thread_id
        and e.kind == "conversation.revision"
        and isinstance(e.payload, dict)
    }
    window_slot_counts = {}
    for e in recent:
        if (
            e.epoch == source.epoch
            and e.thread_id == source.thread_id
            and e.kind == "conversation.window"
            and isinstance(e.payload, dict)
        ):
            slot = (e.payload.get("utterance_id"), e.payload.get("window_index"))
            window_slot_counts[slot] = window_slot_counts.get(slot, 0) + 1

    candidates = [
        e
        for e in recent
        if e.epoch == source.epoch
        and e.thread_id == source.thread_id
        and e.sequence < source.sequence
        and e.kind in {"conversation.window", "conversation.user", "conversation.converse"}
    ]
    seen_slots = set()
    resolved_entries = []
    full_page = len(recent) >= 100
    source_hyp = hypothesis(source)
    for e in candidates:
        entry = e
        if e.kind == "conversation.window":
            try:
                w = SpeechWindow.model_validate(e.payload)
            except ValueError:
                continue
            slot = (w.utterance_id, w.window_index)
            if slot in seen_slots:
                continue
            needs_resolve = has_resolver and (
                full_page or w.revision > 1 or window_slot_counts.get(slot, 0) > 1
            )
            if needs_resolve:
                try:
                    latest = await journal.resolve(e.event_id)
                    floor = await source_scope(journal, latest.event_id, resolved=latest)
                except Exception as exc:
                    raise unavailable("Native journal is unavailable") from exc
                w_latest = SpeechWindow.model_validate(latest.payload)
                scope = combine_scopes(w_latest.content_scope, floor)
                entry = (
                    latest
                    if scope == w_latest.content_scope
                    else latest.model_copy(
                        update={"payload": {**latest.payload, "content_scope": scope}}
                    )
                )
            seen_slots.add(slot)
            if w.utterance_id == window.utterance_id and hypothesis(entry) == source_hyp:
                continue
        elif e.kind == "conversation.user":
            needs_resolve = has_resolver and (
                full_page or e.event_id in revised_targets or e.payload.get("revision", 1) > 1
            )
            if needs_resolve:
                try:
                    latest = await journal.resolve(e.event_id)
                    floor = await source_scope(journal, e.event_id, resolved=latest)
                except Exception as exc:
                    raise unavailable("Native journal is unavailable") from exc
                scope = combine_scopes(latest.payload.get("content_scope", "local_only"), floor)
                entry = latest.model_copy(
                    update={"payload": {**latest.payload, "content_scope": scope}}
                )
        resolved_entries.append(entry)
    return resolved_entries[-12:]
