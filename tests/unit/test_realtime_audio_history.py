"""#626: exact role-bearing history negotiation before current PCM admission."""

import asyncio
import copy
import hashlib
import json

import pytest
from test_realtime_audio_provider import (
    CURRENT_TEXT,
    PCM,
    Connector,
    Current,
    Socket,
    audio,
    messages,
    provider,
    session,
    wait,
    wire_pcm,
)

from packages.providers.text import ProviderRefusal

CAPABILITIES = {
    "contract": "native_history.v1",
    "max_messages": 4094,
    "max_utf8_bytes": 1048576,
}


def digest(history):
    return hashlib.sha256(
        json.dumps(history, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class HistorySocket(Socket):
    def __init__(self, *, capabilities=None, initial=None, change_ack=None, **kwargs):
        created = session()
        created["native_history"] = copy.deepcopy(
            CAPABILITIES if capabilities is None else capabilities
        )
        original = [] if initial is None else initial
        created["history_count"] = len(original)
        created["history_sha256"] = digest(original)
        self.change_ack = change_ack
        super().__init__(created={"type": "session.created", "session": created}, **kwargs)

    def push(self, event):
        if type(event) is dict and event.get("type") == "session.updated":
            event = copy.deepcopy(event)
            if "history" in event["session"]:
                history = event["session"].pop("history")
                event["session"].update(history_count=len(history), history_sha256=digest(history))
        if (
            type(event) is dict
            and event.get("type") == "session.updated"
            and self.change_ack is not None
        ):
            event = copy.deepcopy(event)
            self.change_ack(event["session"])
        super().push(event)


def selected(socket):
    return next(e["session"] for e in socket.sent if e["type"] == "session.update")


async def test_capable_endpoint_receives_exact_separate_roles_and_original_pcm():
    socket = HistorySocket()
    target = provider(Connector(socket))
    original = messages()
    try:
        reply = await target.respond(audio(), original, current=Current())
        request = selected(socket)
        assert request["instructions"] == original[0]["content"]
        assert request["history"] == original[1:-1]
        assert request["native_history"] == CAPABILITIES
        assert wire_pcm(socket) == PCM
        assert CURRENT_TEXT not in json.dumps(socket.sent)
        assert reply.history_mode == "native_roles_v1"
        assert socket.close_calls == 1
    finally:
        await target.close()


async def test_unadvertised_endpoint_retains_explicit_legacy_representation():
    socket = Socket()
    target = provider(Connector(socket))
    try:
        reply = await target.respond(audio(), messages(), current=Current())
        request = selected(socket)
        assert "history" not in request and "native_history" not in request
        assert '"role":"assistant"' in request["instructions"]
        assert reply.history_mode == "system_json"
    finally:
        await target.close()


async def test_every_fitting_role_and_literal_placeholder_is_retained_in_order():
    history = [{"role": "system", "content": "Public character. @@USER@@ @@REPLY@@"}]
    for i in range(36):
        history.append(
            {
                "role": ("user", "assistant", "system")[i % 3],
                "content": f"Prior {i}: café / @@USER@@ / @@REPLY@@",
            }
        )
    history.append({"role": "user", "content": CURRENT_TEXT})
    socket = HistorySocket()
    target = provider(Connector(socket))
    try:
        await target.respond(audio(), history, current=Current())
        assert selected(socket)["history"] == history[1:-1]
        assert selected(socket)["instructions"] == history[0]["content"]
    finally:
        await target.close()


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {**CAPABILITIES, "contract": "native_history.v2"},
        {**CAPABILITIES, "max_messages": True},
        {**CAPABILITIES, "max_messages": -1},
        {**CAPABILITIES, "max_utf8_bytes": False},
        {**CAPABILITIES, "max_utf8_bytes": 1048577},
        {**CAPABILITIES, "extra": "unqualified"},
    ],
)
async def test_bad_advertised_capability_refuses_without_disclosure_or_downgrade(capabilities):
    socket = HistorySocket(capabilities=capabilities)
    connector = Connector(socket)
    target = provider(connector)
    try:
        with pytest.raises(ProviderRefusal):
            await target.respond(audio(), messages(), current=Current())
        assert socket.sent == [] and len(connector.calls) == 1
        assert socket.close_calls == 1
    finally:
        await target.close()


