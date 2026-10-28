"""Real owned synthetic processes; no Qwen runtime, weights or inference."""

import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest

from packages.providers.qwen3_ipc import MANIFEST_SHA256, MODEL, PROFILE
from packages.resources.processes import WorkerManager, WorkerSpec
from services.resources.coordinator import Qwen3AdmissionError, ResourceCoordinator
from services.resources.state import ResourceState

MiB = 1024**2
BINDING = {
    "provider": "managed-qwen3",
    "model": MODEL,
    "profile": PROFILE,
    "manifest_sha256": MANIFEST_SHA256,
    "resources_url": "http://127.0.0.1:8764",
    "max_input_tokens": 2048,
    "max_new_tokens": 128,
}


class Control:
    def __init__(self):
        self.value = True
        self.binding = dict(BINDING)
        self.provider = {
            "provider": "managed-qwen3",
            "model": MODEL,
            "local": True,
            "streaming": True,
        }

    async def health(self):
        return {
            "provider": dict(self.provider),
            "managed_text": copy.deepcopy(self.binding),
            "resource_enabled": self.value,
        }

    async def enabled(self, value):
        self.value = value

    async def close(self):
        pass


@pytest.fixture
async def env(tmp_path):
    marker = tmp_path / "ready"
    script = tmp_path / "worker.py"
    script.write_text(
        "import os,time\nfrom pathlib import Path\n"
        "data=bytearray(4*1024*1024)\n"
        "Path('ready').write_text(str(os.getpid()))\n"
        "while True:time.sleep(.01)\n"
    )

    async def ready(spec, process):
        return marker.exists() and marker.read_text() == str(process.pid)

    manager = WorkerManager(
        [WorkerSpec("qwen3", (sys.executable, str(script)), tmp_path)], readiness=ready
    )
    control = Control()
    memory = [1024 * MiB]
    coordinator = ResourceCoordinator(
        manager,
        ResourceState(tmp_path / "control/resources-state.json"),
        control,
        memory_probe=lambda: memory[0],
        qwen3_restore_reserve_bytes=512 * MiB,
        managed_text_binding=BINDING,
    )
    await coordinator.initialize()
    assert coordinator.state.record.phase == "ready"
    try:
        yield SimpleNamespace(
            coordinator=coordinator, manager=manager, control=control, memory=memory
        )
    finally:
        await coordinator.close()


async def action(env, operation, key=None, **fields):
    return await env.coordinator.action(
        {"operation": operation, "idempotency_key": key or operation, **fields}
    )


async def test_bound_local_text_handoff_releases_real_group_and_restores(env):
    original = env.manager.owned_identity("qwen3")
    assert original is not None
    assert not env.coordinator.status()["local_text_worker_unmanaged"]
    await action(env, "profile", profile="local_work_priority")
    handed = await action(env, "handoff", required_bytes=128 * MiB)
    assert handed["ok"] and handed["policy"]["phase"] == "local_work"
    row = env.manager.status()["workers"]["qwen3"]
    assert row["owned_pgid"] is None and not row["ready"]
    assert row["stop_evidence"]["reaped"] and row["stop_evidence"]["group_empty"]
    assert not env.control.value
    assert (await action(env, "restore"))["ok"]
    assert env.manager.owned_identity("qwen3") is not original
    assert env.control.value


@pytest.mark.parametrize("change", ["absent", "model", "origin", "bool-limit", "extra", "cloud"])
async def test_changed_health_cannot_borrow_managed_release_authority(env, change):
    original = env.manager.owned_identity("qwen3")
    await action(env, "profile", profile="local_work_priority")
    if change == "absent":
        env.control.binding = None
    elif change == "model":
        env.control.binding["model"] = "unmanaged-model"
    elif change == "origin":
        env.control.binding["resources_url"] = "http://127.0.0.1:9999"
    elif change == "bool-limit":
        env.control.binding["max_input_tokens"] = True
    elif change == "extra":
        env.control.binding["pid"] = original.pid
    else:
        env.control.provider["local"] = False
    result = await action(env, "handoff", required_bytes=128 * MiB)
    assert not result["ok"] and "not managed" in result["error"]
    assert env.manager.is_current(original) and env.control.value
    assert env.coordinator._qwen3_preemptions == 0


