"""Opt-in real inference/voice/task verification with synthetic inputs only.

Run against `kinaiya serve all` and the optional local voice worker. Every Meta
profile must use the run-local admission proxy. This never measures audibility.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import sys
import time
import wave
from pathlib import Path

import httpx

from packages.providers.speech import SpeechClient


def write_synthetic_wave(path, chunks):
    if not chunks:
        raise ValueError("Synthetic voice fixture has no audio")
    rate = chunks[0]["sample_rate"]
    pcm = []
    for chunk in chunks:
        data = base64.b64decode(chunk["audio"], validate=True)
        if chunk["sample_rate"] != rate or len(data) != chunk["samples"] * 2:
            raise ValueError("Synthetic voice fixture has inconsistent PCM metadata")
        pcm.append(data)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"".join(pcm))


class Probe:
    def __init__(self, args, token):
        self.args = args
        self.client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=120)

    async def get(self, port, path, **kwargs):
        response = await self.client.get(f"http://127.0.0.1:{port}{path}", **kwargs)
        response.raise_for_status()
        return response.json()

    async def post(self, port, path, body):
        response = await self.client.post(f"http://127.0.0.1:{port}{path}", json=body)
        response.raise_for_status()
        return response.json()

    async def session(self, channel):
        return await self.post(8760, "/v1/sessions", {"channel": channel})

    async def journal(self, session):
        return await self.get(
            8760, "/v1/transcript", params={"session_id": session["session_id"], "limit": 200}
        )

    async def wait_task(self, session, *, allow_approval=False):
        async with asyncio.timeout(90):
            while True:
                tasks = await self.get(8761, "/v1/agent/tasks")
                candidates = [t for t in tasks if t["session_id"] == session["session_id"]]
                if candidates:
                    task = candidates[-1]
                    if task["state"] in {"succeeded", "failed", "cancelled"} or (
                        allow_approval and task["state"] == "awaiting_approval"
                    ):
                        return task
                await asyncio.sleep(0.1)

    async def verify_files(self, task, expected):
        assert task["state"] == "succeeded", task
        verified = []
        for evidence in task["evidence"]:
            if evidence["kind"] == "file":
                path = Path(evidence["path"])
                path.resolve().relative_to(self.args.workspace.resolve())
                data = path.read_bytes()
                assert hashlib.sha256(data).hexdigest() == evidence["sha256"]
                if path.name in expected:
                    assert data.decode().strip() == expected[path.name], data
                verified.append({"path": path.name, "sha256": evidence["sha256"]})
        assert set(expected) <= {e["path"] for e in verified}
        return verified

    async def voice_case(self, speech, iteration):
        session = await self.session("converse")
        spoken = "Please create a new file named proof containing only the number seven."
        chunks = [chunk async for chunk in speech.synthesize(spoken)]
        fixture = self.args.data_dir / f"synthetic-spoken-{iteration}.wav"
        write_synthetic_wave(fixture, chunks)
        transcript = await speech.transcribe(fixture.read_bytes())
        request = {key: session[key] for key in ("session_id", "thread_id", "epoch")}
        request.update(
            text=transcript["text"],
            input_mode="voice",
            idempotency_key=f"voice-{iteration}",
            content_scope="public_synthetic",
        )
        started, first_text, state_at_first, events = time.perf_counter(), None, [], []
        async with self.client.stream(
            "POST", "http://127.0.0.1:8762/v1/converse/turns/stream", json=request
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    events.append(event)
                    if event["type"] == "text" and first_text is None:
                        first_text = time.perf_counter() - started
                        tasks = await self.get(8761, "/v1/agent/tasks")
                        state_at_first = [
                            t["state"] for t in tasks if t["session_id"] == session["session_id"]
                        ]
        assert first_text is not None, events
        task = await self.wait_task(session)
        files = await self.verify_files(task, {"proof": "7"})
        async with asyncio.timeout(10):
            while True:
                journal = await self.journal(session)
                notices = [
                    e
                    for e in journal
                    if e["kind"] == "conversation.converse" and e["payload"].get("outcome_event_id")
                ]
                if notices:
                    break
                await asyncio.sleep(0.1)
        notice = notices[-1]
        outcome = next(e for e in journal if e["event_id"] == notice["payload"]["outcome_event_id"])
        assert outcome["origin"] == "agent-supervisor" and outcome["sequence"] < notice["sequence"]
        body = {
            "event_id": notice["event_id"],
            "generation_epoch": notice["payload"]["generation_epoch"],
        }
        audio_start, first_audio, chunks = time.perf_counter(), None, 0
        async with self.client.stream(
            "POST", "http://127.0.0.1:8762/v1/converse/speech", json=body
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line:
                    json.loads(line)
                    first_audio = (
                        first_audio
                        if first_audio is not None
                        else time.perf_counter() - audio_start
                    )
                    chunks += 1
        assert chunks > 0
        await self.post(8762, "/v1/converse/interrupt", {"session_id": session["session_id"]})
        stale = await self.client.post("http://127.0.0.1:8762/v1/converse/speech", json=body)
        assert stale.status_code == 409
        assert (await self.get(8761, "/v1/agent/tasks/" + task["task_id"]))["state"] == "succeeded"
        replay = await self.post(8762, "/v1/converse/turns", request)
        assert all(e.get("replay") for e in replay["events"])
        report = {
            "case": "real_voice_to_task",
            "iteration": iteration,
            "synthetic_audio": True,
            "stt": transcript,
            "first_text_seconds": first_text,
            "agent_states_at_first_text": state_at_first,
            "verified_files": files,
            "outcome_sequence": outcome["sequence"],
            "notice_sequence": notice["sequence"],
            "notice": notice["payload"]["text"],
            "tts_chunks": chunks,
            "first_decoded_audio_seconds": first_audio,
            "audible_latency": None,
            "stale_audio_http": stale.status_code,
            "speech_stop_does_not_cancel_agent": True,
            "transport_replay_deduplicated": True,
        }
        # This is our synthetic fixture's owned artifact, never an arbitrary user file.
        if iteration == 1:
            (self.args.workspace / "proof").unlink()
        return report

    async def steer_case(self):
        session = await self.session("agent")
        body = {key: session[key] for key in ("session_id", "thread_id", "epoch")}
        body.update(
            objective="Write stale.txt containing steered.",
            idempotency_key="steer-task",
            content_scope="public_synthetic",
        )
        task = await self.post(8761, "/v1/agent/tasks", body)
        await self.post(
            8761,
            f"/v1/agent/tasks/{task['task_id']}/steer",
            {
                "instruction": "Use steered.txt instead of stale.txt. Do not create stale.txt.",
                "content_scope": "public_synthetic",
                "expected_objective_revision": 1,
                "idempotency_key": "steer-revision",
            },
        )
        final = await self.wait_task(session)
        files = await self.verify_files(final, {"steered.txt": "steered"})
        assert final["objective_revision"] == 2 and not (self.args.workspace / "stale.txt").exists()
        return {"case": "real_inference_with_steering", "revision": 2, "verified_files": files}

    async def failure_case(self):
        session = await self.session("agent")
        body = {key: session[key] for key in ("session_id", "thread_id", "epoch")}
        body.update(
            objective="Run the shell command false exactly once. Do not retry. Report its failure.",
            content_scope="public_synthetic",
            idempotency_key="failure-task",
        )
        await self.post(8761, "/v1/agent/tasks", body)
        task = await self.wait_task(session, allow_approval=True)
        if task["state"] == "awaiting_approval":
            approval = next(
                e for e in await self.journal(session) if e["kind"] == "approval.requested"
            )
            assert approval["payload"]["arguments"]["command"].strip() == "false"
            await self.post(
                8761,
                "/v1/approvals/" + approval["payload"]["approval_id"] + "/resolve",
                {
                    "approved": True,
                    "action_digest": approval["payload"]["action_digest"],
                    "objective_revision": 1,
                },
            )
        task = await self.wait_task(session)
        assert task["state"] == "failed", task
        assert any(e["kind"] == "command" and e["exit_code"] == 1 for e in task["evidence"])
        return {
            "case": "real_failed_shell_cannot_claim_success",
            "state": task["state"],
            "error": task["error"],
        }

    async def cli_case(self):
        await self.post(8762, "/v1/converse/enabled", {"enabled": False})
        try:
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "services.launcher",
                "--data-dir",
                str(self.args.data_dir),
                "--content-scope",
                "public_synthetic",
                "agent",
                "Write direct.txt containing exactly independent.",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(child.communicate(), 90)
            assert child.returncode == 0, (stdout, stderr)
            task = json.loads(stdout.decode().split("\n", 1)[1])
            files = await self.verify_files(task, {"direct.txt": "independent"})
            return {"case": "actual_cli_without_converse", "verified_files": files}
        finally:
            await self.post(8762, "/v1/converse/enabled", {"enabled": True})


async def run(args):
    if not args.allow_paid_inference:
        raise SystemExit(
            "This test uses real inference. Pass --allow-paid-inference after configuring budget enforcement."
        )
    config = json.loads((args.data_dir / "config.json").read_text())
    for profile in config["providers"].values():
        if profile.get("kind") == "meta" and profile["base_url"] != "http://127.0.0.1:9790/v1":
            raise SystemExit("Meta test calls must use the run-local budget proxy")
    for name in ("proof", "stale.txt", "steered.txt", "direct.txt"):
        if (args.workspace / name).exists():
            raise SystemExit(f"Use a fresh synthetic workspace; {name} already exists")
    tokens = json.loads((args.data_dir / "service-tokens.json").read_text())
    speech = SpeechClient(
        config["voice"]["url"],
        tokens["converse"],
        profile=config["voice"].get("profile", "Kokoro 82M"),
    )
    probe = Probe(args, tokens["owner"])
    reports = []
    try:
        for iteration in range(1, 3):
            reports.append(await probe.voice_case(speech, iteration))
            print(json.dumps(reports[-1]), flush=True)
        for case in (probe.steer_case, probe.failure_case, probe.cli_case):
            reports.append(await case())
            print(json.dumps(reports[-1]), flush=True)
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps({"cases": reports, "audible_latency_measured": False}, indent=2) + "\n"
        )
        await probe.client.aclose()
        await speech.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-paid-inference", action="store_true")
    asyncio.run(run(parser.parse_args()))
