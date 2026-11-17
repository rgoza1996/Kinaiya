"""Decisions may export Agent context only for its exact current native task."""

import sqlite3
from contextlib import contextmanager

from packages.contracts.agent_advice import AgentAdviceExportAdmission, AgentAdviceExportRequest
from packages.contracts.interfaces import AgentControlConflict, AgentControlUnavailable
from services.session.store import JournalError


class AgentAdviceConflict(ValueError):
    pass


class AgentAdviceUnavailable(RuntimeError):
    pass


@contextmanager
def _snapshot(store):
    with store.lock:
        db = store.db
        if db.in_transaction:
            raise AgentAdviceUnavailable("Original native journal transaction remains active")
        # The Python lock owns this connection. BEGIN additionally gives every
        # observation one WAL snapshot when a different Store writes concurrently.
        # This read owns no DML and must never end a caller's earlier transaction.
        db.execute("BEGIN")
        try:
            yield
        finally:
            db.rollback()


def admit_export(store, request):
    request = AgentAdviceExportRequest.model_validate(request)
    try:
        with _snapshot(store):
            # Task state and complete source/causal-closure admission describe
            # one native point, including across independently opened Stores.
            initial = store.agent_control._initial(request)
            revision, task_scope, state, sources = store.agent_control._history(
                initial, require_stable_steer=True
            )
            if (
                revision != request.objective_revision
                or state != "running"
                or sources != set(request.source_revisions)
            ):
                raise AgentAdviceConflict("Agent advice task or source authority changed")
            admitted = store.agent_export_admission(request.export_request(task_scope))
            return AgentAdviceExportAdmission(
                request=request,
                binding_sha256=request.binding_digest(),
                task_content_scope=task_scope,
                export=admitted,
            ).validate_request(request)
    except AgentControlConflict:
        raise AgentAdviceConflict("Agent advice task authority changed") from None
    except (AgentAdviceConflict, PermissionError):
        raise
    except (AgentControlUnavailable, JournalError, KeyError, ValueError, sqlite3.Error):
        raise AgentAdviceUnavailable(
            "Native Agent advice export authority is unavailable"
        ) from None
