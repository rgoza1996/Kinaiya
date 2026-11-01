"""Pure prework Resources binding and finalization packets, never cleanup proof."""

import hashlib
import json
from functools import lru_cache
from typing import Annotated, ClassVar, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from packages.contracts.agent_control import (
    AgentTaskIdentity,
    BackendId,
    ControlInstance,
    ControlKey,
    Digest,
    Revision,
)
from packages.contracts.models import CatalogModelBinding
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_closure import StandingAdmissionClosure
from packages.contracts.standing_dispatch import StandingProducerReceipt, StandingProducerRenewal
from packages.contracts.standing_execution import (
    StandingExecutionClaim,
    StandingExecutionIntent,
    StandingExecutionModelPin,
    StandingExecutionTask,
    StandingOccurrence,
)
from packages.contracts.standing_resources import (
    StandingResourceObservation,
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    TaskResourceContext,
    TaskResourceQuery,
)
from packages.contracts.standing_work import StandingTaskSession
from packages.contracts.workspace_settings import WorkspacePin

MAX_STANDING_RESOURCE_BINDING_BYTES = 163840
MAX_STANDING_RESOURCE_BINDING_RECEIPT_BYTES = 180224
MAX_STANDING_RESOURCE_FINALIZE_REQUEST_BYTES = 147456
MAX_STANDING_RESOURCE_FINALIZE_CONTEXT_BYTES = 786432
MAX_STANDING_RESOURCE_FINALIZATION_BYTES = 802816
MAX_STANDING_RESOURCE_FINALIZATION_RECEIPT_BYTES = 819200

_ERROR = "Invalid standing resource finalization contract"
_FAILURES = (ValueError, TypeError, UnicodeError, RecursionError, OverflowError)
_MAX_INTEGER = 9_007_199_254_740_991
_MAX_DEPTH = 16
_MAX_NODES = 16384
_ActionKey = Annotated[str, Field(min_length=1, max_length=256)]
_Disposition = Literal["no-reservation", "registered-no-lease", "matching-lease-finalized"]
_Restoration = Literal["not-required", "restored", "owner-off"]


def _graph(value):
    """Inspect actual closure before any old model can dump or normalize it."""
    pending = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError(_ERROR)
        if isinstance(item, BaseModel):
            kind = type(item)
            if kind not in _KINDS:
                raise ValueError(_ERROR)
            fields = kind.model_fields
            values = getattr(item, "__dict__", None)
            supplied = getattr(item, "__pydantic_fields_set__", None)
            if (
                type(values) is not dict
                or values.keys() != fields.keys()
                or type(supplied) is not set
                or supplied != fields.keys()
                or getattr(item, "__pydantic_extra__", {}) is not None
            ):
                raise ValueError(_ERROR)
            item = values
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError(_ERROR)
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            item.encode("utf-8")
        elif type(item) is int:
            if abs(item) > _MAX_INTEGER:
                raise ValueError(_ERROR)
        elif item is not None and type(item) is not bool:
            raise ValueError(_ERROR)


def _encode(value):
    def actual(item):
        if isinstance(item, BaseModel):
            return item.__dict__
        raise ValueError(_ERROR)

    _graph(value)
    raw = json.dumps(
        value,
        default=actual,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > MAX_STANDING_RESOURCE_FINALIZATION_RECEIPT_BYTES:
        raise ValueError(_ERROR)
    return raw


def _json(raw, maximum):
    if (
        type(raw) is not bytes
        or type(maximum) is not int
        or not 1 <= maximum <= MAX_STANDING_RESOURCE_FINALIZATION_RECEIPT_BYTES
        or len(raw) > maximum
    ):
        raise ValueError(_ERROR)

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError(_ERROR)
            value[key] = item
        return value

    def invalid(_):
        raise ValueError(_ERROR)

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_float=invalid,
        parse_constant=invalid,
    )
    if type(value) is not dict:
        raise ValueError(_ERROR)
    _graph(value)
    return value


@lru_cache(maxsize=256)
def _adapter(kind, name):
    return TypeAdapter(kind.model_fields[name].rebuild_annotation())


def _model_kind(annotation):
    candidates = (annotation, *get_args(annotation))
    return next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, type) and issubclass(candidate, BaseModel)
        ),
        None,
    )


def _fields(value, kind):
    _graph(value)
    if isinstance(value, BaseModel):
        if type(value) is not kind:
            raise ValueError(_ERROR)
        value = value.__dict__
    if type(value) is not dict or value.keys() != kind.model_fields.keys():
        raise ValueError(_ERROR)
    result = {}
    for name, field in kind.model_fields.items():
        child = value[name]
        nested = _model_kind(field.annotation)
        if nested is not None and child is not None:
            # Hand the native validator a complete dictionary, never an old
            # instance that its own nested validator might trust unchanged.
            result[name] = _fields(child, nested)
        else:
            result[name] = _adapter(kind, name).validate_python(child, strict=True)
    return result


