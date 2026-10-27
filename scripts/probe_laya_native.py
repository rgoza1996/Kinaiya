"""Opt-in local Laya probe with public synthetic text and actual native file tools.

Writes isolated fixtures below a new --output directory. Agent/Converse inference
is scripted. The preceding_converse profile is the broader integration gate;
minimal is only a calibration-shaped positive control. Never sends Meta requests.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from core.policy import ToolPolicy
from packages.contracts.models import (
    AppendRequest,
    ApprovalResolution,
    SessionRequest,
    SpeechWindow,
    TaskRequest,
    TaskState,
)
from packages.providers.laya import LayaRoutingProvider
from packages.providers.text import ScriptedProvider
from services.agent import AgentRuntime
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.decisions.window_routing import WindowAgentDispatcher, WindowClassifier
from services.decisions.window_worker import WindowDecisionsWorker
from services.session.store import LocalJournal, Store


def write(name, content):
    return json.dumps({"tool": "write_file", "arguments": {"path": name, "content": content}})


def finish(revision=1):
    return json.dumps(
        {
            "tool": "finish",
            "arguments": {
                "summary": "Verified public synthetic file tool",
                "objective_revision": revision,
            },
        }
    )


class RecordingLaya(LayaRoutingProvider):
    last_wire = None
    last_response = None

    async def _request(self, method, path, **kwargs):
        self.last_wire = kwargs.get("json")
        self.last_response = await super()._request(method, path, **kwargs)
        return self.last_response


class Boundary(ScriptedProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def complete(self, messages):
        if not self.calls:
            self.entered.set()
            await self.release.wait()
        return await super().complete(messages)


async def state(agent, task_id, desired):
    async with asyncio.timeout(5):
        while True:
            task = await agent.status(task_id)
            if task.state in desired:
                return task
            await asyncio.sleep(0.01)


async def approve(store, agent, task_id):
    await state(agent, task_id, {TaskState.AWAITING_APPROVAL})
    requests = [
        e for e in store.read(kinds=["approval.requested"], limit=200) if task_id in e.task_refs
    ]
    assert len(requests) == 1
    entry = requests[0]
    await agent.resolve_approval(
        entry.payload["approval_id"],
        ApprovalResolution(
            approved=True,
            action_digest=entry.payload["action_digest"],
            objective_revision=entry.payload["objective_revision"],
        ),
    )
    return entry


async def one(provider, output, context_profile, name, text, expected_conversation, expected_route):
    folder = output / name
    folder.mkdir()
    provider.last_wire = provider.last_response = None
    workspace = folder / "workspace"
    workspace.mkdir()
    store = Store(folder / "journal.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    responses = (
        [
            write("stale.txt", "MUST BE DISCARDED"),
            write("steered.txt", "PUBLIC SYNTHETIC STEERED"),
            finish(2),
        ]
        if name == "steer"
        else [
            write("first.txt", "PUBLIC SYNTHETIC FIRST"),
            finish(),
            write("followup.txt", "PUBLIC SYNTHETIC FOLLOWUP"),
            finish(),
        ]
    )
    synthetic = Boundary(responses)
    agent = AgentRuntime(
        LocalJournal(store, "agent"), synthetic, workspace, ToolPolicy(workspace, allow_write=True)
    )
    converse = ConverseRuntime(
        LocalJournal(store, "converse"), ScriptedProvider(["I heard the public example request."])
    )
    controller = WindowConverseController(converse)
    native = WindowDecisionsWorker(
        LocalJournal(store, "decisions"), agent, WindowClassifier(provider), timeout=5
    )
    dispatcher = WindowAgentDispatcher(LocalJournal(store, "decisions"), agent)
    proof = {
        "case": name,
        "text": text,
        "agent_inference": "synthetic bounded fixture",
        "converse_inference": "synthetic bounded fixture",
        "decisions_inference": "actual local fine-tuned Laya HTTP",
        "context_profile": context_profile,
    }
    try:
        task = await agent.submit(
            TaskRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                objective="Perform the public synthetic file-tool fixture",
                idempotency_key="initial-owner-task",
            )
        )
        await asyncio.wait_for(synthetic.entered.wait(), 2)
        if name == "backchannel" or context_profile == "preceding_converse":
            store.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="conversation.converse",
                    payload={
                        "text": "I am explaining the public example; there are several parts.",
                        "content_scope": "public_synthetic",
                        "final": True,
                    },
                    idempotency_key="prior-explanation",
                ),
                "converse",
            )
        window = SpeechWindow(
            session_id=session.session_id,
            thread_id=session.thread_id,
            epoch=session.epoch,
            utterance_id="public-" + name,
            window_index=0,
            text=text,
            start_ms=0,
            end_ms=1000,
            user_speaking=name == "unfinished"
            or (name == "interrupt" and context_profile == "preceding_converse"),
            capture_ended=False,
            content_scope="public_synthetic",
        )
        source = store.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.window",
                payload=window.model_dump(mode="json"),
                idempotency_key="window",
            ),
            "owner",
        )
        began = time.perf_counter()
        await native.classify(source)
        decision = store.window_decision(source.event_id)
        assert decision is not None
        proof.update(
            classifier_seconds=time.perf_counter() - began,
            decision=decision.payload,
            native_task_id=task.task_id,
        )
        assert decision.payload["conversation"] == expected_conversation, proof
        assert decision.payload["agent_route"] == expected_route, proof
        await controller.process(decision)
        await dispatcher.process(decision)
        if name in {"unfinished", "backchannel"}:
            assert len(await agent.status()) == 1 and not list(workspace.iterdir())
            await asyncio.sleep(0.03)
            assert not store.read(kinds=["conversation.converse"], search="I heard")
            proof["no_new_work_or_reply"] = True
        elif name == "steer":
            assert not list(workspace.iterdir())
            synthetic.release.set()
            await state(agent, task.task_id, {TaskState.AWAITING_APPROVAL})
            assert not list(workspace.iterdir())
            approval = await approve(store, agent, task.task_id)
            result = await asyncio.wait_for(agent.wait(task.task_id), 5)
            assert result.state == TaskState.SUCCEEDED and result.objective_revision == 2, result
            assert not (workspace / "stale.txt").exists()
            assert (workspace / "steered.txt").read_text() == "PUBLIC SYNTHETIC STEERED"
            assert source.event_id in result.source_event_ids and result.evidence
            proof.update(
                result=result.model_dump(mode="json"),
                approval_required_before_write=True,
                approval_digest=approval.payload["action_digest"],
            )
        elif name == "interrupt":
            result = await asyncio.wait_for(agent.wait(task.task_id), 5)
            assert result.state == TaskState.CANCELLED and not list(workspace.iterdir())
            proof.update(result=result.model_dump(mode="json"), no_file_written=True)
        elif name == "queue":
            assert len(await agent.status()) == 1 and not list(workspace.iterdir())
            synthetic.release.set()
            result = await asyncio.wait_for(agent.wait(task.task_id), 5)
            assert result.state == TaskState.SUCCEEDED
            assert not (workspace / "followup.txt").exists()
            await dispatcher.tick()
            followup = next(t for t in await agent.status() if t.task_id != task.task_id)
            await state(agent, followup.task_id, {TaskState.AWAITING_APPROVAL})
            assert not (workspace / "followup.txt").exists()
            await approve(store, agent, followup.task_id)
            final = await asyncio.wait_for(agent.wait(followup.task_id), 5)
            assert final.state == TaskState.SUCCEEDED and final.source_event_ids == [
                source.event_id
            ]
            assert (workspace / "first.txt").read_text() == "PUBLIC SYNTHETIC FIRST"
            assert (workspace / "followup.txt").read_text() == "PUBLIC SYNTHETIC FOLLOWUP"
            await dispatcher.process(decision)
            await dispatcher.tick()
            assert len(await agent.status()) == 2
            proof.update(
                result=final.model_dump(mode="json"),
                queued_until_native_completion=True,
                approval_required_for_partial_followup=True,
                replay_created_no_duplicate=True,
            )
        proof["status"] = "passed"
    except BaseException as exc:
        proof.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        proof["last_laya_request"] = provider.last_wire
        proof["last_laya_response"] = provider.last_response
        proof["journal_kinds"] = [e.kind for e in store.read(limit=200)]
        proof["files"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in workspace.iterdir()
            if p.is_file()
        }
        (folder / "proof.json").write_text(json.dumps(proof, indent=2, default=str) + "\n")
        await controller.close()
        await agent.close()
        store.close()
    return proof


async def run(url, model, api_key, output, context_profile):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    provider = RecordingLaya(url, model=model, api_key=api_key)
    cases = [
        (
            "unfinished",
            "Korina, could you... actually...",
            "WAIT_FOR_NEXT_WINDOW",
            "IGNORE_FOR_IMMEDIATE_ROUTING",
        ),
        ("backchannel", "Mm-hmm.", "IGNORE", "IGNORE_FOR_IMMEDIATE_ROUTING"),
        (
            "steer",
            "Also include the documentation.",
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "STEER_CURRENT_TASK",
        ),
        (
            "interrupt",
            "Stop, don't delete that!",
            "RESPOND_INTERRUPT_USER",
            "INTERRUPT_CURRENT_TASK",
        ),
        (
            "queue",
            "Once that finishes, check the changelog.",
            "RESPOND_WITHOUT_INTERRUPTING_USER",
            "QUEUE_UNTIL_TASK_DONE",
        ),
    ]
    results = []
    try:
        for case in cases:
            try:
                result = await one(provider, output, context_profile, *case)
            except Exception:
                proof = output / case[0] / "proof.json"
                if not proof.exists():
                    raise
                result = json.loads(proof.read_text())
            results.append(result)
            print(
                case[0], result["status"], round(result.get("classifier_seconds", 0), 3), flush=True
            )
        summary = {
            "all_passed": all(r["status"] == "passed" for r in results),
            "context_profile": context_profile,
            "cases": results,
            "limits": "Actual Laya classification and native tool effects; text input and Agent/Converse inference are synthetic. Not ASR, paid inference, audio playback or human microphone proof. Minimal is a positive control, not the broader-context acceptance gate.",
        }
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
    finally:
        await provider.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8871")
    parser.add_argument("--model", default="typed-decisions")
    parser.add_argument("--key-env", default="KINAIYA_LAYA_API_KEY")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--context-profile", choices=["minimal", "preceding_converse"], default="preceding_converse"
    )
    args = parser.parse_args()
    summary = asyncio.run(
        run(
            args.url,
            args.model,
            os.environ.get(args.key_env, ""),
            args.output,
            args.context_profile,
        )
    )
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
