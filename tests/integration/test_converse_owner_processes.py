"""Independent Converse HTTP process and real owned memory-worker restoration.

Providers are deliberately unusable synthetic endpoints. This proves native
control/persistence behavior, not voice/model or physical playback performance.
"""

import asyncio
import json
import os
import signal
import socket
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx
import pytest
from test_standing_resources_registration import factory as factory
from test_standing_resources_registration import gateway, ordinary, query

from packages.contracts.client import JournalClient
from packages.contracts.live_captures import (
    PROTOCOL,
    LiveCaptureRecoveryQuery,
    LiveCaptureRegistration,
)
from packages.resources import WorkerManager, WorkerSpec
from services.common.auth import load_or_create_tokens
from services.converse.intent import ConverseIntent
from services.resources.client import ConverseControl
from services.resources.coordinator import ResourceCoordinator
from services.resources.state import ResourceState

MiB = 1024 * 1024


@asynccontextmanager
async def native_services(root, data, config, workspace, ports, tokens):
    """Own actual Session/Decisions/Converse processes, without model requests."""
    children = {}
    logs = []
    try:
        for role, port in ports.items():
            log = NamedTemporaryFile(
                dir=data, prefix=f"readiness-{role}-", suffix=".log", delete=False
            )
            log_path = Path(log.name)
            logs.append(log)
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "services.launcher",
                "--data-dir",
                str(data),
                "--config",
                str(config),
                "--workspace",
                str(workspace),
                "serve",
                role,
                "--port",
                str(port),
                cwd=root,
                env={"PYTHONPATH": str(root), "PATH": os.environ["PATH"]},
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            children[role] = child
            path = "/v1/decisions/readiness" if role == "decisions" else "/health"
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                timeout=1,
                headers={"Authorization": "Bearer " + tokens["owner"]},
            ) as http:
                async with asyncio.timeout(10):
                    while True:
                        assert child.returncode is None, log_path.read_text()
                        try:
                            response = await http.get(path)
                            if response.status_code == 200 and (
                                role != "decisions" or response.json()["ready"]
                            ):
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.02)
        yield children
    finally:
        for child in reversed(tuple(children.values())):
            if child.returncode is None:
                child.terminate()
        forced = []
        for child in reversed(tuple(children.values())):
            try:
                await asyncio.wait_for(child.wait(), 8)
            except TimeoutError:
                child.kill()
                await child.wait()
                forced.append(child.pid)
            assert not Path(f"/proc/{child.pid}").exists()
        for log in logs:
            log.close()
        assert not forced, f"Temporary services needed forced cleanup: {forced}"


async def test_killed_session_recovers_closed_live_registration_over_http(tmp_path):
    data = tmp_path / "data"
    tokens = load_or_create_tokens(data)
    config = data / "config.json"
    config.write_text("{}")
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    args = root, data, config, tmp_path / "workspace", {"session": port}, tokens
    journal = JournalClient(url, tokens["converse"], transport=httpx.AsyncHTTPTransport())
    try:
        async with native_services(*args) as children:
            async with httpx.AsyncClient(base_url=url, trust_env=False) as owner:
                response = await owner.post(
                    "/v1/sessions",
                    headers={"Authorization": "Bearer " + tokens["owner"]},
                    json={"channel": "converse"},
                )
                response.raise_for_status()
                session = response.json()
            original = LiveCaptureRegistration(
                schema_version="2.0",
                protocol=PROTOCOL,
                backend=await journal.live_capture_backend(),
                owner_instance="a" * 32,
                capture_id="b" * 32,
                session_id=session["session_id"],
                thread_id=session["thread_id"],
                epoch=session["epoch"],
            )
            registered = await journal.register_live_capture(original)
            assert registered.capture_open and registered.receipt.original == original
            child = children["session"]
            child.kill()
            assert await asyncio.wait_for(child.wait(), 5) == -signal.SIGKILL

        query = LiveCaptureRecoveryQuery.model_validate(
            original.model_dump(mode="json", exclude={"backend"}), strict=True
        )
        with pytest.raises(httpx.ConnectError):
            await journal.recover_live_capture(query)
        async with native_services(*args):
            recovered = await journal.recover_live_capture(query)
            assert recovered.original == query
            assert recovered.capture.original == original
            assert recovered.capture.receipt == registered.receipt
            assert recovered.capture.backend.backend_id == original.backend.backend_id
            assert recovered.capture.backend.controller_id != original.backend.controller_id
            assert not recovered.capture.capture_open
            assert await journal.recover_live_capture(query) == recovered
    finally:
        await journal.close()


