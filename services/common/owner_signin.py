"""Single-use trusted local owner sign-in handoff and scoped session broker."""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import os
import secrets
import time
import uuid
from collections.abc import Callable
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from services.common.auth import (
    _check_fd_security,
    _fsync_directory,
    _read_validated_tokens,
    _resolve_store_paths,
    _store_lock,
    _write_tokens_atomic,
    load_or_create_tokens,
)

HANDOFF_COOKIE_NAME = "kinaiya_local_handoff"
HANDOFF_PATHS: tuple[str, ...] = ("/__kinaiya/local-signin", "/api/local-owner-handoff")
HANDOFF_FILE_NAME = "local-owner-handoff.json"
SESSIONS_FILE_NAME = "local-owner-sessions.json"
RECOVERY_CHALLENGE_FILE_NAME = "local-recovery-challenge.json"
SESSION_TOKEN_PREFIX = "kny_sess_"
DEFAULT_HANDOFF_TTL_S = 60.0
DEFAULT_SESSION_TTL_S = 8 * 3600.0
DEFAULT_RECOVERY_CHALLENGE_TTL_S = 120.0
RECOVERY_PATHS: tuple[str, ...] = ("/__kinaiya/recover-access", "/api/recover-access")
CHALLENGE_PATHS: tuple[str, ...] = ("/__kinaiya/recovery/challenge", "/api/recovery/challenge")
ROTATION_PATHS: tuple[str, ...] = ("/__kinaiya/rotate-owner", "/api/rotate-owner")
STORAGE_STATUS_PATHS: tuple[str, ...] = ("/__kinaiya/storage-status", "/api/storage-status")


class LocalSignInError(RuntimeError, ValueError):
    """Base error for failed local sign-in handoff or scoped session verification."""

    status_code: int = 403


class LocalSignInDenied(LocalSignInError):
    """Handoff was missing, replayed, cross-origin, or from an untrusted caller."""

    status_code: int = 403


class LocalSignInExpired(LocalSignInDenied):
    """Single-use handoff or scoped owner session expired."""

    status_code: int = 403


class LocalSignInStale(LocalSignInError):
    """Host generation changed or underlying owner credential was rotated."""

    status_code: int = 401


class StorageUnavailable(LocalSignInError):
    """Underlying private credential storage is missing, inaccessible, or corrupt."""

    status_code: int = 503


