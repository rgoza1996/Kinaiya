"""Bounded owner-facing catalog for existing optional Converse notes (#123)."""

import unicodedata

from pydantic import Field, field_validator, model_validator

from packages.contracts.memory import (
    Identifier,
    MemoryKind,
    MemoryState,
    MemoryStateName,
    MemoryWire,
    SafePositive,
    StrictBool,
    _utf8_bytes,
)


class MemorySummary(MemoryWire):
    node_id: Identifier
    ordinal: SafePositive
    kind: MemoryKind
    state: MemoryStateName
    preview: str | None
    preview_truncated: StrictBool

    @field_validator("preview")
    @classmethod
    def _preview(cls, value):
        if value is not None:
            if not 1 <= len(_utf8_bytes(value)) <= 128:
                raise ValueError("Preview requires 1..128 UTF-8 bytes")
            if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in value):
                raise ValueError("Preview permits newline/tab only among controls")
        return value

    @model_validator(mode="after")
    def _redaction(self):
        if self.state == "forgotten":
            if self.preview is not None or self.preview_truncated:
                raise ValueError("Forgotten summaries must be redacted")
        elif self.preview is None:
            raise ValueError("Retained summaries require a preview")
        return self


class MemoryCatalogResult(MemoryWire):
    state: MemoryState
    entries: list[MemorySummary] = Field(max_length=256)

    @model_validator(mode="after")
    def _complete(self):
        if self.state.node_count != len(self.entries):
            raise ValueError("Catalog must contain the complete namespace")
        if len({row.node_id for row in self.entries}) != len(self.entries):
            raise ValueError("Catalog identities must be distinct")
        if [row.ordinal for row in self.entries] != list(range(1, len(self.entries) + 1)):
            raise ValueError("Catalog must be in complete ordinal order")
        return self
