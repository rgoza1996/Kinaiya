"""Exact metadata proofs for a supported native STT worker, never audio replay.

A missing reservation/receipt or a different worker instance proves nothing about
an earlier invocation. Publication proof is positive-only and binds the complete
original AppendRequest digest; it is not a latest-window or current-epoch guess.
"""

import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL = "native_stt_settlement_v1"


def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def settlement_json(raw, limit=8192):
    if type(raw) is not bytes or not 1 <= len(raw) <= limit:
        raise ValueError("STT settlement metadata exceeds its bound")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate STT settlement field")
            value[key] = item
        return value

    def invalid(_):
        raise ValueError("STT settlement metadata requires finite integer counters")

    try:
        value = json.loads(
            raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=invalid
        )
        pending, count = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 8 or count > 256:
                raise ValueError("STT settlement structure exceeds its bound")
            if type(item) is dict:
                pending.extend((child, depth + 1) for child in item.values())
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item)
        if type(value) is not dict:
            raise ValueError("STT settlement requires an object")
        return value
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid STT settlement metadata") from exc


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


class STTWorkerReservation(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["native_stt_settlement_v1"]
    worker_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)


class STTWorkerOperation(STTWorkerReservation):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)

    @model_validator(mode="after")
    def exact_digest(self):
        if self.request_sha256 != digest(self.model_dump(exclude={"request_sha256"})):
            raise ValueError("Native STT operation digest does not match")
        return self


class STTWorkerProof(STTWorkerOperation):
    state: Literal["reserved", "running", "settled", "unknown"]
    settled: bool
    outcome: Literal["completed", "failed", "not_dispatched"] | None

    @model_validator(mode="after")
    def exact_completion(self):
        if self.settled != (self.state == "settled") or self.settled != (self.outcome is not None):
            raise ValueError("Native STT proof has inconsistent settlement state")
        return self


class STTPublication(_Closed):
    state: Literal["none", "not_admitted", "required"]
    session_id: str | None
    thread_id: str | None
    epoch: int | None = Field(ge=0, le=9_007_199_254_740_990)
    idempotency_key: str | None
    request_sha256: str | None
    audio_sha256: str | None
    stt_identity_sha256: str | None

    @model_validator(mode="after")
    def exact_state(self):
        fields = (
            self.session_id,
            self.thread_id,
            self.epoch,
            self.idempotency_key,
            self.request_sha256,
            self.audio_sha256,
            self.stt_identity_sha256,
        )
        if self.state != "required":
            if any(value is not None for value in fields):
                raise ValueError("Unadmitted STT publication has no append identity")
        else:
            for value, bound in (
                (self.session_id, 128),
                (self.thread_id, 128),
                (self.idempotency_key, 256),
            ):
                if (
                    type(value) is not str
                    or not 1 <= len(value.encode("utf-8")) <= bound
                    or any(ord(c) < 32 for c in value)
                ):
                    raise ValueError("Invalid original STT publication identity")
            if type(self.epoch) is not int or any(
                type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in (self.request_sha256, self.audio_sha256, self.stt_identity_sha256)
            ):
                raise ValueError("Invalid original STT publication digest")
        return self


class STTWorkerBinding(_Closed):
    protocol: Literal["native_stt_settlement_v1"]
    origin: str = Field(min_length=1, max_length=256)
    operation: STTWorkerOperation
    publication: STTPublication

    @field_validator("origin")
    @classmethod
    def local_origin(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
            or parsed.port is None
            or value
            != f"http://{'[' + parsed.hostname + ']' if parsed.hostname == '::1' else parsed.hostname}:{parsed.port}"
        ):
            raise ValueError("Native STT binding requires its exact configured loopback origin")
        return value


class STTAdmissionReconcile(_Closed):
    schema_version: Literal["1.0"]
    expected_backend: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    admission_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)


class STTAdmissionItem(_Closed):
    admission_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)
    protocol: Literal["native_stt_settlement_v1"] | None
    worker_instance: str | None = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    operation_id: str | None = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    publication_state: Literal["none", "not_admitted", "required", "legacy_unknown"]
    active: bool
    reconcilable: bool
    reason: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def exact_protocol(self):
        if self.protocol is None:
            if (
                self.worker_instance is not None
                or self.operation_id is not None
                or self.publication_state != "legacy_unknown"
                or self.reconcilable
            ):
                raise ValueError("A legacy STT admission has no worker proof")
        elif (
            self.worker_instance is None
            or self.operation_id is None
            or self.publication_state == "legacy_unknown"
        ):
            raise ValueError("A native STT admission requires its original descriptor")
        if self.active and self.reconcilable:
            raise ValueError("An active original operation cannot be retired by lookup")
        return self


class STTAdmissionInventory(_Closed):
    schema_version: Literal["1.0"]
    role: Literal["stt"]
    backend_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    instance_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    admissions: list[STTAdmissionItem] = Field(max_length=64)
    limit: Literal[64]


class STTAdmissionResult(_Closed):
    schema_version: Literal["1.0"]
    role: Literal["stt"]
    status: Literal["settled", "unknown"]
    backend_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    admission_id: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)
    worker_settled: bool
    publication_settled: bool
    retired: bool
    reason: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def exact_retirement(self):
        if (self.status == "settled") != self.retired or (
            self.retired and not (self.worker_settled and self.publication_settled)
        ):
            raise ValueError("STT retirement requires both exact positive proofs")
        return self


def validate_publication_proof(value, original):
    if (
        type(value) is not dict
        or set(value)
        != {"schema_version", "publication", "recorded", "event_id", "sequence", "origin"}
        or value["schema_version"] != "1.0"
        or type(value["recorded"]) is not bool
        or encode(value["publication"]) != encode(original.model_dump())
    ):
        raise ValueError("STT publication proof does not match its original append")
    if value["recorded"]:
        if (
            type(value["event_id"]) is not str
            or not 1 <= len(value["event_id"].encode("utf-8")) <= 128
            or type(value["sequence"]) is not int
            or not 1 <= value["sequence"] <= 9_007_199_254_740_990
            or value["origin"] != "authenticated-user"
        ):
            raise ValueError("Invalid native STT publication identity")
    elif any(value[name] is not None for name in ("event_id", "sequence", "origin")):
        raise ValueError("Absent publication has no positive identity")
    return value["recorded"]
