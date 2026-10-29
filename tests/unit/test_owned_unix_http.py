import asyncio
import os
import socket
import struct
from contextlib import asynccontextmanager

import httpx
import pytest

from packages.providers.owned_unix_http import OwnedUnixTransport, UnixCleanupError, _SocketStream


@asynccontextmanager
async def listener(tmp_path, handler):
    tasks = set()
    failures = []

    async def handle(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            await handler(reader, writer)
        except Exception as exc:
            failures.append(exc)
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    path = tmp_path / "native.sock"
    server = await asyncio.start_unix_server(handle, path=path)
    try:
        yield path
    finally:
        server.close()
        await server.wait_closed()
        if tasks:
            async with asyncio.timeout(2):
                await asyncio.gather(*tasks)
        assert not failures


async def test_verifier_runs_before_request_bytes(tmp_path):
    accepted, release, ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    received = bytearray()

    async def handle(reader, writer):
        accepted.set()
        received.extend(await reader.read())
        ended.set()

    async def verify(peer):
        pid, uid, _ = struct.unpack(
            "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        assert (pid, uid) == (os.getpid(), os.geteuid())
        await release.wait()
        raise ValueError("synthetic foreign incarnation")

    async with listener(tmp_path, handle) as path:
        transport = OwnedUnixTransport(path, verify)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            request = asyncio.create_task(
                client.post("http://localhost/v1/chat/completions", json={"synthetic": "private"})
            )
            await accepted.wait()
            await asyncio.sleep(0)
            assert not request.done() and not received
            release.set()
            with pytest.raises(ValueError, match="foreign incarnation"):
                await request
            await ended.wait()
            assert received == b""
        assert not transport.cleanup_pending


async def test_cancellation_during_peer_check_closes_without_http(tmp_path):
    checking, ended = asyncio.Event(), asyncio.Event()
    observed = []

    async def handle(reader, writer):
        observed.append(await reader.read())
        ended.set()

    async def verify(peer):
        checking.set()
        await asyncio.Event().wait()

    async with listener(tmp_path, handle) as path:
        transport = OwnedUnixTransport(path, verify)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            request = asyncio.create_task(
                client.post("http://localhost/v1/chat/completions", json={"synthetic": "content"})
            )
            await checking.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await ended.wait()
            assert observed == [b""]
        assert not transport.cleanup_pending


async def test_real_http_stream_delivers_before_native_eof_and_rechecks_each_connection(tmp_path):
    finish, first_sent = asyncio.Event(), asyncio.Event()
    checks, requests = [], []

    async def handle(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/event-stream\r\n\r\n3\r\none\r\n"
        )
        await writer.drain()
        first_sent.set()
        await finish.wait()
        writer.write(b"3\r\ntwo\r\n0\r\n\r\n")
        await writer.drain()

    async def verify(peer):
        checks.append(peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))

    async with listener(tmp_path, handle) as path:
        transport = OwnedUnixTransport(path, verify)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            async with client.stream("GET", "http://localhost/health") as response:
                source = response.aiter_bytes()
                await first_sent.wait()
                async with asyncio.timeout(1):
                    assert await anext(source) == b"one"
                assert not finish.is_set()
                finish.set()
                assert b"".join([part async for part in source]) == b"two"
            assert not transport.cleanup_pending
            assert (await client.get("http://localhost/health")).content == b"onetwo"
            assert len(checks) == len(requests) == 2
        assert not transport.cleanup_pending


async def test_native_eof_before_declared_body_is_transport_failure(tmp_path):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nshort")
        await writer.drain()

    async def verify(peer):
        pass

    async with listener(tmp_path, handle) as path:
        transport = OwnedUnixTransport(path, verify)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.get("http://localhost/health")
        assert not transport.cleanup_pending


async def test_socket_close_is_retained_through_repeated_cancellation():
    closing, released = asyncio.Event(), asyncio.Event()

    class Writer:
        def close(self):
            closing.set()

        async def wait_closed(self):
            await released.wait()

    class Owner:
        streams = set()

    owner = Owner()
    stream = _SocketStream(None, Writer(), owner)
    owner.streams.add(stream)
    close = asyncio.create_task(stream.aclose())
    await closing.wait()
    close.cancel()
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.sleep(0)
    assert not close.done() and stream in owner.streams
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert not owner.streams


async def test_socket_close_failure_keeps_owned_socket():
    class Writer:
        def close(self):
            pass

        async def wait_closed(self):
            raise OSError("synthetic held close failure")

    class Owner:
        streams = set()

    owner = Owner()
    stream = _SocketStream(None, Writer(), owner)
    owner.streams.add(stream)
    with pytest.raises(UnixCleanupError):
        await stream.aclose()
    assert stream in owner.streams


@pytest.mark.parametrize(
    "url",
    ["https://localhost/", "http://127.0.0.1/", "http://localhost:99/", "http://user@localhost/"],
)
async def test_transport_cannot_dial_other_origin(tmp_path, url):
    async def verify(peer):
        pytest.fail("No socket should be opened")

    transport = OwnedUnixTransport(tmp_path / "absent.sock", verify)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.UnsupportedProtocol):
            await client.get(url)
    assert not transport.cleanup_pending
