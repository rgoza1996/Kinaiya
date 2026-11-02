"""Codex-authored pure standing producer, activation and epoch-stop proofs.

Packets cannot prove producer liveness, grant work or establish settlement.
Historical activation and target-stop proofs never authorize another launch.
"""

import hashlib
from typing import Annotated, ClassVar, Literal, Self

from pydantic import Field, field_validator, model_validator

from packages.contracts.agent_control import (
    MAX_AGENT_CONTROL_INTEGER,
    AgentMode,
    AgentModeChange,
    AgentModeReceipt,
    BackendId,
    ControlInstance,
    Digest,
    PositiveInteger,
    Revision,
)
from packages.contracts.standing_work import (
    _checked,
    _Closed,
    _encode,
    canonical_bytes,
    standing_json,
)

MAX_STANDING_DISPATCH_REQUEST_BYTES = 4096
MAX_STANDING_DISPATCH_RESPONSE_BYTES = 32768
MAX_STANDING_DISPATCH_EPOCH = 10000
STANDING_PRODUCER_TTL_MS = 15000

_ERROR = "Invalid standing dispatch contract"
_PROOF_ERROR = "Standing dispatch proof does not match the original request"
_Epoch = Annotated[int, Field(ge=0, le=MAX_STANDING_DISPATCH_EPOCH)]


class _Dispatch(_Closed):
    wire_bytes: ClassVar[int] = MAX_STANDING_DISPATCH_REQUEST_BYTES
    schema_version: Literal["1.0"]

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        # Owner-definition parsing permits 128 KiB. These scalar requests do
        # not inherit that raw-input allowance, even for padding whitespace.
        try:
            return cls.model_validate(standing_json(raw, maximum=cls.wire_bytes))
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        try:
            return canonical_bytes(self)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError(_ERROR) from None


def _bound(proof, request, request_type):
    try:
        result = _checked(proof, type(proof))
        original = _checked(request, request_type)
        if canonical_bytes(result.request) != canonical_bytes(original):
            raise ValueError(_PROOF_ERROR)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_PROOF_ERROR) from None


class StandingProducerRenewal(_Dispatch):
    """Renewal identity only; the native controller owns the monotonic clock."""

    expected_backend: BackendId
    expected_instance: ControlInstance
    producer_id: ControlInstance
    sequence: PositiveInteger


class StandingProducerReceipt(_Dispatch):
    """An acknowledged renewal packet, without a claim of current liveness."""

    wire_bytes: ClassVar[int] = 8192
    request: StandingProducerRenewal
    lease_token: ControlInstance
    issued_monotonic_ms: Annotated[
        int, Field(ge=0, le=MAX_AGENT_CONTROL_INTEGER - STANDING_PRODUCER_TTL_MS)
    ]
    expires_monotonic_ms: Annotated[
        int, Field(ge=STANDING_PRODUCER_TTL_MS, le=MAX_AGENT_CONTROL_INTEGER)
    ]

    @model_validator(mode="after")
    def lease_interval(self):
        if self.expires_monotonic_ms - self.issued_monotonic_ms != STANDING_PRODUCER_TTL_MS:
            raise ValueError(_PROOF_ERROR)
        return self

    def validate_request(self, original: StandingProducerRenewal) -> Self:
        return _bound(self, original, StandingProducerRenewal)


class StandingDispatchState(_Dispatch):
    """A native gate observation; permissions and occurrence slots are separate."""

    wire_bytes: ClassVar[int] = 16384
    backend_id: BackendId
    instance_id: ControlInstance | None
    control_revision: Revision
    mode: AgentMode
    control_writable: bool
    epoch: _Epoch
    stopped_through_epoch: _Epoch
    fence_token: ControlInstance
    activation_digest: Digest | None
    activation_control_revision: PositiveInteger | None
    producer: StandingProducerReceipt | None
    dispatch_ready: bool

    @model_validator(mode="after")
    def gate_observation(self):
        if self.instance_id is None and (self.control_writable or self.producer is not None):
            raise ValueError(_PROOF_ERROR)
        if self.stopped_through_epoch > self.epoch:
            raise ValueError(_PROOF_ERROR)
        if self.epoch == 0:
            if self.activation_digest is not None or self.activation_control_revision is not None:
                raise ValueError(_PROOF_ERROR)
        elif (
            self.activation_digest is None
            or self.activation_control_revision is None
            or self.activation_control_revision > self.control_revision
        ):
            raise ValueError(_PROOF_ERROR)
        if self.producer is not None and (
            self.producer.request.expected_backend != self.backend_id
            or self.producer.request.expected_instance != self.instance_id
        ):
            raise ValueError(_PROOF_ERROR)
        ready = (
            self.control_writable
            and self.mode == "autonomous"
            and self.epoch > self.stopped_through_epoch
            and self.activation_control_revision == self.control_revision
            and self.producer is not None
        )
        if self.dispatch_ready != ready:
            raise ValueError(_PROOF_ERROR)
        return self


