"""Small local control plane: distinct bearer credentials for each trusted service."""

import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import Header, HTTPException, Request


@dataclass(frozen=True, slots=True, repr=False)
class NativeAuthIdentity:
    """Private native identity; neither a credential nor a public role claim."""

    instance_id: str
    credential_fingerprint: bytes


class Auth:
    def __init__(self, tokens: dict[str, str], allowed_origins: tuple[str, ...] = ()):
        if not tokens or any(len(value) < 24 for value in tokens.values()):
            raise ValueError("Use independently generated credentials of at least 24 characters")
        if len(set(tokens.values())) != len(tokens):
            raise ValueError("Service credentials must be distinct")
        # The caller's mutable configuration must not replace an admitted
        # native identity behind the authentication owner's back.
        self.tokens = dict(tokens)
        self.allowed_origins = allowed_origins
        self._media_snapshot = tuple(self.tokens.items())
        self._media_instance = secrets.token_hex(16)
        self._media_identity = None
        self._media_invalidated = False

    def media_identity(self) -> NativeAuthIdentity:
        """Return this actual Auth incarnation while its credentials are intact.

        Media startup HMACs the private fingerprint before durable storage.
        A detected credential replacement permanently fences this identity;
        registering a new native Auth/app is a separate operation. Ordinary
        authenticate/require behavior remains independent of this media gate.
        """

        def exact(items):
            return type(items) is tuple and all(
                type(role) is str
                and 0 < len(role) <= 128
                and type(token) is str
                and 24 <= len(token) <= 4096
                for role, token in items
            )

        try:
            current = tuple(self.tokens.items()) if type(self.tokens) is dict else None
            # Reject non-builtin values before equality can invoke a replaced
            # credential's comparator, including when identity was cached.
            valid = (
                exact(current) and exact(self._media_snapshot) and current == self._media_snapshot
            )
        except (RuntimeError, TypeError, ValueError):
            valid = False
        if self._media_invalidated or not valid:
            self._media_invalidated = True
            raise ValueError("Native authentication identity is unavailable")
        if self._media_identity is None:
            raw = json.dumps(
                sorted(self._media_snapshot), ensure_ascii=True, separators=(",", ":")
            ).encode("ascii")
            fingerprint = hashlib.sha256(
                b"kinaiya.auth.media-credential-snapshot.v1\0" + raw
            ).digest()
            self._media_identity = NativeAuthIdentity(self._media_instance, fingerprint)
        return self._media_identity

    def authenticate(self, token: str) -> str:
        if not isinstance(token, str):
            raise HTTPException(401, "Invalid bearer credential")
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


def load_or_create_tokens(directory: Path) -> dict[str, str]:
    import json
    import os

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "service-tokens.json"
    if not path.exists():
        tokens = {
            role: secrets.token_urlsafe(36)
            for role in ("owner", "agent", "converse", "decisions", "resources")
        }
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(tokens, stream)
    if path.stat().st_mode & 0o077:
        raise PermissionError("Service credential file must have mode 0600")
    return json.loads(path.read_text())
