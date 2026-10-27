"""Dependency interfaces shared across independently launched services."""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from packages.contracts.models import AppendRequest, JournalEntry, ProviderInfo, Session


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


class TextProvider(Protocol):
    info: ProviderInfo

    async def complete(self, messages: list[dict[str, str]]) -> str: ...

    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...


class AgentAdapter(Protocol):
    async def submit(self, request: Any, *, actor: str = "owner") -> Any: ...

    async def steer(self, task_id: str, request: Any, *, actor: str = "owner") -> Any: ...

    async def interrupt(self, task_id: str, request: Any, *, actor: str = "owner") -> Any: ...

    async def status(self, task_id: str | None = None) -> Any: ...
