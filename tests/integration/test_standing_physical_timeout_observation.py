"""Contract-first observation control; historical timeout causes remain unknown."""

import asyncio
import builtins
import io
import json
import os
import socket
from contextlib import ExitStack
from unittest.mock import patch

import test_standing_physical_finalization as physical_module
from test_standing_physical_finalization import factory as factory
from test_standing_physical_finalization import physical as physical

from packages.contracts.standing_resource_finalization import StandingResourceFinalizationReceipt

KEYS = {
    "calls",
    "operations",
    "exchanges",
    "captures",
    "workers",
    "streams",
    "gates",
    "locks",
    "state",
    "truncated",
}
PRIVATE = "PUBLIC-SYNTHETIC-OBSERVER-PRIVATE-7ef0b9d3"


def nodes(value):
    yield value
    if type(value) is dict:
        for child in value.values():
            yield from nodes(child)
    elif type(value) is list:
        for child in value:
            yield from nodes(child)


def identities(value):
    return {node for node in nodes(value) if type(node) is int}


def task_evidence(section, task):
    # Nested labels are intentionally unspecified. Find the original task's
    # descriptor structurally, including its cached status and bounded chain.
    descriptors = [
        node
        for node in nodes(section)
        if type(node) is dict
        and any(type(part) is int and part == id(task) for part in node.values())
    ]
    assert descriptors
    assert any(sum(child is False for child in nodes(node)) >= 2 for node in descriptors)
    chains = [
        child
        for node in descriptors
        for child in nodes(node)
        if type(child) is list
        and child
        and all(
            type(frame) in {dict, list}
            and any(type(part) is str and 0 < len(part) <= 64 for part in nodes(frame))
            and any(type(part) is int and part >= 0 for part in nodes(frame))
            for frame in child
        )
    ]
    assert chains and all(len(chain) <= 24 for chain in chains)


def cached(value, gate, public):
    """A test-local fingerprint, never emitted as diagnostic evidence."""
    owner = value.coordinator
    barrier, handoff = owner.standing_barrier, owner.standing_handoff
    scopes = sorted(barrier.operations, key=id)
    exchanges = sorted(value.gateway._submit_exchanges, key=id)
    tasks = {public, *(task for _, _, task in value.original_calls)}
    tasks.update(exchange.task for exchange in exchanges)
    tasks.update(exchange.close_task for exchange in exchanges if exchange.close_task is not None)
    return (
        tuple(
            (name, id(call), id(task), id(call.scope), call.active)
            for name, call, task in value.original_calls
        ),
        tuple(
            (id(scope), tuple(sorted(map(id, scope.handles))), scope.failed, scope.finished)
            for scope in scopes
        ),
        tuple(
            (id(task), task.done(), task.cancelled(), task.cancelling())
            for task in sorted(tasks, key=id)
        ),
        tuple(
            (
                id(exchange),
                id(exchange.response),
                id(exchange.close_task),
                exchange.abandoned,
                exchange.close_started,
            )
            for exchange in exchanges
        ),
        tuple(sorted(map(id, handoff.attempts.values()))),
        tuple(sorted(map(id, handoff.operations))),
        tuple(sorted(map(id, handoff.native))),
        tuple(
            (id(body), body.closes, body.closed, body.failed, body.cancelled)
            for body in value.native.bodies + value.control_transport.bodies
        ),
        tuple(
            (
                id(worker),
                id(worker.process),
                worker.state,
                worker.ready,
                None if worker.process is None else worker.process.returncode,
                id(worker.pending_spawn),
                id(worker.pending_cleanup),
                tuple(map(id, worker.drains)),
            )
            for worker in owner.manager._workers.values()
        ),
        tuple(value.starts),
        tuple(value.stops),
        tuple(value.memories),
        value.effect_checks,
        len(value.native.requests),
        len(value.control_transport.requests),
        value.native.close_calls,
        value.control_transport.close_calls,
        gate.entered.is_set(),
        gate.release.is_set(),
        gate.claimed,
        id(gate.exchange_task),
        id(gate.close_task),
        owner.lock.locked(),
        owner.admission_lock.locked(),
        owner.manager._lock.locked(),
        owner._closing,
        owner._closed,
        barrier.failed,
        barrier.uncertain,
        value.gateway._submit_closed,
        value.gateway._submit_cleanup_failed,
        owner.state.record.model_dump_json(),
    )


