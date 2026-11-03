"""Small local control plane: distinct bearer credentials for each trusted service."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import socket
import stat
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException, Request

DEFAULT_ROLES: tuple[str, ...] = ("owner", "agent", "converse", "decisions", "resources")
METADATA_KEYS: frozenset[str] = frozenset({"owner_epoch", "rotated_at"})
MAX_TOKEN_FILE_BYTES = 64 * 1024


class CredentialRotationError(RuntimeError, ValueError):
    """Actionable failure during owner credential rotation or recovery; never includes secrets."""


class CredentialRotationConflict(CredentialRotationError):
    """Concurrent modification or CAS mismatch refused owner credential rotation."""


class TokenStoreDict(dict[str, str]):
    """Role-to-token mapping carrying store provenance and owner epoch metadata."""

    owner_epoch: int
    rotated_at: str | None
    path: Path | None
    sha256: str | None
    _stat_sig: tuple[int, int, int, int] | None

    def __init__(
        self,
        tokens: dict[str, str],
        *,
        owner_epoch: int = 1,
        rotated_at: str | None = None,
        path: Path | None = None,
        sha256: str | None = None,
        stat_sig: tuple[int, int, int, int] | None = None,
    ) -> None:
        super().__init__(tokens)
        self.owner_epoch = owner_epoch
        self.rotated_at = rotated_at
        self.path = path
        self.sha256 = sha256
        self._stat_sig = stat_sig


def _validate_role_tokens(tokens: dict[str, Any]) -> dict[str, str]:
    if not isinstance(tokens, dict):
        raise ValueError("Service credentials must be a JSON object")
    role_tokens: dict[str, str] = {}
    for key, value in tokens.items():
        if key in METADATA_KEYS:
            continue
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Service credential roles must be non-empty strings")
        if (
            not isinstance(value, str)
            or len(value) < 24
            or any(ch.isspace() or ord(ch) < 32 for ch in value)
        ):
            raise ValueError("Use independently generated credentials of at least 24 characters")
        role_tokens[key] = value
    if not role_tokens:
        raise ValueError("Use independently generated credentials of at least 24 characters")
    if len(set(role_tokens.values())) != len(role_tokens):
        raise ValueError("Service credentials must be distinct")
    return role_tokens


def _extract_metadata(raw: dict[str, Any]) -> tuple[int, str | None]:
    epoch = raw.get("owner_epoch", 1)
    if type(epoch) is not int or epoch < 1:
        raise ValueError("owner_epoch must be a positive integer")
    rotated_at = raw.get("rotated_at", None)
    if rotated_at is not None and (
        not isinstance(rotated_at, str)
        or not rotated_at.strip()
        or any(ord(ch) < 32 for ch in rotated_at)
    ):
        raise ValueError("rotated_at must be an ISO-8601 timestamp string or null")
    return epoch, rotated_at


def _resolve_store_paths(path: Path) -> tuple[Path, Path]:
    path = Path(path)
    if os.path.islink(path):
        raise PermissionError("Service credential path cannot be a symlink")
    if path.name == "service-tokens.json" or path.suffix == ".json":
        directory = path.parent
        tokens_path = path
    else:
        directory = path
        tokens_path = directory / "service-tokens.json"
    if os.path.islink(directory):
        raise PermissionError("Service credential directory cannot be a symlink")
    if os.path.islink(tokens_path):
        raise PermissionError("Service credential file cannot be a symlink")
    return directory, tokens_path


def _check_fd_security(fd: int, *, label: str = "Service credential file") -> os.stat_result:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise PermissionError(f"{label} must be a regular file")
    if st.st_mode & 0o077 or stat.S_IMODE(st.st_mode) != 0o600:
        raise PermissionError(f"{label} must have mode 0600")
    if st.st_nlink != 1:
        raise PermissionError(f"{label} must not have hard links")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise PermissionError(f"{label} must be owned by the current user")
    if st.st_size <= 0 or st.st_size > MAX_TOKEN_FILE_BYTES:
        raise ValueError(f"{label} size is out of bounds")
    return st


def _read_validated_tokens(tokens_path: Path) -> TokenStoreDict:
    if os.path.islink(tokens_path) or os.path.islink(tokens_path.parent):
        raise PermissionError("Service credential file cannot be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tokens_path, flags)
    except OSError as exc:
        if exc.errno == getattr(errno, "ELOOP", 40):
            raise PermissionError("Service credential file cannot be a symlink") from None
        raise
    try:
        st = _check_fd_security(fd)
        raw_bytes = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw_bytes) > MAX_TOKEN_FILE_BYTES:
        raise ValueError("Service credential file exceeds maximum size")
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed service credential JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Service credential file must contain a JSON object")
    role_tokens = _validate_role_tokens(parsed)
    if "owner" not in role_tokens:
        raise ValueError("Service credential file must define an owner token")
    owner_epoch, rotated_at = _extract_metadata(parsed)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    stat_sig = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
    return TokenStoreDict(
        role_tokens,
        owner_epoch=owner_epoch,
        rotated_at=rotated_at,
        path=tokens_path,
        sha256=digest,
        stat_sig=stat_sig,
    )


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _write_tokens_atomic(
    tokens_path: Path,
    payload: dict[str, Any],
    *,
    before_replace: Callable[[], None] | None = None,
    expected_before_replace: tuple[int, int, str] | None = None,
) -> None:
    directory = tokens_path.parent
    if os.path.islink(directory) or os.path.islink(tokens_path):
        raise PermissionError("Service credential path cannot be a symlink")
    temp_path = directory / f".{tokens_path.name}.tmp.{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp_path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        written = 0
        while written < len(encoded):
            n = os.write(fd, encoded[written:])
            if n <= 0:
                raise OSError("Short write while staging service credentials")
            written += n
        os.fsync(fd)
        _check_fd_security(fd, label="Staged service credential file")
    except BaseException:
        os.close(fd)
        temp_path.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        if before_replace is not None:
            before_replace()
        if expected_before_replace is not None:
            exp_dev, exp_ino, exp_sha = expected_before_replace
            try:
                current = _read_validated_tokens(tokens_path)
            except FileNotFoundError:
                raise CredentialRotationConflict(
                    "Service credential file was removed concurrently before commit"
                ) from None
            assert current._stat_sig is not None
            cur_dev, cur_ino, _, _ = current._stat_sig
            if (cur_dev, cur_ino, current.sha256) != (exp_dev, exp_ino, exp_sha):
                raise CredentialRotationConflict(
                    "Concurrent modification of service-tokens.json detected before commit"
                )
        os.replace(temp_path, tokens_path)
        _fsync_directory(directory)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


@contextmanager
def _store_lock(directory: Path):
    if os.path.islink(directory):
        raise PermissionError("Service credential directory cannot be a symlink")
    lock_path = directory / ".service-tokens.lock"
    if os.path.islink(lock_path):
        raise PermissionError("Service credential lock cannot be a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno == getattr(errno, "ELOOP", 40):
            raise PermissionError("Service credential lock cannot be a symlink") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PermissionError("Service credential lock must be a regular file")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise PermissionError("Service credential lock must be owned by current user")
        if st.st_mode & 0o077:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(lock_path, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CredentialRotationConflict(
                "Another credential operation holds the lock; retry after it finishes"
            ) from None
        yield
    finally:
        os.close(fd)


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class ServiceTokenStore:
    """Atomic, 0600-enforced storage and rotation for service-tokens.json."""

    def __init__(self, path: Path) -> None:
        self.directory, self.path = _resolve_store_paths(path)

    def cleanup_interrupted_staging(self) -> int:
        if not self.directory.exists() or os.path.islink(self.directory):
            return 0
        removed = 0
        prefix = f".{self.path.name}.tmp."
        for item in list(self.directory.iterdir()):
            if item.name.startswith(prefix):
                if os.path.islink(item) or item.is_file():
                    item.unlink(missing_ok=True)
                    removed += 1
        return removed

    def load(self) -> TokenStoreDict:
        return _read_validated_tokens(self.path)

    def load_or_create(self) -> TokenStoreDict:
        if os.path.islink(self.directory):
            raise PermissionError("Service credential directory cannot be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _store_lock(self.directory):
            self.cleanup_interrupted_staging()
            if os.path.islink(self.path):
                raise PermissionError("Service credential file cannot be a symlink")
            if not self.path.exists():
                tokens: dict[str, Any] = {role: secrets.token_urlsafe(36) for role in DEFAULT_ROLES}
                tokens["owner_epoch"] = 1
                tokens["rotated_at"] = None
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.path, flags, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(tokens, stream, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                _fsync_directory(self.directory)
            return _read_validated_tokens(self.path)

    def rotate_owner(
        self,
        *,
        expected_owner_epoch: int | None = None,
        expected_sha256: str | None = None,
        active_ports: Iterable[int] = (),
        _before_replace: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if expected_owner_epoch is not None and (
            type(expected_owner_epoch) is not int or expected_owner_epoch < 1
        ):
            raise ValueError("expected_owner_epoch must be a positive integer")
        if os.path.islink(self.directory) or os.path.islink(self.path):
            raise PermissionError("Service credential path cannot be a symlink")
        if not self.directory.exists() or not os.path.lexists(self.path):
            raise CredentialRotationError(
                f"Missing service credential file: {self.path}. Run kinaiya init first."
            )
        busy_ports = [int(p) for p in active_ports if _port_listening(int(p))]
        if busy_ports:
            raise CredentialRotationError(
                f"Active services are listening on port(s) {', '.join(map(str, busy_ports))}; "
                "stop running services before rotating when hot-reload is not active."
            )
        with _store_lock(self.directory):
            self.cleanup_interrupted_staging()
            current = _read_validated_tokens(self.path)
            if expected_owner_epoch is not None and current.owner_epoch != expected_owner_epoch:
                raise CredentialRotationConflict(
                    f"Owner epoch mismatch: expected {expected_owner_epoch}, found {current.owner_epoch}"
                )
            if expected_sha256 is not None and current.sha256 != expected_sha256:
                raise CredentialRotationConflict(
                    "Concurrent credential modification detected (SHA-256 mismatch)"
                )
            existing_values = set(current.values())
            new_owner = secrets.token_urlsafe(36)
            while new_owner in existing_values or len(new_owner) < 32:
                new_owner = secrets.token_urlsafe(36)

            new_epoch = current.owner_epoch + 1
            rotated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            updated_payload: dict[str, Any] = {
                role: (new_owner if role == "owner" else token) for role, token in current.items()
            }
            updated_payload["owner_epoch"] = new_epoch
            updated_payload["rotated_at"] = rotated_at

            assert current._stat_sig is not None
            assert current.sha256 is not None
            exp_dev, exp_ino, _, _ = current._stat_sig
            _write_tokens_atomic(
                self.path,
                updated_payload,
                before_replace=_before_replace,
                expected_before_replace=(exp_dev, exp_ino, current.sha256),
            )
            verified = _read_validated_tokens(self.path)
            if verified.owner_epoch != new_epoch or not secrets.compare_digest(
                verified["owner"], new_owner
            ):
                raise CredentialRotationError("Post-rotation verification failed")

            owner_login_path = self.directory / "owner-login.txt"
            owner_login_invalidated = False
            if os.path.lexists(owner_login_path):
                owner_login_path.unlink(missing_ok=True)
                owner_login_invalidated = True

            return {
                "status": "rotated",
                "owner_epoch": new_epoch,
                "rotated_at": rotated_at,
                "tokens_path": str(self.path.resolve()),
                "owner_login_invalidated": owner_login_invalidated,
                "reconnect_hint": f"kinaiya --data-dir {self.directory} owner-login",
            }


class Auth:
    def __init__(
        self,
        tokens: dict[str, Any] | Path | ServiceTokenStore,
        allowed_origins: tuple[str, ...] = (),
        *,
        token_store_path: Path | None = None,
    ):
        if isinstance(tokens, ServiceTokenStore):
            loaded = tokens.load()
            token_store_path = token_store_path or loaded.path
            tokens = loaded
        elif isinstance(tokens, Path):
            loaded = ServiceTokenStore(tokens).load()
            token_store_path = token_store_path or loaded.path
            tokens = loaded

        if not isinstance(tokens, dict):
            raise ValueError("Use independently generated credentials of at least 24 characters")
        role_tokens = _validate_role_tokens(tokens)
        epoch, rotated_at = _extract_metadata(tokens)
        if isinstance(tokens, TokenStoreDict):
            epoch = tokens.owner_epoch
            rotated_at = tokens.rotated_at
            if token_store_path is None:
                token_store_path = tokens.path
            self._stat_sig = tokens._stat_sig
            self._sha256 = tokens.sha256
        else:
            self._stat_sig = None
            self._sha256 = None

        self.tokens: dict[str, str] = dict(role_tokens)
        self.owner_epoch: int = epoch
        self.rotated_at: str | None = rotated_at
        self.allowed_origins = tuple(allowed_origins)
        self.token_store_path: Path | None = (
            Path(token_store_path) if token_store_path is not None else None
        )

    def _refresh_from_store(self) -> None:
        if self.token_store_path is None:
            return
        try:
            loaded = _read_validated_tokens(self.token_store_path)
        except (OSError, PermissionError, ValueError) as exc:
            raise HTTPException(401, "Invalid bearer credential") from exc
        if self._sha256 is not None and loaded.sha256 == self._sha256:
            return
        self.tokens = dict(loaded)
        self.owner_epoch = loaded.owner_epoch
        self.rotated_at = loaded.rotated_at
        self._stat_sig = loaded._stat_sig
        self._sha256 = loaded.sha256

    def authenticate(self, token: str) -> str:
        if not isinstance(token, str) or not token:
            raise HTTPException(401, "Invalid bearer credential")
        if self.token_store_path is not None:
            self._refresh_from_store()
        for role, expected in self.tokens.items():
            if secrets.compare_digest(token, expected):
                return role
        raise HTTPException(401, "Invalid bearer credential")

    def require(self, *roles: str):
        async def dependency(request: Request, authorization: str = Header(default="")):
            origin = request.headers.get("origin")
            if origin and origin not in self.allowed_origins:
                raise HTTPException(403, "Browser origin is not permitted")
            if not authorization.startswith("Bearer "):
                raise HTTPException(401, "Bearer credential required")
            role = self.authenticate(authorization[7:])
            if roles and role not in roles:
                raise HTTPException(403, "Credential cannot perform this operation")
            return role

        return dependency


Authenticator = Auth


def load_or_create_tokens(directory: Path) -> TokenStoreDict:
    return ServiceTokenStore(directory).load_or_create()


def rotate_owner_token(
    path: Path,
    *,
    expected_owner_epoch: int | None = None,
    expected_sha256: str | None = None,
    active_ports: Iterable[int] = (),
    _before_replace: Callable[[], None] | None = None,
) -> dict[str, Any]:
    return ServiceTokenStore(path).rotate_owner(
        expected_owner_epoch=expected_owner_epoch,
        expected_sha256=expected_sha256,
        active_ports=active_ports,
        _before_replace=_before_replace,
    )
