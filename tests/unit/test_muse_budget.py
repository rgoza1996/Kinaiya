"""Budget boundaries use synthetic upstreams; these tests make no Meta calls."""

import base64
import copy
import http.client
import importlib.util
import io
import json
import socket
import threading
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def budget(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/muse_budget_proxy.py"
    spec = importlib.util.spec_from_file_location("tested_budget", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.LEDGER = tmp_path / "ledger.json"
    module.STATE = {
        "limit_usd": 25,
        "admission_limit_usd": 22,
        "billing_reported_usd": None,
        "estimated_usd": 0.0,
        "contributor_estimated_usd": 0.0,
        "admission_estimated_usd": 0.0,
        "reserved_usd": 0.0,
        "reported_input_tokens": 0,
        "reported_output_tokens": 0,
        "reported_cached_tokens": 0,
        "calls": [],
    }
    calls = []
    result = SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 20}, stream=False)

    class Upstream:
        def open(self, request, timeout):
            calls.append(request)
            if hasattr(result, "error"):
                raise result.error
            if hasattr(result, "opened"):
                result.opened.set()
                assert result.release.wait(timeout=5)
            if hasattr(result, "response"):
                return result.response
            payload = {"model": getattr(result, "model", module.MODEL), "usage": result.usage}
            if result.stream:
                raw = b"data: " + json.dumps({"response": payload}).encode() + b"\n\n"
            else:
                raw = json.dumps(payload).encode()
            if hasattr(result, "raw"):
                raw = result.raw

            class Response(io.BytesIO):
                def close(self):
                    super().close()
                    if getattr(result, "close_error", False):
                        raise OSError("synthetic close failure")

            response = Response(raw)
            response.status = 200
            response.headers = {
                "Content-Type": "text/event-stream" if result.stream else "application/json"
            }
            return response

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *a: Upstream())
    yield module, calls, result


@pytest.fixture
def gateway(budget):
    module, calls, result = budget
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
        yield module, calls, result, client
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def test_cache_discount_never_reduces_admission(budget):
    module, _, _ = budget
    normalized, costs = module.measured_usage(
        {"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 800}}
    )
    assert normalized["input_tokens_details"]["cached_tokens"] == 800
    assert costs["contributor"] == pytest.approx(0.0000416)
    assert costs["admission"] == pytest.approx(0.00024)
    assert costs["comparison"] == pytest.approx(0.001675)


def test_lower_owner_ceiling_preserves_prior_spend_and_uncertainty(budget):
    module, _, _ = budget
    module.audio_state(module.STATE)
    module.STATE.update(
        cache_aware_admission_estimated_usd=6,
        reserved_usd=3,
        audio_estimated_usd=0.5,
        audio_reserved_usd=0.4,
    )
    before = copy.deepcopy(module.STATE)
    assert module.lower_budget_limit(module.STATE, 10) == 10
    assert {k: v for k, v in module.STATE.items() if k != "limit_usd"} == {
        k: v for k, v in before.items() if k != "limit_usd"
    }
    assert module.admission_available(audio=0.1)
    assert not module.admission_available(text=0.2)
    assert module.lower_budget_limit(module.STATE, 25) == 10
    for invalid in (0, -1, 26, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            module.lower_budget_limit(module.STATE, invalid)
    assert module.STATE["limit_usd"] == 10


def media_body(*, generation):
    if generation:
        return {
            "model": "muse-image-1.0",
            "prompt": "One red square on white.",
            "n": 1,
            "response_format": "b64_json",
        }
    # Explicit synthetic PNG envelope: the upstream is mocked, not a decoder.
    raw = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (320).to_bytes(4, "big")
        + (180).to_bytes(4, "big")
        + b"\0" * 9
    )
    return {
        "model": "sam-3.1",
        "stream": False,
        "store": False,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "red square"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + base64.b64encode(raw).decode(),
                    },
                ],
            }
        ],
    }


