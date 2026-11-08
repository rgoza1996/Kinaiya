"""Task-bound workspace media consultation; grants are native owner objects.

Only ConsultMediaRequest crosses the generated harness boundary. A request,
model response, filename or content scope can never create MediaSourceGrant.
This contract is separate from Session's continuous media-intake protocol.
"""

import hashlib
import json
import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 65536
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
Counter = Annotated[int, Field(strict=True, ge=1, le=9007199254740991)]
MediaOperation = Literal["sam_hands", "transcribe_video"]


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class MediaRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


def source_path(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 1024
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
    ):
        raise ValueError("Media source requires a relative workspace path")
    value.encode("utf-8")
    return value


class MediaTaskBinding(MediaRecord):
    task_id: Identifier
    objective_revision: Counter
    session_id: Identifier
    thread_id: Identifier
    epoch: Counter


class ConsultMediaRequest(MediaRecord):
    operation: MediaOperation
    source_path: str
    source_sha256: Digest

    _path = field_validator("source_path")(source_path)


class MediaTarget(MediaRecord):
    operation: MediaOperation
    provider: Literal["meta"] = "meta"
    model: Literal["sam-3.1", "muse-spark-1.3-contributor", "muse-wonder-1.0"]
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    # Owner text is part of the grant. Generated harness input cannot add a
    # prompt or smuggle unrelated workspace contents through this source grant.
    prompt: str = Field(min_length=1, max_length=8000)

    @field_validator("prompt")
    @classmethod
    def bounded_prompt(cls, value):
        if "\0" in value or len(value.encode("utf-8")) > 16000:
            raise ValueError("Media prompt exceeds its UTF-8 bound")
        return value

    @model_validator(mode="after")
    def exact_operation(self):
        if self.operation == "sam_hands":
            if self.model != "sam-3.1" or self.reasoning is not None or self.prompt != "hands":
                raise ValueError("SAM consultation requires its exact target")
        elif self.model == "muse-wonder-1.0":
            if self.reasoning != "max":
                raise ValueError("Wonder Standard video requires its exact max target")
        elif self.model != "muse-spark-1.3-contributor" or self.reasoning in {None, "max"}:
            raise ValueError("Video transcription requires its exact target")
        return self


class MediaPrimaryResultTarget(MediaRecord):
    """Separate owner grant for these exact derived results at primary export."""

    provider: Literal["meta"] = "meta"
    model: Literal["muse-spark-1.3-contributor", "muse-wonder-1.0"] = "muse-spark-1.3-contributor"
    local: Literal[False] = False


class MediaSourceGrant(MediaRecord):
    """Created only by trusted owner wiring after explicit source disclosure.

    The private scope is an exact media-operation grant, not an exception to
    the ordinary Contributor text, MCP, skill or plugin disclosure policies.
    Copying exact retained results into a primary request additionally requires
    primary_result_target and the native broker's final export check. This
    grant never covers unrelated private text, files or arbitrary history.
    """

    grant_id: Identifier
    binding: MediaTaskBinding
    source_path: str
    source_sha256: Digest
    source_bytes: int = Field(ge=16, le=MAX_SOURCE_BYTES)
    content_scope: Literal["public_synthetic", "private_cloud_authorized"]
    targets: tuple[MediaTarget, ...] = Field(min_length=1, max_length=2)
    primary_result_target: MediaPrimaryResultTarget | None = None
    required_operations: tuple[MediaOperation, ...] = Field(default=(), max_length=2)

    _path = field_validator("source_path")(source_path)

    @model_validator(mode="after")
    def unique_operations(self):
        available = {target.operation for target in self.targets}
        if len(available) != len(self.targets):
            raise ValueError("A media grant requires unique operation targets")
        if (
            len(set(self.required_operations)) != len(self.required_operations)
            or not set(self.required_operations) <= available
        ):
            raise ValueError("Required media operations must be a unique target subset")
        return self


def bounded_data(value):
    """Check bounded plain JSON before encoding a provider's result projection."""
    pending, visited = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 16 or visited > 4096:
            raise ValueError("Media result exceeds its JSON bounds")
        if type(item) is dict:
            if len(item) > 256 or any(type(key) is not str for key in item):
                raise ValueError("Invalid media result object")
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            if len(item) > 1024:
                raise ValueError("Media result list exceeds its bound")
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > 16000:
                raise ValueError("Media result text exceeds its bound")
            item.encode("utf-8")
        elif type(item) is int:
            if not -9007199254740991 <= item <= 9007199254740991:
                raise ValueError("Media result number exceeds its bound")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("Invalid media result number")
        elif item is not None and type(item) is not bool:
            raise ValueError("Media result requires plain JSON")
    if len(canonical(value)) > 48000:
        raise ValueError("Media result data exceeds its byte bound")
    return value


class MediaProviderResult(MediaRecord):
    """Native adapter observation, never raw wire bodies or credentials."""

    model: Literal["sam-3.1", "muse-spark-1.3-contributor", "muse-wonder-1.0"]
    provider_request_sha256: Digest
    provider_response_sha256: Digest
    text: str = Field(max_length=16000)
    data: dict[str, Any] = Field(default_factory=dict)

    _data = field_validator("data", mode="before")(bounded_data)

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        value.encode("utf-8")
        return value


class MediaConsultationResult(MediaProviderResult):
    status: Literal["completed"] = "completed"
    operation: MediaOperation
    binding: MediaTaskBinding
    source_path: str
    source_sha256: Digest
    source_bytes: int = Field(ge=16, le=MAX_SOURCE_BYTES)
    content_scope: Literal["public_synthetic", "private_cloud_authorized"]
    # Digest of the logical, task-bound operation, distinct from wire bytes.
    request_sha256: Digest
    result_sha256: Digest

    @model_validator(mode="after")
    def exact_result(self):
        body = self.model_dump(mode="json", exclude={"result_sha256"})
        if (
            self.result_sha256 != digest(body)
            or len(canonical(self.model_dump(mode="json"))) > MAX_RESULT_BYTES
        ):
            raise ValueError("Media result digest or byte bound is invalid")
        return self
