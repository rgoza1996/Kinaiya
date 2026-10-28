"""The opt-in screen refuses corrupt/reordered audio without running inference."""

import base64
import wave

import pytest

from scripts.live_slice import write_synthetic_wave
from scripts.probe_retained_voice import decode_frame, summarize


@pytest.mark.parametrize(
    "change",
    [
        {"chunk_index": 2},
        {"chunk_index": False},
        {"samples": True},
        {"samples": 44101},
        {"sample_rate": 24000},
        {"audio": "not base64!"},
        {"audio": base64.b64encode(b"x").decode()},
    ],
)
def test_probe_rejects_corrupt_pcm_before_latency_summary(change):
    row = {
        "type": "audio",
        "chunk_index": 0,
        "samples": 1,
        "sample_rate": 44100,
        "audio": base64.b64encode(b"\x01\x00").decode(),
    }
    assert decode_frame(row, 0, 44100) == (b"\x01\x00", 44100)
    with pytest.raises(ValueError):
        decode_frame({**row, **change}, 0, 44100)


def test_small_screen_reports_counts_and_extremes_without_percentile_claim():
    rows = [
        {
            key: value
            for key in (
                "request_to_first_decoded_pcm_seconds",
                "request_to_complete_seconds",
                "rtf_including_http",
            )
        }
        for value in (1, 2, 8)
    ]
    result = summarize(rows)
    assert result["samples"] == 3 and result["percentile_claim"] is None
    assert result["statistics"]["request_to_first_decoded_pcm_seconds"] == {
        "minimum": 1,
        "median": 2,
        "maximum": 8,
    }


@pytest.mark.parametrize("rate", [24000, 44100])
def test_voice_to_task_fixture_preserves_backend_sample_rate(tmp_path, rate):
    path = tmp_path / "synthetic.wav"
    pcm = b"\x01\x00" * 10
    write_synthetic_wave(
        path, [{"sample_rate": rate, "samples": 10, "audio": base64.b64encode(pcm).decode()}]
    )
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == rate
        assert wav.getnframes() == 10
        assert wav.readframes(10) == pcm
