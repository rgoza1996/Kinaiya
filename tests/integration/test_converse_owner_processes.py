"""Independent Converse HTTP process and real owned memory-worker restoration.

Providers are deliberately unusable synthetic endpoints. This proves native
control/persistence behavior, not voice/model or physical playback performance.
"""

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import httpx

from packages.resources import WorkerManager, WorkerSpec
from services.common.auth import load_or_create_tokens
from services.converse.intent import ConverseIntent
from services.resources.client import ConverseControl
from services.resources.coordinator import ResourceCoordinator
from services.resources.state import ResourceState

MiB = 1024 * 1024


async def test_native_owner_off_during_restore_and_later_explicit_on(tmp_path):
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
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    coordinator = ResourceCoordinator(
        manager,
        state,
        ConverseControl(url, tokens["resources"]),
        memory_probe=lambda: 2 * 1024 * MiB,
    )
    config = data / "config.json"
    config.write_text(
        json.dumps(
            {
                "session_url": "http://127.0.0.1:9",  # No journal/inference is used in this probe.
                "providers": {
                    "converse": {
                        # Nonlocal metadata avoids claiming ownership of an unmanaged
                        # local text model. No inference request is made to this endpoint.
                        "kind": "compatible",
                        "base_url": "https://provider-fixture.invalid/v1",
                        "model": "synthetic-unavailable",
                        "allow_cloud": True,
                    }
                },
                "resources": {"enabled": True},
                "streaming_windows": {"enabled": False},
            }
        )
    )
    root = Path(__file__).resolve().parents[2]
    server = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "services.launcher",
        "--data-dir",
        str(data),
        "--config",
        str(config),
        "--workspace",
        str(tmp_path / "workspace"),
        "serve",
        "converse",
        "--port",
        str(port),
        cwd=root,
        env={"PYTHONPATH": str(root), "PATH": os.environ["PATH"]},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
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
                if server.returncode is None:
                    server.terminate()
                try:
                    await asyncio.wait_for(server.wait(), 8)
                except TimeoutError:
                    server.kill()
                    await server.wait()
    proof["cleanup"] = {
        "converse_returncode": server.returncode,
        "converse_reaped": not Path(f"/proc/{server.pid}").exists(),
        "initial_worker_reaped": not Path(f"/proc/{initial_pid}").exists(),
        "final_worker_reaped": not Path(f"/proc/{fresh['owned_pid']}").exists(),
    }
    assert all(
        proof["cleanup"][key]
        for key in ("converse_reaped", "initial_worker_reaped", "final_worker_reaped")
    )
    (tmp_path / "owner-intent-native-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
