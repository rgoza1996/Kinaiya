"""Codex-authored task Resources metadata; no absence or cleanup certificate."""

import hashlib
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from packages.contracts.agent_control import (
    AgentTaskIdentity,
    BackendId,
    ControlInstance,
    Digest,
    PositiveInteger,
    Revision,
)
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

MAX_TASK_RESOURCE_QUERY_BYTES = 4096
MAX_TASK_RESOURCE_CONTEXT_BYTES = 180224
MAX_STANDING_RESOURCE_RESERVATION_BYTES = 196608
MAX_STANDING_RESOURCE_RECEIPT_BYTES = 212992
MAX_STANDING_RESOURCE_OBSERVATION_BYTES = 212992
_ERROR = "Invalid standing Resources contract"
_FAILURES = (ValueError, TypeError, UnicodeError, RecursionError, OverflowError)
_OBSERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "intent_digest",
        "claim_id",
        "closure_digest",
        "coverage_sequence",
        "accepted_sequence",
        "coverage",
        "reservation",
    }
)


def _actual(value):
    _tree(value)
    kinds = {
        TaskResourceQuery,
        TaskResourceContext,
        StandingResourceReservation,
        StandingResourceReservationReceipt,
        StandingResourceObservation,
        AgentTaskIdentity,
        StandingInitialProof,
        StandingExecutionClaim,
        StandingExecutionIntent,
        StandingExecutionTask,
        StandingExecutionModelPin,
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


def _complete(value, kind):
    _actual(value)
    if isinstance(value, BaseModel):
        if type(value) is not kind:
            raise ValueError(_ERROR)
        value = dict(value.__dict__)
    if type(value) is not dict or value.keys() != kind.model_fields.keys():
        raise ValueError(_ERROR)
    return kind.model_validate(value, strict=True)


def checked_resource(value, kind):
    """Revalidate an exact complete actual packet without a lossy dump."""
    try:
        if type(value) is not kind:
            raise ValueError(_ERROR)
        return _complete(value, kind)
    except _FAILURES:
        raise ValueError(_ERROR) from None


def _hash(domain, body):
    return hashlib.sha256(domain + b"\0" + _encode(body)).hexdigest()


class _Resource(_Closed):
    schema_version: Literal["1.0"]

    @model_validator(mode="wrap")
    @classmethod
    def resource_graph(cls, value, handler):
        try:
            if cls not in _PACKETS:
                raise ValueError(_ERROR)
            if isinstance(value, BaseModel) and type(value) is not cls:
                raise ValueError(_ERROR)
            _actual(value)
            return handler(value)
        except _FAILURES:
            raise ValueError(_ERROR) from None

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            if cls not in _PACKETS:
                raise ValueError(_ERROR)
            return cls.model_validate(standing_json(raw, maximum=cls.wire_bytes))
        except _FAILURES:
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        try:
            if type(self) not in _PACKETS:
                raise ValueError(_ERROR)
            _actual(self)
            return canonical_bytes(self)
        except _FAILURES:
            raise ValueError(_ERROR) from None


class TaskResourceQuery(_Resource):
    wire_bytes: ClassVar[int] = MAX_TASK_RESOURCE_QUERY_BYTES
    task_id: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    ]


def _context_body(query, backend_id, family, standing, ordinary):
    query = checked_resource(query, TaskResourceQuery)
    backend_id = TypeAdapter(BackendId).validate_python(backend_id, strict=True)
    if type(family) is not str or family not in ("standing", "ordinary"):
        raise ValueError(_ERROR)
    if standing is not None:
        standing = _complete(standing, StandingInitialProof)
    if ordinary is not None:
        ordinary = _complete(ordinary, AgentTaskIdentity)
    if family == "standing":
        if (
            standing is None
            or ordinary is not None
            or standing.claim.request.expected_backend != backend_id
            or standing.claim.request.task.task_id != query.task_id
        ):
            raise ValueError(_ERROR)
    elif ordinary is None or standing is not None or ordinary.task_id != query.task_id:
        raise ValueError(_ERROR)
    return {
        "schema_version": "1.0",
        "query": query,
        "backend_id": backend_id,
        "family": family,
        "standing": standing,
        "ordinary": ordinary,
    }


def task_resource_context_digest(query, backend_id, family, standing, ordinary) -> str:
    try:
        return _hash(
            b"kinaiya.standing.resource-context.v1",
            _context_body(query, backend_id, family, standing, ordinary),
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None


class TaskResourceContext(_Resource):
    wire_bytes: ClassVar[int] = MAX_TASK_RESOURCE_CONTEXT_BYTES
    query: TaskResourceQuery
    backend_id: BackendId
    family: Literal["standing", "ordinary"]
    standing: StandingInitialProof | None
    ordinary: AgentTaskIdentity | None
    context_digest: Digest

    @field_validator("query", mode="before")
    @classmethod
    def exact_query(cls, value):
        return _complete(value, TaskResourceQuery)

    @field_validator("standing", mode="before")
    @classmethod
    def exact_initial(cls, value):
        return None if value is None else _complete(value, StandingInitialProof)

    @field_validator("ordinary", mode="before")
    @classmethod
    def exact_ordinary(cls, value):
        return None if value is None else _complete(value, AgentTaskIdentity)

    @model_validator(mode="after")
    def full_identity(self):
        if self.context_digest != task_resource_context_digest(
            self.query, self.backend_id, self.family, self.standing, self.ordinary
        ):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: TaskResourceQuery) -> Self:
        try:
            result = checked_resource(self, TaskResourceContext)
            original = checked_resource(original, TaskResourceQuery)
            if canonical_bytes(result.query) != canonical_bytes(original):
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