@pytest.mark.parametrize("generation", [True, False])
def test_media_smokes_require_explicit_enable_and_preserve_combined_ceiling(gateway, generation):
    module, calls, result, client = gateway
    route = "/v1/images/generations" if generation else "/v1/responses"
    body = media_body(generation=generation)
    assert client.post(route, json=body).status_code in {403, 405}
    assert calls == []
    module.MEDIA_SMOKE = True
    module.lower_budget_limit(module.STATE, 10)
    result.raw = json.dumps(
        {"data": [{"b64_json": "synthetic"}]}
        if generation
        else {"status": "completed", "model": "sam-3.1", "output": []}
    ).encode()
    response = client.post(route, json=body, headers={"x-tbh-contributor-mode": "true"})
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].full_url == "https://api.meta.ai" + route
    assert not calls[0].has_header("X-tbh-contributor-mode")
    reserve = module.MEDIA_RESERVATIONS[body["model"]]
    assert module.STATE["media_estimated_usd"] == reserve
    assert module.STATE["media_reserved_usd"] == 0
    assert "red square" not in module.LEDGER.read_text()
    module.STATE["reserved_usd"] = 9.999
    assert client.post(route, json=body).status_code == 402
    assert len(calls) == 1


def test_malformed_media_and_unknown_results_never_refund(gateway):
    module, calls, result, client = gateway
    module.MEDIA_SMOKE = True
    body = media_body(generation=True)
    for extra in ({"n": 2}, {"n": True}, {"tools": []}, {"model": "unknown"}):
        assert client.post("/v1/images/generations", json={**body, **extra}).status_code == 400
    sam = media_body(generation=False)
    for part in (
        {"type": "input_image", "image_url": "https://example.com/image.png"},
        {"type": "input_file", "file_id": "file-arbitrary"},
        {"type": "input_image", "image_url": "data:image/png;base64,invalid"},
    ):
        invalid = copy.deepcopy(sam)
        invalid["input"][0]["content"][1] = part
        assert client.post("/v1/responses", json=invalid).status_code == 400
    assert calls == []
    result.raw = b'{"data":[]}'
    assert client.post("/v1/images/generations", json=body).status_code == 200
    assert module.STATE["media_reserved_usd"] == 0.02
    assert module.STATE["media_calls"][0]["state"] == "uncertain_reservation_retained"
    restored = copy.deepcopy(module.STATE)
    module.media_state(restored)
    assert restored["media_reserved_usd"] == 0.02
    assert restored["media_estimated_usd"] == 0


def test_budget_summary_is_bounded_numeric_and_omits_historical_calls(gateway):
    module, calls, _, client = gateway
    module.STATE["cache_aware_admission_estimated_usd"] = 2
    module.STATE["reserved_usd"] = 3
    module.STATE["calls"] = [{"private_label": "never expose this fixture"}]
    result = client.get("/budget/summary")
    assert result.status_code == 200
    assert result.json()["accounted_with_reservations_usd"] == 5
    assert result.json()["estimates_not_invoices"] is True
    assert len(result.content) < 1024
    assert "calls" not in result.json() and b"private_label" not in result.content
    assert calls == []
    module.STATE.pop("cache_aware_admission_estimated_usd")
    module.STATE["admission_estimated_usd"] = 4
    assert client.get("/budget/summary").json()["accounted_with_reservations_usd"] == 7


