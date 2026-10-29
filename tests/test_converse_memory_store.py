"""Synthetic owner/source/DAG tests against the real SQLite journal and memory core."""

import concurrent.futures
import json
from uuid import uuid4

import pytest

from packages.contracts.memory import (
    MemoryBinding,
    MemoryCapture,
    MemoryConfigure,
    MemoryForget,
    MemoryInspect,
    MemoryQuery,
)
from packages.contracts.models import MAX_PLAYBACK_GENERATION, AppendRequest, SessionRequest
from services.session.memory import MemoryError, MemoryStore
from services.session.store import Store


@pytest.fixture
def host(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest())
    binding = dict(session_id=session.session_id, thread_id=session.thread_id, epoch=session.epoch)
    memory = MemoryStore(store)
    yield store, memory, binding
    store.close()


def source(store, binding, text="Synthetic source", *, kind="conversation.user", role="owner"):
    return store.append(
        AppendRequest(
            **binding,
            kind=kind,
            payload={"text": text, "final": True},
            idempotency_key=str(uuid4()),
        ),
        role,
    )


def correction(store, binding, event, revision, text="Corrected synthetic source"):
    return store.append(
        AppendRequest(
            **binding,
            kind="conversation.revision",
            payload={"target_event_id": event.event_id, "revision": revision, "text": text},
            idempotency_key=str(uuid4()),
        ),
        "owner",
    )


def state(memory, binding):
    return memory.state(MemoryBinding(**binding))


def configure(memory, binding, enabled=True, expected=None):
    return memory.configure(
        MemoryConfigure(
            **binding,
            expected_revision=state(memory, binding).revision if expected is None else expected,
            enabled=enabled,
        )
    )


def capture(
    memory,
    binding,
    event,
    *,
    kind="assertion",
    key="fact",
    value=True,
    parents=(),
    body="Synthetic curated body",
    idem=None,
    expected=None,
    revision=1,
):
    request = MemoryCapture(
        **binding,
        expected_revision=state(memory, binding).revision if expected is None else expected,
        idempotency_key=idem or str(uuid4()),
        kind=kind,
        source_event_id=event.event_id,
        source_revision=revision,
        body=body,
        fact=None if kind == "inference" else {"key": key, "value": value},
        prerequisite_ids=list(parents),
    )
    return memory.capture(request)


def inspect(memory, binding, node):
    return memory.inspect(MemoryInspect(**binding, node_id=node.node_id))


def query(memory, binding, *, seeds=(), keys=()):
    return memory.query(MemoryQuery(**binding, seed_ids=list(seeds), fact_keys=list(keys)))


def forget(memory, binding, node, *, idem=None, expected=None):
    return memory.forget(
        MemoryForget(
            **binding,
            node_id=node.node_id,
            expected_revision=state(memory, binding).revision if expected is None else expected,
            idempotency_key=idem or str(uuid4()),
        )
    )


def refused(status, call, *args, **kwargs):
    with pytest.raises(MemoryError) as error:
        call(*args, **kwargs)
    assert error.value.status == status
    return str(error.value)


def test_default_no_rows_optional_off_and_reopen(host):
    store, memory, binding = host
    assert state(memory, binding).model_dump() == dict(
        schema_version="1.0", **binding, enabled=False, revision=0, node_count=0, status="ready"
    )
    assert store.db.execute("SELECT count(*) FROM memory_namespaces").fetchone()[0] == 0
    event = source(store, binding)
    refused(409, capture, memory, binding, event)
    refused(409, query, memory, binding, keys=["fact"])
    assert configure(memory, binding).revision == 1
    result = capture(memory, binding, event, body="Retained public test body")
    assert result.node.content_scope == "local_only" and result.node.provenance == "owner_curated"
    assert result.node.source_sequence == event.sequence
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        other = MemoryStore(reopened)
        assert inspect(other, binding, result.node).node == result.node
        assert query(other, binding, seeds=[result.node.node_id]).nodes == [result.node]
        assert reopened.db.execute("SELECT version FROM schema_meta").fetchone()[0] == 1
    finally:
        reopened.close()


