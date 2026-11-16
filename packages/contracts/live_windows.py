"""Atomic live capture annotations; no marker grants inference or execution authority."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.models import AppendRequest, JournalEntry, SpeechWindow
from packages.contracts.stt_settlement import digest, encode, settlement_json

PROTOCOL = "live_window_publication_v2"
CAPTURE_KIND = "conversation.window_capture"
MAX_REQUEST_BYTES = 68 * 1024
MAX_RESULT_BYTES = 144 * 1024
MAX_CANDIDATE_BYTES = 288 * 1024
MAX_INTEGER = (1 << 53) - 1
Identity = Annotated[str, Field(min_length=1, max_length=128)]
Token = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]
SHA = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]


def live_window_json(raw, maximum=MAX_REQUEST_BYTES):
    return settlement_json(raw, maximum)


def entry_request(entry):
    return entry.model_dump(mode="json", exclude={"event_id", "sequence", "origin", "created_at"})


def input_digest(window):
    """Exact source content/hints/recognition identity, excluding storage counters."""
    window = SpeechWindow.model_validate(window)
    return digest(
        {
            "text": window.text,
            "user_speaking": window.user_speaking,
            "capture_ended": window.capture_ended,
            "content_scope": window.content_scope,
            "stt_model": None
            if window.stt_model is None
            else window.stt_model.model_dump(mode="json"),
        }
    )


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class LiveWindowPublication(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["live_window_publication_v2"]
    owner_instance: Token
    capture_id: Token
    request: AppendRequest

    @model_validator(mode="after")
    def exact_window(self):
        request = self.request
        if request.kind != "conversation.window":
            raise ValueError("Live publication requires its original transcript window")
        if request.causes or request.task_refs:
            raise ValueError("Captured speech cannot supply causal or task authority")
        window = SpeechWindow.model_validate(request.payload, strict=True)
        if (request.session_id, request.thread_id, request.epoch) != (
            window.session_id,
            window.thread_id,
            window.epoch,
        ):
            raise ValueError("Live source envelope and window scope differ")
        if len(encode(request.model_dump(mode="json")).encode("utf-8")) > 65536:
            raise ValueError("Live source exceeds the unchanged journal bound")
        self.reference()
        return self

    def reference(self):
        return LiveWindowPublicationRef(
            schema_version="2.0",
            protocol=PROTOCOL,
            owner_instance=self.owner_instance,
            capture_id=self.capture_id,
            session_id=self.request.session_id,
            thread_id=self.request.thread_id,
            epoch=self.request.epoch,
            utterance_id=self.request.payload["utterance_id"],
            idempotency_key=self.request.idempotency_key,
            request_sha256=digest(self.request.model_dump(mode="json")),
        )


class LiveWindowPublicationRef(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["live_window_publication_v2"]
    owner_instance: Token
    capture_id: Token
    session_id: Identity
    thread_id: Identity
    epoch: int = Field(ge=1, le=MAX_INTEGER)
    utterance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    idempotency_key: str = Field(min_length=1, max_length=256)
    request_sha256: SHA


class LiveWindowScope(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["live_window_publication_v2"]
    session_id: Identity
    thread_id: Identity
    epoch: int = Field(ge=1, le=MAX_INTEGER)
    utterance_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class LiveWindowCapture(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["live_window_publication_v2"]
    capture_mode: Literal["adaptive_live"]
    original: LiveWindowPublicationRef
    source_event_id: Identity
    source_sequence: int = Field(ge=1, lt=MAX_INTEGER)
    source_revision: int = Field(ge=1, le=MAX_INTEGER)
    input_sha256: SHA
    eligible_revision: int = Field(ge=1, le=MAX_INTEGER)
    eligible_source_event_id: Identity
    eligible_source_sequence: int = Field(ge=1, lt=MAX_INTEGER)
    eligible_source_revision: int = Field(ge=1, le=MAX_INTEGER)

    @model_validator(mode="after")
    def earlier_or_current(self):
        if self.eligible_source_sequence > self.source_sequence:
            raise ValueError("Eligible source cannot follow its capture annotation")
        if self.eligible_source_sequence == self.source_sequence and (
            self.eligible_source_event_id != self.source_event_id
            or self.eligible_source_revision != self.source_revision
        ):
            raise ValueError("Current eligibility must bind the identical original source")
        return self


class LiveWindowPublicationProof(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["live_window_publication_v2"]
    original: LiveWindowPublicationRef
    source: JournalEntry
    capture: JournalEntry

    @model_validator(mode="after")
    def exact_pair(self):
        source, capture, original = self.source, self.capture, self.original
        marker = LiveWindowCapture.model_validate(capture.payload, strict=True)
        window = SpeechWindow.model_validate(source.payload, strict=True)
        if (
            source.kind != "conversation.window"
            or source.origin != "authenticated-user"
            or (source.session_id, source.thread_id, source.epoch, source.idempotency_key)
            != (original.session_id, original.thread_id, original.epoch, original.idempotency_key)
            or window.utterance_id != original.utterance_id
            or digest(entry_request(source)) != original.request_sha256
            or marker.original != original
            or (marker.source_event_id, marker.source_sequence, marker.source_revision)
            != (source.event_id, source.sequence, window.revision)
            or marker.input_sha256 != input_digest(window)
            or capture.kind != CAPTURE_KIND
            or capture.origin != "converse"
            or (capture.session_id, capture.thread_id, capture.epoch)
            != (source.session_id, source.thread_id, source.epoch)
            or capture.causes != [source.event_id]
            or capture.task_refs
            or capture.idempotency_key != "live-window-capture:" + source.event_id
            or capture.sequence != source.sequence + 1
        ):
            raise ValueError("Live publication pair does not bind its exact original")
        if len(encode(self.model_dump(mode="json")).encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("Live publication proof exceeds its bound")
        return self


class LiveWindowPublicationState(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["live_window_publication_v2"]
    original: LiveWindowPublicationRef
    receipt: LiveWindowPublicationProof | None

    @model_validator(mode="after")
    def exact_original(self):
        if self.receipt is not None and self.receipt.original != self.original:
            raise ValueError("Live lookup changed its original publication")
        return self


class LiveWindowCandidate(_Closed):
    """Original eligible pair plus latest raw annotation; never a rebound model result."""

    schema_version: Literal["2.0"]
    protocol: Literal["live_window_publication_v2"]
    eligible: LiveWindowPublicationProof
    latest: LiveWindowPublicationProof

    @model_validator(mode="after")
    def exact_eligibility(self):
        original = LiveWindowCapture.model_validate(self.eligible.capture.payload, strict=True)
        current = LiveWindowCapture.model_validate(self.latest.capture.payload, strict=True)
        if (
            (self.latest.source.session_id, self.latest.source.thread_id, self.latest.source.epoch)
            != (
                self.eligible.source.session_id,
                self.eligible.source.thread_id,
                self.eligible.source.epoch,
            )
            or (
                current.original.owner_instance,
                current.original.capture_id,
                current.original.utterance_id,
            )
            != (
                original.original.owner_instance,
                original.original.capture_id,
                original.original.utterance_id,
            )
            or (
                current.eligible_revision,
                current.input_sha256,
                current.eligible_source_event_id,
                current.eligible_source_sequence,
                current.eligible_source_revision,
            )
            != (
                original.eligible_revision,
                original.input_sha256,
                original.source_event_id,
                original.source_sequence,
                original.source_revision,
            )
            or current.source_sequence < original.source_sequence
            or (
                original.eligible_source_event_id,
                original.eligible_source_sequence,
                original.eligible_source_revision,
            )
            != (original.source_event_id, original.source_sequence, original.source_revision)
        ):
            raise ValueError("Live candidate changed its eligible original or current capture")
        if len(encode(self.model_dump(mode="json")).encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raise ValueError("Live candidate exceeds its bound")
        return self
