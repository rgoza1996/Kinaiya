"""Process-local optional recall controls and retained operation ownership."""

import asyncio
import json
from dataclasses import dataclass, field

from packages.contracts.memory import MemoryBinding
from packages.contracts.memory_context import MemoryUseState
from packages.providers.prompt_budget import PromptTokenCount
from packages.providers.text import ProviderCleanupError, owned_http_stream_cleanup

MAX_GENERATION = 2**53 - 1


def scope_key(binding):
    return binding.session_id, binding.thread_id, binding.epoch


def _provider_stamp(provider):
    inner = getattr(provider, "provider", provider)
    configured = json.dumps(
        {
            "info": provider.info.model_dump(mode="json"),
            "binding": getattr(provider, "managed_binding", None),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    configured.encode("utf-8")
    return (
        id(inner),
        getattr(provider, "role", None),
        str(getattr(provider, "state_path", "")),
        configured,
    )


class MemoryTask(asyncio.Task):
    def cancel(self, msg=None):
        if self.cancelling():
            return False
        return super().cancel(msg)


@dataclass(eq=False, frozen=True)
class MemoryChoice:
    binding: MemoryBinding
    generation: int
    enabled: bool
    implementation_id: str | None
    selector_ids: tuple[str, ...]
    adapter: object


@dataclass(eq=False)
class MemoryLease:
    registry: object
    choice: MemoryChoice
    cancel: object
    snapshot: object = None
    finished: bool = False
    failed: bool = False
    tasks: set = field(default_factory=set)
    reads: set = field(default_factory=set)
    publications: dict = field(default_factory=dict)
    provider: object = None
    provider_stamp: object = None

    def current(self):
        if not self.registry.current(self.choice):
            return False
        try:
            return self.provider is None or self.provider_stamp == _provider_stamp(self.provider)
        except Exception:
            return False

    def _settled(self, task, *, publication=False):
        self.tasks.discard(task)
        self.reads.discard(task)
        if task.cancelled():
            if publication:
                self.failed = True
        else:
            error = task.exception()
            if isinstance(error, ProviderCleanupError) or (publication and error is not None):
                # A transport error cannot establish that an append did not commit.
                self.failed = True
        self.registry.retire(self)

    async def run(self, operation):
        async def owned():
            with owned_http_stream_cleanup():
                return await operation

        task = MemoryTask(owned())
        self.tasks.add(task)
        self.reads.add(task)
        task.add_done_callback(self._settled)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def publish(self, journal, request):
        key = request.idempotency_key
        if key not in self.publications:
            if not self.current() or len(self.publications) >= 64:
                raise RuntimeError("Memory publication is unavailable")
            exact = request.model_copy(deep=True)
            task = asyncio.create_task(journal.append(exact))
            self.publications[key] = (exact, task)
            # A completed window producer can retain chunks for a later speaking
            # opening. Its new write reacquires ownership before its first await.
            self.registry.leases.add(self)
            self.tasks.add(task)
            task.add_done_callback(lambda task: self._settled(task, publication=True))
        exact, task = self.publications[key]
        if request != exact:
            raise ValueError("Memory publication identity changed")
        # Do not cancel an already dispatched write when its caller is stopped.
        return await asyncio.shield(task)

    def finish(self):
        self.finished = True
        self.registry.retire(self)

    def retain_cleanup(self, task, failed):
        if task is None:
            return
        self.tasks.add(task)

        def settled(task):
            if failed():
                self.failed = True
            self._settled(task)

        task.add_done_callback(settled)


class MemoryUse:
    capacity = 32

    def __init__(self, runtime_id, adapters=None, *, on_revoke, pending_extra=lambda _: False):
        self.runtime_id = runtime_id
        self.adapters = dict(adapters or {})
        self.retired_adapters = {}
        if len(self.adapters) > 4:
            raise ValueError("Too many Converse memory adapters")
        self.choices = {}
        self.leases = set()
        self.generation = 0
        self.notices = {}
        self.on_revoke = on_revoke
        self.pending_extra = pending_extra
        self.closed = False

    def current(self, choice):
        return (
            not self.closed
            and choice.enabled
            and self.choices.get(scope_key(choice.binding)) is choice
        )

    def pending(self, binding):
        key = scope_key(binding)
        return any(
            scope_key(lease.choice.binding) == key for lease in self.leases
        ) or self.pending_extra(binding)

    def retire(self, lease):
        if lease.finished and not lease.tasks and not lease.failed:
            self.leases.discard(lease)

    def state(self, binding):
        choice = self.choices.get(scope_key(binding))
        enabled = choice is not None and self.current(choice)
        pending = self.pending(binding)
        return MemoryUseState(
            **binding.model_dump(),
            runtime_id=self.runtime_id,
            use_generation=choice.generation if choice else 0,
            enabled=enabled,
            implementation_id=choice.implementation_id if choice else None,
            selector_ids=list(choice.selector_ids) if choice else [],
            pending=pending,
            notice=(
                "disabled"
                if not enabled
                else "unavailable"
                if choice.adapter is None or pending
                else self.notices.get(scope_key(binding), "ready")
            ),
        )

    def configure(self, request):
        binding = MemoryBinding(
            session_id=request.session_id, thread_id=request.thread_id, epoch=request.epoch
        )
        key = scope_key(binding)
        old = self.choices.get(key)
        if request.expected_generation != (old.generation if old else 0):
            raise ValueError("Memory use generation changed")
        if request.enabled and self.closed:
            raise RuntimeError("Memory use is closed")
        if old is None and not request.enabled:
            return self.state(binding)
        if request.enabled and self.generation >= MAX_GENERATION:
            raise ValueError("Memory use generation exhausted; restart Converse")
        if old is None and len(self.choices) >= self.capacity:
            victim = next(
                (
                    k
                    for k, v in self.choices.items()
                    if not v.enabled and not self.pending(v.binding)
                ),
                None,
            )
            if victim is None:
                raise RuntimeError("Memory use capacity is busy")
            self.choices.pop(victim)
            self.notices.pop(victim, None)
        self.generation = min(MAX_GENERATION, self.generation + 1)
        selected = request.implementation_id if request.enabled else old.implementation_id
        selectors = tuple(request.selector_ids) if request.enabled else old.selector_ids
        choice = MemoryChoice(
            binding,
            self.generation,
            request.enabled,
            selected,
            selectors,
            self.adapters.get(selected),
        )
        # Revoke by object identity even when the safe integer counter is exhausted.
        self.choices[key] = choice
        self.notices.pop(key, None)
        if old is not None:
            self._revoke(old)
        return self.state(binding)

    def acquire(self, binding, cancel):
        choice = self.choices.get(scope_key(binding))
        if (
            self.closed
            or choice is None
            or not choice.enabled
            or not choice.selector_ids
            or choice.adapter is None
            or self.pending(binding)
        ):
            return None
        lease = MemoryLease(self, choice, cancel)
        self.leases.add(lease)
        return lease

    def replace(self, name, adapter):
        if name not in self.adapters and len(self.adapters) >= 4:
            raise ValueError("Too many Converse memory adapters")
        old_adapter = self.adapters.get(name)
        retiring = (
            old_adapter is not None
            and old_adapter is not adapter
            and not any(value is old_adapter for key, value in self.adapters.items() if key != name)
        )
        if (
            retiring
            and id(old_adapter) not in self.retired_adapters
            and len(self.retired_adapters) >= 32
        ):
            raise RuntimeError("Retired memory adapter capacity is busy; restart Converse")
        if retiring:
            self.retired_adapters[id(old_adapter)] = old_adapter
        self.adapters[name] = adapter
        for key, old in list(self.choices.items()):
            if old.implementation_id == name:
                self.generation = min(MAX_GENERATION, self.generation + 1)
                self.choices[key] = MemoryChoice(
                    old.binding, self.generation, False, name, old.selector_ids, adapter
                )
                self._revoke(old)

    def _revoke(self, choice):
        for lease in tuple(self.leases):
            if lease.choice is choice:
                lease.cancel()
                for task in tuple(lease.reads):
                    task.cancel()
        self.on_revoke(choice)

    def shutdown(self):
        if not self.closed:
            self.closed = True
            for choice in tuple(self.choices.values()):
                self._revoke(choice)

    async def prepare(self, binding, provider, compile_context, cancel, provider_current, attach):
        ordinary = compile_context(None)
        if not provider.info.local:
            return ordinary, None
        lease = self.acquire(binding, cancel)
        if lease is None:
            return ordinary, None
        attach(lease)
        try:
            lease.provider = provider
            lease.provider_stamp = _provider_stamp(provider)
            snapshot = await lease.run(
                lease.choice.adapter.recall(
                    binding.model_copy(deep=True),
                    lease.choice.selector_ids,
                    lease.choice.generation,
                )
            )
            if snapshot is None or not lease.current() or not provider_current():
                return ordinary, None
            pin = snapshot.pin
            from services.converse.memory_adapter import RecallSnapshot
            from services.converse.memory_history import validate_pin

            pin = validate_pin(pin)
            if pin is None:
                return ordinary, None
            snapshot = RecallSnapshot(snapshot.binding, pin, snapshot.message)
            if (
                snapshot.binding != binding
                or pin.implementation_id != lease.choice.implementation_id
                or pin.use_generation != lease.choice.generation
                or tuple(pin.selector_ids) != lease.choice.selector_ids
            ):
                return ordinary, None
            projection = compile_context(pin)
            messages = [dict(row) for row in projection.messages]
            messages.insert(1, dict(snapshot.message))
            measure = getattr(provider, "measure_prompt", None)
            if not callable(measure):
                return ordinary, None
            count = await lease.run(measure([dict(row) for row in messages]))
            if not isinstance(count, PromptTokenCount) or not count.fits:
                return ordinary, None
            if not lease.current() or not provider_current():
                return ordinary, None
            if not await lease.run(lease.choice.adapter.validate(snapshot)):
                return ordinary, None
            if not lease.current() or not provider_current():
                return ordinary, None
            from services.converse.context_projection import ContextProjection

            lease.snapshot = snapshot
            self.notices[scope_key(binding)] = "ready"
            return ContextProjection(messages, ["local_only"], consumed_memory=True), lease
        except ProviderCleanupError:
            lease.failed = True
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            return ordinary, None
        finally:
            if not provider_current():
                cancel()
            if lease.snapshot is None:
                self.notices[scope_key(binding)] = "context_omitted"
                lease.finish()
