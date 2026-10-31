"""Bounded owner inspection and creation; metadata never grants execution."""

from typing import Literal

from pydantic import Field, model_validator

from packages.contracts.models import Session, WireModel


class AgentSessionCreate(WireModel):
    harness_version: str = Field(pattern=r"^(seed-v1|v-[a-f0-9]{24})$")
    name: str = Field(default="", max_length=200)


class AgentSessionPageRequest(WireModel):
    channel: Literal["agent"] = "agent"
    harness_version: str | None = Field(
        default=None, pattern=r"^(seed-v1|v-[a-f0-9]{24}|unassociated)$"
    )
    after: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    limit: int = Field(default=20, ge=1, le=20)


class AgentSessionPage(WireModel):
    items: list[Session] = Field(max_length=20)
    next_cursor: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")

    @model_validator(mode="after")
    def agent_sessions_only(self):
        if any(session.channel != "agent" for session in self.items):
            raise ValueError("Agent session page contains a different channel")
        if len({session.session_id for session in self.items}) != len(self.items):
            raise ValueError("Agent session page repeats a session")
        return self

    def validate_request(self, request: AgentSessionPageRequest):
        if len(self.items) > request.limit:
            raise ValueError("Agent session page exceeds the requested limit")
        expected = request.harness_version
        if expected is not None:
            expected = None if expected == "unassociated" else expected
            if any(session.harness_version != expected for session in self.items):
                raise ValueError("Agent session page has a different harness scope")
        if self.next_cursor is not None and (
            not self.items
            or (request.after is not None and int(self.next_cursor) >= int(request.after))
        ):
            raise ValueError("Agent session page cursor does not advance")
        return self
