"""Real disposable workers behind genuine Session registration; no inference."""

import asyncio
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from test_resource_handoff import AUTH, MiB, action
from test_resource_processes import READY_MEM, file_probe, make_spec, write_script
from test_standing_resources_registration import (
    CONTEXT,
    MARKER,
    RESERVE,
    ROWS,
    feature,
    http_accepted,
    join_client,
    ordinary,
)
from test_standing_resources_registration import factory as factory

from packages.contracts.client import JournalClient
from packages.contracts.models import ProviderInfo, SessionRequest
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from packages.providers.text import ScriptedProvider
from packages.resources import WorkerManager
from services.converse.api import create_app as converse_app
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime
from services.resources.api import create_app as resource_app
from services.resources.client import ConverseControl
from services.resources.coordinator import ResourceCoordinator
from services.resources.state import ResourceState
from services.session.store import LocalJournal

LOCAL_META = "standing_handoff_meta"
LOCAL_ROWS = "standing_handoff_intents"


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class _RegistrationSnapshot:
    _native: tuple
    _local: tuple
    _proof: object


def _registration_snapshot(native_rows, local_rows, expected_proof, previous=None):
    """Reuse a test proof only while every actual SQL row remains identical."""
    assert len(native_rows) == len(local_rows) == 1 and local_rows[0][1] is not None
    native = tuple(tuple(row) for row in native_rows)
    local = tuple(tuple(row) for row in local_rows)
    if (
        type(previous) is _RegistrationSnapshot
        and native == previous._native
        and local == previous._local
    ):
        assert previous._proof == expected_proof
        return previous
    request = feature().StandingResourceReservation.from_json(native_rows[0][0].encode())
    assert request.context.standing == expected_proof
    assert json.loads(local_rows[0][0]) == request.model_dump(mode="json")
    receipt = feature().StandingResourceReservationReceipt.from_json(local_rows[0][1].encode())
    assert receipt.validate_request(request).disposition == "created"
    return _RegistrationSnapshot(
        copy.deepcopy(native), copy.deepcopy(local), request.context.standing
    )


async def resistant(gate):
    while not gate.is_set():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            continue


class CountedStream(httpx.AsyncByteStream):
    def __init__(self, inner):
        self.inner, self.closes, self.closed = inner, 0, False

    async def __aiter__(self):
        async for chunk in self.inner:
            yield chunk

    async def aclose(self):
        self.closes += 1
        await self.inner.aclose()
        self.closed = True


class SessionGateway(httpx.AsyncBaseTransport):
    """Actual Session ASGI, with negative delivery faults AFTER native work."""

    def __init__(self, session, mode="normal"):
        self.session, self.mode = session, mode
        self.inner = httpx.ASGITransport(app=session.app)
        self.requests, self.streams = [], []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.coordinator = None

    async def handle_async_request(self, request):
        payload = json.loads(request.content)
        self.requests.append((request.url.path, payload))
        if request.url.path == RESERVE:
            coordinator = self.coordinator
            ((key, action_digest, retained, receipt),) = coordinator.db.execute(
                f"SELECT action_key,action_digest,request,receipt FROM {LOCAL_ROWS}"
            ).fetchall()
            assert key and action_digest == payload["action_digest"]
            assert json.loads(retained) == payload and receipt is None
            assert coordinator._whistle_preemptions == 0
            assert coordinator.state.record.phase == "ready"
            assert coordinator.manager.status()["workers"]["whistle"]["ready"]
        if self.mode == "unavailable":
            return httpx.Response(503, json={"detail": MARKER})
        response = await self.inner.handle_async_request(request)
        stream = CountedStream(response.stream)
        response.stream = stream
        self.streams.append(stream)
        if request.url.path == CONTEXT and self.mode == "context-gate":
            self.entered.set()
            await resistant(self.release)
        if request.url.path == RESERVE and response.status_code == 200:
            assert response.status_code == 200
            assert self.session.store.db.execute(f"SELECT count(*) FROM {ROWS}").fetchone()[0] == 1
            if self.mode == "lost":
                await response.aclose()
                raise httpx.ReadError(MARKER, request=request)
            if self.mode == "retained":
                await response.aclose()
                # Both responses come from actual native calls with the same full original.
                response = await self.inner.handle_async_request(request)
                stream = CountedStream(response.stream)
                response.stream = stream
                self.streams.append(stream)
            if self.mode == "held":
                self.entered.set()
                await resistant(self.release)
        return response

    async def aclose(self):
        await self.inner.aclose()


