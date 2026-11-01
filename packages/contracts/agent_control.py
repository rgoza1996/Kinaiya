"""Codex-authored pure Agent operating-mode and steering-intent wire grammar.

These types validate bounded identity, not authentication, native persistence,
worker ownership or task completion. In particular, an accepted steering intent
is not an applied objective revision, and historical proof never grants launch.
"""

import hashlib
import json
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_AGENT_CONTROL_BYTES = 4096
MAX_AGENT_CONTROL_RESPONSE_BYTES = 16384
MAX_AGENT_TASK_IDENTITY_BYTES = 8192
MAX_AGENT_STEER_BYTES = 131072
MAX_AGENT_STEER_RESPONSE_BYTES = 262144
# Lookup repeats the original request inside its retained receipt. Reserve room
# for the receipt's bounded event identity, counters and outer proof envelope.
AGENT_STEER_PROOF_RESERVE_BYTES = 1024
MAX_AGENT_STEER_CANONICAL_BYTES = MAX_AGENT_STEER_BYTES - AGENT_STEER_PROOF_RESERVE_BYTES
MAX_AGENT_CONTROL_JSON_DEPTH = 12
MAX_AGENT_CONTROL_JSON_NODES = 2048
MAX_AGENT_CONTROL_INTEGER = 9_007_199_254_740_991

_ERROR = "Invalid Agent control contract"
_BINDING_ERROR = "Agent control proof does not match the original request"
_UUID = r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"
_SCOPE_RANK = {"public_synthetic": 0, "private_cloud_authorized": 1, "local_only": 2}

AgentMode = Literal["off", "on_demand", "autonomous"]
ActiveWorkPolicy = Literal["finish_already_accepted"]
ContentScope = Literal["public_synthetic", "private_cloud_authorized", "local_only"]
BackendId = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")]
ControlInstance = Annotated[str, Field(min_length=36, max_length=36, pattern=_UUID)]
ControlKey = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")]
Digest = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(ge=0, le=MAX_AGENT_CONTROL_INTEGER)]
NextRevision = Annotated[int, Field(ge=0, le=MAX_AGENT_CONTROL_INTEGER - 1)]
PositiveInteger = Annotated[int, Field(ge=1, le=MAX_AGENT_CONTROL_INTEGER)]
SafeId = Annotated[str, Field(min_length=1, max_length=128)]
LogicalKey = Annotated[str, Field(min_length=1, max_length=256)]


def _safe_id(value: str) -> str:
    # Match canonical native submit IDs without sanitizing retained identity.
    if "\0" in value:
        raise ValueError(_ERROR)
    return value