def test_transitive_invalidation_crosses_observation_and_never_revives(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding), key="x", value="old").node
    b = capture(
        memory, binding, source(store, binding), kind="observation", key="y", parents=[a.node_id]
    ).node
    c = capture(memory, binding, source(store, binding), kind="inference", parents=[b.node_id]).node
    report = capture(
        memory, binding, source(store, binding), kind="observation", key="x", value="new"
    ).node
    assert [inspect(memory, binding, n).node.state for n in (a, b, c)] == [
        "invalidated",
        "tainted",
        "invalidated",
    ]
    refused(409, query, memory, binding, seeds=[c.node_id])
    assert query(memory, binding, keys=["x"]).nodes == [report]
    capture(memory, binding, source(store, binding), kind="observation", key="x", value="old")
    assert inspect(memory, binding, a).node.state == "invalidated"
    assert inspect(memory, binding, b).node.body == b.body


def test_new_observation_is_in_its_own_dependent_closure(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding), value="a").node
    b = capture(
        memory, binding, source(store, binding), kind="observation", value="b", parents=[a.node_id]
    ).node
    assert b.state == "tainted"
    assert inspect(memory, binding, a).node.state == "invalidated"
    assert query(memory, binding, keys=["fact"]).nodes == []


@pytest.mark.parametrize(("left", "right"), [(True, 1), (1, "1"), (False, None), (None, "null")])
def test_fact_comparison_retains_json_types(host, left, right):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding), value=left).node
    b = capture(memory, binding, source(store, binding), kind="observation", value=right).node
    assert inspect(memory, binding, a).node.state == "invalidated"
    actual = query(memory, binding, keys=["fact"]).nodes[0].fact.value
    assert actual == right and type(actual) is type(right)
    assert b.state == "active"


def test_observation_order_uses_sources_and_corrected_head_does_not_retreat(host):
    store, memory, binding = host
    configure(memory, binding)
    old = source(store, binding)
    new = source(store, binding)
    report = capture(memory, binding, new, kind="observation", value="new").node
    refused(409, capture, memory, binding, old, kind="observation", value="old")
    refused(409, capture, memory, binding, old, value="old")
    correction(store, binding, new, 2)
    assert query(memory, binding, keys=["fact"]).nodes == []
    assert inspect(memory, binding, report).node.state == "tainted"
    refused(409, capture, memory, binding, old, kind="observation", value="new")
    refused(409, capture, memory, binding, old, value="old")
    assert capture(memory, binding, source(store, binding), value="old").node.state == "active"


def test_disabled_inspect_does_not_reconcile_enable_commits_before_cas(host, monkeypatch):
    store, memory, binding = host
    configure(memory, binding)
    event = source(store, binding)
    node = capture(memory, binding, event).node
    off = configure(memory, binding, False)
    correction(store, binding, event, 2)
    original = memory._source

    def no_read(*_):
        pytest.fail("disabled inspection processed source pins")

    monkeypatch.setattr(memory, "_source", no_read)
    assert inspect(memory, binding, node).node.state == "active"
    assert state(memory, binding) == off
    monkeypatch.setattr(memory, "_source", original)
    refused(409, configure, memory, binding, True, off.revision)
    changed = state(memory, binding)
    assert not changed.enabled and changed.revision == off.revision + 1
    assert inspect(memory, binding, node).node.state == "invalidated"
    assert configure(memory, binding, True, changed.revision).enabled


@pytest.mark.parametrize("bad_revision", [True, 2.0, 3])
def test_malformed_noncontiguous_correction_invalidates_full_closure(host, bad_revision):
    store, memory, binding = host
    configure(memory, binding)
    event = source(store, binding)
    a = capture(memory, binding, event).node
    b = capture(memory, binding, source(store, binding), kind="inference", parents=[a.node_id]).node
    change = correction(store, binding, event, 2)
    payload = change.model_dump(mode="json")
    payload["payload"]["revision"] = bad_revision
    with store.lock, store.db:
        store.db.execute(
            "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(payload), change.event_id)
        )
    refused(409, query, memory, binding, seeds=[b.node_id])
    assert inspect(memory, binding, a).node.state == "invalidated"
    assert inspect(memory, binding, b).node.state == "invalidated"
    refused(409, capture, memory, binding, event, revision=1)


