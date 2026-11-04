"""Exact-source buffered video operations through the retained Meta budget guard.

Native owner grants supply prompts, targets and immutable MP4 bytes. Generated
harness text cannot select destinations, credentials, prompts or broader media.
SAM projection is a separately injected trusted parser/artifact writer.
"""

import asyncio
import base64
import hashlib
import json
import math

import httpx

from packages.providers.complete_http import RequestBoundAsyncClient, complete_stream

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class MetaVideoError(Exception):
    pass


class _OwnedStream(httpx.AsyncByteStream):
    def __init__(self, original):
        self.original = original
        self.retirement = None

    async def __aiter__(self):
        async for part in self.original:
            yield part

    async def aclose(self):
        if self.retirement is None:
            self.retirement = asyncio.create_task(self.original.aclose())
        await asyncio.shield(self.retirement)


class _StreamOwner:
    def __init__(self):
        self.streams = []

    def own(self, stream):
        wrapped = _OwnedStream(stream)
        self.streams.append(wrapped)
        return wrapped

    @property
    def retired(self):
        return all(
            stream.retirement is not None
            and stream.retirement.done()
            and not stream.retirement.cancelled()
            and stream.retirement.exception() is None
            for stream in self.streams
        )

    async def settle(self):
        cancelled = False
        for stream in self.streams:
            while True:
                try:
                    await stream.aclose()
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if stream.retirement is not None and stream.retirement.cancelled():
                        raise MetaVideoError(
                            "Original video transport cleanup was cancelled"
                        ) from None
        if cancelled:
            raise asyncio.CancelledError


def request_body(prepared):
    target = prepared.target
    if target.model not in {"sam-3.1", "muse-spark-1.3-contributor", "muse-wonder-1.0"}:
        raise MetaVideoError("Video model is unavailable")
    source = prepared.source_bytes
    if (
        type(source) is not bytes
        or not 16 <= len(source) <= 16 * 1024 * 1024
        or len(source) != prepared.grant.source_bytes
        or hashlib.sha256(source).hexdigest() != prepared.grant.source_sha256
        or source[4:8] != b"ftyp"
    ):
        raise MetaVideoError("Video source identity is unavailable")
    # Only the native grant's prompt is exported. Never use request.prompt.
    prompt = target.prompt
    if type(prompt) is not str or not 1 <= len(prompt.encode("utf-8")) <= 16000:
        raise MetaVideoError("Native video prompt is unavailable")
    body = {
        "model": target.model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_video",
                        "video_url": "data:video/mp4;base64,"
                        + base64.b64encode(source).decode("ascii"),
                    },
                ],
            }
        ],
        "stream": False,
        "store": False,
    }
    if target.model == "muse-spark-1.3-contributor":
        if target.reasoning != "xhigh":
            raise MetaVideoError("The selected video reasoning target is unavailable")
        body.update(reasoning={"effort": "xhigh"}, max_output_tokens=8192, tools=[])
    elif target.model == "muse-wonder-1.0":
        if target.reasoning != "max":
            raise MetaVideoError("The selected Wonder Standard video target is unavailable")
        body.update(reasoning={"effort": "max"}, max_output_tokens=8192, tools=[])
    return body


def output_text(payload):
    if type(payload) is not dict or payload.get("status") != "completed":
        raise MetaVideoError("Video inference did not complete")
    output = payload.get("output")
    if type(output) is not list or len(output) > 64:
        raise MetaVideoError("Video output structure is invalid")
    texts = []
    for item in output:
        if type(item) is not dict:
            raise MetaVideoError("Video output structure is invalid")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise MetaVideoError("Unsupported video output item")
        parts = item.get("content")
        if type(parts) is not list or len(parts) > 64:
            raise MetaVideoError("Video output structure is invalid")
        for part in parts:
            if (
                type(part) is not dict
                or part.get("type") != "output_text"
                or type(part.get("text")) is not str
            ):
                raise MetaVideoError("Video output contains a refusal or unsupported content")
            texts.append(part["text"])
    return "\n".join(texts)


def _json(raw):
    depth, nodes, quoted, escaped = 0, 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            nodes += 1
        elif byte in (93, 125):
            depth -= 1
        elif byte in (44, 58):
            nodes += 1
        if depth > 32 or depth < 0 or nodes > 65536:
            raise ValueError("Provider JSON structure exceeds its bound")
    if quoted or depth:
        raise ValueError("Incomplete provider JSON")

    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError("Duplicate provider key")
            value[key] = child
        return value

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Nonfinite provider JSON")
        return number

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_float=finite_float,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite provider JSON")),
    )


