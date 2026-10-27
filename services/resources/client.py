"""Fixed loopback control links. Never follow redirects or ambient proxies."""

import asyncio
from urllib.parse import urlsplit

import httpx


def local_base(url):
    value = urlsplit(url)
    if (
        value.scheme != "http"
        or value.hostname not in {"127.0.0.1", "localhost", "::1"}
        or value.username
        or value.password
        or value.query
        or value.fragment
        or value.path not in {"", "/"}
    ):
        raise ValueError("Resource control endpoints must be plain loopback HTTP origins")
    return url.rstrip("/")


class ResourceClient:
    def __init__(self, url, token, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=local_base(url),
            headers={"Authorization": "Bearer " + token},
            timeout=45,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def status(self):
        response = await self.http.get("/v1/resources", timeout=3)
        response.raise_for_status()
        return response.json()

    async def action(self, payload):
        response = await self.http.post("/v1/resources/actions", json=payload)
        response.raise_for_status()
        return response.json()

    async def task_finished(self, task_id):
        payload = {
            "operation": "task_finished",
            "task_id": task_id,
            "idempotency_key": "finished:" + task_id,
        }
        for attempt in range(3):
            try:
                result = await self.action(payload)
                break
            except httpx.TransportError:
                if attempt == 2:
                    raise
                # Same durable key reconciles a lost response; an uncertain
                # recorded intent is never retried under a new identity.
                await asyncio.sleep(0.1 * (attempt + 1))
        if result.get("ok") is not True:
            raise RuntimeError("Resource restoration remains unconfirmed")
        return result

    async def close(self):
        await self.http.aclose()


class ConverseControl:
    def __init__(self, url, token, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=local_base(url),
            headers={"Authorization": "Bearer " + token},
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def health(self):
        response = await self.http.get("/health")
        response.raise_for_status()
        return response.json()

    async def enabled(self, enabled):
        response = await self.http.post("/v1/converse/enabled", json={"enabled": enabled})
        response.raise_for_status()
        result = response.json()
        if result.get("enabled") is not enabled:
            raise RuntimeError("Converse did not acknowledge its requested state")
        return result

    async def close(self):
        await self.http.aclose()
