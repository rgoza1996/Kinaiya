import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI


def create_app(worker, auth, window_worker=None, window_dispatcher=None):
    def window_status():
        return {
            "window_agent_available": window_worker.agent_available if window_worker else None,
            "window_dispatch_error": window_dispatcher.last_error if window_dispatcher else None,
            "window_dispatch_running": window_dispatcher.running if window_dispatcher else False,
        }

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker.run())
        extras = [
            asyncio.create_task(w.run())
            for w in (window_worker, window_dispatcher)
            if w is not None
        ]
        yield
        task.cancel()
        for extra in extras:
            extra.cancel()
        await asyncio.gather(task, *extras, return_exceptions=True)

    app = FastAPI(title="Kinaiya Decisions", lifespan=lifespan)

    @app.get("/health")
    async def health():
        provider = worker.classifier.provider
        probe = getattr(provider, "health", None)
        readiness = None
        if probe is not None:
            try:
                readiness = await asyncio.wait_for(probe(), timeout=1)
            except Exception:
                readiness = {"ready": False}
        return {
            "service": "decisions",
            "running": worker.running,
            "enabled": worker.enabled,
            "last_error": worker.last_error,
            "provider": provider.info.model_dump(),
            "provider_readiness": readiness,
            "streaming_windows": window_worker is not None,
            "window_error": window_worker.last_error if window_worker else None,
            **window_status(),
        }

    @app.get("/v1/decisions/status", dependencies=[Depends(auth.require("owner"))])
    async def status():
        return {
            "cursor": await worker.journal.cursor(worker.consumer),
            "last_error": worker.last_error,
            "enabled": worker.enabled,
            **window_status(),
        }

    return app
