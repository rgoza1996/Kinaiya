import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI


def create_app(worker, auth):
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker.run())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Kinaiya Decisions", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {
            "service": "decisions",
            "running": worker.running,
            "enabled": worker.enabled,
            "last_error": worker.last_error,
            "provider": worker.classifier.provider.info.model_dump(),
        }

    @app.get("/v1/decisions/status", dependencies=[Depends(auth.require("owner"))])
    async def status():
        return {
            "cursor": await worker.journal.cursor(worker.consumer),
            "last_error": worker.last_error,
            "enabled": worker.enabled,
        }

    return app
