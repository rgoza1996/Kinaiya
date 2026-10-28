"""Opt-in retained CPU voice managed-resource proof in a disposable owned worker.

Preinstall the selected runtime/assets deliberately. Real model/HTTP PCM stream;
scripted text and in-process authenticated ASGI control, no cloud or microphone.
Credentials/journal/state live in a private temporary directory and are removed.
"""

import argparse
import asyncio
import base64
import errno
import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

from packages.contracts.models import ConverseRequest, ProviderInfo, SessionRequest
from packages.providers.speech import SpeechClient
from packages.providers.text import ScriptedProvider
from packages.providers.tts_backends import SUPPORTED_TTS_PROFILES, profile_settings
from services.common.auth import Auth, load_or_create_tokens
from services.converse.api import create_app as converse_app
from services.converse.intent import ConverseIntent
from services.converse.runtime import ConverseRuntime
from services.resources.client import ConverseControl
from services.resources.launcher import resource_app
from services.session.store import LocalJournal, Store

SOURCE = Path(__file__).resolve().parents[1]
BASE = PYTHON = ASSETS = EXPECTED_HEAD = None
PROFILE = MODE = VOICE = REVISION = None
PORT = 18966
RATE = 24000
RESERVE = 8 * 1024**3
TEXT = "This is a public synthetic resource handoff check."
MiB = 1024 * 1024


def require(condition, label):
    if not condition:
        raise AssertionError(label)


