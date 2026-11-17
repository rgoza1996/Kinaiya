"""Gated owner v2 microphone API; one original native capture per logical mic."""

import asyncio
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from packages.contracts.live_streams import (
    LiveStreamBegin,
    LiveStreamCancel,
    LiveStreamCancellation,
    LiveStreamCapability,
    LiveStreamFinish,
    LiveStreamIdentity,
    LiveStreamLookup,
    LiveStreamNextUtterance,
    LiveStreamPcm,
    LiveStreamProof,
    LiveStreamScope,
    LiveUtteranceCapability,
)
from packages.contracts.stt_settlement import digest
from packages.contracts.whistle_stream_owner import ConverseStreamLookup
from packages.providers.whistle_ipc import decode_json
from services.converse.dependencies import RequiredLiveDecisions
from services.converse.live_streams import LiveIncrementalStreams

BASE = "/v2/converse/audio/live-stream"


class LiveStreamRegistry:
    active_limit = 32
    history_limit = 64

    def __init__(self, runtime, speech, dependency):
        if dependency is not None and not isinstance(dependency, RequiredLiveDecisions):
            raise ValueError("Live capture requires the explicit native v2 Decisions monitor")
        self.runtime, self.speech, self.dependency = runtime, speech, dependency
        self.instance = secrets.token_hex(16)
        self.captures = {}
        if dependency is not None:
            dependency.bind(self)

    def decisions_dependency_changed(self):
        for capture in self.captures.values():
            capture.fence()

    def available(self):
        models = getattr(self.runtime, "stt_model_settings", None)
        owned_stream_available = getattr(models, "owned_stream_available", None)
        return bool(
            self.dependency is not None
            and self.dependency.ready
            and self.runtime.enabled
            and self.speech is not None
            and not getattr(self.runtime, "_stt_stopping", False)
            and getattr(self.runtime, "_stt_options_configured", False)
            and not self.runtime._stt_options_unavailable
            and models is not None
            and not models.closed
            and callable(owned_stream_available)
            and owned_stream_available()
        )

    def _trim(self):
        terminal = [
            capture
            for capture in self.captures.values()
            if capture.capture_fenced
            and capture.capture_task is not None
            and capture.capture_task.done()
            and not capture.capture_task.cancelled()
            and capture.capture_task.exception() is None
            and capture.mic is not None
            and capture.mic.fenced_state is not None
            and not any(not task.done() for task in capture.tasks())
            and (capture.native is None or capture.native.lease.settled)
        ]
        for capture in terminal[: max(0, len(terminal) - self.history_limit)]:
            self.captures.pop(capture.scope.capture_id)
            self.runtime._incremental_stream_owners.discard(capture)

    def owned(self, body):
        capture = self.captures.get(body.capture_id)
        if body.owner_instance != self.instance or capture is None:
            raise HTTPException(409, "No exact owned live microphone is available")
        if isinstance(body, LiveStreamBegin):
            scope = LiveStreamScope.model_validate(
                body.model_dump(include=set(LiveStreamScope.model_fields))
            )
            if scope != capture.scope:
                raise HTTPException(409, "Live microphone scope changed")
        return capture

    async def begin(self, body, request):
        if body.owner_instance != self.instance:
            raise HTTPException(409, "Live microphone belongs to another Converse owner")
        capture = self.captures.get(body.capture_id)
        if capture is None:
            if not self.available():
                raise HTTPException(503, "Adaptive Live capture is unavailable")
            if body.segment_index != 0:
                raise HTTPException(
                    409, "A logical microphone must start with decoder segment zero"
                )
            self._trim()
            if (
                len(self.captures) >= self.active_limit + self.history_limit
                or sum(
                    not value.capture_fenced or value.busy_count for value in self.captures.values()
                )
                >= self.active_limit
            ):
                raise HTTPException(409, "Original live capture work or recovery is retained")
            scope = LiveStreamScope.model_validate(
                body.model_dump(include=set(LiveStreamScope.model_fields))
            )
            capture = LiveIncrementalStreams(
                self.runtime, self.speech, scope, dependency=self.dependency
            )
            self.captures[body.capture_id] = capture
        else:
            self.owned(body)
        await capture.begin_segment(
            body.segment_index,
            body.native_scope(),
            request,
            original_body=body.model_dump(mode="json"),
        )
        return await self.proof(capture, "begin", digest(body.model_dump(mode="json")))

    async def advance(self, phase, body, request):
        capture = self.owned(body)
        state = capture.streams.get(body.stream_id)
        if state is None or state.live_segment != body.segment_index:
            raise HTTPException(409, "Original live decoder segment changed")
        original = body.model_dump(mode="json")
        native = {
            k: v
            for k, v in original.items()
            if k
            not in {"schema_version", "protocol", "owner_instance", "capture_id", "segment_index"}
        }
        await capture.append_or_finish(phase, native, request, original_body=original)
        return await self.proof(capture, phase, digest(original))

    async def next_utterance(self, body, request):
        # Ordinary v2 begin keeps its exact current utterance scope. Only this
        # explicit, versioned boundary can advance that logical source domain.
        capture = self.owned(
            LiveStreamIdentity.model_validate(
                body.model_dump(include=set(LiveStreamIdentity.model_fields)), strict=True
            )
        )
        await capture.next_utterance(body, request)
        return await self.proof(capture, "begin", digest(body.model_dump(mode="json")))

    async def proof(self, capture, phase, sha, *, reconcile=False):
        native = await capture.lookup(
            phase,
            ConverseStreamLookup(
                schema_version="1.0",
                protocol="converse_stream_owner_v1",
                owner_instance=capture.instance,
                request_sha256=sha,
            ),
            reconcile=reconcile,
        )
        known = capture.originals.get((phase, sha))
        state, original = known if known is not None else (None, None)
        observed = await capture.mic.inspect() if capture.mic is not None else None
        return LiveStreamProof(
            schema_version="2.0",
            protocol="converse_live_stream_v2",
            owner_instance=self.instance,
            capture_id=capture.scope.capture_id,
            phase=phase,
            request_sha256=sha,
            segment_index=None if state is None else state.live_segment,
            segment_sample_offset=None if state is None else state.live_offset,
            capture=observed,
            native=native,
            publication=None if original is None else getattr(original, "live_publication", None),
        )

    async def cancel(self, body):
        capture = self.owned(body)
        confirmed = await capture.close(body.reason)
        state = None
        if capture.mic is not None:
            try:
                state = await capture.mic.inspect()
            except (OSError, ValueError, RuntimeError, TimeoutError):
                pass
        return LiveStreamCancellation(
            schema_version="2.0",
            protocol="converse_live_stream_v2",
            owner_instance=self.instance,
            capture_id=body.capture_id,
            fenced=True,
            capture=state,
            cleanup_confirmed=confirmed,
        )

    async def close(self):
        return all(
            await asyncio.gather(*(capture.close("shutdown") for capture in self.captures.values()))
        )