@pytest.fixture
async def handoffs(factory, tmp_path):
    opened = []

    async def make(
        *, session=None, path=None, mode="normal", missing_gateway=False, pending_restart=False
    ):
        feature()
        session = session or await factory()
        if not hasattr(session, "standing_packet"):
            session.standing_packet, session.standing_proof = await http_accepted(session)
        path = path or tmp_path / f"workers-{len(opened)}"
        path.mkdir(mode=0o700, exist_ok=True)
        ready = path / "ready"
        ready.unlink(missing_ok=True)
        script = write_script(path, "worker.py", READY_MEM)
        manager = WorkerManager(
            [make_spec("whistle", [sys.executable, str(script), str(ready), "40"], path)],
            readiness=file_probe(ready),
        )
        state = ResourceState(path / "control/resources-state.json")
        session.store.create_session(SessionRequest(channel="converse"))
        provider = ScriptedProvider([])
        provider.info = ProviderInfo(provider="synthetic-cloud", model="no-inference", local=False)
        runtime = ConverseRuntime(
            LocalJournal(session.store, "converse"),
            provider,
            intent=ConverseIntent(
                state.path.parent / "converse-intent.json",
                initial_enabled=state.record.desired_converse,
            ),
            resources_managed=True,
        )
        control = ConverseControl(
            "http://127.0.0.1:8762",
            "r" * 32,
            transport=httpx.ASGITransport(app=converse_app(runtime, AUTH)),
        )
        transport = SessionGateway(session, mode)
        journal = JournalClient("http://standing.test", "r" * 32, transport=transport)
        startup = []

        async def before_start(worker_id):
            meta = coordinator.db.execute(
                f"SELECT backend_id,instance_id FROM {LOCAL_META}"
            ).fetchone()
            assert meta is not None and meta[1] == coordinator.controller_id
            startup.append(worker_id)

        try:
            coordinator = ResourceCoordinator(
                manager,
                state,
                control,
                journal=LocalJournal(session.store, "resources"),
                handoff_journal=None if missing_gateway else journal,
                memory_probe=lambda: 2 * 1024 * MiB,
                before_start=before_start,
            )
        except BaseException:
            await journal.close()
            await control.close()
            await manager.close()
            state.close()
            raise
        transport.coordinator = coordinator
        env = SimpleNamespace(
            coordinator=coordinator,
            runtime=runtime,
            session=session,
            packet=session.standing_packet,
            proof=session.standing_proof,
            journal=journal,
            transport=transport,
            path=path,
            startup=startup,
            public=[],
            phases=[],
            previews=0,
        )
        opened.append(env)
        await coordinator.initialize()
        if pending_restart:
            assert_pending_without_start(env)
        else:
            assert startup == ["whistle"]
            assert coordinator.status()["workers"]["whistle"]["ready"]
            assert runtime.enabled and coordinator.state.record.phase == "ready"
            assert (await action(coordinator, "profile", profile="local_work_priority"))["ok"]
        original_preview, original_save = coordinator._handoff_may_preempt, coordinator._save
        registration_snapshot = None

        def registered_before_effect():
            nonlocal registration_snapshot
            env.previews += 1
            native = session.store.db.execute(f"SELECT request FROM {ROWS}").fetchall()
            local = coordinator.db.execute(f"SELECT request,receipt FROM {LOCAL_ROWS}").fetchall()
            if env.payload.get("task_id") == env.packet.task.task_id:
                registration_snapshot = _registration_snapshot(
                    native, local, env.proof, previous=registration_snapshot
                )

        async def preview(payload):
            registered_before_effect()
            return await original_preview(payload)

        async def save(**changes):
            env.phases.append(changes.get("phase"))
            if changes.get("phase") in {"quiescing", "unloading", "local_work"}:
                registered_before_effect()
            return await original_save(**changes)

        coordinator._handoff_may_preempt, coordinator._save = preview, save
        env.payload = {
            "operation": "handoff",
            "idempotency_key": "registered-handoff",
            "required_bytes": 128 * MiB,
            "task_id": env.packet.task.task_id,
            "reason": 'Synthetic café "owned" handoff',
        }
        return env

    try:
        yield make
    finally:
        for env in reversed(opened):
            env.transport.release.set()
            for task in env.public:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*env.public, return_exceptions=True)
            if not env.coordinator._closed:
                await env.coordinator.close()
            await env.journal.close()
            await asyncio.wait_for(join_client(env.journal), 4)
            assert all(stream.closed and stream.closes == 1 for stream in env.transport.streams)
            await assert_stopped_worker(env)


