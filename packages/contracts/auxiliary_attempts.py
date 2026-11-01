"""Closed native auxiliary trace payloads; no prompts, endpoints or secrets."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.auxiliary_routes import MAX_ROUTE_REVISION, AuxiliaryRouteScope, Reasoning
from packages.contracts.models import AuxiliaryTask, CatalogModelBinding, ContentScope

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
EventId = Annotated[
    str, Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
]
Counter = Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]
Revision = Annotated[int, Field(ge=0, le=MAX_ROUTE_REVISION)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AttemptModelPin(_Closed):
    schema_version: Literal["1.0"]
    profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    provider: Literal["meta", "lmstudio"]
    model: str = Field(min_length=1, max_length=256)
    local: bool
    reasoning: Reasoning | None
    settings_revision: Revision
    catalog_binding: CatalogModelBinding
    catalog_revision: Revision

    @model_validator(mode="after")
    def exact_identity(self):
        if (
            self.profile_id != self.catalog_binding.id
            or (self.provider == "lmstudio" and (not self.local or self.reasoning != "none"))
            or (self.provider == "meta" and self.local)
        ):
            raise ValueError("Invalid native auxiliary model pin")
        self.model.encode("utf-8")
        if "\0" in self.model:
            raise ValueError("Invalid native auxiliary model identity")
        return self


class AttemptCredential(_Closed):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")


class AttemptCandidate(_Closed):
    ordinal: int = Field(ge=1, le=3)
    model_pin: AttemptModelPin
    cloud_scope: ContentScope
    credential: AttemptCredential | None

    @model_validator(mode="after")
    def native_ceiling(self):
        if self.cloud_scope != ("local_only" if self.model_pin.local else "public_synthetic"):
            raise ValueError("Unsupported native auxiliary disclosure ceiling")
        if self.model_pin.provider == "meta" and self.credential is None:
            raise ValueError("Native Meta route requires an opaque credential generation")
        return self


class AttemptRoute(_Closed):
    purpose: AuxiliaryTask
    harness_version: str | None = Field(pattern=r"^(seed-v1|v-[a-f0-9]{24})$")
    settings_revision: Revision
    route_revision: Revision
    candidates: list[AttemptCandidate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def ordered(self):
        if (
            [candidate.ordinal for candidate in self.candidates]
            != list(range(1, len(self.candidates) + 1))
            or any(
                candidate.model_pin.settings_revision != self.settings_revision
                for candidate in self.candidates
            )
            or len({candidate.model_pin.catalog_binding for candidate in self.candidates})
            != len(self.candidates)
        ):
            raise ValueError("Native auxiliary route is not exact and ordered")
        return self

    def sha256(self):
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()


class AuxiliaryAttempt(_Closed):
    schema_version: Literal["1.0"]
    task_id: Identifier
    state: Literal["running"]
    objective_revision: Counter
    plugin_versions: dict[Identifier, Identifier] = Field(max_length=64)
    content_scope: ContentScope
    submit_sequence: Counter
    purpose: AuxiliaryTask
    logical_request_id: EventId
    logical_request_sha256: Digest
    route_sha256: Digest
    route_revision: Revision
    route_event_id: EventId | None
    phase: Literal[
        "route_captured", "attempt_started", "export_admitted", "attempt_finished", "route_stopped"
    ]
    ordinal: int | None = Field(ge=1, le=3)
    model_pin: AttemptModelPin | None
    route: AttemptRoute | None = None
    export_purpose: Literal["complete_count", "complete_generation"] | None = None
    request_sha256: Digest | None = None
    effective_scope: ContentScope | None = None
    outcome: (
        Literal[
            "completed",
            "context_overflow",
            "disclosure_ineligible",
            "endpoint_refused",
            "model_not_loaded",
            "count_rate_limited",
            "count_unavailable",
            "unconfirmed",
            "refused",
        ]
        | None
    ) = None
    generation_state: Literal["not_started", "completed", "unconfirmed"] | None = None
    cleanup_state: Literal["not_opened", "settled", "unconfirmed"] | None = None
    input_tokens: Counter | None = None
    context_tokens: Counter | None = None
    reserved_output_tokens: Counter | None = None
    output_sha256: Digest | None = None
    stage: Literal["route", "prepare", "generation"] | None = None

    @field_validator("task_id")
    @classmethod
    def safe_text(cls, value):
        if "\0" in value:
            raise ValueError("Invalid native task identity")
        value.encode("utf-8")
        return value

    @field_validator("plugin_versions")
    @classmethod
    def safe_plugins(cls, value):
        for key, child in value.items():
            cls.safe_text(key)
            cls.safe_text(child)
        return value

    @model_validator(mode="after")
    def exact_phase(self):
        base = {
            "schema_version",
            "task_id",
            "state",
            "objective_revision",
            "plugin_versions",
            "content_scope",
            "submit_sequence",
            "purpose",
            "logical_request_id",
            "logical_request_sha256",
            "route_sha256",
            "route_revision",
            "route_event_id",
            "phase",
            "ordinal",
            "model_pin",
        }
        fields = self.model_fields_set - base
        if self.phase == "route_captured":
            if (
                fields != {"route"}
                or self.route is None
                or self.ordinal is not None
                or self.model_pin is not None
                or self.route_event_id is not None
                or self.route.purpose != self.purpose
                or self.route.route_revision != self.route_revision
                or self.route.sha256() != self.route_sha256
            ):
                raise ValueError("Invalid native captured route trace")
            return self
        if self.route_event_id is None:
            # A stopped pre-capture journal operation can have no route receipt.
            if (
                self.phase != "route_stopped"
                or self.ordinal is not None
                or self.model_pin is not None
            ):
                raise ValueError("Native attempt requires its captured route receipt")
        elif self.phase != "route_stopped" and (self.ordinal is None or self.model_pin is None):
            raise ValueError("Native attempt requires its exact candidate pin")
        if self.phase == "attempt_started":
            valid = (
                fields == {"generation_state", "cleanup_state"}
                and self.generation_state == "not_started"
                and self.cleanup_state == "not_opened"
            )
        elif self.phase == "export_admitted":
            valid = fields == {"export_purpose", "request_sha256", "effective_scope"} and all(
                value is not None
                for value in (self.export_purpose, self.request_sha256, self.effective_scope)
            )
        elif self.phase == "attempt_finished":
            expected = {"outcome", "generation_state", "cleanup_state"}
            if self.outcome == "completed":
                expected.add("output_sha256")
                valid = (
                    self.generation_state == "completed"
                    and self.output_sha256 is not None
                    and self.cleanup_state == "settled"
                )
            elif self.outcome == "context_overflow":
                expected |= {
                    "request_sha256",
                    "input_tokens",
                    "context_tokens",
                    "reserved_output_tokens",
                }
                valid = (
                    self.generation_state == "not_started"
                    and self.cleanup_state == "settled"
                    and self.request_sha256 is not None
                    and all(
                        v is not None
                        for v in (
                            self.input_tokens,
                            self.context_tokens,
                            self.reserved_output_tokens,
                        )
                    )
                    and self.input_tokens + self.reserved_output_tokens > self.context_tokens
                )
            elif self.outcome == "disclosure_ineligible":
                valid = self.generation_state == "not_started" and self.cleanup_state in {
                    "not_opened",
                    "settled",
                }
            elif self.outcome in {
                "endpoint_refused",
                "model_not_loaded",
                "count_rate_limited",
                "count_unavailable",
            }:
                expected.add("request_sha256")
                valid = self.generation_state == "not_started" and self.cleanup_state == "settled"
                if self.outcome in {"count_rate_limited", "count_unavailable"}:
                    valid = valid and self.request_sha256 is not None
            else:
                valid = False
            valid = valid and fields == expected
        else:
            valid = (
                fields == {"outcome", "generation_state", "cleanup_state", "stage"}
                and self.outcome in {"unconfirmed", "refused"}
                and self.generation_state in {"not_started", "unconfirmed"}
                and self.cleanup_state in {"settled", "unconfirmed"}
                and self.stage is not None
            )
        if not valid:
            raise ValueError("Invalid native auxiliary attempt phase")
        return self


def route_scope(route):
    return AuxiliaryRouteScope(purpose=route.purpose, harness_version=route.harness_version)
