import httpx

from packages.contracts.context import (
    MAX_CONTEXT_RESPONSE_BYTES,
    ContextHistoryPage,
    ContextHistoryRequest,
    ContextQualificationPage,
    ContextQualificationRequest,
    context_json,
)
from packages.contracts.models import JournalEntry, PlaybackGeneration, Session


class JournalClient:
    def __init__(self, url: str, token: str, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            transport=transport,
        )

    async def append(self, request):
        response = await self.http.post(
            "/v1/transcript/entries", json=request.model_dump(mode="json")
        )
        response.raise_for_status()
        return JournalEntry.model_validate(response.json())

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
        response = await self.http.post("/v1/sessions", json=request.model_dump())
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
