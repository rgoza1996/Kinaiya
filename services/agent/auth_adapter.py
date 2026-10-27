"""Auth adapter for the seed Agent HTTP surface.

Uses ``services.common.auth.Auth`` when the lead has provided it; otherwise
falls back to a documented stub. Both expose ``require(role)`` returning a
FastAPI dependency that yields the authenticated actor string, so the lead
can swap the real Auth in without touching routes.
"""

from __future__ import annotations

from typing import Any


class AuthProtocol:
    def require(self, role: str) -> Any:  # pragma: no cover - interface shape
        raise NotImplementedError


class StubAuth:
    """Stand-in until ``services.common.auth.Auth`` lands.

    Requires the ``X-Agent-Actor`` header to equal the required role, so
    direct HTTP calls are owner-only. The lead's Auth will replace this
    with bearer verification (including the decisions service identity).
    """

    def require(self, role: str) -> Any:
        from fastapi import Header, HTTPException

        async def dep(x_agent_actor: str | None = Header(default=None)) -> str:
            if x_agent_actor != role:
                raise HTTPException(status_code=403, detail=f"requires {role} actor")
            return x_agent_actor

        return dep


def default_auth() -> Any:
    """Prefer the lead's Auth, else the documented stub.

    The lead's ``Auth`` needs configured credentials (``tokens``) that only
    the launcher owns, so a bare default cannot construct it; in that case
    fall back to the stub. Pass a configured auth explicitly to use real
    bearer verification.
    """
    try:
        from services.common.auth import Auth as LeadAuth  # type: ignore[import]

        try:
            return LeadAuth()
        except TypeError:
            return StubAuth()
    except ImportError:
        return StubAuth()
