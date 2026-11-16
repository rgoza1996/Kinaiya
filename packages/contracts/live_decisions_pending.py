"""Bounded durable live-input discovery; a page grants no inference authority."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.live_decisions_attempts import LiveDecisionsSource
from packages.contracts.live_windows import MAX_INTEGER


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class LiveDecisionsPendingQuery(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_decisions_v2"]
    after_sequence: int = Field(ge=0, le=MAX_INTEGER)
    limit: int = Field(ge=1, le=32)


class LiveDecisionsPendingPage(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_decisions_v2"]
    original: LiveDecisionsPendingQuery
    sources: list[LiveDecisionsSource] = Field(max_length=32)
    through_sequence: int = Field(ge=0, le=MAX_INTEGER)
    has_more: bool

    @model_validator(mode="after")
    def exact_page(self):
        sequences = [source.source_sequence for source in self.sources]
        if (
            len(sequences) > self.original.limit
            or sequences != sorted(set(sequences))
            or any(sequence <= self.original.after_sequence for sequence in sequences)
            or self.through_sequence
            != (sequences[-1] if sequences else self.original.after_sequence)
            or self.has_more
            and len(sequences) != self.original.limit
        ):
            raise ValueError("Pending live page changed its original bounded ordering")
        return self
