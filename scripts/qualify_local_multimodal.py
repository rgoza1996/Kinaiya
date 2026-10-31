"""Opt-in synthetic local modality smoke checks; no camera or cloud.

Requires explicitly loaded aliases. This tool never loads/unloads a model,
starts capture, grants permissions or routes work. A smoke result is not a
latency/quality benchmark or an integrated Kinaiya conversation qualification.
"""

import argparse
import base64
import copy
import hashlib
import json
import math
import re
import struct
import subprocess
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_RESPONSE_BYTES = 512 * 1024
SPEECH_TEXT = "Please make a spreadsheet with three columns. The red square moves to the right."


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("Non-finite JSON number")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds parser limits") from exc
    if type(value) is not dict:
        raise ValueError("Expected a JSON object")
    return value


def request(client, method, url, body=None):
    with client.stream(
        method,
        url,
        content=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
    ) as response:
        if response.headers.get("content-encoding", "identity") != "identity":
            raise ValueError("Compressed qualification response refused")
        raw = bytearray()
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("Response exceeds bound")
            raw.extend(chunk)
        return response.status_code, decode(bytes(raw))


def local_root(value):
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port is not None
        and not 1 <= parsed.port <= 65535
    ):
        raise ValueError("Qualification requires an explicit loopback server root")
    return value.rstrip("/")


def create_fixtures(directory, *, speech=False):
    from PIL import Image, ImageDraw

    directory.mkdir(parents=True, exist_ok=True)
    frames = directory / "frames"
    frames.mkdir(exist_ok=True)
    for index in range(12):
        image = Image.new("RGB", (320, 180), "white")
        draw = ImageDraw.Draw(image)
        x = 15 + index * 12
        draw.rectangle((x, 65, x + 50, 115), fill="red")
        draw.ellipse((245, 70, 280, 105), fill="blue")
        image.save(frames / f"{index:02}.png")
    (directory / "image.png").write_bytes((frames / "00.png").read_bytes())
    rate, samples = 16000, []
    for index in range(3 * rate):
        seconds = index / rate
        active = any(start <= seconds < start + 0.2 for start in (0.25, 1.25, 2.25))
        samples.append(round(10000 * math.sin(2 * math.pi * 880 * seconds)) if active else 0)
    with wave.open(str(directory / "beeps.wav"), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    # Fixed local fixture inputs/outputs only; no URLs, devices or shell expansion.
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            "4",
            "-i",
            str(frames / "%02d.png"),
            "-i",
            str(directory / "beeps.wav"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(directory / "clip.mp4"),
        ],
        check=True,
        timeout=30,
    )
    if speech:
        (directory / "speech.txt").write_text(SPEECH_TEXT)
        temporary = directory / "flite-speech.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"flite=textfile={directory / 'speech.txt'}:voice=slt",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(temporary),
            ],
            check=True,
            timeout=30,
        )
        # Normalize to the same canonical 44-byte PCM WAV used by the native
        # buffered voice admission, without ffmpeg's optional metadata chunks.
        with wave.open(str(temporary), "rb") as source:
            params, pcm = source.getparams(), source.readframes(source.getnframes())
        with wave.open(str(directory / "speech.wav"), "wb") as target:
            target.setparams(params)
            target.writeframes(pcm)
        temporary.unlink()
    manifest = {
        "fixture_version": 3,
        "synthetic": True,
        "duration_seconds": 3,
        "audio_beeps": 3,
        "visual": "red square moves left to right; stationary blue circle",
        "files": {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ["image.png", "beeps.wav", "clip.mp4", "frames/00.png", "frames/11.png"]
        },
    }
    if speech:
        manifest["speech_text"] = SPEECH_TEXT
        manifest["files"]["speech.wav"] = hashlib.sha256(
            (directory / "speech.wav").read_bytes()
        ).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def encoded(path):
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Synthetic media exceeds the qualification bound")
    return base64.b64encode(raw).decode("ascii")


