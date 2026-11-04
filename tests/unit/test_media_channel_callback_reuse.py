"""An original media callback cannot remove a new reader on a reused FD.

Only the selected pool/channel API and standard event-loop/socket seams are
observed. A's production implementation remains unread during authorship.
"""

import asyncio
import fcntl
import os
import socket

import pytest
from test_media_processes import _WAIT, _binding, _Harness, _line


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    fixture = _Harness(tmp_path, monkeypatch)
    try:
        yield fixture
    finally:
        await fixture.close()


async def test_old_control_callback_preserves_new_reader_on_exact_reused_socket_fd(
    harness, monkeypatch
):
    pool, _ = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    _, grant = harness.grant(launch, binding)
    child = await pool.start(launch, grant)
    await _line(child.channel)
    assert len(harness.processes) == 1
    original_process = harness.processes[0]

    loop = asyncio.get_running_loop()
    native_add_reader = loop._add_reader
    queued = []
    registered = asyncio.Event()

    def observe_reader(fd, callback, *args):
        queued.append((fd, callback, args))
        result = native_add_reader(fd, callback, *args)
        registered.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(loop, "_add_reader", observe_reader)
        original_read = asyncio.create_task(child.channel.recv_chunk(1))
        await asyncio.wait_for(registered.wait(), _WAIT)
    assert queued, "the original native reader registration was not observed"
    old_fds = {fd for fd, _, _ in queued}
    assert len(old_fds) == 1, "the fixture must identify the original control reader exactly"
    old_fd = old_fds.pop()

    pool.fence(launch)
    assert (await pool.settle(launch)).state == "settled"
    assert original_read.done(), "positive settlement retained original control I/O"
    await asyncio.gather(original_read, return_exceptions=True)
    assert await asyncio.wait_for(original_process.wait(), _WAIT) is not None

    sockets = []
    duplicate_fds = []
    reader_fd = None
    try:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        sockets.extend((left, right))
        left.setblocking(False)
        right.setblocking(False)
        if left.fileno() == old_fd:
            receiver, peer = left, right
        elif right.fileno() == old_fd:
            receiver, peer = right, left
        else:
            candidate = fcntl.fcntl(left.fileno(), fcntl.F_DUPFD_CLOEXEC, old_fd)
            duplicate_fds.append(candidate)
            assert candidate == old_fd, "the retired control FD was not available for exact reuse"
            receiver = socket.socket(fileno=candidate)
            sockets.append(receiver)
            duplicate_fds.remove(candidate)
            peer = right
            left.close()
        assert receiver.fileno() == old_fd
        receiver.setblocking(False)

        delivered = asyncio.Event()
        payload = b"new owner reader remains registered"
        received = bytearray()
        read_errors = []

        def receive_new_owner_bytes():
            try:
                part = receiver.recv(65536)
            except BlockingIOError:
                return
            except OSError as error:
                read_errors.append(error)
                delivered.set()
            else:
                received.extend(part)
                if not part or len(received) >= len(payload):
                    delivered.set()

        reader_fd = receiver.fileno()
        loop.add_reader(reader_fd, receive_new_owner_bytes)
        for fd, callback, args in queued:
            assert fd == reader_fd
            callback(*args)
        await asyncio.wait_for(loop.sock_sendall(peer, payload), _WAIT)
        await asyncio.wait_for(delivered.wait(), _WAIT)
        assert not read_errors
        assert bytes(received) == payload
        assert len(harness.processes) == 1
        assert original_process.returncode is not None
    finally:
        try:
            if reader_fd is not None:
                loop.remove_reader(reader_fd)
        finally:
            close_errors = []
            for fd in duplicate_fds:
                try:
                    os.close(fd)
                except OSError as error:
                    close_errors.append(error)
            for owned_socket in reversed(sockets):
                try:
                    owned_socket.close()
                except OSError as error:
                    close_errors.append(error)
            if close_errors:
                raise close_errors[0]