def public_wav(seconds=1, rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\0\0" * (seconds * rate))
    return output.getvalue()


def voice_parts(module, audio=None, **change):
    request = {"model": module.VOICE_MODEL, "mode": "PUSH_TO_TALK", "audioEncoding": "WAV"}
    request.update(change)
    return {
        "request": (None, json.dumps(request), "application/json"),
        "audio": ("recording.wav", public_wav() if audio is None else audio, "audio/wav"),
    }


def voice_result(**change):
    value = {
        "sessionId": "public-session",
        "transcript": "Synthetic public words.",
        "audioDurationMs": 1000,
        "turns": [],
    }
    value.update(change)
    return json.dumps(value).encode()


def test_exact_free_count_body_no_generation_reservation(gateway):
    module, calls, result, client = gateway
    result.raw = b'{"object":"response.input_tokens","input_tokens":200}'
    body = {
        "model": module.MODEL,
        "input": [{"role": "user", "content": "public count fixture"}],
        "stream": True,
        "max_output_tokens": 128,
        "reasoning": {"effort": "minimal"},
    }
    response = client.post("/v1/responses/input_tokens", json=body)
    assert response.status_code == 200
    assert json.loads(calls[0].data) == body
    assert calls[0].full_url == "https://api.meta.ai/v1/responses/input_tokens"
    assert module.STATE["calls"] == [] and module.STATE["reserved_usd"] == 0
    assert module.STATE["count_requests"] == 1
    assert "public count fixture" not in module.LEDGER.read_text()


@pytest.mark.parametrize("rate,seconds", [(16000, 1), (24000, 60)])
def test_audio_subledger_whole_processed_seconds_and_separate_headers(gateway, rate, seconds):
    module, calls, result, client = gateway
    result.raw = voice_result(audioDurationMs=seconds * 1000)
    response = client.post(
        "/v1/asr/transcribe",
        files=voice_parts(module, public_wav(seconds, rate)),
        headers={
            "authorization": "Bearer synthetic-cloud-only-key",
            "x-tbh-contributor-mode": "true",
        },
    )
    assert response.status_code == 200 and len(calls) == 1
    assert calls[0].full_url == "https://api.meta.ai/v1/asr/transcribe"
    assert not any(k.lower() == "x-tbh-contributor-mode" for k in calls[0].headers)
    row = module.STATE["audio_calls"][0]
    assert row["reservation_usd"] == pytest.approx(60 * 0.18 / 3600)
    assert row["processed_seconds_ceil"] == seconds
    assert row["state"] == "duration_reported"
    assert module.STATE["audio_estimated_usd"] == pytest.approx(seconds * 0.18 / 3600)
    assert module.STATE["audio_reserved_usd"] == 0 and module.STATE["calls"] == []
    saved = module.LEDGER.read_text()
    assert "Synthetic public words" not in saved and "synthetic-cloud-only-key" not in saved


@pytest.mark.parametrize("reported,charged", [(0, 2), (1001, 2), (5001, 6)])
def test_reported_duration_cannot_discount_uploaded_clip(gateway, reported, charged):
    module, _, result, client = gateway
    result.raw = voice_result(audioDurationMs=reported)
    assert (
        client.post("/v1/asr/transcribe", files=voice_parts(module, public_wav(2))).status_code
        == 200
    )
    assert module.STATE["audio_calls"][0]["processed_seconds_ceil"] == charged


@pytest.mark.parametrize(
    "raw",
    [
        b"partial",
        voice_result(audioDurationMs=60001),
        voice_result(audioDurationMs=True),
        b" " * (128 * 1024 + 1),
    ],
)
def test_audio_unknown_or_partial_retains_full_reservation(gateway, raw):
    module, calls, result, client = gateway
    result.raw = raw
    assert client.post("/v1/asr/transcribe", files=voice_parts(module)).status_code == 502
    assert len(calls) == 1 and module.STATE["audio_reserved_usd"] == pytest.approx(0.003)
    assert module.STATE["audio_estimated_usd"] == 0
    module.audio_state(module.STATE)
    assert module.STATE["audio_reserved_usd"] == pytest.approx(0.003)


def test_audio_timeout_retains_reservation_and_never_retries(gateway):
    module, calls, result, client = gateway
    result.error = TimeoutError("synthetic timeout")
    assert client.post("/v1/asr/transcribe", files=voice_parts(module)).status_code == 502
    assert len(calls) == 1 and module.STATE["audio_reserved_usd"] == pytest.approx(0.003)


def test_audio_close_failure_keeps_unknown_charge(gateway):
    module, calls, result, client = gateway
    result.raw, result.close_error = voice_result(), True
    assert client.post("/v1/asr/transcribe", files=voice_parts(module)).status_code == 502
    assert len(calls) == 1
    assert module.STATE["audio_reserved_usd"] == pytest.approx(0.003)
    assert module.STATE["audio_estimated_usd"] == 0


def real_http_response(body, framing):
    """Real stdlib framing over two owned local sockets, no foreign upstream."""
    reader, writer = socket.socketpair()
    try:
        writer.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + framing
            + b"Connection: close\r\n\r\n"
            + body
        )
        writer.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(reader)
        response.begin()
        return response
    finally:
        # The response's makefile retains the read descriptor until real close.
        reader.close()
        writer.close()


@pytest.mark.parametrize("framing", ["truncated", "duplicate", "invalid", "negative", "oversize"])
def test_real_http_response_cannot_discount_audio_before_complete_entity(gateway, framing):
    module, calls, result, client = gateway
    body = voice_result()
    if framing == "truncated":
        headers = f"Content-Length: {len(body) + 20}\r\n".encode()
    elif framing == "duplicate":
        headers = f"Content-Length: {len(body)}\r\nContent-Length: {len(body)}\r\n".encode()
    elif framing == "invalid":
        headers = b"Content-Length: invalid\r\n"
    elif framing == "negative":
        headers = b"Content-Length: -1\r\n"
    else:
        headers = b"Content-Length: 999999999\r\n"
    result.response = real_http_response(body, headers)
    response = client.post("/v1/asr/transcribe", files=voice_parts(module))
    assert response.status_code == 502
    assert len(calls) == 1 and result.response.isclosed()
    assert module.STATE["audio_reserved_usd"] == pytest.approx(0.003)
    assert module.STATE["audio_estimated_usd"] == 0


