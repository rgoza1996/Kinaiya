"""Optional Converse recall and provenance; no memory backend is imported."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from packages.contracts.memory import (
    Identifier,
    MemoryBinding,
    MemoryNode,
    MemoryQueryResult,
    MemoryState,
    MemoryWire,
    SafeNonNeg,
    SafePositive,
    StrictBool,
    _check_distinct,
)

Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


def recall_digest(state: MemoryState, nodes: list[MemoryNode]) -> str:
    base = MemoryQueryResult(state=state, nodes=nodes)
    encoded = json.dumps(
        base.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MemoryContextPin(MemoryWire):
    # Embedded provenance requires an explicit version, including on replay.
    schema_version: Literal["1.0"] = Field(...)
    implementation_id: Identifier
    use_generation: SafePositive
    namespace_revision: SafeNonNeg
    closure_sha256: Digest
    selector_ids: list[Identifier] = Field(min_length=1, max_length=8)

    @field_validator("selector_ids")
    @classmethod
    def _selectors(cls, values):
        return _check_distinct(values, "selector_ids")


class MemoryRecallRequest(MemoryBinding):
    seed_ids: list[Identifier] = Field(min_length=1, max_length=8)

    @field_validator("seed_ids")
    @classmethod
    def _seeds(cls, values):
        return _check_distinct(values, "seed_ids")


class MemoryRecallResult(MemoryQueryResult):
    implementation_id: Literal["korina-dag-v1"] = "korina-dag-v1"
    nodes: list[MemoryNode] = Field(min_length=1, max_length=64)
    closure_sha256: Digest

    @model_validator(mode="after")
    def _snapshot(self):
        if not self.state.enabled or self.state.status != "ready":
            raise ValueError("Recall requires an enabled ready namespace")
        scope = (self.state.session_id, self.state.thread_id, self.state.epoch)
        by_id = {node.node_id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("Recall node identities must be distinct")
        ordinals = [node.ordinal for node in self.nodes]
        if ordinals != sorted(set(ordinals)):
            raise ValueError("Recall must be in strict dependency order")
        depths = {}
        for node in self.nodes:
            if (node.session_id, node.thread_id, node.epoch) != scope or node.state != "active":
                raise ValueError("Recall contains an inactive or foreign node")
            if any(
                parent not in by_id or by_id[parent].ordinal >= node.ordinal
                for parent in node.prerequisite_ids
            ):
                raise ValueError("Recall must contain its complete older dependencies")
            depths[node.node_id] = 1 + max((depths[p] for p in node.prerequisite_ids), default=0)
            if depths[node.node_id] > 16:
                raise ValueError("Recall dependency depth exceeds its bound")
        if self.closure_sha256 != recall_digest(self.state, self.nodes):
            raise ValueError("Recall digest differs")
        if (
            len(MemoryQueryResult(state=self.state, nodes=self.nodes).model_dump_json().encode())
            > 16 * 1024
        ):
            raise ValueError("Recall exceeds its complete closure byte bound")
        return self


class MemoryContextValidate(MemoryBinding):
    pin: MemoryContextPin


class MemoryContextValidation(MemoryWire):
    valid: StrictBool
    state: MemoryState


class MemoryUseConfigure(MemoryBinding):
    expected_generation: SafeNonNeg
    enabled: StrictBool
    implementation_id: Identifier | None = None
    selector_ids: list[Identifier] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _selection(self):
        _check_distinct(self.selector_ids, "selector_ids")
        if self.enabled and self.implementation_id is None:
            raise ValueError("Enabled recall requires a selected implementation")
        if not self.enabled and (self.implementation_id is not None or self.selector_ids):
            raise ValueError("Disable accepts no replacement selection")
        return self


class MemoryUseState(MemoryBinding):
    runtime_id: Identifier
    use_generation: SafeNonNeg
    enabled: StrictBool
    implementation_id: Identifier | None
    selector_ids: list[Identifier] = Field(max_length=8)
    pending: StrictBool
    notice: Literal["disabled", "ready", "unavailable", "context_omitted"]

    @model_validator(mode="after")
    def _selection(self):
        _check_distinct(self.selector_ids, "selector_ids")
        if self.enabled and self.implementation_id is None:
            raise ValueError("Enabled recall requires a selected implementation")
        return self
