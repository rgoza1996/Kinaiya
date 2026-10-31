"""Request-local complete admissions at HTTPX's final transport boundary.

HTTPX 0.28 dispatches request hooks before selecting its transport. Keeping the
check at that selection/forwarding boundary preserves client hooks and pooling
without temporarily changing a shared client's configuration. Unbound requests
retain the ordinary client behavior.
"""

import asyncio
import copy
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import httpx

from packages.providers.prompt_budget import PromptCountUnavailable


@dataclass(frozen=True, slots=True)
class _FrozenBody(httpx.AsyncByteStream):
    body: bytes

    async def __aiter__(self):
        yield self.body


@dataclass(frozen=True)
class _Boundary:
    client: httpx.AsyncClient
    owner: object
    check: Callable[[], None]
    own_stream: Callable[[httpx.AsyncByteStream], httpx.AsyncByteStream]
    transport: httpx.AsyncBaseTransport
    request: httpx.Request
    method: str
    url: httpx.URL
    headers: tuple
    body: bytes
    stream: _FrozenBody
    extensions: dict

    def current(self, transport, request):
        self.check()
        if (
            asyncio.current_task() is not self.owner
            or transport is not self.transport
            or request is not self.request
            or request.method != self.method
            or request.url != self.url
            or tuple(request.headers.raw) != self.headers
            or request.content != self.body
            or request.stream is not self.stream
            or self.stream.body != self.body
            or request.extensions != self.extensions
        ):
            raise PromptCountUnavailable("Meta complete HTTP request binding changed")


_request_boundary = ContextVar("complete_http_request_boundary", default=None)


class _ForwardingTransport(httpx.AsyncBaseTransport):
    def __init__(self, boundary, transport):
        self.boundary, self.transport = boundary, transport

    async def handle_async_request(self, request):
        # No await separates the synchronous native fences and the real
        # transport call. Its connection pool/stream lifetime remains owned by
        # the original client, not this per-request forwarding object.
        self.boundary.current(self.transport, request)
        response = await self.transport.handle_async_request(request)
        # HTTPX runs response hooks before returning to the caller. Establish
        # close ownership before one of those hooks can await or raise.
        response.stream = self.boundary.own_stream(response.stream)
        return response


class RequestBoundAsyncClient(httpx.AsyncClient):
    def _transport_for_url(self, url):
        transport = super()._transport_for_url(url)
        boundary = _request_boundary.get()
        if boundary is None or boundary.client is not self:
            return transport
        return _ForwardingTransport(boundary, transport)


@asynccontextmanager
async def complete_stream(client, path, body, check, own_stream):
    """Send one frozen request; reject mutation after auth/request-hook awaits."""
    if not isinstance(client, RequestBoundAsyncClient):
        raise PromptCountUnavailable("Meta complete HTTP client is unsupported")
    check()
    request = client.build_request("POST", path, json=body, headers={"Accept-Encoding": "identity"})
    # The cached request.content alone is insufficient: a hook could replace
    # its streaming body while leaving that cache intact. The dispatched body
    # must remain this exact native, immutable byte stream.
    request.stream = _FrozenBody(request.content)
    boundary = _Boundary(
        client,
        asyncio.current_task(),
        check,
        own_stream,
        # Select the real transport before installing this request's binding.
        httpx.AsyncClient._transport_for_url(client, request.url),
        request,
        request.method,
        request.url,
        tuple(request.headers.raw),
        request.content,
        request.stream,
        copy.deepcopy(request.extensions),
    )
    token = _request_boundary.set(boundary)
    try:
        response = await client.send(request, stream=True)
        try:
            yield response
        finally:
            await response.aclose()
    finally:
        _request_boundary.reset(token)
