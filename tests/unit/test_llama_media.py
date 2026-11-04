"""Unit and controlled-HTTP tests for packages.providers.llama_media."""

from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import hashlib
import json
import math
import struct
import zlib

import httpx
import pytest

from packages.contracts.media import (
    MediaConsumerRef,
    MediaNativeRef,
    MediaQ,
    MediaSourcePTS,
)
from packages.providers.llama_media import (
    MAX_AUDIO_BYTES,
    MAX_SYSTEMONE_FILES,
    PINNED_LLAMA_CPP_COMMIT,
    UPSTREAM_LLAMA_COMMIT,
    LlamaExchangeRegistration,
    LlamaExchangeSettlementBundle,
    LlamaMediaChatResult,
    LlamaMediaPart,
    LlamaMediaProtocolError,
    LlamaMediaRequestSpec,
    LlamaMediaSystemOneResult,
    LlamaMediaTimeoutError,
    LocalBodyClose,
    LocalBodyOwner,
    LocalCloseObservation,
    LocalExchangeCleanupV2,
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


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _ref(kind: str, ref_id: str, seed: str) -> MediaNativeRef:
    return MediaNativeRef(
        backend_id=_id32(f"backend-{kind}"),
        receipt_id=_id32(ref_id),
        sha256=_sha256(seed),
    )


def _source_ref() -> MediaNativeRef:
    return _ref("source", "src-001", "source-001")


def _range_ref() -> MediaNativeRef:
    return _ref("range", "rng-001", "range-001")


def _consumer_ref() -> MediaConsumerRef:
    return MediaConsumerRef(consumer_id=_id32("con-001"), generation=1)


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


class _StubExchangeOwnerBridge:
    """Test double implementing the local_exchange_v2 registration and settlement contract."""

    def __init__(
        self,
        *,
        body_proof: str = "wire_buffer_retired",
        include_extra_body_component: bool = False,
        liability: dict | None = None,
        omit_publication: bool = False,
        mismatch_binding: bool = False,
    ) -> None:
        self.body_proof = body_proof
        self.include_extra_body_component = include_extra_body_component
        self.liability = liability if liability is not None else {"kind": "local_no_remote"}
        self.omit_publication = omit_publication
        self.mismatch_binding = mismatch_binding
        self.events: list[str] = []
        self._seq = 0

    def register_exchange(self, *, prepared, mode: str, model: str) -> LlamaExchangeRegistration:
        self._seq += 1
        s = f"{self._seq}:{mode}:{model}"
        self.events.append("register")
        exchange_id = _id32(f"ex-{s}")
        body_owner_id = _id32(f"body-{s}")
        reader_id = _id32(f"reader-{s}")
        pub_id = _id32(f"pub-{s}")
        body_owner = LocalBodyOwner(
            v=1,
            exchange_id=exchange_id,
            body_owner_id=body_owner_id,
            source=_source_ref(),
            consumer=_consumer_ref(),
            request_sha256=prepared.request_sha256,
            body_bytes=len(prepared.raw_body),
            body_sha256=prepared.request_sha256,
            reader_owner_ids=(reader_id,),
            body={"kind": "wire_buffer"},
        )
        return LlamaExchangeRegistration(
            operation_id=_id32(f"op-{s}"),
            child_instance=_id32(f"child-{s}"),
            binding_sha256=_sha256(f"bind-{s}"),
            original_owner=_ref("owner", f"orig-{s}", f"orig-{s}"),
            exchange_owner=_ref("exchange", f"exref-{s}", f"exref-{s}"),
            body_owner_ref=_ref("body", f"bodyref-{s}", f"bodyref-{s}"),
            body_owner=body_owner,
            local_task_id=_id32(f"task-{s}"),
            transport_owner_id=_id32(f"tr-{s}"),
            publication_owner_ids=(pub_id,),
        )

    def deliver_candidate(self, *, registration: LlamaExchangeRegistration, candidate) -> None:
        assert registration is not None and candidate is not None
        self.events.append("deliver_candidate")

    def settle_exchange(
        self,
        *,
        registration: LlamaExchangeRegistration,
        prepared,
        outcome: str | None,
        transport_sole_closed: bool,
        wire_buffer_retired: bool,
        candidate=None,
    ) -> LlamaExchangeSettlementBundle:
        self.events.append("settle")
        binding = _sha256("wrong-binding") if self.mismatch_binding else registration.binding_sha256
        body_settled = bool(wire_buffer_retired and self.body_proof == "wire_buffer_retired")
        body_close = LocalBodyClose(
            v=1,
            body_owner=registration.body_owner_ref,
            state="settled" if body_settled else "unknown",
            proof=self.body_proof if body_settled or self.body_proof != "none" else "none",
            request_sha256=prepared.request_sha256,
            binding_sha256=binding,
        )
        body_obs_ref = _ref("body_close", f"bclose-{self._seq}", f"bclose-{self._seq}")

        comp_obs: list[LocalCloseObservation] = [
            LocalCloseObservation(
                v=1,
                exchange_owner=registration.exchange_owner,
                component="local_task",
                owner_id=registration.local_task_id,
                state="settled",
                proof="task_join",
                request_sha256=prepared.request_sha256,
                binding_sha256=binding,
            ),
            LocalCloseObservation(
                v=1,
                exchange_owner=registration.exchange_owner,
                component="transport",
                owner_id=registration.transport_owner_id,
                state="settled" if transport_sole_closed else "unknown",
                proof="sole_transport_close" if transport_sole_closed else "none",
                request_sha256=prepared.request_sha256,
                binding_sha256=binding,
            ),
        ]
        for rid in registration.body_owner.reader_owner_ids:
            comp_obs.append(
                LocalCloseObservation(
                    v=1,
                    exchange_owner=registration.exchange_owner,
                    component="reader",
                    owner_id=rid,
                    state="settled" if wire_buffer_retired else "unknown",
                    proof="reader_join" if wire_buffer_retired else "none",
                    request_sha256=prepared.request_sha256,
                    binding_sha256=binding,
                )
            )
        if not self.omit_publication:
            for pid in registration.publication_owner_ids:
                comp_obs.append(
                    LocalCloseObservation(
                        v=1,
                        exchange_owner=registration.exchange_owner,
                        component="publication",
                        owner_id=pid,
                        state="settled",
                        proof="publication_settlement",
                        request_sha256=prepared.request_sha256,
                        binding_sha256=binding,
                    )
                )
        if self.include_extra_body_component:
            comp_obs.append(
                LocalCloseObservation(
                    v=1,
                    exchange_owner=registration.exchange_owner,
                    component="body",
                    owner_id=registration.body_owner.body_owner_id,
                    state="settled",
                    proof="body_fd_close",
                    request_sha256=prepared.request_sha256,
                    binding_sha256=binding,
                )
            )

        obs_refs = tuple(
            _ref("obs", f"obs-{self._seq}-{idx}", f"obs-{self._seq}-{idx}")
            for idx in range(len(comp_obs))
        )
        all_five = (
            transport_sole_closed
            and body_settled
            and wire_buffer_retired
            and not self.omit_publication
            and outcome is not None
        )
        cleanup = LocalExchangeCleanupV2(
            v=1,
            kind="local_exchange_v2",
            operation_id=registration.operation_id,
            child_instance=registration.child_instance,
            binding_sha256=registration.binding_sha256,
            original_owner=registration.original_owner,
            exchange_owner=registration.exchange_owner,
            body_owner=registration.body_owner_ref,
            body_observation=body_obs_ref,
            state="settled" if all_five else "unknown",
            outcome=outcome if all_five else None,
            local_task_joined=True,
            transport_sole_closed=transport_sole_closed,
            body_settled=body_settled,
            all_readers_settled=wire_buffer_retired,
            publications_settled=not self.omit_publication,
            observations=obs_refs,
            liability=self.liability,
        )
        return LlamaExchangeSettlementBundle(
            body_close=body_close,
            body_observation=body_obs_ref,
            component_observations=tuple(comp_obs),
            observation_refs=obs_refs,
            cleanup=cleanup,
        )


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
    assert prepared.upstream_commit == UPSTREAM_LLAMA_COMMIT == PINNED_LLAMA_CPP_COMMIT
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


def test_systemone_enforces_eight_file_ceiling_and_pre_hash_byte_bounds(monkeypatch):
    questions = _systemone_questions()
    eight_parts = tuple(
        _image_part(seq=idx + 1, kind="video_frame", frame_index=idx)
        for idx in range(MAX_SYSTEMONE_FILES)
    )
    prepared_eight = build_request(
        LlamaMediaRequestSpec(
            mode="decisions_systemone",
            model=DECISIONS_MODEL,
            parts=eight_parts,
            state="Eight files allowed",
            questions=questions,
        )
    )
    assert len(prepared_eight.body["files"]) == MAX_SYSTEMONE_FILES == 8

    # 9th file in SystemOne mode must be rejected before any hashing or base64 encoding.
    nine_raw_dicts = [
        {
            "kind": "video_frame",
            "data": _make_png(),
            "mime_type": "image/png",
            "source": _source_ref(),
            "track_id": "trk-video",
            "source_seq": idx + 1,
            "through_source_seq": idx + 1,
            "pts": MediaSourcePTS(
                pts=idx,
                time_base_num=1,
                time_base_den=4,
                sample_start=None,
                sample_count=None,
                frame_index=idx,
            ),
            "start": _q(idx, 4),
            "end": _q(idx + 1, 4),
            "body_sha256": "0" * 64,
        }
        for idx in range(MAX_SYSTEMONE_FILES + 1)
    ]

    source_ref = _source_ref()
    sha_calls = 0
    real_sha256 = hashlib.sha256

    def counting_sha256(*args, **kwargs):
        nonlocal sha_calls
        sha_calls += 1
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr("packages.providers.llama_media.hashlib.sha256", counting_sha256)
    with pytest.raises(LlamaMediaProtocolError):
        build_request(
            {
                "mode": "decisions_systemone",
                "model": DECISIONS_MODEL,
                "parts": nine_raw_dicts,
                "state": "Nine files rejected",
                "questions": questions,
            }
        )
    assert sha_calls == 0

    # Oversized audio part (> MAX_AUDIO_BYTES) is rejected before SHA-256 hashing.
    oversized_wav = b"RIFF" + b"\x00" * (MAX_AUDIO_BYTES + 1)
    with pytest.raises(LlamaMediaProtocolError):
        LlamaMediaPart(
            kind="audio",
            data=oversized_wav,
            mime_type="audio/wav",
            source=source_ref,
            track_id="trk-audio",
            source_seq=1,
            through_source_seq=1,
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
            body_sha256="0" * 64,
        )
    assert sha_calls == 0


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


def test_parse_response_accepts_valid_and_rejects_missing_or_mismatched_model_and_malformed():
    chat_prep = build_request(
        LlamaMediaRequestSpec(
            mode="converse_chat",
            model=CONVERSE_MODEL,
            parts=(_image_part(seq=1),),
        )
    )
    chat_res = parse_response(_chat_response_bytes("Red"), chat_prep)
    assert isinstance(chat_res, LlamaMediaChatResult)
    assert chat_res.model == CONVERSE_MODEL
    assert chat_res.text == "Red"
    assert chat_res.finish_reason == "stop"
    assert chat_res.usage == {"prompt_tokens": 147, "completion_tokens": 14, "total_tokens": 161}

    # Missing, empty, non-string, or mismatched top-level "model" in Converse chat must be refused.
    for mutate_chat_model in (
        lambda d: d.pop("model"),
        lambda d: d.update({"model": ""}),
        lambda d: d.update({"model": None}),
        lambda d: d.update({"model": "other-model"}),
    ):
        bad_chat = json.loads(_chat_response_bytes())
        mutate_chat_model(bad_chat)
        with pytest.raises(LlamaMediaProtocolError):
            parse_response(json.dumps(bad_chat).encode(), chat_prep)

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

    # SystemOne valid parse and fault rejections (including missing/mismatched model).
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
    assert s1_res.model == DECISIONS_MODEL
    assert s1_res.answers["route"]["choice"] == "respond"
    assert s1_res.answers["red_square"]["noul"] == 0.9375

    for mutate in (
        lambda d: d.pop("model"),
        lambda d: d.update({"model": ""}),
        lambda d: d.update({"model": "other-model"}),
        lambda d: d["answers"]["route"].update({"choice": "wait"}),  # not argmax
        lambda d: d["answers"]["route"].update({"confidence": 0.2}),  # mismatch
        lambda d: d["answers"]["red_square"].update({"noul": 1.5}),  # out of [0, 1]
        lambda d: d["answers"].pop("red_square"),  # missing question
    ):
        corrupted = json.loads(_systemone_response_bytes())
        mutate(corrupted)
        with pytest.raises(LlamaMediaProtocolError):
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
    kw = dict(kwargs)
    with pytest.raises(ProviderError):
        NativeLlamaMediaProvider(url, kw.pop("model", CONVERSE_MODEL), **kw)


async def test_complete_media_without_bridge_records_unknown_settlement_and_retains_history():
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

        result1 = await provider.complete_media(
            {"parts": (_image_part(seq=1), _audio_part(seq=2))},
            current=lambda: True,
        )
        assert isinstance(result1, LlamaMediaChatResult)
        assert result1.text == "Three beeps"
        assert len(seen_requests) == 1
        assert seen_requests[0].url.path == "/v1/chat/completions"
        assert seen_requests[0].url.query == b"autoload=false"

        ex1 = provider.last_exchange
        assert ex1 is not None
        assert ex1.kind == "local_exchange_v2"
        assert ex1.state == "unknown"
        assert ex1.outcome is None
        assert ex1.attempt_disposition == "completed"
        assert ex1.transport_sole_closed is True
        assert ex1.local_task_joined is False
        assert ex1.body_settled is False
        assert ex1.body_fd_closed is False
        assert ex1.all_readers_settled is False
        assert ex1.publications_settled is False
        assert ex1.body_proof == "none"

        # A second exchange does not overwrite or discard the first unresolved exchange.
        result2 = await provider.complete_media(
            {"parts": (_image_part(seq=1),)},
            current=lambda: True,
        )
        assert isinstance(result2, LlamaMediaChatResult)
        assert len(provider.exchanges) == 2
        assert len(provider.unresolved_exchanges) == 2
        assert provider.exchanges[0].exchange_id == ex1.exchange_id
        assert provider.exchanges_by_id[ex1.exchange_id] == ex1

        # Revoked current() guard refuses before sending another request.
        with pytest.raises(ProviderError):
            await provider.complete_media(
                {"parts": (_image_part(seq=1),)},
                current=lambda: False,
            )
        assert len(seen_requests) == 2
    finally:
        await provider.close()


async def test_complete_media_with_valid_local_exchange_v2_bridge_settles_and_rejects_bad_claims():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_chat_response_bytes("Bridge settled"))

    bridge = _StubExchangeOwnerBridge()
    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        CONVERSE_MODEL,
        mode="converse_chat",
        transport=httpx.MockTransport(handler),
    )
    try:
        # 1. Exchange without bridge stays in unresolved_exchanges.
        await provider.complete_media({"parts": (_image_part(seq=1),)})
        first_unresolved = provider.last_exchange
        assert first_unresolved is not None and first_unresolved.state == "unknown"

        # 2. Exchange with valid local_exchange_v2 bridge settles after candidate delivery.
        result = await provider.complete_media(
            {"parts": (_image_part(seq=1), _audio_part(seq=2))},
            exchange_owner_bridge=bridge,
        )
        assert result.text == "Bridge settled"
        assert bridge.events == ["register", "deliver_candidate", "settle"]
        settled = provider.last_exchange
        assert settled is not None
        assert settled.kind == "local_exchange_v2"
        assert settled.state == "settled"
        assert settled.outcome == "completed"
        assert settled.attempt_disposition == "completed"
        assert settled.local_task_joined is True
        assert settled.transport_sole_closed is True
        assert settled.body_settled is True
        assert settled.body_fd_closed is False
        assert settled.all_readers_settled is True
        assert settled.publications_settled is True
        assert settled.body_storage == {"kind": "wire_buffer"}
        assert settled.body_proof == "wire_buffer_retired"
        assert settled.liability == {"kind": "local_no_remote"}
        assert settled.cleanup_receipt is not None
        assert settled.cleanup_receipt.kind == "local_exchange_v2"
        # First unresolved exchange is still preserved in unresolved_exchanges!
        assert provider.unresolved_exchanges == (first_unresolved,)
        assert len(provider.exchanges) == 2

        # 3. Invalid bridge claims (FD proof on wire_buffer, extra body component,
        # missing publication, or binding mismatch) are downgraded to unknown/outcome=None.
        for bad_bridge in (
            _StubExchangeOwnerBridge(body_proof="original_body_fd_closed"),
            _StubExchangeOwnerBridge(include_extra_body_component=True),
            _StubExchangeOwnerBridge(omit_publication=True),
            _StubExchangeOwnerBridge(mismatch_binding=True),
            _StubExchangeOwnerBridge(
                liability={
                    "kind": "disclosed_remote",
                    "remote_liability": _ref("liab", "rem-1", "rem-1"),
                }
            ),
        ):
            await provider.complete_media(
                {"parts": (_image_part(seq=1),)},
                exchange_owner_bridge=bad_bridge,
            )
            assert provider.last_exchange is not None
            assert provider.last_exchange.state == "unknown"
            assert provider.last_exchange.outcome is None
    finally:
        await provider.close()