@pytest.fixture
async def ordinary_handoff(factory):
    native = await factory()
    identity = ordinary(native.store, task_id="synthetic-native-task", key="native-converse-owner")
    async with gateway(native) as journal:
        context = await journal.resource_task_context(query(identity.task_id))
        assert context.family == "ordinary" and context.standing is None
        assert context.ordinary == identity
        yield journal


async def test_native_owner_off_during_restore_and_later_explicit_on(tmp_path, ordinary_handoff):
    data = tmp_path / "owner-data"
    tokens = load_or_create_tokens(data)
    state = ResourceState(data / "resources-state.json", profile="local_work_priority")
    state.save()
    worker = tmp_path / "memory_worker.py"
    marker = tmp_path / "ready"
    worker.write_text(
        "import os,time\nfrom pathlib import Path\n"
        "b=bytearray(40*1024*1024)\n"
        "for n in range(0,len(b),4096):b[n]=1\n"
        "Path('ready').write_text(str(os.getpid()))\n"
        "while True:time.sleep(.1)\n"
    )

    async def ready(spec, process):
        return marker.exists() and marker.read_text() == str(process.pid)

    manager = WorkerManager(
        [WorkerSpec("voice", (sys.executable, str(worker)), tmp_path)], readiness=ready
    )
    ports = {}
    for role in ("session", "decisions", "converse"):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports[role] = sock.getsockname()[1]
    port = ports["converse"]
    url = f"http://127.0.0.1:{port}"
    coordinator = ResourceCoordinator(
        manager,
        state,
        ConverseControl(url, tokens["resources"]),
        handoff_journal=ordinary_handoff,
        memory_probe=lambda: 2 * 1024 * MiB,
    )
    config = data / "config.json"
    config.write_text(
        json.dumps(
            {
                "session_url": f"http://127.0.0.1:{ports['session']}",
                "decisions_url": f"http://127.0.0.1:{ports['decisions']}",
                "agent_url": "http://127.0.0.1:9",
                "agent_enabled": False,
                "distribution": {"agent": False, "converse": True, "decisions": True},
                "providers": {
                    "converse": {
                        # Nonlocal metadata avoids claiming ownership of an unmanaged
                        # local text model. No inference request is made to this endpoint.
                        "kind": "compatible",
                        "base_url": "https://provider-fixture.invalid/v1",
                        "model": "synthetic-unavailable",
                        "allow_cloud": True,
                    },
                    "decisions": {
                        "kind": "compatible",
                        "base_url": "https://provider-fixture.invalid/v1",
                        "model": "synthetic-unavailable",
                        "allow_cloud": True,
                    },
                },
                "resources": {"enabled": True},
                "streaming_windows": {"enabled": False},
            }
        )
    )
    root = Path(__file__).resolve().parents[2]
    services = AsyncExitStack()
    children = await services.enter_async_context(
        native_services(root, data, config, tmp_path / "workspace", ports, tokens)
    )
    server = children["converse"]
    release = asyncio.Event()
    restoration = None
    proof = {
        "scope": "independent native Converse HTTP process; real owned 40MiB fixture; zero model/voice inference"
    }
    async with httpx.AsyncClient(base_url=url, timeout=2, trust_env=False) as owner:
        owner.headers["Authorization"] = "Bearer " + tokens["owner"]
        try:
            for _ in range(150):
                assert server.returncode is None, "Native Converse process exited before readiness"
                try:
                    response = await owner.get("/health")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Native Converse HTTP readiness timed out")
            cold = response.json()
            assert cold["owner_enabled"] and not cold["enabled"]
            assert not cold["resource_enabled"]
            assert cold["decisions"]["required"] and cold["decisions"]["ready"]
            await coordinator.initialize()
            assert coordinator.state.record.phase == "ready"
            initial_pid = manager.status()["workers"]["voice"]["owned_pid"]
            assert (await owner.get("/health")).json()["context_history"] == "measured"
            assert (await owner.get("/health")).json()["enabled"]
            handed = await coordinator.action(
                {
                    "operation": "handoff",
                    "required_bytes": 128 * MiB,
                    "task_id": "synthetic-native-task",
                    "idempotency_key": "native-handoff",
                }
            )
            assert handed["ok"] and handed["policy"]["phase"] == "local_work"
            entered = asyncio.Event()
            original = manager.start

            async def delayed(worker_id):
                entered.set()
                await release.wait()
                return await original(worker_id)

            manager.start = delayed
            restoration = asyncio.create_task(
                coordinator.action(
                    {
                        "operation": "task_finished",
                        "task_id": "synthetic-native-task",
                        "idempotency_key": "native-task-finished",
                    }
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            off = await owner.post("/v1/converse/enabled", json={"enabled": False})
            assert off.status_code == 200 and not off.json()["enabled"]
            assert not restoration.done()
            persisted = ConverseIntent(data / "converse-intent.json").read()
            assert (
                not persisted.enabled and persisted.revision == off.json()["owner_intent_revision"]
            )
            proof.update(
                {
                    "converse_pid": server.pid,
                    "initial_worker_pid": initial_pid,
                    "owner_off_response": off.json(),
                    "owner_off_acknowledged_before_start_release": True,
                    "status_during_restore": coordinator.status()["policy"],
                }
            )
            release.set()
            restored = await asyncio.wait_for(restoration, 5)
            assert restored["ok"] and restored["policy"]["phase"] == "off"
            assert not (await owner.get("/health")).json()["enabled"]
            stopped = manager.status()["workers"]["voice"]
            assert stopped["state"] == "stopped"
            assert stopped["stop_evidence"]["reaped"] and stopped["stop_evidence"]["group_empty"]
            proof["after_restore"] = {"policy": restored["policy"], "worker": stopped}
            on = await owner.post("/v1/converse/enabled", json={"enabled": True})
            assert on.status_code == 200
            assert on.json()["owner_enabled"] and not on.json()["enabled"]
            await coordinator.reconcile_owner_intent()
            assert coordinator.state.record.phase == "ready"
            assert (await owner.get("/health")).json()["enabled"]
            fresh = manager.status()["workers"]["voice"]
            assert fresh["owned_pid"] != initial_pid
            proof["later_explicit_on"] = {"response": on.json(), "worker": fresh}
        finally:
            release.set()
            if restoration is not None:
                await asyncio.gather(restoration, return_exceptions=True)
            try:
                await coordinator.close()
            finally:
                await services.aclose()
    proof["cleanup"] = {
        "converse_returncode": server.returncode,
        "converse_reaped": not Path(f"/proc/{server.pid}").exists(),
        "initial_worker_reaped": not Path(f"/proc/{initial_pid}").exists(),
        "final_worker_reaped": not Path(f"/proc/{fresh['owned_pid']}").exists(),
        "core_session_reaped": not Path(f"/proc/{children['session'].pid}").exists(),
        "decisions_reaped": not Path(f"/proc/{children['decisions'].pid}").exists(),
        "core_session_returncode": children["session"].returncode,
        "decisions_returncode": children["decisions"].returncode,
    }
    assert all(
        proof["cleanup"][key]
        for key in (
            "converse_reaped",
            "initial_worker_reaped",
            "final_worker_reaped",
            "core_session_reaped",
            "decisions_reaped",
        )
    )
    (tmp_path / "owner-intent-native-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
