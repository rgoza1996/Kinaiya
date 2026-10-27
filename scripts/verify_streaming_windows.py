"""Opt-in public/synthetic streaming probe against four native HTTP services.

Uses actual configured STT, Decisions, Converse, TTS and Agent. No fallback
classifications or executor fixtures. The isolated Agent must start with writes
requiring approval; this probe approves only its explicit public fixture tasks.
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import time
import uuid
import wave
from pathlib import Path

import httpx


class Probe:
    def __init__(self, args, tokens):
        self.args, self.tokens = args, tokens
        self.http = httpx.AsyncClient(timeout=120, trust_env=False)
        self.checks, self.windows = {}, []

    async def request(self, port, path, body=None, role="owner", params=None):
        response = await self.http.request(
            "GET" if body is None else "POST",
            f"http://127.0.0.1:{port}{path}",
            headers={"Authorization": "Bearer " + self.tokens[role]},
            json=body,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def budget(self):
        response = await self.http.get(self.args.budget_url)
        response.raise_for_status()
        data = response.json()
        return {
            k: data.get(k)
            for k in (
                "contributor_estimated_usd",
                "admission_estimated_usd",
                "reserved_usd",
                "billing_reported_usd",
                "admission_limit_usd",
            )
        }

    async def entries(self):
        return await self.request(
            self.args.session_port,
            "/v1/transcript",
            params={"session_id": self.session["session_id"], "limit": 200},
        )

    async def wait(self, read, predicate, timeout=45):
        async with asyncio.timeout(timeout):
            while True:
                result = await read()
                if predicate(result):
                    return result
                await asyncio.sleep(0.1)

    async def task(self, task_id):
        return await self.request(self.args.agent_port, "/v1/agent/tasks/" + task_id)

    async def submit(self, objective):
        return await self.request(
            self.args.agent_port,
            "/v1/agent/tasks",
            {
                **{k: self.session[k] for k in ("session_id", "thread_id", "epoch")},
                "objective": objective,
                "idempotency_key": str(uuid.uuid4()),
            },
        )

    async def approve(self, task_id):
        # Approve only the matching native revision/digest in this synthetic scope.
        entries = await self.entries()
        pending = next(
            e
            for e in reversed(entries)
            if e["kind"] == "approval.requested" and task_id in e["task_refs"]
        )
        task = await self.task(task_id)
        payload = pending["payload"]
        assert pending["origin"] == "agent-supervisor"
        assert task["state"] == "awaiting_approval"
        await self.request(
            self.args.agent_port,
            "/v1/approvals/" + payload["approval_id"] + "/resolve",
            {
                "approved": True,
                "action_digest": payload["action_digest"],
                "objective_revision": task["objective_revision"],
            },
        )

    async def window(self, text, label, *, speaking=True, utterance=None, index=0, audio=None):
        body = {
            **{k: self.session[k] for k in ("session_id", "thread_id", "epoch")},
            "utterance_id": utterance or str(uuid.uuid4()),
            "window_index": index,
            "revision": 1,
            "text": text,
            "start_ms": 0,
            "end_ms": 1000,
            "user_speaking": speaking,
            "capture_ended": False,
            "content_scope": "public_synthetic",
        }
        if audio is not None:
            body["audio_base64"] = base64.b64encode(audio).decode()
        started = time.monotonic()
        result = await self.request(
            self.args.converse_port,
            "/v1/converse/audio/windows" if audio else "/v1/converse/windows",
            body,
        )
        source = result["entry"]

        async def decision():
            return await self.request(
                self.args.session_port, "/v1/transcript/windows/" + source["event_id"] + "/decision"
            )

        native = await self.wait(decision, lambda d: d is not None, timeout=20)
        assert native["origin"] == "decisions" and native["causes"] == [source["event_id"]]
        d = native["payload"]
        self.windows.append(
            {
                "case": label,
                "source_event_id": source["event_id"],
                "utterance_id": body["utterance_id"],
                "window_index": index,
                "recognized_text": source["payload"]["text"],
                "conversation": d["conversation"],
                "agent_route": d["agent_route"],
                "classifier_ms": d["classifier_ms"],
                "decision_wall_ms": (time.monotonic() - started) * 1000,
                "stt_ms": result.get("stt_ms"),
            }
        )
        return source, d

    async def spoken_wav(self, text):
        frames = []
        rate = None
        async with self.http.stream(
            "POST",
            self.args.voice_url + "/v1/audio/speech",
            headers={"Authorization": "Bearer " + self.tokens["converse"]},
            json={"input": text, "profile": "Kokoro 82M"},
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                frame = json.loads(line)
                if frame.get("audio"):
                    frames.append(base64.b64decode(frame["audio"]))
                    rate = frame["sample_rate"]
        assert frames and rate
        pcm = b"".join(frames)

        def wav(data):
            output = io.BytesIO()
            with wave.open(output, "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(rate)
                stream.writeframes(data)
            return output.getvalue()

        return wav(pcm), wav(pcm[: (len(pcm) // 4) // 2 * 2])

    async def run(self):
        before = await self.budget()
        assert before["admission_estimated_usd"] + before["reserved_usd"] < 21.9, (
            "Insufficient conservative budget headroom"
        )
        providers = {}
        for role, port in (
            ("agent", self.args.agent_port),
            ("converse", self.args.converse_port),
            ("decisions", self.args.decisions_port),
        ):
            health = await self.request(port, "/health")
            info = health.get("provider")
            if info:
                assert info["local"] or (
                    info["provider"] == "meta" and info["model"] == "muse-spark-1.3-contributor"
                )
                providers[role] = info
        self.session = await self.request(
            self.args.session_port,
            "/v1/sessions",
            {"channel": "converse", "name": "Public streaming verification"},
        )
        s = self.session
        await self.request(
            self.args.session_port,
            "/v1/transcript/entries",
            {
                **{k: s[k] for k in ("session_id", "thread_id", "epoch")},
                "kind": "conversation.converse",
                "payload": {
                    "text": "A comet is a small icy body. I am still explaining its orbit.",
                    "content_scope": "public_synthetic",
                    "final": True,
                },
                "idempotency_key": str(uuid.uuid4()),
            },
            role="converse",
        )
        _, d = await self.window("Mm-hmm.", "backchannel")
        assert (d["conversation"], d["agent_route"]) == (
            "IGNORE",
            "IGNORE_FOR_IMMEDIATE_ROUTING",
        ), d
        _, d = await self.window("Kinaiya, could you... actually...", "unfinished")
        assert d["conversation"] == "WAIT_FOR_NEXT_WINDOW", d
        original = await self.submit(
            "Public synthetic test: write gate.txt containing first. Use write_file, then finish after verification."
        )
        await self.wait(
            lambda: self.task(original["task_id"]), lambda t: t["state"] == "awaiting_approval"
        )
        _, d = await self.window("Also include the documentation.", "steer")
        assert (d["conversation"], d["agent_route"]) == (
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "STEER_CURRENT_TASK",
        ), d
        revised = await self.wait(
            lambda: self.task(original["task_id"]), lambda t: t["objective_revision"] == 2
        )
        self.checks["native_steering_revision"] = revised["objective_revision"]
        urgent, d = await self.window("Stop, don't delete that!", "interrupt")
        assert (d["conversation"], d["agent_route"]) == (
            "RESPOND_INTERRUPT_USER",
            "INTERRUPT_CURRENT_TASK",
        ), d
        cancelled = await self.wait(
            lambda: self.task(original["task_id"]), lambda t: t["state"] == "cancelled"
        )
        self.checks["native_cancelled"] = cancelled["state"]

        async def reply():
            return next(
                (
                    e
                    for e in await self.entries()
                    if e["kind"] == "conversation.converse"
                    and e["payload"].get("window_source_id") == urgent["event_id"]
                ),
                None,
            )

        spoken = await self.wait(reply, lambda e: e is not None)
        first_pcm = None
        start = time.monotonic()
        async with self.http.stream(
            "POST",
            f"http://127.0.0.1:{self.args.converse_port}/v1/converse/speech",
            headers={"Authorization": "Bearer " + self.tokens["owner"]},
            json={
                "event_id": spoken["event_id"],
                "generation_epoch": spoken["payload"]["generation_epoch"],
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line and json.loads(line).get("audio"):
                    first_pcm = (time.monotonic() - start) * 1000
                    break
        assert first_pcm is not None
        self.checks["native_tts_first_pcm_ms"] = first_pcm
        gate = await self.submit(
            "Public synthetic test: write gate-first.txt containing first. Use write_file and finish after verified write."
        )
        await self.wait(
            lambda: self.task(gate["task_id"]), lambda t: t["state"] == "awaiting_approval"
        )
        audio, partial = await self.spoken_wav(
            "Once that finishes, please write hello to window dot txt."
        )
        utterance = str(uuid.uuid4())
        await self.window("", "audio_partial", utterance=utterance, audio=partial)
        full, d = await self.window(
            "", "queue_audio_full", utterance=utterance, index=1, audio=audio
        )
        assert (d["conversation"], d["agent_route"]) == (
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "QUEUE_UNTIL_TASK_DONE",
        ), d
        await self.window(
            "", "queue_audio_opening", utterance=utterance, index=2, audio=audio, speaking=False
        )
        tasks = await self.request(self.args.agent_port, "/v1/agent/tasks")
        assert len([t for t in tasks if t["session_id"] == s["session_id"]]) == 2, (
            "Followup began before boundary"
        )
        await self.approve(gate["task_id"])
        await self.wait(lambda: self.task(gate["task_id"]), lambda t: t["state"] == "succeeded")

        async def followup():
            return next(
                (
                    t
                    for t in await self.request(self.args.agent_port, "/v1/agent/tasks")
                    if t["session_id"] == s["session_id"]
                    and full["event_id"] in t["source_event_ids"]
                ),
                None,
            )

        routed = await self.wait(followup, lambda t: t is not None)
        await self.wait(
            lambda: self.task(routed["task_id"]), lambda t: t["state"] == "awaiting_approval"
        )
        await self.approve(routed["task_id"])
        completed = await self.wait(
            lambda: self.task(routed["task_id"]),
            lambda t: t["state"] in {"succeeded", "failed", "cancelled"},
        )
        assert completed["state"] == "succeeded", completed
        expected = self.args.workspace.resolve() / "window.txt"
        assert expected.read_text().strip().lower() == "hello"
        digest = hashlib.sha256(expected.read_bytes()).hexdigest()
        assert any(
            e.get("sha256") == digest and Path(e.get("path") or "").name == "window.txt"
            for e in completed["evidence"]
        )
        entries = await self.entries()
        retained = [e for e in entries if e["kind"] == "conversation.window"]
        assert len(retained) == len(self.windows)
        self.checks.update(
            {
                "all_windows_retained": len(retained),
                "verified_file": "window.txt",
                "verified_sha256": digest,
                "followup_state": completed["state"],
                "audible_ttfa_ms": None,
            }
        )
        return {
            "source_commit": self.args.source_commit,
            "providers": providers,
            "windows": self.windows,
            "checks": self.checks,
            "budget_before": before,
            "budget_after": await self.budget(),
            "limits": [
                "Public synthetic inputs only; exact-text examples plus actual rolling audio snapshots.",
                "HTTP PCM receipt is not browser audible latency; no human microphone or listening claim.",
                "One bounded provider run does not establish production model quality.",
            ],
        }


async def main(args):
    if not args.allow_billable_public_synthetic:
        raise SystemExit("Explicit --allow-billable-public-synthetic required")
    probe = Probe(args, json.loads((args.data_dir / "service-tokens.json").read_text()))
    try:
        result = await probe.run()
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result["checks"]))
    finally:
        await probe.http.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-port", type=int, default=8860)
    parser.add_argument("--agent-port", type=int, default=8861)
    parser.add_argument("--converse-port", type=int, default=8862)
    parser.add_argument("--decisions-port", type=int, default=8863)
    parser.add_argument("--voice-url", default="http://127.0.0.1:8765")
    parser.add_argument("--budget-url", default="http://127.0.0.1:9790/budget")
    parser.add_argument("--allow-billable-public-synthetic", action="store_true")
    asyncio.run(main(parser.parse_args()))