async def test_complete_media_revalidates_prepared_request_and_refuses_tampering_before_dispatch():
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, content=_chat_response_bytes("Never reached"))

    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        CONVERSE_MODEL,
        mode="converse_chat",
        transport=httpx.MockTransport(handler),
    )
    try:
        base_prep = build_request(
            LlamaMediaRequestSpec(
                mode="converse_chat",
                model=CONVERSE_MODEL,
                parts=(_image_part(seq=1),),
                system_prompt="Original prompt",
            )
        )

        # 1. Absolute HTTPS URL or path traversal in prepared.path is refused before dispatch.
        for bad_path in (
            "https://evil.example:8443/v1/chat/completions",
            "http://evil.example/v1/chat/completions",
            "//evil.example/v1/chat/completions",
            "/v1/chat/completions/../models",
            "/v1/systemone",
            "/v1/chat/completions?autoload=true",
        ):
            tampered_path = dataclasses.replace(base_prep, path=bad_path)
            with pytest.raises(LlamaMediaProtocolError):
                await provider.complete_media(tampered_path)

        # 2. Missing or modified ?autoload=false query is refused.
        for bad_query in ("", "autoload=true", "autoload=false&extra=1"):
            tampered_query = dataclasses.replace(base_prep, query=bad_query)
            with pytest.raises(LlamaMediaProtocolError):
                await provider.complete_media(tampered_query)

        # 3. Wrong upstream_commit is refused.
        tampered_commit = dataclasses.replace(base_prep, upstream_commit="0" * 40)
        with pytest.raises(LlamaMediaProtocolError):
            await provider.complete_media(tampered_commit)

        # 4. In-place mutation of prepared.body is refused.
        mutated_body = copy.deepcopy(base_prep.body)
        mutated_body["messages"][0]["content"] = "Injected system prompt"
        tampered_body = dataclasses.replace(base_prep, body=mutated_body)
        with pytest.raises(LlamaMediaProtocolError):
            await provider.complete_media(tampered_body)

        # 5. Even if raw_body and request_sha256 are recomputed for mutated body,
        # provenance mismatch is caught and refused.
        forged_raw = json.dumps(mutated_body, separators=(",", ":")).encode("utf-8")
        OtherPng = _make_png(width=8, height=8)
        mutated_img_body = copy.deepcopy(base_prep.body)
        mutated_img_body["messages"][-1]["content"][0]["image_url"]["url"] = (
            "data:image/png;base64," + base64.b64encode(OtherPng).decode("ascii")
        )
        forged_img_raw = json.dumps(mutated_img_body, separators=(",", ":")).encode("utf-8")
        tampered_forged = dataclasses.replace(
            base_prep,
            body=mutated_img_body,
            raw_body=forged_img_raw,
            request_sha256=hashlib.sha256(forged_img_raw).hexdigest(),
        )
        with pytest.raises(LlamaMediaProtocolError):
            await provider.complete_media(tampered_forged)
        assert forged_raw != base_prep.raw_body

        assert len(seen_requests) == 0
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

    bridge = _StubExchangeOwnerBridge()
    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        CONVERSE_MODEL,
        mode="converse_chat",
        transport=httpx.MockTransport(handler),
        exchange_owner_bridge=bridge,
    )
    try:
        with pytest.raises(ProviderCleanupError):
            await provider.complete_media({"parts": (_image_part(seq=1),)})
        assert provider.last_exchange is not None
        assert provider.last_exchange.state == "unknown"
        assert provider.last_exchange.transport_sole_closed is False
        assert provider.last_exchange.outcome is None
        assert provider.last_exchange.attempt_disposition == "cleanup_unconfirmed"
        assert provider.last_exchange in provider.unresolved_exchanges
    finally:
        await provider.close()


