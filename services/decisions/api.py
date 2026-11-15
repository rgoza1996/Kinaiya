import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from packages.contracts.subsystem_readiness import DecisionsReadiness
from packages.providers.decision_capture import CaptureNativeUnavailable


def create_app(worker, auth, window_worker=None, window_dispatcher=None, *, model_settings=None):
    instance_id = secrets.token_hex(16)
    state = {"started": False, "stopped": False, "tasks": ()}

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
        state.update(started=True, tasks=(task, *extras))
        try:
            yield
        finally:
            state["stopped"] = True
            task.cancel()
            for extra in extras:
                extra.cancel()
            try:
                await asyncio.gather(task, *extras, return_exceptions=True)
            finally:
                if model_settings is not None:
                    await model_settings.close()

    app = FastAPI(title="Kinaiya Decisions", lifespan=lifespan)

    def routing_phase():
        # This is routing-runtime readiness, not a model/GPU probe or an Agent
        # availability assertion. Inspection must not acquire an inference lease.
        phase = "starting"
        if state["stopped"]:
            phase = "stopped"
        elif state["started"]:
            workers = (worker, *([window_worker] if window_worker is not None else []))
            active = all(not task.done() for task in state["tasks"]) and all(
                item.running is True and item.ready is True for item in workers
            )
            try:
                if model_settings is not None:
                    stamp = model_settings.cache_stamp()
                    configured = (
                        type(stamp) is str
                        and len(stamp) == 64
                        and all(c in "0123456789abcdef" for c in stamp)
                    )
                else:
                    configured = worker.classifier.provider.info is not None
            except (CaptureNativeUnavailable, OSError, ValueError, RuntimeError):
                configured = False
            phase = "ready" if active and configured else "unavailable"
        return phase

    @app.get("/v1/decisions/readiness", dependencies=[Depends(auth.require("owner", "converse"))])
    async def routing_readiness():
        phase = routing_phase()
        result = DecisionsReadiness(
            contract_version=1,
            service="decisions",
            consumer="converse",
            instance_id=instance_id,
            phase=phase,
            ready=phase == "ready",
        )
        return JSONResponse(result.model_dump(), headers={"Cache-Control": "no-store"})

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
            "routing_runtime_ready": routing_phase() == "ready",
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
