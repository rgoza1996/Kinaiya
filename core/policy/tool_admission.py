"""Trusted, one-use native builtin admission; copied async contexts share its lifetime."""

import json
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from core.policy import ApprovalError, PolicyDenied

SCOPED_BUILTINS = frozenset({"read_file", "write_file", "shell", "search"})
_CURRENT = ContextVar("kinaiya_native_tool_admission", default=None)


@dataclass
class NativeToolAdmission:
    policy: Any
    tool: str
    arguments: dict
    broker_arguments: dict
    digest: str
    revision: int
    decision: dict
    current: Callable[[], None]
    approval_id: str | None
    reviewed: bool
    speech_review: bool
    active: bool = True
    consumed: bool = False
    arguments_checked: bool = False
    provenance: dict = field(default_factory=dict)
    async_current: Any = None

    def admit(self, tool, *, path=None, approval_id=None, digest=None, revision=None):
        if not self.active or self.consumed or not self.arguments_checked:
            raise PolicyDenied("Native action admission is retired or already consumed")
        self.current()
        if tool != self.tool or (path is not None and str(path) != self.arguments.get("path", "")):
            raise PolicyDenied("Native action does not match its admission")
        if tool != "read_file" and (approval_id, digest, revision) != (
            self.approval_id,
            self.digest,
            self.revision,
        ):
            raise PolicyDenied("Native action authority does not match its admission")
        mode = self.decision["effective"]
        if mode == "deny" or self.policy.denies(tool):
            raise PolicyDenied("Native action is disabled by scoped or host policy")
        human = mode == "ask" or self.speech_review
        if human:
            if not self.reviewed or self.approval_id is None:
                raise ApprovalError("Native action requires explicit owner review")
            self.policy.consume_approval(self.approval_id, self.digest, self.revision)
        self.consumed = True
        self.provenance = {
            "kind": "owner_approval"
            if human
            else "host"
            if self.decision["source"] == "host"
            else "owner_rule",
            "tool": tool,
            "source": self.decision["source"],
            "source_scope_id": self.decision["source_scope_id"],
            "rule_id": self.decision["rule_id"],
            "rule_revision": self.decision["rule_revision"],
        }
        if "target" in self.decision:
            self.provenance.update(
                target=self.decision["target"],
                binding=self.decision["binding"],
                binding_state=self.decision["binding_state"],
            )


@contextmanager
def native_tool_admission(**fields):
    invocation = NativeToolAdmission(**fields)
    token = _CURRENT.set(invocation)
    try:
        yield invocation
    finally:
        # Async ContextVar copies retain this same object. Retiring the object
        # prevents a child from using a grant after its lexical scope closes.
        invocation.active = False
        _CURRENT.reset(token)


def admit_native_tool(policy, tool, **fields):
    invocation = _CURRENT.get()
    if invocation is None:
        return False
    if invocation.policy is not policy or invocation.tool != tool:
        raise PolicyDenied("Native action does not match its bound invocation")
    invocation.admit(tool, **fields)
    return True


def check_native_arguments(policy, tool, arguments):
    invocation = _CURRENT.get()
    if invocation is None:
        return
    if invocation.policy is not policy or invocation.tool != tool:
        raise PolicyDenied("Native broker does not match its bound invocation")
    if (
        not invocation.active
        or invocation.consumed
        or json.dumps(arguments, sort_keys=True, allow_nan=False)
        != json.dumps(invocation.broker_arguments, sort_keys=True, allow_nan=False)
    ):
        raise PolicyDenied("Native broker arguments do not match their admission")
    invocation.arguments_checked = True


class MCPToolAdmission(NativeToolAdmission):
    """Explicit actor-queue handoff; never depend on actor ContextVar copies."""

    def retire(self):
        self.active = False

    def check(self, server, tool, arguments):
        if not self.active or self.consumed:
            raise PolicyDenied("MCP admission is retired or already consumed")
        if json.dumps(
            {"server_id": server, "tool": tool, "arguments": arguments},
            sort_keys=True,
            allow_nan=False,
        ) != json.dumps(self.arguments, sort_keys=True, allow_nan=False):
            raise PolicyDenied("MCP call differs from its exact native admission")
        self.current()

    async def refresh(self, server, tool, arguments):
        self.check(server, tool, arguments)
        if self.async_current is not None:
            await self.async_current()
        self.check(server, tool, arguments)

    def admit_call(self, server, tool, arguments):
        self.check(server, tool, arguments)
        self.arguments_checked = True
        self.admit(
            "mcp_call", approval_id=self.approval_id, digest=self.digest, revision=self.revision
        )
