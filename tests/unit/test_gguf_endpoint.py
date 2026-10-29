"""Production endpoint with synthetic leases and real HTTPX response ownership.

No socket is opened: the injected AsyncBaseTransport supplies public SSE bytes.
"""

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import httpx
import pytest

from packages.providers import owned_unix_http
from packages.providers.text import ProviderCleanupError, ProviderRefusal
from packages.resources.processes import WorkerIdentity
from services.resources import gguf


class Lease:
    """Synthetic observer: extinction is controlled independently of manager proof."""

    def __init__(self, identity, path):
        self.identity = identity
        self.outer_pid, self.start_time = identity.pid, identity.start_time
        self.socket_path = path
        self.native_pid, self.init_pid = 102, 101
        self.handles = {102: 1102, 101: 1101}
        self.extinction = True
        self.forgotten = False

    def current(self, identity):
        assert identity is self.identity and not self.forgotten

    async def extinct(self):
        return self.extinction

    def forget(self):
        self.forgotten = True


@pytest.fixture
def endpoint(tmp_path):
    directory = tmp_path / "native"
    directory.mkdir(mode=0o700)
    (directory / "ipc").mkdir(mode=0o700)
    value = gguf.GGUFEndpoint(
        directory,
        {
            "alias": "synthetic",
            "assets": [
                {
                    "guest": "/runtime/llama-server",
                    "bytes": 12,
                    "sha256": "a" * 64,
                }
            ],
        },
    )
    return value


def retain(endpoint, *, identity=None, phase="exec_pending"):
    identity = identity or WorkerIdentity("gguf", 100, 10)
    lease = Lease(identity, endpoint.ipc / "model.sock")
    endpoint.leases[identity.pid] = lease
    endpoint.ready = lease
    write_phase(endpoint, phase, identity.pid)
    return lease


def write_phase(endpoint, phase, pid=100):
    path = endpoint.directory / "phase.json"
    path.write_text(json.dumps({"version": 1, "pid": pid, "phase": phase}))
    path.chmod(0o600)


def stopped(pid=100, **proof):
    return {
        "ok": True,
        "stop_evidence": {
            "pid": pid,
            "reaped": True,
            "group_empty": True,
            "ownership_verified": True,
            **proof,
        },
    }


def data(content="hello", finish=None):
    return (
        b"data: "
        + json.dumps(
            {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}
        ).encode()
        + b"\n\n"
    )


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, *, hold_close=False, hold_eof=False, close_error=False):
        self.chunks = chunks
        self.closing, self.release_close = asyncio.Event(), asyncio.Event()
        self.at_eof, self.release_eof = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.close_error = close_error
        if not hold_close:
            self.release_close.set()
        if not hold_eof:
            self.release_eof.set()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        self.at_eof.set()
        await self.release_eof.wait()

    async def aclose(self):
        self.closing.set()
        await self.release_close.wait()
        if self.close_error:
            raise OSError("synthetic HTTP close failure")
        self.closed = True


