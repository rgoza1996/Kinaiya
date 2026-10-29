"""Bounded native playback qualifications for generated live-window history."""

import json
import math
import re

from packages.contracts.models import SpeechWindow
from packages.providers.disclosure import scope_allowed
from services.converse.history import MAX_INTEGER, fold_users, native_snapshot

MAX_BLOCK_BYTES = 8192
MAX_PLAYED_SECONDS = 86400
STATUSES = {"generated", "playing", "played", "interrupted", "delivery_unknown"}
PREFIX = (
    "Native playback context v1\n"
    "This metadata qualifies only the indexed prior assistant messages. Their text is generated, "
    "not proof of heard speech. Receipts are client reports and conservative estimates, not proof "
    "of physical audibility or exact heard words. A stop_requested is a server instruction, not "
    "proof the client/device stopped; it does not erase a prior played report. Missing evidence "
    "means delivery_unknown. Never derive a spoken prefix or work completion from these facts.\n"
)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is not None


def _integer(value, minimum=1):
    return type(value) is int and minimum <= value <= MAX_INTEGER


def _number(value, maximum):
    return type(value) in {int, float} and 0 <= value <= maximum and math.isfinite(value)


def _allowed(info, payload):
    scope = payload.get("content_scope", "local_only")
    return isinstance(scope, str) and scope_allowed(info, scope)


def _receipt(entry, assistant, info):
    p, a = entry.payload, assistant.payload
    if (
        entry.sequence <= assistant.sequence
        or entry.causes != [assistant.event_id]
        or p.get("event_id") != assistant.event_id
        or p.get("chunk_id") != a["chunk_id"]
        or not _integer(p.get("generation_epoch"))
        or p["generation_epoch"] != a["generation_epoch"]
        or not isinstance(p.get("status"), str)
        or p["status"] not in STATUSES
        or p.get("alignment") != "conservative_estimate"
        or p.get("schema_version", "1.0") != "1.0"
        or "control" in p
        or ("runtime_id" in p and p["runtime_id"] != a["runtime_id"])
        or ("content_scope" in p and not _allowed(info, p))
        or not _number(p.get("played_seconds", 0), MAX_PLAYED_SECONDS)
        or (
            p.get("estimated_text_fraction") is not None
            and not _number(p["estimated_text_fraction"], 1)
        )
    ):
        return None
    return {
        "sequence": entry.sequence,
        "status": p["status"],
        "alignment": "conservative_estimate",
        "played_seconds": p.get("played_seconds", 0),
        **(
            {"estimated_text_fraction": p["estimated_text_fraction"]}
            if p.get("estimated_text_fraction") is not None
            else {}
        ),
    }


def _stop(entry, assistant, info):
    p, a = entry.payload, assistant.payload
    source_id = p.get("source_event_id")
    if (
        p.get("control") != "generation_stop"
        or p.get("runtime_id") != a["runtime_id"]
        or p.get("status") != "interrupted"
        or p.get("alignment") != "delivery_unknown"
        or not _integer(p.get("generation_epoch"), 0)
        or not _integer(p.get("next_generation_epoch"))
        or p["next_generation_epoch"] <= max(p["generation_epoch"], a["generation_epoch"])
        or any(
            key in p
            for key in ("event_id", "chunk_id", "played_seconds", "estimated_text_fraction")
        )
        or (source_id is not None and not _identifier(source_id))
        or entry.causes != ([source_id] if source_id is not None else [])
        or ("content_scope" in p and not _allowed(info, p))
    ):
        return None
    return {
        "sequence": entry.sequence,
        "next_generation_epoch": p["next_generation_epoch"],
        "status": "stop_requested",
    }


