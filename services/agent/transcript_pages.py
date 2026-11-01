"""Bounded transcript views with native provenance and disclosure continuity."""

import copy
import hashlib
import json
import re
import unicodedata
from types import SimpleNamespace

from core.policy import PolicyDenied
from packages.contracts.agent_export import MAX_AGENT_CONTEXT_REFS
from packages.contracts.models import Evidence
from packages.contracts.transcript_scan import (
    MAX_TRANSCRIPT_INTEGER,
    TranscriptScanPage,
    TranscriptScanRequest,
    checked_entry,
    checked_scan,
    transcript_bytes,
    transcript_json,
)
from packages.providers.disclosure import (
    ContextScopeClosure,
    combine_scopes,
    context_scope,
    entry_allowed,
    scope_allowed,
)
from services.agent.tools import ToolFailure

_NEW = {
    "entry_view",
    "offset",
    "max_chars",
    "expected_revision",
    "expected_entry_sha256",
    "scope_token",
    "through_sequence",
}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 131072
_MAX_CHARS = 12000
_INVALID = "Invalid transcript page arguments"
_HINT = "Use read_transcript with paged:true for bounded complete transcript retrieval"


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _integer(value, minimum=0, maximum=MAX_TRANSCRIPT_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ToolFailure(_INVALID)
    return value


def _identifier(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or any(unicodedata.category(ch) == "Cc" for ch in value)
    ):
        raise ToolFailure(_INVALID)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ToolFailure(_INVALID) from None
    return value


def _hash(value):
    if value is not None and (type(value) is not str or _HASH.fullmatch(value) is None):
        raise ToolFailure(_INVALID)
    return value


def _entry(entry):
    try:
        entry = checked_entry(entry)
        complete = transcript_bytes(entry)
        # Keep a detached complete view across subsequent asynchronous reads;
        # a mutable nested payload cannot silently change the slice's digest.
        entry = checked_entry(transcript_json(complete))
        text = entry.payload.get("text", "")
        if type(text) is not str:
            raise ValueError
        text.encode("utf-8")
        revision = entry.payload.get("revision", 1)
        _integer(revision, 1)
        digest = hashlib.sha256(complete).hexdigest()
        return entry, text, revision, digest
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ToolFailure("Transcript entry metadata is unavailable") from None


def _slice(entry, text, revision, digest, view, offset, count):
    body = text[offset : offset + count]
    following = offset + len(body) if offset + len(body) < len(text) else None
    return {
        "event_id": entry.event_id,
        "kind": entry.kind,
        "origin": entry.origin,
        "sequence": entry.sequence,
        "revision": revision,
        "text": body,
        "entry_view": view,
        "created_at": entry.created_at.isoformat().replace("+00:00", "Z"),
        "metadata": {key: value for key, value in entry.payload.items() if key != "text"},
        "offset": offset,
        "total_chars": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "entry_sha256": digest,
        "next_offset": following,
        "truncated": following is not None,
    }


async def read_transcript(runtime, rec, args):
    runtime._standing_action(rec, "read_transcript")
    if type(args) is not dict or "paged" in args and type(args["paged"]) is not bool:
        raise ToolFailure(_INVALID)
    paged = args.get("paged", False)
    if not paged and _NEW.intersection(args):
        raise ToolFailure(_HINT)
    if paged and args.keys() - (_NEW | {"paged", "event_ids", "after", "limit"}):
        raise ToolFailure(_INVALID)
    event_ids = args.get("event_ids", [])
    if type(event_ids) is not list or len(event_ids) > 20:
        raise ToolFailure("Transcript retrieval is limited to 20 entries per request")
    if paged:
        for event_id in event_ids:
            _identifier(event_id)
        if event_ids and len(event_ids) != 1:
            raise ToolFailure(_INVALID)
        limit = _integer(args.get("limit", 1 if event_ids else 8), 1, 20)
        after = _integer(args.get("after", 0))
        offset = _integer(args.get("offset", 0))
        maximum = _integer(args.get("max_chars", 2000), 1, _MAX_CHARS)
        expected_revision = args.get("expected_revision")
        if expected_revision is not None:
            _integer(expected_revision, 1)
        expected_hash = _hash(args.get("expected_entry_sha256"))
        incoming_token = _hash(args.get("scope_token"))
        through = args.get("through_sequence")
        if through is not None:
            _integer(through)
        view = args.get("entry_view")
        if view is not None and (type(view) is not str or view not in {"resolved", "immutable"}):
            raise ToolFailure(_INVALID)
        if event_ids:
            view = view or "resolved"
            if (
                after
                or through is not None
                or limit != 1
                or offset
                and (expected_revision is None or expected_hash is None or incoming_token is None)
            ):
                raise ToolFailure(_INVALID)
        else:
            view = "immutable" if view is None else view
            if (
                view != "immutable"
                or offset
                or expected_revision is not None
                or expected_hash is not None
                or (through is None) != (incoming_token is None)
            ):
                raise ToolFailure(_INVALID)
    else:
        try:
            limit = int(args.get("limit", 8))
            after = max(0, int(args.get("after", 0)))
        except (ValueError, TypeError, OverflowError):
            raise ToolFailure(_INVALID) from None
        if not 1 <= limit <= 20:
            raise ToolFailure("Transcript retrieval is limited to 20 entries per request")
        view, incoming_token = ("resolved" if event_ids else "immutable"), None

    # Capture the actual adapter, not merely equal provider metadata. A later
    # adapter replacement must not inherit a continuation or its admitted data.
    task = rec.task
    provider, journal = runtime.provider_for(rec), runtime.journal
    provider_info = copy.deepcopy(provider.info)
    identity = (task.task_id, task.objective_revision, task.session_id, task.thread_id, task.epoch)
    objective, sources = task.objective, tuple(task.source_event_ids)
    source_revisions = dict(rec.source_revisions)
    retained_roots = frozenset(rec.context_event_ids)
    initial_floor = task.content_scope

    def current():
        runtime._workspace_current(rec)
        runtime._check_completion_open(rec, require_active=True)
        if (
            runtime._closed
            or rec.closing
            or rec.cancel_requested
            or rec.recovered
            or rec.pending_steer is not None
            or runtime._tasks.get(identity[0]) is not rec
            or rec.task is not task
            or runtime.journal is not journal
            or runtime.provider_for(rec) is not provider
            or provider.info != provider_info
            or (task.task_id, task.objective_revision, task.session_id, task.thread_id, task.epoch)
            != identity
            or task.objective != objective
            or tuple(task.source_event_ids) != sources
            or rec.source_revisions != source_revisions
            or rec.context_event_ids != retained_roots
            or combine_scopes(initial_floor, task.content_scope) != task.content_scope
        ):
            raise PolicyDenied("Transcript retrieval authority changed")
        if not scope_allowed(provider_info, task.content_scope):
            raise PolicyDenied("Task disclosure does not permit the configured Agent provider")

    def token():
        current()
        body = {
            "schema_version": "1.0",
            "task_id": identity[0],
            "objective_revision": identity[1],
            "session_id": identity[2],
            "thread_id": identity[3],
            "epoch": identity[4],
            "provider": provider_info.model_dump(mode="json"),
            "content_scope": task.content_scope,
        }
        return hashlib.sha256(b"kinaiya.agent.transcript-page-scope.v1\0" + _json(body)).hexdigest()

    current()
    if incoming_token is not None and incoming_token != token():
        raise ToolFailure("Transcript continuation scope changed; restart the page")

    async def call(method, *values, **keywords):
        current()
        try:
            return await getattr(journal, method)(*values, **keywords)
        except (PolicyDenied, ToolFailure):
            raise
        except Exception:
            # Old journal adapters may attach the entire malformed native
            # value to validation errors. No source body belongs in a refusal.
            raise ToolFailure("Transcript source is unavailable") from None
        finally:
            current()

    async def raw_entry(*values, **keywords):
        return await call("entry", *values, **keywords)

    async def resolved_entry(*values, **keywords):
        return await call("resolve", *values, **keywords)

    async def raw_read(*values, **keywords):
        return await call("read", *values, **keywords)

    guarded = SimpleNamespace(entry=raw_entry, resolve=resolved_entry, read=raw_read)

    async def session_current():
        session = await call("session", identity[2])
        if (session.session_id, session.thread_id, session.epoch) != identity[2:]:
            raise PolicyDenied("Transcript reference outside the task's current session epoch")

    await session_current()
    await runtime._task_scope(rec, persist=False)
    current()
    if event_ids:
        method = "resolve" if view == "resolved" else "entry"
        entries = [await call(method, event_id) for event_id in event_ids]
        native = None
    elif paged:
        request = TranscriptScanRequest(
            schema_version="1.0",
            session_id=identity[2],
            thread_id=identity[3],
            epoch=identity[4],
            after=after,
            through_sequence=through,
            limit=limit,
        )
        try:
            native = checked_scan(await call("transcript_scan", request), TranscriptScanPage)
            native = native.validate_request(request)
        except PolicyDenied:
            raise
        except Exception:
            raise ToolFailure("Native transcript scan is unavailable") from None
        entries, through = native.entries, native.through_sequence
    else:
        entries = await call("read", after=after, session_id=identity[2], limit=limit)
        native = None

    selected, retained, proof = [], [], []
    text_size, inspected = 0, after
    consumed = True
    selected_scope = task.content_scope

    def envelope(items):
        return {
            "schema_version": "1.0",
            "scope_token": token(),
            "entries": items,
            "through_sequence": None if event_ids else through,
            "next_after": None if event_ids else MAX_TRANSCRIPT_INTEGER,
            "scan_exhausted": None if event_ids else False,
        }

    for original in entries:
        entry, text, revision, digest = _entry(original)
        if (entry.session_id, entry.thread_id, entry.epoch) != identity[2:]:
            raise PolicyDenied("Transcript reference outside the task's current session epoch")
        try:
            floor = await context_scope(guarded, entry)
        except PolicyDenied:
            raise
        except Exception:
            raise PolicyDenied("Transcript context disclosure cannot be verified") from None
        if not entry_allowed(provider_info, entry) or not scope_allowed(
            provider_info, combine_scopes(task.content_scope, selected_scope, floor)
        ):
            if event_ids:
                raise PolicyDenied("Transcript window disclosure is not authorized for this model")
            inspected = entry.sequence
            continue
        if paged:
            if event_ids and (
                offset > len(text)
                or expected_revision is not None
                and expected_revision != revision
                or expected_hash is not None
                and expected_hash != digest
            ):
                raise ToolFailure(
                    "Transcript entry changed or its offset is invalid; restart the page"
                )
            start = offset if event_ids else 0
            available = min(maximum, len(text) - start)
            # Metadata cannot be split. Probe its complete envelope in isolation
            # before choosing a prefix; no identity or metadata is silently lost.
            empty = _slice(entry, text, revision, digest, view, start, 0)
            if len(_json(envelope([empty]))) > _MAX_BYTES:
                raise ToolFailure("Transcript metadata exceeds the bounded page size")
            high = min(available, _MAX_CHARS - text_size)
            low = 0
            while low < high:
                middle = (low + high + 1) // 2
                probe = _slice(entry, text, revision, digest, view, start, middle)
                if len(_json(envelope([*selected, probe]))) <= _MAX_BYTES:
                    low = middle
                else:
                    high = middle - 1
            item = _slice(entry, text, revision, digest, view, start, low)
            if (available and low == 0) or len(_json(envelope([*selected, item]))) > _MAX_BYTES:
                if not selected:
                    raise ToolFailure("Transcript metadata leaves no room for a progressing page")
                consumed = False
                break
        else:
            body = text[:2000]
            if text_size + len(body) > _MAX_CHARS:
                raise ToolFailure(_HINT)
            item = {
                "event_id": entry.event_id,
                "kind": entry.kind,
                "origin": entry.origin,
                "sequence": entry.sequence,
                "revision": revision,
                "text": body,
            }
            if len(text) > len(body):
                item.update(
                    entry_view=view,
                    truncated=True,
                    total_chars=len(text),
                    continuation_hint=(
                        f"Use read_transcript with paged:true, event_ids:[{json.dumps(entry.event_id)}] "
                        f"and entry_view:{json.dumps(view)} to retrieve complete text"
                    ),
                )
        if (
            len(rec.context_event_ids | {entry.event_id for entry in retained} | {entry.event_id})
            > MAX_AGENT_CONTEXT_REFS
        ):
            raise ToolFailure("Transcript context provenance limit reached")
        selected.append(item)
        retained.append(entry)
        text_size += len(item["text"])
        selected_scope = combine_scopes(selected_scope, floor)
        inspected = entry.sequence
        proof.append(
            {
                "event_id": entry.event_id,
                "entry_view": view,
                "sequence": entry.sequence,
                "revision": revision,
                "entry_sha256": digest,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "offset": item.get("offset", 0),
                "end_offset": item.get("offset", 0) + len(item["text"]),
                "next_offset": (
                    item.get("next_offset")
                    if paged
                    else len(item["text"])
                    if len(item["text"]) < len(text)
                    else None
                ),
                "total_chars": len(text),
                "source_event_ids": list(entry.causes),
            }
        )

    # Enforce the aggregate closure over old and newly selected roots. Denied
    # candidates used independent closures and never consume retained capacity.
    closure = ContextScopeClosure(*identity[2:])
    roots = rec.context_event_ids | {entry.event_id for entry in retained}
    if len(roots) > MAX_AGENT_CONTEXT_REFS:
        raise ToolFailure("Transcript context provenance limit reached")
    final_scope = selected_scope
    try:
        for event_id in sorted(roots):
            final_scope = combine_scopes(
                final_scope,
                await context_scope(guarded, await call("entry", event_id), closure=closure),
            )
        # A resolved view must still be the complete view whose slice and hash
        # are returned; corrections during asynchronous disclosure reads refuse.
        for entry in retained:
            fresh = await call("resolve" if view == "resolved" else "entry", entry.event_id)
            if transcript_bytes(checked_entry(fresh)) != transcript_bytes(entry):
                raise ToolFailure("Transcript entry changed; restart the page")
    except (PolicyDenied, ToolFailure):
        raise
    except Exception:
        raise PolicyDenied("Transcript context disclosure cannot be verified") from None
    await runtime._task_scope(rec, persist=False)
    current()
    await session_current()
    final_scope = combine_scopes(task.content_scope, final_scope)
    if not scope_allowed(provider_info, final_scope):
        raise PolicyDenied("Transcript context disclosure is no longer authorized")
    task.content_scope = final_scope
    rec.context_event_ids.update(roots)
    retained_roots = frozenset(rec.context_event_ids)
    if paged:
        result = envelope(selected)
        if not event_ids:
            exhausted = consumed and native.exhausted
            result["next_after"] = None if exhausted else inspected
            result["scan_exhausted"] = exhausted
            if not exhausted and inspected <= after:
                raise ToolFailure("Native transcript scan made no progress")
    else:
        result = selected
    encoded = _json(result)
    if len(encoded) > _MAX_BYTES or text_size > _MAX_CHARS:
        raise ToolFailure("Transcript response exceeds the bounded page size")
    current()
    evidence = Evidence(
        kind="read",
        summary=f"Retrieved {len(selected)} scoped journal entries",
        output=json.dumps({"event_ids": [entry.event_id for entry in retained], "entries": proof}),
        objective_revision=identity[1],
    )
    return encoded.decode("utf-8"), evidence