def _complete(value, kind):
    if kind not in _KINDS:
        raise ValueError(_ERROR)
    if kind in _PACKETS:
        return kind.model_validate(value, strict=True)
    # Rebuild old children from complete actual fields. In particular, older
    # models without instance revalidation must never accept a forged copy.
    return kind.model_validate(_fields(value, kind), strict=True)


def checked_finalization(value, kind):
    """Revalidate one exact supported old or new packet without a lossy dump."""
    try:
        if type(value) is not kind:
            raise ValueError(_ERROR)
        return _complete(value, kind)
    except _FAILURES:
        raise ValueError(_ERROR) from None


def finalization_bytes(value):
    """Canonicalize revalidated packets using only the new-family codec."""
    try:
        return _encode(checked_finalization(value, type(value)))
    except _FAILURES:
        raise ValueError(_ERROR) from None


def _hash(domain, body):
    return hashlib.sha256(domain + b"\0" + _encode(body)).hexdigest()


class _Finalization(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
        hide_input_in_errors=True,
    )
    schema_version: Literal["1.0"]
    wire_bytes: ClassVar[int]

    def __setattr__(self, name, value):
        raise ValueError(_ERROR) from None

    def __delattr__(self, name):
        raise ValueError(_ERROR) from None

    @model_validator(mode="wrap")
    @classmethod
    def complete_graph(cls, value, handler):
        try:
            if cls not in _PACKETS:
                raise ValueError(_ERROR)
            result = handler(_fields(value, cls))
            if len(_encode(result)) > cls.wire_bytes:
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            if cls not in _PACKETS:
                raise ValueError(_ERROR)
            return cls.model_validate(_json(raw, cls.wire_bytes), strict=True)
        except _FAILURES:
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        return finalization_bytes(self)


class StandingResourceBindingRequest(_Finalization):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_BINDING_BYTES
    claim: StandingExecutionClaim
    resource_backend: BackendId
    resource_instance: ControlInstance


def resource_binding_digest(request):
    try:
        request = checked_finalization(request, StandingResourceBindingRequest)
        return _hash(b"kinaiya.standing.resource-binding.v1", request)
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingResourceBindingReceipt(_Finalization):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_BINDING_RECEIPT_BYTES
    disposition: Literal["created", "retained"]
    request: StandingResourceBindingRequest
    request_digest: Digest

    @model_validator(mode="after")
    def full_digest(self):
        if self.request_digest != _hash(b"kinaiya.standing.resource-binding.v1", self.request):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: StandingResourceBindingRequest) -> Self:
        try:
            result = checked_finalization(self, StandingResourceBindingReceipt)
            original = checked_finalization(original, StandingResourceBindingRequest)
            if _encode(result.request) != _encode(original):
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


class StandingResourceFinalizeRequest(_Finalization):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_FINALIZE_REQUEST_BYTES
    idempotency_key: ControlKey
    original: StandingExecutionIntent
    expected_resource_backend: BackendId
    expected_resource_instance: ControlInstance


def _context_body(request, binding, closure, observation, *, validated=False):
    if not validated:
        request = checked_finalization(request, StandingResourceFinalizeRequest)
        binding = checked_finalization(binding, StandingResourceBindingRequest)
        closure = checked_finalization(closure, StandingAdmissionClosure)
        observation = checked_finalization(observation, StandingResourceObservation)
    original = request.original
    closure.validate_request(original)
    observation.validate_request(original)
    initial = closure.initial
    reservation = observation.reservation
    if (
        _encode(binding.claim.request) != _encode(original)
        or request.expected_resource_backend != binding.resource_backend
        or request.expected_resource_instance != binding.resource_instance
        or observation.closure_digest != closure.closure_digest
        or observation.accepted_sequence != (None if initial is None else initial.sequence)
        or initial is not None
        and _encode(initial.claim) != _encode(binding.claim)
    ):
        raise ValueError(_ERROR)
    if reservation is not None and (
        initial is None
        or reservation.context.family != "standing"
        or _encode(reservation.context.standing) != _encode(initial)
        or _encode(reservation.context.standing.claim) != _encode(binding.claim)
        or reservation.resource_backend != binding.resource_backend
        or reservation.resource_instance != binding.resource_instance
    ):
        raise ValueError(_ERROR)
    return {
        "schema_version": "1.0",
        "request": request,
        "binding": binding,
        "closure": closure,
        "observation": observation,
    }


def resource_finalize_context_digest(request, binding, closure, observation):
    try:
        return _hash(
            b"kinaiya.standing.resource-finalize-context.v1",
            _context_body(request, binding, closure, observation),
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingResourceFinalizeContext(_Finalization):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_FINALIZE_CONTEXT_BYTES
    request: StandingResourceFinalizeRequest
    binding: StandingResourceBindingRequest
    closure: StandingAdmissionClosure
    observation: StandingResourceObservation
    context_digest: Digest

    @model_validator(mode="after")
    def full_context(self):
        if self.context_digest != _hash(
            b"kinaiya.standing.resource-finalize-context.v1",
            _context_body(
                self.request, self.binding, self.closure, self.observation, validated=True
            ),
        ):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: StandingResourceFinalizeRequest) -> Self:
        try:
            result = checked_finalization(self, StandingResourceFinalizeContext)
            original = checked_finalization(original, StandingResourceFinalizeRequest)
            if _encode(result.request) != _encode(original):
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


