"""Read-only Agent metadata over the existing retained bounded HTTP transport."""

import asyncio
import copy
import re

import httpx

from packages.contracts.client import JournalClient
from packages.contracts.context import context_json
from packages.contracts.task_context import (
    MAX_TASK_CONTEXT_REQUEST_BYTES,
    MAX_TASK_CONTEXT_RESPONSE_BYTES,
    TaskContextRequest,
    TaskContextResponse,
    TaskContextUnavailable,
)


class TaskContextClient:
    def __init__(self, url, token, *, transport=None):
        matched = (
            re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})/?", url) if type(url) is str else None
        )
        if matched is None or not 1 <= int(matched[1]) <= 65535:
            raise ValueError("Task context requires a literal loopback Agent endpoint")
        # Supplying the transport disables AsyncClient's environment proxy
        # mounts, so service credentials never follow HTTP_PROXY/ALL_PROXY.
        self._transport = JournalClient(
            url,
            token,
            transport=transport
            if transport is not None
            else httpx.AsyncHTTPTransport(trust_env=False),
        )
        self._http = self._transport.http
        self._binding = self._selection()
        self._close_task = None

    def _selection(self):
        http = self._transport.http
        return (id(http), str(http.base_url), tuple(http.headers.raw), id(http._transport))

    def current(self):
        return (
            self._transport.http is self._http
            and not self._transport._submit_closed
            and not self._transport._submit_cleanup_failed
            and self._selection() == self._binding
        )

    async def lookup(self, request):
        try:
            if not self.current():
                raise TaskContextUnavailable()
            proposal = TaskContextRequest.model_validate(
                copy.deepcopy(request.model_dump(mode="python")), strict=True
            )
            wire = proposal.model_dump_json().encode()
            if len(wire) > MAX_TASK_CONTEXT_REQUEST_BYTES:
                raise TaskContextUnavailable()
            async with asyncio.timeout(2.5):
                raw = await self._transport._owned_metadata(
                    "POST",
                    "/v1/agent/task-context",
                    json=proposal.model_dump(mode="json"),
                    maximum=MAX_TASK_CONTEXT_RESPONSE_BYTES,
                    raw=True,
                )
            value = context_json(raw, maximum=MAX_TASK_CONTEXT_RESPONSE_BYTES)
            if not self.current():
                raise TaskContextUnavailable()
            return TaskContextResponse.model_validate(value).validate_request(proposal)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TaskContextUnavailable() from None

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._transport.close())
        await asyncio.shield(self._close_task)