async def test_snapshot_observes_original_held_context_without_advancing_owners(physical):
    # Resolve the new capability before the factory opens any native owner.
    snapshot = getattr(physical_module, "_physical_timeout_snapshot", None)
    assert callable(snapshot), "Frozen physical timeout observation helper is unavailable"

    value = await physical()
    await physical_module.bound(value)
    await physical_module.closure(value)
    gate = value.native.gate = physical_module.Gate(physical_module.CONTEXT, "read")
    value.diagnostic_private = gate.diagnostic_private = PRIVATE
    public = physical_module.begin(value)
    await physical_module.gate_entered(gate.entered)
    originals = physical_module.context_originals(value, gate)
    scope, work, exchange, body = originals
    assert not public.done() and not work.done() and not exchange.task.done()
    assert exchange.close_task is None and body.closes == 0
    assert not scope.failed and not scope.finished and work in scope.handles

    # Observe database contents outside the synchronous helper interval. Trace
    # callbacks also detect a read-only query that leaves those bytes unchanged.
    databases = list(dict.fromkeys([value.session.store.db, *value.connections]))
    sql_before = [tuple(database.iterdump()) for database in databases]
    before = cached(value, gate, public)
    tasks_before = {id(task) for task in asyncio.all_tasks()}
    loop = asyncio.get_running_loop()
    ready_before = tuple(map(id, loop._ready))
    scheduled_before = tuple(map(id, loop._scheduled))
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append("forbidden-side-effect")
        raise AssertionError("Synchronous observation attempted a side effect")

    for database in databases:
        database.set_trace_callback(lambda statement: attempted.append("sql"))
    try:
        with ExitStack() as guards:
            for target, name in (
                (builtins, "open"),
                (io, "open"),
                (os, "open"),
                (os, "read"),
                (os, "stat"),
                (os, "lstat"),
                (os, "readlink"),
                (socket, "socket"),
                (asyncio, "create_task"),
                (asyncio, "all_tasks"),
                (loop, "create_task"),
                (value.gateway.http, "send"),
                (value.control.http, "send"),
                (value.coordinator, "status"),
                (value.coordinator.manager, "status"),
                (value.coordinator.manager, "owned_identity"),
                (scope, "hold"),
                (scope, "release"),
                (scope, "fail"),
            ):
                guards.enter_context(patch.object(target, name, forbidden))
            observed = snapshot(value)
    finally:
        for database in databases:
            database.set_trace_callback(None)

    assert attempted == []
    assert cached(value, gate, public) == before
    assert {id(task) for task in asyncio.all_tasks()} == tasks_before
    assert tuple(map(id, loop._ready)) == ready_before
    assert tuple(map(id, loop._scheduled)) == scheduled_before
    assert [tuple(database.iterdump()) for database in databases] == sql_before
    assert not gate.release.is_set() and not public.done() and not work.done()

    assert type(observed) is dict and set(observed) == KEYS
    assert type(observed["truncated"]) is bool
    encoded = json.dumps(observed, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= 32768
    assert json.loads(encoded) == observed
    for node in nodes(observed):
        assert type(node) in {dict, list, str, int, bool, type(None)}
        if type(node) in {dict, list}:
            assert len(node) <= 32
    assert id(value.original_calls[-1][1]) in identities(observed["calls"])
    assert id(scope) in identities(observed["operations"])
    assert {id(handle) for handle in scope.handles} <= identities(observed["operations"])
    task_evidence(observed["calls"], work)
    assert id(exchange) in identities(observed["exchanges"])
    task_evidence(observed["exchanges"], exchange.task)
    assert id(body) in identities(observed["streams"])
    for private in (
        PRIVATE,
        physical_module.SECRET.hex(),
        "Bearer ",
        "a" * 32,
        "r" * 32,
        value.claim.claim_id,
        value.packet.task.task_id,
        value.request.expected_resource_backend,
        value.request.expected_resource_instance,
        str(value.path),
        str(physical_module.ROOT),
    ):
        assert private not in encoded

    # This is a healthy observation control, not a forced timeout or a claim
    # that the historical eight-second CI failures have been explained.
    gate.release.set()
    receipt = await asyncio.wait_for(public, 8)
    assert type(receipt) is StandingResourceFinalizationReceipt
    assert receipt.validate_request(value.request) == receipt
    assert receipt.finalization.resource_disposition == "no-reservation"
    assert receipt.finalization.restoration == "not-required"
    assert receipt.finalization.context.observation.reservation is None
    native = await value.agent.standing_resource_finalization_lookup(value.request)
    assert native is not None and native.finalization == receipt.finalization
    await physical_module.join_context_originals(value, originals)
    assert work.exception() is None
    assert value.starts == value.stops == [] and value.effect_checks == 0
    assert value.provider.calls == []
    # The unchanged physical factory finally joins all original roots, sole
    # closes, native clients, ledger handles and Manager retirement.
