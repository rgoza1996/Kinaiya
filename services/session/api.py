import asyncio
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from packages.contracts.agent_control import (
    AgentModeChange,
    AgentSteerAdmissionRequest,
)
from packages.contracts.agent_export import (
    MAX_AGENT_EXPORT_BYTES,
    AgentExportAdmission,
    AgentExportRequest,
    agent_export_json,
)
from packages.contracts.agent_submit import (
    MAX_AGENT_SUBMIT_BYTES,
    MAX_AGENT_SUBMIT_LOOKUP_BYTES,
    MAX_AGENT_SUBMIT_RESPONSE_BYTES,
    AgentSubmitConflict,
    AgentSubmitLookupRequest,
    AgentSubmitUnavailable,
    agent_submit_json,
    submit_request,
)
from packages.contracts.agent_workspace import AgentSessionPageRequest
from packages.contracts.context import (
    MAX_CONTEXT_REQUEST_BYTES,
    ContextHistoryPage,
    ContextQualificationPage,
    ContextQualificationRequest,
    context_json,
)
from packages.contracts.interfaces import (
    AgentControlConflict,
    AgentControlUnavailable,
    AgentModeOff,
)
from packages.contracts.models import AppendRequest, PlaybackGeneration, SessionRequest
from services.common.auth import Auth
from services.session.agent_control import AgentControl
from services.session.store import JournalError, Store


