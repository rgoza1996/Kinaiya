"""Explicit in-memory submit semantics for downstream Agent test doubles.

These fixtures do not qualify SQLite transactions, authentication or persistence.
Their recorded entry lists are the truth; a separate empty cache cannot establish
absence after a test copies journal history into a recovered fixture.
"""

import asyncio
import copy

from packages.contracts.agent_submit import (
    AgentSubmitConflict,
    AgentSubmitLookupRequest,
    AgentSubmitLookupResult,
    AgentSubmitReceipt,
    AgentSubmitUnavailable,
    submit_entry,
    submit_request,
)
from packages.contracts.models import AppendRequest


def _snapshot(value):
    if isinstance(value, (AppendRequest, AgentSubmitLookupRequest)):
        value = value.model_dump(mode="python", warnings="error")
    return copy.deepcopy(value)


class InMemorySubmitProtocol:
    """Test-only native protocol over the subclass's actual append/entries state."""

    def __init__(self):
        self._submit_lock = asyncio.Lock()

    def _recorded_submits(self):
        recorded = {}
        event_ids, sequences, native_keys = set(), set(), set()
        for value in self.entries:
            raw = _snapshot(value)
            if type(raw) is not dict:
                raise AgentSubmitUnavailable()
            payload = raw.get("payload")
            command = raw.get("kind") == "agent.command"
            if command and (
                type(payload) is not dict
                or payload.get("op")
                not in {
                    "submit",
                    "steer-requested",
                    "steer-rejected",
                    "steer-applied",
                    "interrupt",
                    "disclosure-narrowed",
                }
            ):
                raise AgentSubmitUnavailable()
            native_key = raw.get("idempotency_key")
            submit_key = (
                type(native_key) is str
                and native_key.startswith("agent:")
                and native_key.endswith(":agent.command:1")
            )
            if not submit_key and (type(payload) is not dict or payload.get("op") != "submit"):
                continue
            entry = submit_entry(raw)
            # The MCP fixture separately retains authenticated adapter writers.
            writers = getattr(self, "_writers", None)
            if writers is not None and writers.get(entry.event_id) != "agent":
                raise AgentSubmitUnavailable()
            key = entry.session_id, entry.payload["idempotency_key"]
            if (
                key in recorded
                or entry.event_id in event_ids
                or entry.sequence in sequences
                or entry.idempotency_key in native_keys
            ):
                raise AgentSubmitUnavailable()
            recorded[key] = entry
            event_ids.add(entry.event_id)
            sequences.add(entry.sequence)
            native_keys.add(entry.idempotency_key)
        return recorded

    async def admit_submit(self, request):
        try:
            proposal = submit_request(_snapshot(request))
            key = proposal.session_id, proposal.payload["idempotency_key"]
            async with self._submit_lock:
                recorded = self._recorded_submits()
                if key in recorded:
                    receipt = AgentSubmitReceipt(disposition="retained", entry=recorded[key])
                    try:
                        return receipt.validate_request(proposal)
                    except ValueError:
                        raise AgentSubmitConflict() from None
                if any(
                    entry.idempotency_key == proposal.idempotency_key for entry in recorded.values()
                ):
                    raise AgentSubmitConflict()
                acknowledged = submit_entry(
                    _snapshot(await self.append(proposal.model_copy(deep=True)))
                )
                canonical = self._recorded_submits().get(key)
                if canonical is None or canonical != acknowledged:
                    raise AgentSubmitUnavailable()
                return AgentSubmitReceipt(disposition="created", entry=canonical).validate_request(
                    proposal
                )
        except AgentSubmitConflict:
            raise
        except Exception:
            raise AgentSubmitUnavailable() from None

    async def lookup_submit(self, request):
        try:
            query = AgentSubmitLookupRequest.model_validate(_snapshot(request), strict=True)
            async with self._submit_lock:
                entry = self._recorded_submits().get((query.session_id, query.idempotency_key))
                result = AgentSubmitLookupResult(
                    status="unobserved" if entry is None else "recorded", entry=entry
                )
                try:
                    return result.validate_request(query)
                except ValueError:
                    raise AgentSubmitConflict() from None
        except AgentSubmitConflict:
            raise
        except Exception:
            raise AgentSubmitUnavailable() from None