def test_exact32_revision_chain_and_index_then_overflow(host):
    store, memory, binding = host
    configure(memory, binding)
    event = source(store, binding)
    latest = None
    for revision in range(2, 34):
        latest = correction(store, binding, event, revision)
    result = capture(memory, binding, event, revision=33)
    assert result.node.source_sequence == latest.sequence
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM entries WHERE kind='conversation.revision' AND json_extract(value,'$.payload.target_event_id')=? ORDER BY sequence LIMIT 33",
        (event.event_id,),
    ).fetchall()
    assert any(
        "SEARCH entries USING INDEX entries_memory_revision_target" in row[3] for row in plan
    )
    correction(store, binding, event, 34)
    refused(409, query, memory, binding, seeds=[result.node.node_id])
    refused(409, capture, memory, binding, event, revision=34)
    assert inspect(memory, binding, result.node).node.state == "invalidated"


def test_sources_foreign_assistant_draft_and_forged_envelope_refuse(host):
    store, memory, binding = host
    configure(memory, binding)
    assistant = source(store, binding, kind="conversation.converse", role="converse")
    refused(409, capture, memory, binding, assistant)
    other = store.create_session(SessionRequest())
    foreign = source(store, dict(session_id=other.session_id, thread_id=other.thread_id, epoch=1))
    refused(409, capture, memory, binding, foreign)
    event = source(store, binding)
    changed = event.model_dump(mode="json")
    changed["payload"]["final"] = False
    with store.lock, store.db:
        store.db.execute(
            "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(changed), event.event_id)
        )
    refused(409, capture, memory, binding, event)
    assert state(memory, binding).node_count == 0


def test_replay_cas_and_noop_forget_receipt_bound(host):
    store, memory, binding = host
    configure(memory, binding)
    event = source(store, binding)
    request = MemoryCapture(
        **binding,
        expected_revision=1,
        idempotency_key="capture",
        kind="assertion",
        source_event_id=event.event_id,
        source_revision=1,
        body="Retained",
        fact={"key": "x", "value": 1},
    )
    first = memory.capture(request)
    assert memory.capture(request) == first
    refused(409, memory.capture, request.model_copy(update={"body": "Different"}))
    forgotten = forget(memory, binding, first.node, idem="forget")
    assert forgotten.forgotten_ids == [first.node.node_id]
    rows = store.db.execute("SELECT count(*) FROM memory_receipts").fetchone()[0]
    rev = state(memory, binding).revision
    assert forget(memory, binding, first.node, idem="noop").forgotten_ids == []
    assert state(memory, binding).revision == rev
    assert store.db.execute("SELECT count(*) FROM memory_receipts").fetchone()[0] == rows
    refused(409, memory.capture, request)
    refused(409, forget, memory, binding, first.node, idem="capture")