def create_app(
    store: Store,
    auth: Auth,
    *,
    companion_clock=None,
    converse_memory: bool = False,
    agent_enabled: bool | None = None,
) -> FastAPI:
    if auth is None or not hasattr(auth, "require"):
        raise RuntimeError("Session control requires configured authentication")
    if agent_enabled is not None and type(agent_enabled) is not bool:
        raise ValueError("Legacy Agent bootstrap must be an explicit boolean")
    # This app owns its registration; another app using the same Store never
    # upgrades this object's generation or acquires its write authority.
    control = AgentControl(store, new_store=store.agent_control.new_store)
    try:
        control.register(legacy_agent_enabled=agent_enabled)
        control_ready = True
    except AgentControlUnavailable:
        control_ready = False

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            control.close()

    app = FastAPI(title="Kinaiya shared session journal", version="1.0", lifespan=lifespan)
    app.state.agent_control = control
    authenticated = Depends(auth.require())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(auth.allowed_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def control_no_store(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(
            ("/v1/agent-control", "/v1/transcript/agent-steers", "/v1/transcript/agent-submits")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AgentControlConflict)
    async def control_conflict(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=409,
            content={"detail": str(AgentControlConflict())},
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(AgentControlUnavailable)
    async def control_unavailable(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"detail": str(AgentControlUnavailable())},
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(AgentModeOff)
    async def agent_off(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=403,
            content={"detail": str(AgentModeOff())},
            headers={"Cache-Control": "no-store"},
        )

    async def control_body(request, cls):
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Agent control requires identity JSON")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > cls.wire_bytes:
                raise HTTPException(422, "Invalid Agent control request")
            raw.extend(chunk)
        try:
            return cls.from_json(bytes(raw))
        except ValueError:
            raise HTTPException(422, "Invalid Agent control request") from None

    def control_response(result):
        from fastapi.responses import Response

        return Response(
            result.canonical_bytes(),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    def registered_control():
        if not control_ready:
            raise AgentControlUnavailable
        return control

    @app.get("/v1/agent-control/state", dependencies=[Depends(auth.require("owner", "agent"))])
    async def agent_control_state():
        return control_response(registered_control().snapshot())

    @app.post("/v1/agent-control/changes", dependencies=[Depends(auth.require("owner"))])
    async def agent_mode_change(request: Request):
        body = await control_body(request, AgentModeChange)
        return control_response(registered_control().change(body))

    @app.post("/v1/agent-control/changes/lookup", dependencies=[Depends(auth.require("owner"))])
    async def agent_mode_lookup(request: Request):
        body = await control_body(request, AgentModeChange)
        return control_response(registered_control().lookup(body))

    @app.post("/v1/transcript/agent-steers", dependencies=[Depends(auth.require("agent"))])
    async def agent_steer_admission(request: Request):
        body = await control_body(request, AgentSteerAdmissionRequest)
        return control_response(store.agent_control.admit_steer(body))

    @app.post("/v1/transcript/agent-steers/lookup", dependencies=[Depends(auth.require("agent"))])
    async def agent_steer_lookup(request: Request):
        body = await control_body(request, AgentSteerAdmissionRequest)
        return control_response(store.agent_control.lookup_steer(body))

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

    @app.get("/v1/sessions/page", dependencies=[Depends(auth.require("owner"))])
    async def sessions_page(query: Annotated[AgentSessionPageRequest, Depends()]):
        try:
            return store.sessions_page(**query.model_dump(exclude={"schema_version"}))
        except ValueError as exc:
            raise HTTPException(422, "Invalid Agent session page") from exc

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

    @app.post(
        "/v1/transcript/agent-export-admission",
        response_model=AgentExportAdmission,
        dependencies=[Depends(auth.require("agent"))],
    )
    async def agent_export_admission(request: Request):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "Agent export admission requires JSON")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_AGENT_EXPORT_BYTES:
                raise HTTPException(413, "Agent export admission exceeds its byte bound")
            raw.extend(chunk)
        try:
            body = AgentExportRequest.model_validate(agent_export_json(bytes(raw)))
        except (ValueError, RecursionError):
            raise HTTPException(422, "Invalid Agent export admission request") from None
        return store.agent_export_admission(body)

    @app.post("/v1/transcript/agent-submits")
    async def agent_submits(request: Request, role: str = Depends(auth.require("agent"))):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "Agent submit admission requires JSON")
        encoding = request.headers.get("content-encoding", "")
        if encoding and encoding.strip().lower() not in ("", "identity"):
            raise HTTPException(415, "Agent submit admission encoding is not supported")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_AGENT_SUBMIT_BYTES:
                raise HTTPException(422, "Invalid Agent submit request") from None
            raw.extend(chunk)
        try:
            data = agent_submit_json(bytes(raw), maximum=MAX_AGENT_SUBMIT_BYTES)
            proposal = submit_request(data)
        except (ValueError, RecursionError):
            raise HTTPException(422, "Invalid Agent submit request") from None
        try:
            receipt = store.admit_submit(proposal, role)
        except AgentSubmitConflict:
            raise HTTPException(
                409, "Agent submission key conflicts with retained scope or content"
            ) from None
        except AgentSubmitUnavailable:
            raise HTTPException(503, "Agent submission admission is unavailable") from None
        except AgentModeOff:
            raise
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Agent submission admission is unavailable") from None
        try:
            if len(receipt.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_RESPONSE_BYTES:
                raise ValueError
        except (ValueError, RecursionError):
            raise HTTPException(503, "Agent submission admission is unavailable") from None
        return receipt

    @app.post("/v1/transcript/agent-submits/lookup")
    async def agent_submit_lookup(request: Request, role: str = Depends(auth.require("agent"))):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "Agent submit lookup requires JSON")
        encoding = request.headers.get("content-encoding", "")
        if encoding and encoding.strip().lower() not in ("", "identity"):
            raise HTTPException(415, "Agent submit lookup encoding is not supported")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_AGENT_SUBMIT_LOOKUP_BYTES:
                raise HTTPException(422, "Invalid Agent submit lookup request") from None
            raw.extend(chunk)
        try:
            data = agent_submit_json(bytes(raw), maximum=MAX_AGENT_SUBMIT_LOOKUP_BYTES)
            body = AgentSubmitLookupRequest.model_validate(data)
        except (ValueError, RecursionError):
            raise HTTPException(422, "Invalid Agent submit lookup request") from None
        try:
            result = store.lookup_submit(body, role)
        except AgentSubmitConflict:
            raise HTTPException(
                409, "Agent submission key conflicts with retained scope or content"
            ) from None
        except AgentSubmitUnavailable:
            raise HTTPException(503, "Agent submission admission is unavailable") from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Agent submission admission is unavailable") from None
        try:
            if len(result.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_RESPONSE_BYTES:
                raise ValueError
        except (ValueError, RecursionError):
            raise HTTPException(503, "Agent submission admission is unavailable") from None
        return result

    @app.get("/v1/transcript", dependencies=[Depends(auth.require())])
    async def read(
        request: Request,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        session_id: str | None = None,
        thread_id: str | None = None,
        kinds: str | None = None,
        task_id: str | None = None,
        search: str | None = None,
        epoch: int | None = Query(default=None, ge=1, le=9_007_199_254_740_991),
        utterance_id: str | None = Query(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$"),
        window_index: int | None = Query(default=None, ge=0, le=1_000_000),
    ):
        for key in ("epoch", "utterance_id", "window_index"):
            if len(request.query_params.getlist(key)) > 1:
                raise HTTPException(422, "Ambiguous exact window query")
        for key in ("epoch", "window_index"):
            if key in request.query_params:
                raw = request.query_params[key]
                if not raw.isascii() or not raw.isdigit() or (len(raw) > 1 and raw.startswith("0")):
                    raise HTTPException(422, "Invalid exact window counter")
        return store.read(
            after=after,
            limit=limit,
            session_id=session_id,
            thread_id=thread_id,
            kinds=kinds.split(",") if kinds else None,
            task_id=task_id,
            search=search,
            epoch=epoch,
            utterance_id=utterance_id,
            window_index=window_index,
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
        "/v1/transcript/context-history",
        response_model=ContextHistoryPage,
        dependencies=[Depends(auth.require("owner", "converse"))],
    )
    async def context_history(
        request: Request,
        source_event_id: str = Query(min_length=1, max_length=128),
        through_sequence: int | None = Query(default=None, ge=1),
        before_sequence: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        allowed = {"source_event_id", "through_sequence", "before_sequence", "limit"}
        if any(
            key not in allowed or len(request.query_params.getlist(key)) != 1
            for key in request.query_params
        ):
            raise HTTPException(422, "Ambiguous native context history query")
        for key in allowed - {"source_event_id"}:
            raw = request.query_params.get(key)
            if raw is not None and (not raw.isascii() or not raw.isdigit() or raw.startswith("0")):
                raise HTTPException(
                    422, "Native context counters require positive decimal integers"
                )
        return store.context_history(
            source_event_id=source_event_id,
            through_sequence=through_sequence,
            before_sequence=before_sequence,
            limit=limit,
        )

    @app.post(
        "/v1/transcript/context-qualifications",
        response_model=ContextQualificationPage,
        dependencies=[Depends(auth.require("owner", "converse"))],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": ContextQualificationRequest.model_json_schema()}
                },
            }
        },
    )
    async def context_qualifications(request: Request):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "Native context qualifications require JSON")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_CONTEXT_REQUEST_BYTES:
                raise HTTPException(413, "Native context request exceeds its byte limit")
            raw.extend(chunk)
        try:
            data = context_json(raw, maximum=MAX_CONTEXT_REQUEST_BYTES)
            body = ContextQualificationRequest.model_validate(data)
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Invalid native context qualification JSON") from exc
        return store.context_qualifications(**body.model_dump(exclude={"schema_version"}))

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

    from services.session.companion_api import install_companion

    install_companion(app, store, auth, clock=companion_clock)
    from services.session.decisions_api import install_decisions

    install_decisions(app, store, auth)
    if converse_memory:
        from services.session.memory_api import install_memory

        install_memory(app, store, auth)
    return app