class StandingDispatchActivation(_Dispatch):
    """Explicit owner activation with the original mode key and observed fence."""

    mode_change: AgentModeChange
    expected_epoch: Annotated[int, Field(ge=0, lt=MAX_STANDING_DISPATCH_EPOCH)]
    expected_fence_token: ControlInstance

    @model_validator(mode="after")
    def autonomous_mode(self):
        if self.mode_change.mode != "autonomous":
            raise ValueError(_PROOF_ERROR)
        return self


def activation_digest(request: StandingDispatchActivation, epoch: int) -> str:
    """Bind the entire owner envelope and exactly its next dispatch epoch."""
    try:
        original = _checked(request, StandingDispatchActivation)
        if type(epoch) is not int or epoch != original.expected_epoch + 1:
            raise ValueError(_ERROR)
        body = {"epoch": epoch, "request": original}
        return hashlib.sha256(
            b"kinaiya.standing.dispatch.activation.v1\0" + _encode(body)
        ).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


class StandingDispatchActivationReceipt(_Dispatch):
    """Full historical activation proof; replay never rearms dispatch."""

    wire_bytes: ClassVar[int] = 16384
    request: StandingDispatchActivation
    mode_receipt: AgentModeReceipt
    epoch: Annotated[int, Field(ge=1, le=MAX_STANDING_DISPATCH_EPOCH)]
    activation_digest: Digest

    @model_validator(mode="after")
    def activation_proof(self):
        # The outer closed-tree check precedes the older mode models' dumps,
        # so forbidden fields in copied nested mode proofs cannot disappear.
        if (
            canonical_bytes(self.mode_receipt.request) != canonical_bytes(self.request.mode_change)
            or self.epoch != self.request.expected_epoch + 1
            or self.activation_digest != activation_digest(self.request, self.epoch)
        ):
            raise ValueError(_PROOF_ERROR)
        return self

    def validate_request(self, original: StandingDispatchActivation) -> Self:
        return _bound(self, original, StandingDispatchActivation)


class StandingDispatchActivationLookup(_Dispatch):
    """Full-request observation; only native history can prove retired authority."""

    wire_bytes: ClassVar[int] = MAX_STANDING_DISPATCH_RESPONSE_BYTES
    status: Literal["recorded", "unobserved"]
    request: StandingDispatchActivation
    state: StandingDispatchState
    receipt: StandingDispatchActivationReceipt | None
    mutation_fenced: bool

    @model_validator(mode="after")
    def activation_observation(self):
        original = self.request.mode_change
        if (
            self.state.backend_id != original.expected_backend
            or self.state.control_revision < original.expected_revision
            or self.state.epoch < self.request.expected_epoch
        ):
            raise ValueError(_PROOF_ERROR)
        if self.status == "recorded":
            if self.receipt is None:
                raise ValueError(_PROOF_ERROR)
            self.receipt.validate_request(self.request)
            if (
                self.state.epoch < self.receipt.epoch
                or self.state.control_revision < self.receipt.mode_receipt.revision
                or (
                    self.state.epoch == self.receipt.epoch
                    and (
                        self.state.activation_digest != self.receipt.activation_digest
                        or self.state.activation_control_revision
                        != self.receipt.mode_receipt.revision
                    )
                )
            ):
                raise ValueError(_PROOF_ERROR)
        elif self.receipt is not None:
            raise ValueError(_PROOF_ERROR)
        fenced = (
            self.receipt is not None
            or self.state.instance_id != original.expected_instance
            or self.state.control_revision > original.expected_revision
            or self.state.epoch > self.request.expected_epoch
            or self.state.fence_token != self.request.expected_fence_token
        )
        if self.mutation_fenced != fenced:
            raise ValueError(_PROOF_ERROR)
        return self

    def validate_request(self, original: StandingDispatchActivation) -> Self:
        return _bound(self, original, StandingDispatchActivation)


class StandingDispatchStop(_Dispatch):
    """Target one issued epoch; a retry cannot disable a later activation."""

    expected_backend: BackendId
    expected_instance: ControlInstance
    issued_epoch: _Epoch
    activation_digest: Digest | None
    expected_fence_token: ControlInstance

    @model_validator(mode="after")
    def issued_activation(self):
        if (self.issued_epoch == 0) != (self.activation_digest is None):
            raise ValueError(_PROOF_ERROR)
        return self


class StandingDispatchStopProof(_Dispatch):
    """Only this target is fenced; admitted workers may still be unsettled."""

    wire_bytes: ClassVar[int] = MAX_STANDING_DISPATCH_RESPONSE_BYTES
    request: StandingDispatchStop
    state: StandingDispatchState
    target_fenced: Literal[True]

    @field_validator("target_fenced", mode="before")
    @classmethod
    def literal_true(cls, value):
        if value is not True:
            raise ValueError(_PROOF_ERROR)
        return value

    @model_validator(mode="after")
    def target_proof(self):
        if (
            self.state.backend_id != self.request.expected_backend
            or self.state.epoch < self.request.issued_epoch
            or self.state.stopped_through_epoch < self.request.issued_epoch
            or (
                self.state.epoch == self.request.issued_epoch
                and self.state.activation_digest != self.request.activation_digest
            )
        ):
            raise ValueError(_PROOF_ERROR)
        return self

    def validate_request(self, original: StandingDispatchStop) -> Self:
        return _bound(self, original, StandingDispatchStop)
