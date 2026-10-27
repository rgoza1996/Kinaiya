import httpx

from packages.contracts.models import JournalEntry, Session


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

    async def session(self, session_id):
        response = await self.http.get(f"/v1/sessions/{session_id}")
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

    async def cursor(self, consumer):
        response = await self.http.get(f"/v1/cursors/{consumer}")
        response.raise_for_status()
        return response.json()["sequence"]

    async def acknowledge(self, consumer, sequence):
        response = await self.http.post(f"/v1/cursors/{consumer}", json={"sequence": sequence})
        response.raise_for_status()

    async def close(self):
        await self.http.aclose()