def test_forget_redacts_complete_closure_blocks_sources_and_global_floor(host):
    store, memory, binding = host
    configure(memory, binding)
    early = source(store, binding)
    a = capture(
        memory,
        binding,
        source(store, binding),
        key="erasable-key",
        value="erasable-value",
        body="erasable-body",
    ).node
    b = capture(
        memory,
        binding,
        source(store, binding),
        kind="observation",
        key="other-key",
        parents=[a.node_id],
        body="dependent-body",
    ).node
    result = forget(memory, binding, a)
    assert result.forgotten_ids == [a.node_id, b.node_id]
    for node in (a, b):
        erased = inspect(memory, binding, node).node
        assert erased.state == "forgotten" and erased.body is None and erased.fact is None
    serialized = "\n".join(str(tuple(r)) for r in store.db.execute("SELECT * FROM memory_nodes"))
    assert all(
        secret not in serialized
        for secret in (
            "erasable-key",
            "erasable-value",
            "erasable-body",
            "dependent-body",
            "other-key",
        )
    )
    receipts = "\n".join(str(tuple(r)) for r in store.db.execute("SELECT * FROM memory_receipts"))
    assert "erasable-body" not in receipts and "erasable-key" not in receipts
    original = store.entry(a.source_event_id)
    correction(store, binding, original, 2)
    refused(409, capture, memory, binding, original, revision=2)
    refused(409, capture, memory, binding, early, key="unrelated")
    assert capture(memory, binding, source(store, binding), key="unrelated").node.state == "active"
    assert store.entry(a.source_event_id).payload["text"] == "Synthetic source"


def test_graph_corruption_quarantines_persistently_disable_and_forget_remain(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    b = capture(memory, binding, source(store, binding), kind="inference", parents=[a.node_id]).node
    with store.lock, store.db:
        store.db.execute("INSERT INTO memory_edges VALUES(?,?)", (b.node_id, a.node_id))
    old = state(memory, binding).revision
    refused(409, query, memory, binding, seeds=[b.node_id])
    assert state(memory, binding).status == "quarantined"
    assert state(memory, binding).revision == old + 1
    assert MemoryStore(store).state(MemoryBinding(**binding)).status == "quarantined"
    assert not configure(memory, binding, False).enabled
    result = forget(memory, binding, a)
    assert result.forgotten_ids == [a.node_id, b.node_id]
    assert result.state.status == "quarantined"
    refused(409, configure, memory, binding, True)
    assert (
        store.db.execute("SELECT count(*) FROM memory_nodes WHERE body IS NOT NULL").fetchone()[0]
        == 0
    )


def test_depth_and_complete_response_byte_bound(host):
    store, memory, binding = host
    configure(memory, binding)
    event = source(store, binding)
    node = capture(memory, binding, event).node
    for _ in range(15):
        node = capture(memory, binding, event, kind="inference", parents=[node.node_id]).node
    refused(409, capture, memory, binding, event, kind="inference", parents=[node.node_id])
    assert len(query(memory, binding, seeds=[node.node_id]).nodes) == 16
    large = []
    for _ in range(8):
        large.append(capture(memory, binding, event, body="b" * 2048).node)
    refused(409, query, memory, binding, seeds=[n.node_id for n in large])
    assert inspect(memory, binding, large[-1]).node.body == "b" * 2048


def test_node_capacity_does_not_deny_content_erasure(host):
    store, memory, binding = host
    configure(memory, binding)
    event = source(store, binding)
    nodes = [capture(memory, binding, event).node for _ in range(256)]
    refused(409, capture, memory, binding, event)
    assert state(memory, binding).node_count == 256
    assert forget(memory, binding, nodes[0]).forgotten_ids == [nodes[0].node_id]
    assert state(memory, binding).node_count == 256
    refused(409, capture, memory, binding, source(store, binding))


def test_concurrent_cas_exactly_one_admission(host):
    store, memory, binding = host
    configure(memory, binding)
    event = source(store, binding)

    def run(i):
        try:
            return capture(memory, binding, event, expected=1, idem=str(i)).node.node_id
        except MemoryError as error:
            return error.status

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results.count(409) == 1 and len([r for r in results if isinstance(r, str)]) == 1
    assert state(memory, binding).node_count == 1


def test_sql_failure_rolls_back_node_refutations_revision_and_receipt(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding), value="old").node
    new = source(store, binding)
    before = state(memory, binding)
    with store.lock, store.db:
        store.db.execute(
            "CREATE TRIGGER reject_memory_receipt BEFORE INSERT ON memory_receipts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    message = refused(
        503, capture, memory, binding, new, kind="observation", value="new", idem="retry"
    )
    assert "synthetic failure" not in message
    assert state(memory, binding) == before
    assert inspect(memory, binding, a).node.state == "active"
    assert store.db.execute("SELECT count(*) FROM memory_refutations").fetchone()[0] == 0
    with store.lock, store.db:
        store.db.execute("DROP TRIGGER reject_memory_receipt")
    assert (
        capture(memory, binding, new, kind="observation", value="new", idem="retry").node.state
        == "active"
    )


