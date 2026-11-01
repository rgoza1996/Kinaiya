"""Native, template-inclusive context selection; no inferred token counts."""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import replace

from packages.contracts.context import (
    ContextHistoryPage,
    ContextHistoryRequest,
    ContextQualificationPage,
    ContextQualificationRequest,
)
from packages.contracts.context_usage import ContextSelection
from packages.providers.prompt_budget import PromptCountUnavailable, PromptTokenCount
from packages.providers.text import ProviderCleanupError, ProviderError
from services.converse.delivery_context import window_context_projection


class ContextBudgetUnavailable(ProviderError):
    def __init__(self):
        super().__init__("Exact context measurement is unavailable for the selected model")


class ContextBudgetOverflow(ProviderError):
    def __init__(self):
        super().__init__("Instructions and current input exceed the model context window")


class ContextBudgetStale(ProviderError):
    def __init__(self):
        super().__init__("The conversation or model changed during context preparation")


class ContextHistoryUnavailable(ProviderError):
    def __init__(self):
        super().__init__("Conversation history retrieval is incomplete")


class ContextBudgetBuilder:
    """One frozen native snapshot, reusable for ordinary and selected-recall fits.

    Page/count/byte guards bound work and refuse explicitly; they never become a
    smaller implicit dialogue window. Native qualifiers are fetched completely
    for each base page, including stops preceding a late assistant chunk.
    """

    page_size = 100
    timeout_s = 10.0
    max_measurements = 256
    max_native_bytes = 16 * 1024 * 1024

    def __init__(
        self,
        journal,
        source,
        provider,
        *,
        personality: str,
        runtime_id: str,
        current: Callable[[], bool],
        native=None,
    ):
        self.journal, self.source, self.provider = journal, source, provider
        self.personality, self.runtime_id = personality, runtime_id
        self.current = current
        self.native = native or self._journal_call
        self.deadline = time.monotonic() + self.timeout_s
        self.measurements = 0
        self.native_bytes = 0
        self.through_sequence = None
        self.before_sequence = None
        self.exhausted = False
        self.pages = []
        self.qualifiers = {}
        self.seen = {}
        self.binding = None

    async def _journal_call(self, method, **kwargs):
        return await getattr(self.journal, method)(**kwargs)

    def _check(self):
        if not self.current():
            raise ContextBudgetStale()
        if time.monotonic() >= self.deadline:
            raise ContextHistoryUnavailable()

    async def _await(self, operation):
        try:
            self._check()
        except BaseException:
            close = getattr(operation, "close", None)
            if close is not None:
                close()
            raise
        try:
            async with asyncio.timeout(max(0, self.deadline - time.monotonic())):
                result = await operation
                self._check()
                session = await self.native("session", session_id=self.source.session_id)
                self._check()
                source = await self.native("resolve", event_id=self.source.event_id)
                self._check()
        except (asyncio.CancelledError, ProviderCleanupError):
            raise
        except (ContextBudgetStale, ContextHistoryUnavailable):
            raise
        except TimeoutError as exc:
            raise ContextHistoryUnavailable() from exc
        except ProviderError:
            raise
        except Exception as exc:
            # A measured snapshot is all-or-refuse. Do not let live journal
            # recovery replace a successfully acquired memory lease mid-fit.
            raise ContextHistoryUnavailable() from exc
        if session.channel != "converse" or (
            session.session_id,
            session.thread_id,
            session.epoch,
        ) != (self.source.session_id, self.source.thread_id, self.source.epoch):
            raise ContextBudgetStale()
        if source != self.source:
            raise ContextBudgetStale()
        return result

    def _account(self, entries):
        for entry in entries:
            encoded = entry.model_dump_json().encode("utf-8")
            previous = self.seen.get(entry.event_id)
            if previous is not None:
                if previous != encoded:
                    raise ContextHistoryUnavailable()
                continue
            self.seen[entry.event_id] = encoded
            self.native_bytes += len(encoded)
            if self.native_bytes > self.max_native_bytes:
                raise ContextHistoryUnavailable()

    def _scope(self, page):
        if (
            page.source_event_id != self.source.event_id
            or (page.session_id, page.thread_id, page.epoch)
            != (self.source.session_id, self.source.thread_id, self.source.epoch)
            or (
                self.through_sequence is not None and page.through_sequence != self.through_sequence
            )
        ):
            raise ContextHistoryUnavailable()

    async def _page(self):
        request = ContextHistoryRequest(
            source_event_id=self.source.event_id,
            through_sequence=self.through_sequence,
            before_sequence=self.before_sequence,
            limit=self.page_size,
        )
        page = ContextHistoryPage.model_validate(
            await self._await(
                self.native(
                    "context_history",
                    source_event_id=self.source.event_id,
                    through_sequence=self.through_sequence,
                    before_sequence=self.before_sequence,
                    limit=self.page_size,
                )
            )
        )
        page.validate_request(request)
        self._scope(page)
        self.through_sequence = page.through_sequence
        self._account(page.entries)
        if not page.exhausted and (
            page.next_before_sequence is None
            or (
                self.before_sequence is not None
                and page.next_before_sequence >= self.before_sequence
            )
        ):
            raise ContextHistoryUnavailable()
        references = [entry.event_id for entry in page.entries]
        qualifications = []
        before = None
        while references:
            qualification_request = ContextQualificationRequest(
                source_event_id=self.source.event_id,
                through_sequence=self.through_sequence,
                base_event_ids=references,
                before_sequence=before,
                limit=self.page_size,
            )
            qualified = ContextQualificationPage.model_validate(
                await self._await(
                    self.native(
                        "context_qualifications",
                        source_event_id=self.source.event_id,
                        through_sequence=self.through_sequence,
                        base_event_ids=references,
                        before_sequence=before,
                        limit=self.page_size,
                    )
                )
            )
            qualified.validate_request(qualification_request)
            self._scope(qualified)
            if qualified.base_event_ids != references:
                raise ContextHistoryUnavailable()
            self._account(qualified.entries)
            qualifications.extend(qualified.entries)
            if qualified.exhausted:
                break
            cursor = qualified.next_before_sequence
            if cursor is None or (before is not None and cursor >= before):
                raise ContextHistoryUnavailable()
            before = cursor
        self.pages.append(page.entries)
        for entry in qualifications:
            self.qualifiers[entry.event_id] = entry
        self.before_sequence = page.next_before_sequence
        self.exhausted = page.exhausted
        return page.entries

    def _compile(self, bases, pin, recall):
        projection = window_context_projection(
            [*bases, *self.qualifiers.values()],
            self.source,
            self.provider.info,
            memory_context=pin,
            runtime_id=self.runtime_id,
            budgeted_snapshot=True,
        )
        messages = [{"role": "system", "content": self.personality}]
        if recall is not None:
            messages.append(dict(recall))
        if projection.delivery is not None:
            messages.append({"role": "system", "content": projection.delivery})
        messages.extend(projection.messages)
        messages.append({"role": "user", "content": self.source.payload["text"]})
        return replace(projection, messages=messages, history_messages=len(projection.messages))

    def _selection(self, projection, pin, recall):
        # Counts describe only eligible messages in the native pages examined.
        # A suffix fit can stop paging early; unseen older history is unknown.
        try:
            examined = self._compile(
                [entry for page in self.pages for entry in page], pin, recall
            ).history_messages
            kept = projection.history_messages
            selection = ContextSelection(
                unit="message",
                kept=kept,
                dropped=examined - kept,
                examined=examined,
                history_complete=self.exhausted,
                measurement_attempts=self.measurements,
            )
        except (TypeError, ValueError):
            # This additional pure projection supplies observation metadata only.
            # It cannot replace or refuse the already selected measured prompt.
            selection = None
        return replace(
            projection, history_selection=selection, history_watermark=self.through_sequence
        )

    async def _measure(self, projection):
        self._check()
        measure = getattr(self.provider, "measure_prompt", None)
        if not callable(measure) or self.measurements >= self.max_measurements:
            raise ContextBudgetUnavailable()
        self.measurements += 1
        messages = [dict(message) for message in projection.messages]
        stamp = json.dumps(messages, sort_keys=True, ensure_ascii=False, allow_nan=False)
        try:
            count = await self._await(measure(messages))
        except (PromptCountUnavailable, ProviderError) as exc:
            if isinstance(
                exc, (ProviderCleanupError, ContextBudgetStale, ContextHistoryUnavailable)
            ):
                raise
            raise ContextBudgetUnavailable() from exc
        if (
            not isinstance(count, PromptTokenCount)
            or json.dumps(messages, sort_keys=True, ensure_ascii=False, allow_nan=False) != stamp
        ):
            raise ContextBudgetUnavailable()
        binding = (count.binding_sha256, count.context_tokens, count.reserved_output_tokens)
        if self.binding is not None and self.binding != binding:
            raise ContextBudgetStale()
        self.binding = binding
        return replace(projection, measurement=count)

    async def build(self, pin=None, recall=None):
        """Keep a chronological suffix, dropping only whole oldest messages."""
        protected = await self._measure(self._compile([], pin, recall))
        if not protected.measurement.fits:
            raise ContextBudgetOverflow()
        selected = []
        index = 0
        while True:
            if index < len(self.pages):
                page = self.pages[index]
            elif self.exhausted:
                break
            else:
                page = await self._page()
            index += 1
            selected.extend(page)
            selected.sort(key=lambda entry: entry.sequence)
            projection = await self._measure(self._compile(selected, pin, recall))
            overflow = not projection.measurement.fits
            while not projection.measurement.fits and selected:
                selected.pop(0)
                projection = await self._measure(self._compile(selected, pin, recall))
            if overflow:
                return self._selection(projection, pin, recall)
            if self.exhausted and index == len(self.pages):
                return self._selection(projection, pin, recall)
        return self._selection(
            await self._measure(self._compile(selected, pin, recall)), pin, recall
        )

    async def finalize(self, projection):
        """Refresh the selected request after memory fallback and before dispatch."""
        measured = await self._measure(projection)
        prior, count = projection.measurement, measured.measurement
        if prior is None or (
            prior.request_sha256,
            prior.binding_sha256,
            prior.context_tokens,
            prior.reserved_output_tokens,
            prior.input_tokens,
        ) != (
            count.request_sha256,
            count.binding_sha256,
            count.context_tokens,
            count.reserved_output_tokens,
            count.input_tokens,
        ):
            raise ContextBudgetStale()
        if not count.fits:
            raise ContextBudgetOverflow()
        selection = projection.history_selection
        return replace(
            measured,
            history_selection=None
            if selection is None
            else selection.model_copy(update={"measurement_attempts": self.measurements}),
        )