def cases(directory, converse, decisions, *, include_speech=False, prompt_profile="generic"):
    if prompt_profile not in {"generic", "lfm3", "gemma4"}:
        raise ValueError("Unknown documented prompt profile")
    image = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + encoded(directory / "image.png")},
    }
    audio = {
        "type": "input_audio",
        "input_audio": {"data": encoded(directory / "beeps.wav"), "format": "wav"},
    }
    video = {"type": "input_video", "input_video": {"data": encoded(directory / "clip.mp4")}}
    last_frame = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + encoded(directory / "frames/11.png")},
    }

    def chat(case_id, media, question, expected):
        text = {"type": "text", "text": question}
        content = (
            [
                *[p for p in media if p["type"] != "input_audio"],
                text,
                *[p for p in media if p["type"] == "input_audio"],
            ]
            if prompt_profile == "gemma4"
            else [*media, text]
        )
        return {
            "id": case_id,
            "path": "/v1/chat/completions",
            "expected": expected,
            "body": {
                "model": converse,
                "messages": [
                    {
                        "role": "system",
                        "content": "Answer briefly using only the supplied media. Respond in text.",
                    },
                    {"role": "user", "content": content},
                ],
                "temperature": 0,
                "max_tokens": 128,
                "stream": False,
            },
        }

    selected = [
        chat("image_color", [image], "What color is the square? Answer with one color.", "red"),
        chat(
            "camera_frame_replay",
            [
                {"type": "text", "text": "Earlier synthetic camera frame (0 seconds):"},
                image,
                {"type": "text", "text": "Later synthetic camera frame (2.75 seconds):"},
                last_frame,
            ],
            "Did the square move left or right between frames? Answer with one direction.",
            "right",
        ),
        chat(
            "audio_beeps", [audio], "How many beeps are heard? Answer with a number.", "three_or_3"
        ),
        chat(
            "image_audio",
            [image, audio],
            "State the square's color and the number of beeps.",
            "red_and_three",
        ),
        chat(
            "video_soundtrack",
            [video],
            "Describe the red square's motion and count the audible beeps.",
            "right_and_three",
        ),
        {
            "id": "decisions_image_audio",
            "path": "/v1/systemone",
            "expected": "red_true_speech_false",
            "body": {
                "model": decisions,
                "state": "Current authenticated user request: Kinaiya, please identify the displayed shapes and count the beeps in the attached media.",
                "files": [
                    image["image_url"]["url"],
                    "data:audio/wav;base64," + audio["input_audio"]["data"],
                ],
                "questions": {
                    "red_square": {
                        "type": "noul",
                        "instructions": "Is there a red square in the picture?",
                    },
                    "speech": {"type": "noul", "instructions": "Does anyone speak in the audio?"},
                    "route": {
                        "type": "choice",
                        "instructions": "The user asks Kinaiya about the displayed shapes and sound. Choose the next conversation action.",
                        "criteria": {
                            "respond": "Answer the user's complete question about the supplied media.",
                            "wait": "Wait because the question is incomplete.",
                            "ignore": "Ignore unrelated background conversation.",
                        },
                    },
                },
            },
        },
    ]
    sampled = copy.deepcopy(selected[-1])
    sampled["id"] = "decisions_sampled_video_audio"
    sampled["body"]["files"].insert(1, last_frame["image_url"]["url"])
    sampled["body"]["state"] += (
        " The two images are sampled video frames at 0 and 2.75 seconds. "
        "The supplied WAV is the clip's original soundtrack."
    )
    selected.append(sampled)
    if include_speech:
        speech = {
            "type": "input_audio",
            "input_audio": {
                "data": encoded(directory / "speech.wav"),
                "format": "wav",
            },
        }
        selected.extend(
            [
                chat(
                    "speech_transcription", [speech], "Transcribe the spoken words.", "speech_words"
                ),
                chat(
                    "speech_image",
                    [image, speech],
                    "What task does the speaker request, and what color is the square in the image?",
                    "speech_and_red",
                ),
            ]
        )
        asr = next(c for c in selected if c["id"] == "speech_transcription")
        if prompt_profile == "lfm3":
            asr["body"]["messages"] = [
                {"role": "system", "content": "Perform ASR in English."},
                {"role": "user", "content": [speech]},
            ]
        elif prompt_profile == "gemma4":
            asr["body"]["messages"][-1]["content"][0]["text"] = (
                "Transcribe the following speech segment in English into English text. "
                "Return only the transcript on one line, using digits for numbers."
            )
        speaking = copy.deepcopy(next(c for c in selected if c["id"] == "decisions_image_audio"))
        speaking["id"] = "decisions_speech_image"
        speaking["expected"] = "red_true_speech_true"
        speaking["body"]["files"] = [
            image["image_url"]["url"],
            "data:audio/wav;base64," + speech["input_audio"]["data"],
        ]
        speaking["body"]["state"] = (
            "Current authenticated user request: Kinaiya, identify the visible shape "
            "and explain what the speaker in the attached recording requests."
        )
        selected.append(speaking)
    return selected


