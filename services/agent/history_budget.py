"""Chronological whole-exchange selection for measured complete requests."""

import inspect
from asyncio import CancelledError
from dataclasses import dataclass

from packages.providers.completion_budget import PreparedComplete
from packages.providers.prompt_budget import PromptCountUnavailable, PromptTokenCount
from packages.providers.text import ProviderCleanupError

_MAX_EXCHANGES = 512
_MAX_BYTES = 16 * 1024 * 1024


class AgentContextUnavailable(PromptCountUnavailable):
    def __init__(self, *_args, **_kwargs):
        super().__init__("Agent history context is unavailable")


class AgentContextOverflow(PromptCountUnavailable):
    def __init__(self, *_args, **_kwargs):
        super().__init__("Agent history context overflows protected messages")


class AgentContextStale(PromptCountUnavailable):
    def __init__(self, *_args, **_kwargs):
        super().__init__("Agent history sources changed during selection")


@dataclass(frozen=True)
class HistorySelection:
    prepared: PreparedComplete
    messages: list[dict[str, str]]
    kept_exchanges: int
    dropped_exchanges: int
    measurement_attempts: int


def _message_size(message, roles, remaining):
    """Size of native json.dumps(ensure_ascii=False), without a huge encoding."""
    if (
        type(message) is not dict
        or message.keys() != {"role", "content"}
        or type(message["role"]) is not str
        or message["role"] not in roles
        or type(message["content"]) is not str
    ):
        raise AgentContextUnavailable()
    text = message["content"]
    # Every Unicode scalar needs at least one encoded byte. Refuse before
    # scanning/encoding a caller string larger than the entire native budget.
    size = len('{"role": "", "content": ""}') + len(message["role"])
    if len(text) + size > remaining:
        raise AgentContextUnavailable()
    for ch in text:
        point = ord(ch)
        if 0xD800 <= point <= 0xDFFF:
            raise AgentContextUnavailable()
        if ch in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
            size += 2
        elif point < 0x20:
            size += 6
        elif point < 0x80:
            size += 1
        elif point < 0x800:
            size += 2
        elif point < 0x10000:
            size += 3
        else:
            size += 4
        if size > remaining:
            raise AgentContextUnavailable()
    return size


def _validate_and_snapshot(base, exchanges):
    if (
        type(base) is not list
        or type(exchanges) is not list
        or len(base) < 2
        or len(exchanges) > _MAX_EXCHANGES
    ):
        raise AgentContextUnavailable()
    size, count = 2, 0  # The flattened JSON list's brackets and separators.

    def snapshot(messages, roles):
        nonlocal size, count
        result = []
        for message in messages:
            separator = 2 if count else 0
            size += separator + _message_size(message, roles, _MAX_BYTES - size - separator)
            result.append(dict(message))
            count += 1
        return result

    snap_base = snapshot(base, {"system", "user", "assistant"})
    snap_exchanges = []
    for exchange in exchanges:
        if type(exchange) is not list or not exchange:
            raise AgentContextUnavailable()
        snap_exchanges.append(snapshot(exchange, {"user", "assistant"}))
    return snap_base, snap_exchanges


def _unchanged(messages, snapshot):
    return (
        type(messages) is list
        and len(messages) == len(snapshot)
        and all(
            type(current) is dict
            and current.keys() == {"role", "content"}
            and type(current["role"]) is str
            and type(current["content"]) is str
            and current == original
            for current, original in zip(messages, snapshot, strict=True)
        )
    )


async def select_history(base, exchanges, prepare, check, *, max_measurements=256):
    if (
        type(max_measurements) is not int
        or not 1 <= max_measurements <= 513
        or not callable(prepare)
        or not callable(check)
    ):
        raise AgentContextUnavailable()
    snap_base, snap_exchanges = _validate_and_snapshot(base, exchanges)
    total = len(snap_exchanges)

    def inputs_unchanged():
        return (
            _unchanged(base, snap_base)
            and type(exchanges) is list
            and len(exchanges) == total
            and all(
                _unchanged(current, original)
                for current, original in zip(exchanges, snap_exchanges, strict=True)
            )
        )

    async def fence():
        checked = check()
        if not inspect.isawaitable(checked):
            raise AgentContextUnavailable()
        await checked
        if not inputs_unchanged():
            raise AgentContextStale()

    first_binding = None
    for dropped in range(min(total + 1, max_measurements)):
        kept = total - dropped
        detached = [dict(message) for message in snap_base] + [
            dict(message) for exchange in snap_exchanges[dropped:] for message in exchange
        ]
        detached_snapshot = [dict(message) for message in detached]
        prepared, selected = None, False
        try:
            await fence()
            try:
                pending = prepare(detached)
                if not inspect.isawaitable(pending):
                    raise AgentContextUnavailable()
                prepared = await pending
            except (
                CancelledError,
                ProviderCleanupError,
                AgentContextStale,
                AgentContextOverflow,
                AgentContextUnavailable,
            ):
                raise
            except Exception as exc:
                if not inputs_unchanged() or not _unchanged(detached, detached_snapshot):
                    raise AgentContextStale() from None
                if isinstance(exc, PromptCountUnavailable):
                    raise AgentContextUnavailable() from None
                raise
            # Check mutable input immediately after prepare, before another
            # await, and again after the native currentness/policy fence.
            if not inputs_unchanged() or not _unchanged(detached, detached_snapshot):
                raise AgentContextStale()
            admitted_measurement = (
                prepared.measurement if type(prepared) is PreparedComplete else None
            )
            admitted_fixture = prepared.fixture if type(prepared) is PreparedComplete else None
            await fence()
            if not _unchanged(detached, detached_snapshot):
                raise AgentContextStale()
            if type(prepared) is not PreparedComplete:
                raise AgentContextUnavailable()
            if type(prepared.fixture) is not bool:
                raise AgentContextUnavailable()
            if (
                prepared.measurement is not admitted_measurement
                or prepared.fixture != admitted_fixture
            ):
                raise AgentContextStale()
            if prepared.fixture:
                if prepared.measurement is not None:
                    raise AgentContextUnavailable()
                if dropped:
                    raise AgentContextStale()
            else:
                measurement = prepared.measurement
                if type(measurement) is not PromptTokenCount:
                    raise AgentContextUnavailable()
                # The request digest legitimately changes as whole exchanges
                # are removed. Provider/context/output identities must not.
                binding = (
                    measurement.context_tokens,
                    measurement.reserved_output_tokens,
                    measurement.binding_sha256,
                )
                if first_binding is None:
                    first_binding = binding
                elif binding != first_binding:
                    raise AgentContextStale()
                if not measurement.fits:
                    if kept <= 1:
                        raise AgentContextOverflow()
                    continue
            selected = True
            return HistorySelection(
                prepared=prepared,
                messages=[dict(message) for message in detached_snapshot],
                kept_exchanges=kept,
                dropped_exchanges=dropped,
                measurement_attempts=dropped + 1,
            )
        finally:
            # A prepared callback is local-only. Never invoke an untrusted
            # object's duck-typed cleanup method or generate while selecting.
            if not selected and type(prepared) is PreparedComplete:
                prepared.discard()
    raise AgentContextUnavailable()
