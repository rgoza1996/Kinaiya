"""Pure, bounded native history projection shared by typed and live-window replies."""

from dataclasses import dataclass

from packages.providers.disclosure import combine_scopes, entry_allowed
from services.converse.context_projection import ContextProjection
from services.converse.memory_history import resolve_current_pin, tagged_assistant_eligible

MAX_INTEGER = 9_007_199_254_740_991


def native_snapshot(recent, source, *, limit=100):
    """Filter one contiguous suffix without filling gaps from older or foreign history."""
    return sorted(
        (
            entry
            for entry in (recent if limit is None else recent[-limit:])
            if (entry.session_id, entry.thread_id, entry.epoch)
            == (source.session_id, source.thread_id, source.epoch)
        ),
        key=lambda entry: entry.sequence,
    )


def _revision(value):
    return type(value) is int and 1 <= value <= MAX_INTEGER


def _entry_scope(entry):
    """Missing/invalid historical scope never implies export permission."""
    scope = entry.payload.get("content_scope", "local_only")
    if not isinstance(scope, str) or scope not in {
        "local_only",
        "public_synthetic",
        "private_cloud_authorized",
    }:
        return "local_only"
    return scope


@dataclass
class UserHistory:
    entry: object
    revision: object
    text: object
    permitted: bool
    floor: str = "local_only"


def fold_users(snapshot, allowed):
    """Retain original positions and a sticky disclosure floor through native corrections."""
    users = {
        entry.event_id: UserHistory(
            entry,
            entry.payload.get("revision", 1),
            entry.payload.get("text"),
            _revision(entry.payload.get("revision", 1)) and allowed(entry),
            _entry_scope(entry),
        )
        for entry in snapshot
        if entry.kind == "conversation.user" and entry.origin == "authenticated-user"
    }
    for entry in snapshot:
        if entry.kind != "conversation.revision" or entry.origin not in {
            "converse",
            "authenticated-user",
        }:
            continue
        payload = entry.payload
        target = payload.get("target_event_id")
        original = users.get(target) if isinstance(target, str) else None
        if original is None:
            continue
        if not (
            entry.sequence > original.entry.sequence
            and _revision(original.revision)
            and _revision(payload.get("revision"))
            and payload["revision"] == original.revision + 1
            and isinstance(payload.get("text"), str)
        ):
            # An unprocessable correction cannot restore an old exportable
            # draft, even if a later correction looks public and well formed.
            original.permitted = False
            original.floor = "local_only"
            continue
        original.revision, original.text = payload["revision"], payload["text"]
        # A missing correction scope is a local-only floor: later public
        # labels cannot restore eligibility for a private original/correction.
        original.floor = combine_scopes(original.floor, _entry_scope(entry))
        original.permitted = original.permitted and allowed(entry)
    return users


def _typed_allowed(info, entry):
    # Legacy unscoped entries are local-only; malformed scopes are never grants.
    if "content_scope" in entry.payload and not isinstance(entry.payload["content_scope"], str):
        return False
    return entry_allowed(info, entry)


def _keep_typed_assistant(entry, source, info, current_pin, runtime_id):
    if (
        entry.kind != "conversation.converse"
        or entry.origin != "converse"
        or not isinstance(entry.payload.get("text"), str)
    ):
        return False
    if "memory_context" not in entry.payload:
        return _typed_allowed(info, entry)
    if not tagged_assistant_eligible(entry, source, info, current_pin, runtime_id):
        return False
    return _typed_allowed(info, entry)


def typed_history_projection(
    recent, source, info, context_turns, *, memory_context=None, runtime_id=None
):
    """Project typed history with optional selected-recall assistants.

    Untagged behavior matches the legacy compatibility path byte-for-byte.
    Tagged assistants are fail-closed: only an exact current pin, matching
    runtime and outer scope, local provider, and explicit local_only scope
    is retained. The gate runs before the suffix selection inside the
    original contiguous recent100 snapshot; consumed_memory is true only
    when a tagged assistant is actually selected.
    """
    snapshot = native_snapshot(recent, source)
    users = fold_users(snapshot, lambda entry: _typed_allowed(info, entry))
    current_pin = resolve_current_pin(memory_context)
    candidates = [
        (user.entry, user.text, user.floor)
        for user in users.values()
        if user.permitted and isinstance(user.text, str)
    ]
    candidates.extend(
        (entry, entry.payload["text"], _entry_scope(entry))
        for entry in snapshot
        if _keep_typed_assistant(entry, source, info, current_pin, runtime_id)
    )
    ordered = sorted(candidates, key=lambda triple: triple[0].sequence)[-context_turns:]
    messages = [
        {"role": "user" if entry.kind == "conversation.user" else "assistant", "content": text}
        for entry, text, _scope in ordered
    ]
    consumed = [scope for _entry, _text, scope in ordered]
    consumed_memory = any(
        entry.kind == "conversation.converse" and "memory_context" in entry.payload
        for entry, _text, _scope in ordered
    )
    return ContextProjection(
        messages=messages,
        consumed_scopes=consumed,
        delivery=None,
        consumed_memory=consumed_memory,
    )


def typed_history(recent, source, info, context_turns):
    """Apply revisions before selecting typed turns; retain their full existing text."""
    return typed_history_projection(
        recent, source, info, context_turns, memory_context=None, runtime_id=None
    ).messages


def typed_history_scoped(recent, source, info, context_turns):
    """Message list plus the sticky floors actually consumed.

    Returns (messages, consumed_scopes). consumed_scopes holds one floor per
    included turn in source order: folded user floors and per-reply entry
    scopes. Missing/invalid provenance is already folded to local_only, so
    combine_scopes over this list plus current input and personality cannot
    promote private history to a public reply. typed_history() stays as the
    narrow compatibility projection returning only the message list.
    """
    projection = typed_history_projection(
        recent, source, info, context_turns, memory_context=None, runtime_id=None
    )
    return projection.messages, projection.consumed_scopes