@pytest.mark.parametrize("framing", ["length", "close", "chunked"])
def test_complete_real_http_framing_settles_only_after_close(gateway, framing):
    module, _, result, client = gateway
    body = voice_result()
    if framing == "length":
        headers = f"Content-Length: {len(body)}\r\n".encode()
    elif framing == "close":
        headers = b""
    else:
        headers = b"Transfer-Encoding: chunked\r\n"
        body = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    result.response = real_http_response(body, headers)
    assert client.post("/v1/asr/transcribe", files=voice_parts(module)).status_code == 200
    assert result.response.isclosed()
    assert module.STATE["audio_reserved_usd"] == 0
    assert module.STATE["audio_estimated_usd"] == pytest.approx(0.00005)


@pytest.mark.parametrize("stage", ["eof", "close"])
def test_audio_reservation_stays_owned_until_real_eof_and_close(gateway, stage):
    module, _, result, client = gateway
    entered, release = threading.Event(), threading.Event()

    class HeldResponse(io.BytesIO):
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, amount):
            value = super().read(amount)
            if stage == "eof" and not value:
                entered.set()
                assert release.wait(timeout=3)
            return value

        def close(self):
            if stage == "close":
                entered.set()
                assert release.wait(timeout=3)
            super().close()

    result.response = HeldResponse(voice_result())
    responses = []
    thread = threading.Thread(
        target=lambda: responses.append(
            client.post("/v1/asr/transcribe", files=voice_parts(module))
        )
    )
    thread.start()
    try:
        assert entered.wait(timeout=2)
        assert module.STATE["audio_reserved_usd"] == pytest.approx(0.003)
        assert module.STATE["audio_estimated_usd"] == 0
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and responses[0].status_code == 200


def test_concurrent_audio_admission_reserves_before_upstream(gateway):
    module, calls, result, client = gateway
    module.STATE["audio_reserved_usd"] = 2.996
    result.raw, result.opened, result.release = voice_result(), threading.Event(), threading.Event()
    responses = []
    thread = threading.Thread(
        target=lambda: responses.append(
            client.post("/v1/asr/transcribe", files=voice_parts(module))
        )
    )
    thread.start()
    try:
        assert result.opened.wait(timeout=2)
        denied = client.post("/v1/asr/transcribe", files=voice_parts(module))
        assert denied.status_code == 402 and len(calls) == 1
        assert module.STATE["audio_reserved_usd"] == pytest.approx(2.999)
    finally:
        result.release.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and responses[0].status_code == 200


@pytest.mark.parametrize(
    "raw",
    [
        b'{"object":"response.input_tokens","input_tokens":true}',
        b'{"object":"response.input_tokens","input_tokens":1,"input_tokens":2}',
        b" " * 4097,
    ],
)
def test_auxiliary_count_rejects_invalid_response_without_generation(gateway, raw):
    module, calls, result, client = gateway
    result.raw = raw
    response = client.post(
        "/v1/responses/input_tokens", json={"model": module.MODEL, "max_output_tokens": 128}
    )
    assert response.status_code == 502 and len(calls) == 1
    assert module.STATE["count_requests"] == 1 and module.STATE["calls"] == []
    assert module.STATE["reserved_usd"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"model": "other"},
        {"mode": "STREAMING"},
        {"audioEncoding": "OPUS"},
        {"extra": "not allowed"},
    ],
)
def test_voice_model_mode_and_part_shape_are_exact(gateway, change):
    module, calls, _, client = gateway
    assert client.post("/v1/asr/transcribe", files=voice_parts(module, **change)).status_code == 400
    assert not calls and not module.STATE.get("audio_calls")


@pytest.mark.parametrize(
    "audio", [b"not WAV", public_wav(61), public_wav(1, 8000), public_wav() + b"extra"]
)
def test_voice_actual_format_duration_and_trailing_bytes_before_reservation(gateway, audio):
    module, calls, _, client = gateway
    assert client.post("/v1/asr/transcribe", files=voice_parts(module, audio)).status_code == 400
    assert not calls and not module.STATE.get("audio_calls")


