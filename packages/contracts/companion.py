"""Strict foreground companion control contracts; no media or general credential."""

import unicodedata
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator

from packages.contracts.models import MAX_PLAYBACK_GENERATION, WireModel

SafeInt = Annotated[int, Field(strict=True, ge=1, le=MAX_PLAYBACK_GENERATION)]
Timestamp = Annotated[int, Field(strict=True, ge=0, le=MAX_PLAYBACK_GENERATION)]
Identifier = Annotated[
    str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
]
Key = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^[!-~]+$")]
ShortText = Annotated[str, Field(strict=True, min_length=1, max_length=120)]
CallState = Literal["ringing", "accepted_waiting_media", "declined", "missed", "ended"]
EndReason = Literal[
    "declined",
    "unanswered",
    "owner_ended",
    "device_ended",
    "media_unavailable",
    "device_revoked",
    "device_expired",
    "session_invalidated",
]


class CompanionWire(WireModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def bounded_strings(cls, value):
        if isinstance(value, str):
            try:
                raw = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("Invalid UTF-8 text") from exc
            if len(raw) > 4096 or any(
                unicodedata.category(c) == "Cc" and c not in "\n\t" for c in value
            ):
                raise ValueError("Text exceeds its bound or contains controls")
        return value

    @field_validator("label", "device_name", "title", check_fields=False)
    @classmethod
    def short_text(cls, value):
        if (
            len(value.encode("utf-8")) > 480
            or not value.strip()
            or any(unicodedata.category(c) == "Cc" for c in value)
        ):
            raise ValueError("Names and titles require1..120 characters/480UTF8bytes, no controls")
        return value


class CompanionBinding(CompanionWire):
    session_id: Identifier
    thread_id: Identifier
    epoch: SafeInt


class PairingRequest(CompanionBinding):
    label: ShortText


class PairingIssued(CompanionBinding):
    label: ShortText
    pairing_code: str = Field(min_length=43, max_length=128)
    expires_at: Timestamp


class PairingRedeem(CompanionWire):
    code: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    device_name: ShortText


class CompanionDevice(CompanionBinding):
    device_id: Identifier
    device_name: ShortText
    label: ShortText
    scope: Literal["companion_control"] = "companion_control"
    created_at: Timestamp
    expires_at: Timestamp
    revoked_at: Timestamp | None = None
    status: Literal["active", "expired", "revoked", "stale"]


class DeviceIssued(CompanionWire):
    device_token: str = Field(min_length=43, max_length=128)
    device: CompanionDevice


class DevicePage(CompanionWire):
    devices: list[CompanionDevice] = Field(max_length=32)


class CallRequest(CompanionWire):
    device_id: Identifier
    idempotency_key: Key
    title: ShortText


class CompanionCall(CompanionBinding):
    call_id: Identifier
    sequence: SafeInt
    device_id: Identifier
    title: ShortText
    state: CallState
    revision: SafeInt
    created_at: Timestamp
    ringing_deadline: Timestamp
    media_deadline: Timestamp | None = None
    end_reason: EndReason | None = None
    note: str = Field(default="", max_length=4096)


class CallPage(CompanionWire):
    calls: list[CompanionCall] = Field(max_length=20)
    next_after: SafeInt | None = None


class CallAction(CompanionWire):
    action: Literal["answer", "decline", "end"]
    expected_revision: SafeInt
    idempotency_key: Key


class CallEnd(CompanionWire):
    expected_revision: SafeInt
    idempotency_key: Key


class CallNote(CompanionWire):
    expected_revision: SafeInt
    note: str = Field(max_length=4096)

    @field_validator("note")
    @classmethod
    def plain_text_only(cls, value):
        # The complete string is literal text, including markup/JSON-looking
        # punctuation. No interpreter, URL binding or dispatch consumes it.
        return value
