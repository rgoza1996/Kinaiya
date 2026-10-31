"""Explicit synthetic Meta smoke tests through the retained $10 gateway only.

Default invocation prepares fixtures without reading credentials or making API
requests. No camera, user media, model loading or automatic retry is supported.
"""

import argparse
import base64
import hashlib
import json
import time
from pathlib import Path

import httpx

from scripts.qualify_local_multimodal import (
    SPEECH_TEXT,
    create_fixtures,
    decode,
    encoded,
    media_bytes,
    transcript_words,
)

PROXY = "http://127.0.0.1:9790"
SPARK = "muse-spark-1.3-contributor"
VOICE = "muse-voice-transcribe-1.0"
CASE_IDS = (
    "spark-image",
    "spark-speech",
    "spark-image-speech",
    "voice-speech",
    "voice-beeps",
    "sam-image",
    "muse-image",
)


def specs(directory):
    image = {
        "type": "input_image",
        "image_url": "data:image/png;base64," + encoded(directory / "image.png"),
    }
    speech = {
        "type": "input_audio",
        "input_audio": {"data": encoded(directory / "speech.wav"), "format": "wav"},
    }
    result = {}
    for name, media, question in [
        ("spark-image", [image], "What color is the square? Answer with one color."),
        ("spark-speech", [speech], "Transcribe the spoken words."),
        (
            "spark-image-speech",
            [image, speech],
            "What task does the speaker request, and what color is the square in the image?",
        ),
    ]:
        result[name] = {
            "path": "/v1/responses",
            "body": {
                "model": SPARK,
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": question}, *media],
                    }
                ],
                "max_output_tokens": 1024,
                "reasoning": {"effort": "minimal"},
                "store": False,
                "stream": False,
                "tools": [],
            },
        }
    for name, file in [("voice-speech", "speech.wav"), ("voice-beeps", "beeps.wav")]:
        result[name] = {
            "path": "/v1/asr/transcribe",
            "file": file,
            "body": {
                "model": VOICE,
                "mode": "PUSH_TO_TALK",
                "audioEncoding": "WAV",
            },
        }
    result["sam-image"] = {
        "path": "/v1/responses",
        "body": {
            "model": "sam-3.1",
            "stream": False,
            "store": False,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "red square"},
                        image,
                    ],
                }
            ],
        },
    }
    result["muse-image"] = {
        "path": "/v1/images/generations",
        "body": {
            "model": "muse-image-1.0",
            "prompt": "One simple solid red square centered on a plain white background. Flat graphic, no text, no other objects.",
            "n": 1,
            "response_format": "b64_json",
        },
    }
    return result


def exchange(client, method, path, **kwargs):
    limit = 20 * 1024 * 1024 if path == "/v1/images/generations" else 512 * 1024
    with client.stream(method, PROXY + path, **kwargs) as response:
        if response.headers.get("content-encoding", "identity") != "identity":
            raise ValueError("Compressed qualification response refused")
        raw = bytearray()
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > limit:
                raise ValueError("Qualification response exceeds bound")
            raw.extend(chunk)
        return response.status_code, decode(bytes(raw)), hashlib.sha256(raw).hexdigest()


def budget_check(client):
    status, budget, _ = exchange(client, "GET", "/budget/summary")
    limit = budget.get("limit_usd")
    if status != 200 or type(limit) not in {int, float} or not 0 < limit <= 10:
        raise ValueError("A retained combined gateway cap of at most $10 is required")
    return budget


