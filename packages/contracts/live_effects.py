"""Original live-result consumption; never an execution or physical-success grant."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.contracts.live_decisions_attempts import LiveDecisionsPublication

PROTOCOL = "native_live_effect_v2"
MAX_REQUEST_BYTES = 16384
MAX_RESPONSE_BYTES = 32768
MAX_RECORDS = 10000
MAX_ACTIVE = 64
MAX_STRUCTURE_DEPTH = 16
MAX_STRUCTURE_NODES = 1024


class LiveEffectRefused(ValueError):
    """The native metadata operation was definitively refused before a new start."""


def effect_key(receipt):
    return "live-effect:" + receipt.receipt_id


def effect_json(raw, limit=MAX_REQUEST_BYTES):
    if type(raw) is not bytes or not raw or len(raw) > limit:
        raise ValueError("Live consumer metadata exceeds its byte bound")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate live consumer metadata field")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("Live consumer metadata requires native integer counters")

    try:
        value = json.loads(
            raw, object_pairs_hook=unique, parse_float=invalid, parse_constant=invalid
        )
        if type(value) is not dict:
            raise ValueError("Live consumer metadata requires a closed object")
        pending, count = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > MAX_STRUCTURE_DEPTH or count > MAX_STRUCTURE_NODES:
                raise ValueError("Live consumer metadata exceeds its structure bound")
            if isinstance(item, dict):
                for key in item:
                    key.encode("utf-8")
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
            elif isinstance(item, str):
                item.encode("utf-8")
        return value
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid live consumer metadata structure") from exc


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class LiveEffectQuery(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    publication: LiveDecisionsPublication
    effect: Literal["converse_reply", "converse_backchannel", "agent_route"]


class LiveEffectAdmit(LiveEffectQuery):
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$")
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class LiveEffectReceipt(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    original: LiveEffectAdmit
    receipt_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    through_sequence: int = Field(ge=1, le=(1 << 53) - 1)


class LiveEffectEvidence(_Closed):
    event_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1, le=(1 << 53) - 1)


class LiveEffectStart(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    original: LiveEffectReceipt
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$")
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class LiveEffectStarted(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    original: LiveEffectStart
    started_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    through_sequence: int = Field(ge=1, le=(1 << 53) - 1)


class LiveEffectFinish(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    original: LiveEffectReceipt
    status: Literal["recorded", "declined", "refused", "uncertain"]
    evidence: LiveEffectEvidence | None
    reason_code: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def exact_evidence(self):
        if (self.status == "recorded") != (self.evidence is not None):
            raise ValueError("A recorded consumer effect requires its native journal evidence")
        if not self.reason_code.strip() or "\x00" in self.reason_code:
            raise ValueError("A consumer result requires a bounded reason code")
        self.reason_code.encode("utf-8")
        return self


class LiveEffectRecord(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    receipt: LiveEffectReceipt
    started: LiveEffectStarted | None
    completion: LiveEffectFinish | None

    @model_validator(mode="after")
    def original_completion(self):
        if self.started is not None and self.started.original.original != self.receipt:
            raise ValueError("Consumer start changed its original reservation")
        if self.completion is not None and self.completion.original != self.receipt:
            raise ValueError("Consumer completion changed its original receipt")
        if (
            self.completion is not None
            and self.completion.status in {"recorded", "uncertain"}
            and self.started is None
        ):
            raise ValueError("Recorded or uncertain work requires its original start marker")
        return self


class LiveEffectState(_Closed):
    schema_version: Literal["2.0"]
    protocol: Literal["native_live_effect_v2"]
    original: LiveEffectQuery
    record: LiveEffectRecord | None
    source_current: bool

    @model_validator(mode="after")
    def exact_query(self):
        if self.record is not None:
            original = self.record.receipt.original
            if (
                original.publication != self.original.publication
                or original.effect != self.original.effect
            ):
                raise ValueError("Consumer lookup changed its original live result")
        return self
