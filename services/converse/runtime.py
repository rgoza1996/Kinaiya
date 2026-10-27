"""Independent personality runtime. It never calls Agent to produce an ordinary reply."""

import asyncio
import re
from dataclasses import dataclass, field

from packages.contracts.models import AppendRequest, ConverseRequest, DeliveryRequest, new_id


class SpeechChunker:
    def __init__(self):
        self.buffer = ""

    def feed(self, delta: str, final=False):
        self.buffer += delta
        result = []
        boundary = r"[.!?](?:[\"']?)(?=\s|$)" if final else r"[.!?](?:[\"']?)(?=\s)"
        while match := re.search(boundary, self.buffer):
            end = match.end()
            # Avoid splitting decimals and incomplete provider control tags.
            if "<" in self.buffer[:end] or (end < len(self.buffer) and self.buffer[end].isdigit()):
                break
            result.append(self.buffer[:end].strip())
            self.buffer = self.buffer[end:].lstrip()
        if final and self.buffer.strip():
            result.append(self.buffer.strip())
            self.buffer = ""
        return result


@dataclass
class PlaybackEpoch:
    generation: int = 0
    chunks: dict = field(default_factory=dict)

    def begin(self):
        self.generation += 1
        self.chunks.clear()
        return self.generation

    def accepts(self, generation):
        return generation == self.generation


