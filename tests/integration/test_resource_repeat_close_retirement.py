"""Review-derived shutdown retirement controls, frozen before repeat-close repair.

Actual Coordinator, protected SQLite, WorkerManager and HTTPX clients; only
explicit lifecycle/transport close faults are synthetic. No worker is started.
"""

import asyncio
import sqlite3
import sys
from types import SimpleNamespace

import httpx
import pytest
from test_standing_execution_api import factory as factory

from packages.contracts.client import JournalClient
from packages.contracts.standing_work import StandingUnavailable
from packages.resources import WorkerManager, WorkerSpec
from services.resources.client import ConverseControl
from services.resources.coordinator import ResourceCoordinator, WhistleAdmissionError
from services.resources.state import ResourceState

MESSAGE = "Synthetic current retirement failure"


class CloseGate:
    def __init__(self):
        self.mode = None
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls, self.owner, self.positive = 0, None, False

    async def run(self, invoke):
        self.calls += 1
        self.owner = asyncio.current_task()
        self.entered.set()
        if self.mode == "hold":
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    pass
        if self.mode == "fail":
            raise RuntimeError(MESSAGE)
        if self.mode == "cancel":
            raise asyncio.CancelledError(MESSAGE)
        await invoke()
        self.positive = True


class Manager(WorkerManager):
    def __init__(self, path):
        super().__init__(
            [WorkerSpec("whistle", (sys.executable, "-c", "raise SystemExit(97)"), path)]
        )
        self.retirement = CloseGate()

    async def close(self):
        await self.retirement.run(super().close)


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, app=None):
        self.delegate = None if app is None else httpx.ASGITransport(app=app)
        self.retirement = CloseGate()

    async def handle_async_request(self, request):
        if self.delegate is not None:
            return await self.delegate.handle_async_request(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"provider": {"local": False}})
        assert request.url.path == "/v1/converse/enabled"
        import json

        return httpx.Response(
            200, json={"resource_enabled": json.loads(request.content)["enabled"]}
        )

    async def aclose(self):
        async def retire():
            if self.delegate is not None:
                await self.delegate.aclose()

        await self.retirement.run(retire)


@pytest.fixture
async def hosts(factory, tmp_path, record_property):
    opened = []

    async def make():
        session = await factory()
        path = tmp_path / f"retirement-{len(opened)}"
        path.mkdir(mode=0o700)
        state = ResourceState(path / "control/resources.json")
        state.save(desired_converse=False)
        manager = Manager(path)
        transports = [Transport(), Transport(session.app), Transport(session.app)]
        clients = [
            ConverseControl("http://127.0.0.1:8762", "r" * 32, transport=transports[0]),
            JournalClient("http://127.0.0.1:8760", "r" * 32, transport=transports[1]),
            JournalClient("http://127.0.0.1:8760", "r" * 32, transport=transports[2]),
        ]
        coordinator = ResourceCoordinator(
            manager,
            state,
            clients[0],
            journal=clients[1],
            handoff_journal=clients[2],
            memory_probe=lambda: 2 * 1024**3,
        )
        value = SimpleNamespace(
            coordinator=coordinator,
            manager=manager,
            transports=transports,
            clients=clients,
            state=state,
            session=session,
        )
        opened.append(value)
        await coordinator.initialize()
        value.ledger, value.gate_ledger = coordinator.db, coordinator.standing_handoff.db
        assert coordinator.standing_barrier.ready
        assert all(worker.process is None for worker in manager._workers.values())
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            for gate in [value.manager.retirement, *[t.retirement for t in value.transports]]:
                gate.release.set()
            owner = value.coordinator._close_owner
            if owner is not None:
                await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 5)
                assert owner.done()
            # Independent fixture cleanup follows failed-current-owner assertions;
            # it never changes that original owner's outcome into positive proof.
            if not value.manager.retirement.positive:
                await WorkerManager.close(value.manager)
            for transport in value.transports:
                if not transport.retirement.positive:
                    transport.retirement.mode = None
                    await transport.aclose()
            for client in value.clients:
                await client.http.aclose()
                assert client.http.is_closed and not getattr(client, "_submit_exchanges", ())
            for db in (getattr(value, "ledger", None), getattr(value, "gate_ledger", None)):
                if db is not None:
                    db.close()
            value.state.close()
            assert not value.coordinator.standing_handoff.native
            assert not value.coordinator.standing_handoff.operations
            assert not value.coordinator.standing_barrier.operations
            for worker in value.manager._workers.values():
                assert worker.process is None and worker.pending_spawn is None
                assert worker.pending_cleanup is None and worker.leader_fd is None
            record_property(
                "actual_retirement_cleanup", "original owner joined; no workers started"
            )


