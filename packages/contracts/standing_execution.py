"""Codex-authored pure standing execution identities and retained claim proof.

These packets create no native claim, grant no permission and start no work.
The capability secret remains transient; only its contextual hash is carried.
"""

import hashlib
import re
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from packages.contracts.agent_control import (
    BackendId,
    ContentScope,
    ControlInstance,
    ControlKey,
    Digest,
    NextRevision,
    PositiveInteger,
    Revision,
)
from packages.contracts.models import AppendRequest, CatalogModelBinding
from packages.contracts.standing_dispatch import StandingProducerReceipt
from packages.contracts.standing_work import (
    StandingTaskSession,
    _checked,
    _civil_date,
    _Closed,
    _encode,
    _tree,
    _utc,
    canonical_bytes,
    standing_json,
)
from packages.contracts.workspace_settings import WorkspacePin

MAX_STANDING_OCCURRENCE_BYTES = 4096
MAX_STANDING_EXECUTION_MODEL_PIN_BYTES = 8192
MAX_STANDING_EXECUTION_TASK_BYTES = 65536
MAX_STANDING_EXECUTION_INTENT_BYTES = 131072
MAX_STANDING_EXECUTION_CLAIM_BYTES = 147456
MAX_STANDING_EXECUTION_ADMISSION_BYTES = 163840
_MAX_INITIAL_REQUEST_BYTES = 65536

_ERROR = "Invalid standing execution contract"
_PROOF_ERROR = "Standing execution proof does not match the original request"
_FAILURES = (ValueError, TypeError, UnicodeError, RecursionError, OverflowError)
_SESSION_FIELDS = frozenset({"session_id", "thread_id", "epoch"})
_CATALOG_FIELDS = frozenset({"id", "generation", "configuration_sha256"})
_WORKSPACE_FIELDS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "root_identity",
        "settings_revision",
        "source",
        "shell_backend",
        "mode",
    }
)


class _Execution(_Closed):
    wire_bytes: ClassVar[int] = MAX_STANDING_OCCURRENCE_BYTES
    schema_version: Literal["1.0"]

    @model_validator(mode="wrap")
    @classmethod
    def execution_object(cls, value, handler):
        try:
            return handler(value)
        except _FAILURES:
            raise ValueError(_ERROR) from None

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        try:
            return cls.model_validate(standing_json(raw, maximum=cls.wire_bytes))
        except _FAILURES:
            raise ValueError(_ERROR) from None

    def canonical_bytes(self) -> bytes:
        try:
            return canonical_bytes(self)
        except _FAILURES:
            raise ValueError(_ERROR) from None


def _nested(value, kind, fields):
    """Preserve actual closure before an older nested constructor can dump it."""
    _tree(value)
    if isinstance(value, BaseModel):
        if not isinstance(value, kind):
            raise ValueError(_ERROR)
        value = dict(value.__dict__)
    if type(value) is not dict or value.keys() != fields:
        raise ValueError(_ERROR)
    return kind.model_validate(value, strict=True)


def _hash(domain: str, body) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + _encode(body)).hexdigest()


class StandingOccurrence(_Execution):
    """Civil occurrence identity, without an IANA schedule qualification."""

    local_date: str
    due_utc: str
    utc_offset_seconds: Annotated[int, Field(ge=-86399, le=86399)]

    @field_validator("local_date")
    @classmethod
    def exact_date(cls, value):
        _civil_date(value)
        return value

    @field_validator("due_utc")
    @classmethod
    def exact_instant(cls, value):
        _utc(value)
        return value


class StandingExecutionModelPin(_Execution):
    """Complete exact model declaration; no legacy omitted-field form."""

    wire_bytes: ClassVar[int] = MAX_STANDING_EXECUTION_MODEL_PIN_BYTES
    profile_id: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    local: bool
    reasoning: str | None = Field(max_length=16)
    settings_revision: Revision
    catalog_binding: CatalogModelBinding | None
    catalog_revision: NextRevision | None

    @field_validator("profile_id", "provider", "model", "reasoning")
    @classmethod
    def exact_text(cls, value):
        if value is not None and "\0" in value:
            raise ValueError(_ERROR)
        return value

    @field_validator("catalog_binding", mode="before")
    @classmethod
    def complete_catalog(cls, value):
        if value is None:
            return None
        binding = _nested(value, CatalogModelBinding, _CATALOG_FIELDS)
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", binding.id) is None
            or re.fullmatch(r"[a-f0-9]{64}", binding.configuration_sha256) is None
        ):
            raise ValueError(_ERROR)
        return binding

    @model_validator(mode="after")
    def catalog_pair(self):
        if (self.catalog_binding is None) != (self.catalog_revision is None):
            raise ValueError(_ERROR)
        if self.catalog_binding is not None and self.catalog_binding.id != self.profile_id:
            raise ValueError(_ERROR)
        return self


