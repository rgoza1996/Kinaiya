"""Owner inspection and exact retirement of process-local, one-use permits.

These identities contain hashes, never tool arguments or source content. A
receipt proves a native decision at its linearization point, not cancellation
of admitted work. Missing history or a different broker proves no outcome.
"""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.contracts.models import AppendRequest, JournalEntry

MAX_REVISION = 9_007_199_254_740_990
AuthorizationState = Literal[
    "requested", "approved_unspent", "used", "expired", "revoked", "reissued", "retired"
]


class AuthorizationIdentity(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", revalidate_instances="always"
    )
    approval_id: str = Field(pattern=r"^apr_[a-f0-9]{16}$", min_length=20, max_length=20)
    task_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=MAX_REVISION)
    objective_revision: int = Field(ge=1, le=MAX_REVISION)
    tool: str = Field(min_length=1, max_length=256)
    action_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$", min_length=71, max_length=71)
    arguments_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)

    @field_validator("task_id", "session_id", "thread_id", "tool")
    @classmethod
    def text_identity(cls, value):
        if "\0" in value:
            raise ValueError("Authorization identity contains a NUL")
        value.encode("utf-8")
        return value

    @field_validator("approval_id", "action_digest", "arguments_sha256", "source_sha256")
    @classmethod
    def exact_hash(cls, value, info):
        pattern = {
            "approval_id": r"apr_[a-f0-9]{16}",
            "action_digest": r"sha256:[a-f0-9]{64}",
        }.get(info.field_name, r"[a-f0-9]{64}")
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid authorization identity")
        return value


class AuthorizationRevoke(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", revalidate_instances="always"
    )
    schema_version: Literal["1.0"]
    broker_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,128}$", min_length=1, max_length=128)
    identity: AuthorizationIdentity
    expected_revision: int = Field(ge=0, le=MAX_REVISION)
    expected_state: Literal["approved_unspent"]

    @field_validator("broker_instance", "idempotency_key")
    @classmethod
    def exact_token(cls, value, info):
        pattern = (
            r"[a-f0-9]{32}" if info.field_name == "broker_instance" else r"[A-Za-z0-9:._-]{1,128}"
        )
        if re.fullmatch(pattern, value) is None:
            raise ValueError("Invalid authorization request token")
        return value


class AuthorizationRevokeReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["1.0"]
    status: Literal["revoked", "not_revoked"]
    request: AuthorizationRevoke
    observed_state: AuthorizationState
    revision: int = Field(ge=0, le=MAX_REVISION)
    journal_status: Literal["not_required", "pending", "confirmed", "unconfirmed"]


class AuthorizationLookup(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["1.0"]
    status: Literal["observed", "unobserved"]
    broker_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    receipt: AuthorizationRevokeReceipt | None


class AuthorizationJournalProof(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["1.0"]
    request: AuthorizationRevoke
    recorded: bool
    event_id: str | None = Field(min_length=1, max_length=128)
    sequence: int | None = Field(ge=1, le=MAX_REVISION)
    origin: Literal["agent-supervisor"] | None

    @model_validator(mode="after")
    def exact_result(self):
        if self.recorded != all(
            value is not None for value in (self.event_id, self.sequence, self.origin)
        ):
            raise ValueError("Incomplete native authorization journal proof")
        if not self.recorded and any(
            value is not None for value in (self.event_id, self.sequence, self.origin)
        ):
            raise ValueError("Unobserved authorization has no positive identity")
        return self

    def validate_request(self, request):
        if self.request != request:
            raise ValueError("Authorization proof belongs to another original request")
        return self


def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def revocation_append(request: AuthorizationRevoke) -> AppendRequest:
    """A stable metadata event: no mutable task state or source content."""
    identity = request.identity
    return AppendRequest(
        session_id=identity.session_id,
        thread_id=identity.thread_id,
        epoch=identity.epoch,
        kind="approval.resolved",
        payload={
            "task_id": identity.task_id,
            "approval_id": identity.approval_id,
            "approved": False,
            "reason": "owner_revoked",
            "authorization_revocation": request.model_dump(mode="json"),
            "authorization_revision": request.expected_revision + 1,
            "content_scope": "local_only",
        },
        task_refs=[identity.task_id],
        idempotency_key="authorization-revoke:" + digest(request.model_dump(mode="json")),
    )


def validate_revocation_entry(raw, request):
    entry = raw if isinstance(raw, JournalEntry) else JournalEntry.model_validate(raw, strict=True)
    expected = revocation_append(request)
    if (
        entry.origin != "agent-supervisor"
        or type(entry.sequence) is not int
        or not 1 <= entry.sequence <= MAX_REVISION
        or not isinstance(entry.event_id, str)
        or not 1 <= len(entry.event_id) <= 128
        or encode(
            entry.model_dump(mode="json", exclude={"event_id", "sequence", "origin", "created_at"})
        )
        != encode(expected.model_dump(mode="json"))
    ):
        raise ValueError("Exact native authorization journal proof is unavailable")
    return entry