def test_final_revision_reserved_disable_and_reset_delete(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    with store.lock, store.db:
        store.db.execute("UPDATE memory_namespaces SET revision=?", (MAX_PLAYBACK_GENERATION - 1,))
    refused(409, capture, memory, binding, source(store, binding))
    refused(409, forget, memory, binding, a)
    assert configure(memory, binding, False).revision == MAX_PLAYBACK_GENERATION
    reset = store.reset(binding["session_id"])
    refused(409, state, memory, binding)
    current = dict(session_id=reset.session_id, thread_id=reset.thread_id, epoch=reset.epoch)
    assert state(memory, current).revision == 0 and not state(memory, current).enabled
    store.delete(reset.session_id)
    refused(404, state, memory, current)
    for table in (
        "memory_namespaces",
        "memory_nodes",
        "memory_edges",
        "memory_refutations",
        "memory_receipts",
    ):
        assert store.db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_oversized_corrupt_graph_erases_all_then_fixed_refusal(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    with store.lock, store.db:
        for i in range(2, 258):
            store.db.execute(
                """INSERT INTO memory_nodes SELECT ?,session_id,thread_id,epoch,?,kind,
                source_event_id,source_revision,source_sequence,body,fact_key,value_json,state
                FROM memory_nodes WHERE node_id=?""",
                (str(uuid4()), i, a.node_id),
            )
    refused(409, query, memory, binding, seeds=[a.node_id])
    assert state(memory, binding).node_count == 257
    message = refused(409, forget, memory, binding, a)
    assert message == "Namespace content erased; graph remains quarantined"
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_nodes WHERE body IS NOT NULL OR fact_key IS NOT NULL OR value_json IS NOT NULL OR state!='forgotten'"
        ).fetchone()[0]
        == 0
    )
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_receipts WHERE operation='forget'"
        ).fetchone()[0]
        == 0
    )
    rev = state(memory, binding).revision
    assert forget(memory, binding, a).forgotten_ids == []
    assert state(memory, binding).revision == rev


def test_corrupt_source_sequence_still_erases_whole_quarantined_scope(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    with store.lock, store.db:
        store.db.execute(
            "UPDATE memory_nodes SET source_sequence='corrupt' WHERE node_id=?", (a.node_id,)
        )
    refused(409, query, memory, binding, seeds=[a.node_id])
    result = forget(memory, binding, a)
    assert result.forgotten_ids == [a.node_id]
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_nodes WHERE body IS NOT NULL OR fact_key IS NOT NULL OR value_json IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    assert (
        store.db.execute("SELECT capture_floor FROM memory_namespaces").fetchone()[0]
        == MAX_PLAYBACK_GENERATION
    )


def test_corrupt_unrepresentable_ids_erase_before_fixed_refusal(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    with store.lock, store.db:
        store.db.execute(
            """INSERT INTO memory_nodes SELECT 'bad!identifier',session_id,thread_id,epoch,2,
        kind,source_event_id,source_revision,source_sequence,body,fact_key,value_json,state
        FROM memory_nodes WHERE node_id=?""",
            (a.node_id,),
        )
    refused(409, query, memory, binding, seeds=[a.node_id])
    assert (
        refused(409, forget, memory, binding, a)
        == "Namespace content erased; graph remains quarantined"
    )
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_nodes WHERE body IS NOT NULL OR fact_key IS NOT NULL OR value_json IS NOT NULL OR state!='forgotten'"
        ).fetchone()[0]
        == 0
    )
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_receipts WHERE operation='forget'"
        ).fetchone()[0]
        == 0
    )


