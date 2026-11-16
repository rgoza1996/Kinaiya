"""#623: Converse owns capture preferences without starting work or audio."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.capture_settings import CaptureModeSettings
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store


@pytest.mark.parametrize(
    ("method", "path", "token", "status"),
    [
        ("GET", "/capture-mode", "o" * 32, 503),
        ("GET", "/capture-mode", None, 401),
        ("GET", "/capture-mode", "a" * 32, 403),
        ("POST", "/capture-mode-changes/lookup", "o" * 32, 503),
    ],
)
async def test_capture_controls_are_authenticated_and_honestly_unavailable(
    tmp_path, method, path, token, status
):
    store = Store(tmp_path / "journal.sqlite")
    provider = ScriptedProvider([])
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    app = create_app(runtime, Auth({"owner": "o" * 32, "agent": "a" * 32}))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.request(
                method,
                "/v1/converse/options" + path,
                headers={} if token is None else {"Authorization": "Bearer " + token},
            )
        assert response.status_code == status
        if status == 503:
            assert response.headers["cache-control"] == "no-store"
        assert store.read() == [] and provider.calls == []
        assert runtime.enabled is True
    finally:
        await runtime.close()
        store.close()


def original(snapshot, *, key, mode):
    return {
        "contract": "converse_capture.v1",
        "backend_id": snapshot["backend_id"],
        "authority_generation": snapshot["authority_generation"],
        "authority_epoch": snapshot["authority_epoch"],
        "expected_revision": snapshot["revision"],
        "expected_default_mode": snapshot["default_mode"],
        "state_token": snapshot["state_token"],
        "idempotency_key": key,
        "selection": {"source": "saved", "mode": mode},
    }


@pytest.fixture
async def controlled(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    provider = ScriptedProvider([])
    runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
    native = CaptureModeSettings(
        lambda: "rolling_windows",
        path=tmp_path / "private/capture.sqlite",
        workspace=tmp_path / "workspace",
    )
    runtime.capture_settings = native
    runtime.enabled = False
    app = create_app(runtime, Auth({"owner": "o" * 32, "agent": "a" * 32}))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + "o" * 32},
        ) as client:
            yield client, native, runtime
        assert store.read() == [] and provider.calls == []
        assert runtime.enabled is False
    finally:
        await runtime.close()
        store.close()
        assert native.closed and native._fd is None


@pytest.mark.parametrize("mode", ["adaptive_live", "rolling_windows", "legacy_silence"])
async def test_actual_sqlite_save_replay_lookup_do_not_wake_or_run_a_provider(controlled, mode):
    client, native, runtime = controlled
    snapshot = (await client.get("/v1/converse/options/capture-mode")).json()
    body = original(snapshot, key="public-capture-http", mode=mode)
    saved = await client.post("/v1/converse/options/capture-mode", json=body)
    assert saved.status_code == 200 and saved.headers["cache-control"] == "no-store"
    assert saved.json()["selected_mode_observed"] == mode
    replay = await client.post("/v1/converse/options/capture-mode", json=body)
    assert replay.status_code == 200 and replay.json() == saved.json()
    lookup = await client.post("/v1/converse/options/capture-mode-changes/lookup", json=body)
    assert lookup.status_code == 200 and lookup.json()["receipt"] == saved.json()
    inspected = await client.get("/v1/converse/options/capture-mode")
    assert inspected.json()["selected_mode"] == mode
    assert inspected.json()["capture_state"] == "not_observed"
    assert inspected.json()["adaptive_live_enabled"] is False
    assert native.db.execute("SELECT COUNT(*) FROM capture_mode_operations").fetchone() == (1,)
    assert not runtime._active and not runtime._stt_operations and not runtime._speech


async def test_concurrent_http_originals_compare_and_set_one_native_revision(controlled):
    client, native, _ = controlled
    snapshot = (await client.get("/v1/converse/options/capture-mode")).json()
    bodies = [
        original(snapshot, key="first-http", mode="rolling_windows"),
        original(snapshot, key="second-http", mode="legacy_silence"),
    ]
    responses = await asyncio.gather(
        *(client.post("/v1/converse/options/capture-mode", json=body) for body in bodies)
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert native.snapshot()["revision"] == 1
    assert native.db.execute("SELECT COUNT(*) FROM capture_mode_operations").fetchone() == (1,)


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"x" * 4097, 413),
        (b'{"contract":"converse_capture.v1","contract":"other"}', 422),
        (b'{"expected_revision":NaN}', 422),
        (b'{"expected_revision":0.0}', 422),
        (b"\xff", 422),
    ],
)
async def test_untrusted_control_body_is_bounded_before_native_write(controlled, raw, status):
    client, native, _ = controlled
    response = await client.post("/v1/converse/options/capture-mode", content=raw)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert native.snapshot()["revision"] == 0


async def test_capture_database_retires_even_if_required_dependency_close_fails(controlled):
    _, native, runtime = controlled

    async def unavailable():
        raise RuntimeError("PUBLIC-DEPENDENCY-CLOSE-FAULT")

    runtime.decisions_dependency = SimpleNamespace(close=unavailable)
    try:
        with pytest.raises(RuntimeError, match="PUBLIC-DEPENDENCY-CLOSE-FAULT"):
            await runtime.close()
        assert native.closed and native._fd is None
    finally:
        runtime.decisions_dependency = None
