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
