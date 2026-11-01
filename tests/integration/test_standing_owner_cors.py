"""Codex independent no-store regressions through the actual Session CORS stack."""

import httpx
import pytest

from services.common.auth import Auth
from services.session.api import create_app
from services.session.store import Store

OWNER = "synthetic-standing-owner-00000001"
ORIGIN = "https://standing-owner.invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "expected_status"),
    [(ORIGIN, 200), ("https://other.invalid", 400)],
    ids=["allowed-preflight", "refused-preflight"],
)
async def test_standing_cors_preflight_responses_are_no_store(tmp_path, origin, expected_status):
    store = Store(tmp_path / "native.sqlite3")
    app = create_app(store, Auth({"owner": OWNER}, allowed_origins=(ORIGIN,)))
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://session.test"
            ) as client:
                headers = {"Authorization": "Bearer " + OWNER, "Origin": ORIGIN}
                control = await client.get("/v1/standing/responsibilities", headers=headers)
                assert control.status_code == 200
                assert control.headers.get("Cache-Control") == "no-store"
                missing = await client.get("/v1/standing/not-a-route", headers=headers)
                assert missing.status_code == 404
                assert missing.headers.get("Cache-Control") == "no-store"
                response = await client.options(
                    "/v1/standing/responsibilities",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "authorization",
                    },
                )
                assert response.status_code == expected_status
                assert response.headers.get("Cache-Control") == "no-store"
    finally:
        store.close()
