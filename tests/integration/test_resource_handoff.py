"""Real process release, deterministic admission inputs and real API authority.

These fixtures use a synthetic memory worker and scripted inference, not voice
models. Live voice/physical memory evidence is collected separately.
"""

import asyncio
import io
import json
import sys
import tempfile
from types import SimpleNamespace

import httpx
import pytest

from packages.contracts.models import ProviderInfo, SessionRequest
from packages.providers.text import ProviderError, ScriptedProvider
from packages.providers.voice_worker import create_app as voice_app
from packages.resources import WorkerManager, WorkerSpec
from services.common.auth import Auth
from services.converse.api import create_app as converse_app
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime
from services.resources.api import ResourceAction, create_app
from services.resources.client import ConverseControl, ResourceClient, local_base
from services.resources.coordinator import ResourceCoordinator
from services.resources.providers import ResourceBoundProvider
from services.resources.state import ResourceState
from services.session.store import LocalJournal, Store

AUTH = Auth({"owner": "o" * 32, "resources": "r" * 32, "converse": "c" * 32, "agent": "a" * 32})
MiB = 1024 * 1024


async def make_coordinated(tmp_path):
    ready = tmp_path / "ready"
    script = tmp_path / "worker.py"
    script.write_text(
        "import os,time\nfrom pathlib import Path\nb=bytearray(40*1024*1024)\nfor n in range(0,len(b),4096):b[n]=1\nPath('ready').write_text(str(os.getpid()))\nwhile True:time.sleep(.1)\n"
    )

    async def probe(spec, process):
        return ready.exists() and ready.read_text() == str(process.pid)

    manager = WorkerManager(
        [WorkerSpec("voice", (sys.executable, str(script)), tmp_path)], readiness=probe
    )
    state = ResourceState(tmp_path / "control/resources-state.json")
    store = Store(tmp_path / "journal.sqlite")
    store.create_session(SessionRequest(channel="converse"))
    provider = ScriptedProvider([])
    provider.info = ProviderInfo(provider="synthetic-cloud", model="fixture", local=False)
    runtime = ConverseRuntime(
        LocalJournal(store, "converse"),
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
    coordinator = ResourceCoordinator(
        manager,
        state,
        control,
        journal=LocalJournal(store, "resources"),
        memory_probe=lambda: 2 * 1024 * MiB,
    )
    await coordinator.initialize()
    return coordinator, runtime, store


@pytest.fixture
async def coordinated(tmp_path):
    coordinator, runtime, store = await make_coordinated(tmp_path)
    yield coordinator, runtime, store
    await coordinator.close()
    store.close()


async def action(coordinator, operation, key=None, **fields):
    payload = ResourceAction(operation=operation, idempotency_key=key or operation, **fields)
    return await coordinator.action(payload.model_dump(exclude_unset=True))


async def test_explicit_handoff_stops_real_worker_and_restores_new_pid(coordinated):
    coordinator, runtime, store = coordinated
    before = coordinator.status()["workers"]["voice"]
    assert before["ready"] and before["rss_bytes"] > 32 * MiB
    refused = await action(coordinator, "handoff", required_bytes=128 * MiB)
    assert not refused["ok"] and runtime.enabled
    assert coordinator.state.record.phase == "ready"
    await action(coordinator, "profile", profile="local_work_priority")
    handed = await action(coordinator, "handoff", key="accepted-handoff", required_bytes=128 * MiB)
    assert handed["ok"] and handed["policy"]["phase"] == "local_work"
    assert not runtime.enabled
    stopped = coordinator.status()["workers"]["voice"]
    assert stopped["state"] == "stopped" and not stopped["ready"]
    assert stopped["stop_evidence"]["reaped"] and stopped["stop_evidence"]["group_empty"]
    assert stopped["stop_evidence"]["rss_before_bytes"] > 32 * MiB
    assert (
        await action(coordinator, "handoff", key="accepted-handoff", required_bytes=128 * MiB)
        == handed
    )
    restored = await action(coordinator, "restore")
    assert restored["ok"] and runtime.enabled
    assert coordinator.status()["workers"]["voice"]["owned_pid"] != before["owned_pid"]
    assert any(
        event.origin == "resources" and event.kind == "resource.changed" for event in store.read()
    )


async def test_user_off_choice_is_not_overridden_by_restore(coordinated):
    coordinator, runtime, _ = coordinated
    off = await action(coordinator, "converse", enabled=False)
    assert off["ok"] and not runtime.enabled
    restored = await action(coordinator, "restore")
    assert restored["ok"] and restored["policy"]["phase"] == "off"
    assert coordinator.status()["workers"]["voice"]["state"] == "stopped"


async def legacy_choice(runtime, enabled):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=converse_app(runtime, AUTH)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + "o" * 32},
    ) as client:
        response = await client.post("/v1/converse/enabled", json={"enabled": enabled})
        assert response.status_code == 200, response.text
        return response.json()


