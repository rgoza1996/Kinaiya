"""Native video owner tests: synthetic bytes and MockTransport, no live Meta.

Positive result tests require the companion agent_media contract to be integrated
before execution. The native SAM parser remains an explicit injected fixture.
"""

import asyncio
import hashlib
import json
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest

from packages.providers.meta_video import MetaVideoError, MetaVideoProvider, request_body

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"PUBLIC SYNTHETIC VIDEO CONTRACT BYTES"
MODEL = "muse-spark-1.3-contributor"


def prepared(model="sam-3.1"):
    return SimpleNamespace(
        source_bytes=VIDEO,
        grant=SimpleNamespace(
            source_bytes=len(VIDEO), source_sha256=hashlib.sha256(VIDEO).hexdigest()
        ),
        target=SimpleNamespace(
            model=model,
            reasoning="xhigh" if model == MODEL else None,
            prompt="Native source-bound prompt.",
        ),
        request=SimpleNamespace(prompt="UNTRUSTED HARNESS PROMPT MUST NOT BE EXPORTED"),
    )


def payload(model):
    return {
        "status": "completed",
        "model": model,
        "usage": {"input_tokens": 2, "output_tokens": 3},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Public synthetic video observation."},
                ],
            }
        ],
    }


class HeldStream(httpx.AsyncByteStream):
    def __init__(self, model, *, failure=False, raw=None):
        self.raw = json.dumps(payload(model)).encode() if raw is None else raw
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.calls, self.closed, self.failure = 0, False, failure

    async def __aiter__(self):
        yield self.raw

    async def aclose(self):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.failure:
            raise RuntimeError("SYNTHETIC_PRIVATE_CREDENTIAL_IN_CLOSE_ERROR")
        self.closed = True


async def projection(_prepared, _payload, text, current):
    current()
    return {"text": text, "data": {"synthetic_projection": True}}


@pytest.mark.parametrize("model", ["sam-3.1", MODEL])
def test_native_request_uses_entire_source_and_only_trusted_prompt(model):
    import base64

    body = request_body(prepared(model))
    assert body["stream"] is False and body["store"] is False
    text, video = body["input"][0]["content"]
    assert text["text"] == "Native source-bound prompt."
    assert "UNTRUSTED" not in json.dumps(body)
    assert base64.b64decode(video["video_url"].split(",", 1)[1], validate=True) == VIDEO
    assert video["type"] == "input_video"
    if model == MODEL:
        assert body["reasoning"] == {"effort": "xhigh"}
        assert body["max_output_tokens"] == 8192 and body["tools"] == []
    else:
        assert set(body) == {"model", "input", "stream", "store"}


@pytest.mark.parametrize("change", ["bytes", "digest", "model", "reasoning"])
def test_changed_source_or_target_is_refused_before_transport(change):
    value = prepared(MODEL)
    if change == "bytes":
        value.source_bytes += b"UNAUTHORIZED EXTRA"
    elif change == "digest":
        value.grant.source_sha256 = "f" * 64
    elif change == "model":
        value.target.model = "wonder"
    else:
        value.target.reasoning = "low"
    with pytest.raises(MetaVideoError):
        request_body(value)


@pytest.mark.asyncio
async def test_concurrent_sam_and_spark_keep_separate_original_stream_owners():
    streams = {model: HeldStream(model) for model in ("sam-3.1", MODEL)}
    requests, projected = [], []

    def handle(request):
        body = json.loads(request.content)
        requests.append((request, body))
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=streams[body["model"]]
        )

    async def project(*args):
        projected.append(args[0].target.model)
        return await projection(*args)

    provider = MetaVideoProvider(
        api_key="SYNTHETIC_TEST_CREDENTIAL",
        sam_projector=project,
        transport=httpx.MockTransport(handle),
    )
    tasks = [asyncio.create_task(provider(prepared(model), lambda: None)) for model in streams]
    try:
        for stream in streams.values():
            await asyncio.wait_for(stream.started.wait(), 1)
        assert len(provider._owners) == 2 and len(requests) == 2
        assert not projected and not any(task.done() for task in tasks)
        for request, body in requests:
            assert str(request.url) == "http://127.0.0.1:9790/v1/responses"
            assert body["input"][0]["content"][0]["text"] == "Native source-bound prompt."
        streams[MODEL].release.set()
        spark = await tasks[1]
        assert spark.model == MODEL and streams[MODEL].closed
        assert len(provider._owners) == 1 and not projected and not tasks[0].done()
        streams["sam-3.1"].release.set()
        sam = await tasks[0]
        assert sam.model == "sam-3.1" and projected == ["sam-3.1"]
        assert all(stream.calls == 1 and stream.closed for stream in streams.values())
        assert not provider._owners
        await provider.close()
    finally:
        for stream in streams.values():
            stream.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await provider.http.aclose()


