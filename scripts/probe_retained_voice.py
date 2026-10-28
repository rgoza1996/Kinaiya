"""Opt-in, synthetic CPU TTS screen in a disposable, independently owned worker.

No paid inference, user config, reference service or physical audio device is used.
The worker environment and model assets must already be installed by the owner.
"""

import argparse
import base64
import hashlib
import json
import os
import platform
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import httpx

from packages.providers.speech import RETAINED_TTS
from services.common.auth import load_or_create_tokens

SYNTHETIC_TEXTS = (
    "I am here. What would you like to do?",
    "The synthetic report contains three items, and the next review is at ten thirty.",
    "Kinaiya can keep our conversation available while the independent agent works.",
)


def decode_frame(row, index, sample_rate):
    """Refuse malformed measurements instead of silently measuring corrupt audio."""
    rate = row.get("sample_rate")
    if (
        row.get("type") != "audio"
        or type(row.get("chunk_index")) is not int
        or row["chunk_index"] != index
        or type(rate) is not int
        or not 8000 <= rate <= 96000
        or (sample_rate is not None and sample_rate != rate)
        or type(row.get("samples")) is not int
        or not 0 < row["samples"] <= rate
    ):
        raise ValueError("Invalid or reordered PCM metadata")
    pcm = base64.b64decode(row["audio"], validate=True)
    if len(pcm) != row["samples"] * 2:
        raise ValueError("PCM size disagrees with sample count")
    return pcm, rate


def process_sample(pid):
    status = Path(f"/proc/{pid}/status").read_text().splitlines()
    memory = {
        line.split(":", 1)[0]: int(line.split()[1]) * 1024
        for line in status
        if line.startswith(("VmRSS:", "VmHWM:"))
    }
    # The command can contain spaces or parentheses; split after its last ')'.
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    memory["cpu_seconds"] = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
    return memory