class MetaVideoProvider:
    def __init__(self, *, api_key, sam_projector, transport=None):
        if (
            type(api_key) is not str
            or not 1 <= len(api_key) <= 16384
            or any(c in api_key for c in "\0\r\n")
        ):
            raise ValueError("Meta video credential is unavailable")
        if not callable(sam_projector):
            raise ValueError("A native SAM projector is required")
        self.http = RequestBoundAsyncClient(
            base_url="http://127.0.0.1:9790/v1/",
            headers={"Authorization": "Bearer " + api_key, "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(310, connect=5),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self.sam_projector = sam_projector
        self._owners = set()
        self._closed = False
        self._client_close = None

    async def __call__(self, prepared, current):
        from packages.contracts.agent_media import MediaProviderResult

        if self._closed:
            raise MetaVideoError("Video provider is closed")
        body = request_body(prepared)
        request = self.http.build_request("POST", "responses", json=body)
        request_sha = hashlib.sha256(request.content).hexdigest()
        current()
        raw = bytearray()
        owner = _StreamOwner()
        self._owners.add(owner)
        try:
            if prepared.target.model == "muse-wonder-1.0":
                # The optional Standard gateway requires one exact-model count
                # receipt for this same complete body before any generation.
                # Retain both original streams in this request's owner; the
                # count must reach EOF and close before the generation opens.
                count_raw = bytearray()
                async with asyncio.timeout(15):
                    async with complete_stream(
                        self.http, "responses/input_tokens", body, current, owner.own
                    ) as counted:
                        if (
                            counted.status_code != 200
                            or counted.headers.get("content-encoding", "identity") != "identity"
                            or counted.headers.get("content-type", "").split(";")[0]
                            != "application/json"
                        ):
                            raise MetaVideoError("Wonder video input measurement is unavailable")
                        async for chunk in counted.aiter_raw():
                            current()
                            if len(count_raw) + len(chunk) > 4096:
                                raise MetaVideoError(
                                    "Wonder video input measurement exceeds its bound"
                                )
                            count_raw.extend(chunk)
                count = _json(count_raw)
                if (
                    set(count) != {"object", "input_tokens"}
                    or count["object"] != "response.input_tokens"
                    or type(count["input_tokens"]) is not int
                    or not 1 <= count["input_tokens"] <= 32768
                ):
                    raise MetaVideoError("Wonder video input exceeds its measured budget")
                current()
            async with complete_stream(
                self.http, "responses", body, current, owner.own
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity") != "identity"
                    or response.headers.get("content-type", "").split(";")[0] != "application/json"
                ):
                    raise MetaVideoError("Budgeted video inference is unavailable")
                async for chunk in response.aiter_raw():
                    current()
                    limit = MAX_RESPONSE_BYTES if prepared.target.model == "sam-3.1" else 512 * 1024
                    if len(raw) + len(chunk) > limit:
                        raise MetaVideoError("Video response exceeds its bound")
                    raw.extend(chunk)
            payload = _json(raw)
            current()
            if payload.get("model") != prepared.target.model:
                raise MetaVideoError("Video response model differs from the granted model")
            text = output_text(payload)
            if prepared.target.model == "sam-3.1":
                # Parser/artifact work is native and source-bound, never model
                # executable code. It must validate masks before marking them.
                projection = await self.sam_projector(prepared, payload, text, current)
                current()
                text, data = projection["text"], projection["data"]
            else:
                if not text or len(text) > 16000:
                    raise MetaVideoError("Video transcription is empty or exceeds its bound")
                data = {
                    "modality": "complete_video_with_embedded_audio",
                    "usage": payload.get("usage", {}),
                }
            return MediaProviderResult(
                model=prepared.target.model,
                provider_request_sha256=request_sha,
                provider_response_sha256=hashlib.sha256(raw).hexdigest(),
                text=text,
                data=data,
            )
        except (asyncio.CancelledError, MetaVideoError):
            raise
        except Exception:
            raise MetaVideoError("Video inference or projection failed") from None
        finally:
            try:
                await owner.settle()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise MetaVideoError("Original video transport cleanup is unconfirmed") from None
            finally:
                if owner.retired:
                    self._owners.discard(owner)

    async def close(self):
        self._closed = True
        if self._owners:
            raise MetaVideoError("Original video operations have not retired")
        if self._client_close is None:
            self._client_close = asyncio.create_task(self.http.aclose())
        await asyncio.shield(self._client_close)
