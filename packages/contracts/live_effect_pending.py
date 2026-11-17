"""Bounded passive discovery of native live consumer work, never a work grant."""

from typing import Literal

from pydantic import Field, model_validator

from packages.contracts.live_decisions_attempts import LiveDecisionsPublication
from packages.contracts.live_effects import LiveEffectQuery, _Closed

MAX_PAGE_BYTES = 262144


class LiveEffectPendingQuery(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    effect: Literal["converse_reply", "converse_backchannel", "agent_route"]
    after_sequence: int = Field(ge=0, le=(1 << 53) - 1)
    limit: int = Field(ge=1, le=32)


class LiveEffectPendingItem(_Closed):
    event_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1, le=(1 << 53) - 1)
    publication: LiveDecisionsPublication

    def query(self, effect):
        return LiveEffectQuery(
            schema_version="2.0",
            protocol="native_live_effect_v2",
            publication=self.publication,
            effect=effect,
        )


class LiveEffectPendingPage(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    original: LiveEffectPendingQuery
    items: list[LiveEffectPendingItem] = Field(max_length=32)
    through_sequence: int = Field(ge=0, le=(1 << 53) - 1)
    exhausted: bool

    @model_validator(mode="after")
    def exact_progress(self):
        previous = self.original.after_sequence
        if len(self.items) > self.original.limit:
            raise ValueError("Live consumer page exceeded its original limit")
        for item in self.items:
            if item.sequence <= previous:
                raise ValueError("Live consumer page changed native ordering")
            previous = item.sequence
        if self.through_sequence != previous:
            raise ValueError("Live consumer page changed its native cursor")
        if not self.items and not self.exhausted:
            raise ValueError("An unexhausted page must make positive native progress")
        return self