@pytest.mark.asyncio
async def test_repeated_call_cancellation_joins_original_close_then_removes_retired_owner():
    stream = HeldStream("sam-3.1")
    provider = MetaVideoProvider(
        api_key="SYNTHETIC_TEST_CREDENTIAL",
        sam_projector=projection,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
    )
    call = asyncio.create_task(provider(prepared(), lambda: None))
    try:
        await asyncio.wait_for(stream.started.wait(), 1)
        call.cancel()
        await asyncio.sleep(0)
        call.cancel()
        await asyncio.sleep(0)
        assert not call.done() and stream.calls == 1 and not stream.closed
        stream.release.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert stream.closed and stream.calls == 1
        assert not provider._owners
        await provider.close()
    finally:
        stream.release.set()
        with suppress(asyncio.CancelledError):
            await call
        await provider.http.aclose()


@pytest.mark.asyncio
async def test_provider_close_retains_one_original_client_close_through_repeated_cancellation():
    provider = MetaVideoProvider(
        api_key="SYNTHETIC_TEST_CREDENTIAL",
        sam_projector=projection,
        transport=httpx.MockTransport(lambda _: pytest.fail("Close must never dispatch")),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.http.aclose
    calls = 0

    async def held_close():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original()

    provider.http.aclose = held_close
    closing = asyncio.create_task(provider.close())
    joining = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert calls == 1 and provider._client_close is not None
        assert not provider._client_close.done() and not provider._client_close.cancelled()
        joining = asyncio.create_task(provider.close())
        await asyncio.sleep(0)
        assert not joining.done() and calls == 1
        release.set()
        await asyncio.wait_for(joining, 1)
        await provider.close()
        assert calls == 1
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        if joining is not None:
            await asyncio.gather(joining, return_exceptions=True)
        await original()


@pytest.mark.asyncio
async def test_failed_original_close_cannot_export_result_or_private_error_or_disappear():
    stream = HeldStream("sam-3.1", failure=True)
    stream.release.set()
    provider = MetaVideoProvider(
        api_key="SYNTHETIC_TEST_CREDENTIAL",
        sam_projector=projection,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
    )
    try:
        with pytest.raises(MetaVideoError) as failure:
            await provider(prepared(), lambda: None)
        assert "SYNTHETIC_PRIVATE_CREDENTIAL" not in str(failure.value)
        assert provider._owners and stream.calls == 1 and not stream.closed
        with pytest.raises(MetaVideoError):
            await provider.close()
    finally:
        await provider.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["depth", "nodes", "duplicate", "nonfinite", "root", "model", "refusal"]
)
async def test_untrusted_response_is_bounded_and_refused_before_native_projection(case):
    value = payload("sam-3.1")
    prefix = json.dumps(value)[:-1].encode() + b',"untrusted":'
    if case == "depth":
        raw = prefix + b"[" * 33 + b"0" + b"]" * 33 + b"}"
    elif case == "nodes":
        raw = prefix + b"[" + b"0," * 65536 + b"0]}"
    elif case == "duplicate":
        raw = (
            json.dumps(value)
            .replace('"completed"', '"incomplete", "status": "completed"', 1)
            .encode()
        )
    elif case == "nonfinite":
        raw = prefix + b'NaN,"SYNTHETIC_PRIVATE_CREDENTIAL":true}'
    elif case == "root":
        raw = b"[]"
    else:
        if case == "model":
            value["model"] = "wonder"
        else:
            value["output"][0]["content"] = [
                {"type": "refusal", "refusal": "SYNTHETIC_PRIVATE_CREDENTIAL"}
            ]
        raw = json.dumps(value).encode()
    stream = HeldStream("sam-3.1", raw=raw)
    stream.release.set()
    projected = []

    async def project(*args):
        projected.append(True)
        return await projection(*args)

    provider = MetaVideoProvider(
        api_key="SYNTHETIC_TEST_CREDENTIAL",
        sam_projector=project,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "application/json"}, stream=stream
            )
        ),
    )
    try:
        with pytest.raises(MetaVideoError) as failure:
            await provider(prepared(), lambda: None)
        assert "SYNTHETIC_PRIVATE_CREDENTIAL" not in str(failure.value)
        assert not projected and not provider._owners
        assert stream.closed and stream.calls == 1
    finally:
        await provider.close()
