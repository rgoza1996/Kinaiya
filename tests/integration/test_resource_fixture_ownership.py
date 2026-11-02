"""Actual stream-close controls for the reviewed CI fixture-only repair.

No process or socket is opened. Real brokers enroll the real StreamClosures
registry into the coordinator's original shutdown operation.
"""

import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.operation_ownership import StreamClosures
from packages.contracts.standing_work import StandingUnavailable
from packages.resources.processes import WorkerManager, WorkerSpec
from services.resources.client import ConverseControl
from services.resources.coordinator import ResourceCoordinator
from services.resources.qwen3 import Qwen3Broker, Qwen3Endpoint
from services.resources.state import ResourceState
from services.resources.whistle import WhistleBroker, WhistleEndpoint


async def joined(task):
    return await asyncio.wait_for(asyncio.shield(task), 5)


async def reached(event):
    await asyncio.wait_for(event.wait(), 5)


async def enrolled(registry, original):
    async with asyncio.timeout(5):
        while not any(original in owner.handles for owner in registry.owners):
            await asyncio.sleep(0.005)
    return next(owner for owner in registry.owners if original in owner.handles)


class OriginalWriter:
    def __init__(self, *, fail):
        self.fail, self.close_calls, self.wait_calls = fail, 0, 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.original = None

    def close(self):
        self.close_calls += 1

    async def wait_closed(self):
        self.wait_calls += 1
        self.original = asyncio.current_task()
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise RuntimeError("Synthetic original stream close failed")