async def test_complete_media_cancellation_closes_stream_once_and_records_cancelled_disposition():
    started = asyncio.Event()
    close_calls = 0

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.sleep(10)
            yield _chat_response_bytes("Never reached")

        async def aclose(self) -> None:
            nonlocal close_calls
            close_calls += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SlowStream())

    bridge = _StubExchangeOwnerBridge()
    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        CONVERSE_MODEL,
        mode="converse_chat",
        transport=httpx.MockTransport(handler),
        exchange_owner_bridge=bridge,
    )
    try:
        task = asyncio.create_task(provider.complete_media({"parts": (_image_part(seq=1),)}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert close_calls == 1
        assert provider.last_exchange is not None
        assert provider.last_exchange.transport_sole_closed is True
        assert provider.last_exchange.state == "settled"
        assert provider.last_exchange.outcome == "cancelled"
        assert provider.last_exchange.attempt_disposition == "cancelled"
    finally:
        await provider.close()


class _SuppressionGateStream(httpx.AsyncByteStream):
    def __init__(self, transport: _SuppressionGateTransport) -> None:
        self.owner = transport

    async def __aiter__(self):
        self.owner.read_task = asyncio.current_task()
        if self.owner.phase == "read":
            await self.owner.hold()
        yield self.owner.response_bytes

    async def aclose(self) -> None:
        self.owner.close_count += 1
        self.owner.close_task = asyncio.current_task()
        if self.owner.phase == "close":
            await self.owner.hold()


class _SuppressionGateTransport(httpx.AsyncBaseTransport):
    """Controlled transport that suppresses CancelledError in send, read, or close."""

    def __init__(self, phase: str, response_bytes: bytes) -> None:
        self.phase = phase
        self.response_bytes = response_bytes
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancel_observed = asyncio.Event()
        self.send_task: asyncio.Task | None = None
        self.read_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self.close_count = 0

    async def hold(self) -> None:
        self.entered.set()
        while not self.release.is_set():
            try:
                await asyncio.shield(self.release.wait())
            except asyncio.CancelledError:
                self.cancel_observed.set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.send_task = asyncio.current_task()
        if self.phase == "send":
            await self.hold()
        return httpx.Response(200, stream=_SuppressionGateStream(self))


def _spec_for_mode(mode: str) -> dict:
    if mode == "converse_chat":
        return {"parts": (_image_part(seq=1), _audio_part(seq=2))}
    return {
        "parts": (_image_part(seq=1), _audio_part(seq=2)),
        "state": "Current authenticated user request: count the beeps.",
        "questions": _systemone_questions(),
    }


def _response_for_mode(mode: str) -> bytes:
    if mode == "converse_chat":
        return _chat_response_bytes("Suppressed result must never escape")
    return _systemone_response_bytes()


def _model_for_mode(mode: str) -> str:
    return CONVERSE_MODEL if mode == "converse_chat" else DECISIONS_MODEL


@pytest.mark.parametrize("mode", ["converse_chat", "decisions_systemone"])
@pytest.mark.parametrize("phase", ["send", "read", "close"])
async def test_complete_media_suppressed_caller_cancellation_rejected_after_single_close(
    mode: str,
    phase: str,
):
    wire = _SuppressionGateTransport(phase, _response_for_mode(mode))
    bridge = _StubExchangeOwnerBridge()
    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        _model_for_mode(mode),
        mode=mode,
        timeout=5.0,
        transport=wire,
        exchange_owner_bridge=bridge,
    )
    runner = asyncio.create_task(provider.complete_media(_spec_for_mode(mode)))
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
        runner.cancel()
        await asyncio.sleep(0)
        assert not runner.done()
        wire.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(runner), 5)

        assert wire.close_count == 1
        assert provider.last_exchange is not None
        assert provider.last_exchange.transport_sole_closed is True
        assert provider.last_exchange.attempt_disposition == "cancelled"
        assert provider.last_exchange.outcome != "completed"
        assert "deliver_candidate" not in bridge.events
    finally:
        wire.release.set()
        await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 5)
        await provider.close()