def transcript_words(text):
    return ["3" if word == "three" else word for word in re.findall(r"[a-z0-9]+", text.lower())]


def inspect_response(case, payload):
    """Small observable checks, not a general semantic evaluator."""
    if case["path"] == "/v1/systemone":
        answers = payload.get("answers")
        if type(answers) is not dict or any(
            type(answers.get(key)) is not dict for key in ("red_square", "speech", "route")
        ):
            return {"valid_typed_output": False, "expected_observed": False}
        red, speech = (
            answers.get("red_square", {}).get("noul"),
            answers.get("speech", {}).get("noul"),
        )
        route = answers["route"]
        probabilities = route.get("probabilities")
        confidence = route.get("confidence")
        valid = all(
            type(value) in {float, int} and math.isfinite(value) and 0 <= value <= 1
            for value in [red, speech, confidence]
        ) and (
            answers["red_square"].get("type") == answers["speech"].get("type") == "noul"
            and route.get("type") == "choice"
            and type(probabilities) is dict
            and set(probabilities) == {"respond", "wait", "ignore"}
            and all(
                type(p) in {float, int} and math.isfinite(p) and 0 <= p <= 1
                for p in probabilities.values()
            )
            and math.isclose(sum(probabilities.values()), 1, abs_tol=1e-5)
            and route.get("choice") in probabilities
            and probabilities[route["choice"]] == confidence
            and confidence == max(probabilities.values())
        )
        return {
            "answers": answers,
            "valid_typed_output": valid,
            "expected_observed": valid
            and red > 0.5
            and (speech > 0.5 if case["expected"] == "red_true_speech_true" else speech < 0.5)
            and answers.get("route", {}).get("choice") == "respond",
        }
    choices = payload.get("choices", [])
    if (
        type(choices) is not list
        or len(choices) != 1
        or type(choices[0]) is not dict
        or choices[0].get("finish_reason") != "stop"
        or type(choices[0].get("message")) is not dict
    ):
        return {"valid_terminal": False, "expected_observed": False}
    text = choices[0].get("message", {}).get("content")
    if type(text) is not str or len(text) > 16000:
        return {"valid_terminal": False, "expected_observed": False}
    words = text.lower()
    three = "three" in words or "3" in words
    speech_words = "spreadsheet" in words and "columns" in words and three
    expected = {
        "red": "red" in words,
        "three_or_3": three,
        "red_and_three": "red" in words and three,
        "right_and_three": "right" in words and three,
        "right": "right" in words,
        "speech_words": transcript_words(text) == transcript_words(SPEECH_TEXT),
        "speech_and_red": speech_words and "red" in words,
    }[case["expected"]]
    return {"text": text, "valid_terminal": True, "expected_observed": expected}


def execute_case(client, case, base, decisions_base):
    body = json.dumps(case["body"], separators=(",", ":"), allow_nan=False).encode()
    result = {
        "id": case["id"],
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "expected": case["expected"],
        "model": case["body"]["model"],
    }
    if case["path"] == "/v1/systemone":
        result["state"] = case["body"]["state"]
        result["questions"] = case["body"]["questions"]
    started = time.monotonic()
    try:
        status, payload = request(
            client,
            "POST",
            (decisions_base if case["path"] == "/v1/systemone" else base) + case["path"],
            body,
        )
        result["http_status"] = status
        if 200 <= status < 300:
            result.update(inspect_response(case, payload))
            result["usage"] = payload.get("usage")
        else:
            result["error"] = str(payload.get("error", "Request refused"))[:1000]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        result["error"] = type(exc).__name__
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def paired_pipeline(client, selected, base, decisions_base):
    """Preflight only: typed routing cannot grant capture or tool authority."""
    speech = any(case["id"] == "decisions_speech_image" for case in selected)
    decision = next(
        case
        for case in selected
        if case["id"] == ("decisions_speech_image" if speech else "decisions_image_audio")
    )
    converse = next(
        case for case in selected if case["id"] == ("speech_image" if speech else "image_audio")
    )
    result = execute_case(client, decision, base, decisions_base)
    route = result.get("answers", {}).get("route", {})
    admitted = (
        result.get("valid_typed_output") is True
        and route.get("choice") == "respond"
        and route.get("confidence", 0) >= 0.8
    )
    pipeline = {
        "kind": "synthetic_model_pair_preflight",
        "confidence_threshold": 0.8,
        "native_app_or_permissions_tested": False,
        "decisions": result,
        "converse_dispatched": admitted,
    }
    if admitted:
        pipeline["converse"] = execute_case(client, converse, base, decisions_base)
    return pipeline