def install_http(monkeypatch, body):
    transports, requests = [], []

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self, path, verify):
            self.cleanup_pending = False
            self.closed = False
            transports.append(self)

        async def handle_async_request(self, request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(gguf, "OwnedUnixTransport", Transport)
    return transports, requests


async def test_done_is_withheld_until_native_eof_and_actual_http_close(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body([data(), data("", "stop"), b"data: [DONE]\n\n"], hold_eof=True, hold_close=True)
    transports, requests = install_http(monkeypatch, body)
    async with aclosing(endpoint.stream([{"role": "user", "content": "public"}], lease)) as source:
        assert b"hello" in await anext(source)
        assert b'"stop"' in await anext(source)
        terminal = asyncio.create_task(anext(source))
        await body.at_eof.wait()
        assert not terminal.done() and endpoint.exchanges
        body.release_eof.set()
        await body.closing.wait()
        assert not terminal.done() and endpoint.exchanges
        body.release_close.set()
        assert await terminal == b"data: [DONE]\n\n"
        assert body.closed and transports[0].closed and not endpoint.exchanges
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "model": "synthetic",
        "messages": [{"role": "user", "content": "public"}],
        "max_tokens": 128,
        "stream": True,
    }


async def test_sse_reframing_cannot_exceed_public_wire_limit(endpoint, monkeypatch):
    lease = retain(endpoint)
    # Native SSE permits empty data fields inside a multiline JSON event.
    # Adding the optional space during forwarding expands this valid body.
    usage = b"data:\n" * 35 + b'data:{"choices":[],"usage":{"n":1}}\n\n'
    raw = usage * 4000 + data("", "stop") + b"data: [DONE]\n\n"
    assert len(raw) < 1024**2
    body = Body([raw])
    install_http(monkeypatch, body)
    published = []
    with pytest.raises(ValueError, match="public SSE"):
        async with aclosing(endpoint.stream([], lease)) as source:
            async for chunk in source:
                published.append(chunk)
    assert sum(map(len, published)) <= 1024**2
    assert all(b"[DONE]" not in chunk for chunk in published)
    assert body.closed and not endpoint.exchanges


async def test_duplicate_startup_phase_fields_are_unknown(endpoint):
    path = endpoint.directory / "phase.json"
    path.write_bytes(b'{"version":1,"pid":100,"pid":100,"phase":"pre_exec_failed"}')
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Duplicate"):
        await endpoint.release_after_stop(None, stopped())
    assert path.exists()


@pytest.mark.parametrize(
    "chunks",
    [
        [data(), data("", "stop")],
        [data(), data("", "stop"), b"data: [DONE]\n\n", data("late")],
        [data(), data("", "length"), b"data: [DONE]\n\n"],
    ],
)
async def test_missing_or_late_terminal_refuses_after_preserving_deltas_and_cleanup(
    endpoint, monkeypatch, chunks
):
    lease = retain(endpoint)
    body = Body(chunks)
    transports, requests = install_http(monkeypatch, body)
    captured = []
    with pytest.raises(ProviderRefusal):
        async for event in endpoint.stream([], lease):
            captured.append(event)
    assert b"hello" in captured[0]
    assert b"data: [DONE]\n\n" not in captured
    assert body.closed and transports[0].closed and not endpoint.exchanges
    assert len(requests) == 1


async def test_cancellation_waits_for_http_close_and_retains_failed_close(endpoint, monkeypatch):
    lease = retain(endpoint)
    # Exercise real HTTPX/httpcore/socket ownership without opening a socket.
    raw = data() + data("", "stop") + b"data: [DONE]\n\n"
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
        + str(len(raw)).encode()
        + b"\r\n\r\n"
        + raw
    )
    reader.feed_eof()
    closing, release = asyncio.Event(), asyncio.Event()
    writes = bytearray()

    class Writer:
        def write(self, value):
            writes.extend(value)

        async def drain(self):
            pass

        def get_extra_info(self, kind):
            return object() if kind == "socket" else None

        def close(self):
            closing.set()

        async def wait_closed(self):
            await release.wait()
            raise OSError("synthetic owned socket close failure")

    async def open_synthetic(path):
        assert path == str(lease.socket_path)
        return reader, Writer()

    monkeypatch.setattr(owned_unix_http.asyncio, "open_unix_connection", open_synthetic)
    monkeypatch.setattr(lease, "verify_peer", lambda peer: None, raising=False)

    async def consume():
        return [event async for event in endpoint.stream([], lease)]

    task = asyncio.create_task(consume())
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and endpoint.exchanges
    release.set()
    with pytest.raises((ProviderCleanupError, owned_unix_http.UnixCleanupError)):
        await task
    assert writes.startswith(b"POST /v1/chat/completions HTTP/1.1\r\n")
    # Manager proof does not repair a failed response close or forget the lease.
    assert not await endpoint.release_after_stop(lease.identity, stopped())
    assert endpoint.exchanges and endpoint.leases[100] is lease and not lease.forgotten


