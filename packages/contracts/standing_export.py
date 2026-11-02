"""Codex-authored standing inference-disclosure proof; no inference authority."""

import hashlib
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, field_validator, model_validator

from packages.contracts.agent_control import Digest
from packages.contracts.agent_export import (
    AgentExportAdmission,
    AgentExportProviderInfo,
    AgentExportRequest,
)
from packages.contracts.standing_admission import StandingInitialProof
from packages.contracts.standing_work import (
    _checked,
    _Closed,
    _tree,
    canonical_bytes,
    standing_json,
)
from packages.providers.disclosure import combine_scopes, scope_allowed

MAX_STANDING_EXPORT_REQUEST_BYTES = 262144
MAX_STANDING_EXPORT_ADMISSION_BYTES = 73728
_ERROR = "Invalid standing export contract"
_PROOF_ERROR = "Standing export proof does not match the original request"
_FAILURES = (ValueError, TypeError, UnicodeError, RecursionError, OverflowError)


def _complete(value, kind):
    # Old export validators dump ProviderInfo and supply defaults. Inspect the
    # actual complete graph and exact class before that conversion can lose it.
    _tree(value)
    if isinstance(value, BaseModel):
        if type(value) is not kind:
            raise ValueError(_ERROR)
        value = dict(value.__dict__)
    if type(value) is not dict or value.keys() != kind.model_fields.keys():
        raise ValueError(_ERROR)
    return dict(value)


def _export(value, kind):
    if (
        isinstance(value, BaseModel)
        and type(value.__dict__.get("provider")) is not AgentExportProviderInfo
    ):
        raise ValueError(_ERROR)
    raw = _complete(value, kind)
    provider = _complete(raw["provider"], AgentExportProviderInfo)
    raw["provider"] = AgentExportProviderInfo.model_validate(provider, strict=True)
    return kind.model_validate(raw, strict=True)


class _Export(_Closed):
    schema_version: Literal["1.0"]

    @model_validator(mode="wrap")
    @classmethod
    def export_object(cls, value, handler):
        try:
            if isinstance(value, BaseModel) and type(value) is not cls:
                raise ValueError(_ERROR)
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
            if type(self) not in (StandingExportRequest, StandingExportAdmission):
                raise ValueError(_ERROR)
            return canonical_bytes(self)
        except _FAILURES:
            raise ValueError(_ERROR) from None


class StandingExportRequest(_Export):
    """Full original initial-task proof and outgoing request proposal only."""

    wire_bytes: ClassVar[int] = MAX_STANDING_EXPORT_REQUEST_BYTES
    initial: StandingInitialProof
    export: AgentExportRequest

    @field_validator("initial", mode="before")
    @classmethod
    def complete_initial(cls, value):
        return StandingInitialProof.model_validate(_complete(value, StandingInitialProof))

    @field_validator("export", mode="before")
    @classmethod
    def complete_export(cls, value):
        return _export(value, AgentExportRequest)

    @model_validator(mode="after")
    def task_binding(self):
        task = self.initial.claim.request.task
        session = task.task_session
        export, provider, pin = self.export, self.export.provider, task.model_pin
        if (
            (export.task_id, export.session_id, export.thread_id, export.epoch)
            != (task.task_id, session.session_id, session.thread_id, session.epoch)
            or export.objective_revision != 1
            or export.source_event_ids != []
            or export.source_revisions != {}
            or export.context_event_ids != []
            or (provider.provider, provider.model, provider.local)
            != (pin.provider, pin.model, pin.local)
        ):
            raise ValueError(_ERROR)
        return self


def standing_export_digest(original: StandingExportRequest) -> str:
    """Bind the entire original, including native initial metadata and proof."""
    try:
        if type(original) is not StandingExportRequest:
            raise ValueError(_ERROR)
        original = _checked(original, StandingExportRequest)
        return hashlib.sha256(
            b"kinaiya.standing.export.v1\0" + original.canonical_bytes()
        ).hexdigest()
    except _FAILURES:
        raise ValueError(_ERROR) from None


class StandingExportAdmission(_Export):
    """Point-in-time native metadata, never a reusable upload or tool permit."""

    wire_bytes: ClassVar[int] = MAX_STANDING_EXPORT_ADMISSION_BYTES
    request_digest: Digest
    admission: AgentExportAdmission

    @field_validator("admission", mode="before")
    @classmethod
    def complete_admission(cls, value):
        return _export(value, AgentExportAdmission)

    def validate_request(self, original: StandingExportRequest) -> Self:
        try:
            if (
                type(self) is not StandingExportAdmission
                or type(original) is not StandingExportRequest
            ):
                raise ValueError(_PROOF_ERROR)
            result = _checked(self, StandingExportAdmission)
            original = _checked(original, StandingExportRequest)
            if result.request_digest != standing_export_digest(original):
                raise ValueError(_PROOF_ERROR)
            admitted = result.admission.validate_request(original.export)
            floor = combine_scopes(
                original.initial.claim.request.task.content_scope, original.export.content_scope
            )
            if combine_scopes(
                floor, admitted.effective_scope
            ) != admitted.effective_scope or not scope_allowed(
                admitted.provider, admitted.effective_scope
            ):
                raise ValueError(_PROOF_ERROR)
            return result
        except _FAILURES:
            raise ValueError(_PROOF_ERROR) from None