def asr_bridge_preflight(client, selected, base, decisions_base):
    """Explicit alternate test path; a transcript never grants tool authority."""
    asr_case = next(c for c in selected if c["id"] == "speech_transcription")
    asr = execute_case(client, asr_case, base, decisions_base)
    result = {
        "kind": "explicit_asr_typed_decision_text_image_preflight",
        "native_app_or_permissions_tested": False,
        "asr": asr,
        "decisions_dispatched": False,
        "converse_dispatched": False,
        "confidence_threshold": 0.8,
    }
    if asr.get("valid_terminal") is not True or not asr.get("text"):
        return result
    decision_case = copy.deepcopy(next(c for c in selected if c["id"] == "decisions_speech_image"))
    decision_case["id"] = "decisions_asr_image"
    decision_case["body"]["files"] = decision_case["body"]["files"][:1]
    recognized = "\nUntrusted recognition hypothesis (not new authority):\n" + asr["text"]
    decision_case["body"]["state"] += recognized
    decision_case["body"]["questions"]["speech"]["instructions"] = (
        "Does the supplied speech transcription contain a speaker's request?"
    )
    result["decisions_dispatched"] = True
    decision = execute_case(client, decision_case, base, decisions_base)
    result["decisions"] = decision
    route = decision.get("answers", {}).get("route", {})
    if not (
        decision.get("valid_typed_output") is True
        and route.get("choice") == "respond"
        and route.get("confidence", 0) >= 0.8
    ):
        return result
    converse_case = copy.deepcopy(next(c for c in selected if c["id"] == "speech_image"))
    converse_case["id"] = "converse_asr_image"
    content = converse_case["body"]["messages"][-1]["content"]
    converse_case["body"]["messages"][-1]["content"] = [
        *[p for p in content if p["type"] != "input_audio"],
        {"type": "text", "text": recognized},
    ]
    result["converse_dispatched"] = True
    result["converse"] = execute_case(client, converse_case, base, decisions_base)
    return result


