"""Actual trusted bootstrap entry precedes original process identity checks.

The gate instruments only public first-party fixture copies. It neither grants
execution nor substitutes a PID/proc/pidfd identity predicate.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest
from media_process_helpers import _binding, _Harness

import packages.resources.media_processes as media_processes
import packages.resources.processes as processes

ENTRY_FLAG = "--kinaiya-bootstrap-entry-v1"
ENTRY = b"\x00kinaiya-bootstrap-entry-v1\x00"
OUTPUT = b"PUBLIC-CONFIGURED-OUTPUT" + ENTRY + b"PRESERVE-THIS-TOO"


async def observed(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


def gated_helpers(tmp_path, gate_fd, kind, finish):
    source = Path(processes.__file__).parent
    target = tmp_path / "trusted-helper-copy"
    target.mkdir()
    entered = target / "at-entry-gate"
    for name in ("_bootstrap.py", "_media_bootstrap.py", "_bootstrap_protocol.py"):
        original = source / name
        if not original.exists():
            continue  # The new protocol module is absent at the frozen baseline.
        shutil.copy2(original, target / name)
        selected = "_bootstrap.py" if kind == "worker" else "_media_bootstrap.py"
        if name != selected:
            continue
        text = (target / name).read_text()
        anchor = "from __future__ import annotations\n"
        assert text.count(anchor) == 1
        gate = (
            "\nimport os as _entry_gate_os\n"
            f"with open({str(entered)!r}, 'xb') as _entry_gate_marker:\n"
            "    _entry_gate_marker.write(b'original bootstrap reached gate')\n"
            f"if _entry_gate_os.read({gate_fd}, 1) != b'G':\n"
            "    _entry_gate_os._exit(0)\n"
            f"_entry_gate_os.close({gate_fd})\n"
        )
        if finish in {"malformed", "partial", "eof"}:
            frame = (
                b"?" * len(ENTRY)
                if finish == "malformed"
                else ENTRY[:5]
                if finish == "partial"
                else b""
            )
            gate += f"_entry_gate_os.write(1, {frame!r})\n_entry_gate_os._exit(0)\n"
        (target / name).write_text(text.replace(anchor, anchor + gate, 1))
    return target, entered


@pytest.mark.parametrize("kind", ["worker", "media"])
@pytest.mark.parametrize("finish", ["continue", "cancel", "budget", "malformed", "partial", "eof"])
async def test_original_parent_waits_for_trusted_entry_before_identity_or_grant(
    tmp_path, monkeypatch, kind, finish
):
    gate_read, gate_write = os.pipe()
    helper_dir, at_gate = gated_helpers(tmp_path, gate_read, kind, finish)
    released, verifier_calls, dispatched = asyncio.Event(), [], []
    manager, harness, start = None, None, None

    def release():
        if not released.is_set():
            released.set()
            try:
                os.write(gate_write, b"G")
            except BrokenPipeError:
                pass

    try:
        if kind == "worker":
            marker = tmp_path / "configured-executed"
            program = (
                "import os,sys,time; from pathlib import Path; "
                "Path(sys.argv[1]).write_bytes(b'configured once'); "
                f"os.write(1,{OUTPUT!r}); time.sleep(60)"
            )
            manager = processes.WorkerManager(
                [
                    processes.WorkerSpec(
                        id="phase-control",
                        argv=(sys.executable, "-B", "-c", program, str(marker)),
                        cwd=tmp_path,
                        startup_timeout_s=2,
                        stop_timeout_s=0.1,
                    )
                ],
                readiness=lambda *_: marker.exists(),
            )
            monkeypatch.setattr(
                processes, "_bootstrap_path", lambda: str(helper_dir / "_bootstrap.py")
            )
            verify = processes._verify_bootstrap

            def record_verify(*args):
                verifier_calls.append(released.is_set())
                return verify(*args)

            monkeypatch.setattr(processes, "_verify_bootstrap", record_verify)
        else:
            harness = _Harness(tmp_path, monkeypatch)
            pool, marker = harness.pool()
            binding = _binding()
            launch = harness.register(pool, binding)
            _, grant = harness.grant(launch, binding)
            monkeypatch.setattr(media_processes, "__file__", str(helper_dir / "media_processes.py"))
            verify = media_processes.MediaLaunch._verify_bootstrap

            def record_verify(owner, *args):
                verifier_calls.append(released.is_set())
                return verify(owner, *args)

            monkeypatch.setattr(media_processes.MediaLaunch, "_verify_bootstrap", record_verify)

        native_spawn = asyncio.create_subprocess_exec

        async def original_spawn(*args, **kwargs):
            dispatched.append(args)
            kwargs["pass_fds"] = (*kwargs["pass_fds"], gate_read)
            return await native_spawn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", original_spawn)
        start = asyncio.create_task(
            manager.start("phase-control") if manager is not None else pool.start(launch, grant)
        )
        await observed(at_gate.exists)
        assert verifier_calls == [], "Native identity was read before trusted bootstrap entry"
        assert not marker.exists(), "Configured code executed before entry and native identity"
        assert len(dispatched) == 1
        if manager is not None:
            managed = manager._workers["phase-control"]
            await observed(lambda: managed.process is not None and managed.grant_w is not None)
        if finish == "budget" and harness is not None:
            # Legacy unscoped media supplies no startup timer. Only an explicit
            # fence may retire this still-held original before entry arrives.
            await asyncio.sleep(2.05)
            assert not start.done() and not launch._start_task.done()
            assert not launch._granted and not marker.exists() and verifier_calls == []
            pool.fence(launch)
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
        elif finish == "cancel":
            if harness is not None:
                pool.fence(launch)
            start.cancel()
            if manager is not None:
                await observed(lambda: managed.grant_w is None)
                assert managed.process.returncode is None and not managed.granted
                assert manager._quiesced(managed)[0] is False
                assert not marker.exists() and verifier_calls == []
                release()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(start, 5)
        elif finish != "continue":
            if finish != "budget":
                release()
            if manager is not None:
                if finish == "budget":
                    await observed(lambda: managed.grant_w is None)
                    assert managed.process.returncode is None and not managed.granted
                    assert manager._quiesced(managed)[0] is False
                    assert not marker.exists() and verifier_calls == []
                    release()
                result = await asyncio.wait_for(asyncio.shield(start), 5)
                assert result["ok"] is False and result["error"] == "ownership-unverified"
                assert not manager._workers["phase-control"].granted
            else:
                with pytest.raises(media_processes.MediaProcessError):
                    await asyncio.wait_for(asyncio.shield(start), 5)
                assert not launch._verified and not launch._granted
        if finish != "continue":
            assert verifier_calls == [], "Invalid/absent entry cannot reach the native verifier"
            assert not marker.exists()
            release()
            if manager is not None:
                process = manager._workers["phase-control"].process
                await asyncio.wait_for(process.wait(), 5)
            else:
                settlement = await pool.settle(launch)
                assert settlement.state == "settled"
                assert harness.processes[0].returncode is not None
            assert ENTRY_FLAG in dispatched[0] and len(dispatched) == 1
            assert not marker.exists(), "A refused/fenced original executed after entry release"
            return
        release()
        result = await asyncio.wait_for(asyncio.shield(start), 5)
        assert ENTRY_FLAG in dispatched[0]
        assert verifier_calls == [True]
        await observed(marker.exists)
        if manager is not None:
            assert result["ok"]
            managed = manager._workers["phase-control"]
            await observed(lambda: bytes(managed.stdout_tail) == OUTPUT)
            assert managed.granted and managed.ready
            assert marker.read_bytes() == b"configured once"
        else:
            assert marker.read_bytes() == b"original executed once"
            assert launch._verified and launch._granted
    finally:
        release()
        if start is not None:
            if not start.done():
                start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        if manager is not None:
            await manager.close()
        if harness is not None:
            await harness.close()
        os.close(gate_write)
        os.close(gate_read)


async def test_media_observer_cancel_during_entry_retains_one_original_and_one_execution(
    tmp_path, monkeypatch
):
    gate_read, gate_write = os.pipe()
    helper_dir, at_gate = gated_helpers(tmp_path, gate_read, "media", "continue")
    harness = _Harness(tmp_path, monkeypatch)
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    monkeypatch.setattr(media_processes, "__file__", str(helper_dir / "media_processes.py"))
    native_spawn = asyncio.create_subprocess_exec
    dispatched = []

    async def original_spawn(*args, **kwargs):
        dispatched.append(args)
        kwargs["pass_fds"] = (*kwargs["pass_fds"], gate_read)
        return await native_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", original_spawn)
    observer = asyncio.create_task(pool.start(launch, grant))
    released = False
    try:
        await observed(at_gate.exists)
        original = launch._start_task
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert original is not None and not original.done()
        assert not launch._fenced and not launch._granted and not marker.exists()
        os.write(gate_write, b"G")
        released = True
        child = await asyncio.wait_for(pool.start(launch, grant), 5)
        assert launch._start_task is original and original.done() and not original.cancelled()
        assert child is launch._child and launch._verified and launch._granted
        assert len(dispatched) == len(harness.processes) == 1
        await observed(marker.exists)
        assert marker.read_bytes() == b"original executed once"
    finally:
        if not released:
            try:
                os.write(gate_write, b"G")
            except BrokenPipeError:
                pass
        await asyncio.gather(observer, return_exceptions=True)
        await harness.close()
        os.close(gate_write)
        os.close(gate_read)