@pytest.mark.parametrize("operation", ["restore", "task_finished", "converse"])
async def test_acknowledged_legacy_off_wins_during_worker_restoration(coordinated, operation):
    coordinator, runtime, _ = coordinated
    await action(coordinator, "profile", profile="local_work_priority")
    assert (await action(coordinator, "handoff", required_bytes=128 * MiB, task_id="target"))["ok"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.manager.start

    async def delayed(worker_id):
        entered.set()
        await release.wait()
        return await original(worker_id)

    coordinator.manager.start = delayed
    fields = {"task_id": "target"} if operation == "task_finished" else {}
    if operation == "converse":
        fields["enabled"] = True
    restoration = asyncio.create_task(action(coordinator, operation, key="race-restore", **fields))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        off = await asyncio.wait_for(legacy_choice(runtime, False), 1)
        assert off["enabled"] is False and off["owner_enabled"] is False
        assert not restoration.done(), "owner acknowledgment must not wait for the worker lock"
        assert runtime.enabled is False
        # Status observes owner intent even before a worker transition can finish.
        policy = coordinator.status()["policy"]
        assert policy["desired_converse"] is False
        assert policy["owner_intent_revision"] == off["owner_intent_revision"]
        release.set()
        result = await asyncio.wait_for(restoration, 4)
        assert result["ok"] and result["policy"]["phase"] == "off"
        assert runtime.enabled is False
        stopped = coordinator.status()["workers"]["voice"]
        assert stopped["state"] == "stopped"
        assert stopped["stop_evidence"]["reaped"] and stopped["stop_evidence"]["group_empty"]
        assert (await action(coordinator, "restore", key="later-automatic-restore"))["policy"][
            "phase"
        ] == "off"
        assert runtime.enabled is False
    finally:
        release.set()
        await asyncio.gather(restoration, return_exceptions=True)


async def test_owner_on_during_handoff_is_not_lost_by_a_policy_snapshot(coordinated):
    coordinator, runtime, _ = coordinated
    await action(coordinator, "profile", profile="local_work_priority")
    entered, release = asyncio.Event(), asyncio.Event()
    original = coordinator.manager.stop

    async def delayed(worker_id):
        entered.set()
        await release.wait()
        return await original(worker_id)

    coordinator.manager.stop = delayed
    handoff = asyncio.create_task(action(coordinator, "handoff", required_bytes=128 * MiB))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await legacy_choice(runtime, False)
        on = await legacy_choice(runtime, True)
        assert on["owner_enabled"] is True and on["enabled"] is False
        release.set()
        assert (await asyncio.wait_for(handoff, 4))["policy"]["phase"] == "local_work"
        assert coordinator.state.record.owner_intent_revision < on["owner_intent_revision"]
        await coordinator.reconcile_owner_intent()
        assert coordinator.state.record.phase == "ready" and runtime.enabled
    finally:
        release.set()
        await asyncio.gather(handoff, return_exceptions=True)


async def test_controller_outage_and_both_service_restarts_preserve_off_then_explicit_on(tmp_path):
    coordinator, runtime, store = await make_coordinated(tmp_path)
    old_pid = coordinator.status()["workers"]["voice"]["owned_pid"]
    await coordinator.close()
    try:
        # Converse's native owner control needs neither the resource controller
        # nor an inference worker to commit the preference.
        off = await legacy_choice(runtime, False)
        assert off["enabled"] is False and off["owner_enabled"] is False
    finally:
        store.close()
    coordinator, runtime, store = await make_coordinated(tmp_path)
    try:
        assert not runtime.enabled
        assert coordinator.state.record.phase == "off"
        assert coordinator.status()["workers"]["voice"]["owned_pid"] is None
        on = await legacy_choice(runtime, True)
        assert on["owner_enabled"] and not on["enabled"]
        await coordinator.reconcile_owner_intent()
        assert coordinator.state.record.phase == "ready" and runtime.enabled
        assert coordinator.status()["workers"]["voice"]["owned_pid"] != old_pid
    finally:
        await coordinator.close()
        store.close()


async def test_legacy_resource_owner_off_seeds_new_intent_without_starting_workers(tmp_path):
    from services.resources.state import ResourceRecord

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    legacy = ResourceRecord(desired_converse=False).model_dump()
    legacy.pop("owner_intent_revision")
    policy = control / "resources-state.json"
    policy.write_text(json.dumps(legacy))
    policy.chmod(0o600)
    coordinator, runtime, store = await make_coordinated(tmp_path)
    try:
        assert coordinator.intent.read().enabled is False
        assert runtime.enabled is False and coordinator.state.record.phase == "off"
        assert coordinator.status()["workers"]["voice"]["owned_pid"] is None
    finally:
        await coordinator.close()
        store.close()


async def test_failed_owner_on_is_not_retried_until_a_new_explicit_request(coordinated):
    coordinator, runtime, _ = coordinated
    await action(coordinator, "converse", enabled=False)
    coordinator.memory_probe = lambda: 0
    await legacy_choice(runtime, True)
    await coordinator.reconcile_owner_intent()
    assert coordinator.state.record.phase == "failed" and not runtime.enabled
    previous = coordinator.state.record.revision
    coordinator.memory_probe = lambda: 2 * 1024 * MiB
    await coordinator.reconcile_owner_intent()
    assert coordinator.state.record.revision == previous and not runtime.enabled
    await legacy_choice(runtime, True)
    await coordinator.reconcile_owner_intent()
    assert coordinator.state.record.phase == "ready" and runtime.enabled


async def test_replayed_old_owner_on_receipt_cannot_change_a_newer_legacy_off(coordinated):
    coordinator, runtime, _ = coordinated
    original = await action(coordinator, "converse", key="old-owner-on", enabled=True)
    assert original["ok"]
    off = await legacy_choice(runtime, False)
    replayed = await action(coordinator, "converse", key="old-owner-on", enabled=True)
    assert replayed == original
    assert not runtime.enabled
    assert coordinator.intent.read().revision == off["owner_intent_revision"]
    assert coordinator.status()["policy"]["desired_converse"] is False
    await coordinator.reconcile_owner_intent()
    assert coordinator.state.record.phase == "off"


async def test_resource_owner_off_still_stops_owned_workers_when_converse_http_is_down(coordinated):
    coordinator, runtime, _ = coordinated
    original = coordinator.converse.enabled

    async def unavailable(enabled):
        raise httpx.ConnectError("synthetic Converse control outage")

    coordinator.converse.enabled = unavailable
    try:
        result = await action(coordinator, "converse", enabled=False)
        assert result["ok"] is False, "missing control acknowledgment must stay unconfirmed"
        assert not coordinator.intent.read().enabled and not runtime.enabled
        stopped = coordinator.status()["workers"]["voice"]
        assert stopped["state"] == "stopped"
        assert stopped["stop_evidence"]["reaped"] and stopped["stop_evidence"]["group_empty"]
    finally:
        coordinator.converse.enabled = original


async def test_converse_only_restart_requires_and_recovers_resource_readiness(coordinated):
    coordinator, runtime, store = coordinated
    pid = coordinator.status()["workers"]["voice"]["owned_pid"]
    recovered = ConverseRuntime(
        LocalJournal(store, "converse"),
        runtime.provider,
        intent=ConverseIntent(runtime.intent.path),
        resources_managed=True,
    )
    assert not recovered.enabled and recovered.intent.read().enabled
    previous = coordinator.converse
    coordinator.converse = ConverseControl(
        "http://127.0.0.1:8762",
        "r" * 32,
        transport=httpx.ASGITransport(app=converse_app(recovered, AUTH)),
    )
    try:
        await coordinator.reconcile_owner_intent()
        assert recovered.enabled and coordinator.state.record.phase == "ready"
        assert coordinator.status()["workers"]["voice"]["owned_pid"] == pid
        await legacy_choice(recovered, False)
        await coordinator.reconcile_owner_intent()
        assert not recovered.enabled and coordinator.state.record.phase == "off"
    finally:
        await previous.close()


async def test_handoff_rechecks_provider_locality_after_converse_reconfiguration(coordinated):
    coordinator, runtime, _ = coordinated
    before = coordinator.status()["workers"]["voice"]["owned_pid"]
    await action(coordinator, "profile", profile="local_work_priority")
    runtime.provider.info = ProviderInfo(provider="synthetic-local", model="fixture", local=True)
    result = await action(coordinator, "handoff", required_bytes=128 * MiB)
    assert not result["ok"] and "not managed" in result["error"]
    assert runtime.enabled and coordinator.state.record.phase == "ready"
    assert coordinator.status()["workers"]["voice"]["owned_pid"] == before


async def test_unknown_headroom_refuses_admission_after_verified_stop(coordinated):
    coordinator, runtime, _ = coordinated
    await action(coordinator, "profile", profile="local_work_priority")
    coordinator.memory_probe = lambda: None
    result = await action(coordinator, "handoff", required_bytes=128 * MiB)
    assert not result["ok"] and result["policy"]["lease_id"] is None
    assert result["policy"]["phase"] == "failed" and not runtime.enabled
    assert coordinator.status()["workers"]["voice"]["state"] == "stopped"


def synthetic_gpu(coordinator):
    running = coordinator.manager.status()["workers"]["voice"]["state"] == "running"
    return {
        "pci_bus_id": "0000:03:00.0",
        "total_bytes": 8 * 1024 * MiB,
        "used_bytes": (4 if running else 3) * 1024 * MiB,
        "free_bytes": (4 if running else 5) * 1024 * MiB,
        "worker_resident_bytes": 1024 * MiB if running else 0,
    }


async def test_gpu_declared_headroom_and_restore_use_measurement_not_a_pid_guess(coordinated):
    coordinator, runtime, _ = coordinated
    coordinator.gpu_probe = lambda: synthetic_gpu(coordinator)
    coordinator.gpu_restore_reserve = 512 * MiB
    before = coordinator.manager.status()["workers"]["voice"]["owned_pid"]
    await action(coordinator, "profile", profile="local_work_priority")
    result = await action(
        coordinator,
        "handoff",
        key="gpu-handoff",
        required_bytes=128 * MiB,
        required_gpu_bytes=1024 * MiB,
    )
    assert result["ok"] and not runtime.enabled
    assert result["policy"]["required_gpu_bytes"] == 1024 * MiB
    assert result["policy"]["restore_gpu_bytes"] >= 1152 * MiB
    assert coordinator.gpu_release["used_bytes_decrease"] == 1024 * MiB
    repeated = await action(
        coordinator,
        "handoff",
        key="gpu-handoff",
        required_bytes=128 * MiB,
        required_gpu_bytes=1024 * MiB,
    )
    assert repeated == result
    restored = await action(coordinator, "restore", key="gpu-restore")
    assert restored["ok"] and runtime.enabled
    assert restored["policy"]["required_gpu_bytes"] == 0
    assert coordinator.manager.status()["workers"]["voice"]["owned_pid"] != before


async def test_gpu_request_without_supported_telemetry_does_not_disrupt_voice(coordinated):
    coordinator, runtime, _ = coordinated
    before = coordinator.manager.status()["workers"]["voice"]["owned_pid"]
    await action(coordinator, "profile", profile="local_work_priority")
    result = await action(
        coordinator, "handoff", required_bytes=128 * MiB, required_gpu_bytes=1024 * MiB
    )
    assert not result["ok"] and runtime.enabled and result["policy"]["phase"] == "ready"
    assert coordinator.manager.status()["workers"]["voice"]["owned_pid"] == before


@pytest.mark.parametrize("when", ["before", "after"])
async def test_unknown_gpu_measurement_never_admits_work(coordinated, when):
    coordinator, runtime, _ = coordinated
    coordinator.gpu_probe = lambda: (
        None
        if (
            when == "before"
            or coordinator.manager.status()["workers"]["voice"]["state"] == "stopped"
        )
        else synthetic_gpu(coordinator)
    )
    await action(coordinator, "profile", profile="local_work_priority")
    result = await action(
        coordinator, "handoff", required_bytes=128 * MiB, required_gpu_bytes=1024 * MiB
    )
    assert not result["ok"] and result["policy"]["lease_id"] is None
    assert runtime.enabled is (when == "before")


async def test_insufficient_gpu_after_release_refuses_and_recovery_keeps_high_water_mark(
    coordinated,
):
    coordinator, runtime, _ = coordinated
    coordinator.gpu_probe = lambda: synthetic_gpu(coordinator)
    await action(coordinator, "profile", profile="local_work_priority")
    result = await action(
        coordinator, "handoff", required_bytes=128 * MiB, required_gpu_bytes=6 * 1024 * MiB
    )
    assert not result["ok"] and not runtime.enabled and result["policy"]["phase"] == "failed"
    reserve = result["policy"]["restore_gpu_bytes"]
    coordinator.gpu_probe = lambda: {**synthetic_gpu(coordinator), "free_bytes": 1}
    refused = await action(coordinator, "restore", key="gpu-restore-pressure")
    assert (
        not refused["ok"] and coordinator.manager.status()["workers"]["voice"]["state"] == "stopped"
    )
    coordinator.gpu_probe = lambda: synthetic_gpu(coordinator)
    restored = await action(coordinator, "restore", key="gpu-restore-recovered")
    assert restored["ok"] and runtime.enabled and restored["policy"]["restore_gpu_bytes"] == reserve


async def test_local_decisions_pause_during_handoff_but_direct_agent_remains_available(tmp_path):
    state = ResourceState(tmp_path / "resources-state.json")
    provider = ScriptedProvider(["direct Agent response"])
    provider.info = ProviderInfo(provider="synthetic-local", model="fixture", local=True)
    try:
        state.save(phase="local_work")
        with pytest.raises(ProviderError, match="Local Decisions.*paused"):
            await ResourceBoundProvider(provider, state.path, "decisions").complete([])
        assert provider.calls == []
        assert (
            await ResourceBoundProvider(provider, state.path, "agent").complete([])
            == "direct Agent response"
        )
    finally:
        state.close()


async def test_task_bound_release_ignores_other_tasks_then_restores(coordinated):
    coordinator, _, _ = coordinated

    async def running(task):
        return {"state": "awaiting_approval"}

    coordinator.task_status = running
    await action(coordinator, "profile", profile="local_work_priority")
    assert (await action(coordinator, "handoff", required_bytes=128 * MiB, task_id="target"))["ok"]
    await action(coordinator, "task_finished", key="other", task_id="other")
    assert coordinator.state.record.phase == "local_work"
    await action(coordinator, "task_finished", key="target", task_id="target")
    assert coordinator.state.record.phase == "ready"


@pytest.mark.parametrize("inactive", ["succeeded", "failed", "cancelled", "paused"])
async def test_inactive_bound_task_refuses_before_disrupting_voice(coordinated, inactive):
    coordinator, runtime, _ = coordinated
    before = coordinator.status()["workers"]["voice"]["owned_pid"]

    async def status(task_id):
        return {"state": inactive}

    coordinator.task_status = status
    await action(coordinator, "profile", profile="local_work_priority")
    refused = await action(coordinator, "handoff", required_bytes=128 * MiB, task_id="target")
    assert not refused["ok"] and runtime.enabled
    assert coordinator.state.record.phase == "ready"
    assert coordinator.status()["workers"]["voice"]["owned_pid"] == before


async def test_resource_api_owner_only_and_no_process_parameters(coordinated):
    coordinator, _, _ = coordinated
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(coordinator, AUTH)), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/resources")).status_code == 401
        client.headers["Authorization"] = "Bearer " + "r" * 32
        assert (
            await client.post(
                "/v1/resources/actions", json={"operation": "restore", "idempotency_key": "x"}
            )
        ).status_code == 403
        client.headers["Authorization"] = "Bearer " + "o" * 32
        assert (await client.get("/v1/resources")).status_code == 200
        assert (
            await client.post(
                "/v1/resources/actions",
                json={"operation": "restore", "idempotency_key": "x", "argv": ["bad"]},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/v1/resources/actions",
                json={"operation": "restore", "idempotency_key": "x", "enabled": True},
            )
        ).status_code == 422