def _model_json(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", warnings=False)
    raise ValueError(_ERROR)


def _check_tree(value: Any) -> None:
    pending = [(value, 1)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_AGENT_CONTROL_JSON_NODES or depth > MAX_AGENT_CONTROL_JSON_DEPTH:
            raise ValueError(_ERROR)
        if isinstance(node, BaseModel):
            node = node.model_dump(mode="json", warnings=False)
        if type(node) is dict:
            if any(type(key) is not str for key in node):
                raise ValueError(_ERROR)
            pending.extend((item, depth + 1) for pair in node.items() for item in pair)
        elif type(node) in (list, tuple):
            pending.extend((item, depth + 1) for item in node)
        elif type(node) is str:
            node.encode("utf-8")
        elif type(node) is int:
            if abs(node) > MAX_AGENT_CONTROL_INTEGER:
                raise ValueError(_ERROR)
        elif node is not None and type(node) is not bool:
            raise ValueError(_ERROR)


def agent_control_json(raw: bytes, *, maximum: int = MAX_AGENT_CONTROL_BYTES) -> dict:
    """Decode one strict bounded UTF-8 object before model validation."""
    if (
        type(raw) is not bytes
        or type(maximum) is not int
        or not 1 <= maximum <= MAX_AGENT_STEER_RESPONSE_BYTES
        or len(raw) > maximum
    ):
        raise ValueError(_ERROR)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(_ERROR)
            result[key] = value
        return result

    def invalid(_):
        raise ValueError(_ERROR)

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=invalid,
            parse_constant=invalid,
        )
        if type(value) is not dict:
            raise ValueError(_ERROR)
        _check_tree(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


class _Wire(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )
    wire_bytes: ClassVar[int] = MAX_AGENT_CONTROL_BYTES
    schema_version: Literal["1.0"]

    @model_validator(mode="before")
    @classmethod
    def bounded_object(cls, value):
        try:
            if isinstance(value, cls):
                value = value.model_dump(mode="python", warnings=False)
            if type(value) is not dict:
                raise ValueError(_ERROR)
            _check_tree(value)
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, default=_model_json)
            if len(encoded.encode("utf-8")) > cls.wire_bytes:
                raise ValueError(_ERROR)
            return value
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError(_ERROR) from None

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            return cls.model_validate(agent_control_json(raw, maximum=cls.wire_bytes))
        except (ValueError, TypeError, RecursionError):
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        """Revalidate even a model_copy/model_construct result before hashing."""
        try:
            value = type(self).model_validate(self.model_dump(mode="python", warnings=False))
            return json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError(_ERROR) from None

    def request_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class AgentModeChange(_Wire):
    """Seven required immutable fields; owner authority comes from the service."""

    idempotency_key: ControlKey
    expected_backend: BackendId
    expected_instance: ControlInstance
    expected_revision: NextRevision
    mode: AgentMode
    active_work_policy: ActiveWorkPolicy

    @field_validator("idempotency_key")
    @classmethod
    def safe_key(cls, value):
        return _safe_id(value)


class AgentControlSnapshot(_Wire):
    """Shared desired/admission mode, separate from Agent health and live tasks.

    instance_id is the persisted active control-writer generation. The serving
    API reports control_writable only when its explicit registration is active.
    Opening another Store/LocalJournal must never register or retire a writer.
    """

    wire_bytes: ClassVar[int] = MAX_AGENT_CONTROL_RESPONSE_BYTES
    backend_id: BackendId
    instance_id: ControlInstance
    revision: Revision
    mode: AgentMode
    active_work_policy: ActiveWorkPolicy
    control_writable: bool
    available_modes: tuple[AgentMode, ...] = Field(min_length=2, max_length=3)
    standing_work_available: bool

    @field_validator("available_modes", mode="before")
    @classmethod
    def immutable_modes(cls, value):
        if type(value) not in (list, tuple):
            raise ValueError(_ERROR)
        return tuple(value)

    @model_validator(mode="after")
    def actual_capabilities(self):
        expected = ("off", "on_demand") + (("autonomous",) if self.standing_work_available else ())
        if self.available_modes != expected:
            raise ValueError(_ERROR)
        return self


class AgentModeReceipt(_Wire):
    """Historical full-request proof. Returning it must never reapply its mode."""

    wire_bytes: ClassVar[int] = MAX_AGENT_CONTROL_RESPONSE_BYTES
    status: Literal["mode_saved"]
    backend_id: BackendId
    service_instance: ControlInstance
    revision: PositiveInteger
    request: AgentModeChange

    @model_validator(mode="after")
    def identity(self):
        if (
            self.backend_id != self.request.expected_backend
            or self.service_instance != self.request.expected_instance
            or self.revision != self.request.expected_revision + 1
        ):
            raise ValueError(_BINDING_ERROR)
        return self

    def validate_request(self, request: Any) -> Self:
        return _bound(self, request, AgentModeChange)


class AgentModeLookupResult(_Wire):
    """Native observation must first prove the expected generation was registered.

    An unknown UUID is a conflict, not a retired generation. This grammar checks
    observation consistency; only the native generation registry proves history.
    """

    wire_bytes: ClassVar[int] = MAX_AGENT_CONTROL_RESPONSE_BYTES
    status: Literal["recorded", "unobserved"]
    request: AgentModeChange
    state: AgentControlSnapshot
    receipt: AgentModeReceipt | None
    mutation_fenced: bool

    @model_validator(mode="after")
    def proof(self):
        if self.state.backend_id != self.request.expected_backend:
            raise ValueError(_BINDING_ERROR)
        if self.status == "recorded":
            if self.receipt is None:
                raise ValueError(_BINDING_ERROR)
            self.receipt.validate_request(self.request)
            if self.state.revision < self.receipt.revision or (
                self.state.revision == self.receipt.revision
                and self.state.mode != self.request.mode
            ):
                raise ValueError(_BINDING_ERROR)
        elif self.receipt is not None:
            raise ValueError(_BINDING_ERROR)
        fenced = (
            self.receipt is not None
            or self.state.instance_id != self.request.expected_instance
            or self.state.revision > self.request.expected_revision
        )
        if self.mutation_fenced != fenced:
            raise ValueError(_BINDING_ERROR)
        return self

    def validate_request(self, request: Any) -> Self:
        return _bound(self, request, AgentModeChange)


class AgentTaskIdentity(_Wire):
    """Immutable canonical native initial-submit identity, never a mutable Task."""

    wire_bytes: ClassVar[int] = MAX_AGENT_TASK_IDENTITY_BYTES
    session_id: SafeId
    thread_id: SafeId
    epoch: PositiveInteger
    task_id: SafeId
    submit_event_id: SafeId
    submit_sequence: PositiveInteger
    submit_idempotency_key: LogicalKey
    submit_payload_digest: Digest

    @field_validator(
        "session_id", "thread_id", "task_id", "submit_event_id", "submit_idempotency_key"
    )
    @classmethod
    def safe_identity(cls, value):
        return _safe_id(value)


class AgentSteerChange(_Wire):
    """Strict immutable projection of all six original SteerRequest fields."""

    wire_bytes: ClassVar[int] = MAX_AGENT_STEER_BYTES
    instruction: str = Field(min_length=1, max_length=16000)
    content_scope: ContentScope
    expected_objective_revision: int = Field(ge=1, le=MAX_AGENT_CONTROL_INTEGER - 1)
    idempotency_key: LogicalKey
    source_event_ids: tuple[SafeId, ...] = Field(max_length=32)

    @field_validator("source_event_ids", mode="before")
    @classmethod
    def immutable_sources(cls, value):
        if type(value) not in (list, tuple):
            raise ValueError(_ERROR)
        return tuple(value)

    @field_validator("source_event_ids")
    @classmethod
    def safe_sources(cls, value):
        return tuple(_safe_id(item) for item in value)


class AgentSteerSource(_Wire):
    event_id: SafeId
    revision: PositiveInteger
    content_scope: ContentScope

    @field_validator("event_id")
    @classmethod
    def safe_source(cls, value):
        return _safe_id(value)


class AgentSteerAdmissionRequest(_Wire):
    wire_bytes: ClassVar[int] = MAX_AGENT_STEER_BYTES
    expected_backend: BackendId
    task: AgentTaskIdentity
    actor: Literal["owner", "decisions"]
    request: AgentSteerChange
    effective_content_scope: ContentScope
    source_revisions: tuple[AgentSteerSource, ...] = Field(max_length=32)

    @field_validator("source_revisions", mode="before")
    @classmethod
    def immutable_revisions(cls, value):
        if type(value) not in (list, tuple):
            raise ValueError(_ERROR)
        return tuple(value)

    @model_validator(mode="after")
    def provenance(self):
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_AGENT_STEER_CANONICAL_BYTES:
            raise ValueError(_ERROR)
        if (
            tuple(source.event_id for source in self.source_revisions)
            != self.request.source_event_ids
        ):
            raise ValueError(_BINDING_ERROR)
        observed = {}
        scope = _SCOPE_RANK[self.effective_content_scope]
        if scope < _SCOPE_RANK[self.request.content_scope]:
            raise ValueError(_BINDING_ERROR)
        for source in self.source_revisions:
            if source.event_id in observed and observed[source.event_id] != source:
                raise ValueError(_BINDING_ERROR)
            observed[source.event_id] = source
            if scope < _SCOPE_RANK[source.content_scope]:
                raise ValueError(_BINDING_ERROR)
        if self.actor == "decisions" and not observed:
            raise ValueError(_BINDING_ERROR)
        return self


class AgentSteerReceipt(_Wire):
    """Committed intent only. Only the original direct created path may install it."""

    wire_bytes: ClassVar[int] = MAX_AGENT_STEER_RESPONSE_BYTES
    disposition: Literal["created", "retained"]
    status: Literal["intent_accepted"]
    request: AgentSteerAdmissionRequest
    event_id: SafeId
    sequence: PositiveInteger
    control_revision: Revision
    admission_mode: Literal["on_demand", "autonomous"]
    target_objective_revision: PositiveInteger

    @field_validator("event_id")
    @classmethod
    def safe_event(cls, value):
        return _safe_id(value)

    @model_validator(mode="after")
    def identity(self):
        if (
            self.event_id == self.request.task.submit_event_id
            or self.sequence <= self.request.task.submit_sequence
            or self.target_objective_revision
            != self.request.request.expected_objective_revision + 1
        ):
            raise ValueError(_BINDING_ERROR)
        return self

    def validate_request(self, request: Any) -> Self:
        return _bound(self, request, AgentSteerAdmissionRequest)


class AgentSteerLookupResult(_Wire):
    """Negative observation never fences a Task or steering attempt, including Off ABA."""

    wire_bytes: ClassVar[int] = MAX_AGENT_STEER_RESPONSE_BYTES
    status: Literal["recorded", "unobserved"]
    request: AgentSteerAdmissionRequest
    receipt: AgentSteerReceipt | None

    @model_validator(mode="after")
    def proof(self):
        if self.status == "recorded":
            if self.receipt is None or self.receipt.disposition != "retained":
                raise ValueError(_BINDING_ERROR)
            self.receipt.validate_request(self.request)
        elif self.receipt is not None:
            raise ValueError(_BINDING_ERROR)
        return self

    def validate_request(self, request: Any) -> Self:
        return _bound(self, request, AgentSteerAdmissionRequest)


def _bound(proof, request, request_type):
    try:
        result = type(proof).model_validate(proof.model_dump(mode="python", warnings=False))
        original = request_type.model_validate(request)
        if result.request.canonical_bytes() != original.canonical_bytes():
            raise ValueError(_BINDING_ERROR)
        return result
    except (ValueError, TypeError, RecursionError):
        raise ValueError(_BINDING_ERROR) from None
