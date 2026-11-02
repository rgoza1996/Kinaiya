"""#278 cached observations of original disposable Manager bootstrap owners.

These are controlled faults, never a reconstruction of the historical CI cause.
Only an owned synthetic bootstrap may self-terminate; Manager cannot signal,
adopt, write a grant, execute the configured marker, or add a diagnostic waiter.
"""

import asyncio
import builtins
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from packages.resources import WorkerManager, WorkerSpec, processes

DETAIL = "worker bootstrap command observation: result=%s"
EXIT = "worker bootstrap refusal exit: before_grant_close=%s after_reap_attempt=%s"
COARSE = "worker bootstrap refused: reason=%s errno=%s"
PRIVATE = "synthetic-private-bootstrap-config"


@pytest.fixture
async def refused_worker(tmp_path, monkeypatch, record_property):
    opened = []
    original_spawn = asyncio.create_subprocess_exec
    original_process = asyncio.subprocess.Process
    original_os = processes.os
    original_signal = processes.signal
    original_open = builtins.open
    original_pipes = WorkerManager._finalize_pipes
    observations = []

    async def make(mode="after-close", *, hostile=False):
        directory = tmp_path / str(len(opened))
        directory.mkdir(mode=0o700)
        marker = directory / "configured-executed"
        script = directory / "configured-marker.py"
        script.write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        )
        if mode.startswith("before-"):
            bootstrap = directory / "synthetic-bootstrap.py"
            bodies = {
                "before-zero": "import os\nos._exit(0)\n",
                "before-nonzero": "import os\nos._exit(7)\n",
                # Explicitly authorized self-termination of this disposable
                # fixture only; no parent/Manager/foreign process is signalled.
                "before-terminated": "import os,signal\nos.kill(os.getpid(),signal.SIGTERM)\n",
            }
            bootstrap.write_text(bodies[mode])
            monkeypatch.setattr(processes, "_bootstrap_path", lambda: str(bootstrap))
        value = SimpleNamespace(
            mode=mode,
            events=[],
            process=None,
            spawn=None,
            waits=[],
            warning_records=[],
            grant_closes=[],
            fds=(),
            wait_entered=asyncio.Event(),
            wait_release=asyncio.Event(),
            marker=marker,
            manager=None,
            start=None,
            pipe_calls=0,
            diagnostic_cached_failures=0,
            native_wait_results=[],
        )
        manager = WorkerManager(
            [
                WorkerSpec(
                    id="oracle",
                    argv=(sys.executable, str(script)),
                    cwd=directory,
                    env={"ORACLE_PRIVATE": PRIVATE},
                )
            ]
        )
        value.manager = manager
        managed = manager._workers["oracle"]

        class ObservedProcess(original_process):
            @property
            def returncode(self):
                actual = super().returncode
                if managed.process is self:
                    before = managed.grant_w is not None
                    value.events.append(("cached", before, actual, value.pipe_calls))
                    if mode == "cached-unavailable" and before:
                        value.diagnostic_cached_failures += 1
                        raise RuntimeError(PRIVATE)
                return actual

            async def wait(self):
                task = asyncio.current_task()
                native = super().wait()
                observation = SimpleNamespace(
                    caller=task,
                    native=native,
                    native_returned=False,
                    native_result=None,
                    outer_returned=False,
                    outer_finished=False,
                    outer_cancelled=False,
                    outer_error=None,
                )
                value.waits.append(observation)
                value.events.append(("wait_enter", task))
                try:
                    result = await native
                    observation.native_returned = True
                    observation.native_result = result
                    value.native_wait_results.append(result)
                    value.events.append(("wait_native_done", task))
                    if mode == "cancel-reap":
                        value.wait_entered.set()
                        await value.wait_release.wait()
                    observation.outer_returned = True
                    value.events.append(("wait_return", task))
                    return result
                except BaseException as error:
                    observation.outer_cancelled = isinstance(error, asyncio.CancelledError)
                    observation.outer_error = type(error).__name__
                    raise
                finally:
                    observation.outer_finished = True
                    value.events.append(("wait_finally", task))

        async def spawn(*args, **kwargs):
            value.spawn = asyncio.current_task()
            value.fds = tuple(kwargs["pass_fds"])
            process = await original_spawn(*args, **kwargs)
            value.process = process
            assert isinstance(process, original_process)
            if mode.startswith("before-"):
                await process.wait()
                assert process.returncode is not None
            return process

        class OperatingSystem:
            def __getattr__(self, name):
                return getattr(original_os, name)

            def close(self, fd):
                if managed.grant_w is not None and fd == managed.grant_w:
                    value.events.append(("grant_close", fd))
                    value.grant_closes.append(fd)
                return original_os.close(fd)

            def write(self, fd, data):
                assert fd != managed.grant_w, "Refusal must never grant configured execution"
                return original_os.write(fd, data)

            def kill(self, *args):
                pytest.fail("Manager must not signal an unverified bootstrap")

            killpg = kill

        def forbidden(*args, **kwargs):
            pytest.fail("Refusal diagnostics cannot signal or adopt")

        class Signals:
            def __getattr__(self, name):
                return getattr(original_signal, name)

            pidfd_send_signal = staticmethod(forbidden)

        def observed_open(path, *args, **kwargs):
            if isinstance(path, str) and path.endswith("/cmdline"):
                value.events.append(("existing_command_read", path))
                return io.BytesIO(b"")
            return original_open(path, *args, **kwargs)

        async def pipes(owner, worker):
            assert owner is manager and worker is managed
            value.events.append(("pipes_enter",))
            result = await original_pipes(owner, worker)
            value.pipe_calls += 1
            value.events.append(("pipes_done",))
            return result

        def warning(message, *args, **kwargs):
            value.warning_records.append((message, args, kwargs))
            value.events.append(("warning", message, args))
            if hostile and message in {DETAIL, EXIT}:
                raise RuntimeError(PRIVATE)

        monkeypatch.setattr(asyncio.subprocess, "Process", ObservedProcess)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(processes, "os", OperatingSystem())
        monkeypatch.setattr(processes, "open", observed_open, raising=False)
        monkeypatch.setattr(processes, "_pidfd_send", forbidden)
        monkeypatch.setattr(processes, "_adopt_member", forbidden)
        monkeypatch.setattr(processes, "signal", Signals())
        monkeypatch.setattr(WorkerManager, "_finalize_pipes", pipes)
        monkeypatch.setattr(processes.LOG, "warning", warning)
        opened.append(value)
        return value

    try:
        yield make
    finally:
        for value in reversed(opened):
            value.wait_release.set()
            if value.start is not None:
                await asyncio.wait_for(asyncio.gather(value.start, return_exceptions=True), 8)
            await asyncio.wait_for(value.manager.close(), 10)
            managed = value.manager._workers["oracle"]
            assert value.spawn is not None and value.spawn.done() and not value.spawn.cancelled()
            assert value.process is value.spawn.result()
            assert value.process.returncode is not None
            assert all(wait.outer_finished and wait.native.cr_frame is None for wait in value.waits)
            # Python3.12 wait_for can await close inline in this finalizer.
            # Completion of that native await does not finish its caller Task.
            assert all(
                wait.caller is asyncio.current_task() or wait.caller.done() for wait in value.waits
            )
            assert managed.pending_spawn is managed.pending_cleanup is None
            assert managed.pending_fds == [] and managed.pending_obj is None
            assert managed.drains == [] and managed.leader_fd is None
            assert managed.grant_w is None and not managed.granted
            for fd in value.fds:
                with pytest.raises(OSError):
                    os.fstat(fd)
            for number in (1, 2):
                pipe = value.process._transport.get_pipe_transport(number)
                assert pipe is None or pipe.is_closing()
            assert len(value.grant_closes) == 1 and not value.marker.exists()
            observations.append(
                {
                    "mode": value.mode,
                    "original_spawn_done": value.spawn.done(),
                    "original_process_exited": value.process.returncode is not None,
                    "original_waits_done": all(
                        wait.outer_finished and wait.native.cr_frame is None for wait in value.waits
                    ),
                    "cancelled_outer_waits": sum(wait.outer_cancelled for wait in value.waits),
                    "original_pipes_closed": True,
                    "original_grant_closed_once": len(value.grant_closes) == 1,
                    "original_control_fds_closed": True,
                    "configured_marker_absent": not value.marker.exists(),
                    "manager_closed": value.manager._closed,
                }
            )
        record_property("bootstrap_actual_cleanup", json.dumps(observations, sort_keys=True))