def head():
    return subprocess.check_output(
        ["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True
    ).strip()


def port_unoccupied(port):
    # Binding a fresh socket never adopts, signals or stops an existing process.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def endpoint_unavailable(port):
    # A stopped TCP server can leave TIME_WAIT sockets which legitimately
    # prevent immediate bind(). A refused connection checks the endpoint
    # without treating TCP cleanup state as a still-running owned process.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        result = probe.connect_ex(("127.0.0.1", port))
    require(result == errno.ECONNREFUSED, "reserved voice endpoint no longer accepts connections")
    return result


def process_identity(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = raw[raw.rfind(")") + 2 :].split()
    return {"pid": pid, "start_ticks": int(fields[19])}


def worker_snapshot(status):
    row = status["workers"]["voice"]
    return {
        key: row[key]
        for key in (
            "state",
            "ready",
            "readiness",
            "owned_pid",
            "owned_pgid",
            "rss_bytes",
            "group_rss_bytes",
            "stop_evidence",
        )
    }


def safe_health(payload):
    return {
        key: payload[key]
        for key in (
            "service",
            "pid",
            "device",
            "loaded",
            "tts_profile",
            "tts_voice",
            "synthesis_mode",
            "tts_artifact_revision",
            "assets_state",
        )
    }


async def health(client, expected_pid, *, loaded):
    response = await client.get("/health")
    response.raise_for_status()
    value = safe_health(response.json())
    require(
        value["service"] == "local-voice" and value["pid"] == expected_pid,
        "owned health PID identity",
    )
    require(value["device"] == "cpu" and value["tts_profile"] == PROFILE, "selected CPU profile")
    require(value["tts_voice"] == VOICE and value["synthesis_mode"] == MODE, "selected voice/mode")
    require(value["tts_artifact_revision"] == REVISION, "pinned worker revision")
    require(value["loaded"] == (["tts"] if loaded else []), "actual model loading state")
    require(
        value["assets_state"] == ("verified" if loaded and REVISION else "not_checked"),
        "asset hash state",
    )
    return value


async def collect_audio(speech):
    digest = hashlib.sha256()
    samples = nonzero = frames = 0
    async for frame in speech.synthesize(TEXT):
        require(frame["type"] == "audio" and frame["provider"] == PROFILE, "actual audio backend")
        require(
            frame["device"] == "cpu" and frame["synthesis_mode"] == MODE, "honest synthesis mode"
        )
        require(
            frame["sample_rate"] == RATE and frame["chunk_index"] == frames, "rate and chunk order"
        )
        pcm = base64.b64decode(frame["audio"], validate=True)
        require(0 < len(pcm) <= RATE * 2 and len(pcm) % 2 == 0, "one-second PCM frame bound")
        require(len(pcm) // 2 == frame["samples"], "accurate sample count")
        digest.update(pcm)
        samples += frame["samples"]
        nonzero += sum(value != 0 for (value,) in struct.iter_unpack("<h", pcm))
        frames += 1
        require(samples <= 120 * RATE, "total audio bound")
    require(frames > 0 and samples > 0 and nonzero > 0, "nonempty actual PCM")
    return {
        "frames": frames,
        "samples": samples,
        "sample_rate": RATE,
        "audio_seconds": samples / RATE,
        "nonzero_samples": nonzero,
        "pcm_sha256": digest.hexdigest(),
        "synthesis_mode": MODE,
        "timing_measurement": False,
    }


async def probe(report):
    require(head() == EXPECTED_HEAD, "reviewed source revision")
    for port in (18960, 18961, 18962, PORT):
        port_unoccupied(port)
    report["reserved_ports_initially_unoccupied"] = [18960, 18961, 18962, PORT]
    report["source"] = {
        "commit": head(),
        "files_sha256": {
            relative: hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest()
            for relative in (
                "packages/providers/voice_worker.py",
                "packages/providers/tts_backends.py",
                "packages/providers/supertonic3_manifest.json",
                "scripts/probe_retained_resources.py",
                *(
                    [
                        "packages/providers/vibevoice_backend.py",
                        "packages/providers/vibevoice_manifest.json",
                    ]
                    if PROFILE == "VibeVoice Realtime 0.5B"
                    else []
                ),
                "services/resources/launcher.py",
                "services/converse/api.py",
                "services/converse/runtime.py",
                "packages/providers/speech.py",
                "services/resources/coordinator.py",
                "packages/resources/processes.py",
            )
        },
    }
    with tempfile.TemporaryDirectory(prefix="resource-proof-", dir=BASE) as temporary:
        private = Path(temporary)
        private.chmod(0o700)
        data, workspace = private / "data", private / "workspace"
        workspace.mkdir(mode=0o700)
        tokens = load_or_create_tokens(data)
        auth = Auth(tokens)
        store = Store(data / "synthetic-journal.sqlite")
        session = store.create_session(SessionRequest(channel="converse"))
        scripted = ScriptedProvider(["Public synthetic resource probe reply."])
        # Declaration is deliberately synthetic: no remote provider exists/calls.
        # This tests the coordinator path without claiming unmanaged local LLM release.
        scripted.info = ProviderInfo(
            provider="synthetic-no-network", model="scripted-fixture", local=False
        )
        runtime = ConverseRuntime(
            LocalJournal(store, "converse"),
            scripted,
            intent=ConverseIntent(data / "converse-intent.json"),
            resources_managed=True,
        )
        speech = SpeechClient(f"http://127.0.0.1:{PORT}", tokens["converse"], profile=PROFILE)
        converse = converse_app(runtime, auth, speech=speech)
        config = {
            "session_url": "http://127.0.0.1:18960",
            "agent_url": "http://127.0.0.1:18961",
            "converse_url": "http://127.0.0.1:18962",
            "voice": {"url": f"http://127.0.0.1:{PORT}", "profile": PROFILE},
            "resources": {
                "enabled": True,
                "voice_python": str(PYTHON),
                "voice_port": PORT,
                "tts_model_dir": str(ASSETS) if ASSETS is not None else None,
                "tts_voice": VOICE,
                "profile": "local_work_priority",
                "minimum_free_bytes": 128 * MiB,
                "voice_memory_reserve_bytes": RESERVE,
            },
        }
        app = resource_app(
            SimpleNamespace(data_dir=data, workspace=workspace), config, auth, tokens, SOURCE
        )
        coordinator = app.state.coordinator
        await coordinator.converse.close()
        await coordinator.journal.close()
        coordinator.converse = ConverseControl(
            "http://127.0.0.1:18962",
            tokens["resources"],
            transport=httpx.ASGITransport(app=converse),
        )
        coordinator.journal = LocalJournal(store, "resources")

        async def active_task(task_id):
            require(task_id == "synthetic-bound-task", "bounded synthetic task identity")
            return {"task_id": task_id, "state": "running"}

        coordinator.task_status = active_task
        owner_headers = {"Authorization": "Bearer " + tokens["owner"]}
        pidfds = []
        try:
            async with app.router.lifespan_context(app):
                async with (
                    httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{PORT}",
                        timeout=300,
                        trust_env=False,
                        follow_redirects=False,
                    ) as worker_http,
                    httpx.AsyncClient(
                        base_url="http://resource-probe",
                        headers=owner_headers,
                        transport=httpx.ASGITransport(app=app),
                        trust_env=False,
                    ) as controls,
                    httpx.AsyncClient(
                        base_url="http://converse-probe",
                        headers=owner_headers,
                        transport=httpx.ASGITransport(app=converse),
                        trust_env=False,
                    ) as converse_http,
                ):

                    async def status():
                        response = await controls.get("/v1/resources")
                        response.raise_for_status()
                        result = response.json()
                        require(
                            result["worker_count"] == 1 and set(result["workers"]) == {"voice"},
                            "one owned worker",
                        )
                        return result

                    async def action(operation, key, **fields):
                        response = await controls.post(
                            "/v1/resources/actions",
                            json={"operation": operation, "idempotency_key": key, **fields},
                        )
                        response.raise_for_status()
                        result = response.json()
                        require(result["ok"] is True, f"{operation} acknowledged")
                        return result

                    initial = await status()
                    require(
                        initial["policy"]["phase"] == "ready" and runtime.enabled,
                        "actual managed startup",
                    )
                    before = worker_snapshot(initial)
                    require(
                        before["state"] == "running" and before["ready"], "verified worker ready"
                    )
                    old_pid = before["owned_pid"]
                    old_identity = process_identity(old_pid)
                    require(old_identity is not None, "kernel process identity before load")
                    old_pidfd = os.pidfd_open(old_pid, 0)
                    pidfds.append(old_pidfd)
                    require(
                        process_identity(old_pid) == old_identity,
                        "identity stable around pidfd capture",
                    )
                    initial_health = await health(worker_http, old_pid, loaded=False)
                    before_audio = await collect_audio(speech)
                    loaded_health = await health(worker_http, old_pid, loaded=True)
                    loaded = await status()
                    loaded_worker = worker_snapshot(loaded)
                    require(
                        loaded_worker["group_rss_bytes"] is not None
                        and loaded_worker["group_rss_bytes"] > 128 * MiB,
                        "actual loaded process group memory",
                    )
                    report["before_handoff"] = {
                        "identity": old_identity,
                        "api_ready_health": initial_health,
                        "loaded_health": loaded_health,
                        "worker": loaded_worker,
                        "audio": before_audio,
                        "memory_available_bytes": loaded["memory_available_bytes"],
                    }
                    turn = await runtime.turn(
                        ConverseRequest(
                            session_id=session.session_id,
                            thread_id=session.thread_id,
                            epoch=session.epoch,
                            text="Public synthetic generation epoch fixture.",
                            idempotency_key="resource-proof-turn",
                        )
                    )
                    entry = next(row["entry"] for row in turn["events"] if row["type"] == "text")
                    old_speech = {
                        "event_id": entry["event_id"],
                        "generation_epoch": entry["payload"]["generation_epoch"],
                    }
                    await runtime.validate_speech(**old_speech)
                    handed = await action(
                        "handoff",
                        "resource-proof-handoff",
                        required_bytes=128 * MiB,
                        task_id="synthetic-bound-task",
                        reason="Public synthetic retained voice process release proof",
                    )
                    stopped_status = await status()
                    stopped = worker_snapshot(stopped_status)
                    require(
                        handed["policy"]["phase"] == "local_work" and not runtime.enabled,
                        "handoff gates Converse",
                    )
                    evidence = stopped["stop_evidence"]
                    require(
                        stopped["state"] == "stopped" and not stopped["ready"],
                        "owned worker stopped",
                    )
                    require(
                        evidence["reaped"]
                        and evidence["group_empty"]
                        and evidence["ownership_verified"],
                        "verified owned process release",
                    )
                    require(
                        evidence["residual_pids"] == []
                        and evidence["rss_before_bytes"] > 0
                        and evidence["group_rss_before_bytes"] > 0,
                        "kernel memory and empty process group evidence",
                    )
                    poll = select.poll()
                    poll.register(old_pidfd, select.POLLIN)
                    require(bool(poll.poll(0)), "retained pidfd observes original process exit")
                    old_after = process_identity(old_pid)
                    require(
                        old_after is None
                        or old_after["start_ticks"] != old_identity["start_ticks"],
                        "original process identity absent",
                    )
                    stale = await converse_http.post("/v1/converse/speech", json=old_speech)
                    require(stale.status_code == 409, "old speech refused while unloaded")
                    report["handoff"] = {
                        "phase": handed["policy"]["phase"],
                        "worker": stopped,
                        "required_bytes": handed["policy"]["required_bytes"],
                        "restore_high_water_bytes": handed["policy"]["restore_bytes"],
                        "memory_available_bytes": stopped_status["memory_available_bytes"],
                        "retained_pidfd_exited": True,
                        "old_identity_absent": True,
                        "old_speech_http": stale.status_code,
                        "bound_task_status": "scripted_running",
                    }
                    restored = await action(
                        "task_finished",
                        "resource-proof-task-finished",
                        task_id="synthetic-bound-task",
                    )
                    restored_status = await status()
                    restored_worker = worker_snapshot(restored_status)
                    new_pid = restored_worker["owned_pid"]
                    require(
                        restored["policy"]["phase"] == "ready" and runtime.enabled,
                        "actual worker restoration",
                    )
                    require(
                        new_pid != old_pid and restored_worker["ready"], "new verified owned PID"
                    )
                    new_identity = process_identity(new_pid)
                    require(new_identity is not None, "restored kernel identity")
                    new_pidfd = os.pidfd_open(new_pid, 0)
                    pidfds.append(new_pidfd)
                    require(
                        process_identity(new_pid) == new_identity,
                        "restored identity stable around pidfd capture",
                    )
                    restored_api = await health(worker_http, new_pid, loaded=False)
                    stale_after_restore = await converse_http.post(
                        "/v1/converse/speech", json=old_speech
                    )
                    require(
                        stale_after_restore.status_code == 409,
                        "old speech cannot revive after restoration",
                    )
                    await speech.close()
                    speech = SpeechClient(
                        f"http://127.0.0.1:{PORT}", tokens["converse"], profile=PROFILE
                    )
                    after_audio = await collect_audio(speech)
                    after_health = await health(worker_http, new_pid, loaded=True)
                    report["restored"] = {
                        "identity": new_identity,
                        "api_ready_health": restored_api,
                        "loaded_health": after_health,
                        "worker": worker_snapshot(await status()),
                        "audio": after_audio,
                        "old_speech_http": stale_after_restore.status_code,
                    }
                    off = await converse_http.post("/v1/converse/enabled", json={"enabled": False})
                    off.raise_for_status()
                    require(
                        off.json()["owner_enabled"] is False and not runtime.enabled,
                        "durable native owner off",
                    )
                    off_restore = await action("restore", "resource-proof-owner-off-restore")
                    off_status = await status()
                    off_worker = worker_snapshot(off_status)
                    require(
                        off_restore["policy"]["phase"] == "off" and not runtime.enabled,
                        "owner off survives restoration",
                    )
                    require(
                        off_worker["state"] == "stopped"
                        and off_worker["stop_evidence"]["reaped"]
                        and off_worker["stop_evidence"]["group_empty"],
                        "owner off releases restored process",
                    )
                    poll = select.poll()
                    poll.register(new_pidfd, select.POLLIN)
                    require(bool(poll.poll(0)), "restored process exit on owner off")
                    report["owner_off_restore"] = {
                        "owner_enabled": False,
                        "phase": off_restore["policy"]["phase"],
                        "worker": off_worker,
                        "retained_pidfd_exited": True,
                    }
                    report["synthetic_text_calls"] = len(scripted.calls)
                    report["resource_journal_events"] = len(store.read(kinds=["resource.changed"]))
                    require(len(scripted.calls) == 1, "exactly one scripted text fixture call")
            report["cleanup"] = {
                "manager_closed": coordinator.manager.status()["closed"],
                "all_owned_workers_stopped": all(
                    row["state"] == "stopped"
                    for row in coordinator.manager.status()["workers"].values()
                ),
            }
            endpoint_unavailable(PORT)
            report["cleanup"]["reserved_voice_port_released"] = True
        finally:
            await speech.close()
            for fd in pidfds:
                os.close(fd)
            store.close()
    report["cleanup"]["private_temporary_data_removed"] = not private.exists()
    report["source"]["commit_after_probe"] = head()
    require(
        report["source"]["commit_after_probe"] == EXPECTED_HEAD,
        "source revision stable during proof",
    )
    for relative, expected in report["source"]["files_sha256"].items():
        require(
            hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest() == expected,
            "source file stable during proof",
        )


async def main():
    global BASE, PYTHON, ASSETS, EXPECTED_HEAD, PROFILE, MODE, VOICE, REVISION, PORT, RATE, RESERVE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--profile", choices=SUPPORTED_TTS_PROFILES, required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--port", type=int, default=18966)
    parser.add_argument("--reserve-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    BASE = args.output.parent.resolve()
    PYTHON = args.python.absolute()  # resolving a venv symlink loses its environment
    ASSETS = args.model_dir.absolute() if args.model_dir else None
    PROFILE, PORT, RESERVE = args.profile, args.port, args.reserve_bytes
    settings = profile_settings(PROFILE)
    VOICE, MODE, REVISION = settings.voice, settings.mode, settings.artifact_revision
    RATE = 44100 if PROFILE == "Supertonic 3" else 24000
    EXPECTED_HEAD = head()
    report = {
        "schema": 1,
        "profile": PROFILE,
        "scope": "Actual owned CPU voice worker and HTTP PCM; scripted text plus authenticated ASGI controls",
        "started_utc": datetime.now(UTC).isoformat(),
        "passed": False,
        "limits": [
            "No physical playback or microphone",
            "No real cloud inference or Agent task execution",
            "No GPU release/reservation proof",
            "No latency benchmark or percentile claim",
            "Only disposable reserved ports; existing live ports are never contacted",
        ],
    }
    try:
        await asyncio.wait_for(probe(report), timeout=600)
        report["passed"] = True
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        if isinstance(exc, AssertionError):
            report["failed_check"] = str(exc)
    finally:
        report["finished_utc"] = datetime.now(UTC).isoformat()
        raw = json.dumps(report, indent=2) + "\n"
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(raw)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "report": args.output.name,
                    "failure_type": report.get("failure_type"),
                    "failed_check": report.get("failed_check"),
                }
            )
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
