"""Transport-independent owner and routing contract for external Agents."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from packages.contracts.models import ContentScope

MAX_CURSOR = (1 << 53) - 1
MAX_EXTERNAL_NATIVE_REQUEST_BYTES = 8192
MAX_EXTERNAL_COMMAND_BYTES = 131072
MAX_EXTERNAL_TRANSCRIPT_PAGE_BYTES = 262144
BridgeIdentifier = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
]
BridgeAttachmentId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
BridgeCursor = Annotated[int, Field(ge=0, le=MAX_CURSOR)]
BridgeRevision = Annotated[int, Field(ge=1, le=MAX_CURSOR)]
BridgeInstruction = Annotated[str, Field(min_length=1, max_length=16000)]
BridgeTaskState = Literal[
    "queued",
    "starting",
    "running",
    "interrupt_requested",
    "settled",
    "failed",
    "interrupted",
    "uncertain",
]


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class _Envelope(_Closed):
    contract_version: Literal[1]
    request_id: BridgeIdentifier

    @field_validator("contract_version", mode="before")
    @classmethod
    def check_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported external Agent bridge version")
        return value


class ExternalAgentScope(_Closed):
    session_id: BridgeIdentifier
    thread_id: BridgeIdentifier
    epoch: BridgeRevision


class ExternalAgentTarget(_Closed):
    attachment_id: BridgeAttachmentId
    backend_session_id: BridgeIdentifier


class ExternalAgentSource(_Closed):
    event_ids: Annotated[tuple[BridgeIdentifier, ...], Field(min_length=1, max_length=32)]
    revision: BridgeRevision

    @field_validator("event_ids")
    @classmethod
    def unique_events(cls, event_ids):
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("External Agent source events must be distinct")
        return event_ids


class ExternalAgentCapabilities(_Closed):
    submit: bool
    queue: bool
    steer: bool
    interrupt: bool
    event_replay: bool
    transcript_read: bool
    approvals: bool
    artifacts: bool
    shutdown_owned: bool


class ExternalAgentConnect(_Envelope):
    operation: Literal["connect"]
    profile_id: BridgeIdentifier
    scope: ExternalAgentScope
    expected_backend_session_id: BridgeIdentifier | None
    allowed_content: tuple[ContentScope, ...] = Field(min_length=1, max_length=3)

    @field_validator("allowed_content")
    @classmethod
    def distinct_content(cls, scopes):
        if len(set(scopes)) != len(scopes):
            raise ValueError("External Agent content scopes must be distinct")
        return scopes


class ExternalAgentBind(_Envelope):
    operation: Literal["bind"]
    target: ExternalAgentTarget


class ExternalAgentRevoke(_Envelope):
    operation: Literal["revoke"]
    attachment_id: BridgeAttachmentId


class ExternalAgentGrantLookup(_Envelope):
    operation: Literal["lookup_grant"]
    attachment_id: BridgeAttachmentId


class ExternalAgentAttach(_Envelope):
    operation: Literal["attach"]
    attachment_id: BridgeAttachmentId


class _TargetRequest(_Envelope):
    target: ExternalAgentTarget


class ExternalAgentDisconnect(_TargetRequest):
    operation: Literal["disconnect"]
    mode: Literal["detach", "shutdown_owned"]


class ExternalAgentInspect(_TargetRequest):
    operation: Literal["status"]


class _Command(_TargetRequest):
    idempotency_key: BridgeIdentifier
    source: ExternalAgentSource


class ExternalAgentSubmit(_Command):
    operation: Literal["submit"]
    instruction: BridgeInstruction
    content_scope: ContentScope
    execution_policy: Literal["backend_owned"]
    queue_if_busy: bool


class _TaskCommand(_Command):
    task_id: BridgeIdentifier
    expected_revision: BridgeRevision


class ExternalAgentSteer(_TaskCommand):
    operation: Literal["steer"]
    instruction: BridgeInstruction
    content_scope: ContentScope


class ExternalAgentInterrupt(_TaskCommand):
    operation: Literal["interrupt"]


class ExternalAgentClaim(_Envelope):
    operation: Literal["claim"]
    decision_event_id: BridgeIdentifier
    command: ExternalAgentSubmit

    def command_digest(self):
        # HTTP request IDs correlate replies. The task key and exact action
        # determine whether another delivery is the same original.
        payload = {
            "version": self.contract_version,
            "decision_event_id": self.decision_event_id,
            "command": self.command.model_dump(mode="json", exclude={"request_id"}),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()


class ExternalAgentReplay(_TargetRequest):
    operation: Literal["events"]
    after: BridgeCursor
    limit: int = Field(ge=1, le=128)


class ExternalAgentTranscriptPosition(_Closed):
    sequence: BridgeCursor
    byte_offset: BridgeCursor

    @model_validator(mode="after")
    def offset_has_entry(self):
        if self.byte_offset and not self.sequence:
            raise ValueError("A transcript offset requires an entry sequence")
        return self


class ExternalAgentReadTranscript(_TargetRequest):
    operation: Literal["read_transcript"]
    after: ExternalAgentTranscriptPosition
    through_sequence: BridgeCursor | None
    limit: int = Field(ge=1, le=128)

    @model_validator(mode="after")
    def within_snapshot(self):
        if (self.through_sequence is not None and self.after.sequence > self.through_sequence) or (
            self.through_sequence is None and self.after.byte_offset
        ):
            raise ValueError("Transcript position is beyond its snapshot")
        return self


class ExternalAgentRequest(RootModel):
    root: Annotated[
        ExternalAgentConnect
        | ExternalAgentBind
        | ExternalAgentRevoke
        | ExternalAgentGrantLookup
        | ExternalAgentAttach
        | ExternalAgentDisconnect
        | ExternalAgentInspect
        | ExternalAgentSubmit
        | ExternalAgentSteer
        | ExternalAgentInterrupt
        | ExternalAgentClaim
        | ExternalAgentReplay
        | ExternalAgentReadTranscript,
        Field(discriminator="operation"),
    ]


class ExternalAgentTask(_Closed):
    task_id: BridgeIdentifier
    source: ExternalAgentSource
    revision: BridgeRevision
    state: BridgeTaskState


class ExternalAgentStatus(_Envelope):
    kind: Literal["status"]
    target: ExternalAgentTarget
    scope: ExternalAgentScope
    backend: BridgeIdentifier
    capabilities: ExternalAgentCapabilities
    state: Literal["connected", "detached", "unavailable", "uncertain", "stopped"]
    tasks: tuple[ExternalAgentTask, ...] = Field(max_length=128)


class ExternalAgentGrant(_Envelope):
    kind: Literal["grant"]
    attachment_id: BridgeAttachmentId
    original: ExternalAgentConnect
    phase: Literal["authorized", "bound", "revoked"]
    backend_session_id: BridgeIdentifier | None

    @model_validator(mode="after")
    def consistent(self):
        if (self.phase == "authorized" and self.backend_session_id is not None) or (
            self.phase == "bound" and self.backend_session_id is None
        ):
            raise ValueError("External Agent grant has an inconsistent binding")
        return self


class ExternalAgentDisconnectReceipt(_Envelope):
    kind: Literal["disconnect"]
    target: ExternalAgentTarget
    disposition: Literal["detached", "stopped", "uncertain"]


class ExternalAgentCommandReceipt(_Envelope):
    kind: Literal["command"]
    target: ExternalAgentTarget
    operation: Literal["submit", "steer", "interrupt"]
    idempotency_key: BridgeIdentifier
    task: ExternalAgentTask
    disposition: Literal["queued", "accepted", "requested", "uncertain"]


class ExternalAgentClaimReceipt(_Envelope):
    kind: Literal["claim"]
    target: ExternalAgentTarget
    decision_event_id: BridgeIdentifier
    idempotency_key: BridgeIdentifier
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    admission_id: BridgeAttachmentId
    disposition: Literal["original", "retained"]


class _Event(_Closed):
    cursor: BridgeRevision
    task_id: BridgeIdentifier


class ExternalAgentStateEvent(_Event):
    kind: Literal["task_state"]
    state: BridgeTaskState
    revision: BridgeRevision


class ExternalAgentMessageEvent(_Event):
    kind: Literal["assistant_message"]
    text: str = Field(max_length=262144)


class ExternalAgentToolEvent(_Event):
    kind: Literal["tool_activity"]
    tool_call_id: BridgeIdentifier
    tool_name: BridgeIdentifier
    phase: Literal["started", "finished", "failed"]


class ExternalAgentOutputUnavailable(_Event):
    kind: Literal["output_unavailable"]


class ExternalAgentEventPage(_Envelope):
    kind: Literal["events"]
    target: ExternalAgentTarget
    after: BridgeCursor
    next_after: BridgeCursor
    has_more: bool
    events: tuple[
        Annotated[
            ExternalAgentStateEvent
            | ExternalAgentMessageEvent
            | ExternalAgentToolEvent
            | ExternalAgentOutputUnavailable,
            Field(discriminator="kind"),
        ],
        ...,
    ] = Field(max_length=128)

    @model_validator(mode="after")
    def contiguous(self):
        expected = tuple(range(self.after + 1, self.after + len(self.events) + 1))
        if (
            tuple(event.cursor for event in self.events) != expected
            or self.next_after != self.after + len(self.events)
            or (not self.events and self.has_more)
        ):
            raise ValueError("External Agent replay page is discontinuous")
        return self


class ExternalAgentTranscriptEntry(_Closed):
    sequence: BridgeRevision
    event_id: BridgeIdentifier
    kind: Literal[
        "conversation.user", "conversation.converse", "conversation.window", "conversation.revision"
    ]
    origin: Literal["authenticated-user", "converse"]
    revision: BridgeRevision
    revises_event_id: BridgeIdentifier | None
    utterance_id: BridgeIdentifier | None
    window_index: BridgeCursor | None
    content_scope: ContentScope
    byte_offset: BridgeCursor
    end_of_entry: bool
    text: str = Field(max_length=32768)

    @model_validator(mode="after")
    def provenance_shape(self):
        window = self.kind == "conversation.window"
        if (
            (self.utterance_id is not None) != window
            or (self.window_index is not None) != window
            or (self.revises_event_id is not None) != (self.kind == "conversation.revision")
        ):
            raise ValueError("Transcript entry metadata does not match its native kind")
        return self


class ExternalAgentTranscriptPage(_Envelope):
    kind: Literal["transcript"]
    target: ExternalAgentTarget
    scope: ExternalAgentScope
    after: ExternalAgentTranscriptPosition
    through_sequence: BridgeCursor
    next_after: ExternalAgentTranscriptPosition
    has_more: bool
    entries: tuple[ExternalAgentTranscriptEntry, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def bounded_positions(self):
        position = self.after
        for entry in self.entries:
            if position.byte_offset:
                if (entry.sequence, entry.byte_offset) != (position.sequence, position.byte_offset):
                    raise ValueError("Transcript continuation does not match its original entry")
            elif entry.sequence <= position.sequence or entry.byte_offset:
                raise ValueError("Transcript entries are out of native order")
            read_bytes = len(entry.text.encode("utf-8"))
            if not entry.end_of_entry and not read_bytes:
                raise ValueError("Transcript continuation made no progress")
            position = ExternalAgentTranscriptPosition(
                sequence=entry.sequence,
                byte_offset=0 if entry.end_of_entry else entry.byte_offset + read_bytes,
            )
        if (
            not position.sequence <= self.next_after.sequence <= self.through_sequence
            or (position.byte_offset and self.next_after != position)
            or (not position.byte_offset and self.next_after.byte_offset)
            or (self.has_more and self.next_after == self.after)
            or (
                not self.has_more
                and (
                    self.next_after.sequence != self.through_sequence or self.next_after.byte_offset
                )
            )
        ):
            raise ValueError("Transcript entries are outside their snapshot page")
        return self


class ExternalAgentRefusal(_Envelope):
    kind: Literal["refusal"]
    reason: Literal[
        "unsupported",
        "unavailable",
        "busy",
        "unauthorized",
        "scope_changed",
        "conflict",
        "replay_expired",
    ]


class ExternalAgentResponse(RootModel):
    root: Annotated[
        ExternalAgentStatus
        | ExternalAgentGrant
        | ExternalAgentDisconnectReceipt
        | ExternalAgentCommandReceipt
        | ExternalAgentClaimReceipt
        | ExternalAgentEventPage
        | ExternalAgentTranscriptPage
        | ExternalAgentRefusal,
        Field(discriminator="kind"),
    ]
