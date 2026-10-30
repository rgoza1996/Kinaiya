"""Opt-in native count/generation/cancel proof; synthetic local journal only.

No model loads, settings changes, private journals, cloud or speech workers.
The selected resident model must fit the public 24-message fixture with its
complete persona/template and 128 output tokens. This checks retention and
lifecycle; it does not qualify model quality or physical conversational latency.
"""

import asyncio
import copy
import hashlib
import json
import os
import subprocess
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.models import (
    AppendRequest,
    ConverseRequest,
    SessionRequest,
    SpeechWindow,
    WindowDecision,
)
from packages.providers.lmstudio import LMStudioProvider
from services.common.auth import Auth
from services.converse.runtime import ConverseRuntime
from services.converse.windows import WindowConverseController
from services.session.api import create_app
from services.session.store import Store

BASE = os.environ.get("KINAIYA_LMSTUDIO_URL", "http://127.0.0.1:1234")
MODEL = os.environ.get("KINAIYA_LMSTUDIO_MODEL", "kinaiya-converse-gpu")


class PublicProvider(LMStudioProvider):
    def __init__(self):
        super().__init__(BASE, MODEL, max_tokens=128)
        self.counts = []
        self.prompts = []
        self.terminals = []
        self.creations = []

    async def measure_prompt(self, messages):
        count = await super().measure_prompt(messages)
        self.counts.append((copy.deepcopy(messages), count))
        return count

    async def stream(self, messages):
        self.prompts.append(copy.deepcopy(messages))
        async with aclosing(super().stream(messages)) as iterator:
            async for fragment in iterator:
                yield fragment

    async def _send(self, op, packet):
        if packet.get("type") == "channelCreate":
            self.creations.append(copy.deepcopy(packet["creationParameter"]))
        return await super()._send(op, packet)

    async def _receive(self, op):
        packet = await super()._receive(op)
        if (
            packet.get("type") == "channelSend"
            and packet.get("message", {}).get("type") == "success"
        ):
            terminal = packet["message"]
            self.terminals.append(
                {
                    "stats": terminal["stats"],
                    "instance_sha256": hashlib.sha256(
                        terminal["modelInfo"]["instanceReference"].encode()
                    ).hexdigest(),
                }
            )
        return packet


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("KINAIYA_LIVE_LMSTUDIO_CONTEXT") != "1",
    reason="Requires explicit already-resident local-model integration opt-in",
)
async def test_native_leading_system_sections_preserve_count_and_generation(tmp_path):
    p = PublicProvider()
    sections = [
        "Public synthetic character instructions: reply briefly.",
        "Public synthetic recall: the test color is blue.",
        "Playback qualification: the prior response was interrupted; it was not fully heard.",
    ]
    messages = [{"role": "system", "content": section} for section in sections] + [
        {"role": "user", "content": "Say hello in one short sentence."}
    ]
    original = copy.deepcopy(messages)
    try:
        measured = await p.measure_prompt(messages)
        reply = await p.complete(messages)
        assert reply and messages == original
        assert len(p.creations) == len(p.terminals) == 1
        history = p.creations[0]["history"]["messages"]
        assert [m["role"] for m in history] == ["system", "user"]
        assert history[0]["content"] == [{"type": "text", "text": "\n\n".join(sections)}]
        assert p.terminals[0]["stats"]["promptTokensCount"] == measured.input_tokens
        assert p.terminals[0]["stats"]["stopReason"] in {"eosFound", "stopStringFound"}
        assert p._active is None and not p._uncertain
        receipt = {
            "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "public_synthetic_only": True,
            "raw_system_sections": len(sections),
            "native_system_messages": 1,
            "input_tokens": measured.input_tokens,
            "context_tokens": measured.context_tokens,
            "public_reply": reply,
            "terminal_stats": p.terminals[0]["stats"],
            "source_unchanged": messages == original,
            "cleanup_confirmed": True,
        }
        (tmp_path / "system-prefix-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        proof = os.environ.get("KINAIYA_CONTEXT_PROOF_DIR")
        if proof:
            destination = Path(proof)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "system-prefix-receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
    finally:
        await p.close()


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("KINAIYA_LIVE_LMSTUDIO_CONTEXT") != "1",
    reason="Requires explicit already-resident local-model integration opt-in",
)
async def test_native_context_history_and_request_owned_cancellation(tmp_path):
    ROOT = tmp_path
    store = Store(ROOT / "public-history.sqlite")
    session = store.create_session(SessionRequest(channel="converse"))
    auth = Auth({"owner": "o" * 32, "converse": "c" * 32})
    journal = JournalClient(
        "http://fixture", "c" * 32, transport=httpx.ASGITransport(app=create_app(store, auth))
    )
    p = PublicProvider()
    runtime = ConverseRuntime(journal, p, context_budgeted=True)
    windows = WindowConverseController(runtime)
    scope = dict(session_id=session.session_id, thread_id=session.thread_id, epoch=session.epoch)
    try:
        for i in range(24):
            store.append(
                AppendRequest(
                    **scope,
                    kind="conversation.user",
                    payload={
                        "text": f"Public test item {i}.",
                        "revision": 1,
                        "final": True,
                        "content_scope": "public_synthetic",
                    },
                    idempotency_key=f"public-history-{i}",
                ),
                "owner",
            )
        for i in range(350):
            store.append(
                AppendRequest(
                    **scope,
                    kind="converse.window_state",
                    payload={"phase": "synthetic-status", "index": i},
                    idempotency_key=f"public-noise-{i}",
                ),
                "converse",
            )
        result = await runtime.turn(
            ConverseRequest(
                **scope,
                text="Please say hello in one short sentence.",
                content_scope="public_synthetic",
                idempotency_key="public-typed",
            )
        )
        assert result["text"] and len(p.prompts) == 1, result
        first = copy.deepcopy(p.prompts[0])
        assert len(first[1:-1]) == 24
        assert first == p.counts[-1][0]
        typed_receipt = {
            "history_messages": 24,
            "input_tokens": p.counts[-1][1].input_tokens,
            "count": p.counts[-1][1].to_wire(),
            "public_reply": result["text"],
        }
        source = await windows.ingest(
            SpeechWindow(
                **scope,
                utterance_id="public-window",
                window_index=0,
                text="Please say good evening in one short sentence.",
                start_ms=0,
                end_ms=1000,
                user_speaking=False,
                capture_ended=True,
                content_scope="public_synthetic",
            )
        )
        d = WindowDecision(
            source_event_id=source.event_id,
            source_sequence=source.sequence,
            source_revision=1,
            conversation="RESPOND_WITHOUT_INTERRUPTING_USER",
            agent_route="IGNORE_FOR_IMMEDIATE_ROUTING",
        )
        event = store.append(
            AppendRequest(
                **scope,
                kind="routing.window_decided",
                causes=[source.event_id],
                payload=d.model_dump(mode="json"),
                idempotency_key="public-window-decision",
            ),
            "decisions",
        )
        await windows.process(event)
        await asyncio.wait_for(asyncio.gather(*(v.task for v in windows.pending.values())), 15)
        await windows.tick()
        assert len(p.prompts) == 2, (windows.last_error, len(p.prompts))
        assert len([m for m in p.prompts[-1] if m["role"] != "system"]) > 10
        assert p.prompts[-1] == p.counts[-1][0]
        assert len(p.terminals) == 2 and all(
            t["stats"]["stopReason"] == "eosFound" for t in p.terminals
        )
        assert p._active is None and not p._uncertain
        # Real request-owned cancellation: close after the first native text fragment.
        cancellation = [
            {"role": "system", "content": "Public synthetic cancellation test."},
            {"role": "user", "content": "Count from one to one hundred in words."},
        ]
        await p.measure_prompt(cancellation)
        iterator = p.stream(cancellation)
        first_fragment = await anext(iterator)
        await iterator.aclose()
        assert p._active is None and not p._uncertain
        receipt = {
            "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "typed": typed_receipt,
            "live": {
                "prompt_messages": len(p.prompts[1]),
                "history_messages": len([m for m in p.prompts[1][1:-1] if m["role"] != "system"]),
                "count": p.counts[-2][1].to_wire(),
            },
            "terminal_records": p.terminals,
            "cancellation_first_fragment_chars": len(first_fragment),
            "cancellation_cleanup_confirmed": p._active is None and not p._uncertain,
            "only_public_synthetic": True,
            "human_quality_not_asserted": True,
        }
        (ROOT / "history-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        (ROOT / "public-prompts.json").write_text(json.dumps(p.prompts, indent=2) + "\n")
        proof = os.environ.get("KINAIYA_CONTEXT_PROOF_DIR")
        if proof:
            destination = Path(proof)
            destination.mkdir(parents=True, exist_ok=True)
            for name in ("history-receipt.json", "public-prompts.json"):
                (destination / name).write_bytes((ROOT / name).read_bytes())
    finally:
        await windows.close()
        await runtime.close()
        await p.close()
        await journal.close()
        store.close()
