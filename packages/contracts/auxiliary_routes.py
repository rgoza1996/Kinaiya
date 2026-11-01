"""Owner configuration of ordered, immutable auxiliary model routes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.models import AuxiliaryTask, CatalogModelBinding, ContentScope

MAX_ROUTE_REVISION = 9_007_199_254_740_990
Reasoning = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AuxiliaryRouteScope(_Closed):
    purpose: AuxiliaryTask
    harness_version: str | None = Field(pattern=r"^(seed-v1|v-[a-f0-9]{24})$")


class AuxiliaryRouteCandidate(_Closed):
    catalog_binding: CatalogModelBinding
    reasoning: Reasoning | None
    # This is a maximum disclosure ceiling, never a cloud permission grant.
    cloud_scope: ContentScope


class AuxiliaryRoute(_Closed):
    candidates: list[AuxiliaryRouteCandidate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_generations(self):
        identities = [candidate.catalog_binding for candidate in self.candidates]
        if len(set(identities)) != len(identities):
            raise ValueError("A route may select each exact catalog generation once")
        return self


class AuxiliaryRouteChange(_Closed):
    schema_version: Literal["1.0"]
    expected_backend: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_instance: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_revision: int = Field(ge=0, le=MAX_ROUTE_REVISION)
    expected_state: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,128}$")
    scope: AuxiliaryRouteScope
    route: AuxiliaryRoute | None
