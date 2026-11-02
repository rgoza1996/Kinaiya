"""Codex-authored permanent admission-fence metadata; never settlement proof."""

import hashlib
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, field_validator, model_validator

from packages.contracts.agent_control import Digest
from packages.contracts.models import CatalogModelBinding
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_dispatch import StandingProducerReceipt, StandingProducerRenewal
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionIntent,
    StandingExecutionModelPin,
    StandingExecutionTask,
    StandingOccurrence,
    claim_id,
    intent_digest,
)
from packages.contracts.standing_work import (
    StandingTaskSession,
    _checked,
    _Closed,
    _encode,
    _tree,
    canonical_bytes,
    standing_json,
)
from packages.contracts.workspace_settings import WorkspacePin

MAX_STANDING_ADMISSION_CLOSURE_BYTES = 180224
_ERROR = "Invalid standing admission closure"
_FAILURES = (ValueError, TypeError, UnicodeError, RecursionError, OverflowError)


def _actual(value):
    # Inspect exact classes and actual fields before any older serializer can
    # drop extras or normalize a copied/constructed nested model.
    _tree(value)
    kinds = {
        StandingAdmissionClosure,
        StandingInitialProof,
        StandingExecutionClaim,
        StandingExecutionIntent,
        StandingExecutionModelPin,
        StandingExecutionTask,
        StandingOccurrence,
        StandingProducerReceipt,
        StandingProducerRenewal,
        StandingTaskSession,
        WorkspacePin,
        CatalogModelBinding,
    }
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, BaseModel):
            if type(item) not in kinds:
                raise ValueError(_ERROR)
            item = item.__dict__
        if type(item) is dict:
            pending.extend(item.values())
        elif type(item) is tuple:
            raise ValueError(_ERROR)
        elif type(item) is list:
            pending.extend(item)


def _original(value):
    if type(value) is not StandingExecutionIntent:
        raise ValueError(_ERROR)
    _actual(value)
    return _checked(value, StandingExecutionIntent)


def _digest(intent, claim, initial):
    return hashlib.sha256(
        b"kinaiya.standing.admission-closure.v1\0"
        + _encode(
            {
                "schema_version": "1.0",
                "intent_digest": intent,
                "claim_id": claim,
                "initial": initial,
            }
        )
    ).hexdigest()


def standing_closure_digest(
    original: StandingExecutionIntent, initial: StandingInitialProof | None
) -> str:
    """Hash a complete original/proof projection, without native authority."""
    try:
        original = _original(original)
        if initial is not None:
            if type(initial) is not StandingInitialProof:
                raise ValueError(_ERROR)
            _actual(initial)
            initial = initial.validate_request(original)
        return _digest(intent_digest(original), claim_id(original), initial)
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingAdmissionClosure(_Closed):
    """A canonical closure proposal; parsing cannot prove a native COMMIT."""

    wire_bytes: ClassVar[int] = MAX_STANDING_ADMISSION_CLOSURE_BYTES
    schema_version: Literal["1.0"]
    intent_digest: Digest
    claim_id: Digest
    initial: StandingInitialProof | None
    closure_digest: Digest

    @model_validator(mode="wrap")
    @classmethod
    def actual_graph(cls, value, handler):
        try:
            if isinstance(value, BaseModel) and type(value) is not cls:
                raise ValueError(_ERROR)
            _actual(value)
            return handler(value)
        except _FAILURES:
            raise ValueError(_ERROR) from None

    @field_validator("initial", mode="before")
    @classmethod
    def exact_initial(cls, value):
        if value is None:
            return None
        _actual(value)
        if isinstance(value, BaseModel):
            if type(value) is not StandingInitialProof:
                raise ValueError(_ERROR)
            value = dict(value.__dict__)
        if type(value) is not dict or value.keys() != StandingInitialProof.model_fields.keys():
            raise ValueError(_ERROR)
        return StandingInitialProof.model_validate(value)

    @model_validator(mode="after")
    def exact_digest(self):
        if self.initial is not None and (
            self.initial.claim.claim_id != self.claim_id
            or self.initial.claim.intent_digest != self.intent_digest
        ):
            raise ValueError(_ERROR)
        if self.closure_digest != _digest(self.intent_digest, self.claim_id, self.initial):
            raise ValueError(_ERROR)
        return self

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            if cls is not StandingAdmissionClosure:
                raise ValueError(_ERROR)
            return cls.model_validate(standing_json(raw, maximum=cls.wire_bytes))
        except _FAILURES:
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        try:
            if type(self) is not StandingAdmissionClosure:
                raise ValueError(_ERROR)
            _actual(self)
            return canonical_bytes(self)
        except _FAILURES:
            raise ValueError(_ERROR) from None

    def validate_request(self, original: StandingExecutionIntent) -> Self:
        try:
            if type(self) is not StandingAdmissionClosure:
                raise ValueError(_ERROR)
            _actual(self)
            result = _checked(self, StandingAdmissionClosure)
            original = _original(original)
            if (
                result.intent_digest != intent_digest(original)
                or result.claim_id != claim_id(original)
                or result.closure_digest != standing_closure_digest(original, result.initial)
            ):
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None
