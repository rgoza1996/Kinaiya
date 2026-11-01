"""Codex-authored bounded Agent standing transport on JournalClient ownership."""

import asyncio

from pydantic import BaseModel

from packages.contracts.client import _SubmitExchange
from packages.contracts.standing_admission import StandingInitialProof, StandingTaskAdmission
from packages.contracts.standing_dispatch import StandingProducerReceipt, StandingProducerRenewal
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionClaimAdmission,
    StandingExecutionIntent,
)
from packages.contracts.standing_work import StandingConflict, StandingUnavailable, _checked


def _freeze(original, kind):
    if type(original) is not kind:
        raise StandingUnavailable
    # Inspect the actual graph before older nested models can discard fields
    # while dumping. Round-trip only the fully checked immutable original.
    checked = _checked(original, kind)
    wire = checked.canonical_bytes()
    proposal = kind.from_json(wire)
    pending = [(original, proposal)]
    while pending:
        actual, expected = pending.pop()
        if type(actual) is not type(expected):
            raise StandingUnavailable
        if isinstance(expected, BaseModel):
            pending.extend(
                (actual.__dict__[name], expected.__dict__[name])
                for name in type(expected).model_fields
            )
        elif type(expected) is dict:
            if actual.keys() != expected.keys():
                raise StandingUnavailable
            pending.extend((actual[name], expected[name]) for name in expected)
        elif type(expected) in (list, tuple):
            if len(actual) != len(expected):
                raise StandingUnavailable
            pending.extend(zip(actual, expected, strict=True))
        elif actual != expected:
            raise StandingUnavailable
    return proposal, wire


class _StandingExchange(_SubmitExchange):
    """Use the parent's actual registry, abandonment and sole-close lifecycle."""

    def _timely(self, deadline):
        if self.abandoned or asyncio.get_running_loop().time() >= deadline:
            raise StandingUnavailable

    async def _run(self, request, proposal, result_type, deadline):
        try:
            self._timely(deadline)
            self.response = await self.owner.http.send(request, stream=True, follow_redirects=False)
            self._timely(deadline)
            response = self.response
            if response.status_code == 409:
                raise StandingConflict
            if response.status_code != 200:
                raise StandingUnavailable
            if (
                response.headers.get("content-encoding", "identity").strip(" \t").lower()
                != "identity"
                or response.headers.get("content-type", "").split(";", 1)[0].strip(" \t").lower()
                != "application/json"
            ):
                raise StandingUnavailable
            maximum = result_type.wire_bytes
            declared = response.headers.get("content-length")
            length = None
            if declared is not None:
                if (
                    "transfer-encoding" in response.headers
                    or not declared.isascii()
                    or not declared.isdigit()
                    or len(declared) > 8
                    or int(declared) > maximum
                ):
                    raise StandingUnavailable
                length = int(declared)
            raw = bytearray()
            # Read the actual raw identity stream. HTTPX's aiter_* helpers
            # auto-close at EOF, outside the independently retained sole close.
            async for chunk in response.stream:
                if type(chunk) is not bytes or len(raw) + len(chunk) > maximum:
                    raise StandingUnavailable
                raw.extend(chunk)
                self._timely(deadline)
            if length is not None and len(raw) != length:
                raise StandingUnavailable
            self._timely(deadline)
            if (
                result_type in (StandingExecutionClaim, StandingInitialProof)
                and bytes(raw).strip(b" \t\r\n") == b"null"
            ):
                result = None
            else:
                result = result_type.from_json(bytes(raw))
                if proposal is not None:
                    result = result.validate_request(proposal)
            self._timely(deadline)
        finally:
            if self.response is not None:
                self.close_task = asyncio.create_task(self._close())
                self.close_task.add_done_callback(self._settled)
                await asyncio.shield(self.close_task)
        self._timely(deadline)
        return result


async def _exchange(owner, method, path, original, request_type, result_type, *, capability=None):
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
        headers = {"Accept-Encoding": "identity"}
        if capability is not None:
            if type(capability) is not bytes or len(capability) != 32:
                raise StandingUnavailable
            headers["X-Kinaiya-Standing-Capability"] = capability.hex()
        proposal, body = None, None
        if request_type is not None:
            proposal, body = _freeze(original, request_type)
            maximum = 8192 if request_type is StandingProducerRenewal else 131072
            if len(body) > maximum:
                raise StandingUnavailable
            headers["Content-Type"] = "application/json"
        available()
        request = owner.http.build_request(
            method, path, content=body, headers=headers, timeout=None
        )
        available()
        exchange = _StandingExchange(owner, request, proposal, result_type, deadline)
        done, _ = await asyncio.wait({exchange.task}, timeout=max(0, deadline - loop.time()))
        if not done or exchange.abandoned or loop.time() >= deadline:
            raise StandingUnavailable
        return exchange.task.result()
    except asyncio.CancelledError:
        if exchange is not None:
            exchange.abandon()
        if asyncio.current_task().cancelling():
            raise
        # A cancelled actual close is cleanup uncertainty, not cancellation
        # of the public caller. The inherited callback retains its failure.
        raise StandingUnavailable from None
    except StandingConflict:
        raise StandingConflict from None
    except Exception:
        if exchange is not None:
            exchange.abandon()
        raise StandingUnavailable from None


async def standing_renew(owner, request: StandingProducerRenewal) -> StandingProducerReceipt:
    return await _exchange(
        owner,
        "POST",
        "/v1/standing/producer/renew",
        request,
        StandingProducerRenewal,
        StandingProducerReceipt,
    )


async def standing_prepare(
    owner, request: StandingExecutionIntent
) -> StandingExecutionClaimAdmission:
    return await _exchange(
        owner,
        "POST",
        "/v1/standing/executions/prepare",
        request,
        StandingExecutionIntent,
        StandingExecutionClaimAdmission,
    )


async def standing_claim_lookup(
    owner, request: StandingExecutionIntent
) -> StandingExecutionClaim | None:
    return await _exchange(
        owner,
        "POST",
        "/v1/standing/executions/lookup",
        request,
        StandingExecutionIntent,
        StandingExecutionClaim,
    )


async def standing_unresolved(owner) -> StandingExecutionClaim | None:
    return await _exchange(
        owner, "GET", "/v1/standing/executions/unresolved", None, None, StandingExecutionClaim
    )


async def standing_admit(
    owner, request: StandingExecutionIntent, *, capability=None
) -> StandingTaskAdmission:
    return await _exchange(
        owner,
        "POST",
        "/v1/standing/tasks/admit",
        request,
        StandingExecutionIntent,
        StandingTaskAdmission,
        capability=capability,
    )


async def standing_task_lookup(
    owner, request: StandingExecutionIntent
) -> StandingInitialProof | None:
    return await _exchange(
        owner,
        "POST",
        "/v1/standing/tasks/lookup",
        request,
        StandingExecutionIntent,
        StandingInitialProof,
    )
