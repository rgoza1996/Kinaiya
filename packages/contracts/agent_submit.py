"""Pure Agent submit admission grammar and identity checks.

This module supplies bounded wire grammar and identity validation only.
It performs no authentication, storage, execution, or completion work.
"""

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import ConfigDict, Field, field_validator, model_validator

from packages.contracts import models

MAX_AGENT_SUBMIT_BYTES = 65536
MAX_AGENT_SUBMIT_LOOKUP_BYTES = 4096
MAX_AGENT_SUBMIT_RESPONSE_BYTES = 69632
AGENT_SUBMIT_ADMISSION_TIMEOUT_S = 15.0
AGENT_SUBMIT_LOOKUP_TIMEOUT_S = 5.0

_JSON_ERROR = "Invalid Agent submit admission JSON"
_REQUEST_ERROR = "Invalid Agent submit request"
_ENTRY_ERROR = "Invalid Agent submit entry"
_RECEIPT_ERROR = "Invalid Agent submit receipt binding"
_LOOKUP_REQUEST_ERROR = "Invalid Agent submit lookup request"
_LOOKUP_RESULT_ERROR = "Invalid Agent submit lookup result"

_MAX_INT = models.MAX_PLAYBACK_GENERATION
_SCOPE_RANK = {"public_synthetic": 0, "private_cloud_authorized": 1, "local_only": 2}
_HEXDIGITS = frozenset("0123456789abcdef")
_AUXILIARY_ROLES = frozenset(get_args(models.AuxiliaryTask))

_CURRENT_PAYLOAD_KEYS = frozenset(
    {
        "task_id",
        "state",
        "op",
        "actor",
        "session_id",
        "thread_id",
        "epoch",
        "objective",
        "objective_revision",
        "source_event_ids",
        "source_revision",
        "requested_content_scope",
        "idempotency_key",
        "payload_digest",
        "harness_version",
        "model_pin",
        "explicit_skill",
        "plugin_versions",
        "content_scope",
    }
)
_HISTORICAL_OPTIONAL_KEYS = frozenset(
    {"model_pin", "explicit_skill", "plugin_versions", "requested_content_scope", "content_scope"}
)
_TOP_APPEND_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "thread_id",
        "epoch",
        "kind",
        "payload",
        "causes",
        "task_refs",
        "idempotency_key",
    }
)
_TOP_ENTRY_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "thread_id",
        "epoch",
        "kind",
        "payload",
        "causes",
        "task_refs",
        "idempotency_key",
        "event_id",
        "sequence",
        "origin",
        "created_at",
    }
)
_PIN_KEYS = frozenset(
    {
        "schema_version",
        "profile_id",
        "provider",
        "model",
        "local",
        "reasoning",
        "settings_revision",
        "catalog_binding",
        "catalog_revision",
    }
)
_PIN_REQUIRED_KEYS = frozenset({"profile_id", "provider", "model", "local", "settings_revision"})


class AgentSubmitUnavailable(ValueError):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__("Agent submission admission is unavailable")


class AgentSubmitConflict(ValueError):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__("Agent submission key conflicts with retained scope or content")


class AgentSubmitUnconfirmed(ValueError):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__("Agent submission persistence is unconfirmed")


def agent_submit_json(raw: bytes, *, maximum: int = MAX_AGENT_SUBMIT_BYTES) -> Any:
    if type(maximum) is not int or maximum <= 0 or maximum > MAX_AGENT_SUBMIT_RESPONSE_BYTES:
        raise ValueError(_JSON_ERROR) from None
    if type(raw) is not bytes:
        raise ValueError(_JSON_ERROR) from None
    if len(raw) > maximum:
        raise ValueError(_JSON_ERROR) from None

    def _pairs(items: Any) -> Any:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(_JSON_ERROR)
            result[key] = value
        return result

    def _constant(_text: Any) -> Any:
        raise ValueError(_JSON_ERROR)

    def _finite_float(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise ValueError(_JSON_ERROR) from None
        if not math.isfinite(value):
            raise ValueError(_JSON_ERROR)
        return value

    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_JSON_ERROR) from None
    try:
        pending: list[Any] = [decoded]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeError:
                    raise ValueError(_JSON_ERROR) from None
            elif isinstance(value, float):
                if not math.isfinite(value):
                    raise ValueError(_JSON_ERROR)
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_JSON_ERROR) from None
    return decoded