class StandingExecutionTask(_Execution):
    """Explicit future native-files task family, without runtime enforcement."""

    wire_bytes: ClassVar[int] = MAX_STANDING_EXECUTION_TASK_BYTES
    task_id: ControlInstance
    task_session: StandingTaskSession
    objective: str = Field(min_length=1, max_length=16000)
    content_scope: ContentScope
    workspace_pin: WorkspacePin
    harness_version: str
    model_pin: StandingExecutionModelPin
    tool_subset: Literal["native-files-v1"]

    @field_validator("task_session", mode="before")
    @classmethod
    def complete_session(cls, value):
        return _nested(value, StandingTaskSession, _SESSION_FIELDS)

    @field_validator("workspace_pin", mode="before")
    @classmethod
    def complete_workspace(cls, value):
        return _nested(value, WorkspacePin, _WORKSPACE_FIELDS)

    @field_validator("harness_version")
    @classmethod
    def exact_harness(cls, value):
        if re.fullmatch(r"seed-v1|v-[a-f0-9]{24}", value) is None:
            raise ValueError(_ERROR)
        return value


class StandingExecutionIntent(_Execution):
    """Frozen full claim proposal; native joined authority remains necessary."""

    wire_bytes: ClassVar[int] = MAX_STANDING_EXECUTION_INTENT_BYTES
    idempotency_key: ControlKey
    expected_backend: BackendId
    expected_instance: ControlInstance
    responsibility_id: ControlInstance
    authorization_revision: PositiveInteger
    authorization_digest: Digest
    record_digest: Digest
    occurrence: StandingOccurrence
    dispatch_epoch: Annotated[int, Field(ge=1, le=10000)]
    activation_digest: Digest
    control_revision: PositiveInteger
    producer: StandingProducerReceipt
    executor_id: ControlInstance
    capability_sha256: Digest
    task: StandingExecutionTask

    @field_validator("idempotency_key")
    @classmethod
    def exact_key(cls, value):
        if re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", value) is None:
            raise ValueError(_ERROR)
        return value

    @model_validator(mode="after")
    def producer_identity(self):
        if (
            self.producer.request.expected_backend != self.expected_backend
            or self.producer.request.expected_instance != self.expected_instance
        ):
            raise ValueError(_ERROR)
        return self


def _occurrence_id(intent) -> str:
    return _hash(
        "kinaiya.standing.occurrence.v1",
        {
            "backend_id": intent.expected_backend,
            "responsibility_id": intent.responsibility_id,
            "authorization_revision": intent.authorization_revision,
            "authorization_digest": intent.authorization_digest,
            "record_digest": intent.record_digest,
            "occurrence": intent.occurrence,
        },
    )


def _intent_digest(intent) -> str:
    return _hash("kinaiya.standing.intent.v1", intent)


def _claim_id(digest: str) -> str:
    return _hash("kinaiya.standing.claim.id.v1", {"intent_digest": digest})


def _claim_digest(intent, prepared_utc: str, occurrence: str, digest: str, claim: str) -> str:
    return _hash(
        "kinaiya.standing.claim.record.v1",
        {
            "request": intent,
            "claim_id": claim,
            "occurrence_id": occurrence,
            "intent_digest": digest,
            "prepared_utc": prepared_utc,
        },
    )


def occurrence_id(intent: StandingExecutionIntent) -> str:
    try:
        return _occurrence_id(_checked(intent, StandingExecutionIntent))
    except _FAILURES:
        raise ValueError(_ERROR) from None


def intent_digest(intent: StandingExecutionIntent) -> str:
    try:
        return _intent_digest(_checked(intent, StandingExecutionIntent))
    except _FAILURES:
        raise ValueError(_ERROR) from None


def claim_id(intent: StandingExecutionIntent) -> str:
    try:
        return _claim_id(intent_digest(intent))
    except _FAILURES:
        raise ValueError(_ERROR) from None


def claim_digest(intent: StandingExecutionIntent, prepared_utc: str) -> str:
    try:
        checked = _checked(intent, StandingExecutionIntent)
        _utc(prepared_utc)
        digest = _intent_digest(checked)
        return _claim_digest(
            checked, prepared_utc, _occurrence_id(checked), digest, _claim_id(digest)
        )
    except _FAILURES:
        raise ValueError(_ERROR) from None


