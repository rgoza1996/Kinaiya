"""Small local control plane: distinct bearer credentials for each trusted service."""

import secrets
from pathlib import Path

from fastapi import Header, HTTPException, Request


class Auth:
    def __init__(self, tokens: dict[str, str], allowed_origins: tuple[str, ...] = ()):
        if not tokens or any(len(value) < 24 for value in tokens.values()):
            raise ValueError("Use independently generated credentials of at least 24 characters")
        if len(set(tokens.values())) != len(tokens):
            raise ValueError("Service credentials must be distinct")
        self.tokens = tokens
        self.allowed_origins = allowed_origins

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