def _require_safe_id(value: Any, max_len: int, error: str) -> str:
    if type(value) is not str:
        raise ValueError(error) from None
    if not 1 <= len(value) <= max_len:
        raise ValueError(error) from None
    if "\0" in value:
        raise ValueError(error) from None
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(error) from None
    return value


def _require_exact_int(value: Any, lo: int, hi: int, error: str) -> int:
    if type(value) is not int:
        raise ValueError(error) from None
    if not lo <= value <= hi:
        raise ValueError(error) from None
    return value


def _require_scope(value: Any, error: str) -> str:
    if type(value) is not str or value not in _SCOPE_RANK:
        raise ValueError(error) from None
    return value


def _require_hex64(value: Any, error: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise ValueError(error) from None
    for ch in value:
        if ch not in _HEXDIGITS:
            raise ValueError(error) from None
    return value


def _validate_created_at(value: Any, error: str) -> Any:
    if isinstance(value, datetime):
        return value
    if type(value) is not str:
        raise ValueError(error) from None
    if not value or "\0" in value:
        raise ValueError(error) from None
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(error) from None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(error) from None
    return value


def _validate_model_pin(raw: Any, error: str) -> Any:
    if raw is None:
        return None
    if isinstance(raw, models.AgentModelPin):
        try:
            data = copy.deepcopy(raw.model_dump(mode="python"))
        except Exception:
            raise ValueError(error) from None
        raw = data
    if type(raw) is not dict:
        raise ValueError(error) from None
    for key in raw.keys():
        if type(key) is not str:
            raise ValueError(error) from None
    keys = set(raw.keys())
    if not keys.issubset(_PIN_KEYS):
        raise ValueError(error) from None
    if not _PIN_REQUIRED_KEYS.issubset(keys):
        raise ValueError(error) from None
    if "schema_version" in raw:
        if type(raw["schema_version"]) is not str or raw["schema_version"] != "1.0":
            raise ValueError(error) from None
    _require_safe_id(raw["profile_id"], 80, error)
    _require_safe_id(raw["provider"], 128, error)
    _require_safe_id(raw["model"], 256, error)
    if type(raw["local"]) is not bool:
        raise ValueError(error) from None
    if "reasoning" in raw:
        reasoning = raw["reasoning"]
        if reasoning is not None:
            if type(reasoning) is not str:
                raise ValueError(error) from None
            if len(reasoning) > 16:
                raise ValueError(error) from None
            if "\0" in reasoning:
                raise ValueError(error) from None
            try:
                reasoning.encode("utf-8")
            except UnicodeError:
                raise ValueError(error) from None
    _require_exact_int(raw["settings_revision"], 0, _MAX_INT, error)
    catalog_keys = {"catalog_binding", "catalog_revision"}
    if keys & catalog_keys:
        if not catalog_keys.issubset(keys):
            raise ValueError(error) from None
        binding, revision = raw["catalog_binding"], raw["catalog_revision"]
        if binding is None or revision is None:
            if binding is not None or revision is not None:
                raise ValueError(error) from None
        else:
            if type(binding) is not dict or set(binding) != {
                "id",
                "generation",
                "configuration_sha256",
            }:
                raise ValueError(error) from None
            identity = _require_safe_id(binding["id"], 64, error)
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", identity) is None:
                raise ValueError(error) from None
            _require_exact_int(binding["generation"], 1, 64, error)
            _require_hex64(binding["configuration_sha256"], error)
            _require_exact_int(revision, 0, _MAX_INT - 1, error)
            if raw["profile_id"] != identity:
                raise ValueError(error) from None
    # Preserve original omitted legacy fields and explicit null pairs. Native
    # entry identity compares the original wire projection, not model defaults.
    return raw


def _compute_digest(payload: dict[str, Any], is_pre_scope: bool, error: str) -> str:
    try:
        logical: dict[str, Any] = {
            "session_id": payload["session_id"],
            "thread_id": payload["thread_id"],
            "epoch": payload["epoch"],
            "objective": payload["objective"],
            "source_event_ids": payload["source_event_ids"],
            "source_revision": payload["source_revision"],
        }
        if not is_pre_scope:
            logical["content_scope"] = payload["requested_content_scope"]
        skill = payload.get("explicit_skill", None)
        if skill is not None:
            logical["explicit_skill"] = skill
        encoded = json.dumps(logical, sort_keys=True, separators=(",", ":"), default=str).encode()
    except Exception:
        raise ValueError(error) from None
    return hashlib.sha256(encoded).hexdigest()


def _validate_payload(
    payload: Any,
    envelope_session: str,
    envelope_thread: str,
    envelope_epoch: int,
    allow_historical: bool,
    error: str,
) -> bool:
    if type(payload) is not dict:
        raise ValueError(error) from None
    for key in payload.keys():
        if type(key) is not str:
            raise ValueError(error) from None
    keys = set(payload.keys())
    if not keys.issubset(_CURRENT_PAYLOAD_KEYS | {"auxiliary_models", "workspace_pin", "limits_pin"}):
        raise ValueError(error) from None
    missing = _CURRENT_PAYLOAD_KEYS - keys
    is_pre_scope = False
    if missing:
        if not allow_historical:
            raise ValueError(error) from None
        if not missing.issubset(_HISTORICAL_OPTIONAL_KEYS):
            raise ValueError(error) from None
        if "requested_content_scope" in missing:
            is_pre_scope = True
        if "content_scope" in missing and not is_pre_scope:
            raise ValueError(error) from None
    task_id = _require_safe_id(payload["task_id"], 128, error)
    if type(payload["state"]) is not str or payload["state"] != "accepted":
        raise ValueError(error) from None
    if type(payload["op"]) is not str or payload["op"] != "submit":
        raise ValueError(error) from None
    actor = payload["actor"]
    if type(actor) is not str or actor not in ("owner", "decisions"):
        raise ValueError(error) from None
    payload_session = _require_safe_id(payload["session_id"], 128, error)
    payload_thread = _require_safe_id(payload["thread_id"], 128, error)
    payload_epoch = _require_exact_int(payload["epoch"], 1, _MAX_INT, error)
    if payload_session != envelope_session or payload_thread != envelope_thread:
        raise ValueError(error) from None
    if payload_epoch != envelope_epoch:
        raise ValueError(error) from None
    objective = payload["objective"]
    if type(objective) is not str or not 1 <= len(objective) <= 16000:
        raise ValueError(error) from None
    try:
        objective.encode("utf-8")
    except UnicodeError:
        raise ValueError(error) from None
    objective_revision = _require_exact_int(payload["objective_revision"], 1, _MAX_INT, error)
    source_revision = _require_exact_int(payload["source_revision"], 1, _MAX_INT, error)
    if objective_revision != source_revision:
        raise ValueError(error) from None
    source_ids = payload["source_event_ids"]
    if type(source_ids) is not list or len(source_ids) > 32:
        raise ValueError(error) from None
    for item in source_ids:
        _require_safe_id(item, 128, error)
    if actor == "decisions" and len(source_ids) < 1:
        raise ValueError(error) from None
    requested: Any = None
    if not is_pre_scope:
        requested = _require_scope(payload["requested_content_scope"], error)
    logical_key = _require_safe_id(payload["idempotency_key"], 256, error)
    _ = logical_key
    digest = _require_hex64(payload["payload_digest"], error)
    _require_safe_id(payload["harness_version"], 128, error)
    if "model_pin" in payload:
        payload["model_pin"] = _validate_model_pin(payload["model_pin"], error)
    elif not allow_historical:
        raise ValueError(error) from None
    # Older native submissions retain their missing metadata. Explicit pins
    # describe this attempt and do not change the original logical digest.
    if "auxiliary_models" in payload:
        auxiliary = payload["auxiliary_models"]
        if type(auxiliary) is not dict or len(auxiliary) > 5:
            raise ValueError(error) from None
        for role, pin in auxiliary.items():
            if type(role) is not str or role not in _AUXILIARY_ROLES or pin is None:
                raise ValueError(error) from None
            auxiliary[role] = _validate_model_pin(pin, error)
    if "workspace_pin" in payload:
        from packages.contracts.workspace_settings import WorkspacePin

        raw_pin = payload["workspace_pin"]
        if type(raw_pin) is not dict:
            raise ValueError(error) from None
        try:
            checked_pin = WorkspacePin.model_validate(raw_pin).model_dump(mode="json")
        except ValueError:
            raise ValueError(error) from None
        if checked_pin != raw_pin:
            raise ValueError(error) from None
    if "limits_pin" in payload and payload["limits_pin"] is not None:
        from packages.contracts.task_limits import TaskLimitsPin

        raw_pin = payload["limits_pin"]
        if type(raw_pin) is not dict or set(raw_pin) != set(TaskLimitsPin.model_fields):
            raise ValueError(error) from None
        try:
            TaskLimitsPin.model_validate(raw_pin, strict=True)
        except ValueError:
            raise ValueError(error) from None
        # Server admission metadata does not enter the logical request digest.
        # Validate in place without filling defaults, changing representation,
        # or assigning today's authority to an old absent/null submission.
    if "explicit_skill" in payload:
        skill = payload["explicit_skill"]
        if skill is not None:
            _require_safe_id(skill, 256, error)
    elif not allow_historical:
        raise ValueError(error) from None
    else:
        skill = None
    if actor == "decisions" and payload.get("explicit_skill", None) is not None:
        raise ValueError(error) from None
    if "plugin_versions" in payload:
        plugins = payload["plugin_versions"]
        if type(plugins) is not dict:
            raise ValueError(error) from None
        for k, v in plugins.items():
            _require_safe_id(k, 128, error)
            _require_safe_id(v, 128, error)
    elif not allow_historical:
        raise ValueError(error) from None
    # Authentic pre-scope writers omitted both scopes. Interpret the missing
    # field conservatively without inserting metadata into their native proof.
    effective = _require_scope(payload.get("content_scope", "local_only"), error)
    if is_pre_scope:
        if effective != "local_only":
            raise ValueError(error) from None
    else:
        assert requested is not None
        if _SCOPE_RANK[effective] < _SCOPE_RANK[requested]:
            raise ValueError(error) from None
    recomputed = _compute_digest(payload, is_pre_scope, error)
    if digest != recomputed:
        raise ValueError(error) from None
    _ = task_id
    return is_pre_scope


def _raw_append_dict(request: Any, error: str) -> dict[str, Any]:
    if isinstance(request, dict):
        try:
            return copy.deepcopy(request)
        except Exception:
            raise ValueError(error) from None
    if isinstance(request, models.AppendRequest):
        try:
            return copy.deepcopy(request.model_dump(mode="python"))
        except Exception:
            raise ValueError(error) from None
    raise ValueError(error) from None


def _validate_proposal_dict(
    raw: dict[str, Any], is_entry: bool, allow_historical: bool, error: str
) -> dict[str, Any]:
    if type(raw) is not dict:
        raise ValueError(error) from None
    for key in raw.keys():
        if type(key) is not str:
            raise ValueError(error) from None
    allowed = _TOP_ENTRY_KEYS if is_entry else _TOP_APPEND_KEYS
    if not set(raw.keys()).issubset(allowed):
        raise ValueError(error) from None
    required = {
        "session_id",
        "thread_id",
        "epoch",
        "kind",
        "payload",
        "causes",
        "task_refs",
        "idempotency_key",
    }
    if is_entry:
        required = required | {"event_id", "sequence", "origin", "created_at"}
    if not required.issubset(set(raw.keys())):
        raise ValueError(error) from None
    if "schema_version" in raw:
        if type(raw["schema_version"]) is not str or raw["schema_version"] != "1.0":
            raise ValueError(error) from None
    envelope_session = _require_safe_id(raw["session_id"], 128, error)
    envelope_thread = _require_safe_id(raw["thread_id"], 128, error)
    envelope_epoch = _require_exact_int(raw["epoch"], 1, _MAX_INT, error)
    if type(raw["kind"]) is not str or raw["kind"] != "agent.command":
        raise ValueError(error) from None
    causes = raw["causes"]
    if type(causes) is not list or len(causes) > 32:
        raise ValueError(error) from None
    for item in causes:
        _require_safe_id(item, 128, error)
    task_refs = raw["task_refs"]
    if type(task_refs) is not list or len(task_refs) != 1:
        raise ValueError(error) from None
    _require_safe_id(task_refs[0], 128, error)
    if type(raw["payload"]) is not dict:
        raise ValueError(error) from None
    _validate_payload(
        raw["payload"], envelope_session, envelope_thread, envelope_epoch, allow_historical, error
    )
    if task_refs[0] != raw["payload"]["task_id"]:
        raise ValueError(error) from None
    native_key = raw["idempotency_key"]
    if type(native_key) is not str or not 1 <= len(native_key) <= 256:
        raise ValueError(error) from None
    if "\0" in native_key:
        raise ValueError(error) from None
    try:
        native_key.encode("utf-8")
    except UnicodeError:
        raise ValueError(error) from None
    expected_native = "agent:" + str(raw["payload"]["task_id"]) + ":agent.command:1"
    if native_key != expected_native:
        raise ValueError(error) from None
    if is_entry:
        _require_safe_id(raw["event_id"], 128, error)
        _require_exact_int(raw["sequence"], 1, _MAX_INT, error)
        if type(raw["origin"]) is not str or raw["origin"] != "agent-supervisor":
            raise ValueError(error) from None
        _validate_created_at(raw["created_at"], error)
    return raw


def submit_request(request: Any) -> models.AppendRequest:
    try:
        raw = _raw_append_dict(request, _REQUEST_ERROR)
        if set(raw.keys()) - _TOP_APPEND_KEYS:
            raise ValueError(_REQUEST_ERROR) from None
        if isinstance(request, models.JournalEntry):
            raise ValueError(_REQUEST_ERROR) from None
        if type(raw) is dict and any(k in _TOP_ENTRY_KEYS - _TOP_APPEND_KEYS for k in raw.keys()):
            raise ValueError(_REQUEST_ERROR) from None
        validated = _validate_proposal_dict(
            raw, is_entry=False, allow_historical=False, error=_REQUEST_ERROR
        )
        instance = models.AppendRequest.model_validate(validated)
        if len(instance.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_BYTES:
            raise ValueError(_REQUEST_ERROR) from None
        return instance
    except Exception:
        raise ValueError(_REQUEST_ERROR) from None


def submit_entry(entry: Any) -> models.JournalEntry:
    try:
        raw = _raw_append_dict(entry, _ENTRY_ERROR)
        validated = _validate_proposal_dict(
            raw, is_entry=True, allow_historical=True, error=_ENTRY_ERROR
        )
        instance = models.JournalEntry.model_validate(validated)
        if len(instance.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_RESPONSE_BYTES:
            raise ValueError(_ENTRY_ERROR) from None
        return instance
    except Exception:
        raise ValueError(_ENTRY_ERROR) from None


def _check_safe_field(value: Any, max_len: int, error: str) -> str:
    return _require_safe_id(value, max_len, error)


class AgentSubmitLookupRequest(models.WireModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, revalidate_instances="always", hide_input_in_errors=True
    )

    session_id: str = Field(strict=True, min_length=1, max_length=128)
    thread_id: str = Field(strict=True, min_length=1, max_length=128)
    epoch: int = Field(strict=True, ge=1, le=models.MAX_PLAYBACK_GENERATION)
    idempotency_key: str = Field(strict=True, min_length=1, max_length=256)

    @field_validator("session_id")
    @classmethod
    def _validate_session(cls, v: Any) -> Any:
        return _require_safe_id(v, 128, _LOOKUP_REQUEST_ERROR)

    @field_validator("thread_id")
    @classmethod
    def _validate_thread(cls, v: Any) -> Any:
        return _require_safe_id(v, 128, _LOOKUP_REQUEST_ERROR)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_key(cls, v: Any) -> Any:
        return _require_safe_id(v, 256, _LOOKUP_REQUEST_ERROR)

    @model_validator(mode="after")
    def _check_bounds(self) -> "AgentSubmitLookupRequest":
        try:
            if len(self.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_LOOKUP_BYTES:
                raise ValueError(_LOOKUP_REQUEST_ERROR)
        except Exception:
            raise ValueError(_LOOKUP_REQUEST_ERROR) from None
        return self


class AgentSubmitReceipt(models.WireModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, revalidate_instances="always", hide_input_in_errors=True
    )

    disposition: Literal["created", "retained"]
    entry: models.JournalEntry

    @field_validator("entry", mode="before")
    @classmethod
    def _validate_entry_raw(cls, v: Any) -> Any:
        if isinstance(v, str) or v is None:
            raise ValueError(_RECEIPT_ERROR) from None
        try:
            if isinstance(v, models.JournalEntry):
                try:
                    raw = copy.deepcopy(v.model_dump(mode="python"))
                except Exception:
                    raise ValueError(_RECEIPT_ERROR) from None
            elif isinstance(v, dict):
                try:
                    raw = copy.deepcopy(v)
                except Exception:
                    raise ValueError(_RECEIPT_ERROR) from None
            else:
                raise ValueError(_RECEIPT_ERROR) from None
            validated = _validate_proposal_dict(
                raw, is_entry=True, allow_historical=True, error=_RECEIPT_ERROR
            )
            return validated
        except Exception:
            raise ValueError(_RECEIPT_ERROR) from None

    @model_validator(mode="after")
    def _check_receipt(self) -> "AgentSubmitReceipt":
        try:
            if len(self.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_RESPONSE_BYTES:
                raise ValueError(_RECEIPT_ERROR)
        except Exception:
            raise ValueError(_RECEIPT_ERROR) from None
        return self

    def validate_request(self, request: Any) -> "AgentSubmitReceipt":
        try:
            if isinstance(self, dict):
                self_raw: Any = copy.deepcopy(self)
            else:
                try:
                    self_raw = copy.deepcopy(self.model_dump(mode="python"))
                except Exception:
                    raise ValueError(_RECEIPT_ERROR) from None
            receipt = AgentSubmitReceipt.model_validate(self_raw)
            try:
                proposal = submit_request(request)
            except Exception:
                raise ValueError(_RECEIPT_ERROR) from None
            entry_raw = copy.deepcopy(receipt.entry.model_dump(mode="python"))
            entry_dict = _validate_proposal_dict(
                entry_raw, is_entry=True, allow_historical=True, error=_RECEIPT_ERROR
            )
            entry = models.JournalEntry.model_validate(entry_dict)
            if receipt.disposition == "created":
                if (
                    entry.session_id != proposal.session_id
                    or entry.thread_id != proposal.thread_id
                    or entry.epoch != proposal.epoch
                    or entry.kind != proposal.kind
                    or entry.payload != proposal.payload
                    or entry.causes != proposal.causes
                    or entry.task_refs != proposal.task_refs
                    or entry.idempotency_key != proposal.idempotency_key
                    or entry.schema_version != proposal.schema_version
                ):
                    raise ValueError(_RECEIPT_ERROR) from None
            else:
                if (
                    entry.session_id != proposal.session_id
                    or entry.thread_id != proposal.thread_id
                    or entry.epoch != proposal.epoch
                ):
                    raise ValueError(_RECEIPT_ERROR) from None
                if entry.payload.get("idempotency_key") != proposal.payload.get("idempotency_key"):
                    raise ValueError(_RECEIPT_ERROR) from None
                winner_pre = "requested_content_scope" not in entry.payload
                if winner_pre:
                    if entry.payload.get("content_scope", "local_only") != "local_only":
                        raise ValueError(_RECEIPT_ERROR) from None
                    if proposal.payload.get("requested_content_scope") != "local_only":
                        raise ValueError(_RECEIPT_ERROR) from None
                    if proposal.payload.get("content_scope") != "local_only":
                        raise ValueError(_RECEIPT_ERROR) from None
                    for field in (
                        "session_id",
                        "thread_id",
                        "epoch",
                        "objective",
                        "source_event_ids",
                        "source_revision",
                    ):
                        if entry.payload.get(field) != proposal.payload.get(field):
                            raise ValueError(_RECEIPT_ERROR) from None
                    winner_skill = entry.payload.get("explicit_skill", None)
                    proposal_skill = proposal.payload.get("explicit_skill", None)
                    if winner_skill != proposal_skill:
                        raise ValueError(_RECEIPT_ERROR) from None
                else:
                    for field in (
                        "session_id",
                        "thread_id",
                        "epoch",
                        "objective",
                        "source_event_ids",
                        "source_revision",
                        "requested_content_scope",
                    ):
                        if entry.payload.get(field) != proposal.payload.get(field):
                            raise ValueError(_RECEIPT_ERROR) from None
                    if entry.payload.get("explicit_skill", None) != proposal.payload.get(
                        "explicit_skill", None
                    ):
                        raise ValueError(_RECEIPT_ERROR) from None
                    if entry.payload.get("payload_digest") != proposal.payload.get(
                        "payload_digest"
                    ):
                        raise ValueError(_RECEIPT_ERROR) from None
                winner_effective = entry.payload.get("content_scope", "local_only")
                proposal_effective = proposal.payload.get("content_scope")
                if _SCOPE_RANK[winner_effective] < _SCOPE_RANK[proposal_effective]:
                    raise ValueError(_RECEIPT_ERROR) from None
            if len(receipt.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_RESPONSE_BYTES:
                raise ValueError(_RECEIPT_ERROR) from None
            return receipt
        except Exception:
            raise ValueError(_RECEIPT_ERROR) from None


class AgentSubmitLookupResult(models.WireModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, revalidate_instances="always", hide_input_in_errors=True
    )

    status: Literal["recorded", "unobserved"]
    entry: models.JournalEntry | None

    @field_validator("entry", mode="before")
    @classmethod
    def _validate_lookup_entry_raw(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str):
            raise ValueError(_LOOKUP_RESULT_ERROR) from None
        try:
            if isinstance(v, models.JournalEntry):
                try:
                    raw = copy.deepcopy(v.model_dump(mode="python"))
                except Exception:
                    raise ValueError(_LOOKUP_RESULT_ERROR) from None
            elif isinstance(v, dict):
                try:
                    raw = copy.deepcopy(v)
                except Exception:
                    raise ValueError(_LOOKUP_RESULT_ERROR) from None
            else:
                raise ValueError(_LOOKUP_RESULT_ERROR) from None
            validated = _validate_proposal_dict(
                raw, is_entry=True, allow_historical=True, error=_LOOKUP_RESULT_ERROR
            )
            return validated
        except Exception:
            raise ValueError(_LOOKUP_RESULT_ERROR) from None

    @model_validator(mode="after")
    def _check_result(self) -> "AgentSubmitLookupResult":
        try:
            if self.status == "recorded" and self.entry is None:
                raise ValueError(_LOOKUP_RESULT_ERROR)
            if self.status == "unobserved" and self.entry is not None:
                raise ValueError(_LOOKUP_RESULT_ERROR)
            if len(self.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_RESPONSE_BYTES:
                raise ValueError(_LOOKUP_RESULT_ERROR)
        except Exception:
            raise ValueError(_LOOKUP_RESULT_ERROR) from None
        return self

    def validate_request(self, request: Any) -> "AgentSubmitLookupResult":
        try:
            if isinstance(self, dict):
                self_raw: Any = copy.deepcopy(self)
            else:
                try:
                    self_raw = copy.deepcopy(self.model_dump(mode="python"))
                except Exception:
                    raise ValueError(_LOOKUP_RESULT_ERROR) from None
            result = AgentSubmitLookupResult.model_validate(self_raw)
            if isinstance(request, dict):
                req_raw = copy.deepcopy(request)
            elif isinstance(request, AgentSubmitLookupRequest):
                try:
                    req_raw = copy.deepcopy(request.model_dump(mode="python"))
                except Exception:
                    raise ValueError(_LOOKUP_RESULT_ERROR) from None
            else:
                raise ValueError(_LOOKUP_RESULT_ERROR) from None
            lookup = AgentSubmitLookupRequest.model_validate(req_raw)
            if result.status == "unobserved":
                if result.entry is not None:
                    raise ValueError(_LOOKUP_RESULT_ERROR) from None
            else:
                if result.entry is None:
                    raise ValueError(_LOOKUP_RESULT_ERROR) from None
                entry_raw = copy.deepcopy(result.entry.model_dump(mode="python"))
                entry_dict = _validate_proposal_dict(
                    entry_raw, is_entry=True, allow_historical=True, error=_LOOKUP_RESULT_ERROR
                )
                validated_entry = models.JournalEntry.model_validate(entry_dict)
                result.entry = validated_entry
                if (
                    validated_entry.session_id != lookup.session_id
                    or validated_entry.thread_id != lookup.thread_id
                    or validated_entry.epoch != lookup.epoch
                    or validated_entry.payload.get("idempotency_key") != lookup.idempotency_key
                ):
                    raise ValueError(_LOOKUP_RESULT_ERROR) from None
            if len(result.model_dump_json().encode("utf-8")) > MAX_AGENT_SUBMIT_RESPONSE_BYTES:
                raise ValueError(_LOOKUP_RESULT_ERROR) from None
            return result
        except Exception:
            raise ValueError(_LOOKUP_RESULT_ERROR) from None