class StandingResourceReservation(_Resource):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_RESERVATION_BYTES
    context: TaskResourceContext
    resource_backend: BackendId
    resource_instance: ControlInstance
    action_digest: Digest
    lease_id: ControlInstance

    @field_validator("context", mode="before")
    @classmethod
    def exact_context(cls, value):
        return _complete(value, TaskResourceContext)

    @model_validator(mode="after")
    def standing_only(self):
        if self.context.family != "standing":
            raise ValueError(_ERROR)
        return self


def standing_resource_digest(original: StandingResourceReservation) -> str:
    try:
        original = checked_resource(original, StandingResourceReservation)
        return _hash(b"kinaiya.standing.resource-reservation.v1", original)
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingResourceReservationReceipt(_Resource):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_RECEIPT_BYTES
    disposition: Literal["created", "retained"]
    request: StandingResourceReservation
    request_digest: Digest

    @field_validator("request", mode="before")
    @classmethod
    def exact_request(cls, value):
        return _complete(value, StandingResourceReservation)

    @model_validator(mode="after")
    def full_digest(self):
        if self.request_digest != standing_resource_digest(self.request):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: StandingResourceReservation) -> Self:
        try:
            result = checked_resource(self, StandingResourceReservationReceipt)
            original = checked_resource(original, StandingResourceReservation)
            if canonical_bytes(result.request) != canonical_bytes(original):
                raise ValueError(_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


def _observation_body(body):
    _actual(body)
    if type(body) is not dict or body.keys() != _OBSERVATION_FIELDS:
        raise ValueError(_ERROR)
    result = dict(body)
    if result["schema_version"] != "1.0":
        raise ValueError(_ERROR)
    for name in ("intent_digest", "claim_id", "closure_digest"):
        result[name] = TypeAdapter(Digest).validate_python(result[name], strict=True)
    result["coverage_sequence"] = TypeAdapter(Revision).validate_python(
        result["coverage_sequence"], strict=True
    )
    sequence = result["accepted_sequence"]
    if sequence is not None:
        sequence = TypeAdapter(PositiveInteger).validate_python(sequence, strict=True)
    result["accepted_sequence"] = sequence
    coverage = (
        "accepted-after-bootstrap"
        if sequence is not None and sequence > result["coverage_sequence"]
        else "legacy-or-unadmitted"
    )
    if type(result["coverage"]) is not str or result["coverage"] != coverage:
        raise ValueError(_ERROR)
    reservation = result["reservation"]
    if reservation is not None:
        reservation = _complete(reservation, StandingResourceReservation)
        initial = reservation.context.standing
        if (
            initial.claim.intent_digest != result["intent_digest"]
            or initial.claim.claim_id != result["claim_id"]
            or initial.sequence != sequence
        ):
            raise ValueError(_ERROR)
    result["reservation"] = reservation
    return result


def standing_resource_observation_digest(body) -> str:
    """Hash the complete eight-field body, excluding observation_digest."""
    try:
        return _hash(b"kinaiya.standing.resource-observation.v1", _observation_body(body))
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingResourceObservation(_Resource):
    wire_bytes: ClassVar[int] = MAX_STANDING_RESOURCE_OBSERVATION_BYTES
    intent_digest: Digest
    claim_id: Digest
    closure_digest: Digest
    coverage_sequence: Revision
    accepted_sequence: PositiveInteger | None
    coverage: Literal["accepted-after-bootstrap", "legacy-or-unadmitted"]
    reservation: StandingResourceReservation | None
    observation_digest: Digest

    @field_validator("reservation", mode="before")
    @classmethod
    def exact_reservation(cls, value):
        return None if value is None else _complete(value, StandingResourceReservation)

    @model_validator(mode="after")
    def full_observation(self):
        body = {key: value for key, value in self.__dict__.items() if key != "observation_digest"}
        if self.observation_digest != standing_resource_observation_digest(body):
            raise ValueError(_ERROR)
        return self

    def validate_request(self, original: StandingExecutionIntent) -> Self:
        try:
            result = checked_resource(self, StandingResourceObservation)
            if type(original) is not StandingExecutionIntent:
                raise ValueError(_ERROR)
            _actual(original)
            original = _checked(original, StandingExecutionIntent)
            if result.intent_digest != intent_digest(original) or result.claim_id != claim_id(
                original
            ):
                raise ValueError(_ERROR)
            if result.reservation is not None:
                result.reservation.context.standing.validate_request(original)
            return result
        except _FAILURES:
            raise ValueError(_ERROR) from None


_PACKETS = (
    TaskResourceQuery,
    TaskResourceContext,
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    StandingResourceObservation,
)