def _finalization_body(
    context,
    resource_disposition,
    handoff_action_key,
    handoff_action_digest,
    reserved_lease_id,
    policy_revision,
    owner_intent_revision,
    restoration,
    *,
    validated=False,
):
    if not validated:
        context = checked_finalization(context, StandingResourceFinalizeContext)
    fields = {
        "resource_disposition": resource_disposition,
        "handoff_action_key": handoff_action_key,
        "handoff_action_digest": handoff_action_digest,
        "reserved_lease_id": reserved_lease_id,
        "policy_revision": policy_revision,
        "owner_intent_revision": owner_intent_revision,
        "restoration": restoration,
    }
    if not validated:
        fields = {
            name: _adapter(StandingResourceFinalization, name).validate_python(value, strict=True)
            for name, value in fields.items()
        }
    reservation = context.observation.reservation
    if fields["resource_disposition"] == "no-reservation":
        if (
            reservation is not None
            or any(
                fields[name] is not None
                for name in ("handoff_action_key", "handoff_action_digest", "reserved_lease_id")
            )
            or fields["restoration"] != "not-required"
        ):
            raise ValueError(_ERROR)
    elif (
        reservation is None
        or fields["handoff_action_key"] is None
        or fields["handoff_action_digest"] != reservation.action_digest
        or fields["reserved_lease_id"] != reservation.lease_id
        or fields["resource_disposition"] == "matching-lease-finalized"
        and fields["restoration"] == "not-required"
    ):
        raise ValueError(_ERROR)
    return {"schema_version": "1.0", "context": context, **fields}


def resource_finalization_digest(
    context,
    resource_disposition,
    handoff_action_key,
    handoff_action_digest,
    reserved_lease_id,
    policy_revision,
    owner_intent_revision,
    restoration,
):
    try:
        return _hash(
            b"kinaiya.standing.resource-finalization.v1",
            _finalization_body(
                context,
                resource_disposition,
                handoff_action_key,
                handoff_action_digest,
                reserved_lease_id,
                policy_revision,
                owner_intent_revision,
                restoration,
            ),
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingResourceFinalization(_Finalization):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_FINALIZATION_BYTES
    context: StandingResourceFinalizeContext
    resource_disposition: _Disposition
    handoff_action_key: _ActionKey | None
    handoff_action_digest: Digest | None
    reserved_lease_id: ControlInstance | None
    policy_revision: Revision
    owner_intent_revision: Revision
    restoration: _Restoration
    finalization_digest: Digest

    @model_validator(mode="after")
    def full_finalization(self):
        if self.finalization_digest != _hash(
            b"kinaiya.standing.resource-finalization.v1",
            _finalization_body(
                self.context,
                self.resource_disposition,
                self.handoff_action_key,
                self.handoff_action_digest,
                self.reserved_lease_id,
                self.policy_revision,
                self.owner_intent_revision,
                self.restoration,
                validated=True,
            ),
        ):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: StandingResourceFinalizeRequest) -> Self:
        try:
            result = checked_finalization(self, StandingResourceFinalization)
            original = checked_finalization(original, StandingResourceFinalizeRequest)
            if _encode(result.context.request) != _encode(original):
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


class StandingResourceFinalizationReceipt(_Finalization):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_FINALIZATION_RECEIPT_BYTES
    disposition: Literal["created", "retained"]
    finalization: StandingResourceFinalization

    def validate_request(self, original: StandingResourceFinalizeRequest) -> Self:
        try:
            result = checked_finalization(self, StandingResourceFinalizationReceipt)
            original = checked_finalization(original, StandingResourceFinalizeRequest)
            if _encode(result.finalization.context.request) != _encode(original):
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


_PACKETS = (
    StandingResourceBindingRequest,
    StandingResourceBindingReceipt,
    StandingResourceFinalizeRequest,
    StandingResourceFinalizeContext,
    StandingResourceFinalization,
    StandingResourceFinalizationReceipt,
)
_KINDS = (
    *_PACKETS,
    AgentTaskIdentity,
    CatalogModelBinding,
    WorkspacePin,
    StandingTaskSession,
    StandingOccurrence,
    StandingExecutionModelPin,
    StandingExecutionTask,
    StandingProducerRenewal,
    StandingProducerReceipt,
    StandingExecutionIntent,
    StandingExecutionClaim,
    StandingInitialProof,
    StandingAdmissionClosure,
    TaskResourceQuery,
    TaskResourceContext,
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    StandingResourceObservation,
)