def install_live_streams(app, runtime, auth, speech, dependency, reserve_api_model):
    registry = LiveStreamRegistry(runtime, speech, dependency)
    authorize = auth.require("owner")
    routes = APIRouter(prefix=BASE, dependencies=[Depends(authorize)])

    def response(value):
        result = JSONResponse(value.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
        if len(result.body) > 448 * 1024:
            raise HTTPException(503, "Original live stream response exceeds its bound")
        return result

    async def parse(request, model, limit=4096):
        if request.query_params or any(
            len(request.headers.getlist(name)) > 1
            for name in ("content-type", "content-length", "content-encoding")
        ):
            raise HTTPException(400, "Invalid live stream request envelope")
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, "Live stream requests require identity JSON")
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > limit:
                        raise HTTPException(413, "Live stream request exceeds its bound")
                    raw.extend(chunk)
            result = model.model_validate(decode_json(bytes(raw), limit))
        except TimeoutError:
            raise HTTPException(408, "Live stream request body deadline expired") from None
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(400, "Invalid original live stream request") from None
        await authorize(request, request.headers.get("authorization", ""))
        return result

    def documented(model):
        return {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": model.model_json_schema()}},
            }
        }

    @routes.get("/ownership")
    async def ownership(request: Request):
        if request.query_params:
            raise HTTPException(400, "Live stream ownership has no query envelope")
        return response(
            LiveStreamCapability(
                schema_version="2.0",
                protocol="converse_live_stream_v2",
                owner_instance=registry.instance,
                available=registry.available(),
            )
        )

    @routes.post("/begin", openapi_extra=documented(LiveStreamBegin))
    async def begin(request: Request):
        body = await parse(request, LiveStreamBegin)
        reserve_api_model(runtime, request)
        return response(await registry.begin(body, request))

    @routes.get("/next-utterance")
    async def utterance_ownership(request: Request):
        if request.query_params:
            raise HTTPException(400, "Live utterance ownership has no query envelope")
        return response(
            LiveUtteranceCapability(
                schema_version="1.0",
                protocol="native_live_utterance_v1",
                owner_instance=registry.instance,
                available=registry.available(),
            )
        )

    @routes.post("/next-utterance", openapi_extra=documented(LiveStreamNextUtterance))
    async def next_utterance(request: Request):
        body = await parse(request, LiveStreamNextUtterance)
        reserve_api_model(runtime, request)
        return response(await registry.next_utterance(body, request))

    @routes.post("/pcm", openapi_extra=documented(LiveStreamPcm))
    async def pcm(request: Request):
        body = await parse(request, LiveStreamPcm, 96000)
        reserve_api_model(runtime, request)
        return response(await registry.advance("pcm", body, request))

    @routes.post("/finish", openapi_extra=documented(LiveStreamFinish))
    async def finish(request: Request):
        body = await parse(request, LiveStreamFinish)
        reserve_api_model(runtime, request)
        return response(await registry.advance("finish", body, request))

    @routes.post("/lookup", openapi_extra=documented(LiveStreamLookup))
    @routes.post("/reconcile", openapi_extra=documented(LiveStreamLookup))
    async def lookup(request: Request):
        body = await parse(request, LiveStreamLookup)
        capture = registry.owned(body)
        return response(
            await registry.proof(
                capture,
                body.phase,
                body.request_sha256,
                reconcile=request.url.path.endswith("/reconcile"),
            )
        )

    @routes.post("/cancel", openapi_extra=documented(LiveStreamCancel))
    async def cancel(request: Request):
        return response(await registry.cancel(await parse(request, LiveStreamCancel)))

    app.include_router(routes)
    return registry