@pytest.mark.parametrize(
    "proof",
    [{"reaped": False}, {"group_empty": False}, {"ownership_verified": False}, {"pid": 999}],
)
async def test_release_requires_exact_manager_release_proof(endpoint, proof):
    lease = retain(endpoint)
    assert not await endpoint.release_after_stop(lease.identity, stopped(**proof))
    assert endpoint.ready is lease and not lease.forgotten


async def test_release_waits_for_http_close_before_forgetting_observers(endpoint, monkeypatch):
    lease = retain(endpoint)
    body = Body([], hold_close=True)
    transports, _ = install_http(monkeypatch, body)
    context = endpoint.connection(lease)
    await context.__aenter__()
    exchange = next(iter(endpoint.exchanges))
    close_entered = asyncio.Event()
    original_close = exchange.client.aclose

    async def held_client_close():
        close_entered.set()
        await body.release_close.wait()
        await original_close()

    monkeypatch.setattr(exchange.client, "aclose", held_client_close)
    release = asyncio.create_task(endpoint.release_after_stop(lease.identity, stopped()))
    await close_entered.wait()
    assert not release.done() and not lease.forgotten and endpoint.leases
    body.release_close.set()
    assert await release
    assert (
        lease.forgotten and not endpoint.leases and not endpoint.exchanges and transports[0].closed
    )
    await context.__aexit__(None, None, None)


async def test_cancelled_release_retains_exchange_until_close_then_allows_reconciliation(
    endpoint, monkeypatch
):
    lease = retain(endpoint)
    body = Body([])
    install_http(monkeypatch, body)
    context = endpoint.connection(lease)
    await context.__aenter__()
    exchange = next(iter(endpoint.exchanges))
    closing, close_done = asyncio.Event(), asyncio.Event()
    original_close = exchange.client.aclose

    async def held_close():
        closing.set()
        await close_done.wait()
        await original_close()

    monkeypatch.setattr(exchange.client, "aclose", held_close)
    release = asyncio.create_task(endpoint.release_after_stop(lease.identity, stopped()))
    await closing.wait()
    release.cancel()
    await asyncio.sleep(0)
    release.cancel()
    await asyncio.sleep(0)
    assert not release.done() and endpoint.exchanges and not lease.forgotten
    close_done.set()
    with pytest.raises(asyncio.CancelledError):
        await release
    assert endpoint.exchanges and endpoint.leases and not lease.forgotten
    assert await endpoint.release_after_stop(lease.identity, stopped())
    assert not endpoint.exchanges and lease.forgotten
    await context.__aexit__(None, None, None)


async def test_concurrent_release_reconciles_one_exact_lease_once(endpoint, monkeypatch):
    lease = retain(endpoint)
    forgotten = []
    original = lease.forget

    def forget():
        forgotten.append(lease)
        original()

    monkeypatch.setattr(lease, "forget", forget)
    assert await asyncio.gather(
        *(endpoint.release_after_stop(lease.identity, stopped()) for _ in range(2))
    ) == [True, True]
    assert forgotten == [lease] and not endpoint.leases


async def test_exact_identity_object_required_to_release_current_lease(endpoint):
    lease = retain(endpoint)
    impostor = WorkerIdentity("gguf", 100, 10)
    assert not await endpoint.release_after_stop(impostor, stopped())
    assert endpoint.ready is lease and not lease.forgotten


async def test_released_identity_replay_cannot_release_new_incarnation(endpoint):
    old = retain(endpoint)
    assert await endpoint.release_after_stop(old.identity, stopped())
    new = retain(endpoint, identity=WorkerIdentity("gguf", 100, 20))
    assert await endpoint.release_after_stop(old.identity, stopped())
    assert endpoint.ready is new and not new.forgotten


@pytest.mark.parametrize("phase", ["preparing", "exec_pending"])
@pytest.mark.parametrize("with_pid", [False, True])
async def test_unknown_startup_without_lease_is_not_release_proof(endpoint, phase, with_pid):
    write_phase(endpoint, phase)
    result = stopped() if with_pid else {"ok": False, "stop_evidence": {}}
    assert not await endpoint.release_after_stop(None, result)
    assert (endpoint.directory / "phase.json").exists()