def assert_ledgers_retired(value):
    for db in (value.ledger, value.gate_ledger):
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
    assert value.state._lock is None and value.state._dir is None


def assert_positive_retirement(value, owner):
    assert value.coordinator._close_owner is owner and owner.done() and not owner.cancelled()
    assert value.manager.retirement.positive and value.manager.retirement.calls == 1
    assert value.manager.retirement.owner is owner
    assert all(t.retirement.positive and t.retirement.calls == 1 for t in value.transports)
    assert all(t.retirement.owner is owner for t in value.transports)
    assert all(c.http.is_closed for c in value.clients)
    assert_ledgers_retired(value)


async def test_failed_drain_with_positive_current_retirement_has_idempotent_later_close(hosts):
    value = await hosts()
    coordinator = value.coordinator

    async def drain():
        raise RuntimeError(MESSAGE)

    coordinator.register_whistle_lifecycle(abort=lambda: None, drain=drain)
    # Historical capability uncertainty is retained independently of positive
    # current transport/manager retirement; no capability is reopened here.
    coordinator.standing_barrier.uncertain = True
    value.clients[2]._submit_cleanup_failed = True
    with pytest.raises(RuntimeError, match=MESSAGE):
        await coordinator.close()
    owner = coordinator._close_owner
    assert_positive_retirement(value, owner)
    failed_state = value.state.record.model_dump()
    assert failed_state["phase"] == "failed"
    assert await coordinator.close() is None
    assert coordinator._close_owner is owner and value.state.record.model_dump() == failed_state
    assert coordinator._closing and coordinator.standing_barrier.closed
    assert coordinator.standing_barrier.uncertain and value.clients[2]._submit_cleanup_failed
    assert_positive_retirement(value, owner)


@pytest.mark.parametrize("phase", ["manager", "client"])
async def test_pending_actual_retirement_keeps_same_owner_until_positive_completion(
    hosts, phase, record_property
):
    value = await hosts()
    coordinator = value.coordinator
    gate = value.manager.retirement if phase == "manager" else value.transports[0].retirement
    gate.mode = "hold"
    facade = asyncio.create_task(coordinator.close())
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        owner = coordinator._close_owner
        assert gate.owner is owner and not owner.done() and not gate.positive
        if phase == "client":
            # HTTPX closes its public flag before actual transport.aclose.
            # Coordinator flag timing is deliberately not an acceptance rule.
            record_property("coordinator_closed_at_transport_entry", coordinator._closed)
            assert value.clients[0].http.is_closed
        else:
            assert value.ledger.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(StandingUnavailable):
            await facade
        with pytest.raises(StandingUnavailable):
            await coordinator.close()
        assert coordinator._close_owner is owner and not owner.done() and gate.calls == 1
        with pytest.raises(WhistleAdmissionError):
            await coordinator.admit_whistle((0, 0))
        gate.release.set()
        await asyncio.wait_for(asyncio.shield(owner), 5)
        assert await coordinator.close() is None
        assert_positive_retirement(value, owner)
    finally:
        gate.release.set()
        await asyncio.wait_for(asyncio.gather(facade, return_exceptions=True), 5)


@pytest.mark.parametrize(
    "phase,mode", [("manager", "fail"), ("client", "fail"), ("client", "cancel")]
)
async def test_current_failed_or_cancelled_retirement_never_acknowledges_from_closed_flag(
    hosts, phase, mode, record_property
):
    value = await hosts()
    coordinator = value.coordinator
    gate = value.manager.retirement if phase == "manager" else value.transports[0].retirement
    gate.mode = mode
    original_error = asyncio.CancelledError if mode == "cancel" else RuntimeError
    with pytest.raises(original_error, match=MESSAGE):
        await coordinator.close()
    owner = coordinator._close_owner
    assert owner.done() and coordinator._closing
    record_property("coordinator_closed_after_failed_retirement", coordinator._closed)
    assert gate.owner is owner and gate.calls == 1 and not gate.positive
    assert owner.cancelled() is (mode == "cancel")
    if phase == "client":
        assert value.clients[0].http.is_closed
    with pytest.raises((original_error, StandingUnavailable)):
        await coordinator.close()
    assert coordinator._close_owner is owner and gate.calls == 1 and not gate.positive
    assert coordinator.standing_barrier.closed
    with pytest.raises(WhistleAdmissionError):
        await coordinator.admit_whistle((0, 0))
