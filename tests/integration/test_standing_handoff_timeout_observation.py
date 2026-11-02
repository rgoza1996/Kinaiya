"""Source-aware qualification of a passive original-owner diagnostic.

Frozen from the CI430 observation contract before the helper was consulted.
This does not reproduce the historical four-second timeout or establish its
cause. It preserves the genuine held Session response and positive handoff.
"""

import asyncio
import json

import test_standing_resources_handoff as handoff_module
from test_standing_resources_handoff import handoffs as handoffs
from test_standing_resources_registration import factory as factory


async def test_snapshot_observes_held_original_without_advancing_real_handoff(handoffs):
    # Resolve before any fixture creates a process/client. The cold baseline is
    # one diagnostic capability absence, never a worker/cleanup regression.
    snapshot = getattr(handoff_module, "_handoff_timeout_snapshot", None)
    assert callable(snapshot), "Frozen original-handoff observation helper is absent"

    env = await handoffs(mode="context-gate")
    coordinator, gateway = env.coordinator, env.transport
    task = asyncio.create_task(coordinator.action(env.payload))
    env.public.append(task)
    await asyncio.wait_for(gateway.entered.wait(), 3)
    capture = coordinator.standing_handoff.attempts[env.payload["idempotency_key"]]
    operation, scope = capture.task, capture.ownership
    managed = coordinator.manager._workers["whistle"]
    process = managed.process
    (exchange,) = env.journal._submit_exchanges
    before = {
        "record": coordinator.state.record.model_dump(mode="json"),
        "phases": tuple(env.phases),
        "previews": env.previews,
        "worker": (managed.state, managed.ready, managed.pgid, process.returncode),
        "operations": frozenset(coordinator.standing_handoff.operations),
        "handles": frozenset(scope.handles),
        "native": frozenset(coordinator.standing_handoff.native),
        "exchanges": frozenset(env.journal._submit_exchanges),
        "streams": tuple((id(stream), stream.closes, stream.closed) for stream in gateway.streams),
        "requests": tuple(path for path, _ in gateway.requests),
        "native_rows": env.session.store.db.execute(
            f"SELECT count(*) FROM {handoff_module.ROWS}"
        ).fetchone()[0],
        "local_rows": coordinator.db.execute(
            f"SELECT count(*) FROM {handoff_module.LOCAL_ROWS}"
        ).fetchone()[0],
    }
    assert not task.done() and not operation.done() and operation in scope.handles
    assert not capture.effects_started and not capture.started.done()
    assert not gateway.release.is_set() and process.returncode is None
    assert before["native_rows"] == before["local_rows"] == 0
    assert not before["phases"] and before["previews"] == 0
    assert exchange.actual_owner is scope and exchange in scope.handles
    assert exchange.response is None and exchange.close_task is None

    try:
        observed = snapshot(env, capture)
        assert type(observed) is dict and set(observed) == {
            "capture_id",
            "operation_id",
            "scope_id",
            "effects_started",
            "operation",
            "handles",
            "phases",
            "streams",
            "state_phase",
            "truncated",
        }
        assert observed["capture_id"] == id(capture)
        assert observed["operation_id"] == id(operation) and observed["scope_id"] == id(scope)
        assert observed["effects_started"] is False and observed["truncated"] is False
        original = observed["operation"]
        assert (
            type(original) is dict
            and {"id", "kind", "done", "cancelled", "await_chain"} <= original.keys()
        )
        assert original["id"] == id(operation)
        assert type(original["kind"]) is str and original["kind"]
        assert original["done"] is operation.done() is False
        assert original["cancelled"] is operation.cancelled() is False
        assert type(original["await_chain"]) is list and 1 <= len(original["await_chain"]) <= 24
        handles = observed["handles"]
        assert type(handles) is list and len(handles) == len(before["handles"]) <= 32
        assert all(type(item) is dict and {"id", "kind"} <= item.keys() for item in handles)
        assert {item["id"] for item in handles} == {id(item) for item in before["handles"]}
        assert all(type(item["kind"]) is str and item["kind"] for item in handles)
        assert id(operation) in {item["id"] for item in handles}
        assert observed["phases"] == list(before["phases"])[-32:]
        assert observed["state_phase"] == before["record"]["phase"]
        streams = observed["streams"]
        assert type(streams) is list and len(streams) == len(before["streams"]) <= 32
        assert [(item["closes"], item["closed"]) for item in streams] == [
            (closes, closed) for _, closes, closed in before["streams"]
        ]
        encoded = json.dumps(observed, ensure_ascii=False, allow_nan=False).encode("utf-8")
        assert len(encoded) <= 32768 and env.payload["reason"] not in encoded.decode("utf-8")

        # All comparisons are synchronous while the original real gate remains
        # held. Observation may not move owners, SQL, worker or physical phase.
        assert coordinator.standing_handoff.attempts[env.payload["idempotency_key"]] is capture
        assert (
            capture.task is operation and capture.ownership is scope and managed.process is process
        )
        assert not task.done() and not operation.done() and not capture.effects_started
        assert not capture.started.done() and not scope.finished and not scope.failed
        assert not gateway.release.is_set() and capture.active
        assert coordinator.state.record.model_dump(mode="json") == before["record"]
        assert (tuple(env.phases), env.previews) == (before["phases"], before["previews"])
        assert (managed.state, managed.ready, managed.pgid, process.returncode) == before["worker"]
        assert frozenset(coordinator.standing_handoff.operations) == before["operations"]
        assert frozenset(scope.handles) == before["handles"]
        assert frozenset(coordinator.standing_handoff.native) == before["native"]
        assert frozenset(env.journal._submit_exchanges) == before["exchanges"]
        assert exchange.response is None and exchange.close_task is None
        assert (
            tuple((id(stream), stream.closes, stream.closed) for stream in gateway.streams)
            == before["streams"]
        )
        assert tuple(path for path, _ in gateway.requests) == before["requests"]
        assert (
            env.session.store.db.execute(f"SELECT count(*) FROM {handoff_module.ROWS}").fetchone()[
                0
            ]
            == before["native_rows"]
        )
        assert (
            coordinator.db.execute(f"SELECT count(*) FROM {handoff_module.LOCAL_ROWS}").fetchone()[
                0
            ]
            == before["local_rows"]
        )
    finally:
        gateway.release.set()

    result = await asyncio.wait_for(task, 4)
    assert result["ok"] and result["policy"]["phase"] == "local_work"
    assert result["policy"]["required_bytes"] == env.payload["required_bytes"]
    assert operation.done() and not operation.cancelled() and operation.exception() is None
    assert all(stream.closed and stream.closes == 1 for stream in gateway.streams)