async def assert_stopped_worker(env):
    """Join the actual process; a retained diagnostic PID is not live ownership."""
    manager = env.coordinator.manager
    row = manager.status()["workers"]["whistle"]
    managed = manager._workers["whistle"]
    assert row["state"] == "stopped" and not row["ready"] and row["owned_pgid"] is None
    assert managed.pending_spawn is None and managed.pending_cleanup is None
    assert all(
        task.done() and not task.cancelled() and task.exception() is None for task in managed.drains
    )
    if managed.process is None:
        assert env.startup == [] and row["owned_pid"] is None and row["stop_evidence"] is None
    else:
        code = await asyncio.wait_for(managed.process.wait(), 2)
        proof = row["stop_evidence"]
        assert code is not None and managed.process.returncode == code
        assert proof["pid"] == row["owned_pid"] == managed.process.pid
        assert proof["reaped"] and proof["group_empty"] and proof["ownership_verified"]
        assert proof["residual_pids"] == [] and manager.owned_identity("whistle") is None


def assert_pending_without_start(env):
    """Pending registration is preserved unknown; this fixture starts no process."""
    row = env.coordinator.manager.status()["workers"]["whistle"]
    managed = env.coordinator.manager._workers["whistle"]
    assert env.startup == [] and env.coordinator.state.record.phase == "failed"
    assert row["state"] == "stopped" and not row["ready"]
    assert row["owned_pid"] is None and row["owned_pgid"] is None and managed.process is None
    assert managed.pending_spawn is None and managed.pending_cleanup is None
    assert len(env.coordinator.db.execute(f"SELECT request FROM {LOCAL_ROWS}").fetchall()) == 1


def no_handoff_effects(env, pid):
    coordinator = env.coordinator
    row = coordinator.manager.status()["workers"]["whistle"]
    assert row["ready"] and row["owned_pid"] == pid
    assert env.runtime.enabled and coordinator.state.record.phase == "ready"
    assert coordinator.state.record.lease_id is None and coordinator._whistle_preemptions == 0
    assert env.previews == 0 and not {"quiescing", "unloading", "local_work"}.intersection(
        env.phases
    )