def test_oversized_source_refused_without_materializing_payload(host):
    import tracemalloc

    store, memory, binding = host
    configure(memory, binding)
    event = source(store, binding)
    value = event.model_dump(mode="json")
    value["payload"]["text"] = "x" * (3 * 1024 * 1024)
    with store.lock, store.db:
        store.db.execute(
            "UPDATE entries SET value=? WHERE event_id=?", (json.dumps(value), event.event_id)
        )
    tracemalloc.start()
    try:
        refused(409, capture, memory, binding, event)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("column", ["body", "value_json"])
def test_oversized_node_payload_refuses_before_python_materialization(host, enabled, column):
    import tracemalloc

    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    if not enabled:
        configure(memory, binding, False)
    with store.lock, store.db:
        store.db.execute(
            "UPDATE memory_nodes SET " + column + "=? WHERE node_id=?",
            ("b" * (3 * 1024 * 1024), a.node_id),
        )
    tracemalloc.start()
    try:
        if enabled:
            refused(409, query, memory, binding, seeds=[a.node_id])
        else:
            refused(503, inspect, memory, binding, a)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024
    assert state(memory, binding).status == ("quarantined" if enabled else "ready")


def test_corrupt_capture_floor_does_not_deny_erasure(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    with store.lock, store.db:
        store.db.execute("UPDATE memory_namespaces SET capture_floor='corrupt'")
    refused(409, query, memory, binding, seeds=[a.node_id])
    assert forget(memory, binding, a).forgotten_ids == [a.node_id]
    assert (
        store.db.execute("SELECT capture_floor FROM memory_namespaces").fetchone()[0]
        == MAX_PLAYBACK_GENERATION
    )
    assert (
        store.db.execute("SELECT body FROM memory_nodes WHERE node_id=?", (a.node_id,)).fetchone()[
            0
        ]
        is None
    )


@pytest.mark.parametrize("corrupt_graph", [False, True])
def test_corrupt_receipt_cannot_rollback_erasure_in_either_path(host, corrupt_graph):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    with store.lock, store.db:
        store.db.execute(
            "INSERT INTO memory_receipts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                *[binding[k] for k in ("session_id", "thread_id", "epoch")],
                "impossible-old-forget",
                "forget",
                "0" * 64,
                a.node_id,
                2,
                json.dumps([a.node_id]),
            ),
        )
        if corrupt_graph:
            store.db.execute("UPDATE memory_namespaces SET status='quarantined'")
            store.db.execute("UPDATE memory_nodes SET source_sequence='corrupt'")
    outcome = None
    try:
        forget(memory, binding, a, idem="fresh-owner-forget")
    except MemoryError as error:
        outcome = (error.status, str(error))
    row = store.db.execute(
        "SELECT body,fact_key,value_json,state FROM memory_nodes WHERE node_id=?", (a.node_id,)
    ).fetchone()
    assert tuple(row) == (None, None, None, "forgotten")
    assert outcome == (409, "Namespace content erased; graph remains quarantined")
    assert state(memory, binding).status == "quarantined"
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_receipts WHERE idem='fresh-owner-forget'"
        ).fetchone()[0]
        == 0
    )
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_receipts WHERE idem='impossible-old-forget'"
        ).fetchone()[0]
        == 1
    )


def test_unrelated_forget_receipt_write_failure_rolls_back(host):
    store, memory, binding = host
    configure(memory, binding)
    a = capture(memory, binding, source(store, binding)).node
    before = state(memory, binding)
    with store.lock, store.db:
        store.db.execute(
            "CREATE TRIGGER fail_forget_receipt BEFORE INSERT ON memory_receipts WHEN NEW.operation='forget' BEGIN SELECT RAISE(ABORT,'synthetic erasure storage failure'); END"
        )
    refused(503, forget, memory, binding, a)
    assert state(memory, binding) == before
    assert inspect(memory, binding, a).node.body == a.body
    assert (
        store.db.execute(
            "SELECT count(*) FROM memory_receipts WHERE operation='forget'"
        ).fetchone()[0]
        == 0
    )
