import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware


def create_app(worker, auth, window_worker=None, window_dispatcher=None, *, model_settings=None):
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
        try:
            yield
        finally:
            task.cancel()
            for extra in extras:
                extra.cancel()
            try:
                await asyncio.gather(task, *extras, return_exceptions=True)
            finally:
                if model_settings is not None:
                    await model_settings.close()

    app = FastAPI(title="Kinaiya Decisions", lifespan=lifespan)
    if model_settings is not None:
        from services.decisions.model_api import router

        app.include_router(router(model_settings, auth))
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(auth.allowed_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.get("/health")
    async def health():
        from packages.providers.decision_capture import CaptureNativeUnavailable

        provider = worker.classifier.provider
        readiness = None
        provider_metadata = None
        if model_settings is None:
            provider_metadata = provider.info.model_dump()
            probe = getattr(provider, "health", None)
            if probe is not None:
                try:
                    readiness = await asyncio.wait_for(probe(), timeout=1)
                except Exception:
                    readiness = {"ready": False}
        else:
            try:
                with model_settings.preparing("health"):
                    async with model_settings.operation() as lease:
                        provider_metadata = lease.provider.info.model_dump()
                        probe = getattr(lease.provider, "health", None)
                        if probe is not None:
                            readiness = await asyncio.wait_for(probe(), timeout=1)
            except (CaptureNativeUnavailable, OSError, ValueError, RuntimeError):
                provider_metadata = None
                readiness = {"ready": False}
        return {
            "service": "decisions",
            "running": worker.running,
            "enabled": worker.enabled,
            "last_error": worker.last_error,
            "provider": provider_metadata,
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
