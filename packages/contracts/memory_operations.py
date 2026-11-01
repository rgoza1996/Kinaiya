"""Owner-only historical memory receipts, never recall or mutation authority.

Lookup supplies the exact original capture/forget request so the native owner can
check its canonical digest without retaining another content snapshot. A matching
receipt proves that historical commit, even after the namespace revision/scope
advances or memory becomes disabled/quarantined. Stored dispositions describe
only redacted node metadata, not source freshness or current recall eligibility.
``not_found`` includes erased/deleted/no-op receipts and does not prove that an
earlier delayed POST cannot still commit. Lookup never retries or reapplies it.
``mutation_fenced`` is a separate native observation: a later namespace revision,
retired session identity/epoch or deleted session prevents new effects from that
original request. It never turns a missing receipt into success or failure.
"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from packages.contracts.memory import (
    IdempotencyKey,
    Identifier,
    MemoryBinding,
    MemoryCapture,
    MemoryForget,
    MemoryWire,
    SafePositive,
    StrictBool,
)

MemoryOperation = Literal["capture", "forget"]
MemoryOperationId = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
MemoryStoredDisposition = Literal[
    "stored_active", "stored_invalidated", "stored_tainted", "forgotten", "unavailable"
]


class MemoryOperationLookup(MemoryWire):
    operation: MemoryOperation
    request: MemoryCapture | MemoryForget

    @model_validator(mode="after")
    def _operation_matches(self):
        expected = MemoryCapture if self.operation == "capture" else MemoryForget
        if not isinstance(self.request, expected):
            raise ValueError("Memory operation and original request do not match")
        return self


class MemoryOperationReceipt(MemoryWire):
    operation_id: MemoryOperationId
    revision: SafePositive
    node_id: Identifier


class MemoryOperationResult(MemoryWire):
    binding: MemoryBinding
    operation: MemoryOperation
    idempotency_key: IdempotencyKey
    status: Literal["committed", "not_found"]
    receipt: MemoryOperationReceipt | None
    disposition: MemoryStoredDisposition | None
    mutation_fenced: StrictBool

    @model_validator(mode="after")
    def _receipt_presence(self):
        if self.status == "committed":
            if self.receipt is None or self.disposition is None:
                raise ValueError("Committed memory operations require redacted receipt metadata")
        elif self.receipt is not None or self.disposition is not None:
            raise ValueError("Missing memory operations cannot claim receipt metadata")
        return self