async def test_released_generation_is_zero_residency_only_with_retained_proof(env):
    original = env.manager.owned_identity("qwen3")
    stopped = await env.manager.stop_if_current(original)
    assert stopped["stop_evidence"]["group_empty"]
    await action(env, "profile", profile="local_work_priority")
    assert (await action(env, "handoff", required_bytes=128 * MiB))["ok"]
    assert env.manager.owned_identity("qwen3") is None


async def test_recovery_requires_fresh_headroom_but_resident_use_does_not(env):
    policy = env.coordinator.qwen3_policy()
    original = env.manager.owned_identity("qwen3")
    env.memory[0] = None
    assert await env.coordinator.admit_qwen3(policy) is original
    await env.manager.stop_if_current(original)
    for memory in (None, 511 * MiB):
        env.memory[0] = memory
        with pytest.raises(Qwen3AdmissionError) as caught:
            await env.coordinator.admit_qwen3(policy)
        assert caught.value.cleanup_confirmed
        assert env.manager.owned_identity("qwen3") is None
    env.memory[0] = 512 * MiB
    recovered = await env.coordinator.admit_qwen3(policy)
    assert recovered is not original and env.manager.is_current(recovered)


async def test_new_off_preempts_awaited_recovery_before_identity_escapes(env):
    coordinator = env.coordinator
    await env.manager.stop("qwen3")
    entered, release = asyncio.Event(), asyncio.Event()
    original_start = env.manager.start

    async def delayed(worker_id):
        entered.set()
        await release.wait()
        return await original_start(worker_id)

    env.manager.start = delayed
    admission = asyncio.create_task(coordinator.admit_qwen3(coordinator.qwen3_policy()))
    off = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        off = asyncio.create_task(action(env, "converse", enabled=False))
        async with asyncio.timeout(2):
            while coordinator._qwen3_preemptions != 1:
                await asyncio.sleep(0.001)
        release.set()
        with pytest.raises(Qwen3AdmissionError) as caught:
            await asyncio.wait_for(admission, 4)
        assert caught.value.cleanup_confirmed
        assert (await off)["policy"]["phase"] == "off"
        assert env.manager.owned_identity("qwen3") is None
        assert coordinator._qwen3_preemptions == 0
    finally:
        release.set()
        await asyncio.gather(admission, *([off] if off is not None else []), return_exceptions=True)


async def test_replayed_off_does_not_revoke_later_admission(env):
    off = await action(env, "converse", key="off-once", enabled=False)
    assert off["ok"]
    assert (await action(env, "converse", key="later-on", enabled=True))["ok"]
    current = env.manager.owned_identity("qwen3")
    assert await action(env, "converse", key="off-once", enabled=False) == off
    assert env.manager.is_current(current)
    assert await env.coordinator.admit_qwen3(env.coordinator.qwen3_policy()) is current
    assert env.coordinator._qwen3_preemptions == 0


async def test_reopening_converse_does_not_reserve_a_resident_text_child_twice(env):
    current = env.manager.owned_identity("qwen3")
    env.coordinator.restore_reserve = 800 * MiB
    env.memory[0] = 400 * MiB
    assert (await action(env, "restore"))["ok"]
    assert env.manager.is_current(current)
    assert env.control.value
    await env.manager.stop_if_current(current)
    denied = await action(env, "restore", key="needs-fresh-allocation")
    assert not denied["ok"] and "headroom" in denied["error"]
    assert env.manager.owned_identity("qwen3") is None


async def test_lost_resident_credit_requires_fresh_headroom_after_await(env):
    credited = env.manager.owned_identity("qwen3")
    env.coordinator.restore_reserve = 800 * MiB
    env.memory[0] = 400 * MiB

    async def lose_credited_child(worker_id):
        assert worker_id == "qwen3"
        stopped = await env.manager.stop_if_current(credited)
        assert stopped["stop_evidence"]["reaped"] and stopped["stop_evidence"]["group_empty"]

    env.coordinator.before_start = lose_credited_child
    refused = await action(env, "restore", key="lost-credit")
    assert not refused["ok"] and "headroom" in refused["error"]
    assert env.manager.owned_identity("qwen3") is None
    assert env.coordinator.state.record.phase == "failed"