@pytest.mark.parametrize("mode", ["converse_chat", "decisions_systemone"])
@pytest.mark.parametrize("phase", ["send", "read", "close"])
async def test_complete_media_suppressed_deadline_expiration_rejected_after_single_close(
    mode: str,
    phase: str,
):
    wire = _SuppressionGateTransport(phase, _response_for_mode(mode))
    bridge = _StubExchangeOwnerBridge()
    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        _model_for_mode(mode),
        mode=mode,
        timeout=0.03,
        transport=wire,
        exchange_owner_bridge=bridge,
    )
    runner = asyncio.create_task(provider.complete_media(_spec_for_mode(mode)))
    try:
        await asyncio.wait_for(wire.entered.wait(), 5)
        if phase in {"send", "read"}:
            await asyncio.wait_for(wire.cancel_observed.wait(), 5)
        else:
            # In close phase, _OwnedHTTPStream shields the inner close task while
            # the caller task receives the timeout cancellation.
            await asyncio.sleep(0.06)
        assert not runner.done()
        wire.release.set()
        with pytest.raises(TimeoutError) as exc_info:
            await asyncio.wait_for(asyncio.shield(runner), 5)
        assert isinstance(exc_info.value, LlamaMediaTimeoutError)
        assert isinstance(exc_info.value, ProviderError)
        assert not isinstance(exc_info.value, ProviderRefusal)

        assert wire.close_count == 1
        assert provider.last_exchange is not None
        assert provider.last_exchange.transport_sole_closed is True
        assert provider.last_exchange.attempt_disposition == "timed_out"
        assert provider.last_exchange.outcome != "completed"
        assert "deliver_candidate" not in bridge.events
    finally:
        wire.release.set()
        await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 5)
        await provider.close()


