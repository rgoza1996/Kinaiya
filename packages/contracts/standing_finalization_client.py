"""New-family packets on the journal's existing actual exchange ownership."""

import asyncio

from packages.contracts.standing_client import _StandingExchange
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceBindingRequest,
    StandingResourceFinalization,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    checked_finalization,
    finalization_bytes,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable


async def _exchange(owner, path, original, request_type, result_type, *, nullable=False):
    exchange = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 25

    def available():
        if (
            owner._submit_closed
            or owner._submit_cleanup_failed
            or len(owner._submit_exchanges) >= 64
            or loop.time() >= deadline
        ):
            raise StandingUnavailable

    try:
        available()
        frozen = checked_finalization(original, request_type)
        body = finalization_bytes(frozen)
        if len(body) > request_type.wire_bytes:
            raise StandingUnavailable
        frozen = request_type.from_json(body)
        proposal = (
            frozen.context.request if request_type is StandingResourceFinalization else frozen
        )
        available()
        request = owner.http.build_request(
            "POST",
            path,
            content=body,
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            timeout=None,
        )
        if "x-kinaiya-standing-capability" in request.headers:
            raise StandingUnavailable
        available()
        exchange = _StandingExchange(
            owner, request, proposal, result_type, deadline, allow_null=nullable
        )
        done, _ = await asyncio.wait({exchange.task}, timeout=max(0, deadline - loop.time()))
        if not done or exchange.abandoned or loop.time() >= deadline:
            raise StandingUnavailable
        result = exchange.task.result()
        if request_type is StandingResourceFinalization and (
            result is None or finalization_bytes(result.finalization) != body
        ):
            raise StandingUnavailable
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


async def standing_resource_bind(owner, request: StandingResourceBindingRequest):
    return await _exchange(
        owner,
        "/v1/standing/resources/binding",
        request,
        StandingResourceBindingRequest,
        StandingResourceBindingReceipt,
    )


async def standing_resource_binding_lookup(owner, request: StandingResourceBindingRequest):
    return await _exchange(
        owner,
        "/v1/standing/resources/binding/lookup",
        request,
        StandingResourceBindingRequest,
        StandingResourceBindingReceipt,
        nullable=True,
    )


async def standing_resource_finalize_context(owner, request: StandingResourceFinalizeRequest):
    return await _exchange(
        owner,
        "/v1/standing/resources/finalization/context",
        request,
        StandingResourceFinalizeRequest,
        StandingResourceFinalizeContext,
    )


async def standing_resource_report(owner, report: StandingResourceFinalization):
    return await _exchange(
        owner,
        "/v1/standing/resources/finalization/report",
        report,
        StandingResourceFinalization,
        StandingResourceFinalizationReceipt,
    )


async def standing_resource_finalization_lookup(owner, request: StandingResourceFinalizeRequest):
    return await _exchange(
        owner,
        "/v1/standing/resources/finalization/lookup",
        request,
        StandingResourceFinalizeRequest,
        StandingResourceFinalizationReceipt,
        nullable=True,
    )
