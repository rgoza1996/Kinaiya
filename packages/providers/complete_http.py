"""Request-local complete admissions at HTTPX's final transport boundary.

HTTPX 0.28 dispatches request hooks before selecting its transport. Keeping the
check at that selection/forwarding boundary preserves client hooks and pooling
without temporarily changing a shared client's configuration. Unbound requests
retain the ordinary client behavior.
"""

import asyncio
import copy
import hashlib
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
    admission: object
    purpose: str
    sent: list[bool]

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
_decision_boundary = ContextVar("decisions_final_transport_boundary", default=None)


def _enter_decision(client, transport, request):
    bound = _decision_boundary.get()
    if bound is None or bound.client is not client:
        return
    bound.current(transport, request)
    from packages.providers.decision_capture import _capture

    capture = _capture.get()
    if capture is None or capture.admission is None:
        raise PromptCountUnavailable("Decisions original admission scope changed")
    capture.admission.enter(client, request.content)


class _DecisionTransport(httpx.AsyncBaseTransport):
    def __init__(self, client, transport):
        self.client, self.transport = client, transport

    async def handle_async_request(self, request):
        _enter_decision(self.client, self.transport, request)
        response = await self.transport.handle_async_request(request)
        from packages.providers.text import _OwnedHTTPStream

        # Enroll the original close before an awaited response hook can fail.
        response.stream = _OwnedHTTPStream(response.stream)
        return response


class _ForwardingTransport(httpx.AsyncBaseTransport):
    def __init__(self, boundary, transport):
        self.boundary, self.transport = boundary, transport

    async def handle_async_request(self, request):
        # No await separates the synchronous native fences and the real
        # transport call. Its connection pool/stream lifetime remains owned by
        # the original client, not this per-request forwarding object.
        self.boundary.current(self.transport, request)
        if self.boundary.sent[0]:
            raise PromptCountUnavailable("Meta complete HTTP dispatch is consumed")
        self.boundary.sent[0] = True
        if self.boundary.admission is not None:
            # Native authority is read atomically after awaited HTTP hooks.
            # Its admission is the linearization point; a later revocation
            # cannot recall an already admitted in-flight disclosure.
            await self.boundary.admission(
                self.boundary.purpose, hashlib.sha256(self.boundary.body).hexdigest()
            )
            self.boundary.current(self.transport, request)
        _enter_decision(self.boundary.client, self.transport, request)
        response = await self.transport.handle_async_request(request)
        # HTTPX runs response hooks before returning to the caller. Establish
        # close ownership before one of those hooks can await or raise.
        response.stream = self.boundary.own_stream(response.stream)
        return response


class RequestBoundAsyncClient(httpx.AsyncClient):
    async def send(self, request, **kwargs):
        from packages.providers.decision_capture import _capture

        capture = _capture.get()
        native = capture.admission if capture is not None else None
        if native is None or native.client is not self:
            return await super().send(request, **kwargs)
        native.check_transport(self, request.content)
        request.stream = _FrozenBody(request.content)
        bound = _Boundary(
            self,
            asyncio.current_task(),
            lambda: native.check_transport(self, request.content),
            lambda stream: stream,
            httpx.AsyncClient._transport_for_url(self, request.url),
            request,
            request.method,
            request.url,
            tuple(request.headers.raw),
            request.content,
            request.stream,
            copy.deepcopy(request.extensions),
            None,
            "decisions",
            [False],
        )
        token = _decision_boundary.set(bound)
        try:
            return await super().send(request, **kwargs)
        finally:
            _decision_boundary.reset(token)

    def _transport_for_url(self, url):
        transport = super()._transport_for_url(url)
        boundary = _request_boundary.get()
        if boundary is None or boundary.client is not self:
            decision = _decision_boundary.get()
            if decision is not None and decision.client is self:
                return _DecisionTransport(self, transport)
            return transport
        return _ForwardingTransport(boundary, transport)


@asynccontextmanager
async def complete_stream(client, path, body, check, own_stream, *, admission=None, purpose=""):
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
        admission,
        purpose,
        [False],
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
