"""Retained close ownership for fresh catalog adapters captured by one task."""

import asyncio

from packages.contracts.operation_ownership import actual_owner, current_owner
from packages.providers.text import ProviderCleanupError


class TaskModelLease:
    def __init__(self, released):
        self.adapters = []
        self.cleanup = None
        self.failed = False
        self.settled = False
        self._released = released
        self.auxiliary_routes = {}
        self.retiring = set()
        self.retiring_providers = set()
        self.ownership = None

    def retain(self, owner):
        if self.ownership is not None and self.ownership is not owner:
            owner.fail()
            raise ProviderCleanupError("Catalog task cleanup owner changed")
        if self.ownership is owner:
            return
        self.ownership = owner
        if self.cleanup is not None:
            owner.retain_task(self.cleanup, cleanup=True)
        for task in self.retiring:
            owner.retain_task(task, cleanup=True)

    def own(self, provider):
        if self.cleanup is not None or self.failed or len(self.adapters) >= 6:
            raise ValueError("Catalog task model lease exceeds its native bound")
        self.adapters.append(provider)

    async def retire(self, provider):
        """Advance a route only after its actual adapter has closed."""
        if (
            self.cleanup is not None
            or provider not in self.adapters
            or id(provider) in self.retiring_providers
        ):
            raise ProviderCleanupError("Catalog attempt close ownership changed")

        async def close_owned():
            await self._close_one(provider)
            # Removal belongs to the actual close operation. Shutdown waiting
            # on it cannot race its waiter's bookkeeping and close it again.
            if provider in self.adapters:
                self.adapters.remove(provider)

        with actual_owner(self.ownership if self.ownership is not None else current_owner()):
            operation = asyncio.create_task(close_owned())
        if self.ownership is not None:
            self.ownership.retain_task(operation, cleanup=True)
        self.retiring.add(operation)
        self.retiring_providers.add(id(provider))
        interrupted = False
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                break
        try:
            operation.result()
        except BaseException:
            self.failed = True
            self.start_close()
            raise ProviderCleanupError("Catalog attempt cleanup is unconfirmed") from None
        finally:
            self.retiring.discard(operation)
            self.retiring_providers.discard(id(provider))
        if interrupted:
            raise asyncio.CancelledError

    @staticmethod
    async def _close_one(provider):
        adapter = getattr(provider, "provider", provider)
        await adapter.close()
        client = getattr(adapter, "http", None)
        if client is not None:
            client.headers.clear()

    def start_close(self):
        if self.cleanup is None:
            with actual_owner(self.ownership if self.ownership is not None else current_owner()):
                self.cleanup = asyncio.create_task(self._close())
            if self.ownership is not None:
                self.ownership.retain_task(self.cleanup, cleanup=True)
            self.cleanup.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return self.cleanup

    async def close(self):
        return await asyncio.shield(self.start_close())

    async def _close(self):
        for route in self.auxiliary_routes.values():
            route.release()
        if self.retiring:
            retired = await asyncio.gather(*tuple(self.retiring), return_exceptions=True)
            if any(isinstance(result, BaseException) for result in retired):
                self.failed = True
        results = await asyncio.gather(
            *(self._close_one(p) for p in self.adapters), return_exceptions=True
        )
        if self.failed or any(isinstance(result, BaseException) for result in results):
            self.failed = True
            raise ProviderCleanupError("Catalog task provider cleanup is unconfirmed")
        self.settled = True
        self.adapters.clear()
        self.auxiliary_routes.clear()
        self._released(self)
