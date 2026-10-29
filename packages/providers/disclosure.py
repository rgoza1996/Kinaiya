"""Native entry-scope admission shared by window consumers and transcript tools."""


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


async def source_scope(journal, source_id, resolved=None):
    """Fold one bounded native source/revision chain without granting missing consent.

    A full metadata page cannot prove the whole chain and conservatively stays
    local. Read failures propagate: callers cannot infer authorization from an
    unavailable journal. This does not classify arbitrary workspace contents.
    """
    original = await journal.entry(source_id)
    if resolved is None:
        resolved = await journal.resolve(source_id)
    if (original.session_id, original.thread_id, original.epoch, original.kind) != (
        resolved.session_id,
        resolved.thread_id,
        resolved.epoch,
        resolved.kind,
    ):
        return "local_only"
    scope = combine_scopes(
        original.payload.get("content_scope", "local_only"),
        resolved.payload.get("content_scope", "local_only"),
    )
    if original.kind != "conversation.user":
        return scope
    revisions = await journal.read(
        after=original.sequence,
        session_id=original.session_id,
        kinds=["conversation.revision"],
        limit=200,
    )
    if len(revisions) >= 200:
        return "local_only"
    revision = original.payload.get("revision", 1)
    if type(revision) is not int or revision < 1:
        return "local_only"
    for entry in revisions:
        if entry.payload.get("target_event_id") != source_id:
            continue
        if (
            (entry.session_id, entry.thread_id, entry.epoch)
            != (original.session_id, original.thread_id, original.epoch)
            or entry.origin not in {"authenticated-user", "converse"}
            or type(entry.payload.get("revision")) is not int
            or entry.payload["revision"] != revision + 1
            or not isinstance(entry.payload.get("text"), str)
        ):
            return "local_only"
        revision = entry.payload["revision"]
        scope = combine_scopes(scope, entry.payload.get("content_scope", "local_only"))
    if type(resolved.payload.get("revision", 1)) is not int or revision != resolved.payload.get(
        "revision", 1
    ):
        return "local_only"
    return scope
