import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI


def create_app(worker, auth, window_worker=None, window_dispatcher=None):
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
        return {
            "service": "decisions",
            "running": worker.running,
            "enabled": worker.enabled,
            "last_error": worker.last_error,
            "provider": worker.classifier.provider.info.model_dump(),
            "streaming_windows": window_worker is not None,
            "window_error": window_worker.last_error if window_worker else None,
        }

    @app.get("/v1/decisions/status", dependencies=[Depends(auth.require("owner"))])
    async def status():
        return {
            "cursor": await worker.journal.cursor(worker.consumer),
            "last_error": worker.last_error,
            "enabled": worker.enabled,
        }

    return app