def noncanonical_wav(kind):
    raw = bytearray(public_wav())
    if kind == "second_data_90_seconds":
        second = b"\0\0" * (90 * 16000)
        raw.extend(b"data" + len(second).to_bytes(4, "little") + second)
    elif kind == "junk_after_data":
        raw.extend(b"JUNK\x04\x00\x00\x00test")
    elif kind == "junk_before_data":
        raw[36:36] = b"JUNK\x04\x00\x00\x00test"
    elif kind == "duplicate_fmt":
        raw[36:36] = raw[12:36]
    elif kind == "short_data_size":
        raw[40:44] = (2).to_bytes(4, "little")
    elif kind == "wrong_byte_rate":
        raw[28:32] = (1).to_bytes(4, "little")
    elif kind == "wrong_block_alignment":
        raw[32:34] = (1).to_bytes(2, "little")
    else:
        raise AssertionError(kind)
    raw[4:8] = (len(raw) - 8).to_bytes(4, "little")
    return bytes(raw)


@pytest.mark.parametrize(
    "kind",
    [
        "second_data_90_seconds",
        "junk_after_data",
        "junk_before_data",
        "duplicate_fmt",
        "short_data_size",
        "wrong_byte_rate",
        "wrong_block_alignment",
    ],
)
def test_noncanonical_riff_cannot_hide_unreserved_audio(budget, kind):
    module, _, _ = budget
    audio = noncanonical_wav(kind)
    request = httpx.Request("POST", "http://synthetic", files=voice_parts(module, audio))
    body = request.read()
    assert len(body) < module.MAX_VOICE_BODY
    with pytest.raises(ValueError):
        module.voice_request(body, request.headers["content-type"])


def test_second_data_chunk_is_refused_before_any_charge_or_export(gateway):
    module, calls, _, client = gateway
    response = client.post(
        "/v1/asr/transcribe", files=voice_parts(module, noncanonical_wav("second_data_90_seconds"))
    )
    assert response.status_code == 400
    assert not calls and not module.STATE.get("audio_calls")
    assert module.STATE.get("audio_reserved_usd", 0) == 0


@pytest.mark.parametrize("kind", ["audio", "combined", "text_combined"])
def test_shared_admission_includes_both_subledgers(gateway, kind):
    module, calls, _, client = gateway
    if kind == "audio":
        module.STATE["audio_reserved_usd"] = 2.999
    elif kind == "combined":
        module.STATE.update(billing_reported_usd=24.999)
    else:
        module.STATE.update(audio_reserved_usd=3, reserved_usd=21.9)
    if kind == "text_combined":
        response = client.post("/v1/responses", json={"model": module.MODEL})
    else:
        response = client.post("/v1/asr/transcribe", files=voice_parts(module))
    assert response.status_code == 402 and not calls


def test_audio_migration_keeps_all_unknowns_and_prior_floors(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state.update(audio_reserved_usd=0.3, audio_estimated_usd=0.4)
    state["audio_calls"] = [{"state": "in_flight", "reservation_usd": 0.003}]
    module.audio_state(state)
    assert state["audio_reserved_usd"] == 0.3 and state["audio_estimated_usd"] == 0.4
    assert module.STATE["calls"] == []
    before = copy.deepcopy(state)
    module.audio_state(state)
    assert before == state


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"total_tokens": 10},
        {"input_tokens": -1, "output_tokens": 2},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": "100", "output_tokens": 2},
        {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 11}},
        {"input_tokens": 10, "prompt_tokens": 11, "output_tokens": 2},
        {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": []},
        {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": None},
        {"input_tokens": 10, "output_tokens": 2, "prompt_tokens_details": {"cached_tokens": False}},
        {
            "input_tokens": 10,
            "output_tokens": 2,
            "input_tokens_details": {"cached_tokens": 1},
            "prompt_tokens_details": {"cached_tokens": 2},
        },
    ],
)
def test_incomplete_or_invalid_usage_is_uncertain(budget, usage):
    module, _, _ = budget
    with pytest.raises(ValueError):
        module.measured_usage(usage)


