"""Content-controlled speech, independent of work dispatch and clip silence timers."""

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from packages.contracts.models import AppendRequest, SpeechWindow, WindowDecision
from packages.providers.disclosure import scope_allowed
from services.converse.runtime import SpeechChunker


def allowed_scope(provider, scope):
    return scope_allowed(provider.info, scope)


def unverified_work_claim(text, work_routed):
    # This is a conservative guard for explicit effect claims, not a blanket
    # ban on ordinary words such as "well done" or "finished my introduction".
    actor = r"(?:i(?:'ve)?|we(?:'ve)?|the agent)"
    verb = r"(?:(?:have|has|just|already)\s+){0,2}(?:completed?|finished|sent|saved|created|deleted|verified)"
    obj = r"(?:\s+(?:the|your|a|an|those|that|requested)){0,2}\s+(?:task|work|job|file|email|message|document|script|changes|report)\b"
    return bool(
        re.search(rf"\b{actor}\s+{verb}{obj}", text, re.I)
        or re.search(
            r"\b(?:the )?(?:agent|task|work|job)\s+(?:(?:is|was|has|been|now)\s+){0,3}(?:done|completed?|finished)\b",
            text,
            re.I,
        )
        or (work_routed and re.fullmatch(r"\s*(?:done|completed?|finished)[.!]?\s*", text, re.I))
    )


@dataclass
class PreparedReply:
    source: object
    decision: object
    behavior: str
    base_generation: int
    chunks: list[str] = field(default_factory=list)
    published: int = 0
    generation: int | None = None
    started: bool = False
    task: object = None


