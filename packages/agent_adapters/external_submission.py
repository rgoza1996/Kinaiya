"""Send only the original submission opportunity from authenticated Session."""

import asyncio

from packages.agent_adapters.pi_sdk import PiCommandUnconfirmed, PiPeerUnavailable
from packages.contracts.external_agent import (
    ExternalAgentClaim,
    ExternalAgentCommandReceipt,
    ExternalAgentRefusal,
    ExternalAgentTask,
)


class PiSubmission:
    def __init__(self, authority, peer, *, observations=None):
        self.authority = authority
        self.peer = peer
        self.observations = observations

    async def submit(self, request, *, retain_only=False):
        request = ExternalAgentClaim.model_validate(request, strict=True)
        command = request.command
        if command.target.backend_session_id != self.peer.session_id:
            return ExternalAgentRefusal(
                contract_version=1,
                request_id=request.request_id,
                kind="refusal",
                reason="scope_changed",
            )
        claim = (
            await self.authority.claim(request, retain_only=True)
            if retain_only
            else await self.authority.claim(request)
        )
        if claim is None:
            return ExternalAgentRefusal(
                contract_version=1, request_id=request.request_id, kind="refusal", reason="busy"
            )
        if claim.disposition == "original":
            if self.observations is not None:
                await asyncio.to_thread(self.observations.prepare, claim, command.source)
            try:
                reply = await self.peer.submit(claim.admission_id, command.instruction)
            except (PiCommandUnconfirmed, PiPeerUnavailable):
                reply = None
        else:
            reply = (
                await asyncio.to_thread(self.observations.original_reply, claim)
                if self.observations is not None
                else self.peer.submission_reply(claim.admission_id)
            )
        if reply is not None and reply.disposition == "refused":
            return ExternalAgentRefusal(
                contract_version=1,
                request_id=request.request_id,
                kind="refusal",
                reason="busy" if reply.reason == "busy" else "unavailable",
            )
        accepted = reply is not None and reply.disposition == "accepted"
        return ExternalAgentCommandReceipt(
            contract_version=1,
            request_id=request.request_id,
            kind="command",
            operation="submit",
            target=command.target,
            idempotency_key=command.idempotency_key,
            task=ExternalAgentTask(
                task_id=claim.admission_id,
                source=command.source,
                revision=1,
                state="starting" if accepted else "uncertain",
            ),
            disposition="accepted" if accepted else "uncertain",
        )