async def test_uncertain_resource_action_is_not_replayed(coordinated):
    coordinator, _, _ = coordinated
    payload = ResourceAction(operation="restore", idempotency_key="interrupted").model_dump(
        exclude_unset=True
    )
    import hashlib
    import json

    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    coordinator.db.execute("INSERT INTO actions VALUES(?,?,NULL)", ("interrupted", digest))
    coordinator.db.commit()
    result = await coordinator.action(payload)
    assert result["uncertain"] and not result["ok"]


async def test_local_only_and_missing_policy_block_cloud_before_call(tmp_path):
    state = ResourceState(tmp_path / "resources-state.json", profile="local_only")
    provider = ScriptedProvider(["public"])
    provider.info = ProviderInfo(provider="meta", model="contributor", local=False)
    guarded = ResourceBoundProvider(provider, state.path, "agent")
    with pytest.raises(ProviderError, match="unavailable"):
        await guarded.complete([])
    state.save(phase="ready")
    with pytest.raises(ProviderError, match="Local-only"):
        await guarded.complete([])
    assert provider.calls == []
    provider.info = ProviderInfo(provider="fixture", model="local", local=True)
    assert await guarded.complete([]) == "public"
    state.save(phase="local_work")
    with pytest.raises(ProviderError, match="paused"):
        await ResourceBoundProvider(provider, state.path, "converse").complete([])
    state.close()


