"""Pure, bounded native history projection shared by typed and live-window replies."""

from dataclasses import dataclass

from packages.providers.disclosure import entry_allowed

MAX_INTEGER = 9_007_199_254_740_991


def native_snapshot(recent, source):
    """Filter one contiguous suffix without filling gaps from older or foreign history."""
    return sorted(
        (
            entry
            for entry in recent[-100:]
            if (entry.session_id, entry.thread_id, entry.epoch)
            == (source.session_id, source.thread_id, source.epoch)
        ),
        key=lambda entry: entry.sequence,
    )


def _revision(value):
    return type(value) is int and 1 <= value <= MAX_INTEGER


@dataclass
class UserHistory:
    entry: object
    revision: object
    text: object
    permitted: bool


def fold_users(snapshot, allowed):
    """Retain original positions and a sticky disclosure floor through native corrections."""
    users = {
        entry.event_id: UserHistory(
            entry,
            entry.payload.get("revision", 1),
            entry.payload.get("text"),
            _revision(entry.payload.get("revision", 1)) and allowed(entry),
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
            continue
        original.revision, original.text = payload["revision"], payload["text"]
        if "content_scope" in payload:
            original.permitted = original.permitted and allowed(entry)
    return users


def _typed_allowed(info, entry):
    # Missing scope keeps typed history's legacy consent path. A present
    # malformed scope is denied before the policy's set membership lookup.
    if "content_scope" in entry.payload and not isinstance(entry.payload["content_scope"], str):
        return False
    return entry_allowed(info, entry)


def typed_history(recent, source, info, context_turns):
    """Apply revisions before selecting typed turns; retain their full existing text."""
    snapshot = native_snapshot(recent, source)
    users = fold_users(snapshot, lambda entry: _typed_allowed(info, entry))
    candidates = [
        (user.entry, user.text)
        for user in users.values()
        if user.permitted and isinstance(user.text, str)
    ]
    candidates.extend(
        (entry, entry.payload["text"])
        for entry in snapshot
        if entry.kind == "conversation.converse"
        and entry.origin == "converse"
        and isinstance(entry.payload.get("text"), str)
        and _typed_allowed(info, entry)
    )
    return [
        {
            "role": "user" if entry.kind == "conversation.user" else "assistant",
            "content": text,
        }
        for entry, text in sorted(candidates, key=lambda pair: pair[0].sequence)[-context_turns:]
    ]
