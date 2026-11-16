"""An owned, expiring Decisions runtime observation; never an inference lease."""

import asyncio
import json
from time import monotonic
from urllib.parse import urlsplit

import httpx

from packages.contracts.live_readiness import LiveDecisionsReadiness
from packages.contracts.subsystem_readiness import DecisionsReadiness

MAX_READINESS_BYTES = 4096
PROBE_TIMEOUT = 1.0
POLL_INTERVAL = 0.25
MAX_OBSERVATION_AGE = 2.0


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate readiness field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("Invalid readiness constant")


class RequiredDecisions:
    endpoint = "v1/decisions/readiness"
    response_model = DecisionsReadiness

    def __init__(self, base_url, token):
        try:
            url = urlsplit(base_url)
            valid = (
                type(base_url) is str
                and url.scheme in {"http", "https"}
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and (url.port is None or 1 <= url.port <= 65535)
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Configure a valid required Decisions service URL")
        if type(token) is not str or len(token) < 24 or any(c.isspace() for c in token):
            raise ValueError("Required Decisions service credential is unavailable")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
            timeout=PROBE_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        )
        self._runtime = None
        self._observation = None
        self._expires = 0.0
        self._reason = "starting"
        self._task = self._close_task = None
        self._closed = self._fence_failed = False
        self._lock = asyncio.Lock()

    def bind(self, runtime):
        if self._runtime is not None:
            raise RuntimeError("Required Decisions observation already has an owner")
        self._runtime = runtime

    @property
    def ready(self):
        return bool(
            not self._closed
            and not self._fence_failed
            and self._task is not None
            and not self._task.done()
            and self._observation is not None
            and self._observation.ready
            and monotonic() < self._expires
        )

    def snapshot(self):
        reason = self._reason
        if self._fence_failed:
            reason = "cleanup_unavailable"
        elif self._closed:
            reason = "stopped"
        elif self._observation is not None and monotonic() >= self._expires:
            reason = "stale"
        elif self._task is not None and self._task.done():
            reason = "stopped"
        return {"required": True, "ready": self.ready, "state": reason}

    def _replace(self, observation, reason, *, observed_at=None):
        before = self.ready
        old_instance = self._observation.instance_id if self._observation is not None else None
        self._observation = observation
        self._reason = reason
        self._expires = (monotonic() if observed_at is None else observed_at) + MAX_OBSERVATION_AGE
        after = observation is not None and observation.ready
        instance = observation.instance_id if observation is not None else None
        if before != after or (after and old_instance != instance):
            try:
                self._runtime.decisions_dependency_changed()
            except Exception:
                # An uncertain native fence cannot be cleared by the next GET.
                self._fence_failed = True

    async def refresh(self):
        async with self._lock:
            if self._closed:
                return
            observed_at = monotonic()
            try:
                async with asyncio.timeout(PROBE_TIMEOUT):
                    async with self._client.stream("GET", self.endpoint) as response:
                        response.raise_for_status()
                        if (
                            response.headers.get("content-type", "").split(";", 1)[0]
                            != "application/json"
                        ):
                            raise ValueError("Required Decisions response is not JSON")
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise ValueError("Required Decisions response cannot be compressed")
                        body = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=MAX_READINESS_BYTES + 1):
                            if len(body) + len(chunk) > MAX_READINESS_BYTES:
                                raise ValueError("Required Decisions response exceeds its bound")
                            body.extend(chunk)
                        value = json.loads(
                            body, object_pairs_hook=_unique, parse_constant=_constant
                        )
                        observation = self.response_model.model_validate(value)
                self._replace(observation, observation.phase, observed_at=observed_at)
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError, RecursionError):
                self._replace(None, "unavailable")

    async def start(self):
        if self._runtime is None or self._task is not None or self._closed:
            raise RuntimeError("Required Decisions monitor cannot start")
        await self.refresh()
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                await self.refresh()
        finally:
            self._replace(None, "stopped")

    async def _close(self):
        self._replace(None, "stopped")
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._client.aclose()

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)


class RequiredLiveDecisions(RequiredDecisions):
    endpoint = "v2/decisions/live-readiness"
    response_model = LiveDecisionsReadiness
