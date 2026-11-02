"""Pure original-Agent registration and settlement grammar, never retirement proof.

Native provenance, secret possession and original worker/transport retirement
must be established by their protected owners, not by these packet fields.
"""

import hashlib
import json
from functools import lru_cache
from typing import ClassVar, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, TypeAdapter, field_validator, model_validator

from packages.contracts.agent_control import (
    AgentTaskIdentity,
    BackendId,
    ControlInstance,
    ControlKey,
    Digest,
    PositiveInteger,
    SafeId,
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
from packages.contracts.standing_resource_finalization import (
    StandingResourceBindingReceipt,
    StandingResourceBindingRequest,
    StandingResourceFinalization,
    StandingResourceFinalizationReceipt,
    StandingResourceFinalizeContext,
    StandingResourceFinalizeRequest,
    checked_finalization,
)
from packages.contracts.standing_resources import (
    StandingResourceObservation,
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    TaskResourceContext,
    TaskResourceQuery,
)
from packages.contracts.standing_work import StandingTaskSession, _utc
from packages.contracts.workspace_settings import WorkspacePin

MAX_STANDING_AGENT_RETIREMENT_REGISTRATION_REQUEST_BYTES = 148480
MAX_STANDING_AGENT_RETIREMENT_REGISTRATION_RECORD_BYTES = 149504
MAX_STANDING_AGENT_RETIREMENT_REGISTRATION_RECEIPT_BYTES = 149632
MAX_STANDING_AGENT_OUTCOME_REFERENCE_BYTES = 2048
MAX_STANDING_AGENT_RETIREMENT_WORK_BYTES = 3072
MAX_STANDING_AGENT_SETTLEMENT_REQUEST_BYTES = 972800
MAX_STANDING_AGENT_SETTLEMENT_RECORD_BYTES = 973824
MAX_STANDING_AGENT_SETTLEMENT_RECEIPT_BYTES = 973952
MAX_STANDING_AGENT_SETTLEMENT_QUERY_BYTES = 149760
MAX_STANDING_AGENT_SETTLEMENT_JSON_DEPTH = 20
MAX_STANDING_AGENT_SETTLEMENT_JSON_NODES = 20480

_ERROR = "Invalid standing Agent settlement contract"
_FAILURES = (ValueError, TypeError, UnicodeError, RecursionError, OverflowError)
_MAX_INTEGER = 9_007_199_254_740_991
_ORIGIN = Literal["fresh-v2-prepared"]
_DISPOSITION = Literal["created", "retained"]
_WORK = Literal[
    "launched-outcome-known",
    "launched-outcome-unconfirmed",
    "created-unlaunched",
    "bound-never-admitted",
]


def _graph(value):
    """Check actual metadata before a nested old model can normalize it."""
    pending = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if (
            depth > MAX_STANDING_AGENT_SETTLEMENT_JSON_DEPTH
            or nodes > MAX_STANDING_AGENT_SETTLEMENT_JSON_NODES
        ):
            raise ValueError(_ERROR)
        kind = type(item)
        if isinstance(item, BaseModel):
            if kind not in _KINDS:
                raise ValueError(_ERROR)
            fields = kind.model_fields
            values = getattr(item, "__dict__", None)
            supplied = getattr(item, "__pydantic_fields_set__", None)
            if (
                type(values) is not dict
                or values.keys() != fields.keys()
                or type(supplied) is not set
                or any(type(name) is not str for name in supplied)
                or supplied != fields.keys()
                or getattr(item, "__pydantic_extra__", {}) is not None
                or getattr(item, "__pydantic_private__", None) is not None
            ):
                raise ValueError(_ERROR)
            item, kind = values, dict
        if kind is dict:
            if any(type(key) is not str for key in item):
                raise ValueError(_ERROR)
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif kind is list:
            pending.extend((child, depth + 1) for child in item)
        elif kind is str:
            item.encode("utf-8")
        elif kind is int:
            if abs(item) > _MAX_INTEGER:
                raise ValueError(_ERROR)
        elif item is not None and kind is not bool:
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
    if len(raw) > MAX_STANDING_AGENT_SETTLEMENT_RECEIPT_BYTES:
        raise ValueError(_ERROR)
    return raw


def _integer(token):
    # Refuse long or noninteger tokens before json.loads can construct them.
    digits = token[1:] if token.startswith("-") else token
    if (
        not digits
        or len(digits) > 16
        or any(char not in "0123456789" for char in digits)
        or len(digits) > 1
        and digits[0] == "0"
    ):
        raise ValueError(_ERROR)
    value = int(token)
    if abs(value) > _MAX_INTEGER:
        raise ValueError(_ERROR)
    return value


def _lexical(text):
    """Bound tokens/containers before recursively parsing untrusted JSON."""
    stack = []
    nodes = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in " \t\r\n,:":
            index += 1
            continue
        if char in "]}":
            if not stack or stack.pop() != ("]" if char == "]" else "}"):
                raise ValueError(_ERROR)
            index += 1
            continue
        nodes += 1
        if (
            nodes > MAX_STANDING_AGENT_SETTLEMENT_JSON_NODES
            or len(stack) + 1 > MAX_STANDING_AGENT_SETTLEMENT_JSON_DEPTH
        ):
            raise ValueError(_ERROR)
        if char in "[{":
            stack.append("]" if char == "[" else "}")
            index += 1
        elif char == '"':
            index += 1
            while index < len(text):
                char = text[index]
                index += 1
                if char == '"':
                    break
                if ord(char) < 32:
                    raise ValueError(_ERROR)
                if char == "\\":
                    if index == len(text):
                        raise ValueError(_ERROR)
                    escape = text[index]
                    index += 1
                    if escape == "u":
                        digits = text[index : index + 4]
                        if len(digits) != 4 or any(
                            part not in "0123456789abcdefABCDEF" for part in digits
                        ):
                            raise ValueError(_ERROR)
                        index += 4
                    elif escape not in '"\\/bfnrt':
                        raise ValueError(_ERROR)
            else:
                raise ValueError(_ERROR)
        elif char == "-" or char in "0123456789":
            start = index
            while index < len(text) and text[index] not in " \t\r\n,:[]{}":
                index += 1
            _integer(text[start:index])
        else:
            token = next(
                (item for item in ("true", "false", "null") if text.startswith(item, index)),
                None,
            )
            if token is None:
                raise ValueError(_ERROR)
            index += len(token)
    if stack or not nodes:
        raise ValueError(_ERROR)


def _json(raw, maximum):
    if (
        type(raw) is not bytes
        or type(maximum) is not int
        or not 1 <= maximum <= MAX_STANDING_AGENT_SETTLEMENT_RECEIPT_BYTES
        or len(raw) > maximum
    ):
        raise ValueError(_ERROR)
    text = raw.decode("utf-8")
    _lexical(text)

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError(_ERROR)
            result[key] = item
        return result

    def invalid(_):
        raise ValueError(_ERROR)

    value = json.loads(
        text,
        object_pairs_hook=pairs,
        parse_int=_integer,
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
    return next(
        (
            candidate
            for candidate in (annotation, *get_args(annotation))
            if isinstance(candidate, type) and issubclass(candidate, BaseModel)
        ),
        None,
    )


def _fields(value, kind):
    """Rebuild complete fields after the caller's whole actual-graph walk."""
    typed = isinstance(value, BaseModel)
    if typed:
        if type(value) is not kind:
            raise ValueError(_ERROR)
        value = value.__dict__
    if kind not in _KINDS or type(value) is not dict or value.keys() != kind.model_fields.keys():
        raise ValueError(_ERROR)
    result = {}
    for name, field in kind.model_fields.items():
        child = value[name]
        nested = _model_kind(field.annotation)
        if nested is not None and child is not None:
            if typed and type(child) is not nested:
                raise ValueError(_ERROR)
            result[name] = _fields(child, nested)
        else:
            result[name] = _adapter(kind, name).validate_python(child, strict=True)
    return result


def checked_agent_settlement(value, kind):
    """Freshly validate and detach one exact supported new settlement packet."""
    try:
        if kind not in _PACKETS or type(value) is not kind:
            raise ValueError(_ERROR)
        return kind.model_validate(value, strict=True)
    except _FAILURES:
        raise ValueError(_ERROR) from None


def agent_settlement_bytes(value):
    """Canonical bytes always include fresh complete semantic validation."""
    try:
        return _encode(checked_agent_settlement(value, type(value)))
    except _FAILURES:
        raise ValueError(_ERROR) from None


def _hash(domain, body):
    return hashlib.sha256(domain + b"\0" + _encode(body)).hexdigest()


def _scalar(kind, name, value):
    _graph(value)
    return _adapter(kind, name).validate_python(value, strict=True)


class _Settlement(BaseModel):
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
            _graph(value)
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
        return agent_settlement_bytes(self)


def agent_retirement_capability_digest(claim, owner_id, registration_key, secret32):
    """Purpose-separated commitment; the transient secret is never serialized."""
    try:
        claim = checked_finalization(claim, StandingExecutionClaim)
        owner_id = _scalar(StandingAgentRetirementRegistrationRequest, "owner_id", owner_id)
        registration_key = _scalar(
            StandingAgentRetirementRegistrationRequest, "idempotency_key", registration_key
        )
        if type(secret32) is not bytes or len(secret32) != 32:
            raise ValueError(_ERROR)
        body = {
            "backend_id": claim.request.expected_backend,
            "claim_id": claim.claim_id,
            "intent_digest": claim.intent_digest,
            "executor_id": claim.request.executor_id,
            "owner_id": owner_id,
            "registration_key": registration_key,
        }
        return hashlib.sha256(
            b"kinaiya.standing.agent-retirement-capability.v1\0"
            + _encode(body)
            + b"\0"
            + secret32
        ).hexdigest()
    except _FAILURES:
        raise ValueError(_ERROR) from None


def _registration_body(claim, registration_key, owner_id, capability_sha256, *, validated=False):
    if not validated:
        claim = checked_finalization(claim, StandingExecutionClaim)
        registration_key = _scalar(
            StandingAgentRetirementRegistrationRequest, "idempotency_key", registration_key
        )
        owner_id = _scalar(StandingAgentRetirementRegistrationRequest, "owner_id", owner_id)
        capability_sha256 = _scalar(
            StandingAgentRetirementRegistrationRequest, "capability_sha256", capability_sha256
        )
    return {
        "schema_version": "1.0",
        "claim": claim,
        "idempotency_key": registration_key,
        "owner_id": owner_id,
        "capability_sha256": capability_sha256,
    }


def agent_retirement_registration_digest(claim, registration_key, owner_id, capability_sha256):
    try:
        return _hash(
            b"kinaiya.standing.agent-retirement-registration.v1",
            _registration_body(claim, registration_key, owner_id, capability_sha256),
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingAgentRetirementRegistrationRequest(_Settlement):
    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_RETIREMENT_REGISTRATION_REQUEST_BYTES
    claim: StandingExecutionClaim
    idempotency_key: ControlKey
    owner_id: ControlInstance
    capability_sha256: Digest
    registration_digest: Digest

    @model_validator(mode="after")
    def full_registration(self):
        if self.registration_digest != _hash(
            b"kinaiya.standing.agent-retirement-registration.v1",
            _registration_body(
                self.claim,
                self.idempotency_key,
                self.owner_id,
                self.capability_sha256,
                validated=True,
            ),
        ):
            raise ValueError(_ERROR)
        return self


def _registration_record_body(
    request, backend_id, registered_instance, registered_utc, claim_origin, *, validated=False
):
    if not validated:
        request = checked_agent_settlement(request, StandingAgentRetirementRegistrationRequest)
        fields = {
            "backend_id": backend_id,
            "registered_instance": registered_instance,
            "registered_utc": registered_utc,
            "claim_origin": claim_origin,
        }
        fields = {
            name: _scalar(StandingAgentRetirementRegistrationRecord, name, value)
            for name, value in fields.items()
        }
        backend_id = fields["backend_id"]
        registered_instance = fields["registered_instance"]
        registered_utc = fields["registered_utc"]
        claim_origin = fields["claim_origin"]
    _utc(registered_utc)
    if backend_id != request.claim.request.expected_backend:
        raise ValueError(_ERROR)
    return {
        "schema_version": "1.0",
        "request": request,
        "backend_id": backend_id,
        "registered_instance": registered_instance,
        "registered_utc": registered_utc,
        "claim_origin": claim_origin,
    }


def agent_retirement_registration_record_digest(
    request, backend_id, registered_instance, registered_utc, claim_origin
):
    try:
        return _hash(
            b"kinaiya.standing.agent-retirement-registration-record.v1",
            _registration_record_body(
                request, backend_id, registered_instance, registered_utc, claim_origin
            ),
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingAgentRetirementRegistrationRecord(_Settlement):
    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_RETIREMENT_REGISTRATION_RECORD_BYTES
    request: StandingAgentRetirementRegistrationRequest
    backend_id: BackendId
    registered_instance: ControlInstance
    registered_utc: str
    claim_origin: _ORIGIN
    record_digest: Digest

    @model_validator(mode="after")
    def full_record(self):
        if self.record_digest != _hash(
            b"kinaiya.standing.agent-retirement-registration-record.v1",
            _registration_record_body(
                self.request,
                self.backend_id,
                self.registered_instance,
                self.registered_utc,
                self.claim_origin,
                validated=True,
            ),
        ):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: StandingAgentRetirementRegistrationRequest) -> Self:
        return _bound(self, original, StandingAgentRetirementRegistrationRequest)


class StandingAgentRetirementRegistrationReceipt(_Settlement):
    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_RETIREMENT_REGISTRATION_RECEIPT_BYTES
    disposition: _DISPOSITION
    registration: StandingAgentRetirementRegistrationRecord

    def validate_request(self, original: StandingAgentRetirementRegistrationRequest) -> Self:
        return _bound(self, original, StandingAgentRetirementRegistrationRequest)


class StandingAgentOutcomeReference(_Settlement):
    """Plain-SHA references to captured complete native bytes, not work proof."""

    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_OUTCOME_REFERENCE_BYTES
    task_id: ControlInstance
    event_id: SafeId
    sequence: PositiveInteger
    objective_revision: PositiveInteger
    state: Literal["succeeded", "failed", "cancelled"]
    request_sha256: Digest
    entry_sha256: Digest

    @field_validator("event_id")
    @classmethod
    def safe_event(cls, value):
        if "\0" in value:
            raise ValueError(_ERROR)
        return value


class StandingAgentRetirementWork(_Settlement):
    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_RETIREMENT_WORK_BYTES
    kind: _WORK
    outcome: StandingAgentOutcomeReference | None

    @model_validator(mode="after")
    def exact_outcome_discriminator(self):
        if (self.kind == "launched-outcome-known") != (self.outcome is not None):
            raise ValueError(_ERROR)
        return self


def _settlement_body(registration, resources, work, settlement_key, *, validated=False):
    if not validated:
        registration = checked_agent_settlement(
            registration, StandingAgentRetirementRegistrationRecord
        )
        resources = checked_finalization(resources, StandingResourceFinalizationReceipt)
        work = checked_agent_settlement(work, StandingAgentRetirementWork)
        settlement_key = _scalar(StandingAgentSettlementRequest, "idempotency_key", settlement_key)
    claim = registration.request.claim
    context = resources.finalization.context
    initial = context.closure.initial
    if (
        _encode(claim) != _encode(context.binding.claim)
        or _encode(claim.request) != _encode(context.request.original)
        or (work.kind == "bound-never-admitted") != (initial is None)
    ):
        raise ValueError(_ERROR)
    if work.outcome is not None and (
        work.outcome.task_id != claim.request.task.task_id
        or work.outcome.sequence <= initial.sequence
    ):
        raise ValueError(_ERROR)
    return {
        "schema_version": "1.0",
        "idempotency_key": settlement_key,
        "registration": registration,
        "resources": resources,
        "work": work,
    }


def agent_settlement_digest(registration, resources, work, settlement_key):
    try:
        return _hash(
            b"kinaiya.standing.agent-settlement.v1",
            _settlement_body(registration, resources, work, settlement_key),
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingAgentSettlementRequest(_Settlement):
    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_SETTLEMENT_REQUEST_BYTES
    idempotency_key: ControlKey
    registration: StandingAgentRetirementRegistrationRecord
    resources: StandingResourceFinalizationReceipt
    work: StandingAgentRetirementWork
    settlement_digest: Digest

    @model_validator(mode="after")
    def full_settlement(self):
        if self.settlement_digest != _hash(
            b"kinaiya.standing.agent-settlement.v1",
            _settlement_body(
                self.registration,
                self.resources,
                self.work,
                self.idempotency_key,
                validated=True,
            ),
        ):
            raise ValueError(_ERROR)
        return self


def _settlement_record_body(
    request, backend_id, settled_instance, settled_utc, *, validated=False
):
    if not validated:
        request = checked_agent_settlement(request, StandingAgentSettlementRequest)
        fields = {
            "backend_id": backend_id,
            "settled_instance": settled_instance,
            "settled_utc": settled_utc,
        }
        fields = {
            name: _scalar(StandingAgentSettlementRecord, name, value)
            for name, value in fields.items()
        }
        backend_id = fields["backend_id"]
        settled_instance = fields["settled_instance"]
        settled_utc = fields["settled_utc"]
    _utc(settled_utc)
    if backend_id != request.registration.backend_id:
        raise ValueError(_ERROR)
    return {
        "schema_version": "1.0",
        "request": request,
        "backend_id": backend_id,
        "settled_instance": settled_instance,
        "settled_utc": settled_utc,
    }


def agent_settlement_record_digest(request, backend_id, settled_instance, settled_utc):
    try:
        return _hash(
            b"kinaiya.standing.agent-settlement-record.v1",
            _settlement_record_body(request, backend_id, settled_instance, settled_utc),
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingAgentSettlementRecord(_Settlement):
    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_SETTLEMENT_RECORD_BYTES
    request: StandingAgentSettlementRequest
    backend_id: BackendId
    settled_instance: ControlInstance
    settled_utc: str
    record_digest: Digest

    @model_validator(mode="after")
    def full_record(self):
        if self.record_digest != _hash(
            b"kinaiya.standing.agent-settlement-record.v1",
            _settlement_record_body(
                self.request,
                self.backend_id,
                self.settled_instance,
                self.settled_utc,
                validated=True,
            ),
        ):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: StandingAgentSettlementRequest) -> Self:
        return _bound(self, original, StandingAgentSettlementRequest)

    def validate_query(self, original: "StandingAgentSettlementQuery") -> Self:
        return _query_bound(self, original)


class StandingAgentSettlementReceipt(_Settlement):
    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_SETTLEMENT_RECEIPT_BYTES
    disposition: _DISPOSITION
    settlement: StandingAgentSettlementRecord

    def validate_request(self, original: StandingAgentSettlementRequest) -> Self:
        return _bound(self, original, StandingAgentSettlementRequest)

    def validate_query(self, original: "StandingAgentSettlementQuery") -> Self:
        return _query_bound(self, original)


class StandingAgentSettlementQuery(_Settlement):
    wire_bytes: ClassVar[int] = MAX_STANDING_AGENT_SETTLEMENT_QUERY_BYTES
    registration: StandingAgentRetirementRegistrationRecord
    settlement_key: ControlKey

    def validate_request(self, original: "StandingAgentSettlementQuery") -> Self:
        return _bound(self, original, StandingAgentSettlementQuery)


def _bound(proof, original, kind):
    try:
        result = checked_agent_settlement(proof, type(proof))
        original = checked_agent_settlement(original, kind)
        if type(result) is StandingAgentRetirementRegistrationReceipt:
            request = result.registration.request
        elif type(result) is StandingAgentSettlementReceipt:
            request = result.settlement.request
        elif type(result) is StandingAgentSettlementQuery:
            request = result
        elif type(result) in (
            StandingAgentRetirementRegistrationRecord,
            StandingAgentSettlementRecord,
        ):
            request = result.request
        else:
            raise ValueError(_ERROR)
        # The full proof was freshly revalidated; compare its complete original
        # request rather than trusting a retained idempotency/digest alone.
        if _encode(request) != _encode(original):
            raise ValueError(_ERROR)
        return result
    except _FAILURES:
        raise ValueError(_ERROR) from None


def _query_bound(proof, original):
    try:
        result = checked_agent_settlement(proof, type(proof))
        original = checked_agent_settlement(original, StandingAgentSettlementQuery)
        if type(result) not in (StandingAgentSettlementRecord, StandingAgentSettlementReceipt):
            raise ValueError(_ERROR)
        record = result.settlement if type(result) is StandingAgentSettlementReceipt else result
        if (
            _encode(record.request.registration) != _encode(original.registration)
            or record.request.idempotency_key != original.settlement_key
        ):
            raise ValueError(_ERROR)
        return result
    except _FAILURES:
        raise ValueError(_ERROR) from None


_PACKETS = (
    StandingAgentRetirementRegistrationRequest,
    StandingAgentRetirementRegistrationRecord,
    StandingAgentRetirementRegistrationReceipt,
    StandingAgentOutcomeReference,
    StandingAgentRetirementWork,
    StandingAgentSettlementRequest,
    StandingAgentSettlementRecord,
    StandingAgentSettlementReceipt,
    StandingAgentSettlementQuery,
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
    StandingResourceBindingRequest,
    StandingResourceBindingReceipt,
    StandingResourceFinalizeRequest,
    StandingResourceFinalizeContext,
    StandingResourceFinalization,
    StandingResourceFinalizationReceipt,
)