async def test_voice_stt_uses_memory_and_paused_requests_do_not_load_models(tmp_path, monkeypatch):
    data = b"public synthetic audio fixture"
    calls = []

    class Whisper:
        def __init__(self, *args, **kwargs):
            calls.append("loaded")

        def transcribe(self, audio, **kwargs):
            assert isinstance(audio, io.BytesIO) and audio.read() == data
            return [SimpleNamespace(text="Synthetic text")], SimpleNamespace(language="en")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setattr(
        tempfile,
        "NamedTemporaryFile",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Named audio file forbidden")),
    )
    state = ResourceState(tmp_path / "resources-state.json")
    state.save(phase="ready")
    app = voice_app(AUTH, resource_policy=state.path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer " + "c" * 32},
    ) as client:
        result = await client.post(
            "/v1/audio/transcriptions", files={"file": ("fixture.wav", data, "audio/wav")}
        )
        assert result.status_code == 200 and result.json()["retained_audio"] is False
        assert calls == ["loaded"]
        state.save(phase="unloading")
        result = await client.post(
            "/v1/audio/transcriptions", files={"file": ("fixture.wav", data, "audio/wav")}
        )
        assert result.status_code == 409 and calls == ["loaded"]
    state.close()


@pytest.mark.parametrize(
    "url", ["https://example.invalid", "http://user:secret@127.0.0.1", "http://127.0.0.1?secret=x"]
)
def test_control_origin_rejects_remote_credentials_and_queries(url):
    with pytest.raises(ValueError, match="loopback"):
        local_base(url)


