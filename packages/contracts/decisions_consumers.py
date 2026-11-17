"""Independent Decisions consumer configuration; no execution authority."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.agent_advice import encoded
from packages.contracts.agent_control import agent_control_json
from packages.contracts.models import CatalogModelBinding, ContentScope

MAX_POLICY_CHANGES = 1000
MAX_POLICY_CHANGE_BYTES = 65536
MAX_POLICY_SETTINGS_BYTES = 163840
_Identity = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]
_ChangeKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9:._-]+$", min_length=1, max_length=128)]
_Revision = Annotated[int, Field(ge=0, le=MAX_POLICY_CHANGES)]
_SelectionSource = Literal["host", "disabled", "saved"]
_CatalogStatus = Literal["not_selected", "eligible", "unavailable"]


class _ConsumerContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    contract_version: Literal[1] = 1

    @field_validator("contract_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported Decisions consumer contract version")
        return value


class DecisionsAgentPolicy(_ConsumerContract):
    """Host-owned optional advisory policy, referencing Core's existing catalog."""

    consumer: Literal["agent"] = "agent"
    protocol: Literal["measured_agent_advice_v1"] = "measured_agent_advice_v1"
    catalog_binding: CatalogModelBinding
    reasoning: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    instructions: str = Field(max_length=8192)
    content_scope: ContentScope = "local_only"

    def fingerprint(self):
        raw = json.dumps(
            self.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @field_validator("instructions")
    @classmethod
    def bounded_instructions(cls, value):
        try:
            if "\x00" in value or len(value.encode("utf-8")) > 8192:
                raise ValueError("Agent decision instructions exceed the UTF-8 resource bound")
        except UnicodeError:
            raise ValueError("Agent decision instructions require valid UTF-8") from None
        return value


class DecisionsAgentPolicyState(_ConsumerContract):
    """Read-only configuration observation, never model or request readiness."""

    owner: Literal["decisions"] = "decisions"
    consumer: Literal["agent"] = "agent"
    source: Literal["host", "unconfigured", "invalid_host"]
    policy: DecisionsAgentPolicy | None
    policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    catalog_status: Literal["not_selected", "eligible", "unavailable"]
    observation: Literal["catalog_metadata_only"] = "catalog_metadata_only"
    change_boundary: Literal["restart_required"] = "restart_required"
    request_admission: Literal["unavailable"] = "unavailable"

    @model_validator(mode="after")
    def coherent_source(self):
        selected = self.source == "host"
        if selected != (self.policy is not None and self.policy_sha256 is not None):
            raise ValueError("Agent decision policy observation has inconsistent provenance")
        if not selected and (self.policy is not None or self.policy_sha256 is not None):
            raise ValueError("Unselected Agent decision policy cannot carry a host selection")
        if selected == (self.catalog_status == "not_selected"):
            raise ValueError("Agent decision catalog observation has inconsistent selection")
        if selected and self.policy_sha256 != self.policy.fingerprint():
            raise ValueError("Agent decision policy digest does not match its host snapshot")
        return self


class DecisionsAgentPolicySelection(_ConsumerContract):
    """Retain a host pointer, an explicit disable, or one complete saved policy."""

    source: _SelectionSource
    policy: DecisionsAgentPolicy | None = None

    @model_validator(mode="after")
    def exact_selection(self):
        if (self.source == "saved") != (self.policy is not None):
            raise ValueError("Only a saved selection contains an Agent advice policy")
        return self


class DecisionsAgentPolicyObservation(_ConsumerContract):
    source: Literal["host", "unconfigured", "invalid_host", "disabled", "saved", "unavailable"]
    policy: DecisionsAgentPolicy | None
    policy_sha256: _Digest | None
    catalog_status: _CatalogStatus

    @model_validator(mode="after")
    def exact_observation(self):
        if self.source in {"host", "saved"}:
            if (
                self.policy is None
                or self.policy_sha256 != self.policy.fingerprint()
                or self.catalog_status == "not_selected"
            ):
                raise ValueError("Selected Agent policy observation is inconsistent")
        elif (
            self.policy is not None
            or self.policy_sha256 is not None
            or self.catalog_status != "not_selected"
        ):
            raise ValueError("Unselected Agent policy observation is inconsistent")
        return self


class DecisionsAgentPolicySettingsSnapshot(_ConsumerContract):
    owner: Literal["decisions"] = "decisions"
    consumer: Literal["agent"] = "agent"
    backend_id: _Identity
    instance_id: _Identity
    revision: _Revision
    selection_source: _SelectionSource
    host: DecisionsAgentPolicyState
    active: DecisionsAgentPolicyObservation
    next_launch: DecisionsAgentPolicyObservation
    change_boundary: Literal["restart_required"] = "restart_required"
    writable: bool

    @model_validator(mode="after")
    def exact_source(self):
        if self.selection_source == "host":
            if (
                self.next_launch.source != self.host.source
                or self.next_launch.policy != self.host.policy
                or self.next_launch.catalog_status != self.host.catalog_status
            ):
                raise ValueError("The next-launch policy does not match the host selection")
        elif self.next_launch.source != self.selection_source:
            raise ValueError("The next-launch policy does not match its saved source")
        if self.writable != (self.revision < MAX_POLICY_CHANGES):
            raise ValueError("The policy settings write budget is inconsistent")
        return self


class DecisionsAgentPolicyChange(_ConsumerContract):
    expected_backend: _Identity
    expected_instance: _Identity
    expected_revision: _Revision
    idempotency_key: _ChangeKey
    selection: DecisionsAgentPolicySelection

    @classmethod
    def from_json(cls, raw):
        return cls.model_validate(agent_control_json(raw, maximum=MAX_POLICY_CHANGE_BYTES))

    def fingerprint(self):
        return hashlib.sha256(encoded(self.model_dump(mode="json"))).hexdigest()


class DecisionsAgentPolicyReceipt(_ConsumerContract):
    status: Literal["saved"] = "saved"
    backend_id: _Identity
    instance_id: _Identity
    revision: Annotated[int, Field(ge=1, le=MAX_POLICY_CHANGES)]
    idempotency_key: _ChangeKey
    request_sha256: _Digest
    selection_source: _SelectionSource
    policy_sha256: _Digest | None
    change_boundary: Literal["restart_required"] = "restart_required"

    @model_validator(mode="after")
    def exact_selection(self):
        if (self.selection_source == "saved") != (self.policy_sha256 is not None):
            raise ValueError("The receipt policy digest does not match its source")
        return self


class DecisionsAgentPolicyRefusal(_ConsumerContract):
    """The owner evaluated this exact selection before any persistence."""

    status: Literal["refused"] = "refused"
    request_sha256: _Digest
    reason: Literal["selection_unavailable"] = "selection_unavailable"


class DecisionsAgentPolicyLookup(_ConsumerContract):
    backend_id: _Identity
    instance_id: _Identity
    current_revision: _Revision
    request_sha256: _Digest
    receipt: DecisionsAgentPolicyReceipt | None
    mutation_fenced: bool

    @model_validator(mode="after")
    def exact_receipt(self):
        if self.receipt is not None and (
            self.mutation_fenced
            or self.receipt.backend_id != self.backend_id
            or self.receipt.request_sha256 != self.request_sha256
            or self.receipt.revision > self.current_revision
        ):
            raise ValueError("The receipt does not belong to this policy lookup")
        return self
