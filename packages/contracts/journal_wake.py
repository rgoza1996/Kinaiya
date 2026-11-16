"""A journal-change hint has no publication, freshness or execution authority."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BYTES = 4096
MAX_WAIT_MS = 1000
MAX_WAITERS = 64
MAX_REVISION = (1 << 53) - 1


def wake_json(raw):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise ValueError("Journal wake metadata exceeds its bound")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate journal wake field")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("Nonfinite journal wake number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


class JournalWakeCursor(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["journal_wake_v1"]
    owner_instance: str = Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)
    revision: int = Field(ge=0, le=MAX_REVISION)


class JournalWakeWait(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["journal_wake_v1"]
    cursor: JournalWakeCursor
    wait_ms: int = Field(ge=0, le=MAX_WAIT_MS)


class JournalWakeObservation(_Closed):
    schema_version: Literal["1.0"]
    protocol: Literal["journal_wake_v1"]
    original: JournalWakeWait
    cursor: JournalWakeCursor
    changed: bool

    @model_validator(mode="after")
    def exact_observation(self):
        if (
            self.original.cursor.owner_instance != self.cursor.owner_instance
            or self.cursor.revision < self.original.cursor.revision
            or self.changed != (self.cursor.revision > self.original.cursor.revision)
        ):
            raise ValueError("Journal wake observation does not bind its original cursor")
        return self
