"""One-use native admission owned by the original finalized classifier task."""

import asyncio
import copy
import hashlib
from uuid import uuid4

from packages.contracts.decisions_attempts import (
    DecisionsAttemptAdmit,
    DecisionsAttemptFinish,
    DecisionsSource,
    digest,
)
from packages.providers.decision_capture import CaptureNativeUnavailable

# Preserve original owners and immutable publications after a lost ACK. Neither
# a timeout nor a replacement worker acquires these requests or refunds them.
_unsettled = set()


def _cleanup_unconfirmed(error, provider):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if type(error).__name__ == "ProviderCleanupError":
            return True
        error = error.__cause__ or error.__context__
    return getattr(provider, "_cleanup", None) is not None or bool(
        getattr(provider, "_count_cleanup_failed", False)
    )


class NativeDecisionAttempt:
    def __init__(self, journal, source, *, expected_attempts, max_attempts=None):
        self.journal, self.source = journal, DecisionsSource.of(source)
        self.expected_attempts = expected_attempts
        self.max_attempts = max_attempts
        self.owner, self.owner_id = asyncio.current_task(), uuid4().hex
        self.attempt_id = uuid4().hex
        self.request = self.receipt = self.completion = None
        self.provider = self.client = self.body = None
        self._binding = None
        self.settled = False
        self.entered = False
        self.cleanup_unconfirmed = False
        self.model_current = None

    def _transport_binding(self, provider):
        client = getattr(provider, "http", None)
        return (
            id(provider),
            id(client),
            provider.info.model_dump(mode="json"),
            str(getattr(client, "base_url", "")),
            digest(dict(client.headers)) if client is not None else None,
            id(getattr(client, "_transport", None)),
            tuple((str(key), id(value)) for key, value in getattr(client, "_mounts", {}).items()),
        )

    def check_transport(self, client, wire):
        if (
            asyncio.current_task() is not self.owner
            or client is not self.client
            or self.receipt is None
            or self.entered
            or (self.model_current is not None and not self.model_current())
            or self._transport_binding(self.provider) != self._binding
            or hashlib.sha256(wire).hexdigest() != self.request.request_sha256
        ):
            raise CaptureNativeUnavailable("Decisions original provider transport changed")

    def enter(self, client, wire):
        self.check_transport(client, wire)
        self.entered = True

    async def admit(self, capture, provider, body, request_format):
        if self.request is not None or asyncio.current_task() is not self.owner:
            raise CaptureNativeUnavailable("Decisions original request opportunity is consumed")
        self.provider, self.client, self.body = (
            provider,
            getattr(provider, "http", None),
            copy.deepcopy(body),
        )
        self._binding = self._transport_binding(provider)
        self.model_current = capture.model_current
        try:
            request = DecisionsAttemptAdmit(
                source=self.source,
                attempt_id=self.attempt_id,
                owner_id=self.owner_id,
                expected_attempts=self.expected_attempts,
                max_attempts=self.max_attempts,
                provider=provider.info.model_dump(mode="json"),
                model_pin=capture.model_pin,
                request_format=request_format,
                request_sha256=digest(self.body),
                transport_sha256=digest(self._binding),
                disclosure_sources=[
                    DecisionsSource.of(entry) for entry in capture.disclosure_sources
                ],
            )
            self.request = request
            _unsettled.add(self)
            # This task may only enter the provider after this exact ACK. A late
            # ACK remains owned by JournalClient; it cannot resume this facade.
            async with asyncio.timeout(2):
                self.receipt = await self.journal.decisions_attempt_admit(request)
            if self.receipt.admission != request or self.receipt.completion is not None:
                raise CaptureNativeUnavailable("Decisions admission ACK is not a fresh original")
            if (
                self._transport_binding(provider) != self._binding
                or digest(body) != request.request_sha256
            ):
                raise CaptureNativeUnavailable("Decisions original transport or body changed")
            # Session atomically rechecked every native source at admission.
            # Do not charge failed preparatory reads after that linearization
            # point; the remaining model lease fence is synchronous.
            if self.model_current is not None and not self.model_current():
                raise CaptureNativeUnavailable("Decisions original model ownership changed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise CaptureNativeUnavailable(
                "Decisions native attempt admission is unavailable"
            ) from exc

    async def finish(self, capture, status, error=None):
        if self.receipt is None:
            return  # No ACK cannot establish absence of a native charge.
        if self.completion is not None:
            raise CaptureNativeUnavailable("Decisions completion opportunity is consumed")
        if asyncio.current_task() is not self.owner:
            raise CaptureNativeUnavailable("Decisions completion lost its original task")
        cancelled = self.owner.cancelling() > 0
        self.cleanup_unconfirmed = _cleanup_unconfirmed(error, self.provider)
        if (
            cancelled
            or isinstance(error, asyncio.CancelledError)
            or self.cleanup_unconfirmed
            or status == "cancelled"
        ):
            status = "uncertain"
        elif status == "completed" and capture.validated_output is None:
            status = "uncertain"
        self.completion = DecisionsAttemptFinish(
            source=self.source,
            attempt_id=self.attempt_id,
            owner_id=self.owner_id,
            admission_sha256=self.receipt.admission_sha256,
            status=status,
            raw_response=capture.raw_response,
            validated_output=capture.validated_output if status == "completed" else None,
            error_type=type(error).__name__ if error else None,
            reason_code=getattr(error, "reason_code", None),
        )
        try:
            async with asyncio.timeout(2):
                result = await self.journal.decisions_attempt_finish(self.completion)
            if result.completion != self.completion:
                raise ValueError("Decisions completion ACK mismatch")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise CaptureNativeUnavailable("Decisions original completion is uncertain") from exc
        self.settled = True
        # Publication acknowledgement is not proof that the original physical
        # exchange closed. Keep that exact owner until cleanup is established.
        if not self.cleanup_unconfirmed:
            _unsettled.discard(self)
        if cancelled:
            raise asyncio.CancelledError
        if status == "uncertain" and error is None:
            raise CaptureNativeUnavailable("Decisions original result is uncertain")


def observe_completion(state):
    """Release only positively reconciled original publications, never re-enter."""
    for owner in tuple(_unsettled):
        if (
            state.last_attempt is not None
            and owner.request == state.last_attempt.admission
            and owner.completion is not None
            and owner.completion == state.last_attempt.completion
            and not owner.cleanup_unconfirmed
        ):
            owner.settled = True
            _unsettled.discard(owner)
