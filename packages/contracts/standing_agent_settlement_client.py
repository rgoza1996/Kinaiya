"""Purpose-separated packets on the original Journal exchange/sole close."""

import asyncio

from packages.contracts.standing_agent_settlement import (
    StandingAgentRetirementRegistrationReceipt,
    StandingAgentRetirementRegistrationRequest,
    StandingAgentSettlementQuery,
    StandingAgentSettlementReceipt,
    StandingAgentSettlementRequest,
    agent_settlement_bytes,
    checked_agent_settlement,
)
from packages.contracts.standing_client import _StandingExchange
from packages.contracts.standing_work import StandingConflict, StandingUnavailable

_HEADER = "x-kinaiya-standing-settlement-capability"


class _AgentSettlementExchange(_StandingExchange):
    def _validate(self, result, proposal):
        if type(proposal) is StandingAgentSettlementQuery:
            return result.validate_query(proposal)
        return result.validate_request(proposal)


async def _exchange(
    owner, path, original, request_type, result_type, *, secret32=None, nullable=False
):
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
        frozen = checked_agent_settlement(original, request_type)
        body = agent_settlement_bytes(frozen)
        frozen = request_type.from_json(body)
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
        if secret32 is not None:
            if (
                request_type is not StandingAgentSettlementRequest
                or type(secret32) is not bytes
                or len(secret32) != 32
            ):
                raise StandingUnavailable
            headers[_HEADER] = secret32.hex()
        elif request_type is StandingAgentSettlementRequest:
            raise StandingUnavailable
        available()
        request = owner.http.build_request(
            "POST", path, content=body, headers=headers, timeout=None
        )
        # Never inherit a closure secret or a global default capability into a
        # different purpose, including read-only registration/history requests.
        supplied = request.headers.get_list(_HEADER)
        if any(
            name.startswith("x-kinaiya-standing-") and name != _HEADER for name in request.headers
        ) or supplied != ([] if secret32 is None else [secret32.hex()]):
            raise StandingUnavailable
        available()
        exchange = _AgentSettlementExchange(
            owner, request, frozen, result_type, deadline, allow_null=nullable
        )
        done, _ = await asyncio.wait({exchange.task}, timeout=max(0, deadline - loop.time()))
        if not done or exchange.abandoned or loop.time() >= deadline:
            raise StandingUnavailable
        return exchange.task.result()
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


async def standing_retirement_register(owner, request):
    return await _exchange(
        owner,
        "/v1/standing/tasks/retirement/register",
        request,
        StandingAgentRetirementRegistrationRequest,
        StandingAgentRetirementRegistrationReceipt,
    )


async def standing_retirement_registration_lookup(owner, request):
    return await _exchange(
        owner,
        "/v1/standing/tasks/retirement/registration/lookup",
        request,
        StandingAgentRetirementRegistrationRequest,
        StandingAgentRetirementRegistrationReceipt,
        nullable=True,
    )


async def standing_settle(owner, request, *, secret32):
    return await _exchange(
        owner,
        "/v1/standing/tasks/settle",
        request,
        StandingAgentSettlementRequest,
        StandingAgentSettlementReceipt,
        secret32=secret32,
    )


async def standing_settlement_lookup(owner, query):
    return await _exchange(
        owner,
        "/v1/standing/tasks/settlement/lookup",
        query,
        StandingAgentSettlementQuery,
        StandingAgentSettlementReceipt,
        nullable=True,
    )
