import httpx

from packages.contracts.models import Task


class AgentClient:
    def __init__(self, url, token, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            transport=transport,
        )

    async def _post(self, path, request):
        response = await self.http.post(path, json=request.model_dump(mode="json"))
        response.raise_for_status()
        return Task.model_validate(response.json())

    async def submit(self, request, *, actor="owner"):
        return await self._post("/v1/agent/tasks", request)

    async def steer(self, task_id, request, *, actor="owner"):
        return await self._post(f"/v1/agent/tasks/{task_id}/steer", request)

    async def interrupt(self, task_id, request, *, actor="owner"):
        return await self._post(f"/v1/agent/tasks/{task_id}/interrupt", request)

    async def status(self, task_id=None):
        path = f"/v1/agent/tasks/{task_id}" if task_id else "/v1/agent/tasks"
        response = await self.http.get(path)
        response.raise_for_status()
        data = response.json()
        return (
            Task.model_validate(data) if task_id else [Task.model_validate(item) for item in data]
        )

    async def close(self):
        await self.http.aclose()