@pytest.mark.parametrize(
    "capabilities",
    [
        {**CAPABILITIES, "max_messages": 1},
        {**CAPABILITIES, "max_utf8_bytes": 10},
    ],
)
async def test_peer_bound_refuses_complete_history_instead_of_truncating(capabilities):
    socket = HistorySocket(capabilities=capabilities)
    target = provider(Connector(socket))
    try:
        with pytest.raises(ProviderRefusal):
            await target.respond(audio(), messages(), current=Current())
        assert socket.sent == []
    finally:
        await target.close()


async def test_initial_nonempty_history_cannot_supply_an_unbound_conversation():
    socket = HistorySocket(initial=[{"role": "assistant", "content": "Unknown old state"}])
    target = provider(Connector(socket))
    try:
        with pytest.raises(ProviderRefusal):
            await target.respond(audio(), messages(), current=Current())
        assert socket.sent == []
    finally:
        await target.close()


@pytest.mark.parametrize(
    "damage", ["drop", "reorder", "role", "text", "inject", "capability", "count", "boolean_count"]
)
async def test_changed_history_ack_refuses_before_pcm_and_never_retries_legacy(damage):
    def change(ack):
        if damage == "drop":
            ack.pop("history_sha256", None)
        elif damage == "capability":
            ack["native_history"] = {**CAPABILITIES, "max_messages": 10}
        elif damage in {"count", "boolean_count"}:
            ack["history_count"] = 3 if damage == "count" else True
        else:
            history = messages()[1:-1]
            if damage == "reorder":
                history.reverse()
            elif damage == "role":
                history[1]["role"] = "user"
            elif damage == "text":
                history[0]["content"] = "Server changed the input"
            else:
                history.append({"role": "user", "content": CURRENT_TEXT})
            ack["history_sha256"] = digest(history)

    socket = HistorySocket(change_ack=change)
    connector = Connector(socket)
    target = provider(connector)
    try:
        with pytest.raises(ProviderRefusal):
            await target.respond(audio(), messages(), current=Current())
        assert wire_pcm(socket) == b"" and len(connector.calls) == 1
        assert [e["type"] for e in socket.sent] == ["session.update"]
    finally:
        await target.close()


async def test_authorized_history_is_frozen_before_transport_acquisition():
    original = messages()
    expected = copy.deepcopy(original)
    socket = HistorySocket()
    connector = Connector(socket)
    connector.release.clear()
    target = provider(connector)
    running = asyncio.create_task(target.respond(audio(), original, current=Current()))
    try:
        await wait(connector.entered)
        original[0]["content"] = "Changed personality"
        original[1]["role"] = "assistant"
        original[2]["content"] = "Changed history"
        connector.release.set()
        await running
        assert selected(socket)["instructions"] == expected[0]["content"]
        assert selected(socket)["history"] == expected[1:-1]
    finally:
        connector.release.set()
        await target.close()
        await asyncio.gather(running, return_exceptions=True)


async def test_stale_history_ack_does_not_admit_pcm():
    socket = HistorySocket(held_type="session.updated")
    authority = Current()
    target = provider(Connector(socket))
    running = asyncio.create_task(target.respond(audio(), messages(), current=authority))
    try:
        await wait(socket.receive_held)
        authority.allowed = False
        socket.receive_release.set()
        with pytest.raises(ProviderRefusal):
            await running
        assert wire_pcm(socket) == b""
    finally:
        socket.receive_release.set()
        await target.close()
        await asyncio.gather(running, return_exceptions=True)