class NoNetwork(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests, self.close_calls, self.close_owner = [], 0, None

    async def handle_async_request(self, request):
        self.requests.append(request)
        raise AssertionError("Construction-only fixture must not open a control request")

    async def aclose(self):
        self.close_calls += 1
        self.close_owner = asyncio.current_task()


def assert_never_started(manager, worker_id):
    managed = manager._workers[worker_id]
    assert managed.process is None and managed.identity is None and managed.identity_process is None
    assert managed.pgid is None and managed.leader_fd is None and managed.member_fds == {}
    assert managed.pending_spawn is None and managed.pending_cleanup is None
    assert managed.pending_fds == [] and managed.pending_obj is None and managed.drains == []
    assert not managed.granted and managed.grant_w is None
    row = manager.status()["workers"][worker_id]
    assert row["state"] == "stopped" and row["owned_pid"] is None and row["owned_pgid"] is None


@pytest.fixture
async def hosts(tmp_path, record_property):
    opened = []

    def make(family, *, fail):
        path = tmp_path / str(len(opened))
        path.mkdir()
        ipc = path / "ipc"
        ipc.mkdir(mode=0o700)
        worker_id = "qwen3" if family == "text" else "whistle"
        manager = WorkerManager(
            [
                WorkerSpec(
                    worker_id, (sys.executable, "-c", "raise AssertionError('no launch')"), path
                )
            ]
        )
        transport = NoNetwork()
        control = ConverseControl("http://127.0.0.1:8762", "r" * 32, transport=transport)
        state = ResourceState(path / "control/resources-state.json")
        coordinator = ResourceCoordinator(manager, state, control)
        if family == "text":
            endpoint = Qwen3Endpoint(ipc)
            broker = Qwen3Broker(coordinator, endpoint)
            coordinator.register_text_lifecycle(
                abort=broker.abort_active,
                drain=broker.drain,
                release=endpoint.release_after_stop,
            )
            coordinator.register_standing_composition(qwen3=broker)
        else:
            endpoint = WhistleEndpoint(ipc)
            broker = WhistleBroker(coordinator, endpoint)
            coordinator.register_whistle_lifecycle(abort=broker.abort_active, drain=broker.drain)
            coordinator.register_standing_composition(whistle=broker)
        value = SimpleNamespace(
            family=family,
            manager=manager,
            worker_id=worker_id,
            control=control,
            transport=transport,
            coordinator=coordinator,
            endpoint=endpoint,
            broker=broker,
            writer=OriginalWriter(fail=fail),
            facade=None,
            public=None,
            shutdown=None,
        )
        opened.append(value)
        assert_never_started(manager, worker_id)
        assert type(endpoint.stream_closes) is StreamClosures
        assert broker.operation is None
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            value.writer.release.set()
            if value.facade is not None:
                await joined(asyncio.gather(value.facade, return_exceptions=True))
            if value.public is not None:
                await joined(asyncio.gather(value.public, return_exceptions=True))
            if value.writer.original is not None:
                await joined(asyncio.gather(value.writer.original, return_exceptions=True))
            if value.coordinator._close_owner is None:
                await joined(asyncio.gather(value.coordinator.close(), return_exceptions=True))
            shutdown = value.coordinator._close_owner
            assert shutdown is not None
            await joined(asyncio.gather(shutdown, return_exceptions=True))
            await value.broker.close()
            assert_never_started(value.manager, value.worker_id)
            assert value.endpoint.stream_closes.pending == set()
            assert value.endpoint.stream_closes.owners == set()
            assert value.transport.requests == [] and value.transport.close_calls == 1
            assert value.control.http.is_closed and value.transport.close_owner.done()
            value.endpoint.close()
            record_property(
                "actual_fixture_cleanup",
                json.dumps(
                    {
                        "family": value.family,
                        "stream_wait_done": value.writer.original.done(),
                        "shutdown_owner_done": shutdown.done(),
                        "writer_close_calls": value.writer.close_calls,
                        "writer_wait_calls": value.writer.wait_calls,
                        "control_transport_close_calls": value.transport.close_calls,
                        "no_process_or_request": True,
                        "stream_failed": value.endpoint.stream_closes.failed,
                    }
                ),
            )


@pytest.mark.parametrize("family", ["text", "whistle"])
@pytest.mark.parametrize("fail", [False, True], ids=["positive-release", "failed-original-close"])
async def test_real_broker_shutdown_retains_original_stream_close_and_its_outcome(
    hosts, family, fail
):
    value = hosts(family, fail=fail)
    registry, writer, coordinator = value.endpoint.stream_closes, value.writer, value.coordinator
    value.facade = asyncio.create_task(registry.close(writer, timeout=0.01))
    await reached(writer.entered)
    original = writer.original
    assert isinstance(original, asyncio.Task) and registry.pending == {original}
    assert await joined(value.facade) is False
    assert not original.done() and writer.close_calls == writer.wait_calls == 1
    public_close = value.public = asyncio.create_task(coordinator.close())
    owner = await enrolled(registry, original)
    shutdown = value.shutdown = coordinator._close_owner
    assert shutdown is not None and not shutdown.done() and original in owner.handles
    with pytest.raises(StandingUnavailable):
        await joined(public_close)
    assert not original.done() and not shutdown.done()
    assert coordinator.db.execute("SELECT 1").fetchone() == (1,)
    assert coordinator.state._lock is not None and not value.control.http.is_closed
    assert value.transport.close_calls == 0
    assert_never_started(value.manager, value.worker_id)
    writer.release.set()
    results = await joined(asyncio.gather(original, shutdown, return_exceptions=True))
    assert coordinator._close_owner is shutdown and shutdown.done()
    assert registry.pending == set() and registry.owners == set()
    assert writer.close_calls == writer.wait_calls == 1
    if fail:
        assert isinstance(results[0], RuntimeError) and isinstance(results[1], StandingUnavailable)
        assert registry.failed and owner.failed and coordinator.state.record.phase == "failed"
        with pytest.raises(StandingUnavailable):
            await coordinator.close()
    else:
        assert results == [None, None] and not registry.failed and not owner.failed
        await coordinator.close()
    assert coordinator._close_owner is shutdown and writer.close_calls == writer.wait_calls == 1
    assert value.control.http.is_closed and value.transport.close_calls == 1