def observe(name, payload, directory):
    if name.startswith("voice-"):
        text = payload.get("transcript")
        duration = payload.get("audioDurationMs")
        expected = SPEECH_TEXT if name == "voice-speech" else ""
        return {
            "transcript": text,
            "audioDurationMs": payload.get("audioDurationMs"),
            "expected_observed": type(duration) is int
            and 1 <= duration <= 60000
            and type(text) is str
            and transcript_words(text) == transcript_words(expected),
        }
    if name.startswith("spark-"):
        text = "\n".join(
            part.get("text", "")
            for item in payload.get("output", [])
            if type(item) is dict and item.get("type") == "message"
            for part in item.get("content", [])
            if type(part) is dict and part.get("type") == "output_text"
        )
        if (
            not text
            or len(text) > 8000
            or payload.get("status") != "completed"
            or payload.get("model") != SPARK
        ):
            return {"expected_observed": False}
        expected = (
            transcript_words(text) == transcript_words(SPEECH_TEXT)
            if name == "spark-speech"
            else "red" in text.lower()
        )
        if name == "spark-image-speech":
            expected = expected and "spreadsheet" in text.lower() and "columns" in text.lower()
        return {
            "text": text,
            "expected_observed": expected,
            "check_scope": "required facts only; inspect additional claims separately",
        }
    if name == "sam-image":
        if payload.get("status") != "completed" or payload.get("model") != "sam-3.1":
            raise ValueError("SAM result is incomplete or has an unconfirmed model")
        text = "".join(
            part.get("text", "")
            for item in payload.get("output", [])
            if type(item) is dict
            for part in item.get("content", [])
            if type(part) is dict and part.get("type") == "output_text"
        )
        return {
            "terminal_completed": payload.get("status") == "completed",
            "output_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "output_text_bytes": len(text.encode()),
            "mask_decode_tested": False,
        }
    rows = payload.get("data")
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        raise ValueError("Exactly one generated image result is required")
    value = rows[0].get("b64_json")
    if type(value) is not str or len(value) > 16 * 1024 * 1024:
        raise ValueError("Bounded inline image required; remote result URLs are not fetched")
    raw = base64.b64decode(value, validate=True)
    suffix = (
        "png"
        if raw.startswith(b"\x89PNG\r\n\x1a\n")
        else "webp"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
        else None
    )
    if suffix is None:
        raise ValueError("Unrecognized generated image encoding")
    path = directory / ("generated-image." + suffix)
    path.write_bytes(raw)
    return {
        "image_file": path.name,
        "image_bytes": len(raw),
        "image_sha256": hashlib.sha256(raw).hexdigest(),
        "visual_quality_tested": False,
    }


def run(directory, selected, *, transport=None, credential=None):
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(CASE_IDS):
        raise ValueError("Select known unique synthetic cases")
    planned = specs(directory)
    report = {
        "synthetic_only": True,
        "physical_camera_tested": False,
        "production_app_tested": False,
        "provider": "meta",
        "cases": [],
    }
    with httpx.Client(
        trust_env=False,
        follow_redirects=False,
        timeout=150,
        transport=transport,
        headers={"Accept-Encoding": "identity"},
    ) as client:
        report["budget_before"] = budget_check(client)
        if credential is None:
            from packages.providers.text import _meta_credential

            credential = _meta_credential()
        client.headers["Authorization"] = "Bearer " + credential
        for name in selected:
            budget_check(client)
            spec = planned[name]
            row = {"id": name, "model": spec["body"]["model"]}
            started = time.monotonic()
            try:
                if "file" in spec:
                    audio = media_bytes(directory / spec["file"])
                    row["audio_sha256"] = hashlib.sha256(audio).hexdigest()
                    kwargs = {
                        "files": {
                            "request": (None, json.dumps(spec["body"]), "application/json"),
                            "audio": (spec["file"], audio, "audio/wav"),
                        }
                    }
                else:
                    body = json.dumps(spec["body"], separators=(",", ":"), allow_nan=False).encode()
                    row["request_sha256"] = hashlib.sha256(body).hexdigest()
                    kwargs = {
                        "content": body,
                        "headers": {
                            "Content-Type": "application/json",
                            **(
                                {"x-tbh-contributor-mode": "true"}
                                if name.startswith("spark-")
                                else {}
                            ),
                        },
                    }
                status, payload, digest = exchange(client, "POST", spec["path"], **kwargs)
                row.update(http_status=status, response_sha256=digest, usage=payload.get("usage"))
                if status == 200:
                    row.update(observe(name, payload, directory))
                else:
                    row["error"] = "Gateway or provider refused; no retry"
            except (ValueError, TypeError, KeyError, httpx.HTTPError) as exc:
                row["error"] = type(exc).__name__
            row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
            report["cases"].append(row)
            (directory / "meta-report.json").write_text(json.dumps(report, indent=2))
        report["budget_after"] = budget_check(client)
        (directory / "meta-report.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--run-meta", action="store_true")
    parser.add_argument("--cases", nargs="+", choices=CASE_IDS, default=["voice-speech"])
    args = parser.parse_args()
    create_fixtures(args.directory, speech=True)
    if not args.run_meta:
        print("Synthetic fixtures ready; no credential read or API request.")
        return
    report = run(args.directory, args.cases)
    for row in report["cases"]:
        print(json.dumps(row))
    if any("error" in row or row.get("expected_observed") is False for row in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
