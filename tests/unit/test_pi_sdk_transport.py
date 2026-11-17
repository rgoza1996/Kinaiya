"""Controlled malformed I/O and lost acknowledgments at the Pi peer boundary."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from packages.agent_adapters.pi_sdk import PiCommandUnconfirmed, PiPeerUnavailable, PiSdkPeer

READY = {"version": 1, "type": "ready", "sdk_version": "1.5.2", "session_id": "original-session"}
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
async def attach():
    reader, writer = asyncio.StreamReader(), CommandStream()
    feed(reader, READY)
    peer = await PiSdkPeer.attach(reader, writer, expected_session_id=READY["session_id"])
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
