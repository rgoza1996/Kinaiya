"""Native entry-scope admission shared by window consumers and transcript tools."""

from collections import deque

from packages.contracts.models import SpeechWindow

# This bounds native provenance traversal, not a model's context capacity. A
# dependency is never dropped to make a retained transcript row admissible.
MAX_CONTEXT_PROVENANCE_NODES = 256


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


def context_source_id(entry):
    """Identify a context row's source without treating it as an instruction."""
    if entry.kind == "conversation.revision":
        target_id = entry.payload.get("target_event_id")
        if not isinstance(target_id, str) or not target_id:
            raise ValueError("Native context source is unavailable")
        return target_id
    return entry.event_id


def validate_context_source(entry, target):
    if (
        (entry.session_id, entry.thread_id, entry.epoch)
        != (
            target.session_id,
            target.thread_id,
            target.epoch,
        )
        or (
            entry.kind == "conversation.revision"
            and (
                target.kind != "conversation.user"
                or target.origin != "authenticated-user"
                or target.event_id != context_source_id(entry)
            )
        )
        or (
            entry.kind != "conversation.revision"
            and (
                target.kind != entry.kind
                or (entry.kind != "conversation.window" and target.event_id != entry.event_id)
            )
        )
    ):
        raise ValueError("Native context source is unavailable")


def context_revision_query(original, resolved):
    """Select the same bounded source chain in async and atomic admission."""
    if original.kind == "conversation.window":
        window = SpeechWindow.model_validate(resolved.payload)
        return {
            "session_id": original.session_id,
            "thread_id": original.thread_id,
            "epoch": original.epoch,
            "kinds": ["conversation.window"],
            "utterance_id": window.utterance_id,
            "window_index": window.window_index,
            "limit": 200,
        }
    if original.kind == "conversation.user":
        return {
            "after": original.sequence,
            "session_id": original.session_id,
            "kinds": ["conversation.revision"],
            "limit": 200,
        }
    return None


class ContextScopeClosure:
    """Fold retained rows and their complete native disclosure dependencies.

    Causes carry provenance, never instruction authority or grants. Every raw
    causal edge must point to an older row in the same session/thread/epoch;
    this also rejects cycles without rejecting a shared DAG. Resolving a user
    or window includes every bounded revision and those revisions' causes.
    """

    def __init__(self, session_id, thread_id, epoch):
        self.identity = (session_id, thread_id, epoch)
        self.scope = "public_synthetic"
        self._pending = deque()
        self._known = set()
        self._observed = {}
        self._before = {}

    def _reserve(self, event_id):
        if type(event_id) is not str or not 1 <= len(event_id) <= 128 or "\x00" in event_id:
            raise ValueError("Native context provenance is unavailable")
        try:
            event_id.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("Native context provenance is unavailable") from exc
        if event_id not in self._known:
            if len(self._known) >= MAX_CONTEXT_PROVENANCE_NODES:
                raise ValueError("Native context provenance exceeds its resource bound")
            self._known.add(event_id)
            return True
        return False

    def queue(self, event_id, *, before=None):
        fresh = self._reserve(event_id)
        if before is not None:
            self._before[event_id] = min(before, self._before.get(event_id, before))
            observed = self._observed.get(event_id)
            if observed is not None and observed.sequence >= self._before[event_id]:
                raise ValueError("Native context provenance is unavailable")
        if fresh:
            self._pending.append(event_id)

    def next_event_id(self):
        while self._pending:
            event_id = self._pending.popleft()
            if event_id not in self._observed:
                return event_id
        return None

    def observe(self, entry, original, resolved, revisions):
        validate_context_source(entry, original)
        validate_context_source(entry, resolved)
        scope = folded_source_scope(original, resolved, revisions, require_complete=True)
        if original.kind == "conversation.window":
            rows = revisions
        elif original.kind == "conversation.user":
            rows = [original] + [
                row for row in revisions if row.payload.get("target_event_id") == original.event_id
            ]
        else:
            if original != resolved:
                raise ValueError("Native context source is unavailable")
            rows = [original]
        if not any(row == entry for row in rows):
            raise ValueError("Native context source is unavailable")
        for row in rows:
            self._reserve(row.event_id)
            if (
                (row.session_id, row.thread_id, row.epoch) != self.identity
                or type(row.sequence) is not int
                or row.sequence < 1
                or type(row.causes) is not list
                or len(row.causes) > 32
                or row.sequence >= self._before.get(row.event_id, row.sequence + 1)
            ):
                raise ValueError("Native context provenance is unavailable")
            previous = self._observed.get(row.event_id)
            if previous is not None and previous != row:
                raise ValueError("Native context provenance is unavailable")
            self._observed[row.event_id] = row
        for row in rows:
            for cause in row.causes:
                self.queue(cause, before=row.sequence)
        self.scope = combine_scopes(self.scope, scope)


async def context_scope(journal, entry, *, closure=None):
    """Recheck the bounded causal closure at continuation/effect fences.

    The Store performs this traversal atomically at each actual upload. Async
    reads use identical folds; an unavailable or inconsistent chain is refused.
    A shared closure is local to one validation pass, never reused at a later
    fence. Unretained scan candidates must use independent closures.
    """
    if closure is None:
        closure = ContextScopeClosure(entry.session_id, entry.thread_id, entry.epoch)
    if type(closure) is not ContextScopeClosure or closure.identity != (
        entry.session_id,
        entry.thread_id,
        entry.epoch,
    ):
        raise ValueError("Native context provenance is unavailable")
    closure.queue(entry.event_id)
    while (event_id := closure.next_event_id()) is not None:
        current = await journal.entry(event_id)
        if current.event_id != event_id:
            raise ValueError("Native context provenance is unavailable")
        source_id = context_source_id(current)
        original = current if source_id == event_id else await journal.entry(source_id)
        resolved = await journal.resolve(source_id)
        validate_context_source(current, original)
        validate_context_source(current, resolved)
        query = context_revision_query(original, resolved)
        revisions = await journal.read(**query) if query is not None else []
        closure.observe(current, original, resolved, revisions)
    return combine_scopes(entry.payload.get("content_scope", "local_only"), closure.scope)
