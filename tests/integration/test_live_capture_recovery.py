"""#685: exact native metadata recovery has no registration or stop authority."""

import httpx
import pytest

from packages.contracts.client import JournalClient
from packages.contracts.live_captures import LiveCaptureRecoveryQuery
from services.session.live_captures import LiveCaptureStore
from services.session.store import LocalJournal
from tests.integration.test_live_capture_permission import registration
from tests.integration.test_live_capture_permission_http import BASE
from tests.integration.test_live_capture_permission_http import wire as wire
from tests.integration.test_live_window_publication import env as env


def recovery_query(request):
    return LiveCaptureRecoveryQuery.model_validate(
        request.model_dump(exclude={"backend"}, mode="json"), strict=True
    )


@pytest.mark.parametrize("role", ["converse", "decisions", "owner", "agent", "resources", None])
async def test_recovery_roles_and_absent_original_are_passive(env, wire, role):
    auth, transport = wire
    query = recovery_query(registration(env))
    headers = {} if role is None else {"Authorization": "Bearer " + auth.tokens[role]}
    before = env.store.db.total_changes
    async with httpx.AsyncClient(
        transport=transport, base_url="http://session.local", headers=headers
    ) as client:
        result = await client.post(BASE + "/recover", json=query.model_dump(mode="json"))
    local = LocalJournal(env.store, role)
    if role in {"converse", "decisions"}:
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json()["capture"] is None
        assert (await local.recover_live_capture(query)).capture is None
    else:
        assert result.status_code == (401 if role is None else 403)
        with pytest.raises(PermissionError):
            await local.recover_live_capture(query)
    assert env.store.db.total_changes == before
    assert env.store.db.execute("SELECT count(*) FROM live_captures").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change", [{}, {"session_id": "foreign"}, {"thread_id": "foreign"}, {"epoch": 2}]
)
async def test_actual_client_recovers_exact_original_across_controller_replacement(
    env, wire, change
):
    auth, transport = wire
    original = registration(env)
    receipt = env.store.live_captures.register(original)
    replacement = LiveCaptureStore(env.store)
    assert replacement.activate()
    env.store.live_captures = replacement
    before = env.store.db.total_changes
    query = recovery_query(original).model_copy(update=change)
    client = JournalClient("http://session.local", auth.tokens["converse"], transport=transport)
    try:
        if change:
            with pytest.raises(httpx.HTTPStatusError):
                await client.recover_live_capture(query)
        else:
            recovered = await client.recover_live_capture(query)
            assert recovered.original == query and recovered.capture.original == original
            assert recovered.capture.receipt == receipt.receipt
            assert recovered.capture.backend.controller_id != original.backend.controller_id
            assert recovered.capture.fence is None and not recovered.capture.capture_open
    finally:
        await client.close()
    assert env.store.db.total_changes == before


@pytest.mark.parametrize("raw", [b'{"epoch":1,"epoch":2}', b'{"epoch":NaN}', b" " * 4097])
async def test_recovery_keeps_bounded_strict_metadata_body(env, wire, raw):
    auth, transport = wire
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://session.local",
        headers={"Authorization": "Bearer " + auth.tokens["converse"]},
    ) as client:
        result = await client.post(
            BASE + "/recover", content=raw, headers={"content-type": "application/json"}
        )
    assert result.status_code == (413 if len(raw) > 4096 else 422)
