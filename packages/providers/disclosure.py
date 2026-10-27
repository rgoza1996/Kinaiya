"""Native entry-scope admission shared by window consumers and transcript tools."""


def scope_allowed(info, scope):
    if scope not in {"local_only", "public_synthetic", "private_cloud_authorized"}:
        return False
    if info.local:
        return True
    if info.provider == "meta" or "contributor" in info.model.lower():
        return scope == "public_synthetic"
    return scope in {"public_synthetic", "private_cloud_authorized"}


def entry_allowed(info, entry):
    """Legacy unscoped typed entries retain their existing consent path."""
    if entry.kind == "conversation.window" or "content_scope" in entry.payload:
        return scope_allowed(info, entry.payload.get("content_scope", "local_only"))
    return True
