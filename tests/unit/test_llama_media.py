"""Unit and controlled-HTTP tests for packages.providers.llama_media."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import struct
import zlib

import httpx
import pytest

from packages.contracts.media import MediaNativeRef, MediaQ, MediaSourcePTS
from packages.providers.llama_media import (
    UPSTREAM_LLAMA_COMMIT,
    LlamaMediaChatResult,
    LlamaMediaPart,
    LlamaMediaRequestSpec,
    LlamaMediaSystemOneResult,
    NativeLlamaMediaProvider,
    build_request,
    parse_response,
)
from packages.providers.text import ProviderCleanupError, ProviderError, ProviderRefusal

CONVERSE_MODEL = "kinaiya-qual-lfm3-omni"
DECISIONS_MODEL = "kinaiya-qual-d1-omni"


def _make_png(width: int = 4, height: int = 4) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    raw_rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    idat = zlib.compress(raw_rows)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_wav(rate: int = 16000, channels: int = 1, samples: int = 160) -> bytes:
    pcm = struct.pack("<" + "h" * (samples * channels), *([1000] * (samples * channels)))
    byte_rate = rate * channels * 2
    block_align = channels * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        rate,
        byte_rate,
        block_align,
        16,
        b"data",
        len(pcm),
    )
    return header + pcm


def _make_mp4() -> bytes:
    ftyp_payload = b"isom\x00\x00\x02\x00isomiso2mp41"
    ftyp = struct.pack(">I4s", 8 + len(ftyp_payload), b"ftyp") + ftyp_payload
    mdat = struct.pack(">I4s", 16, b"mdat") + b"\x00" * 8
    return ftyp + mdat


def _id32(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _ref(kind: str, ref_id: str, seed: str) -> MediaNativeRef:
    return MediaNativeRef(
        backend_id=_id32(f"backend-{kind}"),
        receipt_id=_id32(ref_id),
        sha256=hashlib.sha256(seed.encode()).hexdigest(),
    )


def _source_ref() -> MediaNativeRef:
    return _ref("source", "src-001", "source-001")


def _range_ref() -> MediaNativeRef:
    return _ref("range", "rng-001", "range-001")


def _q(num: int, den: int) -> MediaQ:
    g = math.gcd(num, den)
    return MediaQ(num=num // g, den=den // g)


def _image_part(seq: int = 1, *, kind: str = "image", frame_index: int = 0) -> LlamaMediaPart:
    png = _make_png()
    return LlamaMediaPart(
        kind=kind,
        data=png,
        mime_type="image/png",
        source=_source_ref(),
        track_id="trk-video",
        source_seq=seq,
        through_source_seq=seq,
        pts=MediaSourcePTS(
            pts=frame_index,
            time_base_num=1,
            time_base_den=4,
            sample_start=None,
            sample_count=None,
            frame_index=frame_index,
        ),
        start=_q(frame_index, 4),
        end=_q(frame_index + 1, 4),
        body_sha256=hashlib.sha256(png).hexdigest(),
    )


def _audio_part(seq: int = 2) -> LlamaMediaPart:
    wav = _make_wav()
    return LlamaMediaPart(
        kind="audio",
        data=wav,
        mime_type="audio/wav",
        source=_source_ref(),
        track_id="trk-audio",
        source_seq=seq,
        through_source_seq=seq,
        pts=MediaSourcePTS(
            pts=0,
            time_base_num=1,
            time_base_den=16000,
            sample_start=0,
            sample_count=160,
            frame_index=None,
        ),
        start=MediaQ(num=0, den=1),
        end=MediaQ(num=1, den=100),
        body_sha256=hashlib.sha256(wav).hexdigest(),
    )


def _video_clip_part(seq: int = 3, through_seq: int = 6) -> LlamaMediaPart:
    mp4 = _make_mp4()
    return LlamaMediaPart(
        kind="video_clip",
        data=mp4,
        mime_type="video/mp4",
        source=_source_ref(),
        range=_range_ref(),
        track_id=None,
        source_seq=seq,
        through_source_seq=through_seq,
        pts=None,
        start=MediaQ(num=0, den=1),
        end=MediaQ(num=3, den=1),
        body_sha256=hashlib.sha256(mp4).hexdigest(),
    )


def _systemone_questions() -> dict[str, dict]:
    return {
        "red_square": {
            "type": "noul",
            "instructions": "Is there a red square in the picture?",
        },
        "route": {
            "type": "choice",
            "instructions": "Choose the next conversation action.",
            "criteria": {
                "respond": "Answer the user's question.",
                "wait": "Wait because the question is incomplete.",
                "ignore": "Ignore unrelated background conversation.",
            },
        },
    }


def _chat_response_bytes(text: str = "The square is red and there are 3 beeps.") -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-local-1",
            "model": CONVERSE_MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": text,
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 147,
                "completion_tokens": 14,
                "total_tokens": 161,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _systemone_response_bytes() -> bytes:
    return json.dumps(
        {
            "model": DECISIONS_MODEL,
            "answers": {
                "red_square": {
                    "type": "noul",
                    "noul": 0.9375,
                },
                "route": {
                    "type": "choice",
                    "choice": "respond",
                    "probabilities": {
                        "respond": 0.9,
                        "wait": 0.07,
                        "ignore": 0.03,
                    },
                    "confidence": 0.9,
                },
            },
            "usage": {
                "input_tokens": 523,
                "output_tokens": 0,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_build_request_converse_image_audio_video_and_mixed_parts_preserve_order_and_pts():
    text_part = LlamaMediaPart(kind="text", text="Describe the frame, audio, and clip in order.")
    img_part = _image_part(seq=1, kind="image", frame_index=0)
    frame_part = _image_part(seq=2, kind="video_frame", frame_index=11)
    aud_part = _audio_part(seq=3)
    clip_part = _video_clip_part(seq=4, through_seq=8)

    spec = LlamaMediaRequestSpec(
        mode="converse_chat",
        model=CONVERSE_MODEL,
        parts=(text_part, img_part, frame_part, aud_part, clip_part),
        system_prompt="Answer concisely.",
        temperature=0,
        max_tokens=128,
        reasoning_budget=0,
    )
    prepared = build_request(spec)

    assert prepared.path == "/v1/chat/completions"
    assert prepared.query == "autoload=false"
    assert prepared.upstream_commit == UPSTREAM_LLAMA_COMMIT
    assert prepared.request_sha256 == hashlib.sha256(prepared.raw_body).hexdigest()

    decoded = json.loads(prepared.raw_body)
    assert decoded["model"] == CONVERSE_MODEL
    assert decoded["temperature"] == 0
    assert decoded["max_tokens"] == 128
    assert decoded["stream"] is False
    assert decoded["reasoning_budget"] == 0
    assert len(decoded["messages"]) == 2
    assert decoded["messages"][0] == {"role": "system", "content": "Answer concisely."}

    content = decoded["messages"][1]["content"]
    assert [item["type"] for item in content] == [
        "text",
        "image_url",
        "image_url",
        "input_audio",
        "input_video",
    ]
    assert content[0]["text"] == "Describe the frame, audio, and clip in order."
    assert content[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(
        img_part.data
    ).decode("ascii")
    assert content[3]["input_audio"] == {
        "data": base64.b64encode(aud_part.data).decode("ascii"),
        "format": "wav",
    }
    assert content[4]["input_video"] == {
        "data": base64.b64encode(clip_part.data).decode("ascii"),
    }

    assert [p.kind for p in prepared.provenance] == [
        "text",
        "image",
        "video_frame",
        "audio",
        "video_clip",
    ]
    assert prepared.provenance[1].pts.frame_index == 0
    assert prepared.provenance[2].pts.frame_index == 11
    assert prepared.provenance[3].pts.sample_count == 160
    assert prepared.provenance[4].range == _range_ref()
    assert prepared.provenance[4].source_seq == 4
    assert prepared.provenance[4].through_source_seq == 8


def test_build_request_decisions_systemone_files_and_questions_and_reject_raw_mp4():
    img_part = _image_part(seq=1, kind="video_frame", frame_index=0)
    aud_part = _audio_part(seq=2)
    questions = _systemone_questions()

    prepared = build_request(
        LlamaMediaRequestSpec(
            mode="decisions_systemone",
            model=DECISIONS_MODEL,
            parts=(img_part, aud_part),
            state="Current authenticated user request: count the beeps.",
            questions=questions,
        )
    )
    assert prepared.path == "/v1/systemone"
    assert prepared.query == "autoload=false"
    decoded = json.loads(prepared.raw_body)
    assert set(decoded.keys()) == {"model", "state", "files", "questions"}
    assert decoded["model"] == DECISIONS_MODEL
    assert decoded["files"] == [
        "data:image/png;base64," + base64.b64encode(img_part.data).decode("ascii"),
        "data:audio/wav;base64," + base64.b64encode(aud_part.data).decode("ascii"),
    ]
    assert decoded["questions"] == questions

    # Raw MP4 video_clip is rejected on /v1/systemone (must use sampled frames + WAV).
    with pytest.raises(ProviderRefusal):
        build_request(
            LlamaMediaRequestSpec(
                mode="decisions_systemone",
                model=DECISIONS_MODEL,
                parts=(_video_clip_part(seq=1, through_seq=2),),
                state="Test clip",
                questions=questions,
            )
        )


def test_part_validation_rejects_bad_magic_hashes_and_out_of_order_sequences():
    bad_png = b"not-a-png"
    with pytest.raises(ProviderRefusal):
        LlamaMediaPart(
            kind="image",
            data=bad_png,
            mime_type="image/png",
            source=_source_ref(),
            track_id="trk-video",
            source_seq=1,
            through_source_seq=1,
            pts=MediaSourcePTS(
                pts=0,
                time_base_num=1,
                time_base_den=4,
                sample_start=None,
                sample_count=None,
                frame_index=0,
            ),
            start=MediaQ(num=0, den=1),
            end=MediaQ(num=1, den=4),
            body_sha256=hashlib.sha256(bad_png).hexdigest(),
        )

    # Out-of-order source sequences for the same source are refused.
    p1 = _image_part(seq=5, kind="video_frame", frame_index=1)
    p2 = _audio_part(seq=2)
    with pytest.raises(ProviderRefusal):
        build_request(
            LlamaMediaRequestSpec(
                mode="converse_chat",
                model=CONVERSE_MODEL,
                parts=(p1, p2),
            )
        )


def test_parse_response_accepts_valid_and_rejects_malformed_chat_and_systemone():
    chat_prep = build_request(
        LlamaMediaRequestSpec(
            mode="converse_chat",
            model=CONVERSE_MODEL,
            parts=(_image_part(seq=1),),
        )
    )
    chat_res = parse_response(_chat_response_bytes("Red"), chat_prep)
    assert isinstance(chat_res, LlamaMediaChatResult)
    assert chat_res.text == "Red"
    assert chat_res.finish_reason == "stop"
    assert chat_res.usage == {"prompt_tokens": 147, "completion_tokens": 14, "total_tokens": 161}

    # Truncated finish_reason != "stop" must be refused.
    bad_finish = json.loads(_chat_response_bytes())
    bad_finish["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ProviderRefusal):
        parse_response(json.dumps(bad_finish).encode(), chat_prep)

    # Tool calls or audio output claims in message must be refused.
    bad_tool = json.loads(_chat_response_bytes())
    bad_tool["choices"][0]["message"]["tool_calls"] = [{"id": "call_1"}]
    with pytest.raises(ProviderRefusal):
        parse_response(json.dumps(bad_tool).encode(), chat_prep)

    # SystemOne valid parse and fault rejections.
    s1_prep = build_request(
        LlamaMediaRequestSpec(
            mode="decisions_systemone",
            model=DECISIONS_MODEL,
            parts=(_image_part(seq=1), _audio_part(seq=2)),
            state="State text",
            questions=_systemone_questions(),
        )
    )
    s1_res = parse_response(_systemone_response_bytes(), s1_prep)
    assert isinstance(s1_res, LlamaMediaSystemOneResult)
    assert s1_res.answers["route"]["choice"] == "respond"
    assert s1_res.answers["red_square"]["noul"] == 0.9375

    for mutate in (
        lambda d: d.update({"model": "other-model"}),
        lambda d: d["answers"]["route"].update({"choice": "wait"}),  # not argmax
        lambda d: d["answers"]["route"].update({"confidence": 0.2}),  # mismatch
        lambda d: d["answers"]["red_square"].update({"noul": 1.5}),  # out of [0, 1]
        lambda d: d["answers"].pop("red_square"),  # missing question
    ):
        corrupted = json.loads(_systemone_response_bytes())
        mutate(corrupted)
        with pytest.raises(ProviderRefusal):
            parse_response(json.dumps(corrupted).encode(), s1_prep)

    # Duplicate keys and NaN are refused.
    with pytest.raises(ProviderRefusal):
        parse_response(b'{"model":"a","model":"b"}', s1_prep)
    with pytest.raises(ProviderRefusal):
        parse_response(b'{"model":NaN}', s1_prep)


@pytest.mark.parametrize(
    "url,kwargs",
    [
        ("http://remote.example:8080", {}),
        ("https://127.0.0.1:8080", {"allow_cloud": True}),
        ("http://127.0.0.1", {}),
        ("http://user:pass@127.0.0.1:8080", {}),
        ("http://127.0.0.1:8080/v1", {}),
        ("http://127.0.0.1:8080?x=1", {}),
        ("http://127.0.0.1:8080#frag", {}),
        ("http://127.0.0.1:8080", {"upstream_commit": "0" * 40}),
        ("http://127.0.0.1:8080", {"model": "bad alias"}),
    ],
)
def test_provider_enforces_literal_loopback_and_pinned_commit(url, kwargs):
    with pytest.raises(ProviderError):
        NativeLlamaMediaProvider(url, kwargs.pop("model", CONVERSE_MODEL), **kwargs)


async def test_complete_media_http_exchange_settlement_and_current_guard():
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_chat_response_bytes("Three beeps"))

    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        CONVERSE_MODEL,
        mode="converse_chat",
        transport=httpx.MockTransport(handler),
    )
    try:
        health = await provider.health()
        assert health["ready"] is False
        assert health["upstream_commit"] == UPSTREAM_LLAMA_COMMIT

        result = await provider.complete_media(
            {"parts": (_image_part(seq=1), _audio_part(seq=2))},
            current=lambda: True,
        )
        assert isinstance(result, LlamaMediaChatResult)
        assert result.text == "Three beeps"
        assert len(seen_requests) == 1
        assert seen_requests[0].url.path == "/v1/chat/completions"
        assert seen_requests[0].url.query == b"autoload=false"
        assert provider.last_exchange is not None
        assert provider.last_exchange.state == "settled"
        assert provider.last_exchange.outcome == "completed"
        assert provider.last_exchange.local_task_joined is True
        assert provider.last_exchange.transport_sole_closed is True
        assert provider.last_exchange.body_fd_closed is True

        # Revoked current() guard refuses before sending another request.
        with pytest.raises(ProviderError):
            await provider.complete_media(
                {"parts": (_image_part(seq=1),)},
                current=lambda: False,
            )
        assert len(seen_requests) == 1
    finally:
        await provider.close()


async def test_complete_media_unconfirmed_stream_close_marks_exchange_unknown():
    class FailingCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _chat_response_bytes("Red")

        async def aclose(self) -> None:
            raise RuntimeError("socket close unconfirmed")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=FailingCloseStream())

    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        CONVERSE_MODEL,
        mode="converse_chat",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ProviderCleanupError):
            await provider.complete_media({"parts": (_image_part(seq=1),)})
        assert provider.last_exchange is not None
        assert provider.last_exchange.state == "unknown"
        assert provider.last_exchange.transport_sole_closed is False
        assert provider.last_exchange.outcome is None
    finally:
        await provider.close()


async def test_complete_media_cancellation_settles_clean_stream_close():
    started = asyncio.Event()
    closed = False

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.sleep(10)
            yield _chat_response_bytes("Never reached")

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SlowStream())

    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        CONVERSE_MODEL,
        mode="converse_chat",
        transport=httpx.MockTransport(handler),
    )
    try:
        task = asyncio.create_task(provider.complete_media({"parts": (_image_part(seq=1),)}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed is True
        assert provider.last_exchange is not None
        assert provider.last_exchange.state == "settled"
        assert provider.last_exchange.outcome == "cancelled"
    finally:
        await provider.close()
