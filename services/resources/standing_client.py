"""Agent-role fixed Resources metadata/barrier RPCs with actual HTTP ownership."""

import asyncio

import httpx

from packages.contracts.standing_client import _StandingExchange
from packages.contracts.standing_execution import StandingExecutionClaim
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    checked_finalization,
    finalization_bytes,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from services.resources.client import local_base


class StandingResourceClient:
    actual_ownership_supported = True

    def __init__(self, url, token, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=local_base(url),
            headers={"Authorization": "Bearer " + token},
            timeout=None,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._submit_exchanges = set()
        self._submit_cleanup_failed = False
        self._submit_closed = False
        self._close_task = None

    def require_available(self):
        if self._submit_closed or self._submit_cleanup_failed or self.http.is_closed:
            raise StandingUnavailable

    async def _settle_ready(self, deadline):
        # A positive sole-close task may finish one turn before its work task
        # and original registry callbacks. Join those exact owners before a
        # new admission, rather than inferring release from response flags.
        ready = {
            exchange.task
            for exchange in self._submit_exchanges
            if exchange.close_task is not None and exchange.close_task.done()
        }
        if ready:
            _, pending = await asyncio.wait(
                ready, timeout=max(0, deadline - asyncio.get_running_loop().time())
            )
            if pending:
                raise StandingUnavailable

    async def _exchange(self, suffix, original, kind, result_type, *, nullable=False):
        exchange = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 25
        try:
            await self._settle_ready(deadline)
            self.require_available()
            if len(self._submit_exchanges) >= 64:
                raise StandingUnavailable
            original = checked_finalization(original, kind)
            wire = finalization_bytes(original)
            maximum = 147456
            if len(wire) > maximum:
                raise StandingUnavailable
            request = self.http.build_request(
                "POST",
                "/v1/resources/standing/" + suffix,
                content=wire,
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
                timeout=None,
            )
            if "x-kinaiya-standing-capability" in request.headers:
                raise StandingUnavailable
            self.require_available()
            # Binding uses a Claim, not a caller-selected BindingRequest.
            exchange = _StandingExchange(
                self, request, None, result_type, deadline, allow_null=nullable
            )
            done, _ = await asyncio.wait({exchange.task}, timeout=max(0, deadline - loop.time()))
            if not done or exchange.abandoned or loop.time() >= deadline:
                raise StandingUnavailable
            result = exchange.task.result()
            self.require_available()
            if result is not None:
                result = (
                    result.validate_claim(original)
                    if kind is StandingExecutionClaim
                    else result.validate_request(original)
                )
            return result
        except asyncio.CancelledError:
            if exchange is not None:
                exchange.abandon()
            if asyncio.current_task().cancelling():
                raise
            raise StandingUnavailable from None
        except StandingConflict:
            raise StandingConflict from None
        except Exception:
            if exchange is not None:
                exchange.abandon()
            raise StandingUnavailable from None

    async def bind_standing_claim(self, claim):
        return await self._exchange(
            "binding", claim, StandingExecutionClaim, StandingResourceBindingReceipt
        )

    async def lookup_standing_binding(self, claim):
        return await self._exchange(
            "binding/lookup",
            claim,
            StandingExecutionClaim,
            StandingResourceBindingReceipt,
            nullable=True,
        )

    async def seal_standing_claim(self, request):
        return await self._exchange(
            "seal", request, StandingResourceFinalizeRequest, StandingResourceFinalizeContext
        )

    async def lookup_standing_seal(self, request):
        return await self._exchange(
            "seal/lookup",
            request,
            StandingResourceFinalizeRequest,
            StandingResourceFinalizeContext,
            nullable=True,
        )

    async def _close_owned(self):
        # Original exchanges remain the sole response-close owners. Retiring
        # the transport before they settle would abandon a late acquisition.
        while self._submit_exchanges:
            pending = {
                task
                for exchange in self._submit_exchanges
                for task in (exchange.task, exchange.close_task)
                if task is not None and not task.done()
            }
            if pending:
                await asyncio.wait(pending)
            else:
                await asyncio.sleep(0)
        try:
            await self.http.aclose()
        except BaseException:
            self._submit_cleanup_failed = True
            raise
        # Capability poison remains sticky, but positive transport retirement
        # is cleanup, never a new binding/drainage or physical certificate.

    async def close(self):
        self._submit_closed = True
        for exchange in tuple(self._submit_exchanges):
            exchange.abandon()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
            self._close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        done, _ = await asyncio.wait({self._close_task}, timeout=1)
        if not done:
            raise StandingUnavailable
        return self._close_task.result()