async def test_real_worker_release_uses_exact_preallocated_native_registered_lease(handoffs):
    env = await handoffs()
    coordinator = env.coordinator
    before = coordinator.manager.status()["workers"]["whistle"]["owned_pid"]
    result = await coordinator.action(env.payload)
    assert result["ok"] and result["policy"]["phase"] == "local_work"
    request = feature().StandingResourceReservation.from_json(
        env.session.store.db.execute(f"SELECT request FROM {ROWS}").fetchone()[0].encode()
    )
    backend, instance = coordinator.db.execute(
        f"SELECT backend_id,instance_id FROM {LOCAL_META}"
    ).fetchone()
    assert (
        request.resource_backend == backend
        and request.resource_instance == instance == coordinator.controller_id
    )
    assert request.lease_id == result["policy"]["lease_id"]
    assert result["policy"]["lease_task_id"] == env.packet.task.task_id
    expected = hashlib.sha256(
        json.dumps(env.payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert request.action_digest == expected and env.previews > 0
    row = coordinator.manager.status()["workers"]["whistle"]
    assert (
        row["state"] == "stopped"
        and row["stop_evidence"]["reaped"]
        and row["stop_evidence"]["group_empty"]
    )
    assert before > 0 and not env.runtime.enabled
    await assert_stopped_worker(env)
    sends = len(env.transport.requests)
    assert await coordinator.action(copy.deepcopy(env.payload)) == result
    assert len(env.transport.requests) == sends


async def test_closed_admission_refuses_before_preview_policy_or_worker_effect(handoffs):
    env = await handoffs()
    env.session.app.state.standing_task_owner.close_admission(
        env.packet, capability=bytes(range(32))
    )
    pid = env.coordinator.manager.status()["workers"]["whistle"]["owned_pid"]
    with pytest.raises(StandingConflict):
        await env.coordinator.action(env.payload)
    no_handoff_effects(env, pid)
    assert env.session.store.db.execute(f"SELECT count(*) FROM {ROWS}").fetchone()[0] == 0


@pytest.mark.parametrize("mode", ("lost", "retained"))
async def test_real_saved_but_lost_or_retained_ack_never_permits_effects_or_replay(handoffs, mode):
    env = await handoffs(mode=mode)
    pid = env.coordinator.manager.status()["workers"]["whistle"]["owned_pid"]
    with pytest.raises(StandingUnavailable):
        await env.coordinator.action(env.payload)
    no_handoff_effects(env, pid)
    before = env.coordinator.db.execute(f"SELECT request,receipt FROM {LOCAL_ROWS}").fetchone()
    assert before is not None
    if mode == "lost":
        assert before[1] is None
    else:
        request = feature().StandingResourceReservation.from_json(before[0].encode())
        receipt = feature().StandingResourceReservationReceipt.from_json(before[1].encode())
        assert receipt.validate_request(request).disposition == "retained"
        assert receipt.canonical_bytes().decode() == before[1]
        assert (
            env.session.store.db.execute(f"SELECT request FROM {ROWS}").fetchone()[0] == before[0]
        )
    assert env.session.store.db.execute(f"SELECT count(*) FROM {ROWS}").fetchone()[0] == 1
    sends = len(env.transport.requests)
    with pytest.raises(StandingUnavailable):
        await env.coordinator.action(copy.deepcopy(env.payload))
    assert len(env.transport.requests) == sends
    assert (
        env.coordinator.db.execute(f"SELECT request,receipt FROM {LOCAL_ROWS}").fetchone() == before
    )
    no_handoff_effects(env, pid)


@pytest.mark.parametrize("refusal", ("cancel", "deadline"))
async def test_actual_late_session_ack_retains_transport_until_cleanup_without_effects(
    handoffs, refusal
):
    env = await handoffs(mode="held")
    pid = env.coordinator.manager.status()["workers"]["whistle"]["owned_pid"]
    task = asyncio.create_task(env.coordinator.action(env.payload))
    env.public.append(task)
    await asyncio.wait_for(env.transport.entered.wait(), 4)
    if refusal == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 2)
    else:
        # Real25s capture/transport deadline; no assumption about a new private clock.
        with pytest.raises(StandingUnavailable):
            await asyncio.wait_for(asyncio.shield(task), 28)
    assert len(env.journal._submit_exchanges) == 1
    no_handoff_effects(env, pid)
    env.transport.release.set()
    await asyncio.wait_for(join_client(env.journal), 3)
    assert all(stream.closed and stream.closes == 1 for stream in env.transport.streams)
    assert env.coordinator.db.execute(f"SELECT receipt FROM {LOCAL_ROWS}").fetchone()[0] is None
    no_handoff_effects(env, pid)


async def test_coordinator_close_suppresses_late_ack_but_joins_real_worker_and_transport(handoffs):
    env = await handoffs(mode="held")
    task = asyncio.create_task(env.coordinator.action(env.payload))
    env.public.append(task)
    await asyncio.wait_for(env.transport.entered.wait(), 4)
    closing = asyncio.create_task(env.coordinator.close())
    env.public.append(closing)
    async with asyncio.timeout(2):
        while not env.coordinator._closing:
            await asyncio.sleep(0)
    assert len(env.journal._submit_exchanges) == 1
    env.transport.release.set()
    with pytest.raises(StandingUnavailable):
        await asyncio.wait_for(asyncio.shield(task), 4)
    await asyncio.wait_for(asyncio.shield(closing), 4)
    await asyncio.wait_for(join_client(env.journal), 3)
    assert not {"quiescing", "unloading", "local_work"}.intersection(env.phases)
    await assert_stopped_worker(env)


@pytest.mark.parametrize("failure", ("missing", "unavailable"))
async def test_missing_or_unavailable_gateway_cannot_be_called_ordinary_and_off_still_works(
    handoffs, failure
):
    env = await handoffs(
        missing_gateway=failure == "missing",
        mode="unavailable" if failure == "unavailable" else "normal",
    )
    pid = env.coordinator.manager.status()["workers"]["whistle"]["owned_pid"]
    with pytest.raises(StandingUnavailable):
        await env.coordinator.action(env.payload)
    no_handoff_effects(env, pid)
    off = await action(env.coordinator, "converse", key="explicit-off", enabled=False)
    assert off["ok"] and off["policy"]["phase"] == "off"
    assert not env.runtime.enabled


@pytest.mark.parametrize("bound", (False, True), ids=("manual-unbound", "native-ordinary"))
async def test_unbound_and_genuine_native_ordinary_handoff_preserve_existing_flow(handoffs, bound):
    env = await handoffs()
    payload = {
        "operation": "handoff",
        "idempotency_key": "legacy-native-control",
        "required_bytes": 128 * MiB,
    }
    if bound:
        payload["task_id"] = ordinary(env.session.store).task_id
    env.payload = payload
    result = await env.coordinator.action(payload)
    assert result["ok"] and result["policy"]["phase"] == "local_work"
    assert (
        not env.runtime.enabled
        and env.coordinator.manager.status()["workers"]["whistle"]["state"] == "stopped"
    )
    assert env.session.store.db.execute(f"SELECT count(*) FROM {ROWS}").fetchone()[0] == 0
    assert env.coordinator.db.execute(f"SELECT count(*) FROM {LOCAL_ROWS}").fetchone()[0] == 0
    assert [path for path, _ in env.transport.requests] == ([CONTEXT] if bound else [])


async def test_new_action_original_is_frozen_before_context_await_and_same_key_conflicts(handoffs):
    env = await handoffs(mode="context-gate")
    original = copy.deepcopy(env.payload)
    task = asyncio.create_task(env.coordinator.action(env.payload))
    env.public.append(task)
    await asyncio.wait_for(env.transport.entered.wait(), 3)
    env.payload["reason"] = "Caller mutated after actual context HTTP entered"
    with pytest.raises(StandingConflict):
        await env.coordinator.action(copy.deepcopy(env.payload))
    env.transport.release.set()
    result = await asyncio.wait_for(task, 4)
    assert result["ok"] and result["policy"]["required_bytes"] == original["required_bytes"]
    request = json.loads(env.session.store.db.execute(f"SELECT request FROM {ROWS}").fetchone()[0])
    assert (
        request["action_digest"]
        == hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert [path for path, _ in env.transport.requests].count(CONTEXT) == 1


async def test_restart_keeps_original_pending_unknown_and_never_relaunches(handoffs):
    first = await handoffs(mode="lost")
    with pytest.raises(StandingUnavailable):
        await first.coordinator.action(first.payload)
    original = first.coordinator.db.execute(f"SELECT request FROM {LOCAL_ROWS}").fetchone()[0]
    await first.coordinator.close()
    second = await handoffs(session=first.session, path=first.path, pending_restart=True)
    assert second.coordinator.controller_id != first.coordinator.controller_id
    assert (
        second.coordinator.db.execute(f"SELECT request FROM {LOCAL_ROWS}").fetchone()[0] == original
    )
    with pytest.raises(StandingUnavailable):
        await second.coordinator.action(copy.deepcopy(first.payload))
    assert second.transport.requests == []
    assert_pending_without_start(second)
    assert second.previews == 0
    assert not {"quiescing", "unloading", "local_work"}.intersection(second.phases)
    assert second.coordinator.db.execute(f"SELECT receipt FROM {LOCAL_ROWS}").fetchone()[0] is None


@pytest.mark.parametrize("kind,status", ((StandingUnavailable, 503), (StandingConflict, 409)))
async def test_resource_owner_http_maps_only_typed_capture_refusals(
    handoffs, monkeypatch, kind, status
):
    env = await handoffs()

    async def refused(_):
        raise kind()

    monkeypatch.setattr(env.coordinator, "action", refused)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=resource_app(env.coordinator, AUTH)),
        base_url="http://resources.test",
    ) as client:
        response = await client.post(
            "/v1/resources/actions",
            json=env.payload,
            headers={"Authorization": "Bearer " + "o" * 32},
        )
    assert response.status_code == status and response.headers.get("cache-control") == "no-store"
    assert MARKER not in response.text and len(response.content) < 256


async def test_actual_closed_coordinator_refuses_new_task_action_before_sql_or_gateway(handoffs):
    env = await handoffs()
    await env.coordinator.close()
    payload = {**env.payload, "idempotency_key": "new-action-after-actual-close"}
    before = tuple(env.session.store.db.iterdump())
    sends = len(env.transport.requests)
    with pytest.raises(StandingUnavailable):
        await env.coordinator.action(payload)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=resource_app(env.coordinator, AUTH)),
        base_url="http://resources.test",
    ) as client:
        response = await client.post(
            "/v1/resources/actions",
            json=payload,
            headers={"Authorization": "Bearer " + "o" * 32},
        )
    assert response.status_code == 503 and response.headers.get("cache-control") == "no-store"
    assert MARKER not in response.text and len(response.content) < 256
    assert len(env.transport.requests) == sends
    assert tuple(env.session.store.db.iterdump()) == before
