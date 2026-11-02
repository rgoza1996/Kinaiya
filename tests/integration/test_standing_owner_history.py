"""Codex independent regressions for strict native standing revision history."""

from datetime import UTC, datetime

import pytest

from packages.contracts.models import SessionRequest
from packages.contracts.standing_work import StandingChange, StandingUnavailable
from services.session.agent_control import AgentControl
from services.session.standing_work import StandingOwner
from services.session.store import Store

RESPONSIBILITY = "22800000-0000-4000-8000-000000000001"
HISTORY_TABLES = ("standing_owner_changes", "standing_authorizations")
OWNER_TABLES = (
    "standing_owner_meta",
    "standing_owner_changes",
    "standing_authorizations",
    "standing_owner_current",
)


def _rows(store):
    return {
        table: [tuple(row) for row in store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in OWNER_TABLES
    }


def _history(store):
    return {
        table: [
            tuple(row)
            for row in store.db.execute(
                f"SELECT revision,typeof(revision) FROM {table} ORDER BY revision"
            )
        ]
        for table in HISTORY_TABLES
    }


def _request(state, session, revision):
    return StandingChange.model_validate(
        {
            "schema_version": "1.0",
            "idempotency_key": f"independent-history-{revision + 1}",
            "expected_backend": state.backend_id,
            "expected_instance": state.instance_id,
            "expected_revision": revision,
            "responsibility_id": RESPONSIBILITY,
            "authorization": {
                "label": "Synthetic history proof",
                "objective": "Write a synthetic bounded status file.",
                "content_scope": "local_only",
                "enabled": True,
                "task_session": {
                    "session_id": session.session_id,
                    "thread_id": session.thread_id,
                    "epoch": session.epoch,
                },
                "workspace_pin": {
                    "schema_version": "1.0",
                    "workspace_id": "a" * 64,
                    "root_identity": "b" * 64,
                    "settings_revision": 0,
                    "source": "host",
                    "shell_backend": "bubblewrap",
                    "mode": "direct",
                },
                "schedule": {
                    "kind": "daily_local",
                    "timezone": "UTC",
                    "local_time": "12:00",
                    "fold_policy": "earlier",
                    "gap_policy": "skip",
                    "grace_seconds": 60,
                    "missed_run_policy": "latest_within_grace",
                    "start_local_date": "2026-11-01",
                    "valid_until_utc": "2026-11-05T00:00:00Z",
                },
                "delivery": {
                    "kind": "in_app",
                    "attention": "routine",
                    "preview": "status_only",
                    "quiet_hours": None,
                },
                "overlap_policy": "one_unresolved_per_authorization",
                "maximum_runs": 4,
                "inference_authorization": "configured_agent_profile",
            },
        }
    )


@pytest.fixture
def native_history(tmp_path):
    store = Store(tmp_path / "native.sqlite3")
    control = AgentControl(store, new_store=store.agent_control.new_store)
    owner = None
    try:
        control.register()
        owner = StandingOwner(
            store, control, clock=lambda: datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
        )
        owner.register()
        session = store.create_session(SessionRequest(channel="agent"))
        state = control.snapshot()
        requests = [_request(state, session, revision) for revision in range(4)]
        receipts = [owner.change(request) for request in requests[:3]]
        assert [receipt.revision for receipt in receipts] == [1, 2, 3]
        assert _history(store) == {
            table: [(1, "integer"), (2, "integer"), (3, "integer")] for table in HISTORY_TABLES
        }
        snapshot = owner.snapshot(RESPONSIBILITY)
        assert snapshot.revision == 3 and snapshot.record.activation == "armed"
        assert owner.lookup(requests[2]).status == "recorded"
        assert not store.db.in_transaction
        yield store, owner, requests
    finally:
        if owner is not None:
            owner.close()
        control.close()
        store.close()


def test_valid_integral_history_preserves_reads_and_new_revision_cas(native_history):
    store, owner, requests = native_history
    before = _rows(store)
    assert owner.snapshot(RESPONSIBILITY).revision == 3
    assert owner.lookup(requests[2]).status == "recorded"
    assert _rows(store) == before
    assert owner.change(requests[3]).revision == 4
    assert owner.snapshot(RESPONSIBILITY).revision == 4
    assert _history(store) == {
        table: [(1, "integer"), (2, "integer"), (3, "integer"), (4, "integer")]
        for table in HISTORY_TABLES
    }
    assert not store.db.in_transaction


@pytest.mark.parametrize(
    "corrupt_tables",
    [HISTORY_TABLES, ("standing_owner_changes",), ("standing_authorizations",)],
    ids=["paired-fractional-history", "changes-only", "authorizations-only"],
)
@pytest.mark.parametrize("operation", ["snapshot", "retained_lookup", "new_revision_cas"])
def test_fractional_history_refuses_proof_and_new_writes(native_history, corrupt_tables, operation):
    store, owner, requests = native_history
    with store.db:
        for table in corrupt_tables:
            changed = store.db.execute(
                f"UPDATE {table} SET revision=? WHERE responsibility_id=? AND revision=2",
                (1.5, RESPONSIBILITY),
            )
            assert changed.rowcount == 1
    assert _history(store) == {
        table: [(1, "integer"), (1.5, "real"), (3, "integer")]
        if table in corrupt_tables
        else [(1, "integer"), (2, "integer"), (3, "integer")]
        for table in HISTORY_TABLES
    }
    # Keep the current revision/body intact: this is historical SQL counter
    # corruption, not an invalid request or an unrelated broken fixture.
    assert (
        store.db.execute(
            "SELECT revision FROM standing_owner_current WHERE responsibility_id=?",
            (RESPONSIBILITY,),
        ).fetchone()[0]
        == 3
    )
    before = _rows(store)
    refused = None
    try:
        if operation == "snapshot":
            owner.snapshot(RESPONSIBILITY)
        elif operation == "retained_lookup":
            owner.lookup(requests[2])
        else:
            owner.change(requests[3])
    except StandingUnavailable as error:
        refused = error
    assert _rows(store) == before, "Corrupt native history must not permit a new durable write"
    assert not store.db.in_transaction
    assert isinstance(refused, StandingUnavailable), (
        "Corrupt history must be unavailable, never proof"
    )