async def test_no_lease_release_needs_empty_ipc_and_absent_phase(endpoint):
    assert await endpoint.release_after_stop(None, {"ok": True})
    (endpoint.ipc / "unknown").write_text("synthetic retained IPC")
    assert not await endpoint.release_after_stop(None, {"ok": True})


@pytest.mark.parametrize("result", [{}, {"ok": False}])
async def test_absent_phase_does_not_override_failed_manager_release(endpoint, result):
    assert not await endpoint.release_after_stop(None, result)


@pytest.mark.parametrize("invalid", ["failed", "unowned", "missing_ownership", "missing_pid"])
async def test_no_lease_pre_exec_failure_needs_exact_successful_manager_proof(endpoint, invalid):
    write_phase(endpoint, "pre_exec_failed")
    result = stopped()
    if invalid == "failed":
        result["ok"] = False
    elif invalid == "unowned":
        result["stop_evidence"]["ownership_verified"] = False
    elif invalid == "missing_ownership":
        del result["stop_evidence"]["ownership_verified"]
    else:
        del result["stop_evidence"]["pid"]
    assert not await endpoint.release_after_stop(None, result)
    assert (endpoint.directory / "phase.json").exists()


async def test_no_lease_confirmed_pre_exec_failure_prunes_only_exact_phase(endpoint):
    write_phase(endpoint, "pre_exec_failed")
    assert await endpoint.release_after_stop(None, stopped())
    assert not (endpoint.directory / "phase.json").exists()
    endpoint.close()
    assert not endpoint.directory.exists()


async def test_dangling_phase_is_unknown_not_never_launched(endpoint):
    (endpoint.directory / "phase.json").symlink_to(endpoint.directory / "absent")
    with pytest.raises(OSError):
        await endpoint.release_after_stop(None, stopped())
    assert (endpoint.directory / "phase.json").is_symlink()


async def test_partial_exec_pending_lease_stays_retained_even_when_outer_group_is_empty(endpoint):
    lease = retain(endpoint)
    lease.native_pid = lease.init_pid = None
    assert not await endpoint.release_after_stop(lease.identity, stopped())
    assert not lease.forgotten and endpoint.leases and (endpoint.directory / "phase.json").exists()


async def test_failed_initial_group_scan_retains_partial_startup_for_reconciliation(
    endpoint, monkeypatch
):
    write_phase(endpoint, "exec_pending")
    monkeypatch.setattr(gguf, "_proc_stat_full", lambda pid: ("S", 1, 100, 100, 10))
    monkeypatch.setattr(gguf, "_scan_group", lambda pid: ({}, False))
    assert not await endpoint.probe(None, SimpleNamespace(pid=100, returncode=None))
    lease = endpoint.leases[100]
    assert lease.native_pid is None and not lease.handles
    assert not await endpoint.release_after_stop(None, stopped())
    assert endpoint.leases[100] is lease


@pytest.mark.parametrize("fd_pid", [None, 101, -1])
async def test_partial_pre_exec_failure_requires_all_original_observers_extinct(
    endpoint, monkeypatch, fd_pid
):
    from packages.resources import processes

    lease = retain(endpoint, phase="pre_exec_failed")
    lease.native_pid = lease.init_pid = None
    monkeypatch.setattr(processes, "_pidfd_pid", lambda fd: fd_pid)
    released = await endpoint.release_after_stop(lease.identity, stopped())
    assert released is (fd_pid == -1)
    assert lease.forgotten is released
    assert bool(endpoint.leases) is not released


async def test_extant_native_identity_outlives_outer_release_proof(endpoint):
    lease = retain(endpoint)
    lease.extinction = False
    assert not await endpoint.release_after_stop(lease.identity, stopped())
    assert not lease.forgotten and endpoint.ready is lease
