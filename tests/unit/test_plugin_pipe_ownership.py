"""Codex #226: actual child reaping and owned stdout/stderr pipe cleanup."""

import errno
import os
import select
import signal
import subprocess
import sys
import threading

import pytest

from core.plugins import sandbox
from core.plugins.errors import PluginCandidateFailure


@pytest.fixture
def child():
    processes = []

    def start(source):
        # Trusted synthetic control children exercise pipe ownership directly;
        # plugin execution still requires the existing Bubblewrap boundary.
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-u", "-c", source],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        processes.append(process)
        assert os.getpgid(process.pid) == process.pid
        return process

    try:
        yield start
    finally:
        # The red baseline intentionally leaves pipes (and setup-fault children)
        # alive. This fixture owns and explicitly cleans those actual resources.
        for process in processes:
            try:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=5)
            finally:
                try:
                    process.stdout.close()
                finally:
                    process.stderr.close()


def assert_cleaned(process, streams):
    assert (process.stdout, process.stderr) == streams
    assert process.returncode is not None, "actual child was not reaped before settlement"
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)
    assert all(stream.closed for stream in streams), "owned stdout/stderr remain open"


def test_success_captures_each_pipe_at_its_byte_limit_and_closes_after_reap(child):
    process = child("import os; os.write(1, b'o' * 128); os.write(2, b'e' * 128)")
    streams = process.stdout, process.stderr
    result = sandbox._communicate_bounded(process, limit_bytes=128, timeout_s=3)
    assert result == (b"o" * 128, b"e" * 128)
    assert process.returncode == 0
    assert_cleaned(process, streams)


def test_already_cancelled_kills_reaps_and_closes_both_actual_pipes(child):
    process = child("import time; time.sleep(60)")
    streams = process.stdout, process.stderr
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PluginCandidateFailure, match="cancelled before start"):
        sandbox._communicate_bounded(process, limit_bytes=128, timeout_s=3, cancel_event=cancel)
    assert process.returncode == -signal.SIGKILL
    assert_cleaned(process, streams)


def test_cancellation_after_actual_stdout_read_reaps_and_closes(child, monkeypatch):
    process = child("import os, time; os.write(1, b'ready'); time.sleep(60)")
    streams = process.stdout, process.stderr
    stdout_fd = process.stdout.fileno()
    cancel = threading.Event()
    actual_read = os.read
    observed = []

    def cancel_after_read(fd, size):
        data = actual_read(fd, size)
        if fd == stdout_fd and data:
            observed.append(data)
            cancel.set()
        return data

    monkeypatch.setattr(sandbox.os, "read", cancel_after_read)
    with pytest.raises(PluginCandidateFailure, match="cancelled; group killed"):
        sandbox._communicate_bounded(process, limit_bytes=128, timeout_s=3, cancel_event=cancel)
    assert observed == [b"ready"]
    assert process.returncode == -signal.SIGKILL
    assert_cleaned(process, streams)


@pytest.mark.parametrize("eof_before_exit", [False, True], ids=["open-pipes", "pipe-eof"])
def test_deadline_reaps_and_closes_even_when_eof_precedes_exit(child, eof_before_exit):
    source = "import os, time; "
    if eof_before_exit:
        source += "os.close(1); os.close(2); "
    process = child(source + "time.sleep(60)")
    streams = process.stdout, process.stderr
    if eof_before_exit:
        for stream in streams:
            readable, _, _ = select.select([stream], [], [], 3)
            assert readable and os.read(stream.fileno(), 1) == b""
        assert process.returncode is None
    with pytest.raises(PluginCandidateFailure, match="wall timeout"):
        sandbox._communicate_bounded(process, limit_bytes=128, timeout_s=0.2)
    assert process.returncode == -signal.SIGKILL
    assert_cleaned(process, streams)


@pytest.mark.parametrize("fd", [1, 2], ids=["stdout", "stderr"])
def test_incremental_overflow_kills_reaps_and_closes(child, fd):
    process = child(f"import os, time; os.write({fd}, b'x' * 4096); time.sleep(60)")
    streams = process.stdout, process.stderr
    with pytest.raises(PluginCandidateFailure, match="output exceeded 128 bytes"):
        sandbox._communicate_bounded(process, limit_bytes=128, timeout_s=3)
    assert process.returncode == -signal.SIGKILL
    assert_cleaned(process, streams)


@pytest.mark.parametrize("pipe_index", [0, 1], ids=["first-pipe", "second-pipe"])
def test_nonblocking_setup_failure_reaps_before_closing_actual_pipes(
    child, monkeypatch, pipe_index
):
    process = child("import time; time.sleep(60)")
    streams = process.stdout, process.stderr
    pipe_fds = tuple(stream.fileno() for stream in streams)
    target_fd = pipe_fds[pipe_index]
    actual_set_blocking = os.set_blocking
    configured = []

    def fail_real_pipe(fd, blocking):
        assert fd in pipe_fds
        if fd == target_fd:
            raise OSError(errno.EIO, "synthetic nonblocking pipe setup failure")
        actual_set_blocking(fd, blocking)
        configured.append(fd)

    monkeypatch.setattr(sandbox.os, "set_blocking", fail_real_pipe)
    with pytest.raises(OSError, match="synthetic nonblocking pipe setup failure"):
        sandbox._communicate_bounded(process, limit_bytes=128, timeout_s=3)
    assert configured == ([pipe_fds[0]] if pipe_index else [])
    assert_cleaned(process, streams)
