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
import math
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


class ApprovalReceiptUnavailable(ApprovalError):
    """Bounded historical renewal metadata is no longer available."""


@dataclass
class Approval:
    approval_id: str
    action_digest: str
    objective_revision: int
    expires_at: float
    used: bool = field(default=False)
    retired: str | None = None
    replacement_id: str | None = None
    replacement_expires_at: float | None = None
    deadline: float | None = None


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
    protected-root boundary and never grants core authority. ``readonly_roots``
    remain readable/searchable but cannot be written, including with approvals.
    Shell execution overmounts these references read-only.
    """

    def __init__(
        self,
        workspace: Path,
        allow_write: bool = False,
        allow_shell: bool = False,
        yolo: bool = False,
        network: bool = False,
        protected_roots: tuple[Path, ...] | list[Path] = (),
        readonly_roots: tuple[Path, ...] | list[Path] = (),
        deny_write: bool = False,
        deny_shell: bool = False,
        shell_working_directory: str = ".",
        shell_timeout_s: float = 120,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.allow_write = bool(allow_write)
        self.allow_shell = bool(allow_shell)
        self.yolo = bool(yolo)
        self.network = bool(network)
        self.deny_write = bool(deny_write)
        self.deny_shell = bool(deny_shell)
        self.shell_working_directory = shell_working_directory
        self.shell_timeout_s = shell_timeout_s
        self.protected_roots = tuple(Path(p).resolve() for p in protected_roots)
        references = []
        for root in readonly_roots:
            candidate = Path(root)
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            resolved = candidate.resolve()
            if resolved != Path(os.path.abspath(candidate)):
                raise PolicyDenied("read-only roots must not use symlink aliases")
            references.append(resolved)
        self.readonly_roots = tuple(references)
        self._approvals: dict[str, Approval] = {}
        self._approval_owners: set[str] = set()

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
        return not self.deny_write and (self.allow_write or self.yolo)

    @property
    def effective_shell(self) -> bool:
        return not self.deny_shell and (self.allow_shell or self.yolo)

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
        from core.policy.tool_admission import admit_native_tool

        resolved = self.resolve_path(path)
        admit_native_tool(self, "read_file", path=path)
        return resolved

    @contextmanager
    def scoped_parent(self, path: str | Path, *, create=False):
        """Walk with directory descriptors; a racing symlink cannot redirect host I/O."""
        if create:
            self._deny_readonly_write(path)
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

    def _deny_readonly_write(self, path: str | Path) -> None:
        if not self.readonly_roots:
            return
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        # Preserve the configured pathname if a reference was replaced by a
        # symlink; also reject other symlinks resolving into the reference.
        lexical = Path(os.path.abspath(candidate))
        resolved = self.resolve_path(path)
        for root in self.readonly_roots:
            for observed in (lexical, resolved):
                if observed == root or root in observed.parents or observed in root.parents:
                    raise PolicyDenied("write touches a read-only reference")

    def check_write(
        self,
        path: str | Path,
        *,
        approval_id: str | None = None,
        digest: str | None = None,
        objective_revision: int = 1,
    ) -> Path:
        """Writes need a grant or a bound approval; scoping always applies."""
        if self.deny_write:
            raise PolicyDenied("Native workspace writes are disabled by owner policy")
        self._deny_readonly_write(path)
        resolved = self.resolve_path(path)
        from core.policy.tool_admission import admit_native_tool

        if admit_native_tool(
            self,
            "write_file",
            path=path,
            approval_id=approval_id,
            digest=digest,
            revision=objective_revision,
        ):
            return resolved
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
        if self.deny_shell:
            raise PolicyDenied("Native shell commands are disabled by owner policy")
        from core.policy.tool_admission import admit_native_tool

        if admit_native_tool(
            self, "shell", approval_id=approval_id, digest=digest, revision=objective_revision
        ):
            return
        if self.effective_shell:
            return
        if approval_id is None or digest is None:
            raise ApprovalError("shell requires an owner approval for this command")
        self.consume_approval(approval_id, digest, objective_revision)

    def denies(self, tool: str) -> bool:
        """A denial is neither a broad grant nor a requestable approval."""
        return (tool == "write_file" and self.deny_write) or (tool == "shell" and self.deny_shell)

    def needs_approval(self, tool: str, arguments: dict[str, Any] | None = None) -> bool:
        """True when the tool would need a per-action approval under grants."""
        if tool == "write_file":
            return not self.deny_write and not self.effective_write
        if tool == "shell":
            return not self.deny_shell and not self.effective_shell
        return False

    def digest_for(self, tool: str, arguments: dict[str, Any], objective_revision: int) -> str:
        return action_digest(tool, _norm_args(arguments), objective_revision)

    # -- approvals ------------------------------------------------------
    MAX_LIVE_APPROVALS = 1024
    MAX_CLOSED_APPROVALS = 256

    def approval_state(self, approval_id: str) -> str:
        approval = self._approvals.get(approval_id)
        if approval is None:
            return "unavailable"
        if approval.used:
            return "used"
        if approval.retired is not None:
            return "expired" if approval.retired == "expired" else "retired"
        expired = approval.expires_at <= time.time() or (
            approval.deadline is not None and approval.deadline <= time.monotonic()
        )
        return "expired" if expired else "pending"

    def prune_approvals(self) -> None:
        """Bound closed history without evicting a live waiter/dispatch permit."""
        closed = [
            key
            for key in self._approvals
            if key not in self._approval_owners and self.approval_state(key) != "pending"
        ]
        for key in closed[: max(0, len(closed) - self.MAX_CLOSED_APPROVALS)]:
            del self._approvals[key]

    def retire_approval(self, approval_id: str, reason: str = "retired") -> None:
        approval = self._approvals.get(approval_id)
        if approval is not None and (
            approval.retired is None or approval.retired == "expired" and reason != "expired"
        ):
            approval.retired = reason
            # Closed history is ordered by its most recent native transition.
            self._approvals[approval_id] = self._approvals.pop(approval_id)
        self.prune_approvals()

    def release_approval(self, approval_id: str) -> None:
        self.retire_approval(approval_id)
        self._approval_owners.discard(approval_id)
        self.prune_approvals()

    def create_approval(
        self,
        action_digest_: str,
        objective_revision: int,
        ttl_s: float = 300.0,
        *,
        retained: bool = False,
        _replaces: str | None = None,
    ) -> Approval:
        if (
            isinstance(ttl_s, bool)
            or not isinstance(ttl_s, (int, float))
            or not math.isfinite(ttl_s)
            or not -3600 <= ttl_s <= 3600
        ):
            raise ApprovalError("Approval lifetime is outside the native bound")
        self.prune_approvals()
        live = sum(
            key in self._approval_owners or self.approval_state(key) == "pending"
            for key in self._approvals
            if key != _replaces
        )
        if live >= self.MAX_LIVE_APPROVALS:
            raise ApprovalError("Approval capacity requires owner retirement")
        approval = Approval(
            approval_id="apr_" + secrets.token_hex(8),
            action_digest=action_digest_,
            objective_revision=objective_revision,
            expires_at=time.time() + ttl_s,
            deadline=time.monotonic() + max(0, ttl_s),
        )
        self._approvals[approval.approval_id] = approval
        if retained:
            self._approval_owners.add(approval.approval_id)
        self.prune_approvals()
        return approval

    def reissue_approval(
        self, approval_id: str, action_digest_: str, objective_revision: int
    ) -> Approval:
        """Native same-action replacement; old identity never grants twice."""
        original = self._approvals.get(approval_id)
        if original is None:
            raise ApprovalError("Approval renewal receipt is unavailable")
        if (original.action_digest, original.objective_revision) != (
            action_digest_,
            objective_revision,
        ):
            raise ApprovalError("Approval renewal does not match the original action")
        if original.replacement_id is not None:
            raise ApprovalError("Approval already has a replacement")
        if self.approval_state(approval_id) != "expired":
            raise ApprovalError("Only an expired approval can be renewed")
        replacement = self.create_approval(
            action_digest_, objective_revision, retained=True, _replaces=approval_id
        )
        original.replacement_id = replacement.approval_id
        original.replacement_expires_at = replacement.expires_at
        # New retained ownership replaces the old waiter's slot synchronously.
        original.retired = "reissued"
        self._approvals[approval_id] = self._approvals.pop(approval_id)
        self._approval_owners.discard(approval_id)
        self.prune_approvals()
        return replacement

    def peek_approval(self, approval_id: str) -> Approval | None:
        return self._approvals.get(approval_id)

    def consume_approval(
        self, approval_id: str, action_digest_: str, objective_revision: int
    ) -> Approval:
        """One-use, expiring, digest- and revision-bound approval spend."""
        approval = self.check_approval(approval_id, action_digest_, objective_revision)
        approval.used = True
        self._approvals[approval_id] = self._approvals.pop(approval_id)
        self.prune_approvals()
        return approval

    def check_approval(
        self, approval_id: str, action_digest_: str, objective_revision: int
    ) -> Approval:
        """Validate without spending; the effect broker still consumes once."""
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise ApprovalError(f"unknown approval {approval_id!r}")
        if approval.used:
            raise ApprovalError(f"approval {approval_id!r} already spent")
        if approval.retired is not None:
            raise ApprovalError("approval is retired; a separate owner review is required")
        if self.approval_state(approval_id) == "expired":
            raise ApprovalError(f"approval {approval_id!r} expired")
        if approval.action_digest != action_digest_:
            raise ApprovalError("approval does not match this action; re-approval required")
        if approval.objective_revision != objective_revision:
            raise ApprovalError("approval is for a stale objective revision")
        return approval
