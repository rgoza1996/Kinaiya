"""Client for Session's retained external Agent authorization boundary."""

import asyncio
import secrets

import httpx

from packages.contracts.external_agent import (
    MAX_EXTERNAL_COMMAND_BYTES,
    MAX_EXTERNAL_NATIVE_REQUEST_BYTES,
    MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES,
    ExternalAgentBind,
    ExternalAgentClaim,
    ExternalAgentClaimReceipt,
    ExternalAgentConnect,
    ExternalAgentGrant,
    ExternalAgentGrantLookup,
    ExternalAgentReadTranscript,
    ExternalAgentRevoke,
    ExternalAgentScope,
    ExternalAgentTarget,
    ExternalAgentTranscriptPage,
)
from packages.contracts.transcript_scan import transcript_json


class ExternalAgentNativeUnavailable(RuntimeError):
    pass


class ExternalAgentNativeConflict(ValueError):
    pass


class ExternalAgentSessionClient:
    def __init__(self, journal):
        self.journal = journal

    async def _request(
        self,
        action,
        original,
        model,
        bound=MAX_EXTERNAL_NATIVE_REQUEST_BYTES,
        *,
        input_bound=MAX_EXTERNAL_NATIVE_REQUEST_BYTES,
        allow_empty=False,
    ):
        wire = original.model_dump_json().encode("utf-8")
        if len(wire) > input_bound:
            raise ValueError("External Agent request exceeds its declared bound")
        try:
            async with asyncio.timeout(5):
                raw = await self.journal._owned_metadata(
                    "POST",
                    "/v1/external-agent/" + action,
                    json=original.model_dump(mode="json"),
                    maximum=bound,
                    raw=True,
                )
            if allow_empty and raw == b"":
                return None
            transcript_json(raw, bound)
            result = model.model_validate_json(raw, strict=True)
            if result.request_id != original.request_id:
                raise ExternalAgentNativeUnavailable
            return result
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409:
                raise ExternalAgentNativeConflict from None
            if error.response.status_code in {401, 403}:
                raise PermissionError("External Agent native role is refused") from None
            raise ExternalAgentNativeUnavailable from None
        except (httpx.RequestError, RuntimeError, ValueError, TimeoutError, RecursionError):
            raise ExternalAgentNativeUnavailable from None

    async def authorize(self, request):
        original = ExternalAgentConnect.model_validate(request, strict=True)
        grant = await self._request("authorize", original, ExternalAgentGrant)
        if grant.original != original:
            raise ExternalAgentNativeUnavailable
        return grant

    async def bind(self, request):
        original = ExternalAgentBind.model_validate(request, strict=True)
        grant = await self._request("bind", original, ExternalAgentGrant)
        if (
            grant.phase != "bound"
            or grant.attachment_id != original.target.attachment_id
            or grant.backend_session_id != original.target.backend_session_id
        ):
            raise ExternalAgentNativeUnavailable
        return grant

    async def lookup_grant(self, request):
        original = ExternalAgentGrantLookup.model_validate(request, strict=True)
        grant = await self._request("grant", original, ExternalAgentGrant)
        if grant.phase == "revoked" or grant.attachment_id != original.attachment_id:
            raise ExternalAgentNativeUnavailable
        return grant

    async def revoke(self, request):
        original = ExternalAgentRevoke.model_validate(request, strict=True)
        grant = await self._request("revoke", original, ExternalAgentGrant)
        if grant.phase != "revoked" or grant.attachment_id != original.attachment_id:
            raise ExternalAgentNativeUnavailable
        return grant

    async def claim(self, request, *, retain_only=False):
        return await self._claim(
            request,
            "claim/retained" if retain_only else "claim",
            {"retained"} if retain_only else {"original", "retained"},
            allow_empty=retain_only,
        )

    async def eligible_claim(self, request):
        return await self._claim(request, "claim/eligible", {"eligible", "retained"})

    async def _claim(self, request, action, dispositions, *, allow_empty=False):
        original = ExternalAgentClaim.model_validate(request, strict=True)
        result = await self._request(
            action,
            original,
            ExternalAgentClaimReceipt,
            input_bound=MAX_EXTERNAL_COMMAND_BYTES,
            allow_empty=allow_empty,
        )
        if result is None and allow_empty:
            return None
        if (
            result.target != original.command.target
            or result.decision_event_id != original.decision_event_id
            or result.idempotency_key != original.command.idempotency_key
            or result.command_digest != original.command_digest()
            or result.disposition not in dispositions
        ):
            raise ExternalAgentNativeUnavailable
        return result

    async def transcript(self, request, *, scope):
        expected = ExternalAgentScope.model_validate(scope, strict=True)
        original = ExternalAgentReadTranscript.model_validate(request, strict=True)
        page = await self._request(
            "transcript", original, ExternalAgentTranscriptPage, MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES
        )
        if (
            page.target != original.target
            or page.scope != expected
            or page.after != original.after
            or original.through_sequence not in (None, page.through_sequence)
        ):
            raise ExternalAgentNativeUnavailable
        return page

    async def read_bound_transcript(self, grant, *, after, through_sequence, limit):
        retained = ExternalAgentGrant.model_validate(grant, strict=True)
        if retained.phase != "bound":
            raise ExternalAgentNativeConflict
        request = ExternalAgentReadTranscript(
            contract_version=1,
            operation="read_transcript",
            request_id=secrets.token_hex(16),
            target=ExternalAgentTarget(
                attachment_id=retained.attachment_id,
                backend_session_id=retained.backend_session_id,
            ),
            after=after,
            through_sequence=through_sequence,
            limit=limit,
        )
        return await self.transcript(request, scope=retained.original.scope)
