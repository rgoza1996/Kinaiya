import asyncio

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from packages.contracts.models import AppendRequest, PlaybackGeneration, SessionRequest
from services.common.auth import Auth
from services.session.store import JournalError, Store


def create_app(store: Store, auth: Auth) -> FastAPI:
    app = FastAPI(title="Kinaiya shared session journal", version="1.0")
    authenticated = Depends(auth.require())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(auth.allowed_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(JournalError)
    async def conflict(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": "Unknown journal reference"})

    @app.exception_handler(PermissionError)
    async def denied(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.get("/health")
    async def health():
        return {"service": "session", "schema_version": "1.0", "status": "ready"}

    @app.post("/v1/sessions", dependencies=[Depends(auth.require("owner"))])
    async def create(request: SessionRequest):
        return store.create_session(request)

    @app.get("/v1/sessions", dependencies=[Depends(auth.require("owner", "resources"))])
    async def sessions():
        return store.sessions()

    @app.get("/v1/sessions/{session_id}", dependencies=[Depends(auth.require())])
    async def session(session_id: str):
        return store.session(session_id)

    @app.post("/v1/sessions/{session_id}/reset", dependencies=[Depends(auth.require("owner"))])
    async def reset(session_id: str):
        return store.reset(session_id)

    @app.delete("/v1/sessions/{session_id}", dependencies=[Depends(auth.require("owner"))])
    async def delete(session_id: str):
        store.delete(session_id)
        return {"deleted": True}

    @app.post("/v1/transcript/entries")
    async def append(request: AppendRequest, role=authenticated):
        return store.append(request, role)

    @app.get("/v1/transcript", dependencies=[Depends(auth.require())])
    async def read(
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        session_id: str | None = None,
        thread_id: str | None = None,
        kinds: str | None = None,
        task_id: str | None = None,
        search: str | None = None,
    ):
        return store.read(
            after=after,
            limit=limit,
            session_id=session_id,
            thread_id=thread_id,
            kinds=kinds.split(",") if kinds else None,
            task_id=task_id,
            search=search,
        )

    @app.get("/v1/transcript/entries/{event_id}", dependencies=[Depends(auth.require())])
    async def entry(event_id: str, resolve: bool = False):
        return store.resolve(event_id) if resolve else store.entry(event_id)

    @app.get("/v1/transcript/windows/latest", dependencies=[Depends(auth.require())])
    async def latest_window(
        session_id: str,
        epoch: int = Query(ge=1),
        utterance_id: str = Query(min_length=1, max_length=128),
        window_index: int | None = Query(default=None, ge=0),
    ):
        return store.latest_window(session_id, epoch, utterance_id, window_index)

    @app.get(
        "/v1/transcript/windows/pending",
        dependencies=[Depends(auth.require("owner", "decisions"))],
    )
    async def pending_windows(limit: int = Query(100, ge=1, le=200)):
        return store.pending_windows(limit=limit)

    @app.get(
        "/v1/transcript/windows/attempts",
        dependencies=[Depends(auth.require("owner", "decisions"))],
    )
    async def window_attempts(source_event_id: str = Query(min_length=1, max_length=128)):
        return {"attempts": store.window_classification_attempts(source_event_id)}

    @app.get(
        "/v1/transcript/windows/{source_event_id}/decision", dependencies=[Depends(auth.require())]
    )
    async def window_decision(source_event_id: str):
        return store.window_decision(source_event_id)

    @app.get("/v1/transcript/windows/response", dependencies=[Depends(auth.require())])
    async def window_response(
        session_id: str,
        epoch: int = Query(ge=1),
        utterance_id: str = Query(min_length=1, max_length=128),
    ):
        return store.window_response(session_id, epoch, utterance_id)

    @app.get("/v1/transcript/recent", dependencies=[Depends(auth.require())])
    async def recent(session_id: str, limit: int = Query(100, ge=1, le=200)):
        return store.recent(session_id=session_id, limit=limit)

    @app.get(
        "/v1/transcript/playback/generation",
        response_model=PlaybackGeneration,
        dependencies=[Depends(auth.require("owner", "converse"))],
    )
    async def playback_generation(
        session_id: str = Query(min_length=1, max_length=128),
        thread_id: str = Query(min_length=1, max_length=128),
        epoch: int = Query(ge=1),
    ):
        return store.playback_generation(session_id, thread_id, epoch)

    @app.get("/v1/transcript/windows/dispatches", dependencies=[Depends(auth.require())])
    async def window_dispatches(
        session_id: str | None = None,
        decision_id: str | None = None,
        semantic_key: str | None = None,
        pending_only: bool = False,
        interrupt_only: bool = False,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
    ):
        return store.window_dispatches(
            session_id=session_id,
            decision_id=decision_id,
            semantic_key=semantic_key,
            pending_only=pending_only,
            interrupt_only=interrupt_only,
            after=after,
            limit=limit,
        )

    @app.get("/v1/transcript/windows/control", dependencies=[Depends(auth.require())])
    async def window_control(
        session_id: str, epoch: int = Query(ge=1), agent_interrupt: bool = False
    ):
        return store.window_control(session_id, epoch, agent_interrupt)

    @app.get("/v1/cursors/{consumer}")
    async def cursor(consumer: str, role=authenticated):
        return {"sequence": store.cursor(role, consumer)}

    class CursorAck(BaseModel):
        sequence: int = Field(ge=0)

    @app.post("/v1/cursors/{consumer}")
    async def acknowledge(consumer: str, request: CursorAck, role=authenticated):
        store.acknowledge(role, consumer, request.sequence)
        return {"sequence": request.sequence}

    @app.websocket("/v1/events")
    async def events(ws: WebSocket):
        origin = ws.headers.get("origin")
        if origin and origin not in auth.allowed_origins:
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            # A first frame supports web clients without putting credentials in URLs.
            hello = await asyncio.wait_for(ws.receive_json(), timeout=5)
            auth.authenticate(hello.get("token", ""))
            after = int(hello.get("after", 0))
            session_id = hello.get("session_id")
            while True:
                entries = store.read(after=after, limit=100, session_id=session_id)
                for event in entries:
                    await ws.send_json(event.model_dump(mode="json"))
                    after = event.sequence
                if not entries:
                    await asyncio.sleep(0.1)
        except (WebSocketDisconnect, TimeoutError, HTTPException, ValueError):
            await ws.close(code=1008)

    return app
