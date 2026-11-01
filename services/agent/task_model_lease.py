"""Retained close ownership for fresh catalog adapters captured by one task."""

import asyncio

from packages.providers.text import ProviderCleanupError


class TaskModelLease:
    def __init__(self, released):
        self.adapters = []
        self.cleanup = None
        self.failed = False
        self.settled = False
        self._released = released

    def own(self, provider):
        if self.cleanup is not None or len(self.adapters) >= 6:
            raise ValueError("Catalog task model lease exceeds its native bound")
        self.adapters.append(provider)

    def start_close(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
            self.cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self.cleanup

    async def close(self):
        return await asyncio.shield(self.start_close())

    async def _close(self):
        async def close_one(provider):
            adapter = getattr(provider, "provider", provider)
            await adapter.close()
            # These are fresh native catalog adapters only. After real close,
            # historical task inspection needs identity, never retained keys.
            client = getattr(adapter, "http", None)
            if client is not None:
                client.headers.clear()

        results = await asyncio.gather(
            *(close_one(p) for p in self.adapters), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            self.failed = True
            raise ProviderCleanupError("Catalog task provider cleanup is unconfirmed")
        self.settled = True
        self.adapters.clear()
        self._released(self)