async def test_resource_client_does_not_forward_owner_auth_on_redirect():
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://example.invalid/steal"})

    client = ResourceClient(
        "http://127.0.0.1:8764", "synthetic-owner", transport=httpx.MockTransport(response)
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.status()
        assert len(requests) == 1 and requests[0].url.host == "127.0.0.1"
    finally:
        await client.close()


async def test_task_release_retries_lost_transport_with_same_durable_key():
    requests = []

    def response(request):
        requests.append(json.loads(request.content))
        if len(requests) < 3:
            raise httpx.ReadError("Synthetic lost control response", request=request)
        return httpx.Response(200, json={"ok": True})

    client = ResourceClient(
        "http://127.0.0.1:8764", "synthetic-owner", transport=httpx.MockTransport(response)
    )
    try:
        assert (await client.task_finished("target"))["ok"]
        assert len(requests) == 3 and all(row == requests[0] for row in requests)
    finally:
        await client.close()


async def test_task_release_never_retries_recorded_uncertain_intent():
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": False, "uncertain": True})

    client = ResourceClient(
        "http://127.0.0.1:8764", "synthetic-owner", transport=httpx.MockTransport(response)
    )
    try:
        with pytest.raises(RuntimeError, match="unconfirmed"):
            await client.task_finished("target")
        assert len(requests) == 1
    finally:
        await client.close()


async def test_agent_shutdown_waits_for_terminal_resource_callback(tmp_path):
    from core.policy import ToolPolicy
    from packages.contracts.models import TaskRequest
    from services.agent.runtime import AgentRuntime

    entered, release = asyncio.Event(), asyncio.Event()

    class Client:
        restored = False
        closed = False

        async def task_finished(self, task_id):
            entered.set()
            await release.wait()
            assert not self.closed
            self.restored = True

        async def close(self):
            assert self.restored
            self.closed = True

    store = Store(tmp_path / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="agent"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "proof.txt").write_text("public synthetic proof")
    provider = ScriptedProvider(
        [
            json.dumps({"tool": "read_file", "arguments": {"path": "proof.txt"}}),
            json.dumps(
                {"tool": "finish", "arguments": {"objective_revision": 1, "summary": "read"}}
            ),
        ]
    )
    client = Client()
    runtime = AgentRuntime(
        LocalJournal(store, "agent"),
        provider,
        workspace,
        ToolPolicy(workspace),
        resource_client=client,
    )
    try:
        task = await runtime.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=1,
                objective="Read proof.txt",
                idempotency_key="shutdown-callback",
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert (await runtime.status(task.task_id)).state == "succeeded"
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert not closing.done() and not client.closed
        release.set()
        await asyncio.wait_for(closing, 2)
        assert client.closed and client.restored
    finally:
        release.set()
        await runtime.close()
        store.close()
