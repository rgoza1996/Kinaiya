import asyncio
import copy

import httpx

from packages.contracts.agent_export import (
    MAX_AGENT_EXPORT_BYTES,
    AgentExportAdmission,
    AgentExportRequest,
    agent_export_json,
)
from packages.contracts.agent_submit import (
    AGENT_SUBMIT_ADMISSION_TIMEOUT_S,
    AGENT_SUBMIT_LOOKUP_TIMEOUT_S,
    MAX_AGENT_SUBMIT_BYTES,
    MAX_AGENT_SUBMIT_LOOKUP_BYTES,
    MAX_AGENT_SUBMIT_RESPONSE_BYTES,
    AgentSubmitConflict,
    AgentSubmitLookupRequest,
    AgentSubmitLookupResult,
    AgentSubmitReceipt,
    AgentSubmitUnavailable,
    agent_submit_json,
    submit_request,
)
from packages.contracts.agent_workspace import AgentSessionPage, AgentSessionPageRequest
from packages.contracts.context import (
    MAX_CONTEXT_RESPONSE_BYTES,
    ContextHistoryPage,
    ContextHistoryRequest,
    ContextQualificationPage,
    ContextQualificationRequest,
    context_json,
)
from packages.contracts.models import AppendRequest, JournalEntry, PlaybackGeneration, Session

AGENT_EXPORT_TIMEOUT_S = 15
_SUBMIT_CLOSE_DRAIN_S = 1.0


class _SubmitExchange:
    """Keep late acquisition and actual response cleanup owned after public refusal."""

    def __init__(self, owner, request, proposal, result_type, deadline):
        self.owner = owner
        self.response = None
        self.close_task = None
        self.close_started = False
        self.abandoned = False
        self.work_cancel_requested = False
        self.close_cancel_requested = False
        owner._submit_exchanges.add(self)
        self.task = asyncio.create_task(self._run(request, proposal, result_type, deadline))
        self.task.add_done_callback(self._settled)

    def _settled(self, task):
        if not task.cancelled():
            task.exception()
        if (
            self.close_task is not None
            and self.close_task.done()
            and (self.close_task.cancelled() or self.close_task.exception() is not None)
        ):
            # Preserve actual close uncertainty before the bounded exchange
            # entry disappears. Ordinary HTTP/admission failures do not set it.
            self.owner._submit_cleanup_failed = True
        if self.task.done() and (self.close_task is None or self.close_task.done()):
            self.owner._submit_exchanges.discard(self)

    def abandon(self):
        self.abandoned = True
        if not self.task.done() and not self.work_cancel_requested:
            self.work_cancel_requested = True
            self.task.cancel()
        # Never cancel a close before its coroutine has actually acquired the
        # response. A close scheduled for a late arrival must still run once.
        if self.close_started and not self.close_task.done() and not self.close_cancel_requested:
            self.close_cancel_requested = True
            self.close_task.cancel()

    async def _close(self):
        self.close_started = True
        await self.response.aclose()

    async def _run(self, request, proposal, result_type, deadline):
        try:
            if self.abandoned or asyncio.get_running_loop().time() >= deadline:
                raise AgentSubmitUnavailable()
            self.response = await self.owner.http.send(request, stream=True, follow_redirects=False)
            if self.abandoned:
                raise AgentSubmitUnavailable()
            response = self.response
            if response.status_code == 409:
                raise AgentSubmitConflict()
            if response.status_code != 200:
                raise AgentSubmitUnavailable()
            if (
                response.headers.get("content-encoding", "identity").strip(" \t").lower()
                != "identity"
                or response.headers.get("content-type", "").split(";", 1)[0].strip(" \t").lower()
                != "application/json"
            ):
                raise AgentSubmitUnavailable()
            declared = response.headers.get("content-length")
            length = None
            if declared is not None:
                declared = declared.strip(" \t")
                if (
                    "transfer-encoding" in response.headers
                    or not declared.isascii()
                    or not declared.isdigit()
                    or len(declared) > 8
                    or int(declared) > MAX_AGENT_SUBMIT_RESPONSE_BYTES
                ):
                    raise AgentSubmitUnavailable()
                length = int(declared)
            raw = bytearray()
            # HTTPX aiter_raw/aiter_bytes auto-close at EOF. Reading the raw
            # identity stream keeps the sole close in our independently owned
            # task, whose completion (not response.is_closed) proves cleanup.
            async for chunk in response.stream:
                if (
                    not isinstance(chunk, bytes)
                    or len(raw) + len(chunk) > MAX_AGENT_SUBMIT_RESPONSE_BYTES
                ):
                    raise AgentSubmitUnavailable()
                raw.extend(chunk)
            if length is not None and length != len(raw):
                raise AgentSubmitUnavailable()
            if self.abandoned or asyncio.get_running_loop().time() >= deadline:
                raise AgentSubmitUnavailable()
            parsed = agent_submit_json(bytes(raw), maximum=MAX_AGENT_SUBMIT_RESPONSE_BYTES)
            return result_type.model_validate(parsed).validate_request(proposal)
        finally:
            if self.response is not None:
                self.close_task = asyncio.create_task(self._close())
                self.close_task.add_done_callback(self._settled)
                await asyncio.shield(self.close_task)