def test_migration_preserves_unknown_reserve_prior_charges_and_legacy_aliases(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state.update(reserved_usd=1.33, admission_estimated_usd=7.0, estimated_usd=90.0)
    state["calls"] = [
        {
            "state": "http_404_reservation_retained",
            "reservation_usd": 1.33,
        },
        {
            "state": "usage_reported",
            "reservation_usd": 1.3,
            "reported_usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1000},
        },
    ]
    module.reconcile(state)
    assert state["reserved_usd"] == 1.33
    assert state["admission_estimated_usd"] == 7.0
    assert state["ordinary_spark_comparison_usd"] == 90.0
    assert state["calls"][0]["state"] == "http_404_reservation_retained"
    assert state["calls"][1]["admission_estimated_usd"] == pytest.approx(0.2004)
    assert module.reconcile(copy.deepcopy(state)) == state


def test_bad_settled_usage_restores_reservation_once(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state["reserved_usd"] = 0.75
    state["calls"] = [
        {
            "state": "usage_reported",
            "reservation_usd": 0.25,
            "reported_usage": {"total_tokens": 100},
        }
    ]
    module.reconcile(state)
    assert state["reserved_usd"] == 1.0
    module.reconcile(state)
    assert state["reserved_usd"] == 1.0


def test_second_writer_and_symlink_lock_refuse_before_ledger_creation(budget, tmp_path):
    module, _, _ = budget
    with module.ledger_owner(module.LEDGER):
        with pytest.raises(BlockingIOError):
            with module.ledger_owner(module.LEDGER):
                pytest.fail("Two budget owners")
    assert not module.LEDGER.exists()
    other = tmp_path / "other"
    (tmp_path / "other.lock").symlink_to(tmp_path / "ledger.json.lock")
    with pytest.raises(OSError):
        with module.ledger_owner(other):
            pytest.fail("Symlink lock accepted")


@pytest.mark.parametrize("stream", [False, True])
def test_real_gateway_settles_valid_usage_without_persisting_prompt(gateway, stream):
    module, calls, result, client = gateway
    result.stream = stream
    result.usage.update(provider_debug={"prompt": "synthetic provider debug marker"})
    response = client.post(
        "/v1/responses",
        json={
            "model": module.MODEL,
            "input": "synthetic request prompt marker",
            "max_output_tokens": 999999,
        },
    )
    assert response.status_code == 200 and len(calls) == 1
    assert json.loads(calls[0].data)["max_output_tokens"] == module.OUTPUT_CAP
    assert module.STATE["calls"][0]["state"] == "usage_reported"
    assert module.STATE["reserved_usd"] == pytest.approx(0)
    assert module.STATE["reported_input_tokens"] == 100
    saved = module.LEDGER.read_text()
    assert "synthetic request prompt marker" not in saved
    assert "synthetic provider debug marker" not in saved
    assert module.LEDGER.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "usage", [None, {"total_tokens": 100}, {"input_tokens": -1, "output_tokens": 0}]
)
def test_gateway_keeps_full_reservation_for_uncertain_usage(gateway, usage):
    module, _, result, client = gateway
    result.usage = usage
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 200
    assert module.STATE["reserved_usd"] > 0.2
    assert module.STATE["admission_estimated_usd"] == 0
    assert module.STATE["calls"][0]["state"] == "usage_missing_reservation_retained"


@pytest.mark.parametrize("reported", [False, True])
def test_admission_refuses_before_any_upstream_call(gateway, reported):
    module, calls, _, client = gateway
    if reported:
        module.STATE["billing_reported_usd"] = 22.0
    else:
        module.STATE["admission_estimated_usd"] = 21.9
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 402
    assert calls == [] and module.STATE["calls"] == []


def test_unauthorized_model_and_invalid_output_never_reach_upstream(gateway):
    module, calls, _, client = gateway
    assert client.post("/v1/responses", json={"model": "muse-spark-1.3"}).status_code == 403
    assert (
        client.post(
            "/v1/responses", json={"model": module.MODEL, "max_output_tokens": -1}
        ).status_code
        == 400
    )
    assert calls == []


def test_unmetered_routes_never_reach_upstream(gateway):
    _, calls, _, client = gateway
    assert client.get("/v1/responses").status_code == 405
    assert (
        client.post("/v1/batches", json={"model": "muse-spark-1.3-contributor"}).status_code == 405
    )
    assert calls == []


def settled_contributor(module, *, cached=950_000, state="usage_reported"):
    return {
        "model": module.MODEL,
        "resolved_model": module.MODEL,
        "state": state,
        "reservation_usd": 0.2278138,
        "reported_usage": {
            "input_tokens": 1_000_000,
            "output_tokens": 20_000,
            "input_tokens_details": {"cached_tokens": cached},
        },
    }


def test_verified_settled_cache_has_separate_enforcement_basis(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state["calls"] = [settled_contributor(module)]
    module.reconcile(state)
    assert state["admission_estimated_usd"] == pytest.approx(0.208)
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(0.0218)
    assert state["calls"][0]["reported_usage"]["input_tokens_details"]["cached_tokens"] == 950_000
    assert module.reconcile(copy.deepcopy(state)) == state


@pytest.mark.parametrize("cached", [0, 1_000_000])
def test_cache_misses_and_full_hits_use_reported_counts(budget, cached):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    row = settled_contributor(module, cached=cached)
    state["calls"] = [row]
    module.reconcile(state)
    expected = 2 * ((1_000_000 - cached) * 0.10 + cached * 0.002 + 20_000 * 0.20) / 1_000_000
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(expected)
    assert state["admission_estimated_usd"] == pytest.approx(0.208)


@pytest.mark.parametrize("key", ["model", "resolved_model"])
@pytest.mark.parametrize("identity", [None, "muse-spark-1.3", "unverified-alias"])
def test_cache_credit_requires_both_contributor_identities(budget, key, identity):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    row = settled_contributor(module)
    if identity is None:
        row.pop(key)
    else:
        row[key] = identity
    state["calls"] = [row]
    module.reconcile(state)
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(0.208)


def test_cache_credit_preserves_legacy_premiums_unknown_floor_and_reservations(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    row = settled_contributor(module)
    row["admission_estimated_usd"] = 0.508  # unexplained per-record premium0.30
    state.update(admission_estimated_usd=0.758, reserved_usd=0.75, billing_reported_usd=9.0)
    unknown = {"state": "http_401_reservation_retained", "reservation_usd": 0.75}
    state["calls"] = [row, unknown]
    module.reconcile(state)
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(0.0218 + 0.30 + 0.25)
    assert state["admission_estimated_usd"] == 0.758
    assert state["reserved_usd"] == 0.75
    assert state["billing_reported_usd"] == 9.0
    assert unknown == {"state": "http_401_reservation_retained", "reservation_usd": 0.75}
    assert module.reconcile(copy.deepcopy(state)) == state


@pytest.mark.parametrize("phase", ["in_flight", "usage_missing_reservation_retained"])
def test_unsettled_usage_never_receives_cache_credit(budget, phase):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state["calls"] = [settled_contributor(module, state=phase)]
    module.reconcile(state)
    assert state["cache_aware_admission_estimated_usd"] == pytest.approx(0.208)
    assert state["reserved_usd"] == pytest.approx(0.2278138)


def test_cache_basis_never_refunds_existing_new_policy_charge(budget):
    module, _, _ = budget
    state = copy.deepcopy(module.STATE)
    state["calls"] = [settled_contributor(module)]
    state["cache_aware_admission_estimated_usd"] = 3.0
    module.reconcile(state)
    assert state["cache_aware_admission_estimated_usd"] == 3.0


def test_gateway_cache_settlement_and_billing_floor(gateway):
    module, calls, result, client = gateway
    result.usage = settled_contributor(module)["reported_usage"]
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 200
    assert module.STATE["admission_estimated_usd"] == pytest.approx(0.208)
    assert module.STATE["cache_aware_admission_estimated_usd"] == pytest.approx(0.0218)
    assert module.STATE["reserved_usd"] == pytest.approx(0)
    module.STATE["billing_reported_usd"] = 22.0
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 402
    assert len(calls) == 1


def test_gateway_refuses_cache_discount_for_unexpected_resolved_model(gateway):
    module, _, result, client = gateway
    result.usage = settled_contributor(module)["reported_usage"]
    result.model = "muse-spark-1.3"
    assert client.post("/v1/responses", json={"model": module.MODEL}).status_code == 200
    assert module.STATE["cache_aware_admission_estimated_usd"] == pytest.approx(0.208)


@pytest.mark.parametrize("tools", [[{"type": "web_search"}], [{"type": "mcp"}], {}, [None]])
def test_hosted_or_ambiguous_tools_are_not_silently_unmetered(gateway, tools):
    module, calls, _, client = gateway
    assert (
        client.post("/v1/responses", json={"model": module.MODEL, "tools": tools}).status_code
        == 403
    )
    assert calls == [] and module.STATE["calls"] == []


def test_local_function_tool_schema_remains_usable(gateway):
    module, calls, _, client = gateway
    response = client.post(
        "/v1/responses",
        json={"model": module.MODEL, "tools": [{"type": "function", "name": "public_fixture"}]},
    )
    assert response.status_code == 200 and len(calls) == 1


def test_simultaneous_requests_reserve_before_dispatch(gateway):
    from concurrent.futures import ThreadPoolExecutor

    module, calls, result, client = gateway
    module.STATE["cache_aware_admission_estimated_usd"] = 21.65
    module.STATE["admission_estimated_usd"] = 21.65
    result.opened, result.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/v1/responses", json={"model": module.MODEL})
        try:
            assert result.opened.wait(timeout=3)
            assert module.STATE["reserved_usd"] > 0.22
            second = client.post("/v1/responses", json={"model": module.MODEL})
            assert second.status_code == 402 and len(calls) == 1
        finally:
            result.release.set()
        assert first.result(timeout=3).status_code == 200


def test_genuine_muse_namespace_shape_accepts_its_local_functions(gateway):
    module, calls, _, client = gateway
    # Structural shape observed from the installed CLI against an isolated
    # synthetic400 endpoint. Names/descriptions/schemas here are fixtures.
    functions = [
        {"type": "function", "name": f"fixture_{i}", "parameters": {"type": "object"}}
        for i in range(19)
    ]
    tools = [{"type": "namespace", "name": "local", "tools": functions}]
    response = client.post("/v1/responses", json={"model": module.MODEL, "tools": tools})
    assert response.status_code == 200 and len(calls) == 1
    assert json.loads(calls[0].data)["tools"] == tools
    assert module.STATE["calls"][0]["state"] == "usage_reported"


@pytest.mark.parametrize("kind", ["web_search", "mcp", "custom", "unknown", None])
@pytest.mark.parametrize("depth", [1, 3])
def test_hosted_or_unknown_nested_tools_never_reach_upstream(gateway, kind, depth):
    module, calls, _, client = gateway
    tools = [{"type": kind}]
    for _ in range(depth):
        tools = [{"type": "namespace", "name": "local", "tools": tools}]
    response = client.post("/v1/responses", json={"model": module.MODEL, "tools": tools})
    assert response.status_code == 403
    assert calls == [] and module.STATE["calls"] == []


@pytest.mark.parametrize("children", [None, {}, "function", [None]])
def test_malformed_namespace_is_not_a_local_function_group(gateway, children):
    module, calls, _, client = gateway
    response = client.post(
        "/v1/responses",
        json={"model": module.MODEL, "tools": [{"type": "namespace", "tools": children}]},
    )
    assert response.status_code == 403 and calls == []


def test_nested_function_groups_are_bounded_without_reading_parameter_schemas(budget):
    module, _, _ = budget
    # An enum/schema value named web_search is data, not a hosted tool.
    leaf = [{"type": "function", "parameters": {"enum": [{"type": "web_search"}]}}]
    tools = leaf
    for _ in range(module.MAX_TOOL_DEPTH):
        tools = [{"type": "namespace", "tools": tools}]
    assert module.local_function_tools(tools)
    assert not module.local_function_tools([{"type": "namespace", "tools": tools}])
    assert module.local_function_tools(leaf * module.MAX_TOOL_NODES)
    assert not module.local_function_tools(leaf * (module.MAX_TOOL_NODES + 1))


@pytest.mark.parametrize("path", ["/v1/models", "/muse-code/models"])
def test_exact_read_only_catalog_routes_preserve_even_exhausted_budget(gateway, path):
    module, calls, _, client = gateway
    module.STATE["billing_reported_usd"] = 25.0
    before = copy.deepcopy(module.STATE)
    assert client.get(path).status_code == 200
    assert len(calls) == 1
    assert calls[0].get_method() == "GET"
    assert calls[0].full_url == "https://api.meta.ai" + path
    assert module.STATE == before and not module.LEDGER.exists()


@pytest.mark.parametrize(
    "path",
    ["/muse-code/models?x=1", "/muse-code/models/", "/muse-code/models/other", "/muse-code/usage"],
)
def test_catalog_allowlist_does_not_expand_to_other_gets_or_posts(gateway, path):
    module, calls, _, client = gateway
    assert client.get(path).status_code == 405
    assert client.post(path, json={"model": module.MODEL}).status_code == 405
    assert client.post("/muse-code/models", json={"model": module.MODEL}).status_code == 405
    assert calls == [] and module.STATE["calls"] == []


def test_rejected_post_body_cannot_corrupt_next_metered_request(gateway):
    module, calls, _, client = gateway
    rejected = client.post("/muse-code/models", content=b"unconsumed synthetic body")
    assert rejected.status_code == 405 and rejected.headers["Connection"] == "close"
    accepted = client.post("/v1/responses", json={"model": module.MODEL})
    assert accepted.status_code == 200 and len(calls) == 1
    assert len(module.STATE["calls"]) == 1
    assert module.STATE["calls"][0]["state"] == "usage_reported"