@pytest.mark.parametrize("mode", ["converse_chat", "decisions_systemone"])
async def test_complete_media_preexisting_cancellation_count_allows_healthy_exchange(mode: str):
    wire = _SuppressionGateTransport("healthy", _response_for_mode(mode))
    bridge = _StubExchangeOwnerBridge()
    provider = NativeLlamaMediaProvider(
        "http://127.0.0.1:8890",
        _model_for_mode(mode),
        mode=mode,
        timeout=5.0,
        transport=wire,
        exchange_owner_bridge=bridge,
    )
    recorded_baseline: list[int] = []

    async def run_with_preexisting_cancel():
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        recorded_baseline.append(task.cancelling())
        assert recorded_baseline[0] > 0
        return await provider.complete_media(_spec_for_mode(mode))

    runner = asyncio.create_task(run_with_preexisting_cancel())
    try:
        result = await asyncio.wait_for(asyncio.shield(runner), 5)
        assert runner.cancelling() == recorded_baseline[0]
        assert wire.close_count == 1
        assert provider.last_exchange is not None
        assert provider.last_exchange.state == "settled"
        assert provider.last_exchange.outcome == "completed"
        assert provider.last_exchange.attempt_disposition == "completed"
        if mode == "converse_chat":
            assert isinstance(result, LlamaMediaChatResult)
        else:
            assert isinstance(result, LlamaMediaSystemOneResult)
    finally:
        await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 5)
        await provider.close()
