"""Streaming HTTP over a Unix socket checked before any HTTP request bytes.

The verifier is trusted host code, never a response header or model claim. No
TCP, TLS, proxy or connection reuse is available through this transport. The
backend retains every socket until its real asynchronous close has joined.
"""

import asyncio
from contextlib import contextmanager

import httpcore
import httpx


class UnixCleanupError(RuntimeError):
    """An owned socket could not confirm closure; its owner must retain it."""


_CORE_ERRORS = tuple(
    getattr(httpcore, name)
    for name in (
        "TimeoutException",
        "NetworkError",
        "ProtocolError",
        "ProxyError",
        "UnsupportedProtocol",
    )
)


@contextmanager
def _http_errors():
    try:
        yield
    except _CORE_ERRORS as exc:
        # Use HTTPX's public exceptions so existing provider callers retain the
        # same transport/error boundary. Order specific subclasses first.
        for name in (
            "ConnectTimeout",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
            "ConnectError",
            "ReadError",
            "WriteError",
            "RemoteProtocolError",
            "LocalProtocolError",
            "UnsupportedProtocol",
            "ProxyError",
        ):
            if isinstance(exc, getattr(httpcore, name)):
                raise getattr(httpx, name)(str(exc)) from exc
        raise httpx.TransportError(str(exc)) from exc


class _SocketStream(httpcore.AsyncNetworkStream):
    def __init__(self, reader, writer, owner):
        self.reader, self.writer, self.owner = reader, writer, owner
        self.cleanup = None

    async def read(self, max_bytes, timeout=None):
        try:
            async with asyncio.timeout(timeout):
                return await self.reader.read(max_bytes)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout() from exc
        except OSError as exc:
            raise httpcore.ReadError() from exc

    async def write(self, buffer, timeout=None):
        try:
            async with asyncio.timeout(timeout):
                self.writer.write(buffer)
                await self.writer.drain()
        except TimeoutError as exc:
            raise httpcore.WriteTimeout() from exc
        except OSError as exc:
            raise httpcore.WriteError() from exc

    def get_extra_info(self, info):
        return self.writer.get_extra_info(info)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        raise httpcore.UnsupportedProtocol("Owned native HTTP requires a plain Unix socket")

    async def _close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception as exc:
            raise UnixCleanupError("Unix socket close is unconfirmed") from exc
        self.owner.streams.discard(self)

    async def aclose(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        interrupted = False
        while not self.cleanup.done():
            try:
                await asyncio.shield(self.cleanup)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                break
        self.cleanup.result()
        if interrupted:
            raise asyncio.CancelledError


class _UnixBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, path, verify_peer):
        self.path, self.verify_peer = str(path), verify_peer
        self.streams = set()
        self.openings = set()
        self.closed = False

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        raise httpcore.ConnectError("TCP is unavailable in owned native transport")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        if self.closed or path != self.path or socket_options:
            raise httpcore.ConnectError("Owned Unix transport is unavailable")
        opened = asyncio.get_running_loop().create_future()
        self.openings.add(opened)
        try:
            try:
                async with asyncio.timeout(timeout):
                    reader, writer = await asyncio.open_unix_connection(path)
            except TimeoutError as exc:
                raise httpcore.ConnectTimeout() from exc
            except OSError as exc:
                raise httpcore.ConnectError() from exc
            stream = _SocketStream(reader, writer, self)
            self.streams.add(stream)
            try:
                # Pool receives no stream until the kernel peer passes. HTTP
                # request bytes cannot be transmitted during verification.
                await self.verify_peer(writer.get_extra_info("socket"))
                if self.closed:
                    raise httpcore.ConnectError("Owned Unix transport closed during verification")
                return stream
            except BaseException:
                await stream.aclose()
                raise
        finally:
            opened.set_result(None)
            self.openings.discard(opened)

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)

    async def aclose(self):
        self.closed = True
        if self.openings:
            await asyncio.gather(*(asyncio.shield(item) for item in tuple(self.openings)))
        results = await asyncio.gather(
            *(stream.aclose() for stream in tuple(self.streams)), return_exceptions=True
        )
        if self.streams or any(isinstance(item, BaseException) for item in results):
            raise UnixCleanupError("Owned Unix HTTP cleanup is unconfirmed")


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, source):
        self.source = source

    async def __aiter__(self):
        with _http_errors():
            async for chunk in self.source:
                yield chunk

    async def aclose(self):
        with _http_errors():
            await self.source.aclose()


class OwnedUnixTransport(httpx.AsyncBaseTransport):
    """Public HTTPX/httpcore bridge with an awaited pre-request peer check."""

    def __init__(self, path, verify_peer):
        self.backend = _UnixBackend(path, verify_peer)
        self.pool = httpcore.AsyncConnectionPool(
            uds=str(path),
            network_backend=self.backend,
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
        )

    @property
    def cleanup_pending(self):
        return bool(self.backend.streams or self.backend.openings)

    async def handle_async_request(self, request):
        if (
            request.url.scheme != "http"
            or request.url.host != "localhost"
            or request.url.port not in {None, 80}
            or request.url.username
            or request.url.password
        ):
            raise httpx.UnsupportedProtocol("Owned Unix HTTP requires http://localhost")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _http_errors():
            response = await self.pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        try:
            with _http_errors():
                await self.pool.aclose()
        finally:
            await self.backend.aclose()
