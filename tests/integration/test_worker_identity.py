"""Exact owned identity guards, using synthetic children only."""

import asyncio
import sys

import pytest

from packages.resources.processes import WorkerIdentity, WorkerManager, WorkerSpec


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def manager(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(
        "import sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('ready')\nwhile not Path(sys.argv[2]).exists():time.sleep(.01)\n"
    )
    ready, done = tmp_path / "ready", tmp_path / "done"
    value = WorkerManager(
        [
            WorkerSpec(
                "fixture",
                (sys.executable, str(script), str(ready), str(done)),
                tmp_path,
                stop_timeout_s=0.1,
            )
        ],
        readiness=lambda spec, process: ready.exists(),
    )
    assert (await value.start("fixture"))["ready"]
    yield value, done
    await value.close()


async def test_same_binding_natural_exit_is_reaped_not_misclassified_superseded(manager):
    worker, done = manager
    identity = worker.owned_identity("fixture")
    assert identity is not None
    done.touch()
    await wait_for(lambda: worker.status()["workers"]["fixture"]["state"] == "failed")
    assert not worker.is_current(identity)
    result = await worker.stop_if_current(identity)
    assert result["ok"] and not result.get("superseded")
    assert result["stop_evidence"]["reaped"] and result["stop_evidence"]["group_empty"]
    assert (await worker.stop_if_current(identity))["ok"]


async def test_failed_stop_cached_ready_is_not_current_and_unknown_cleanup_blocks_start(
    manager, monkeypatch
):
    worker, _ = manager
    identity = worker.owned_identity("fixture")

    def broken(managed):
        raise RuntimeError("synthetic ownership observation failure")

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_capture_members", broken)
        with pytest.raises(RuntimeError):
            await worker.stop("fixture")
        assert worker.owned_identity("fixture") is None and not worker.is_current(identity)
        refused = await worker.start("fixture")
        assert (
            not refused["ok"] and worker.status()["workers"]["fixture"]["owned_pid"] == identity.pid
        )
    assert (await worker.stop_if_current(identity))["ok"]


async def test_identity_does_not_expose_process_and_numeric_clone_is_not_authority(manager):
    worker, _ = manager
    identity = worker.owned_identity("fixture")
    assert set(vars(identity)) == {"worker_id", "pid", "start_time"}
    clone = WorkerIdentity(identity.worker_id, identity.pid, identity.start_time)
    assert not worker.is_current(clone)
    assert (await worker.stop_if_current(clone))["error"] == "unowned-identity"
    assert worker.is_current(identity)


async def test_closed_manager_does_not_supply_replacement_cleanup_proof(manager):
    worker, _ = manager
    old = worker.owned_identity("fixture")
    assert (await worker.stop_if_current(old))["ok"]
    assert (await worker.start("fixture"))["ready"]
    new = worker.owned_identity("fixture")
    assert new is not old
    await worker.close()
    result = await worker.stop_if_current(old)
    assert not result["ok"] and result["error"] == "closed" and "stop_evidence" not in result


async def test_repeated_guarded_stop_labels_retained_release_without_fabricating_event(manager):
    worker, _ = manager
    identity = worker.owned_identity("fixture")
    first = await worker.stop_if_current(identity)
    assert first["stop_evidence"]["reaped"] and first["stop_evidence"]["group_empty"]
    repeated = await worker.stop_if_current(identity)
    assert repeated == {
        "ok": True,
        "worker_id": "fixture",
        "already": True,
        "release_confirmed": True,
    }
    assert "stop_evidence" not in repeated
