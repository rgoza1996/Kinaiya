"""Native, request-local admission for one complete-generation request.

Adapters bind the callback to the exact counted request and transport. This
object neither measures tokens nor grants permission. Constructing it acquires
no stream or remote resource; discarding a selection requires no async cleanup.
"""

from collections.abc import Awaitable, Callable

from packages.providers.prompt_budget import PromptCountUnavailable, PromptTokenCount


class PreparedComplete:
    __slots__ = ("_measurement", "_generate", "_fixture")

    def __init__(
        self,
        measurement: PromptTokenCount | None,
        generate: Callable[[], Awaitable[str]],
        *,
        fixture: bool = False,
    ):
        if (
            type(fixture) is not bool
            or not callable(generate)
            or (fixture and measurement is not None)
            or (not fixture and type(measurement) is not PromptTokenCount)
        ):
            raise PromptCountUnavailable("Complete prompt admission is invalid")
        self._measurement, self._generate, self._fixture = measurement, generate, fixture

    @property
    def measurement(self) -> PromptTokenCount | None:
        return self._measurement

    @property
    def fixture(self) -> bool:
        """Explicit non-inference fixture; never a substitute tokenizer count."""
        return self._fixture

    def discard(self) -> None:
        self._generate = None

    async def complete(self) -> str:
        generate, self._generate = self._generate, None
        if generate is None or (self._measurement is not None and not self._measurement.fits):
            raise PromptCountUnavailable("Complete prompt admission is consumed or overflowing")
        # Consumption precedes the first await, including cancellation/failure.
        return await generate()
