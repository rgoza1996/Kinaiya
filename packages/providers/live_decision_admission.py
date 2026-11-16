"""Same original HTTP transport owner, explicitly selected live source protocol."""

from packages.contracts.live_decisions_attempts import (
    LiveDecisionsAttempt,
    LiveDecisionsAttemptAdmit,
    LiveDecisionsAttemptFinish,
    LiveDecisionsPublication,
    LiveDecisionsSource,
)
from packages.contracts.live_windows import LiveWindowCapture, LiveWindowScope
from packages.providers.decision_admission import NativeDecisionAttempt
from packages.providers.decision_capture import CaptureNativeUnavailable


class NativeLiveDecisionAttempt(NativeDecisionAttempt):
    CONTRACT_VERSION = 2
    ADMIT_TYPE = LiveDecisionsAttemptAdmit
    FINISH_TYPE = LiveDecisionsAttemptFinish
    ADMIT_METHOD = "live_decisions_attempt_admit"
    FINISH_METHOD = "live_decisions_attempt_finish"

    @staticmethod
    def _bind_source(source):
        return LiveDecisionsSource.model_validate(source.model_dump(mode="json"), strict=True)

    async def check_source(self, source):
        try:
            self.source.check_entry(source)
            permission = await self.journal.live_capture_for_source(self.source.publication)
            if (
                not permission.capture_permits_source
                or permission.capture.receipt != self.source.capture
            ):
                raise ValueError("Original capture permission changed")
            reference = self.source.publication
            scope = LiveWindowScope(
                schema_version="2.0",
                protocol="live_window_publication_v2",
                session_id=reference.session_id,
                thread_id=reference.thread_id,
                epoch=reference.epoch,
                utterance_id=reference.utterance_id,
            )
            candidate = await self.journal.latest_live_window(scope)
            if candidate is None or candidate.eligible.original != reference:
                raise ValueError("Original live eligibility changed")
            marker = LiveWindowCapture.model_validate(
                candidate.eligible.capture.payload, strict=True
            )
            if (marker.eligible_revision, marker.input_sha256) != (
                self.source.eligible_revision,
                self.source.input_sha256,
            ):
                raise ValueError("Original native live input changed")
        except (ValueError, KeyError, TypeError) as exc:
            raise CaptureNativeUnavailable("Original live source is no longer current") from exc

    def publication(self):
        if not self.settled or self.receipt is None or self.completion is None:
            raise CaptureNativeUnavailable("Original live completion is not confirmed")
        attempt = LiveDecisionsAttempt(
            admission=self.receipt.admission,
            admission_sha256=self.receipt.admission_sha256,
            attempt=self.receipt.attempt,
            completion=self.completion,
        )
        return LiveDecisionsPublication.of(attempt)