def observations(value):
    records = [record for record in value.warning_records if record[0] in {DETAIL, EXIT}]
    for message, args, kwargs in records:
        assert not kwargs and all(type(arg) is str for arg in args)
        text = message % args
        for secret in (PRIVATE, str(value.marker), str(value.process.pid)):
            assert secret not in text
        assert "Traceback" not in text
    exits = [args for message, args, _ in records if message == EXIT]
    assert len(exits) == 1
    assert all(
        category in {"not_reported", "exited_zero", "exited_nonzero", "terminated", "unavailable"}
        for category in exits[0]
    )
    return exits[0]


@pytest.mark.parametrize(
    "mode,expected_before,expected_after",
    [
        ("after-close", "not_reported", "exited_zero"),
        ("before-zero", "exited_zero", "exited_zero"),
        ("before-nonzero", "exited_nonzero", "exited_nonzero"),
        ("before-terminated", "terminated", "terminated"),
        ("cached-unavailable", "unavailable", "exited_zero"),
    ],
)
async def test_cached_original_status_distinguishes_before_grant_close_from_after_reap(
    refused_worker, mode, expected_before, expected_after
):
    value = await refused_worker(mode)
    value.start = asyncio.create_task(value.manager.start("oracle"))
    result = await asyncio.wait_for(asyncio.shield(value.start), 8)
    assert result["ok"] is False and result["error"] == "ownership-unverified"
    assert observations(value) == (expected_before, expected_after)
    assert not value.marker.exists() and len(value.grant_closes) == 1
    assert value.process.returncode is not None and value.spawn.done()
    grant = next(index for index, event in enumerate(value.events) if event[0] == "grant_close")
    assert value.events[grant - 1][0:2] == ("cached", True)
    assert value.events[grant - 1][3] == 0
    assert any(
        event[0] == "cached" and event[1] is False and event[3] == 1 for event in value.events
    )
    assert value.pipe_calls == 1
    expected_waits = 2 if mode.startswith("before-") else 1
    assert len(value.waits) == expected_waits
    assert all(
        wait.outer_finished
        and wait.outer_returned
        and not wait.outer_cancelled
        and wait.outer_error is None
        and wait.native.cr_frame is None
        for wait in value.waits
    )
    assert value.native_wait_results[-1] == value.process.returncode
    assert result["returncode"] == value.process.returncode
    if mode in {"after-close", "cached-unavailable"}:
        assert [args for message, args, _ in value.warning_records if message == COARSE] == [
            ("pre_command_mismatch", "not_reported")
        ]
        assert [args for message, args, _ in value.warning_records if message == DETAIL] == [
            ("empty_read",)
        ]
    else:
        assert not any(message == DETAIL for message, _, _ in value.warning_records)
    assert value.diagnostic_cached_failures == (mode == "cached-unavailable")


