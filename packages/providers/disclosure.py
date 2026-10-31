"""Native entry-scope admission shared by window consumers and transcript tools."""

from packages.contracts.models import SpeechWindow


def scope_allowed(info, scope):
    if not isinstance(scope, str) or scope not in {
        "local_only",
        "public_synthetic",
        "private_cloud_authorized",
    }:
        return False
    if info.local:
        return True
    if info.provider == "meta" or "contributor" in info.model.lower():
        return scope == "public_synthetic"
    return scope in {"public_synthetic", "private_cloud_authorized"}


def entry_allowed(info, entry):
    """Missing historical scope never implies permission to export content."""
    return scope_allowed(info, entry.payload.get("content_scope", "local_only"))


def combine_scopes(*scopes):
    """Keep the most restrictive input scope; missing/invalid provenance stays local."""
    rank = {"public_synthetic": 0, "private_cloud_authorized": 1, "local_only": 2}
    if not scopes or any(not isinstance(scope, str) or scope not in rank for scope in scopes):
        return "local_only"
    return max(scopes, key=rank.__getitem__)


def _same_source_scope(original, resolved):
    return (original.session_id, original.thread_id, original.epoch, original.kind) == (
        resolved.session_id,
        resolved.thread_id,
        resolved.epoch,
        resolved.kind,
    )


def folded_source_scope(original, resolved, revisions, *, require_complete=False):
    """Pure bounded scope fold shared by async reads and atomic native admission."""

    def unverified():
        if require_complete:
            raise ValueError("Native source chain is unavailable")
        return "local_only"

    if not _same_source_scope(original, resolved):
        return unverified()
    scope = combine_scopes(
        original.payload.get("content_scope", "local_only"),
        resolved.payload.get("content_scope", "local_only"),
    )
    if original.kind == "conversation.window":
        if len(revisions) >= 200:
            return unverified()
        try:
            selected = SpeechWindow.model_validate(resolved.payload)
            original_window = SpeechWindow.model_validate(original.payload)
            slot = (
                selected.session_id,
                selected.thread_id,
                selected.epoch,
                selected.utterance_id,
                selected.window_index,
            )
            if (
                original.origin != "authenticated-user"
                or resolved.origin != "authenticated-user"
                or (original.session_id, original.thread_id, original.epoch) != slot[:3]
                or (resolved.session_id, resolved.thread_id, resolved.epoch) != slot[:3]
            ):
                return unverified()
            if slot != (
                original_window.session_id,
                original_window.thread_id,
                original_window.epoch,
                original_window.utterance_id,
                original_window.window_index,
            ):
                return unverified()
            expected, previous_sequence, seen = 1, 0, set()
            for entry in revisions:
                window = SpeechWindow.model_validate(entry.payload)
                entry_slot = (
                    window.session_id,
                    window.thread_id,
                    window.epoch,
                    window.utterance_id,
                    window.window_index,
                )
                if (
                    entry_slot != slot
                    or entry.kind != "conversation.window"
                    or entry.origin != "authenticated-user"
                    or (entry.session_id, entry.thread_id, entry.epoch) != slot[:3]
                    or window.revision != expected
                    or entry.sequence <= previous_sequence
                ):
                    return unverified()
                previous_sequence, expected = entry.sequence, expected + 1
                seen.add(entry.event_id)
                scope = combine_scopes(scope, window.content_scope)
            if (
                expected != selected.revision + 1
                or original.event_id not in seen
                or not revisions
                or revisions[-1].event_id != resolved.event_id
            ):
                return unverified()
        except (ValueError, TypeError, AttributeError):
            return unverified()
        return scope
    if original.kind != "conversation.user":
        return scope
    if len(revisions) >= 200:
        return unverified()
    revision = original.payload.get("revision", 1)
    if type(revision) is not int or not 1 <= revision <= 9_007_199_254_740_991:
        return unverified()
    for entry in revisions:
        if entry.payload.get("target_event_id") != original.event_id:
            continue
        if (
            (entry.session_id, entry.thread_id, entry.epoch)
            != (original.session_id, original.thread_id, original.epoch)
            or entry.origin not in {"authenticated-user", "converse"}
            or type(entry.payload.get("revision")) is not int
            or not 1 <= entry.payload["revision"] <= 9_007_199_254_740_991
            or entry.payload["revision"] != revision + 1
            or not isinstance(entry.payload.get("text"), str)
        ):
            return unverified()
        revision = entry.payload["revision"]
        scope = combine_scopes(scope, entry.payload.get("content_scope", "local_only"))
    resolved_revision = resolved.payload.get("revision", 1)
    if (
        type(resolved_revision) is not int
        or not 1 <= resolved_revision <= 9_007_199_254_740_991
        or revision != resolved_revision
    ):
        return unverified()
    return scope


async def source_scope(journal, source_id, resolved=None):
    """Fold one bounded native source/revision chain without granting missing consent.

    A full metadata page cannot prove the whole chain and conservatively stays
    local. Read failures propagate: callers cannot infer authorization from an
    unavailable journal. This does not classify arbitrary workspace contents.
    """
    original = await journal.entry(source_id)
    if resolved is None:
        resolved = await journal.resolve(source_id)
    if not _same_source_scope(original, resolved):
        return "local_only"
    if original.kind == "conversation.window":
        window = SpeechWindow.model_validate(original.payload)
        revisions = await journal.read(
            session_id=original.session_id,
            thread_id=original.thread_id,
            epoch=original.epoch,
            kinds=["conversation.window"],
            utterance_id=window.utterance_id,
            window_index=window.window_index,
            limit=200,
        )
        return folded_source_scope(original, resolved, revisions, require_complete=True)
    revisions = (
        await journal.read(
            after=original.sequence,
            session_id=original.session_id,
            kinds=["conversation.revision"],
            limit=200,
        )
        if original.kind == "conversation.user"
        else []
    )
    return folded_source_scope(original, resolved, revisions)
