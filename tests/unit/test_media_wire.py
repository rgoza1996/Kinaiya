"""Independent bounded framing oracles; no native ACK/cleanup is implied."""

import asyncio
import hashlib
import json

import pytest
from services.common.media_wire import MediaWireError, encode_ingress, open_ingress

from packages.contracts import media

_ABC_SHA = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
_UPLOAD_JSON = (
    b'{"body_bytes":3,"body_sha256":"'
    b"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    b'","fence":{"generation":1,"lease_generation":1,"session":{"epoch":1,'
    b'"session_id":"synthetic-session","thread_id":"synthetic-thread"},'
    b'"source_revision":1},"index":0,"key":"upload-0","offset":0,"source":{'
    b'"backend_id":"00000000000000000000000000000000",'
    b'"receipt_id":"11111111111111111111111111111111",'
    b'"sha256":"2222222222222222222222222222222222222222222222222222222222222222"},'
    b'"v":1}'
)
_FIXED_FRAME = b"\x00\x00\x01\xdc" + _UPLOAD_JSON + b"abc"
_ERROR_CODES = {
    "bad_packet",
    "denied",
    "unknown_ref",
    "stale",
    "expired",
    "unsupported",
    "bound",
    "capacity",
    "unavailable",
    "conflict",
}


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _frame(header, body=b""):
    return len(header).to_bytes(4, "big") + header + body


class _Chunks:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pulls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.pulls += 1
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration from None


async def _consume(body):
    return b"".join([chunk async for chunk in body.iter_chunks()])


def _assert_closed_error(failure, code=None):
    assert isinstance(failure.value, ValueError)
    assert failure.value.code in _ERROR_CODES
    if code is not None:
        assert failure.value.code == code
    assert "synthetic-untrusted-detail" not in str(failure.value)


def _capture_header(body=b"\x00\x00"):
    upload = json.loads(_UPLOAD_JSON)
    return {
        "v": 1,
        "key": "capture-0",
        "source": upload["source"],
        "fence": upload["fence"],
        "capture_instance": "a" * 32,
        "track_id": "b" * 32,
        "track_seq": 1,
        "pts": {
            "pts": 0,
            "time_base_num": 1,
            "time_base_den": 16000,
            "sample_start": 0,
            "sample_count": 1,
            "frame_index": None,
        },
        "format_sha256": "f" * 64,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def _asr_header(body=b"\x00\x00"):
    capture = _capture_header(body)
    consumer = {"consumer_id": "c" * 32, "generation": 1}
    session = {
        "v": 1,
        "worker_instance": "d" * 32,
        "operation_id": "e" * 32,
        "source": capture["source"],
        "fence": capture["fence"],
        "track_id": capture["track_id"],
        "consumer": consumer,
        "continuity_id": "a" * 32,
        "first_sample": 0,
        "request_sha256": "f" * 64,
        "retention": "declared_segmented_fallback",
    }
    packet = {
        "v": 1,
        "operation_id": session["operation_id"],
        "consumer": consumer,
        "source": session["source"],
        "fence": session["fence"],
        "payload": "audio",
        "track_id": capture["track_id"],
        "source_seq": 1,
        "through_source_seq": 1,
        "track_seq": 1,
        "pts": capture["pts"],
        "range": None,
        "start": {"num": 0, "den": 1},
        "end": {"num": 1, "den": 16000},
        "format_sha256": capture["format_sha256"],
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "continuity_id": session["continuity_id"],
        "sequence": 1,
    }
    return {"v": 1, "key": "asr-0", "session": session, "sequence": 1, "packet": packet}


@pytest.mark.parametrize("width", [1, 2, 3, 4, 7, 64, 65536])
async def test_fixed_frame_accepts_fragmented_and_coalesced_prefix_header_body(width):
    assert len(_UPLOAD_JSON) == 476
    chunks = [_FIXED_FRAME[i : i + width] for i in range(0, len(_FIXED_FRAME), width)]
    body = await open_ingress(_Chunks(chunks), media.MediaUploadPut)
    assert type(body.header) is media.MediaUploadPut
    assert media.media_bytes(body.header) == _UPLOAD_JSON
    assert body.verified is False
    assert await _consume(body) == b"abc"
    assert body.verified is True


def test_encoder_matches_fixed_external_frame_not_its_own_roundtrip():
    header = media.media_json(_UPLOAD_JSON, media.MediaUploadPut)
    assert encode_ingress(header, b"abc") == _FIXED_FRAME


@pytest.mark.parametrize(
    "kind_name,header",
    [
        ("MediaCapturePut", _capture_header()),
        ("MediaASRFeed", _asr_header()),
    ],
)
async def test_all_three_ingress_kinds_use_exact_declared_binary_body(kind_name, header):
    kind = getattr(media, kind_name)
    raw = _json(header)
    body = await open_ingress(_Chunks([_frame(raw, b"\x00\x00")]), kind)
    assert media.media_bytes(body.header) == raw
    assert body.verified is False
    assert await _consume(body) == b"\x00\x00"
    assert body.verified is True


@pytest.mark.parametrize("prefix", [b"", b"\0", b"\0\0", b"\0\0\1"])
async def test_partial_prefix_never_returns_an_ingress_body(prefix):
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(_Chunks([prefix]), media.MediaUploadPut)
    _assert_closed_error(failure, "bad_packet")


@pytest.mark.parametrize("length,code", [(0, "bad_packet"), (65537, "bound"), (2**32 - 1, "bound")])
async def test_invalid_header_length_refuses_before_pulling_payload(length, code):
    chunks = _Chunks([length.to_bytes(4, "big"), b"synthetic-untrusted-detail"])
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(chunks, media.MediaUploadPut)
    _assert_closed_error(failure, code)
    assert chunks.pulls == 1


@pytest.mark.parametrize("length,header", [(476, _UPLOAD_JSON[:-1]), (65536, b"{")])
async def test_declared_header_must_be_complete(length, header):
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(_Chunks([length.to_bytes(4, "big") + header]), media.MediaUploadPut)
    _assert_closed_error(failure, "bad_packet")


@pytest.mark.parametrize(
    "raw",
    [
        b" " + _UPLOAD_JSON,
        _UPLOAD_JSON + b"\n",
        json.dumps(
            dict(reversed(list(json.loads(_UPLOAD_JSON).items()))), separators=(",", ":")
        ).encode(),
        _UPLOAD_JSON.replace(b"upload-0", b"upload-\\u0030"),
        _UPLOAD_JSON.replace(b'"body_bytes":3', b'"body_bytes":3,"body_bytes":3'),
        _UPLOAD_JSON.replace(b'"generation":1', b'"generation":true'),
        _UPLOAD_JSON.replace(b'"body_bytes":3', b'"body_bytes":3.0'),
        _UPLOAD_JSON.replace(b"upload-0", b"\xff"),
        _UPLOAD_JSON.replace(b'"v":1', b'"unknown":0,"v":1'),
        _json({k: v for k, v in json.loads(_UPLOAD_JSON).items() if k != "offset"}),
    ],
)
async def test_noncanonical_or_malformed_header_never_pulls_body(raw):
    chunks = _Chunks([_frame(raw), b"abc"])
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(chunks, media.MediaUploadPut)
    _assert_closed_error(failure, "bad_packet")
    assert chunks.pulls == 1


@pytest.mark.parametrize(
    "raw",
    [
        b'{"bomb":[' + b",".join([b"0"] * 8192) + b"]}",
        b'{"bomb":' + b"[" * 23 + b"0" + b"]" * 23 + b"}",
    ],
)
async def test_header_node_and_depth_caps_refuse_before_body(raw):
    chunks = _Chunks([_frame(raw), b"abc"])
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(chunks, media.MediaUploadPut)
    _assert_closed_error(failure, "bound")
    assert chunks.pulls == 1


@pytest.mark.parametrize("body_bytes", [0, 4194305, 9007199254740992])
async def test_upload_declared_body_bounds_are_conjunctive_with_outer_wire_cap(body_bytes):
    header = json.loads(_UPLOAD_JSON)
    header["body_bytes"] = body_bytes
    chunks = _Chunks([_frame(_json(header)), b"abc"])
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(chunks, media.MediaUploadPut)
    _assert_closed_error(failure, "bound")
    assert chunks.pulls == 1


async def test_upload_exact_four_mib_boundary_is_accepted():
    payload = b"x" * 4194304
    header = json.loads(_UPLOAD_JSON)
    header.update(body_bytes=len(payload), body_sha256=hashlib.sha256(payload).hexdigest())
    raw = _json(header)
    body = await open_ingress(
        _Chunks([_frame(raw), payload[:17], payload[17:]]), media.MediaUploadPut
    )
    assert body.verified is False
    assert await _consume(body) == payload
    assert body.verified is True


@pytest.mark.parametrize("kind_name", ["MediaCapturePut", "MediaASRFeed"])
async def test_other_ingress_outer_body_cap_is_sixteen_mib(kind_name):
    header = _capture_header() if kind_name == "MediaCapturePut" else _asr_header()
    target = header if kind_name == "MediaCapturePut" else header["packet"]
    target["body_bytes"] = 16777217
    chunks = _Chunks([_frame(_json(header)), b"not-read"])
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(chunks, getattr(media, kind_name))
    _assert_closed_error(failure, "bound")
    assert chunks.pulls == 1


@pytest.mark.parametrize("payload", [b"", b"a", b"ab", b"abcd"])
async def test_short_or_long_body_cannot_be_verified(payload):
    body = await open_ingress(_Chunks([_frame(_UPLOAD_JSON), payload]), media.MediaUploadPut)
    with pytest.raises(MediaWireError) as failure:
        await _consume(body)
    _assert_closed_error(failure, "bad_packet")
    assert body.verified is False


@pytest.mark.parametrize("suffix_chunks", [[b"x"], [_FIXED_FRAME]])
async def test_trailing_bytes_or_second_frame_do_not_ack_first_frame(suffix_chunks):
    body = await open_ingress(_Chunks([_FIXED_FRAME, *suffix_chunks]), media.MediaUploadPut)
    with pytest.raises(MediaWireError) as failure:
        await _consume(body)
    _assert_closed_error(failure, "bad_packet")
    assert body.verified is False


async def test_body_hash_failure_never_sets_verified():
    body = await open_ingress(_Chunks([_frame(_UPLOAD_JSON, b"abd")]), media.MediaUploadPut)
    with pytest.raises(MediaWireError) as failure:
        await _consume(body)
    _assert_closed_error(failure, "bad_packet")
    assert body.verified is False
    with pytest.raises(MediaWireError) as second_failure:
        await _consume(body)
    _assert_closed_error(second_failure)
    assert body.verified is False


async def test_body_is_single_use_even_after_success():
    body = await open_ingress(_Chunks([_FIXED_FRAME]), media.MediaUploadPut)
    assert await _consume(body) == b"abc"
    assert body.verified is True
    with pytest.raises(MediaWireError) as failure:
        await _consume(body)
    _assert_closed_error(failure)


async def test_partial_abandonment_does_not_allow_second_intake_or_verified():
    body = await open_ingress(_Chunks([_frame(_UPLOAD_JSON), b"a", b"bc"]), media.MediaUploadPut)
    iterator = body.iter_chunks()
    first = await anext(iterator)
    assert first and b"abc".startswith(first)
    assert body.verified is False
    await iterator.aclose()
    assert body.verified is False
    with pytest.raises(MediaWireError) as failure:
        await _consume(body)
    _assert_closed_error(failure)
    assert body.verified is False


async def test_declared_bytes_do_not_verify_before_true_stream_end():
    end_requested = asyncio.Event()
    allow_end = asyncio.Event()

    async def chunks():
        yield _FIXED_FRAME
        end_requested.set()
        await allow_end.wait()

    body = await open_ingress(chunks(), media.MediaUploadPut)
    pending_end = asyncio.create_task(_consume(body))
    try:
        await asyncio.wait_for(end_requested.wait(), 2)
        assert body.verified is False
        assert not pending_end.done()
        allow_end.set()
        assert await pending_end == b"abc"
        assert body.verified is True
    finally:
        allow_end.set()
        if not pending_end.done():
            pending_end.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending_end


async def test_cancelled_body_stays_unverified():
    blocked = asyncio.Event()
    never_finish = asyncio.Event()

    async def chunks():
        yield _frame(_UPLOAD_JSON, b"a")
        blocked.set()
        await never_finish.wait()
        yield b"bc"

    body = await open_ingress(chunks(), media.MediaUploadPut)
    pending = asyncio.create_task(_consume(body))
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert body.verified is False
    finally:
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending


async def test_upstream_error_is_fixed_and_cannot_claim_verified():
    async def chunks():
        yield _frame(_UPLOAD_JSON, b"a")
        raise RuntimeError("synthetic-untrusted-detail")

    body = await open_ingress(chunks(), media.MediaUploadPut)
    with pytest.raises(MediaWireError) as failure:
        await _consume(body)
    _assert_closed_error(failure)
    assert body.verified is False


@pytest.mark.parametrize("chunk", [bytearray(_FIXED_FRAME), memoryview(_FIXED_FRAME), "frame"])
async def test_transport_chunks_must_be_exact_bytes(chunk):
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(_Chunks([chunk]), media.MediaUploadPut)
    _assert_closed_error(failure, "bad_packet")


async def test_non_ingress_kind_is_rejected_without_intake():
    chunks = _Chunks([_FIXED_FRAME])
    with pytest.raises(MediaWireError) as failure:
        await open_ingress(chunks, media.MediaSession)
    _assert_closed_error(failure)
    assert chunks.pulls == 0


@pytest.mark.parametrize("payload", [b"ab", b"abd", b"abcd"])
def test_encoder_refuses_inconsistent_body_without_creating_a_frame(payload):
    header = media.media_json(_UPLOAD_JSON, media.MediaUploadPut)
    with pytest.raises(MediaWireError) as failure:
        encode_ingress(header, payload)
    _assert_closed_error(failure, "bad_packet")


async def test_immediately_ready_tiny_chunks_allow_concurrent_cancel_before_verification():
    # Every producer await is immediately ready. A scheduled controller must get
    # a turn while the native reader is accumulating this finite body.
    payload = b"x" * 131072
    header = json.loads(_UPLOAD_JSON)
    header.update(body_bytes=len(payload), body_sha256=hashlib.sha256(payload).hexdigest())
    body_started = asyncio.Event()
    controller_ready = asyncio.Event()
    cancel_results = []

    class ImmediateChunks:
        def __init__(self):
            self.header_sent = False
            self.body_pulls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.header_sent:
                self.header_sent = True
                return _frame(_json(header))
            if self.body_pulls == len(payload):
                raise StopAsyncIteration
            self.body_pulls += 1
            body_started.set()
            return b"x"

    chunks = ImmediateChunks()
    body = await open_ingress(chunks, media.MediaUploadPut)
    assert chunks.body_pulls == 0 and body.verified is False
    consumer = None

    async def controller():
        controller_ready.set()
        await body_started.wait()
        cancel_results.append(consumer.cancel())

    canceller = asyncio.create_task(controller())
    try:
        await controller_ready.wait()
        consumer = asyncio.create_task(_consume(body))
        with pytest.raises(asyncio.CancelledError):
            await consumer
        await canceller
        assert cancel_results == [True]
        assert 0 < chunks.body_pulls < len(payload)
        assert body.verified is False
    finally:
        for task in (consumer, canceller):
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


async def test_true_eof_after_whole_ingress_deadline_never_sets_verified(monkeypatch):
    from contextvars import ContextVar

    loop = asyncio.get_running_loop()
    original_time = loop.time
    initial_time = original_time()
    clock = {"now": initial_time}
    operation_clock = ContextVar("media_eof_deadline_oracle", default=False)

    def controlled_time():
        # The scheduler/framework retains its real clock; operation child Tasks
        # inherit this context, so no private wire/task state is consulted.
        return clock["now"] if operation_clock.get() else original_time()

    class EOFAdvance:
        def __init__(self):
            self.chunks = iter([_frame(_UPLOAD_JSON), b"abc"])
            self.eof_reported = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.chunks)
            except StopIteration:
                self.eof_reported = True
                clock["now"] = initial_time + 61
                raise StopAsyncIteration from None

    chunks = EOFAdvance()
    with monkeypatch.context() as patch:
        patch.setattr(loop, "time", controlled_time)
        token = operation_clock.set(True)
        try:
            body = await open_ingress(chunks, media.MediaUploadPut)
            assert body.verified is False
            with pytest.raises(MediaWireError) as failure:
                await _consume(body)
            _assert_closed_error(failure, "unavailable")
            assert chunks.eof_reported is True
            assert body.verified is False
        finally:
            operation_clock.reset(token)
