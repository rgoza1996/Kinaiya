"""Private Decisions inspection routes, distinct from service transcript reads."""

from fastapi import Depends, HTTPException, Request

from packages.contracts.context import context_json
from packages.contracts.decisions import MAX_CAPTURE_BYTES, DecisionsCallFinish, DecisionsCallStart


def install_decisions(app, store, auth):
    async def body(request, model):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "Decisions capture requires JSON")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_CAPTURE_BYTES:
                raise HTTPException(413, "Decisions capture exceeds its byte bound")
            raw.extend(chunk)
        try:
            return model.model_validate(context_json(raw, maximum=MAX_CAPTURE_BYTES))
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Invalid Decisions capture") from exc

    def query(request, names, counters):
        params = request.query_params
        if any(key not in names or len(params.getlist(key)) != 1 for key in params):
            raise HTTPException(422, "Ambiguous inspection query")
        result = dict(params)
        for name in counters:
            if name in result:
                raw = result[name]
                if (
                    not raw.isascii()
                    or not raw.isdigit()
                    or (len(raw) > 1 and raw.startswith("0"))
                    or len(raw) > 16
                ):
                    raise HTTPException(422, "Inspection counters require decimal integers")
                result[name] = int(raw)
        for name in ("session_id", "outcome"):
            if name in result and not 1 <= len(result[name]) <= 128:
                raise HTTPException(422, "Invalid inspection identifier")
        return result

    @app.get("/v1/decisions/inspections", dependencies=[Depends(auth.require("owner"))])
    async def inspections(request: Request):
        values = query(
            request,
            {
                "session_id",
                "source",
                "outcome",
                "search",
                "from_date",
                "to_date",
                "timezone",
                "through_sequence",
                "before_sequence",
                "limit",
            },
            {"through_sequence", "before_sequence", "limit"},
        )
        try:
            return store.decisions.page(**values)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, "Invalid inspection query") from exc

    @app.get(
        "/v1/decisions/inspections/{source_event_id}", dependencies=[Depends(auth.require("owner"))]
    )
    async def inspection(source_event_id: str, request: Request):
        values = query(
            request,
            {"through_call_sequence", "before_call_sequence", "limit"},
            {"through_call_sequence", "before_call_sequence", "limit"},
        )
        try:
            return store.decisions.detail(source_event_id, **values)
        except (ValueError, OverflowError) as exc:
            raise HTTPException(422, "Invalid inspection continuation") from exc

    @app.post("/v1/decisions/calls/start", dependencies=[Depends(auth.require("decisions"))])
    async def start(request: Request):
        capture = await body(request, DecisionsCallStart)
        try:
            return store.decisions.start(capture)
        except ValueError as exc:
            raise HTTPException(409, "Decisions capture binding conflict") from exc

    @app.post(
        "/v1/decisions/calls/{call_id}/finish", dependencies=[Depends(auth.require("decisions"))]
    )
    async def finish(call_id: str, request: Request):
        capture = await body(request, DecisionsCallFinish)
        try:
            return store.decisions.finish(call_id, capture)
        except ValueError as exc:
            raise HTTPException(409, "Decisions capture completion conflict") from exc