def _delivery_row(assistant, index, snapshot, info):
    row = {
        "history_index": index,
        "latest_status": "delivery_unknown",
        "receipts": [],
        "played_report_sequence": None,
    }
    if _identifier(assistant.event_id):
        row["event_id"] = assistant.event_id
    a = assistant.payload
    if not (
        _identifier(assistant.event_id)
        and _identifier(a.get("chunk_id"))
        and _identifier(a.get("runtime_id"))
        and _integer(a.get("generation_epoch"))
    ):
        return row
    row.update({key: a[key] for key in ("chunk_id", "runtime_id", "generation_epoch")})
    for entry in snapshot:
        if (
            entry.kind != "delivery.updated"
            or entry.origin != "converse"
            or not _integer(entry.sequence)
        ):
            continue
        receipt = _receipt(entry, assistant, info)
        if receipt is not None:
            row["latest_status"] = receipt["status"]
            row["latest_receipt_sequence"] = receipt["sequence"]
            row["receipts"] = [*row["receipts"], receipt][-3:]
            if receipt["status"] == "played":
                row["played_report_sequence"] = receipt["sequence"]
        stop = _stop(entry, assistant, info)
        if stop is not None:
            row["generation_stop"] = stop
    return row


def _block(rows):
    def encode():
        return PREFIX + json.dumps({"version": 1, "history": rows}, separators=(",", ":"))

    content = encode()
    if len(content.encode()) > MAX_BLOCK_BYTES:
        for row in rows:
            if row["receipts"]:
                row["receipts"] = []
                row["receipt_details_omitted"] = True
        content = encode()
    if len(content.encode()) > MAX_BLOCK_BYTES:
        # Every included assistant still receives an unknown qualification. The
        # observed played report stays explicit even if optional detail is gone.
        rows[:] = [
            {
                key: row[key]
                for key in (
                    "history_index",
                    "latest_status",
                    "receipts",
                    "played_report_sequence",
                    "event_id",
                )
                if key in row
            }
            for row in rows
        ]
        for row in rows:
            row["latest_status"] = "delivery_unknown"
            row["receipts"] = []
            row["receipt_details_omitted"] = True
        content = encode()
    return content


def window_context(recent, source, info):
    messages, delivery, _ = window_context_scoped(recent, source, info)
    return messages, delivery


def window_context_scoped(recent, source, info):
    """Use one contiguous native suffix; later corrections/receipts qualify earlier text."""
    snapshot = native_snapshot(recent, source)
    current = SpeechWindow.model_validate(source.payload)
    users = fold_users(snapshot, lambda entry: _allowed(info, entry.payload))
    windows = {}
    prior_utterances = set()
    for entry in snapshot:
        p = entry.payload
        if entry.kind != "conversation.window" or entry.origin != "authenticated-user":
            continue
        try:
            window = SpeechWindow.model_validate(p)
        except ValueError:
            continue
        if (window.session_id, window.thread_id, window.epoch) != (
            entry.session_id,
            entry.thread_id,
            entry.epoch,
        ):
            continue
        if entry.sequence < source.sequence:
            prior_utterances.add(window.utterance_id)
        rank = window.window_index, window.revision, entry.sequence
        previous = windows.get(window.utterance_id)
        if previous is None or rank > previous[0]:
            windows[window.utterance_id] = rank, entry, window

    candidates = [
        (user.entry, user.text, user.floor)
        for user in users.values()
        if user.entry.sequence < source.sequence and user.permitted and isinstance(user.text, str)
    ]
    candidates.extend(
        (entry, window.text, window.content_scope)
        for _, entry, window in windows.values()
        if window.utterance_id in prior_utterances
        and window.utterance_id != current.utterance_id
        and _allowed(info, entry.payload)
    )
    candidates.extend(
        (e, e.payload["text"], e.payload.get("content_scope", "local_only"))
        for e in snapshot
        if e.kind == "conversation.converse"
        and e.origin == "converse"
        and e.sequence < source.sequence
        and isinstance(e.payload.get("text"), str)
        and _allowed(info, e.payload)
    )
    candidates = sorted(candidates, key=lambda pair: pair[0].sequence)[-8:]
    messages, rows, scopes = [], [], []
    for index, (entry, text, scope) in enumerate(candidates):
        assistant = entry.kind == "conversation.converse"
        messages.append({"role": "assistant" if assistant else "user", "content": text[:2000]})
        scopes.append(scope)
        if assistant:
            rows.append(_delivery_row(entry, index, snapshot, info))
    return messages, _block(rows) if rows else None, scopes