def is_loopback_host(host: str | None) -> bool:
    if not host or not isinstance(host, str):
        return False
    cleaned = host.strip().lower()
    if cleaned in {"localhost", "testclient"}:
        return True
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _read_private_json(path: Path, *, label: str) -> dict[str, Any]:
    if os.path.islink(path) or os.path.islink(path.parent):
        raise PermissionError(f"{label} cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == getattr(errno, "ELOOP", 40):
            raise PermissionError(f"{label} cannot be a symlink") from None
        raise
    try:
        _check_fd_security(fd, label=label)
        raw = os.read(fd, 64 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 64 * 1024:
        raise ValueError(f"{label} exceeds size limit")
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    _write_tokens_atomic(path, payload)
    _fsync_directory(path.parent)


def parse_handoff_cookie(cookie_header: str | None) -> str | None:
    if not cookie_header or not isinstance(cookie_header, str) or len(cookie_header) > 8192:
        return None
    # Reject ambiguous duplicate handoff cookies.
    parts = [part.strip() for part in cookie_header.split(";") if part.strip()]
    matches: list[str] = []
    for part in parts:
        if "=" not in part:
            continue
        name, val = part.split("=", 1)
        if name.strip() == HANDOFF_COOKIE_NAME:
            matches.append(val.strip())
    if len(matches) != 1:
        return None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(HANDOFF_COOKIE_NAME)
        value = morsel.value if morsel is not None else matches[0]
    except Exception:
        value = matches[0]
    if not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        return None
    return value


class SessionTokenManager:
    """Manages scoped owner session tokens bound to host_generation and owner_epoch."""

    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory, self.tokens_path = _resolve_store_paths(data_dir)
        self.handoff_path = self.directory / HANDOFF_FILE_NAME
        self.sessions_path = self.directory / SESSIONS_FILE_NAME
        self.clock = clock

    def _current_owner_binding(self) -> tuple[int, str]:
        tokens = _read_validated_tokens(self.tokens_path)
        owner_sha = hashlib.sha256(tokens["owner"].encode("utf-8")).hexdigest()
        return tokens.owner_epoch, owner_sha

    def issue_session_token(
        self,
        *,
        host_generation: str,
        owner_epoch: int,
        owner_token_sha256: str,
        ttl_seconds: float = DEFAULT_SESSION_TTL_S,
    ) -> dict[str, Any]:
        now = float(self.clock())
        expires_at = now + float(ttl_seconds)
        raw_token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(36)
        token_sha256 = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        session_id = uuid.uuid4().hex

        try:
            existing = _read_private_json(self.sessions_path, label="Owner session store")
            sessions = list(existing.get("sessions", []))
        except FileNotFoundError:
            sessions = []

        # Prune expired or superseded sessions so the store stays bounded.
        active_sessions = [
            s
            for s in sessions
            if isinstance(s, dict)
            and not s.get("revoked", False)
            and isinstance(s.get("expires_at"), (int, float))
            and float(s["expires_at"]) > now
            and s.get("host_generation") == host_generation
            and s.get("owner_epoch") == owner_epoch
            and s.get("owner_token_sha256") == owner_token_sha256
        ][-31:]

        active_sessions.append(
            {
                "session_token_id": session_id,
                "token_sha256": token_sha256,
                "role": "owner",
                "host_generation": host_generation,
                "owner_epoch": owner_epoch,
                "owner_token_sha256": owner_token_sha256,
                "issued_at": now,
                "expires_at": expires_at,
                "revoked": False,
            }
        )
        _write_private_json(
            self.sessions_path,
            {
                "schema_version": 1,
                "host_generation": host_generation,
                "owner_epoch": owner_epoch,
                "sessions": active_sessions,
            },
        )
        return {
            "session_token": raw_token,
            "session_token_id": session_id,
            "host_generation": host_generation,
            "owner_epoch": owner_epoch,
            "issued_at": now,
            "expires_at": expires_at,
        }

    def verify_session_token(
        self,
        token: str,
        *,
        expected_owner_epoch: int | None = None,
        expected_owner_token_sha256: str | None = None,
        expected_host_generation: str | None = None,
    ) -> str:
        if (
            not isinstance(token, str)
            or not token.startswith(SESSION_TOKEN_PREFIX)
            or len(token) < 32
        ):
            raise LocalSignInDenied("Invalid scoped owner session token")
        try:
            cur_epoch, cur_owner_sha = self._current_owner_binding()
            handoff = _read_private_json(self.handoff_path, label="Local owner handoff state")
            store = _read_private_json(self.sessions_path, label="Owner session store")
        except (OSError, PermissionError, ValueError) as exc:
            raise LocalSignInStale("Owner session state is unavailable or invalidated") from exc

        if expected_owner_epoch is not None and cur_epoch != expected_owner_epoch:
            raise LocalSignInStale("Owner credential epoch changed")
        if expected_owner_token_sha256 is not None and not secrets.compare_digest(
            cur_owner_sha, expected_owner_token_sha256
        ):
            raise LocalSignInStale("Owner credential was rotated")

        active_host_gen = handoff.get("host_generation")
        if not isinstance(active_host_gen, str) or not active_host_gen:
            raise LocalSignInStale("Missing active host generation")
        if expected_host_generation is not None and active_host_gen != expected_host_generation:
            raise LocalSignInStale("Host generation was superseded")

        candidate_sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = float(self.clock())
        sessions = store.get("sessions", [])
        if not isinstance(sessions, list):
            raise LocalSignInDenied("Malformed session store")

        for record in sessions:
            if not isinstance(record, dict):
                continue
            stored_sha = record.get("token_sha256")
            if not isinstance(stored_sha, str) or not secrets.compare_digest(
                stored_sha, candidate_sha
            ):
                continue
            if record.get("revoked", False):
                raise LocalSignInStale("Scoped owner session was revoked")
            expires_at = record.get("expires_at")
            if not isinstance(expires_at, (int, float)) or now >= float(expires_at):
                raise LocalSignInExpired("Scoped owner session has expired")
            if record.get("host_generation") != active_host_gen:
                raise LocalSignInStale("Scoped owner session belongs to a stale host generation")
            if record.get("owner_epoch") != cur_epoch:
                raise LocalSignInStale("Scoped owner session belongs to a stale owner epoch")
            rec_owner_sha = record.get("owner_token_sha256")
            if not isinstance(rec_owner_sha, str) or not secrets.compare_digest(
                rec_owner_sha, cur_owner_sha
            ):
                raise LocalSignInStale("Owner credential was rotated")
            return "owner"

        raise LocalSignInDenied("Unknown scoped owner session token")


class LocalOwnerSignInBroker:
    """Issues and consumes a single-use loopback launch handoff for the owner web UI."""

    def __init__(
        self,
        data_dir: Path,
        *,
        allowed_origins: tuple[str, ...] | None = None,
        host_generation: str | None = None,
        ttl_seconds: float = DEFAULT_HANDOFF_TTL_S,
        session_ttl_seconds: float = DEFAULT_SESSION_TTL_S,
        session_url: str = "http://127.0.0.1:8760",
        agent_url: str = "http://127.0.0.1:8761",
        converse_url: str = "http://127.0.0.1:8762",
        decisions_url: str | None = "http://127.0.0.1:8763",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0 or session_ttl_seconds <= 0:
            raise ValueError("Handoff and session TTLs must be positive")
        self.directory, self.tokens_path = _resolve_store_paths(data_dir)
        self.handoff_path = self.directory / HANDOFF_FILE_NAME
        self.clock = clock
        self.ttl_seconds = float(ttl_seconds)
        self.session_ttl_seconds = float(session_ttl_seconds)
        self.session_url = session_url
        self.agent_url = agent_url
        self.converse_url = converse_url
        self.decisions_url = decisions_url
        self.allow_dynamic_loopback_port = allowed_origins is None
        self.allowed_origins: tuple[str, ...] = (
            tuple(allowed_origins)
            if allowed_origins is not None
            else ("http://127.0.0.1:8780", "http://localhost:8780")
        )
        self.host_generation = host_generation or secrets.token_hex(16)
        self.session_manager = SessionTokenManager(self.directory, clock=clock)

        tokens = load_or_create_tokens(self.directory)
        self.owner_epoch: int = getattr(tokens, "owner_epoch", 1)
        self.owner_token_sha256: str = hashlib.sha256(tokens["owner"].encode("utf-8")).hexdigest()
        self.grant_id: str = ""
        self._unsealed_cookie_seal: str | None = None
        self._mint_launch_grant()

    @classmethod
    def from_auth(
        cls,
        auth: Any,
        *,
        clock: Callable[[], float] | None = None,
    ) -> LocalOwnerSignInBroker:
        store_path = getattr(auth, "token_store_path", None)
        if store_path is None:
            raise LocalSignInDenied("Authenticator is not bound to a local credential store")
        chosen_clock = clock or getattr(auth, "_session_clock", None) or time.time
        return cls._attach_existing(Path(store_path).parent, clock=chosen_clock)

    @classmethod
    def _attach_existing(
        cls,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> LocalOwnerSignInBroker:
        instance = object.__new__(cls)
        instance.directory, instance.tokens_path = _resolve_store_paths(data_dir)
        instance.handoff_path = instance.directory / HANDOFF_FILE_NAME
        instance.clock = clock
        instance.session_manager = SessionTokenManager(instance.directory, clock=clock)
        handoff = _read_private_json(instance.handoff_path, label="Local owner handoff state")
        instance.host_generation = str(handoff["host_generation"])
        instance.owner_epoch = int(handoff["owner_epoch"])
        instance.owner_token_sha256 = str(handoff["owner_token_sha256"])
        instance.grant_id = str(handoff["grant_id"])
        instance.ttl_seconds = float(handoff.get("ttl_seconds", DEFAULT_HANDOFF_TTL_S))
        instance.session_ttl_seconds = float(
            handoff.get("session_ttl_seconds", DEFAULT_SESSION_TTL_S)
        )
        instance.allowed_origins = tuple(handoff.get("allowed_origins", ()))
        instance.allow_dynamic_loopback_port = bool(
            handoff.get("allow_dynamic_loopback_port", False)
        )
        instance.session_url = str(handoff.get("session_url", "http://127.0.0.1:8760"))
        instance.agent_url = str(handoff.get("agent_url", "http://127.0.0.1:8761"))
        instance.converse_url = str(handoff.get("converse_url", "http://127.0.0.1:8762"))
        instance.decisions_url = handoff.get("decisions_url", "http://127.0.0.1:8763")
        instance._unsealed_cookie_seal = None
        return instance

    def _mint_launch_grant(self) -> None:
        with _store_lock(self.directory):
            tokens = _read_validated_tokens(self.tokens_path)
            self.owner_epoch = tokens.owner_epoch
            self.owner_token_sha256 = hashlib.sha256(tokens["owner"].encode("utf-8")).hexdigest()
            self.grant_id = uuid.uuid4().hex
            raw_seal = secrets.token_urlsafe(36)
            seal_sha256 = hashlib.sha256(raw_seal.encode("utf-8")).hexdigest()
            now = float(self.clock())
            payload: dict[str, Any] = {
                "schema_version": 1,
                "grant_id": self.grant_id,
                "host_generation": self.host_generation,
                "owner_epoch": self.owner_epoch,
                "owner_token_sha256": self.owner_token_sha256,
                "seal_sha256": seal_sha256,
                "state": "unsealed",
                "issued_at": now,
                "expires_at": now + self.ttl_seconds,
                "ttl_seconds": self.ttl_seconds,
                "session_ttl_seconds": self.session_ttl_seconds,
                "allowed_origins": list(self.allowed_origins),
                "allow_dynamic_loopback_port": self.allow_dynamic_loopback_port,
                "session_url": self.session_url,
                "agent_url": self.agent_url,
                "converse_url": self.converse_url,
                "decisions_url": self.decisions_url,
            }
            _write_private_json(self.handoff_path, payload)
            self._unsealed_cookie_seal = raw_seal

    def rotate_host_generation(self, new_generation: str | None = None) -> str:
        """Simulate or perform a host restart that invalidates prior handoffs and sessions."""
        self.host_generation = new_generation or secrets.token_hex(16)
        self._mint_launch_grant()
        return self.host_generation

    def permitted_origins_for_port(self, server_port: int | None = None) -> tuple[str, ...]:
        if self.allow_dynamic_loopback_port and server_port is not None:
            return (
                f"http://127.0.0.1:{server_port}",
                f"http://localhost:{server_port}",
                *self.allowed_origins,
            )
        return self.allowed_origins

    def seal_initial_launch_cookie(
        self,
        *,
        client_host: str | None,
        origin: str | None = None,
        sec_fetch_site: str | None = None,
        server_port: int | None = None,
    ) -> str | None:
        """On the first trusted GET / request, seal the launch grant and return Set-Cookie."""
        if not is_loopback_host(client_host):
            return None
        if sec_fetch_site and sec_fetch_site.strip().lower() in {"cross-site", "same-site"}:
            return None
        permitted = self.permitted_origins_for_port(server_port)
        if origin and origin not in permitted:
            return None
        with _store_lock(self.directory):
            if self._unsealed_cookie_seal is None:
                return None
            try:
                handoff = _read_private_json(self.handoff_path, label="Local owner handoff state")
                tokens = _read_validated_tokens(self.tokens_path)
            except (OSError, PermissionError, ValueError):
                self._unsealed_cookie_seal = None
                return None

            now = float(self.clock())
            current_owner_sha = hashlib.sha256(tokens["owner"].encode("utf-8")).hexdigest()
            if (
                handoff.get("state") != "unsealed"
                or handoff.get("grant_id") != self.grant_id
                or handoff.get("host_generation") != self.host_generation
                or handoff.get("owner_epoch") != tokens.owner_epoch
                or not secrets.compare_digest(
                    str(handoff.get("owner_token_sha256", "")), current_owner_sha
                )
                or now >= float(handoff.get("expires_at", 0))
            ):
                self._unsealed_cookie_seal = None
                return None

            seal = self._unsealed_cookie_seal
            self._unsealed_cookie_seal = None
            handoff["state"] = "sealed"
            handoff["sealed_at"] = now
            if server_port is not None and self.allow_dynamic_loopback_port:
                handoff["allowed_origins"] = [
                    f"http://127.0.0.1:{server_port}",
                    f"http://localhost:{server_port}",
                ]
                self.allowed_origins = tuple(handoff["allowed_origins"])
                self.allow_dynamic_loopback_port = False
                handoff["allow_dynamic_loopback_port"] = False
            _write_private_json(self.handoff_path, handoff)
            max_age = max(1, int(float(handoff["expires_at"]) - now))
            return (
                f"{HANDOFF_COOKIE_NAME}={seal}; HttpOnly; SameSite=Strict; "
                f"Path=/__kinaiya/local-signin; Max-Age={max_age}"
            )

    def consume_handoff(
        self,
        *,
        cookie_seal: str | None,
        origin: str | None,
        csrf_header: str | None,
        client_host: str | None,
        sec_fetch_site: str | None = None,
        server_port: int | None = None,
        expected_host_generation: str | None = None,
    ) -> dict[str, Any]:
        """Validate and atomically consume the single-use handoff to issue a scoped session token."""
        if not is_loopback_host(client_host):
            raise LocalSignInDenied("Local sign-in requires a loopback client address")
        if csrf_header != "1":
            raise LocalSignInDenied("Missing required X-Kinaiya-Local-Handoff header")
        if sec_fetch_site is not None and sec_fetch_site.strip().lower() != "same-origin":
            raise LocalSignInDenied("Cross-site local sign-in request refused")
        permitted = self.permitted_origins_for_port(server_port)
        if not origin or origin not in permitted:
            raise LocalSignInDenied("Browser origin is not permitted for local sign-in")
        if not cookie_seal or not isinstance(cookie_seal, str):
            raise LocalSignInDenied("Missing single-use local sign-in handoff cookie")

        with _store_lock(self.directory):
            try:
                handoff = _read_private_json(self.handoff_path, label="Local owner handoff state")
                tokens = _read_validated_tokens(self.tokens_path)
            except (OSError, PermissionError, ValueError) as exc:
                raise LocalSignInStale("Local sign-in state is unavailable") from exc

            now = float(self.clock())
            current_owner_sha = hashlib.sha256(tokens["owner"].encode("utf-8")).hexdigest()
            target_gen = expected_host_generation or self.host_generation
            if handoff.get("host_generation") != target_gen:
                raise LocalSignInStale("Local sign-in handoff belongs to a stale host generation")
            if handoff.get("owner_epoch") != tokens.owner_epoch or not secrets.compare_digest(
                str(handoff.get("owner_token_sha256", "")), current_owner_sha
            ):
                raise LocalSignInStale("Owner credential was rotated; handoff invalidated")

            state = handoff.get("state")
            if state == "consumed":
                raise LocalSignInDenied("Local sign-in handoff has already been consumed")
            if state != "sealed":
                raise LocalSignInDenied("Local sign-in handoff was not initialized by launch")

            expires_at = float(handoff.get("expires_at", 0))
            if now >= expires_at:
                handoff["state"] = "expired"
                handoff["seal_sha256"] = None
                _write_private_json(self.handoff_path, handoff)
                raise LocalSignInExpired("Local sign-in handoff has expired")

            stored_seal_sha = handoff.get("seal_sha256")
            candidate_seal_sha = hashlib.sha256(cookie_seal.encode("utf-8")).hexdigest()
            if not isinstance(stored_seal_sha, str) or not secrets.compare_digest(
                stored_seal_sha, candidate_seal_sha
            ):
                raise LocalSignInDenied("Invalid local sign-in handoff seal")

            # Atomically mark consumed and wipe seal digest before issuing session token.
            handoff["state"] = "consumed"
            handoff["seal_sha256"] = None
            handoff["consumed_at"] = now
            _write_private_json(self.handoff_path, handoff)

            issued = self.session_manager.issue_session_token(
                host_generation=str(handoff["host_generation"]),
                owner_epoch=tokens.owner_epoch,
                owner_token_sha256=current_owner_sha,
                ttl_seconds=self.session_ttl_seconds,
            )
            return {
                "status": "authenticated",
                "role": "owner",
                "credential_kind": "scoped_owner_session",
                "owner_token": issued["session_token"],
                "session_token": issued["session_token"],
                "host_generation": issued["host_generation"],
                "owner_epoch": issued["owner_epoch"],
                "expires_at": issued["expires_at"],
                "session_url": self.session_url,
                "agent_url": self.agent_url,
                "converse_url": self.converse_url,
                "decisions_url": self.decisions_url,
            }

    def check_storage_status(self) -> dict[str, Any]:
        """Check underlying private storage health without modifying or reinitializing it."""
        try:
            tokens = _read_validated_tokens(self.tokens_path)
            return {
                "status": "ready",
                "owner_epoch": tokens.owner_epoch,
                "rotated_at": getattr(tokens, "rotated_at", None),
                "tokens_path": str(self.tokens_path.resolve()),
            }
        except FileNotFoundError:
            return {
                "status": "missing",
                "detail": f"Private credential storage does not exist: {self.tokens_path}. Run kinaiya init first.",
                "tokens_path": str(self.tokens_path),
            }
        except PermissionError as exc:
            return {
                "status": "permission_denied",
                "detail": f"Private credential storage permission failure: {exc}",
                "tokens_path": str(self.tokens_path),
            }
        except ValueError as exc:
            return {
                "status": "corrupt",
                "detail": f"Private credential storage is corrupt or malformed: {exc}",
                "tokens_path": str(self.tokens_path),
            }

    def create_recovery_challenge(
        self,
        *,
        client_host: str | None,
        origin: str | None,
        csrf_header: str | None,
        sec_fetch_site: str | None = None,
        server_port: int | None = None,
    ) -> dict[str, Any]:
        """Generate a single-use local recovery challenge written to private local storage."""
        if not is_loopback_host(client_host):
            raise LocalSignInDenied("Recovery challenge requires a loopback client address")
        if csrf_header not in {"1", "true"}:
            raise LocalSignInDenied("Missing required X-Kinaiya-Local-Handoff header")
        if sec_fetch_site is not None and sec_fetch_site.strip().lower() != "same-origin":
            raise LocalSignInDenied("Cross-site recovery challenge request refused")
        permitted = self.permitted_origins_for_port(server_port)
        if not origin or origin not in permitted:
            raise LocalSignInDenied("Browser origin is not permitted for recovery challenge")

        storage = self.check_storage_status()
        if storage["status"] != "ready":
            raise StorageUnavailable(storage["detail"])

        with _store_lock(self.directory):
            now = float(self.clock())
            challenge_id = uuid.uuid4().hex
            auth_code = "kny_rec_" + secrets.token_urlsafe(32)
            code_sha256 = hashlib.sha256(auth_code.encode("utf-8")).hexdigest()
            payload = {
                "schema_version": 1,
                "challenge_id": challenge_id,
                "code_sha256": code_sha256,
                "code": auth_code,
                "issued_at": now,
                "expires_at": now + DEFAULT_RECOVERY_CHALLENGE_TTL_S,
                "ttl_seconds": DEFAULT_RECOVERY_CHALLENGE_TTL_S,
                "host_generation": self.host_generation,
            }
            challenge_path = self.directory / RECOVERY_CHALLENGE_FILE_NAME
            _write_private_json(challenge_path, payload)
            return {
                "challenge_id": challenge_id,
                "challenge_path": str(challenge_path.resolve()),
                "expires_at": now + DEFAULT_RECOVERY_CHALLENGE_TTL_S,
                "ttl_seconds": DEFAULT_RECOVERY_CHALLENGE_TTL_S,
            }

    def verify_local_authorization(
        self,
        authorization_proof: str,
        *,
        client_host: str | None,
        origin: str | None,
        csrf_header: str | None,
        sec_fetch_site: str | None = None,
        server_port: int | None = None,
    ) -> tuple[Any, str]:
        """Verify that the caller possesses local host control or valid owner authority."""
        if not is_loopback_host(client_host):
            raise LocalSignInDenied("Local authorization requires a loopback client address")
        if csrf_header not in {"1", "true"}:
            raise LocalSignInDenied("Missing required X-Kinaiya-Local-Handoff header")
        if sec_fetch_site is not None and sec_fetch_site.strip().lower() != "same-origin":
            raise LocalSignInDenied("Cross-site request refused")
        permitted = self.permitted_origins_for_port(server_port)
        if not origin or origin not in permitted:
            raise LocalSignInDenied("Browser origin is not permitted")

        storage = self.check_storage_status()
        if storage["status"] != "ready":
            raise StorageUnavailable(storage["detail"])

        if not authorization_proof or not isinstance(authorization_proof, str):
            raise LocalSignInDenied("Missing local authorization proof")

        cleaned = authorization_proof.strip()
        now = float(self.clock())

        # 1. Check if proof matches a challenge in local-recovery-challenge.json
        challenge_path = self.directory / RECOVERY_CHALLENGE_FILE_NAME
        if challenge_path.exists():
            try:
                challenge = _read_private_json(challenge_path, label="Recovery challenge")
                expires_at = float(challenge.get("expires_at", 0))
                if now < expires_at and challenge.get("host_generation") == self.host_generation:
                    stored_code_sha = challenge.get("code_sha256")
                    candidate_code = cleaned
                    if cleaned.startswith("{") and cleaned.endswith("}"):
                        try:
                            parsed_proof = json.loads(cleaned)
                            if isinstance(parsed_proof, dict):
                                candidate_code = str(
                                    parsed_proof.get("code") or parsed_proof.get("auth_code") or ""
                                ).strip()
                        except Exception:
                            pass
                    candidate_code_sha = hashlib.sha256(candidate_code.encode("utf-8")).hexdigest()
                    if stored_code_sha and secrets.compare_digest(
                        stored_code_sha, candidate_code_sha
                    ):
                        # Consume single-use challenge
                        try:
                            challenge_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        tokens = _read_validated_tokens(self.tokens_path)
                        return tokens, "challenge"
            except Exception:
                pass

        # 2. Check if proof matches active owner token (or JSON with "owner" key)
        tokens = _read_validated_tokens(self.tokens_path)
        candidate_owner = cleaned
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                parsed_proof = json.loads(cleaned)
                if isinstance(parsed_proof, dict) and "owner" in parsed_proof:
                    candidate_owner = str(parsed_proof["owner"]).strip()
            except Exception:
                pass
        if secrets.compare_digest(candidate_owner, tokens["owner"]):
            return tokens, "owner_credential"

        # 3. Check if proof is an active scoped session token
        if cleaned.startswith(SESSION_TOKEN_PREFIX):
            try:
                role = self.session_manager.verify_session_token(
                    cleaned,
                    expected_owner_epoch=tokens.owner_epoch,
                    expected_host_generation=self.host_generation,
                )
                if role == "owner":
                    return tokens, "scoped_session"
            except Exception:
                pass

        raise LocalSignInDenied("Invalid local authorization proof")

    def recover_access(
        self,
        *,
        authorization_proof: str,
        client_host: str | None,
        origin: str | None,
        csrf_header: str | None,
        sec_fetch_site: str | None = None,
        server_port: int | None = None,
    ) -> dict[str, Any]:
        """Recover an owner session using verified local host authorization without credential rotation."""
        tokens, proof_kind = self.verify_local_authorization(
            authorization_proof,
            client_host=client_host,
            origin=origin,
            csrf_header=csrf_header,
            sec_fetch_site=sec_fetch_site,
            server_port=server_port,
        )
        with _store_lock(self.directory):
            current_owner_sha = hashlib.sha256(tokens["owner"].encode("utf-8")).hexdigest()
            issued = self.session_manager.issue_session_token(
                host_generation=self.host_generation,
                owner_epoch=tokens.owner_epoch,
                owner_token_sha256=current_owner_sha,
                ttl_seconds=self.session_ttl_seconds,
            )
            return {
                "status": "authenticated",
                "role": "owner",
                "credential_kind": "scoped_owner_session",
                "owner_token": issued["session_token"],
                "session_token": issued["session_token"],
                "host_generation": issued["host_generation"],
                "owner_epoch": issued["owner_epoch"],
                "expires_at": issued["expires_at"],
                "proof_kind": proof_kind,
                "session_url": self.session_url,
                "agent_url": self.agent_url,
                "converse_url": self.converse_url,
                "decisions_url": self.decisions_url,
            }

    def rotate_owner_credential(
        self,
        *,
        authorization_proof: str,
        confirm: bool,
        return_exported_token: bool = False,
        client_host: str | None,
        origin: str | None,
        csrf_header: str | None,
        sec_fetch_site: str | None = None,
        server_port: int | None = None,
    ) -> dict[str, Any]:
        """Atomically rotate the owner credential, invalidate prior sessions and handoffs, and issue new session."""
        if not confirm:
            raise LocalSignInDenied("Owner credential replacement requires explicit confirmation")
        tokens, proof_kind = self.verify_local_authorization(
            authorization_proof,
            client_host=client_host,
            origin=origin,
            csrf_header=csrf_header,
            sec_fetch_site=sec_fetch_site,
            server_port=server_port,
        )
        from services.common.auth import rotate_owner_token

        rotation_result = rotate_owner_token(self.directory)
        with _store_lock(self.directory):
            new_tokens = _read_validated_tokens(self.tokens_path)
            new_owner_sha = hashlib.sha256(new_tokens["owner"].encode("utf-8")).hexdigest()
            self.owner_epoch = new_tokens.owner_epoch
            self.owner_token_sha256 = new_owner_sha

            # Invalidate all prior sessions in local-owner-sessions.json
            sessions_path = self.directory / SESSIONS_FILE_NAME
            if sessions_path.exists():
                _write_private_json(
                    sessions_path,
                    {
                        "schema_version": 1,
                        "host_generation": self.host_generation,
                        "owner_epoch": self.owner_epoch,
                        "sessions": [],
                    },
                )

            # Invalidate any outstanding handoff in local-owner-handoff.json
            handoff_path = self.directory / HANDOFF_FILE_NAME
            if handoff_path.exists():
                try:
                    handoff = _read_private_json(handoff_path, label="Handoff state")
                    handoff["state"] = "expired"
                    handoff["seal_sha256"] = None
                    _write_private_json(handoff_path, handoff)
                except Exception:
                    pass

            # Mint a new session token for the new epoch
            issued = self.session_manager.issue_session_token(
                host_generation=self.host_generation,
                owner_epoch=self.owner_epoch,
                owner_token_sha256=new_owner_sha,
                ttl_seconds=self.session_ttl_seconds,
            )
            response: dict[str, Any] = {
                "status": "rotated",
                "role": "owner",
                "credential_kind": "scoped_owner_session",
                "owner_token": issued["session_token"],
                "session_token": issued["session_token"],
                "host_generation": issued["host_generation"],
                "owner_epoch": self.owner_epoch,
                "rotated_at": rotation_result.get("rotated_at"),
                "expires_at": issued["expires_at"],
                "proof_kind": proof_kind,
                "session_url": self.session_url,
                "agent_url": self.agent_url,
                "converse_url": self.converse_url,
                "decisions_url": self.decisions_url,
            }
            if return_exported_token:
                response["new_owner_token"] = new_tokens["owner"]
            return response