def measure(client, text, profile, pid, *, wav_path=None):
    start_memory = process_sample(pid)
    start = time.perf_counter()
    first_pcm = first_non_silent = None
    sample_rate = None
    frames = []
    arrivals = []
    metadata = None
    producer_at_first_frame = None
    with client.stream(
        "POST", "/v1/audio/speech", json={"input": text, "profile": profile}
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            if len(line) > 300000:
                raise ValueError("Frame exceeds screen's measurement buffer")
            row = json.loads(line)
            pcm, sample_rate = decode_frame(row, len(frames), sample_rate)
            now = time.perf_counter() - start
            if first_pcm is None:
                first_pcm = now
                metadata = {
                    key: row.get(key)
                    for key in (
                        "provider",
                        "device",
                        "synthesis_mode",
                        "elapsed_seconds",
                        "load_seconds",
                        "first_waveform_seconds",
                    )
                }
                producer_at_first_frame = client.get("/health").json()
                if producer_at_first_frame.get("pid") != pid:
                    raise ValueError("Voice worker identity changed during synthesis")
            if first_non_silent is None and any(
                abs(value[0]) >= 328 for value in struct.iter_unpack("<h", pcm)
            ):
                first_non_silent = now
            frames.append(pcm)
            arrivals.append({"elapsed_seconds": now, "samples": row["samples"]})
            if sum(map(len, frames)) > 120 * sample_rate * 2:
                raise ValueError("Audio exceeds the 120-second screen limit")
    finished = time.perf_counter() - start
    if not frames or first_non_silent is None:
        raise ValueError("No non-silent PCM decoded")
    pcm = b"".join(frames)
    samples = len(pcm) // 2
    duration = samples / sample_rate
    if wav_path is not None:
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        with wave.open(str(wav_path), "rb") as wav:
            assert wav.getnframes() == samples and wav.getframerate() == sample_rate
    after_memory = process_sample(pid)
    return {
        "text": text,
        "request_to_first_decoded_pcm_seconds": first_pcm,
        "request_to_first_non_silent_frame_seconds": first_non_silent,
        "request_to_complete_seconds": finished,
        "audio_seconds": duration,
        "rtf_including_http": finished / duration,
        "sample_rate": sample_rate,
        "samples": samples,
        "chunks": len(frames),
        "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
        "first_frame_metadata": metadata,
        "chunk_arrivals": arrivals,
        "producer_health_at_first_frame": producer_at_first_frame,
        "decoded_before_producer_exit_observed": (
            metadata.get("synthesis_mode") == "native_streaming"
            and producer_at_first_frame.get("active_producers") == 1
            and producer_at_first_frame.get("producer_stopped") is False
        ),
        "worker_rss_before_bytes": start_memory.get("VmRSS"),
        "worker_rss_after_bytes": after_memory.get("VmRSS"),
        "worker_process_lifetime_peak_rss_bytes": after_memory.get("VmHWM"),
        "worker_cpu_seconds": after_memory["cpu_seconds"] - start_memory["cpu_seconds"],
    }


def measure_cancellation(client, profile, pid, *, timeout=10):
    """Close actual HTTP playback and observe owned producer termination.

    This times close-to-observed-thread-exit, not acoustic barge-in or native
    kernel preemption. The isolated worker has no other synthesis clients.
    """
    text = (
        "This public synthetic cancellation fixture describes a long report. "
        "The first section has three items, the second has five, and the next review is tomorrow. "
    ) * 4
    before = client.get("/health").json()
    with client.stream(
        "POST", "/v1/audio/speech", json={"input": text, "profile": profile}
    ) as response:
        response.raise_for_status()
        first = next(line for line in response.iter_lines() if line)
        frame = json.loads(first)
        decode_frame(frame, 0, None)
        during = client.get("/health").json()
        if during.get("pid") != pid or during.get("active_producers") != 1:
            raise ValueError("No live owned producer observed before cancellation")
    closed_at = time.perf_counter()
    while time.perf_counter() - closed_at < timeout:
        after = client.get("/health").json()
        if after.get("pid") != pid:
            raise ValueError("Voice worker identity changed during cancellation")
        if after.get("synthesis_quarantined") is True:
            raise ValueError("Native synthesis cleanup could not confirm producer exit")
        if after.get("producer_stopped") is True and after.get("active_producers") == 0:
            if after.get("last_producer_cancelled") is not True or after.get(
                "completed_producers", 0
            ) <= before.get("completed_producers", 0):
                raise ValueError("Native cancellation was not observed by the producer")
            elapsed = time.perf_counter() - closed_at
            restored = measure(client, SYNTHETIC_TEXTS[0], profile, pid)
            return {
                "boundary": "HTTP response close to observed producer-thread exit; polled at 50ms",
                "close_to_observed_producer_exit_seconds": elapsed,
                "health_before": before,
                "health_after_first_frame": during,
                "health_after_exit": after,
                "subsequent_synthesis": restored,
                "physical_audio_stop_measured": False,
            }
        time.sleep(0.05)
    raise TimeoutError("Native producer did not stop within cancellation observation deadline")


def summarize(rows):
    keys = (
        "request_to_first_decoded_pcm_seconds",
        "request_to_complete_seconds",
        "rtf_including_http",
    )
    return {
        "samples": len(rows),
        "statistics": {
            key: {
                "minimum": min(row[key] for row in rows),
                "median": statistics.median(row[key] for row in rows),
                "maximum": max(row[key] for row in rows),
            }
            for key in keys
        },
        "percentile_claim": None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--profile", choices=RETAINED_TTS, required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--voice")
    parser.add_argument("--port", type=int, default=18965)
    parser.add_argument("--warm-repeats", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retain-synthetic-audio", type=Path)
    parser.add_argument("--exercise-cancellation", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.retain_synthetic_audio:
        args.retain_synthetic_audio.mkdir(parents=True, exist_ok=True)
    # Never adopt, stop or send credentials to an existing listener.
    with socket.socket() as check:
        check.bind(("127.0.0.1", args.port))
    with tempfile.TemporaryDirectory(prefix="kinaiya-tts-screen-") as temporary:
        data = Path(temporary)
        tokens = load_or_create_tokens(data)
        command = [
            str(args.python.absolute()),
            "-m",
            "packages.providers.voice_worker",
            "--port",
            str(args.port),
            "--data-dir",
            str(data),
            "--tts-profile",
            args.profile,
        ]
        if args.model_dir:
            command.extend(["--tts-model-dir", str(args.model_dir.resolve())])
        if args.voice:
            command.extend(["--tts-voice", args.voice])
        env = {
            **os.environ,
            "PYTHONPATH": str(root),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "OMP_NUM_THREADS": "4",
            "TOKENIZERS_PARALLELISM": "false",
        }
        with (data / "worker.log").open("w+") as log:
            worker = subprocess.Popen(
                command,
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{args.port}",
                    timeout=300,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Authorization": "Bearer " + tokens["converse"]},
                ) as client:
                    before = None
                    for _ in range(100):
                        if worker.poll() is not None:
                            log.seek(0)
                            raise RuntimeError("Disposable worker exited: " + log.read()[-4000:])
                        try:
                            health = client.get("/health").json()
                            if (
                                health.get("pid") == worker.pid
                                and health.get("tts_profile") == args.profile
                            ):
                                before = health
                                break
                        except (httpx.HTTPError, ValueError):
                            pass
                        time.sleep(0.1)
                    if before is None:
                        raise RuntimeError("Disposable worker identity/readiness not verified")
                    cold = measure(client, SYNTHETIC_TEXTS[0], args.profile, worker.pid)
                    warm = []
                    for repeat in range(args.warm_repeats):
                        for index, text in enumerate(SYNTHETIC_TEXTS):
                            wav = (
                                args.retain_synthetic_audio / f"warm-{repeat}-{index}.wav"
                                if args.retain_synthetic_audio
                                else None
                            )
                            warm.append(
                                measure(client, text, args.profile, worker.pid, wav_path=wav)
                            )
                    cancellation = (
                        measure_cancellation(client, args.profile, worker.pid)
                        if args.exercise_cancellation
                        else None
                    )
                    report = {
                        "kind": "isolated_retained_tts_screen",
                        "profile": args.profile,
                        "source_commit": subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], cwd=root, text=True
                        ).strip(),
                        "source_dirty": bool(
                            subprocess.check_output(
                                ["git", "status", "--porcelain"], cwd=root, text=True
                            ).strip()
                        ),
                        "host": {
                            "platform": platform.platform(),
                            "machine": platform.machine(),
                            "cpu_model": next(
                                (
                                    line.split(":", 1)[1].strip()
                                    for line in Path("/proc/cpuinfo").read_text().splitlines()
                                    if line.startswith("model name")
                                ),
                                "unknown",
                            ),
                        },
                        "probe_python": sys.version,
                        "worker_environment": json.loads(
                            subprocess.check_output(
                                [
                                    str(args.python.absolute()),
                                    "-c",
                                    "import importlib.metadata,json,sys; "
                                    "names={'fastapi','httpx','uvicorn','pydantic','numpy',"
                                    "'supertonic','onnxruntime','kokoro','torch','faster-whisper',"
                                    "'soundfile','ctranslate2','huggingface-hub',"
                                    "'vibevoice','kinaiya-vibevoice-runtime',"
                                    "'transformers','diffusers','accelerate'}; "
                                    "print(json.dumps({'python':sys.version,'packages':"
                                    "{d.metadata['Name']:d.version for d in "
                                    "importlib.metadata.distributions() "
                                    "if d.metadata['Name'].lower() in names}}))",
                                ],
                                text=True,
                            )
                        ),
                        "health_before": before,
                        "health_after": client.get("/health").json(),
                        "cold_process_first_request": cold,
                        "cold_definition": "New process and unloaded weights; filesystem/page caches were not flushed.",
                        "warm": warm,
                        "warm_summary": summarize(warm),
                        "physical_audible_latency_seconds": None,
                        "client_playback_measured": False,
                        "native_inference_cancellation_measured": cancellation is not None,
                        "cancellation": cancellation,
                        "limitations": [
                            "Synthetic HTTP/PCM screen only; no physical microphone/speaker or hearing measurement.",
                            "Transport frame count alone is not native streaming; first-frame health records whether production was still active.",
                            "No loaded Decisions/Converse contention comparison or M2 performance acceptance.",
                            "A single cold process observation and disclosed warm repetitions; no p95 claim.",
                        ],
                    }
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
                    print(json.dumps({"output": str(args.output), "warm": report["warm_summary"]}))
            finally:
                if worker.poll() is None:
                    os.killpg(worker.pid, signal.SIGTERM)
                    try:
                        worker.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(worker.pid, signal.SIGKILL)
                        worker.wait(timeout=5)


if __name__ == "__main__":
    main()
