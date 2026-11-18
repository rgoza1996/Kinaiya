"""Controlled malformed I/O and lost acknowledgments at the Pi peer boundary."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from packages.agent_adapters.pi_sdk import PiCommandUnconfirmed, PiPeerUnavailable, PiSdkPeer
from packages.contracts.external_agent import (
    ExternalAgentScope,
    ExternalAgentTarget,
    ExternalAgentTranscriptPage,
)

READY = {
    "version": 1,
    "type": "ready",
    "sdk_version": "1.5.2",
    "session_id": "original-session",
    "transcript": False,
}
IDENTITY = "1" * 32


class CommandStream:
    def __init__(self):
        self.writes = []

    def write(self, raw):
        self.writes.append(json.loads(raw))

    async def drain(self):
        pass


def feed(reader, values):
    reader.feed_data(json.dumps({"version": 1, **values}).encode() + b"\n")


@asynccontextmanager
async def attach(transcript_reader=None):
    reader, writer = asyncio.StreamReader(), CommandStream()
    feed(reader, {**READY, "transcript": transcript_reader is not None})
    peer = await PiSdkPeer.attach(
        reader,
        writer,
        expected_session_id=READY["session_id"],
        transcript_reader=transcript_reader,
    )
    try:
        yield peer, reader, writer
    finally:
        reader.feed_eof()
        await peer._reader_task


async def test_pi_transport_can_recover_original_after_its_wait_expires():
    async with attach() as (peer, reader, writer):
        with pytest.raises(PiCommandUnconfirmed) as expired:
            await peer.submit(IDENTITY, "Original scope", timeout=0.01)
        assert expired.value.request_id == IDENTITY
        with pytest.raises(ValueError, match="reused with different content"):
            await peer.submit(IDENTITY, "Changed scope")
        feed(
            reader,
            {
                "type": "response",
                "id": IDENTITY,
                "disposition": "accepted",
                "reason": None,
            },
        )
        assert (await peer.submit(IDENTITY, "Original scope")).disposition == "accepted"
        (first,) = writer.writes
        assert first == {
            "version": 1,
            "command": "submit",
            "id": IDENTITY,
            "text": "Original scope",
        }


async def test_pi_transport_rejects_replacement_session_identity():
    reader, writer = asyncio.StreamReader(), CommandStream()
    feed(reader, READY)
    with pytest.raises(PiPeerUnavailable, match="session identity did not match"):
        await PiSdkPeer.attach(reader, writer, expected_session_id="different-session")
    assert writer.writes == []


async def test_bad_or_unbound_pi_event_fences_new_writes_and_keeps_the_original_uncertain():
    async with attach() as (peer, reader, writer):
        pending = asyncio.create_task(peer.submit(IDENTITY, "Original task"))
        await asyncio.sleep(0)
        feed(reader, {"type": "event", "kind": "settled", "run_id": "f" * 32})
        with pytest.raises(PiCommandUnconfirmed):
            await pending
        assert peer.events(0) == ()
        with pytest.raises(PiPeerUnavailable):
            await peer.submit("2" * 32, "Later task")
        assert [entry["id"] for entry in writer.writes] == [IDENTITY]


@pytest.mark.parametrize("settle", [False, True])
async def test_only_a_live_original_can_receive_its_own_transcript_page(settle):
    entered, returned = asyncio.Event(), asyncio.Event()
    reply_sent = asyncio.Event()

    async def scoped_reader(*, after, through_sequence, limit):
        entered.set()
        await returned.wait()
        try:
            return ExternalAgentTranscriptPage(
                contract_version=1,
                request_id="page",
                kind="transcript",
                target=ExternalAgentTarget(
                    attachment_id="a" * 32,
                    backend_session_id="original-session" if settle else "foreign-backend",
                ),
                scope=ExternalAgentScope(session_id="s", thread_id="t", epoch=1),
                after=after,
                through_sequence=0,
                next_after=after,
                has_more=False,
                entries=(),
            )
        finally:
            reply_sent.set()

    async with attach(scoped_reader) as (peer, reader, writer):
        pending = asyncio.create_task(peer.submit(IDENTITY, "Inspect my transcript"))
        await asyncio.sleep(0)
        feed(
            reader,
            {
                "type": "response",
                "id": IDENTITY,
                "disposition": "accepted",
                "reason": None,
            },
        )
        feed(reader, {"type": "event", "kind": "running", "run_id": IDENTITY})
        assert (await pending).disposition == "accepted"
        read_id = "2" * 32
        feed(
            reader,
            {
                "type": "transcript_request",
                "run_id": IDENTITY,
                "id": read_id,
                "sequence": 0,
                "byte_offset": 0,
                "through_sequence": None,
                "limit": 1,
            },
        )
        await asyncio.wait_for(entered.wait(), 2)
        (operation,) = peer._transcript_tasks.values()
        if settle:
            feed(reader, {"type": "event", "kind": "settled", "run_id": IDENTITY})
            async with asyncio.timeout(2):
                while not any(event.kind == "settled" for event in peer.events(0)):
                    await asyncio.sleep(0)
        returned.set()
        await asyncio.wait_for(reply_sent.wait(), 2)
        await asyncio.wait_for(operation, 2)
        expected = (
            []
            if settle
            else [
                {
                    "version": 1,
                    "command": "transcript_result",
                    "id": read_id,
                    "run_id": IDENTITY,
                    "page": None,
                }
            ]
        )
        assert writer.writes[1:] == expected
