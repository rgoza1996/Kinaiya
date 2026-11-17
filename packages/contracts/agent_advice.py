"""Native task-bound export admission for optional Agent-facing Decisions use."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.agent_control import AgentTaskIdentity, agent_control_json
from packages.contracts.agent_export import (
    AgentExportAdmission,
    AgentExportProviderInfo,
    AgentExportRequest,
)
from packages.contracts.models import ContentScope
from packages.providers.disclosure import combine_scopes

MAX_ADVICE_EXPORT_BYTES = 65536
MAX_ADVICE_EXPORT_RESPONSE_BYTES = 196608
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
Counter = Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


class AgentAdviceExportRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )
    contract_version: Literal[1] = 1
    consumer: Literal["agent"] = "agent"
    protocol: Literal["measured_agent_advice_v1"] = "measured_agent_advice_v1"
    task: AgentTaskIdentity
    objective_revision: Counter
    source_revisions: dict[Identifier, Counter] = Field(max_length=32)
    context_event_ids: list[Identifier] = Field(default_factory=list, max_length=128)
    content_scope: ContentScope
    instruction_scope: ContentScope
    provider: AgentExportProviderInfo
    policy_sha256: Digest
    logical_request_id: Identifier
    request_sha256: Digest
    purpose: Literal["complete_count", "complete_generation"]
    request_nonce: Identifier

    @field_validator("contract_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported Agent advice export version")
        return value

    @model_validator(mode="after")
    def bounded_identity(self):
        if len(set(self.context_event_ids)) != len(self.context_event_ids):
            raise ValueError("Duplicate Agent advice context identity")
        # The native control grammar checks nested task identities without
        # legacy JSON coercions, duplicate keys or non-finite counters.
        agent_control_json(self.canonical_bytes(), maximum=MAX_ADVICE_EXPORT_BYTES)
        return self

    def canonical_bytes(self):
        return encoded(self.model_dump(mode="json"))

    def binding_digest(self):
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_json(cls, raw):
        return cls.model_validate(agent_control_json(raw, maximum=MAX_ADVICE_EXPORT_BYTES))

    def export_request(self, task_scope):
        return AgentExportRequest(
            task_id=self.task.task_id,
            objective_revision=self.objective_revision,
            session_id=self.task.session_id,
            thread_id=self.task.thread_id,
            epoch=self.task.epoch,
            source_event_ids=sorted(self.source_revisions),
            source_revisions=dict(self.source_revisions),
            context_event_ids=list(self.context_event_ids),
            content_scope=combine_scopes(self.content_scope, self.instruction_scope, task_scope),
            provider=self.provider,
            request_sha256=self.request_sha256,
            purpose=self.purpose,
            request_nonce=self.request_nonce,
        )


class AgentAdviceExportAdmission(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )
    contract_version: Literal[1] = 1
    request: AgentAdviceExportRequest
    binding_sha256: Digest
    task_state: Literal["running"] = "running"
    task_content_scope: ContentScope
    export: AgentExportAdmission

    @field_validator("contract_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported Agent advice admission version")
        return value

    @model_validator(mode="after")
    def exact_export(self):
        if self.binding_sha256 != self.request.binding_digest():
            raise ValueError("Agent advice admission changed its original request")
        self.export.validate_request(self.request.export_request(self.task_content_scope))
        if len(self.canonical_bytes()) > MAX_ADVICE_EXPORT_RESPONSE_BYTES:
            raise ValueError("Agent advice export response exceeds its resource bound")
        return self

    def canonical_bytes(self):
        return encoded(self.model_dump(mode="json"))

    def validate_request(self, request):
        request = AgentAdviceExportRequest.model_validate(request)
        if self.request.canonical_bytes() != request.canonical_bytes():
            raise ValueError("Agent advice admission belongs to another original request")
        return self

    @classmethod
    def from_json(cls, raw):
        return cls.model_validate(agent_control_json(raw, maximum=MAX_ADVICE_EXPORT_RESPONSE_BYTES))
