"""Original measured advice operations; model text never establishes task success."""

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.agent_advice import Counter, Digest, Identifier, encoded
from packages.contracts.agent_control import AgentTaskIdentity
from packages.contracts.agent_export import AgentExportProviderInfo
from packages.contracts.models import CatalogModelBinding, ContentScope

MAX_ADVICE_CALL_BYTES = 4 * 1024 * 1024
MAX_ADVICE_OUTPUT_BYTES = 4 * 1024 * 1024
Instance = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class _Versioned(_Closed):
    contract_version: Literal[1] = 1

    @field_validator("contract_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported Agent advice operation version")
        return value


class AdviceMessage(_Closed):
    role: Literal["system", "user", "assistant"]
    content: str


class AgentAdviceCall(_Versioned):
    protocol: Literal["measured_agent_advice_v1"] = "measured_agent_advice_v1"
    expected_instance: Instance
    policy_sha256: Digest
    logical_request_id: Identifier
    task: AgentTaskIdentity
    objective_revision: Counter
    source_revisions: dict[Identifier, Counter] = Field(max_length=32)
    context_event_ids: list[Identifier] = Field(default_factory=list, max_length=128)
    content_scope: ContentScope
    messages: list[AdviceMessage] = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def bounded_request(self):
        if len(set(self.context_event_ids)) != len(self.context_event_ids):
            raise ValueError("Duplicate Agent advice context identity")
        try:
            if len(self.canonical_bytes()) > MAX_ADVICE_CALL_BYTES:
                raise ValueError("Agent advice request exceeds its resource bound")
        except UnicodeError:
            raise ValueError("Agent advice requires valid UTF-8 input") from None
        return self

    def canonical_bytes(self):
        return encoded(self.model_dump(mode="json"))

    def request_digest(self):
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class AgentAdviceLookup(_Versioned):
    expected_instance: Instance
    logical_request_id: Identifier
    request_sha256: Digest

    @classmethod
    def of(cls, request):
        request = AgentAdviceCall.model_validate(request)
        return cls(
            expected_instance=request.expected_instance,
            logical_request_id=request.logical_request_id,
            request_sha256=request.request_digest(),
        )


class AgentAdviceModelPin(_Versioned):
    policy_sha256: Digest
    catalog_binding: CatalogModelBinding
    catalog_revision: int = Field(ge=0, le=9_007_199_254_740_991)
    provider: AgentExportProviderInfo
    reasoning: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None


class AgentAdviceObservation(_Versioned):
    protocol: Literal["measured_agent_advice_v1"] = "measured_agent_advice_v1"
    instance_id: Instance
    logical_request_id: Identifier
    request_sha256: Digest
    policy_sha256: Digest
    task: AgentTaskIdentity
    objective_revision: Counter
    phase: Literal[
        "pending",
        "running",
        "closing",
        "completed",
        "cancelled",
        "failed",
        "setup_unconfirmed",
        "cleanup_unconfirmed",
    ]
    cancellation_requested: bool
    cleanup: Literal["pending", "confirmed", "unconfirmed"]
    model_pin: AgentAdviceModelPin | None
    content_scope: ContentScope
    model_returned: bool
    output_state: Literal["not_returned", "retained", "resource_refused", "invalid_text"]
    model_output: str | None
    output_sha256: Digest | None
    output_bytes: int | None = Field(ge=0)
    generation_admission_observed: bool
    error: (
        Literal[
            "provider_failed",
            "native_admission_refused",
            "policy_unavailable",
            "invalid_output",
            "output_exceeds_bound",
            "original_setup_unconfirmed",
            "actual_cleanup_unconfirmed",
        ]
        | None
    )
    output_authority: Literal["untrusted_model_data"] = "untrusted_model_data"
    task_status_assessment: Literal["not_assessed"] = "not_assessed"

    @model_validator(mode="after")
    def coherent_observation(self):
        if self.model_returned != (self.output_state != "not_returned"):
            raise ValueError("Agent advice output observation is inconsistent")
        if (self.model_output is not None) != (self.output_state == "retained"):
            raise ValueError("Agent advice output retention is inconsistent")
        if self.output_state in {"retained", "resource_refused"}:
            if self.output_sha256 is None or self.output_bytes is None:
                raise ValueError("Agent advice output fingerprint is unavailable")
        elif self.output_sha256 is not None or self.output_bytes is not None:
            raise ValueError("Agent advice has no observed output fingerprint")
        if self.output_state == "retained":
            raw = self.model_output.encode("utf-8")
            if (
                len(raw) > MAX_ADVICE_OUTPUT_BYTES
                or len(raw) != self.output_bytes
                or hashlib.sha256(raw).hexdigest() != self.output_sha256
            ):
                raise ValueError("Agent advice output fingerprint changed")
        if self.phase in {"completed", "cancelled", "failed"} and self.cleanup != "confirmed":
            raise ValueError("Agent advice cannot settle before actual cleanup")
        if self.phase == "completed" and self.output_state != "retained":
            raise ValueError("Completed advice requires retained model output")
        if self.model_pin is not None and self.model_pin.policy_sha256 != self.policy_sha256:
            raise ValueError("Agent advice model pin belongs to another policy")
        return self

    def validate_lookup(self, lookup):
        lookup = AgentAdviceLookup.model_validate(lookup)
        if (self.instance_id, self.logical_request_id, self.request_sha256) != (
            lookup.expected_instance,
            lookup.logical_request_id,
            lookup.request_sha256,
        ):
            raise ValueError("Agent advice observation belongs to another original request")
        return self