class WindowConverseController:
    def __init__(self, runtime):
        self.runtime, self.journal = runtime, runtime.journal
        self.pending = {}
        self.after = 0
        self.last_error = None
        self.locks = {}

    async def ingest(self, window: SpeechWindow):
        return await self.journal.append(
            AppendRequest(
                session_id=window.session_id,
                thread_id=window.thread_id,
                epoch=window.epoch,
                kind="conversation.window",
                payload=window.model_dump(mode="json"),
                idempotency_key=f"speech-window:{window.epoch}:{window.utterance_id}:{window.window_index}:r{window.revision}",
            )
        )

    async def state(self, source, phase, **fields):
        w = SpeechWindow.model_validate(source.payload)
        return await self.journal.append(
            AppendRequest(
                session_id=source.session_id,
                thread_id=source.thread_id,
                epoch=source.epoch,
                kind="converse.window_state",
                causes=[source.event_id],
                payload={
                    "source_event_id": source.event_id,
                    "utterance_id": w.utterance_id,
                    "phase": phase,
                    "runtime_id": self.runtime._runtime_id,
                    **fields,
                },
                idempotency_key=f"window-response:{source.event_id}:{phase}:{self.runtime._runtime_id}",
            )
        )

    async def current(self, prepared):
        source = prepared.source
        session = await self.journal.session(source.session_id)
        if not self.runtime.enabled or (session.epoch, session.thread_id) != (
            source.epoch,
            source.thread_id,
        ):
            return None
        original = await self.journal.resolve(source.event_id)
        if original.event_id != source.event_id:
            return None
        control = await self.journal.window_control(source.session_id, source.epoch)
        if control is not None and control.payload["source_sequence"] > source.sequence:
            newer = await self.journal.resolve(control.payload["source_event_id"])
            if control.payload["conversation"] == "WAIT_FOR_NEXT_WINDOW" or (
                newer.payload.get("utterance_id"),
                newer.payload.get("text"),
            ) != (source.payload.get("utterance_id"), source.payload.get("text")):
                return None
        w = SpeechWindow.model_validate(source.payload)
        latest = await self.journal.latest_window(source.session_id, source.epoch, w.utterance_id)
        if latest is None or latest.payload["text"] != w.text:
            return None
        return SpeechWindow.model_validate(latest.payload)

    async def yield_speech(self, session_id):
        epoch = await self.runtime._epoch(session_id)
        if self.runtime._active.get(session_id) is not None or epoch.chunks:
            await self.runtime.interrupt(session_id)

    async def flush(self, prepared):
        source = prepared.source
        lock = self.locks.setdefault(source.session_id, asyncio.Lock())
        async with lock:
            latest = await self.current(prepared)
            if latest is None:
                return
            epoch = await self.runtime._epoch(source.session_id)
            if prepared.generation is None:
                if (
                    prepared.behavior == "RESPOND_WITHOUT_INTERRUPTING_USER"
                    and latest.user_speaking
                ):
                    return
                if epoch.generation != prepared.base_generation or not prepared.chunks:
                    return
                if await self.journal.window_response(
                    source.session_id, source.epoch, latest.utterance_id
                ):
                    return
                prepared.generation = epoch.begin()
                self.runtime._active[source.session_id] = prepared.task
                await self.state(
                    source,
                    "started",
                    generation_epoch=prepared.generation,
                    behavior=prepared.behavior,
                )
                prepared.started = True
            if not prepared.started:
                recorded = await self.journal.window_response(
                    source.session_id, source.epoch, latest.utterance_id
                )
                if (
                    recorded is None
                    or recorded.payload.get("source_event_id") != source.event_id
                    or recorded.payload.get("runtime_id") != self.runtime._runtime_id
                ):
                    return
                prepared.started = True
            if not epoch.accepts(prepared.generation):
                return
            while prepared.published < len(prepared.chunks):
                index = prepared.published
                text = prepared.chunks[index]
                if unverified_work_claim(
                    text, prepared.decision.payload["agent_route"] != "IGNORE_FOR_IMMEDIATE_ROUTING"
                ):
                    text = "The Agent has not provided a verified result for that claim."
                entry = await self.journal.append(
                    AppendRequest(
                        session_id=source.session_id,
                        thread_id=source.thread_id,
                        epoch=source.epoch,
                        kind="conversation.converse",
                        causes=[source.event_id],
                        payload={
                            "text": text,
                            "final": True,
                            "revision": 1,
                            "generation_epoch": prepared.generation,
                            "runtime_id": self.runtime._runtime_id,
                            "chunk_id": hashlib.sha256(
                                f"{source.event_id}:{index}".encode()
                            ).hexdigest()[:32],
                            "delivery": "generated",
                            "window_source_id": source.event_id,
                            "window_utterance_id": latest.utterance_id,
                            "window_behavior": prepared.behavior,
                            "content_scope": latest.content_scope,
                        },
                        idempotency_key=f"window-reply:{source.event_id}:{index}",
                    )
                )
                epoch.chunks[entry.event_id] = entry
                prepared.published += 1

    async def generate(self, prepared):
        source = prepared.source
        try:
            w = SpeechWindow.model_validate(source.payload)
            if not allowed_scope(self.runtime.provider, w.content_scope):
                raise ValueError("Window disclosure does not permit this provider")
            await self.state(source, "preparing", behavior=prepared.behavior)
            recent = await self.journal.recent(session_id=source.session_id, limit=100)
            context = []
            seen = set()
            for entry in reversed(recent):
                if entry.epoch != source.epoch or entry.sequence >= source.sequence:
                    continue
                scope = entry.payload.get("content_scope", "local_only")
                if not allowed_scope(self.runtime.provider, scope):
                    continue
                if entry.kind == "conversation.window":
                    old = SpeechWindow.model_validate(entry.payload)
                    if old.utterance_id == w.utterance_id or old.utterance_id in seen:
                        continue
                    seen.add(old.utterance_id)
                    context.append({"role": "user", "content": old.text[:2000]})
                elif entry.kind in {"conversation.user", "conversation.converse"}:
                    context.append(
                        {
                            "role": "user" if entry.kind == "conversation.user" else "assistant",
                            "content": entry.payload["text"][:2000],
                        }
                    )
                if len(context) >= 8:
                    break
            messages = [
                {"role": "system", "content": self.runtime.personality},
                *reversed(context),
                {"role": "user", "content": w.text},
            ]
            chunker = SpeechChunker()
            size = 0
            async for delta in self.runtime.provider.stream(messages):
                size += len(delta)
                if size > 16000:
                    raise ValueError("Prepared reply exceeded its bound")
                prepared.chunks.extend(chunker.feed(delta))
                await self.flush(prepared)
            prepared.chunks.extend(chunker.feed("", final=True))
            await self.flush(prepared)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = type(exc).__name__
            try:
                await self.state(source, "error", reason=type(exc).__name__)
            except Exception:
                pass
        finally:
            if self.runtime._active.get(source.session_id) is prepared.task:
                self.runtime._active.pop(source.session_id, None)

    async def process(self, entry):
        if entry.kind == "conversation.window":
            w = SpeechWindow.model_validate(entry.payload)
            latest = await self.journal.latest_window(entry.session_id, entry.epoch, w.utterance_id)
            if latest is None or latest.event_id != entry.event_id:
                return
            p = self.pending.get(entry.session_id)
            if p is not None:
                old = SpeechWindow.model_validate(p.source.payload)
                if w.utterance_id == old.utterance_id:
                    if w.text != old.text and p.generation is None:
                        p.task.cancel()
                        self.pending.pop(entry.session_id, None)
                    else:
                        await self.flush(p)
            return
        if entry.kind != "routing.window_decided" or entry.origin != "decisions":
            return
        d = WindowDecision.model_validate(entry.payload)
        source = await self.journal.resolve(d.source_event_id)
        if (
            source.kind != "conversation.window"
            or source.origin != "authenticated-user"
            or (
                source.session_id,
                source.thread_id,
                source.epoch,
                source.sequence,
                source.payload["revision"],
            )
            != (
                entry.session_id,
                entry.thread_id,
                entry.epoch,
                d.source_sequence,
                d.source_revision,
            )
        ):
            return
        if (datetime.now(UTC) - source.created_at).total_seconds() > 60 or not self.runtime.enabled:
            return
        w = SpeechWindow.model_validate(source.payload)
        epoch = await self.runtime._epoch(source.session_id)
        candidate = PreparedReply(source, entry, d.conversation, epoch.generation)
        if await self.current(candidate) is None:
            return
        if d.conversation == "IGNORE":
            return
        old = self.pending.get(source.session_id)
        if d.conversation == "WAIT_FOR_NEXT_WINDOW":
            if old is not None:
                old.task.cancel()
                self.pending.pop(source.session_id, None)
            await self.yield_speech(source.session_id)
            await self.state(source, "waiting")
            return
        if await self.journal.window_response(source.session_id, source.epoch, w.utterance_id):
            return
        if (
            old is not None
            and old.source.payload["utterance_id"] == w.utterance_id
            and old.source.payload["text"] == w.text
        ):
            old.behavior = d.conversation
            await self.flush(old)
            return
        if old is not None:
            old.task.cancel()
        await self.yield_speech(source.session_id)
        candidate.base_generation = (await self.runtime._epoch(source.session_id)).generation
        candidate.task = asyncio.create_task(self.generate(candidate))
        self.pending[source.session_id] = candidate
        if len(self.pending) > 32:
            key = next(iter(self.pending))
            dropped = self.pending.pop(key)
            dropped.task.cancel()

    async def tick(self):
        entries = await self.journal.read(after=self.after, limit=100)
        for entry in entries:
            self.after = entry.sequence
            try:
                await self.process(entry)
            except Exception as exc:
                self.last_error = type(exc).__name__
        for p in list(self.pending.values()):
            await self.flush(p)
        return len(entries)

    async def close(self):
        for p in self.pending.values():
            p.task.cancel()
        await asyncio.gather(*(p.task for p in self.pending.values()), return_exceptions=True)
        self.pending.clear()

    async def run(self):
        try:
            while True:
                try:
                    await self.tick()
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    await asyncio.sleep(0.2)
        finally:
            await self.close()
