"""Bounded owner reconciliation over existing memory receipts; read-only only.

Unlike mutation replay, an exact historical receipt does not require a current
namespace revision, enabled memory, a healthy graph, or source reconciliation.
The owner-only API is the authentication boundary; do not expose this helper as
a Converse/model tool. Original writes retain all existing admission checks.
"""

import hashlib
import re
import sqlite3

from pydantic import TypeAdapter

from packages.contracts.memory import MAX_SAFE, Identifier, MemoryBinding
from packages.contracts.memory_operations import MemoryOperationLookup, MemoryOperationResult
from packages.contracts.models import Session
from services.session.memory import BINDING, MemoryError, _json

_IDENTIFIER = TypeAdapter(Identifier)
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _mutation_fenced(memory, original, binding):
    """Observe irreversible native guards, never infer the original outcome."""
    try:
        namespace = memory.db.execute(
            """SELECT CASE WHEN typeof(revision)='integer' THEN revision END AS revision
                FROM memory_namespaces WHERE session_id=? AND thread_id=? AND epoch=?""",
            binding,
        ).fetchone()
        if namespace is not None:
            revision = namespace["revision"]
            if type(revision) is int and original.expected_revision < revision <= MAX_SAFE:
                return True
    except sqlite3.Error:
        # A second independent guard may still be observable. Never treat an
        # unreadable/missing namespace as a persisted revision or create it.
        pass
    try:
        row = memory.db.execute(
            """SELECT CASE WHEN typeof(value)='text'
                AND length(CAST(value AS BLOB))<=4096 THEN value END AS value
                FROM sessions WHERE session_id=?""",
            (original.session_id,),
        ).fetchone()
        if row is None:
            # Native creation generates fresh IDs; deletion cascades memory,
            # and a delayed write's _binding cannot recreate its Session.
            return True
        if row["value"] is None:
            return False
        current = Session.model_validate_json(row["value"], strict=True)
        if (
            current.session_id != original.session_id
            or not 1 <= current.epoch <= MAX_SAFE
            or _IDENTIFIER.validate_python(current.thread_id) != current.thread_id
        ):
            return False
        # Native reset only increases epoch and never rewrites the thread ID.
        # A future requested epoch must not be declared permanently fenced.
        return current.thread_id != original.thread_id or current.epoch > original.epoch
    except (sqlite3.Error, ValueError, TypeError):
        return False


def _disposition(memory, binding, node_id):
    # Never read body, fact, source, graph edges or erasure lists into Python.
    row = memory.db.execute(
        """SELECT CASE WHEN typeof(state)='text' AND length(CAST(state AS BLOB))<=16
            THEN state END AS state,
            (body IS NULL AND fact_key IS NULL AND value_json IS NULL) AS redacted
            FROM memory_nodes WHERE node_id=? AND session_id=? AND thread_id=? AND epoch=?""",
        (node_id, *binding),
    ).fetchone()
    if row is None:
        return "unavailable"
    if row["state"] == "forgotten":
        return "forgotten" if row["redacted"] == 1 else "unavailable"
    if row["state"] in {"active", "invalidated", "tainted"}:
        return "stored_" + row["state"]
    return "unavailable"


def lookup_operation(memory, request: MemoryOperationLookup) -> MemoryOperationResult:
    """Inspect one exact receipt without recreating forgotten/deleted content."""
    try:
        request = MemoryOperationLookup.model_validate(
            request.model_dump() if hasattr(request, "model_dump") else request
        )
    except (ValueError, TypeError):
        raise MemoryError(422, "Invalid memory operation lookup") from None
    original = request.request
    binding = {key: getattr(original, key) for key in BINDING}
    values = tuple(binding[key] for key in BINDING)
    result = {
        "binding": MemoryBinding(**binding),
        "operation": request.operation,
        "idempotency_key": original.idempotency_key,
        "status": "not_found",
        "receipt": None,
        "disposition": None,
        "mutation_fenced": False,
    }
    # Exact canonicalization is shared with MemoryStore._receipt. The original
    # request exists only in the caller's bounded RAM/body, never a new ledger.
    digest = hashlib.sha256(_json(original.model_dump()).encode("utf-8")).hexdigest()
    try:
        with memory.store.lock:
            result["mutation_fenced"] = _mutation_fenced(memory, original, values)
            row = memory.db.execute(
                """SELECT
                    CASE WHEN typeof(operation)='text'
                        AND length(CAST(operation AS BLOB))<=7 THEN operation END AS operation,
                    CASE WHEN typeof(digest)='text'
                        AND length(CAST(digest AS BLOB))=64 THEN digest END AS digest,
                    CASE WHEN typeof(node_id)='text'
                        AND length(CAST(node_id AS BLOB))<=128 THEN node_id END AS node_id,
                    CASE WHEN typeof(revision)='integer' THEN revision END AS revision,
                    typeof(epoch) AS epoch_type
                    FROM memory_receipts
                    WHERE session_id=? AND thread_id=? AND epoch=? AND idem=?""",
                (*values, original.idempotency_key),
            ).fetchone()
            if row is None:
                return MemoryOperationResult(**result)
            try:
                node_id = _IDENTIFIER.validate_python(row["node_id"])
                if (
                    row["operation"] not in {"capture", "forget"}
                    or row["digest"] is None
                    or _DIGEST.fullmatch(row["digest"]) is None
                    or row["epoch_type"] != "integer"
                    or type(row["revision"]) is not int
                    or not 1 <= row["revision"] <= MAX_SAFE
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise MemoryError(503, "Memory operation receipt is unavailable") from None
            if row["digest"] != digest or row["operation"] != request.operation:
                raise MemoryError(409, "Memory operation idempotency conflict")
            if row["revision"] != original.expected_revision + 1 or (
                request.operation == "forget" and node_id != original.node_id
            ):
                raise MemoryError(503, "Memory operation receipt is unavailable")
            identity = {
                **binding,
                "operation": request.operation,
                "idempotency_key": original.idempotency_key,
                "digest": row["digest"],
                "revision": row["revision"],
                "node_id": node_id,
            }
            result.update(
                status="committed",
                receipt={
                    "operation_id": hashlib.sha256(_json(identity).encode("utf-8")).hexdigest(),
                    "revision": row["revision"],
                    "node_id": node_id,
                },
                disposition=_disposition(memory, values, node_id),
            )
            return MemoryOperationResult(**result)
    except sqlite3.Error:
        raise MemoryError(503, "Memory operation receipt is unavailable") from None
