"""Filesystem and shell permission policy for the seed Agent.

The policy is enforced by the supervisor (``services.agent``), never by model
prompts. It scopes every tool path to a canonical workspace, rejects path
traversal and symlinks that escape the workspace or touch protected roots,
and gates writes/shell execution on explicit grants or single-use approvals.

Approval records bind to the exact action digest, the objective revision, an
expiry time, and a single use. Changing the action, steering the objective,
letting the approval expire, or replaying it all invalidate the approval.
Only the owner may change permissions or resolve approvals.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PolicyDenied(Exception):
    """Raised when a tool action is outside the granted policy."""


class ApprovalError(PolicyDenied):
    """Raised when an approval is missing, stale, expired, or already spent."""


@dataclass
class Approval:
    approval_id: str
    action_digest: str
    objective_revision: int
    expires_at: float
    used: bool = field(default=False)


def action_digest(tool: str, arguments: dict[str, Any], objective_revision: int) -> str:
    """Stable digest binding a tool call to its exact parameters and revision."""
    canonical = json.dumps(
        {"tool": tool, "arguments": arguments, "objective_revision": objective_revision},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _norm_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize approval-bound arguments so semantically identical actions match."""
    out: dict[str, Any] = {}
    for key in sorted(arguments):
        value = arguments[key]
        if key == "path" and isinstance(value, str):
            out[key] = value.strip().rstrip("/")
        elif key == "content" and isinstance(value, str):
            out[key] = value
        else:
            out[key] = value
    return out


class ToolPolicy:
    """Owns workspace scoping, grants, and one-use approvals.

    ``workspace`` is the only directory tools may touch. ``protected_roots``
    are never writable even when they overlap the workspace (core policy,
    owner goals, evaluator assets). ``yolo`` grants workspace file/shell
    actions without per-action approval but never lifts the workspace or
    protected-root boundary and never grants core authority.
    """

    def __init__(
        self,
        workspace: Path,
        allow_write: bool = False,
        allow_shell: bool = False,
        yolo: bool = False,
        network: bool = False,
        protected_roots: tuple[Path, ...] | list[Path] = (),
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.allow_write = bool(allow_write)
        self.allow_shell = bool(allow_shell)
        self.yolo = bool(yolo)
        self.network = bool(network)
        self.protected_roots = tuple(Path(p).resolve() for p in protected_roots)
        self._approvals: dict[str, Approval] = {}

    # -- ownership ------------------------------------------------------
    @staticmethod
    def _require_owner(actor: str) -> None:
        if actor != "owner":
            raise PolicyDenied(f"permission change requires owner, got {actor!r}")

    def set_permissions(
        self,
        *,
        actor: str,
        allow_write: bool | None = None,
        allow_shell: bool | None = None,
        yolo: bool | None = None,
        network: bool | None = None,
    ) -> None:
        """Owner-only grant changes. Generated tools have no path to this."""
        self._require_owner(actor)
        if allow_write is not None:
            self.allow_write = bool(allow_write)
        if allow_shell is not None:
            self.allow_shell = bool(allow_shell)
        if yolo is not None:
            self.yolo = bool(yolo)
        if network is not None:
            self.network = bool(network)

    @property
    def effective_write(self) -> bool:
        return self.allow_write or self.yolo

    @property
    def effective_shell(self) -> bool:
        return self.allow_shell or self.yolo

    # -- path scoping ---------------------------------------------------
    def is_protected(self, resolved: Path) -> bool:
        """True when an already-resolved path falls under a protected root."""
        for root in self.protected_roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return True
        return False

    def resolve_path(self, path: str | Path) -> Path:
        """Canonicalize and enforce workspace containment plus protected roots.

        ``Path.resolve()`` follows symlinks, so a symlink inside the workspace
        that points outside (or into a protected root) resolves outside and is
        rejected here.
        """
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            raise PolicyDenied(f"path escapes workspace: {path!s}") from None
        for root in self.protected_roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            raise PolicyDenied(f"path touches protected root {root}: {path!s}")
        return resolved

    def check_read(self, path: str | Path) -> Path:
        return self.resolve_path(path)

    @contextmanager
    def scoped_parent(self, path: str | Path, *, create=False):
        """Walk with directory descriptors; a racing symlink cannot redirect host I/O."""
        resolved = self.resolve_path(path)
        parts = resolved.relative_to(self.workspace).parts
        if not parts:
            raise PolicyDenied("A file path is required")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.workspace, flags)
        try:
            for part in parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1], resolved
        finally:
            os.close(descriptor)

    def read_bytes(self, path: str | Path, maximum=262144) -> bytes:
        with self.scoped_parent(path) as (parent, name, _):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                return stream.read(maximum)

    def check_search(self, path: str | Path | None = None) -> Path:
        if path is None:
            return self.workspace
        return self.resolve_path(path)

    def check_write(
        self,
        path: str | Path,
        *,
        approval_id: str | None = None,
        digest: str | None = None,
        objective_revision: int = 1,
    ) -> Path:
        """Writes need a grant or a bound approval; scoping always applies."""
        resolved = self.resolve_path(path)
        if self.effective_write:
            return resolved
        if approval_id is None or digest is None:
            raise ApprovalError("write requires an owner approval for this action")
        self.consume_approval(approval_id, digest, objective_revision)
        return resolved

    def check_shell(
        self,
        *,
        approval_id: str | None = None,
        digest: str | None = None,
        objective_revision: int = 1,
    ) -> None:
        if self.effective_shell:
            return
        if approval_id is None or digest is None:
            raise ApprovalError("shell requires an owner approval for this command")
        self.consume_approval(approval_id, digest, objective_revision)

    def needs_approval(self, tool: str, arguments: dict[str, Any] | None = None) -> bool:
        """True when the tool would need a per-action approval under grants."""
        if tool == "write_file":
            return not self.effective_write
        if tool == "shell":
            return not self.effective_shell
        return False

    def digest_for(self, tool: str, arguments: dict[str, Any], objective_revision: int) -> str:
        return action_digest(tool, _norm_args(arguments), objective_revision)

    # -- approvals ------------------------------------------------------
    def create_approval(
        self, action_digest_: str, objective_revision: int, ttl_s: float = 300.0
    ) -> Approval:
        approval = Approval(
            approval_id="apr_" + secrets.token_hex(8),
            action_digest=action_digest_,
            objective_revision=objective_revision,
            expires_at=time.time() + ttl_s,
        )
        self._approvals[approval.approval_id] = approval
        return approval

    def peek_approval(self, approval_id: str) -> Approval | None:
        return self._approvals.get(approval_id)

    def consume_approval(
        self, approval_id: str, action_digest_: str, objective_revision: int
    ) -> Approval:
        """One-use, expiring, digest- and revision-bound approval spend."""
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ApprovalError(f"unknown approval {approval_id!r}")
        if approval.used:
            raise ApprovalError(f"approval {approval_id!r} already spent")
        if time.time() > approval.expires_at:
            raise ApprovalError(f"approval {approval_id!r} expired")
        if approval.action_digest != action_digest_:
            raise ApprovalError("approval does not match this action; re-approval required")
        if approval.objective_revision != objective_revision:
            raise ApprovalError("approval is for a stale objective revision")
        approval.used = True
        return approval