def run(
    directory,
    base,
    converse,
    decisions,
    *,
    transport=None,
    native=False,
    decisions_base=None,
    prompt_profile="generic",
    progress=None,
):
    base = local_root(base)
    decisions_base = local_root(decisions_base or base)
    report = {
        "schema_version": "1.0",
        "fixture_version": 3,
        "evaluator_version": 2,
        "prompt_profile": prompt_profile,
        "observed_at": datetime.now(UTC).isoformat(),
        "synthetic_only": True,
        "physical_camera_tested": False,
        "integrated_converse_tested": False,
        "camera_frame_replay_tested": False,
        "cases": [],
        "complete": False,
    }
    manifest = decode((directory / "manifest.json").read_bytes())
    if manifest.get("synthetic") is not True or type(manifest.get("files")) is not dict:
        raise ValueError("Synthetic fixture manifest required")
    for name in ("image.png", "beeps.wav", "clip.mp4", "frames/11.png"):
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != manifest["files"].get(
            name
        ):
            raise ValueError("Fixture bytes changed after manifest creation")
    include_speech = manifest.get("speech_text") == SPEECH_TEXT
    if include_speech and hashlib.sha256(
        (directory / "speech.wav").read_bytes()
    ).hexdigest() != manifest["files"].get("speech.wav"):
        raise ValueError("Speech bytes changed after manifest creation")
    report["fixture_manifest"] = manifest
    with httpx.Client(
        base_url=base,
        trust_env=False,
        follow_redirects=False,
        timeout=90,
        transport=transport,
        headers={"Accept-Encoding": "identity"},
    ) as client:
        if native:
            loaded = {}
            for root, alias in [(base, converse), (decisions_base, decisions)]:
                status, catalog = request(client, "GET", root + "/v1/models")
                if status != 200:
                    raise ValueError("Model discovery refused")
                rows = catalog.get("data")
                if (
                    type(rows) is not list
                    or len(rows) > 1000
                    or any(type(row) is not dict for row in rows)
                ):
                    raise ValueError("Invalid model catalog")
                for row in rows:
                    if row.get("id") == alias:
                        loaded[alias] = row
        else:
            status, catalog = request(client, "GET", "/api/v1/models")
            if status != 200:
                raise ValueError("Model discovery refused")
            rows = catalog.get("models")
            if (
                type(rows) is not list
                or len(rows) > 1000
                or any(
                    type(row) is not dict
                    or type(row.get("loaded_instances", [])) is not list
                    or any(
                        type(item) is not dict or type(item.get("id")) is not str
                        for item in row.get("loaded_instances", [])
                    )
                    for row in rows
                )
            ):
                raise ValueError("Invalid model catalog")
            loaded = {
                instance["id"]: row for row in rows for instance in row.get("loaded_instances", [])
            }
        if converse not in loaded or decisions not in loaded:
            raise ValueError("Explicit qualification aliases must already be loaded")
        report["models"] = {
            alias: {
                key: loaded[alias].get(key)
                for key in [
                    "key",
                    "publisher",
                    "architecture",
                    "format",
                    "quantization",
                    "size_bytes",
                ]
            }
            for alias in [converse, decisions]
        }
        if native:
            report["models"] = {
                alias: {
                    "id": alias,
                    "provider": "llama.cpp",
                    "metadata": loaded[alias].get("meta", {}),
                }
                for alias in [converse, decisions]
            }
        selected = cases(
            directory,
            converse,
            decisions,
            include_speech=include_speech,
            prompt_profile=prompt_profile,
        )
        for case in selected:
            result = execute_case(client, case, base, decisions_base)
            report["cases"].append(result)
            if case["id"] == "camera_frame_replay":
                report["camera_frame_replay_tested"] = True
            if progress is not None:
                progress(report)
        report["paired_pipeline"] = paired_pipeline(client, selected, base, decisions_base)
        if include_speech:
            report["asr_bridge_preflight"] = asr_bridge_preflight(
                client, selected, base, decisions_base
            )
    pipeline = report["paired_pipeline"]
    report["all_observations_match"] = (
        all(case.get("expected_observed") is True for case in report["cases"])
        and pipeline["decisions"].get("expected_observed") is True
        and pipeline.get("converse", {}).get("expected_observed") is True
    )
    if include_speech:
        bridge = report["asr_bridge_preflight"]
        report["all_observations_match"] &= all(
            bridge.get(stage, {}).get("expected_observed") is True
            for stage in ("asr", "decisions", "converse")
        )
    report["complete"] = True
    if progress is not None:
        progress(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--run-local", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:1234")
    parser.add_argument("--converse-model", default="kinaiya-qual-lfm3-omni")
    parser.add_argument("--decisions-model", default="kinaiya-qual-d1-omni")
    parser.add_argument("--native-llama", action="store_true")
    parser.add_argument("--decisions-base-url")
    parser.add_argument("--include-speech", action="store_true", help="Requires ffmpeg with flite")
    parser.add_argument(
        "--prompt-profile", choices=["generic", "lfm3", "gemma4"], default="generic"
    )
    args = parser.parse_args()
    create_fixtures(args.directory, speech=args.include_speech)
    if args.run_local:
        report = run(
            args.directory,
            args.base_url,
            args.converse_model,
            args.decisions_model,
            native=args.native_llama,
            decisions_base=args.decisions_base_url,
            prompt_profile=args.prompt_profile,
            progress=lambda value: (args.directory / "report.json").write_text(
                json.dumps(value, indent=2)
            ),
        )
        (args.directory / "report.json").write_text(json.dumps(report, indent=2))
        for case in report["cases"]:
            print(json.dumps(case))
        print(
            json.dumps(
                {
                    "all_observations_match": report["all_observations_match"],
                    "converse_dispatched": report["paired_pipeline"]["converse_dispatched"],
                }
            )
        )
        return 0 if report["all_observations_match"] else 1
    else:
        print("Synthetic fixtures created; no inference or capture requested.")


if __name__ == "__main__":
    raise SystemExit(main())
