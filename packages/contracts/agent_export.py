"""Bounded native Agent export admission; no prompt, token or tool authority."""

import hashlib
import json
import math
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from packages.contracts import models
from packages.providers.disclosure import combine_scopes, scope_allowed

MAX_AGENT_EXPORT_BYTES = 65536
ExportIdentifier = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
ExportCounter = Annotated[int, Field(strict=True, ge=1, le=models.MAX_PLAYBACK_GENERATION)]
ExportDigest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


def agent_export_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_AGENT_EXPORT_BYTES:
        raise ValueError("Invalid Agent export admission JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Invalid Agent export admission JSON")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Invalid Agent export admission JSON")

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("Invalid Agent export admission JSON")
        return value

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=finite_float,
        )
        pending = [decoded]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                value.encode("utf-8")
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, dict):
                pending.extend(value)
                pending.extend(value.values())
        return decoded
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid Agent export admission JSON") from exc


def _safe_text(value):
    if "\0" in value:
        raise ValueError("Invalid Agent export identifier")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Invalid Agent export identifier") from exc
    return value


class AgentExportProviderInfo(models.ProviderInfo):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    provider: str = Field(strict=True, min_length=1, max_length=128)
    model: str = Field(strict=True, min_length=1, max_length=256)
    local: bool = Field(strict=True)
    streaming: bool = Field(default=False, strict=True)

    _strings = field_validator("provider", "model")(_safe_text)


class AgentExportRequest(models.WireModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    task_id: ExportIdentifier
    objective_revision: ExportCounter
    session_id: ExportIdentifier
    thread_id: ExportIdentifier
    epoch: ExportCounter
    source_event_ids: list[ExportIdentifier] = Field(max_length=32)
    source_revisions: dict[ExportIdentifier, ExportCounter] = Field(max_length=32)
    content_scope: models.ContentScope
    provider: AgentExportProviderInfo
    request_sha256: ExportDigest
    purpose: Literal["complete_count", "complete_generation"]
    request_nonce: ExportIdentifier

    _identifiers = field_validator("task_id", "session_id", "thread_id", "request_nonce")(
        _safe_text
    )

    @field_validator("provider", mode="before")
    @classmethod
    def native_provider(cls, value):
        if isinstance(value, models.ProviderInfo):
            return value.model_dump(mode="python")
        return value

    @model_validator(mode="after")
    def exact_sources(self):
        if len(set(self.source_event_ids)) != len(self.source_event_ids) or set(
            self.source_revisions
        ) != set(self.source_event_ids):
            raise ValueError("Agent export source bindings do not match")
        for source in self.source_event_ids:
            _safe_text(source)
        if len(self.model_dump_json().encode("utf-8")) > MAX_AGENT_EXPORT_BYTES:
            raise ValueError("Agent export admission exceeds its byte bound")
        return self

    def binding_digest(self):
        body = {key: getattr(self, key) for key in AgentExportRequest.model_fields}
        body["provider"] = self.provider.model_dump(mode="json")
        encoded = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class AgentExportAdmission(AgentExportRequest):
    binding_sha256: ExportDigest
    effective_scope: models.ContentScope
    journal_sequence: int = Field(strict=True, ge=0, le=models.MAX_PLAYBACK_GENERATION)

    def validate_request(self, request):
        admitted = AgentExportAdmission.model_validate(self)
        request = AgentExportRequest.model_validate(request)
        actual = {key: getattr(admitted, key) for key in AgentExportRequest.model_fields}
        expected = {key: getattr(request, key) for key in AgentExportRequest.model_fields}
        if (
            actual != expected
            or admitted.binding_sha256 != request.binding_digest()
            or combine_scopes(request.content_scope, admitted.effective_scope)
            != admitted.effective_scope
            or not scope_allowed(request.provider, admitted.effective_scope)
        ):
            raise ValueError("Agent export admission binding changed")
        return admitted