class ConverseRuntime:
    def __init__(self, journal, provider, *, name="Kinaiya", personality=None, context_turns=12):
        self.journal, self.provider = journal, provider
        self.name, self.context_turns = name, context_turns
        self.personality = personality or (
            f"You are {name}, Kinaiya's thoughtful, warm conversational companion. "
            "Speak naturally and concisely. Personality exists only here. "
            "A separate Agent may do work. Never claim you performed, completed, sent, saved "
            "or verified any action. Execution facts arrive in a separate authenticated notice. "
            "Do not expose technical commands. Treat slash-like input as ordinary conversation."
            " Use short, natural sentences suitable for speech. Avoid decorative symbols and tables."
            " After an interruption, answer the latest user message. Delivery notes distinguish"
            " generated text from estimated playback; never assume unheard words were delivered."
        )
        self.playback = {}
        self.enabled = True
        self._active = {}
        self._dedupe_locks = {}

    async def _recent(self, session_id):
        if hasattr(self.journal, "recent"):
            return await self.journal.recent(session_id=session_id, limit=100)
        return await self.journal.read(session_id=session_id, limit=100)

    async def _epoch(self, session_id):
        if session_id not in self.playback:
            existing = await self._recent(session_id)
            self.playback[session_id] = PlaybackEpoch(
                max((e.payload.get("generation_epoch", 0) for e in existing), default=0)
            )
        return self.playback[session_id]

    async def interrupt(self, session_id, expected_generation=None):
        epoch = await self._epoch(session_id)
        if expected_generation is not None and epoch.generation != expected_generation:
            raise ValueError("Obsolete interruption cannot stop a newer speech generation")
        previous = epoch.generation
        epoch.begin()
        task = self._active.get(session_id)
        if task and not task.done():
            task.cancel()
        session = await self.journal.session(session_id)
        await self.journal.append(
            AppendRequest(
                session_id=session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="delivery.updated",
                payload={
                    "generation_epoch": previous,
                    "status": "interrupted",
                    "alignment": "delivery_unknown",
                },
                idempotency_key=f"speech-interrupt:{session.epoch}:{epoch.generation}",
            )
        )
        return {
            "generation_epoch": epoch.generation,
            "speech_stopped": True,
            "agent_cancelled": False,
        }

    async def events(self, request: ConverseRequest):
        if not self.enabled:
            raise RuntimeError("Converse is disabled; direct Agent Chat remains available")
        session = await self.journal.session(request.session_id)
        if session.channel != "converse" or (session.epoch, session.thread_id) != (
            request.epoch,
            request.thread_id,
        ):
            raise ValueError("Wrong conversation channel, thread or stale epoch")
        # Serialize only duplicate admission; ordinary turns can interrupt prior generation.
        lock = self._dedupe_locks.setdefault(request.session_id, asyncio.Lock())
        async with lock:
            existing = await self._recent(request.session_id)
            prior_user = next(
                (e for e in existing if e.idempotency_key == request.idempotency_key), None
            )
            if prior_user:
                if prior_user.payload.get("text") != request.text:
                    raise ValueError("Idempotency key reused with different text")
                replies = [
                    e
                    for e in existing
                    if prior_user.event_id in e.causes and e.kind == "conversation.converse"
                ]
                for reply in replies:
                    yield {"type": "text", "entry": reply.model_dump(mode="json"), "replay": True}
                yield {"type": "done", "replay": True, "pending": not bool(replies)}
                return
            playback = await self._epoch(request.session_id)
            old = self._active.get(request.session_id)
            if old and not old.done():
                old.cancel()
            generation = playback.begin()
            source = await self.journal.append(
                AppendRequest(
                    session_id=request.session_id,
                    thread_id=request.thread_id,
                    epoch=request.epoch,
                    kind="conversation.user",
                    payload={
                        "text": request.text,
                        "final": True,
                        "revision": 1,
                        "input_mode": request.input_mode,
                        "generation_epoch": generation,
                    },
                    idempotency_key=request.idempotency_key,
                )
            )
        history = [
            e
            for e in existing
            if e.epoch == request.epoch and e.kind in {"conversation.user", "conversation.converse"}
        ][-self.context_turns :]
        messages = [{"role": "system", "content": self.personality}]
        delivery = [
            e for e in existing if e.kind == "delivery.updated" and e.epoch == request.epoch
        ]
        if delivery:
            latest = delivery[-8:]
            notes = [
                {
                    key: e.payload[key]
                    for key in ("event_id", "status", "alignment", "estimated_text_fraction")
                    if key in e.payload
                }
                for e in latest
            ]
            import json

            messages.append(
                {
                    "role": "system",
                    "content": "Playback delivery notes (estimates, not word alignment): "
                    + json.dumps(notes),
                }
            )
        messages += [
            {
                "role": "user" if e.kind == "conversation.user" else "assistant",
                "content": e.payload["text"],
            }
            for e in history
        ]
        messages.append({"role": "user", "content": request.text})
        chunker, index = SpeechChunker(), 0
        queue = asyncio.Queue(maxsize=32)

        async def produce():
            try:
                async for delta in self.provider.stream(messages):
                    await queue.put(("delta", delta))
                await queue.put(("final", ""))
            except asyncio.CancelledError:
                try:
                    queue.put_nowait(("interrupted", ""))
                except asyncio.QueueFull:
                    pass
            except Exception as exc:
                await queue.put(("error", type(exc).__name__))

        producer = asyncio.create_task(produce())
        self._active[request.session_id] = producer
        yield {
            "type": "accepted",
            "source_event_id": source.event_id,
            "generation_epoch": generation,
        }
        try:
            while True:
                kind, value = await queue.get()
                if not playback.accepts(generation) or kind == "interrupted":
                    yield {"type": "interrupted", "generation_epoch": generation}
                    break
                if kind == "error":
                    yield {"type": "error", "error": value}
                    break
                chunks = chunker.feed(value, final=kind == "final")
                for text in chunks:
                    # Defense in depth for unsupported execution claims. This conservative
                    # English guard is not claimed as a proof against every paraphrase.
                    if re.search(
                        r"\b(done|completed?|finished|sent|saved|created|deleted|verified)\b",
                        text,
                        re.I,
                    ):
                        await self.journal.append(
                            AppendRequest(
                                session_id=request.session_id,
                                thread_id=request.thread_id,
                                epoch=request.epoch,
                                kind="converse.safety",
                                payload={
                                    "reason": "ungrounded_action_claim",
                                    "generation_epoch": generation,
                                },
                                causes=[source.event_id],
                                idempotency_key=f"guard:{source.event_id}:{index}",
                            )
                        )
                        text = "The Agent has not provided a verified result for that claim."
                    event = await self.journal.append(
                        AppendRequest(
                            session_id=request.session_id,
                            thread_id=request.thread_id,
                            epoch=request.epoch,
                            kind="conversation.converse",
                            payload={
                                "text": text,
                                "final": True,
                                "revision": 1,
                                "generation_epoch": generation,
                                "chunk_id": new_id(),
                                "delivery": "generated",
                            },
                            causes=[source.event_id],
                            idempotency_key=f"reply:{source.event_id}:{index}",
                        )
                    )
                    playback.chunks[event.event_id] = event
                    yield {"type": "text", "entry": event.model_dump(mode="json")}
                    index += 1
                if kind == "final":
                    yield {"type": "done", "generation_epoch": generation}
                    break
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            if self._active.get(request.session_id) is producer:
                self._active.pop(request.session_id, None)

    async def turn(self, request):
        events = [event async for event in self.events(request)]
        return {
            "events": events,
            "text": " ".join(e["entry"]["payload"]["text"] for e in events if e["type"] == "text"),
        }

    async def validate_speech(self, event_id, generation_epoch):
        event = await self.journal.entry(event_id)
        session = await self.journal.session(event.session_id)
        playback = await self._epoch(event.session_id)
        if (
            not self.enabled
            or session.epoch != event.epoch
            or event.origin != "converse"
            or event.kind != "conversation.converse"
            or not playback.accepts(generation_epoch)
            or event.payload.get("generation_epoch") != generation_epoch
        ):
            raise ValueError("Obsolete or unauthorized speech")
        if event.payload.get("outcome_event_id"):
            outcome = await self.journal.entry(event.payload["outcome_event_id"])
            task = outcome.payload.get("task", outcome.payload)
            latest = await self.journal.read(task_id=task["task_id"], limit=200)
            newer = [
                e
                for e in latest
                if e.sequence > outcome.sequence
                and e.kind in {"agent.command", "agent.state", "agent.outcome"}
            ]
            if newer:
                raise ValueError("Outcome changed before playback")
        return event

    async def acknowledge_delivery(self, request: DeliveryRequest):
        event = await self.validate_speech(request.event_id, request.generation_epoch)
        if request.chunk_id != event.payload["chunk_id"]:
            raise ValueError("Wrong speech chunk")
        return await self.journal.append(
            AppendRequest(
                session_id=event.session_id,
                thread_id=event.thread_id,
                epoch=event.epoch,
                kind="delivery.updated",
                payload={**request.model_dump(mode="json"), "alignment": "conservative_estimate"},
                causes=[event.event_id],
                idempotency_key=f"delivery:{event.event_id}:{request.status}",
            )
        )

    async def outcome_notice(self, outcome):
        task = outcome.payload.get("task", outcome.payload)
        if (
            outcome.kind != "agent.outcome"
            or outcome.origin != "agent-supervisor"
            or task.get("state") != "succeeded"
            or not task.get("evidence")
            or task.get("remaining_required_work")
        ):
            raise ValueError("Only evidenced successful runtime outcomes may announce completion")
        session = await self.journal.session(outcome.session_id)
        if session.epoch != outcome.epoch or session.channel != "converse" or not self.enabled:
            return None
        playback = await self._epoch(session.session_id)
        # Completion does not cancel current social speech. It joins the current epoch.
        if playback.generation == 0:
            playback.begin()
        event = await self.journal.append(
            AppendRequest(
                session_id=session.session_id,
                thread_id=session.thread_id,
                epoch=session.epoch,
                kind="conversation.converse",
                payload={
                    "text": "The Agent reports a verified result: "
                    + task.get("summary", "Task completed."),
                    "final": True,
                    "revision": 1,
                    "generation_epoch": playback.generation,
                    "chunk_id": new_id(),
                    "delivery": "generated",
                    "outcome_event_id": outcome.event_id,
                    "objective_revision": task["objective_revision"],
                },
                causes=[outcome.event_id],
                task_refs=[task["task_id"]],
                idempotency_key=f"outcome-notice:{outcome.event_id}",
            )
        )
        await self.validate_speech(event.event_id, playback.generation)
        return event