def capability_digest(
    backend_id: str, idempotency_key: str, executor_id: str, secret: bytes
) -> str:
    """Hash a transient capability without putting its secret in a wire model."""
    try:
        if (
            type(backend_id) is not str
            or type(idempotency_key) is not str
            or type(executor_id) is not str
            or type(secret) is not bytes
            or len(secret) != 32
        ):
            raise ValueError(_ERROR)
        backend = TypeAdapter(BackendId).validate_python(backend_id, strict=True)
        key = TypeAdapter(ControlKey).validate_python(idempotency_key, strict=True)
        executor = TypeAdapter(ControlInstance).validate_python(executor_id, strict=True)
        if re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", key) is None:
            raise ValueError(_ERROR)
        context = {"backend_id": backend, "idempotency_key": key, "executor_id": executor}
        return hashlib.sha256(
            b"kinaiya.standing.capability.v1\0" + _encode(context) + b"\0" + secret
        ).hexdigest()
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingExecutionClaim(_Execution):
    """Immutable retained claim proof, never itself a created disposition."""

    wire_bytes: ClassVar[int] = MAX_STANDING_EXECUTION_CLAIM_BYTES
    request: StandingExecutionIntent
    claim_id: Digest
    occurrence_id: Digest
    intent_digest: Digest
    prepared_utc: str
    claim_digest: Digest

    @field_validator("prepared_utc")
    @classmethod
    def exact_preparation(cls, value):
        _utc(value)
        return value

    @model_validator(mode="after")
    def exact_claim(self):
        digest = _intent_digest(self.request)
        occurrence = _occurrence_id(self.request)
        claim = _claim_id(digest)
        record = _claim_digest(self.request, self.prepared_utc, occurrence, digest, claim)
        if (self.intent_digest, self.occurrence_id, self.claim_id, self.claim_digest) != (
            digest,
            occurrence,
            claim,
            record,
        ):
            raise ValueError(_PROOF_ERROR)
        return self

    def validate_request(self, original: StandingExecutionIntent) -> Self:
        try:
            result = _checked(self, StandingExecutionClaim)
            original = _checked(original, StandingExecutionIntent)
            if canonical_bytes(result.request) != canonical_bytes(original):
                raise ValueError(_PROOF_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_PROOF_ERROR) from None


class StandingExecutionClaimAdmission(_Execution):
    """Native disposition packet; its grammar cannot establish a real COMMIT."""

    wire_bytes: ClassVar[int] = MAX_STANDING_EXECUTION_ADMISSION_BYTES
    disposition: Literal["created", "retained"]
    claim: StandingExecutionClaim

    def validate_request(self, original: StandingExecutionIntent) -> Self:
        try:
            result = _checked(self, StandingExecutionClaimAdmission)
            result.claim.validate_request(original)
            return result
        except _FAILURES:
            raise ValueError(_PROOF_ERROR) from None


def standing_initial_request(claim: StandingExecutionClaim) -> AppendRequest:
    """Build the explicit new family without admitting it or selecting origin."""
    try:
        claim = _checked(claim, StandingExecutionClaim)
        intent = claim.request
        task = intent.task
        session = task.task_session
        request = AppendRequest(
            schema_version="1.0",
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            kind="agent.standing-command",
            causes=[],
            task_refs=[task.task_id],
            idempotency_key=f"agent:{task.task_id}:agent.standing-command:1",
            payload={
                "op": "submit",
                "actor": "standing",
                "task_id": task.task_id,
                "state": "accepted",
                "session_id": session.session_id,
                "thread_id": session.thread_id,
                "epoch": session.epoch,
                "objective": task.objective,
                "objective_revision": 1,
                "source_event_ids": [],
                "source_revision": 1,
                "requested_content_scope": task.content_scope,
                "content_scope": task.content_scope,
                "idempotency_key": f"standing:{claim.occurrence_id}",
                "payload_digest": claim.intent_digest,
                "harness_version": task.harness_version,
                "model_pin": task.model_pin.model_dump(mode="python"),
                "workspace_pin": task.workspace_pin.model_dump(mode="python"),
                "auxiliary_models": {},
                "plugin_versions": {},
                "explicit_skill": None,
                "standing": {
                    "schema_version": "1.0",
                    "claim_id": claim.claim_id,
                    "claim_digest": claim.claim_digest,
                    "occurrence_id": claim.occurrence_id,
                    "intent_digest": claim.intent_digest,
                    "responsibility_id": intent.responsibility_id,
                    "authorization_revision": intent.authorization_revision,
                    "authorization_digest": intent.authorization_digest,
                    "record_digest": intent.record_digest,
                    "dispatch_epoch": intent.dispatch_epoch,
                    "activation_digest": intent.activation_digest,
                    "control_revision": intent.control_revision,
                    "producer_id": intent.producer.request.producer_id,
                    "executor_id": intent.executor_id,
                    "capability_sha256": intent.capability_sha256,
                    "tool_subset": task.tool_subset,
                },
            },
        )
        if len(request.model_dump_json().encode("utf-8")) > _MAX_INITIAL_REQUEST_BYTES:
            raise ValueError(_ERROR)
        return request
    except _FAILURES:
        raise ValueError(_ERROR) from None