class JournalClient:
    def __init__(self, url: str, token: str, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            transport=transport,
        )
        self._submit_exchanges = set()
        self._submit_stt_closes = set()
        self._submit_cleanup_failed = False
        self._submit_closed = False

    async def admit_submit(self, request: AppendRequest) -> AgentSubmitReceipt:
        deadline = asyncio.get_running_loop().time() + AGENT_SUBMIT_ADMISSION_TIMEOUT_S
        try:
            proposal = submit_request(request)
        except Exception:
            raise AgentSubmitUnavailable() from None
        return await self._submit(
            "/v1/transcript/agent-submits",
            proposal,
            AgentSubmitReceipt,
            MAX_AGENT_SUBMIT_BYTES,
            deadline,
        )

    async def lookup_submit(self, request: AgentSubmitLookupRequest) -> AgentSubmitLookupResult:
        deadline = asyncio.get_running_loop().time() + AGENT_SUBMIT_LOOKUP_TIMEOUT_S
        try:
            if isinstance(request, AgentSubmitLookupRequest):
                request = request.model_dump(mode="python")
            proposal = AgentSubmitLookupRequest.model_validate(copy.deepcopy(request), strict=True)
        except Exception:
            raise AgentSubmitUnavailable() from None
        return await self._submit(
            "/v1/transcript/agent-submits/lookup",
            proposal,
            AgentSubmitLookupResult,
            MAX_AGENT_SUBMIT_LOOKUP_BYTES,
            deadline,
        )

    async def _submit(self, path, proposal, result_type, maximum, deadline):
        exchange = None
        try:
            loop = asyncio.get_running_loop()
            wire = proposal.model_dump_json().encode("utf-8")
            if self._submit_closed or len(wire) > maximum or loop.time() >= deadline:
                raise AgentSubmitUnavailable()
            request = self.http.build_request(
                "POST",
                path,
                content=wire,
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            )
            exchange = _SubmitExchange(self, request, proposal, result_type, deadline)
            finished, _pending = await asyncio.wait(
                {exchange.task}, timeout=max(0, deadline - loop.time())
            )
            if not finished or exchange.abandoned or loop.time() >= deadline:
                raise AgentSubmitUnavailable()
            return exchange.task.result()
        except asyncio.CancelledError:
            if exchange is not None:
                exchange.abandon()
            raise
        except AgentSubmitConflict:
            raise AgentSubmitConflict() from None
        except Exception:
            if exchange is not None:
                exchange.abandon()
            raise AgentSubmitUnavailable() from None

    async def agent_export_admission(self, request):
        request = AgentExportRequest.model_validate(request)
        async with (
            asyncio.timeout(AGENT_EXPORT_TIMEOUT_S),
            self.http.stream(
                "POST",
                "/v1/transcript/agent-export-admission",
                content=request.model_dump_json().encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            ) as response,
        ):
            response.raise_for_status()
            if (
                response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}
                or response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise ValueError("Invalid Agent export admission response")
            declared = response.headers.get("content-length")
            if declared is not None and (
                not declared.isascii()
                or not declared.isdigit()
                or len(declared) > 8
                or int(declared) > MAX_AGENT_EXPORT_BYTES
            ):
                raise ValueError("Invalid Agent export admission response")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > MAX_AGENT_EXPORT_BYTES:
                    raise ValueError("Agent export admission exceeds its byte bound")
                raw.extend(chunk)
            if declared is not None and len(raw) != int(declared):
                raise ValueError("Invalid Agent export admission response")
        return AgentExportAdmission.model_validate(agent_export_json(bytes(raw))).validate_request(
            request
        )

    async def append(self, request):
        response = await self.http.post(
            "/v1/transcript/entries", json=request.model_dump(mode="json")
        )
        response.raise_for_status()
        return JournalEntry.model_validate(response.json())

    async def lookup_stt_publication(self, original):
        """Narrow bounded metadata exchange; an absent row is never settlement."""
        from packages.contracts.stt_settlement import STTPublication, validate_publication_proof
        from packages.providers.whistle_ipc import decode_json

        original = STTPublication.model_validate(original)
        response = None
        try:
            async with asyncio.timeout(5):
                response = await self.http.send(
                    self.http.build_request(
                        "POST", "/v1/transcript/stt-publications/lookup", json=original.model_dump()
                    ),
                    stream=True,
                )
                response.raise_for_status()
                raw = bytearray()
                async for part in response.aiter_bytes():
                    if len(raw) + len(part) > 4096:
                        raise ValueError("STT publication proof exceeds its bound")
                    raw.extend(part)
                return validate_publication_proof(decode_json(bytes(raw), 4096), original)
        finally:
            if response is not None:
                closing = asyncio.create_task(response.aclose())
                self._submit_stt_closes.add(closing)

                def settled(task):
                    self._submit_stt_closes.discard(task)
                    if task.cancelled() or task.exception() is not None:
                        self._submit_cleanup_failed = True

                closing.add_done_callback(settled)
                await asyncio.shield(closing)

    async def decisions_call_start(self, request):
        response = await self.http.post(
            "/v1/decisions/calls/start", json=request.model_dump(mode="json")
        )
        response.raise_for_status()
        return response.json()

    async def decisions_call_finish(self, call_id, request):
        response = await self.http.post(
            f"/v1/decisions/calls/{call_id}/finish", json=request.model_dump(mode="json")
        )
        response.raise_for_status()
        return response.json()

    async def read(self, **kwargs):
        params = {k: v for k, v in kwargs.items() if v is not None}
        if "kinds" in params:
            params["kinds"] = ",".join(params["kinds"])
        response = await self.http.get("/v1/transcript", params=params)
        response.raise_for_status()
        return [JournalEntry.model_validate(value) for value in response.json()]

    async def recent(self, **kwargs):
        response = await self.http.get("/v1/transcript/recent", params=kwargs)
        response.raise_for_status()
        return [JournalEntry.model_validate(value) for value in response.json()]

    async def context_history(
        self, *, source_event_id, through_sequence=None, before_sequence=None, limit=100
    ) -> ContextHistoryPage:
        request = ContextHistoryRequest(
            source_event_id=source_event_id,
            through_sequence=through_sequence,
            before_sequence=before_sequence,
            limit=limit,
        )
        response = await self.http.get(
            "/v1/transcript/context-history",
            params=request.model_dump(exclude_none=True, exclude={"schema_version"}),
        )
        response.raise_for_status()
        # JSON-mode strict validation accepts native ISO timestamps while rejecting
        # coerced identities, booleans and counters, including nested entries.
        context_json(response.content, maximum=MAX_CONTEXT_RESPONSE_BYTES)
        page = ContextHistoryPage.model_validate_json(response.content, strict=True)
        return page.validate_request(request)

    async def context_qualifications(
        self, *, source_event_id, through_sequence, base_event_ids, before_sequence=None, limit=100
    ) -> ContextQualificationPage:
        request = ContextQualificationRequest(
            source_event_id=source_event_id,
            through_sequence=through_sequence,
            base_event_ids=base_event_ids,
            before_sequence=before_sequence,
            limit=limit,
        )
        response = await self.http.post(
            "/v1/transcript/context-qualifications", json=request.model_dump(mode="json")
        )
        response.raise_for_status()
        context_json(response.content, maximum=MAX_CONTEXT_RESPONSE_BYTES)
        page = ContextQualificationPage.model_validate_json(response.content, strict=True)
        return page.validate_request(request)

    async def session(self, session_id):
        response = await self.http.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        return Session.model_validate(response.json())

    async def sessions_page(self, **kwargs) -> AgentSessionPage:
        request = AgentSessionPageRequest(**kwargs)
        response = await self.http.get(
            "/v1/sessions/page",
            params=request.model_dump(exclude_none=True, exclude={"schema_version"}),
        )
        response.raise_for_status()
        if len(response.content) > 256_000:
            raise ValueError("Agent session page exceeds response bound")
        return AgentSessionPage.model_validate_json(response.content).validate_request(request)

    async def playback_generation(self, session_id, thread_id, epoch) -> PlaybackGeneration:
        response = await self.http.get(
            "/v1/transcript/playback/generation",
            params={"session_id": session_id, "thread_id": thread_id, "epoch": epoch},
        )
        response.raise_for_status()
        maximum = PlaybackGeneration.model_validate(response.json())
        if (maximum.session_id, maximum.thread_id, maximum.epoch) != (session_id, thread_id, epoch):
            raise ValueError("Native playback generation has a different scope")
        return maximum

    async def sessions(self):
        response = await self.http.get("/v1/sessions")
        response.raise_for_status()
        return [Session.model_validate(s) for s in response.json()]

    async def create_session(self, request):
        response = await self.http.post("/v1/sessions", json=request.model_dump(exclude_none=True))
        response.raise_for_status()
        return Session.model_validate(response.json())

    async def entry(self, event_id):
        response = await self.http.get(f"/v1/transcript/entries/{event_id}")
        response.raise_for_status()
        return JournalEntry.model_validate(response.json())

    async def resolve(self, event_id):
        response = await self.http.get(
            f"/v1/transcript/entries/{event_id}", params={"resolve": True}
        )
        response.raise_for_status()
        return JournalEntry.model_validate(response.json())

    async def latest_window(self, session_id, epoch, utterance_id, window_index=None):
        params = {"session_id": session_id, "epoch": epoch, "utterance_id": utterance_id}
        if window_index is not None:
            params["window_index"] = window_index
        response = await self.http.get("/v1/transcript/windows/latest", params=params)
        response.raise_for_status()
        return JournalEntry.model_validate(response.json()) if response.json() is not None else None

    async def window_decision(self, source_event_id):
        response = await self.http.get(f"/v1/transcript/windows/{source_event_id}/decision")
        response.raise_for_status()
        return JournalEntry.model_validate(response.json()) if response.json() is not None else None

    async def pending_windows(self, *, limit=100):
        response = await self.http.get("/v1/transcript/windows/pending", params={"limit": limit})
        response.raise_for_status()
        return [JournalEntry.model_validate(value) for value in response.json()]

    async def window_classification_attempts(self, source_event_id):
        response = await self.http.get(
            "/v1/transcript/windows/attempts", params={"source_event_id": source_event_id}
        )
        response.raise_for_status()
        attempts = response.json()["attempts"]
        if type(attempts) is not int or not 0 <= attempts <= 2:
            raise ValueError("Invalid native window-attempt count")
        return attempts

    async def window_response(self, session_id, epoch, utterance_id):
        response = await self.http.get(
            "/v1/transcript/windows/response",
            params={"session_id": session_id, "epoch": epoch, "utterance_id": utterance_id},
        )
        response.raise_for_status()
        return JournalEntry.model_validate(response.json()) if response.json() is not None else None

    async def window_dispatches(self, **query):
        response = await self.http.get(
            "/v1/transcript/windows/dispatches",
            params={k: v for k, v in query.items() if v is not None},
        )
        response.raise_for_status()
        return [JournalEntry.model_validate(e) for e in response.json()]

    async def window_control(self, session_id, epoch, agent_interrupt=False):
        response = await self.http.get(
            "/v1/transcript/windows/control",
            params={"session_id": session_id, "epoch": epoch, "agent_interrupt": agent_interrupt},
        )
        response.raise_for_status()
        return JournalEntry.model_validate(response.json()) if response.json() is not None else None

    async def cursor(self, consumer):
        response = await self.http.get(f"/v1/cursors/{consumer}")
        response.raise_for_status()
        return response.json()["sequence"]

    async def acknowledge(self, consumer, sequence):
        response = await self.http.post(f"/v1/cursors/{consumer}", json={"sequence": sequence})
        response.raise_for_status()

    async def close(self):
        self._submit_closed = True
        for exchange in tuple(self._submit_exchanges):
            exchange.abandon()
        try:
            await self.http.aclose()
        finally:
            pending = {
                task
                for exchange in self._submit_exchanges
                for task in (exchange.task, exchange.close_task)
                if task is not None and not task.done()
            }
            if pending:
                await asyncio.wait(pending, timeout=_SUBMIT_CLOSE_DRAIN_S)
