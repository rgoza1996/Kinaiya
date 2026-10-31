"""Synthetic transports only: no credentials, model API or capture."""

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_path = Path(__file__).resolve().parents[2] / "scripts/qualify_meta_multimodal.py"
_spec = importlib.util.spec_from_file_location("tested_meta_qualification", _path)
qual = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qual)


@pytest.fixture
def directory(tmp_path):
    for name in ("image.png", "speech.wav", "beeps.wav"):
        (tmp_path / name).write_bytes(b"synthetic mock media")
    return tmp_path


def test_default_preparation_never_contacts_api_or_reads_credentials(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["qualify", "--directory", str(tmp_path)])
    monkeypatch.setattr(qual, "create_fixtures", lambda *a, **k: calls.append("fixtures"))
    monkeypatch.setattr(qual, "run", lambda *a, **k: pytest.fail("API run must be explicit"))
    qual.main()
    assert calls == ["fixtures"]


def test_budget_guard_precedes_paid_calls(directory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"limit_usd": 25})

    with pytest.raises(ValueError, match="at most"):
        qual.run(directory, ["voice-speech"], transport=httpx.MockTransport(handler))
    assert len(calls) == 1 and calls[0].url.path == "/budget/summary"
    assert "authorization" not in calls[0].headers


def test_refused_generation_is_not_retried_or_followed_off_proxy(directory):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 9790
        if request.url.path == "/budget/summary":
            return httpx.Response(200, json={"limit_usd": 10})
        return httpx.Response(302, headers={"Location": "https://example.com"}, json={})

    result = qual.run(
        directory, ["muse-image"], transport=httpx.MockTransport(handler), credential="synthetic"
    )
    assert len(calls) == 4  # initial/per-call admission and final numeric summary
    assert len([request for request in calls if request.method == "POST"]) == 1
    assert result["cases"][0]["http_status"] == 302
    assert "error" in result["cases"][0]
    assert not list(directory.glob("generated-image.*"))


def test_voice_and_spark_requests_keep_modes_and_headers_separate(directory):
    calls = []

    def handler(request):
        if request.url.path == "/budget/summary":
            return httpx.Response(200, json={"limit_usd": 10})
        calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic"
        if request.url.path == "/v1/asr/transcribe":
            assert "x-tbh-contributor-mode" not in request.headers
            assert b"PUSH_TO_TALK" in request.content and b"audioEncoding" in request.content
            return httpx.Response(
                200, json={"transcript": qual.SPEECH_TEXT, "audioDurationMs": 4320}
            )
        assert request.headers["x-tbh-contributor-mode"] == "true"
        payload = json.loads(request.content)
        assert payload["model"] == qual.SPARK and payload["store"] is False
        return httpx.Response(
            200,
            json={
                "model": qual.SPARK,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": qual.SPEECH_TEXT}],
                    }
                ],
            },
        )

    result = qual.run(
        directory,
        ["voice-speech", "spark-speech"],
        transport=httpx.MockTransport(handler),
        credential="synthetic",
    )
    assert len(calls) == 2 and all(row["expected_observed"] for row in result["cases"])
    assert result["physical_camera_tested"] is result["production_app_tested"] is False
    assert "synthetic" not in (directory / "meta-report.json").read_text().replace(
        '"synthetic_only"', ""
    )


def test_unknown_duplicate_cases_and_remote_image_results_refuse(directory):
    for cases in ([], ["unknown"], ["voice-speech", "voice-speech"]):
        with pytest.raises(ValueError):
            qual.run(directory, cases)
    with pytest.raises(ValueError, match="inline image"):
        qual.observe("muse-image", {"data": [{"url": "https://example.com/image"}]}, directory)
    assert not qual.observe("voice-speech", {"transcript": "spreadsheet three columns"}, directory)[
        "expected_observed"
    ]
    assert not qual.observe("voice-beeps", {"transcript": "", "audioDurationMs": None}, directory)[
        "expected_observed"
    ]
    with pytest.raises(ValueError, match="incomplete"):
        qual.observe("sam-image", {"status": "incomplete", "model": "sam-3.1"}, directory)