async def test_cancelled_original_reap_is_an_attempt_not_completed_wait_or_new_waiter(
    refused_worker,
):
    value = await refused_worker("cancel-reap")
    value.start = asyncio.create_task(value.manager.start("oracle"))
    try:
        await asyncio.wait_for(value.wait_entered.wait(), 5)
        original_wait = value.waits[0]
        assert not original_wait.outer_finished and value.native_wait_results == [0]
        assert original_wait.native_returned and original_wait.native_result == 0
        value.start.cancel()
        result = await asyncio.wait_for(asyncio.shield(value.start), 8)
        assert result["ok"] is False and result["error"] == "ownership-unverified"
        assert original_wait.outer_finished and original_wait.outer_cancelled
        assert not original_wait.outer_returned and original_wait.outer_error == "CancelledError"
        assert original_wait.native.cr_frame is None
        assert value.start.done() and not value.start.cancelled()
        assert len(value.waits) == 1, "Cached diagnostics cannot start an extra waiter"
        assert observations(value) == ("not_reported", "exited_zero")
        assert value.process.returncode == 0 and value.pipe_calls == 1
        assert len(value.grant_closes) == 1 and not value.marker.exists()
    finally:
        value.wait_release.set()


@pytest.mark.parametrize("mode", ["after-close", "before-nonzero"])
async def test_hostile_new_warning_sink_preserves_actual_refusal_and_original_cleanup(
    refused_worker, mode
):
    value = await refused_worker(mode, hostile=True)
    value.start = asyncio.create_task(value.manager.start("oracle"))
    result = await asyncio.wait_for(asyncio.shield(value.start), 8)
    assert result["ok"] is False and result["error"] == "ownership-unverified"
    assert not value.marker.exists() and len(value.grant_closes) == 1
    assert value.process.returncode is not None and value.pipe_calls == 1
    assert all(
        wait.outer_finished and wait.outer_returned and wait.native.cr_frame is None
        for wait in value.waits
    )
    assert len([record for record in value.warning_records if record[0] == EXIT]) == 1
    observations(value)
