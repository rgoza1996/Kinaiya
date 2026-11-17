"""Native semantic revision equivalence, never an Agent execution grant."""

from typing import Literal

from pydantic import Field

from packages.contracts.decisions_attempts import DecisionsSource
from packages.contracts.live_effects import _Closed


class LiveAgentSourceQuery(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_agent_source_v2"]
    source: DecisionsSource
    expected_revision: int = Field(ge=1, le=(1 << 53) - 1)


class LiveAgentSourceState(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_agent_source_v2"]
    original: LiveAgentSourceQuery
    live: bool
    revision_equivalent: bool
