"""Dependency interfaces shared across independently launched services."""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from packages.contracts.agent_submit import (
    AgentSubmitLookupRequest,
    AgentSubmitLookupResult,
    AgentSubmitReceipt,
)
from packages.contracts.models import (
    AppendRequest,
    JournalEntry,
    PlaybackGeneration,
    ProviderInfo,
    Session,
)


class Journal(Protocol):
    async def append(self, request: AppendRequest) -> JournalEntry: ...

    async def read(
        self,
        *,
        after: int = 0,
        limit: int = 100,
        session_id: str | None = None,
        thread_id: str | None = None,
        kinds: list[str] | None = None,
        task_id: str | None = None,
        search: str | None = None,
    ) -> list[JournalEntry]: ...

    async def session(self, session_id: str) -> Session: ...

    async def playback_generation(
        self, session_id: str, thread_id: str, epoch: int
    ) -> PlaybackGeneration: ...

    async def latest_window(
        self, session_id: str, epoch: int, utterance_id: str, window_index: int | None = None
    ) -> JournalEntry | None: ...

    async def window_decision(self, source_event_id: str) -> JournalEntry | None: ...

    async def pending_windows(self, *, limit: int = 100) -> list[JournalEntry]: ...

    async def window_classification_attempts(self, source_event_id: str) -> int: ...

    async def window_response(
        self, session_id: str, epoch: int, utterance_id: str
    ) -> JournalEntry | None: ...

    async def window_dispatches(self, **query) -> list[JournalEntry]: ...

    async def window_control(
        self, session_id: str, epoch: int, agent_interrupt: bool = False
    ) -> JournalEntry | None: ...

    async def admit_submit(self, request: AppendRequest) -> AgentSubmitReceipt: ...

    async def lookup_submit(self, request: AgentSubmitLookupRequest) -> AgentSubmitLookupResult: ...


class TextProvider(Protocol):
    info: ProviderInfo

    async def complete(self, messages: list[dict[str, str]]) -> str: ...

    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...


class AgentAdapter(Protocol):
    async def submit(self, request: Any, *, actor: str = "owner") -> Any: ...

    async def steer(self, task_id: str, request: Any, *, actor: str = "owner") -> Any: ...

    async def interrupt(self, task_id: str, request: Any, *, actor: str = "owner") -> Any: ...

    async def status(self, task_id: str | None = None) -> Any: ...
