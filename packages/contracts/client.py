import asyncio

import httpx

from packages.contracts.agent_export import (
    MAX_AGENT_EXPORT_BYTES,
    AgentExportAdmission,
    AgentExportRequest,
    agent_export_json,
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
from packages.contracts.models import JournalEntry, PlaybackGeneration, Session

AGENT_EXPORT_TIMEOUT_S = 15


class JournalClient:
    def __init__(self, url: str, token: str, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            transport=transport,
        )

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
        await self.http.aclose()
