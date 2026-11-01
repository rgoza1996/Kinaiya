"""Strict owner provider declarations and immutable protected generations.

Saving a declaration never constructs an adapter, probes an endpoint, changes
an active role or authorizes disclosure. Each service owns those later fences.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import unicodedata
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from services.settings.credentials import CredentialVault, valid_key

MAX_REVISION = 9_007_199_254_740_990
_CONFIG_BYTES = 8192
_RECEIPT_BYTES = 4096
_OPERATIONS = {
    "profile_upsert",
    "profile_remove",
    "credential_import",
    "credential_reference",
    "credential_remove",
}
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Generation = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


def _text(value, limit):
    if value != value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Display text must be bounded and free of controls")
    try:
        if not 1 <= len(value.encode("utf-8")) <= limit:
            raise ValueError("Display text exceeds its UTF-8 bound")
    except UnicodeError:
        raise ValueError("Display text must be valid UTF-8") from None
    return value


def _origin(value, *, cloud, native=False):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not 1 <= len(value) <= 2048
            or len(value.encode("utf-8")) > 2048
            or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
            or not parsed.hostname
            or not parsed.hostname.isascii()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or port == 0
            or parsed.scheme
            not in ({"http", "https", "ws", "wss"} if native else {"http", "https"})
            or (not local and (not cloud or parsed.scheme != "https"))
            or (
                native
                and (
                    not local
                    or port is None
                    or parsed.path not in {"", "/", "/v1", "/v1/", "/llm", "/llm/"}
                )
            )
        ):
            raise ValueError()
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise ValueError(
            "Provider endpoint requires an explicit permitted origin without credentials"
        ) from None
    return value.rstrip("/")


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CredentialBinding(StrictSettings):
    id: Identifier
    generation: Generation


class TextProfile(StrictSettings):
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)

    @field_validator("model")
    @classmethod
    def display_model(cls, value):
        return _text(value, 256)


class MetaProfile(TextProfile):
    kind: Literal["meta"]
    allow_cloud: bool
    credential: CredentialBinding
    max_tokens: int = Field(default=2048, ge=16, le=65536)
    context_tokens: int | None = Field(default=None, ge=64, le=MAX_REVISION)
    reasoning_effort: Effort = "xhigh"

    @model_validator(mode="after")
    def bounds(self):
        if not self.allow_cloud:
            raise ValueError("Meta requires explicit cloud authorization")
        self.base_url = _origin(self.base_url, cloud=True)
        if self.context_tokens is not None and self.max_tokens >= self.context_tokens:
            raise ValueError("Response reserve must fit the declared context")
        return self


class CompatibleProfile(TextProfile):
    kind: Literal["compatible"]
    allow_cloud: bool = False
    credential: CredentialBinding | None = None
    max_tokens: int = Field(default=2048, ge=16, le=65536)
    context_tokens: int | None = Field(default=None, ge=64, le=MAX_REVISION)
    reasoning_effort: Effort | None = None
    timeout_s: int = Field(default=90, ge=1, le=900)

    @model_validator(mode="after")
    def bounds(self):
        self.base_url = _origin(self.base_url, cloud=self.allow_cloud)
        if self.context_tokens is not None and self.max_tokens >= self.context_tokens:
            raise ValueError("Response reserve must fit the declared context")
        return self


class LMStudioProfile(TextProfile):
    kind: Literal["lmstudio"]
    allow_cloud: bool = False
    max_tokens: int = Field(default=128, ge=16, le=2048)
    reasoning_effort: Literal["none"] = "none"
    timeout_s: int = Field(default=60, ge=1, le=900)

    @model_validator(mode="after")
    def native(self):
        if self.allow_cloud is not False:
            raise ValueError("LM Studio native transport is loopback only")
        self.base_url = _origin(self.base_url, cloud=False, native=True)
        if re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", self.model) is None:
            raise ValueError("LM Studio requires a bounded native model alias")
        return self


class LayaProfile(TextProfile):
    kind: Literal["laya"]
    allow_cloud: bool = False
    credential: CredentialBinding | None = None
    timeout_s: int = Field(default=5, ge=1, le=120)
    max_len: int | None = Field(default=None, ge=2, le=8192)
    head_max_len: int | None = Field(default=None, ge=1, le=8192)
    expected_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bounds(self):
        self.base_url = _origin(self.base_url, cloud=self.allow_cloud)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.model) is None:
            raise ValueError("Laya requires a registered model alias")
        if (
            self.max_len is not None
            and self.head_max_len is not None
            and self.head_max_len >= self.max_len
        ):
            raise ValueError("Laya head window must fit the token window")
        return self


ProfileConfig = Annotated[
    MetaProfile | CompatibleProfile | LMStudioProfile | LayaProfile,
    Field(discriminator="kind"),
]


class ProviderChange(StrictSettings):
    schema_version: Literal["1.0"]
    expected_revision: int = Field(ge=0, le=MAX_REVISION)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")
    operation: Literal[
        "profile_upsert",
        "profile_remove",
        "credential_import",
        "credential_reference",
        "credential_remove",
    ]
    profile_id: Identifier | None = None
    label: str | None = Field(default=None, min_length=1, max_length=128)
    config: ProfileConfig | None = None
    credential_id: Identifier | None = None
    key: SecretStr | None = None
    source: Literal["muse", "openrouter", "host_env"] | None = None
    reference: Identifier | None = None

    @field_validator("label")
    @classmethod
    def label_text(cls, value):
        return None if value is None else _text(value, 128)

    @field_validator("key")
    @classmethod
    def secret_key(cls, value):
        if value is not None:
            valid_key(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def exact_operation(self):
        fields = {
            "profile_upsert": {"profile_id", "label", "config"},
            "profile_remove": {"profile_id"},
            "credential_import": {"credential_id", "key"},
            "credential_reference": {"credential_id", "source", "reference"},
            "credential_remove": {"credential_id"},
        }[self.operation]
        if (
            self.model_fields_set
            - {"schema_version", "expected_revision", "idempotency_key", "operation"}
            - fields
        ):
            raise ValueError("Operation contains unrelated fields")
        required = fields - {"reference"}
        if any(getattr(self, field) is None for field in required):
            raise ValueError("Operation is missing a required field")
        if self.operation == "credential_reference" and (
            (self.source == "host_env") != (self.reference is not None)
        ):
            raise ValueError("Only a host environment alias accepts a reference")
        return self


class ProviderCatalogConflict(ValueError):
    pass


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _stored_json(value, limit):
    """Parse a projected bounded stored document without duplicate/unsafe JSON."""
    if type(value) is not str or not 1 <= len(value.encode("utf-8")) <= limit:
        raise ValueError("Stored provider document exceeds its bound")

    def unique(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError("Stored provider document has duplicate fields")
            result[key] = child
        return result

    def constant(_):
        raise ValueError("Stored provider document has an invalid number")

    try:
        decoded = json.loads(value, object_pairs_hook=unique, parse_constant=constant)
        pending, nodes = [(decoded, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 16 or nodes > 1024:
                raise ValueError("Stored provider document exceeds its structural bound")
            if type(item) is dict:
                if len(item) > 32:
                    raise ValueError("Stored provider document exceeds its field bound")
                pending.extend((key, depth + 1) for key in item)
                pending.extend((child, depth + 1) for child in item.values())
            elif type(item) is list:
                if len(item) > 64:
                    raise ValueError("Stored provider document exceeds its item bound")
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is str:
                item.encode("utf-8")
            elif type(item) is int:
                if not -MAX_REVISION <= item <= MAX_REVISION:
                    raise ValueError("Stored provider document has an unsafe integer")
            elif item is not None and type(item) is not bool:
                raise ValueError("Stored provider document has an invalid primitive")
        return decoded
    except (RecursionError, UnicodeError):
        raise ValueError("Stored provider document is invalid") from None


def _stored_receipt(value, key):
    receipt = _stored_json(value, _RECEIPT_BYTES)
    if (
        type(receipt) is not dict
        or set(receipt)
        != {
            "schema_version",
            "status",
            "revision",
            "idempotency_key",
            "operation",
            "result",
            "change_boundary",
        }
        or receipt["schema_version"] != "1.0"
        or receipt["status"] != "catalog_saved"
        or receipt["change_boundary"] != "role_owner"
        or type(receipt["revision"]) is not int
        or not 1 <= receipt["revision"] <= MAX_REVISION
        or receipt["idempotency_key"] != key
        or type(receipt["operation"]) is not str
        or receipt["operation"] not in _OPERATIONS
    ):
        raise ValueError("Stored provider receipt identity is invalid")
    operation, result = receipt["operation"], receipt["result"]
    fields = {"id", "generation", "removed"}
    if operation == "profile_upsert":
        fields.add("configuration_sha256")
    if (
        type(result) is not dict
        or set(result) != fields
        or type(result["id"]) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", result["id"]) is None
        or type(result["removed"]) is not bool
        or result["removed"] != operation.endswith("_remove")
    ):
        raise ValueError("Stored provider receipt result is invalid")
    generation = result["generation"]
    if operation.startswith("profile_"):
        if type(generation) is not int or not 1 <= generation <= 64:
            raise ValueError("Stored provider receipt generation is invalid")
    elif type(generation) is not str or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
        raise ValueError("Stored provider receipt generation is invalid")
    if operation == "profile_upsert" and (
        type(result["configuration_sha256"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", result["configuration_sha256"]) is None
    ):
        raise ValueError("Stored provider receipt digest is invalid")
    return receipt


def _storage(path, workspace, *, report_created=False):
    path = Path(os.path.abspath(path))
    if path != path.resolve() or path.is_relative_to(Path(workspace).resolve()):
        raise ValueError("Provider storage must be outside the worker workspace without aliases")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise ValueError("Provider storage directory must be owner controlled")
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    created = False
    if report_created:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            # An existing empty file is not proof that its native state never
            # existed. This open must not recreate a concurrently removed file.
            fd = os.open(path, flags)
    else:
        fd = os.open(path, flags | os.O_CREAT, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise ValueError("Provider storage must be an owner-only regular file")
        db = sqlite3.connect(str(path))
        try:
            actual = path.stat(follow_symlinks=False)
            if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("Provider storage identity changed")
        except BaseException:
            db.close()
            raise
        return (db, created) if report_created else db
    finally:
        os.close(fd)


class ProviderCatalog:
    def __init__(self, path, workspace, *, env_refs=None):
        self.db = _storage(path, workspace)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS catalog (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, digest_key BLOB NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, generation INTEGER NOT NULL, removed INTEGER NOT NULL CHECK(removed IN (0,1)))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS profile_versions (id TEXT NOT NULL, generation INTEGER NOT NULL, label TEXT NOT NULL, configuration_sha256 TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(id, generation))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT NOT NULL)"
            )
            self.vault = CredentialVault(self.db, env_refs=env_refs)
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO catalog VALUES (1, 0, ?)", (secrets.token_bytes(32),)
                )
            self._revision()
        except BaseException:
            self.db.close()
            raise

    def _revision(self):
        row = self.db.execute(
            "SELECT CASE WHEN typeof(revision)='integer' THEN revision END, "
            "CASE WHEN typeof(digest_key)='blob' AND length(digest_key)=32 THEN digest_key END "
            "FROM catalog WHERE id=1"
        ).fetchone()
        if (
            row is None
            or type(row[0]) is not int
            or not 0 <= row[0] <= MAX_REVISION
            or type(row[1]) is not bytes
            or len(row[1]) != 32
        ):
            raise ValueError("Stored provider catalog is invalid")
        return row

    def _profile(self, profile_id, generation, *, for_new):
        if (
            type(profile_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", profile_id) is None
            or (
                generation is not None
                and (type(generation) is not int or not 1 <= generation <= 64)
            )
        ):
            raise ValueError("Provider generation identity is invalid")
        row = self.db.execute(
            "SELECT CASE WHEN typeof(generation)='integer' THEN generation END, "
            "CASE WHEN typeof(removed)='integer' THEN removed END FROM profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if row is not None and (
            type(row[0]) is not int
            or not 1 <= row[0] <= 64
            or type(row[1]) is not int
            or row[1] not in {0, 1}
        ):
            raise ValueError("Stored provider generation is invalid")
        if row is None or (for_new and (row[1] or generation not in {None, row[0]})):
            raise ValueError("Provider profile is unavailable for new selection")
        generation = row[0] if generation is None else generation
        version = self.db.execute(
            "SELECT CASE WHEN typeof(label)='text' AND length(CAST(label AS BLOB))<=128 "
            "THEN label END, CASE WHEN typeof(configuration_sha256)='text' "
            "AND length(CAST(configuration_sha256 AS BLOB))=64 THEN configuration_sha256 END, "
            f"CASE WHEN typeof(body)='text' AND length(CAST(body AS BLOB))<={_CONFIG_BYTES} "
            "THEN body END FROM profile_versions WHERE id=? AND generation=?",
            (profile_id, generation),
        ).fetchone()
        if version is None:
            raise ValueError("Provider generation is unavailable")
        # Revalidate persisted declarations before they reach any factory.
        from pydantic import TypeAdapter

        config = TypeAdapter(ProfileConfig).validate_python(
            _stored_json(version[2], _CONFIG_BYTES), strict=True
        )
        canonical = _canonical(config.model_dump(mode="json"))
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != version[1]:
            raise ValueError("Provider configuration digest is invalid")
        credential = getattr(config, "credential", None)
        if type(version[0]) is not str:
            raise ValueError("Stored provider label is invalid")
        if (
            for_new
            and credential is not None
            and not self.vault.binding_exists(credential.id, credential.generation)
        ):
            raise ValueError("Provider credential generation is unavailable")
        return {
            "id": profile_id,
            "label": _text(version[0], 128),
            "generation": generation,
            "configuration_sha256": version[1],
            "removed": bool(row[1]),
            "config": config.model_dump(mode="json"),
        }

    def profile(self, profile_id, generation=None, *, for_new=True):
        """Return an exact declaration; this never resolves a key or adapter."""
        return self._profile(profile_id, generation, for_new=for_new)

    def credential(self, credential_id, generation, *, for_new=True):
        """Native factory use only; never include this result in owner metadata."""
        return self.vault.resolve(credential_id, generation, for_new=for_new)

    def snapshot(self):
        # Hold a read transaction so a different service's commit cannot mix
        # catalog revision, profiles and credential availability in one view.
        self.db.execute("BEGIN")
        try:
            revision, _ = self._revision()
            profiles = []
            rows = self.db.execute(
                "SELECT CASE WHEN typeof(id)='text' AND length(CAST(id AS BLOB))<=64 THEN id END "
                "FROM profiles ORDER BY id LIMIT 33"
            ).fetchall()
            if len(rows) > 32:
                raise ValueError("Stored provider count exceeds its bound")
            for (profile_id,) in rows:
                profile = self._profile(profile_id, None, for_new=False)
                kind = profile["config"]["kind"]
                binding = profile["config"].get("credential")
                profile["credential_available"] = binding is None or self.vault.binding_exists(
                    binding["id"], binding["generation"]
                )
                profile["capability"] = {
                    "declared_roles": ["decisions"]
                    if kind == "laya"
                    else ["agent", "converse", "auxiliary"]
                    if kind == "lmstudio"
                    else ["agent", "converse", "decisions", "auxiliary"],
                    "supported_inputs": ["text"],
                    "measured_complete_interface": kind == "lmstudio"
                    or (kind == "meta" and profile["config"]["context_tokens"] is not None),
                    "measured_stream_interface": kind == "lmstudio"
                    or (kind == "meta" and profile["config"]["context_tokens"] is not None),
                    "qualification": "not_checked",
                    "readiness": "unknown",
                }
                profiles.append(profile)
            result = {
                "schema_version": "1.0",
                "revision": revision,
                "change_boundary": "role_owner",
                "profiles": profiles,
                "credentials": self.vault.snapshot(),
                "known_references": [
                    {"source": "muse", "reference": None},
                    {"source": "openrouter", "reference": None},
                    *[
                        {"source": "host_env", "reference": name}
                        for name in sorted(self.vault.env_refs)
                    ],
                ],
                "credential_reference_boundary": "explicit_registration_snapshot",
                "limits": {
                    "profiles": 32,
                    "profile_generations": 64,
                    "credentials": 32,
                    "credential_generations": 32,
                    "receipts": 10000,
                },
            }
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def _upsert_profile(self, request):
        row = self.db.execute(
            "SELECT CASE WHEN typeof(generation)='integer' THEN generation END "
            "FROM profiles WHERE id=?",
            (request.profile_id,),
        ).fetchone()
        if row is None and self.db.execute("SELECT count(*) FROM profiles").fetchone()[0] >= 32:
            raise ValueError("Provider count requires owner maintenance")
        if row is not None and (type(row[0]) is not int or not 1 <= row[0] <= 64):
            raise ValueError("Stored provider generation is invalid")
        generation = 1 if row is None else row[0] + 1
        if generation > 64:
            raise ValueError("Provider generations require owner maintenance")
        binding = getattr(request.config, "credential", None)
        if binding is not None and not self.vault.binding_exists(binding.id, binding.generation):
            raise ValueError("Provider requires an existing active credential generation")
        body = _canonical(request.config.model_dump(mode="json"))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.db.execute(
            "INSERT INTO profile_versions VALUES (?, ?, ?, ?, ?)",
            (request.profile_id, generation, request.label, digest, body),
        )
        self.db.execute(
            "INSERT INTO profiles VALUES (?, ?, 0) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation, removed=0",
            (request.profile_id, generation),
        )
        return {
            "id": request.profile_id,
            "generation": generation,
            "configuration_sha256": digest,
            "removed": False,
        }

    def _remove_profile(self, request):
        row = self.db.execute(
            "SELECT CASE WHEN typeof(generation)='integer' THEN generation END, "
            "CASE WHEN typeof(removed)='integer' THEN removed END FROM profiles WHERE id=?",
            (request.profile_id,),
        ).fetchone()
        if row is not None and (
            type(row[0]) is not int
            or not 1 <= row[0] <= 64
            or type(row[1]) is not int
            or row[1] not in {0, 1}
        ):
            raise ValueError("Stored provider generation is invalid")
        if row is None or row[1]:
            raise ValueError("Provider is absent or already removed")
        self.db.execute("UPDATE profiles SET removed=1 WHERE id=?", (request.profile_id,))
        return {"id": request.profile_id, "generation": row[0], "removed": True}

    def change(self, request: ProviderChange):
        # Revalidate typed native callers too. Only the HMAC reaches storage;
        # even low-entropy accidentally supplied keys get no public hash oracle.
        request = ProviderChange.model_validate(
            request.model_dump(mode="python", exclude_unset=True)
        )
        payload = request.model_dump(mode="json")
        if request.key is not None:
            payload["key"] = request.key.get_secret_value()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            revision, digest_key = self._revision()
            digest = hmac.new(
                digest_key, _canonical(payload).encode("utf-8"), hashlib.sha256
            ).hexdigest()
            prior = self.db.execute(
                "SELECT CASE WHEN typeof(digest)='text' AND length(CAST(digest AS BLOB))=64 "
                "THEN digest END, CASE WHEN typeof(receipt)='text' "
                f"AND length(CAST(receipt AS BLOB))<={_RECEIPT_BYTES} THEN receipt END "
                "FROM operations WHERE key=?",
                (request.idempotency_key,),
            ).fetchone()
            if prior is not None:
                if type(prior[0]) is not str or re.fullmatch(r"[0-9a-f]{64}", prior[0]) is None:
                    raise RuntimeError("Stored provider operation digest is unavailable")
                if not hmac.compare_digest(prior[0], digest):
                    raise ProviderCatalogConflict(
                        "The request key belongs to another provider change"
                    )
                try:
                    receipt = _stored_receipt(prior[1], request.idempotency_key)
                except (ValueError, TypeError):
                    # A historical write may have committed even when its
                    # stored acknowledgment can no longer be validated.
                    raise RuntimeError("Stored provider operation receipt is unavailable") from None
                if (
                    receipt["operation"] != request.operation
                    or receipt["revision"] != request.expected_revision + 1
                    or receipt["result"]["id"] != (request.profile_id or request.credential_id)
                    or (
                        request.operation == "profile_upsert"
                        and receipt["result"]["configuration_sha256"]
                        != hashlib.sha256(
                            _canonical(request.config.model_dump(mode="json")).encode("utf-8")
                        ).hexdigest()
                    )
                ):
                    raise RuntimeError("Stored provider operation receipt is unavailable")
                self.db.rollback()
                return receipt
            if revision != request.expected_revision:
                raise ProviderCatalogConflict("Provider catalog changed; refresh before saving")
            if (
                revision >= MAX_REVISION
                or self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 10000
            ):
                raise ValueError("Provider catalog requires owner maintenance")
            if request.operation == "profile_upsert":
                result = self._upsert_profile(request)
            elif request.operation == "profile_remove":
                result = self._remove_profile(request)
            elif request.operation == "credential_remove":
                result = self.vault.remove(request.credential_id)
            else:
                key = (
                    request.key.get_secret_value()
                    if request.operation == "credential_import"
                    else self.vault.reference_key(request.source, request.reference)
                )
                result = self.vault.store(
                    request.credential_id,
                    key,
                    source="imported"
                    if request.operation == "credential_import"
                    else request.source,
                    reference=request.reference,
                )
            receipt = {
                "schema_version": "1.0",
                "status": "catalog_saved",
                "revision": revision + 1,
                "idempotency_key": request.idempotency_key,
                "operation": request.operation,
                "result": result,
                "change_boundary": "role_owner",
            }
            self.db.execute("UPDATE catalog SET revision=? WHERE id=1", (revision + 1,))
            self.db.execute(
                "INSERT INTO operations VALUES (?, ?, ?)",
                (request.idempotency_key, digest, _canonical(receipt)),
            )
            self.db.commit()
            return receipt
        except BaseException:
            self.db.rollback()
            raise

    def receipt(self, key):
        row = self.db.execute(
            "SELECT CASE WHEN typeof(receipt)='text' "
            f"AND length(CAST(receipt AS BLOB))<={_RECEIPT_BYTES} THEN receipt END "
            "FROM operations WHERE key=?",
            (key,),
        ).fetchone()
        return _stored_receipt(row[0], key) if row else None

    def close(self):
        self.db.close()
