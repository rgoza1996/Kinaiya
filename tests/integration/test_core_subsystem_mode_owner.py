"""#566/#567: offline CLI and authenticated Core share one saved-mode source."""

import importlib
import json

import httpx
import pytest

from services.common.auth import Auth
from services.distribution import installation
from services.session.api import create_app
from services.session.store import Store


def implementation():
    try:
        return importlib.import_module("services.settings.subsystems")
    except ModuleNotFoundError as error:
        if error.name != "services.settings.subsystems":
            raise
        pytest.fail("Core saved-mode authority is not implemented")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(layout, endpoint="http://127.0.0.1:1234", model="synthetic-agent")
    return layout


def test_mode_edit_preserves_original_host_config_bytes_and_role_settings(layout):
    before = layout.config.read_bytes()
    original = json.loads(before)
    tokens = (layout.data / "service-tokens.json").read_bytes()
    receipt = installation.reconfigure_section(
        layout,
        "services",
        agent=False,
        converse=False,
        decisions=True,
    )
    assert layout.config.read_bytes() == before
    mod = implementation()
    topology = mod.resolve_topology(original, layout.data, layout.workspace)
    assert (topology.agent, topology.converse, topology.decisions) == (False, False, True)
    assert receipt["activation"] == "restart_required" and receipt["ready"] is False
    assert (layout.data / "service-tokens.json").read_bytes() == tokens


async def test_core_route_exists_even_when_capability_is_unavailable(tmp_path):
    store = Store(tmp_path / "journal.sqlite")
    app = create_app(store, Auth({"owner": "o" * 32, "agent": "a" * 32}))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/v1/core/options/subsystems", headers={"Authorization": "Bearer " + "o" * 32}
            )
            assert response.status_code == 503
            assert response.headers["cache-control"] == "no-store"
            unauthenticated = await client.get("/v1/core/options/subsystems")
            assert unauthenticated.status_code == 401
            forbidden = await client.get(
                "/v1/core/options/subsystems", headers={"Authorization": "Bearer " + "a" * 32}
            )
            assert forbidden.status_code == 403
    finally:
        store.close()


async def test_save_lookup_auth_and_no_activation_through_actual_session_api(layout, tmp_path):
    mod = implementation()
    native = mod.CoreSubsystemSettings(
        lambda: "agent",
        path=layout.data / "core-subsystems.sqlite",
        workspace=layout.workspace,
        startup_mode="agent",
    )
    store = Store(tmp_path / "journal.sqlite")
    app = create_app(store, Auth({"owner": "o" * 32, "agent": "a" * 32}), subsystem_settings=native)
    headers = {"Authorization": "Bearer " + "o" * 32}
    before = layout.config.read_bytes()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            metadata = await client.get("/v1/core/options/subsystems", headers=headers)
            assert metadata.status_code == 200 and metadata.headers["cache-control"] == "no-store"
            snapshot = metadata.json()
            body = {
                "contract": "core_subsystems.v1",
                "backend_id": snapshot["backend_id"],
                "authority_epoch": snapshot["authority_epoch"],
                "authority_generation": snapshot["authority_generation"],
                "expected_revision": snapshot["revision"],
                "state_token": snapshot["state_token"],
                "expected_host_mode": snapshot["host_mode"],
                "idempotency_key": "public-http-mode",
                "selection": {"source": "saved", "mode": "converse-decisions"},
            }
            response = await client.post("/v1/core/options/subsystems", json=body, headers=headers)
            assert response.status_code == 200
            receipt = response.json()
            assert (
                receipt["status"] == "saved"
                and receipt["selected_mode_observed"] == "converse-decisions"
            )
            lookup = await client.post(
                "/v1/core/options/subsystem-changes/lookup", json=body, headers=headers
            )
            assert lookup.status_code == 200 and lookup.json()["receipt"] == receipt
            assert (
                await client.post("/v1/core/options/subsystems", json=body, headers=headers)
            ).json() == receipt
            after = (await client.get("/v1/core/options/subsystems", headers=headers)).json()
            assert (
                after["selected_mode"] == "converse-decisions" and after["startup_mode"] == "agent"
            )
            assert after["running_state"] == "not_observed"
            assert layout.config.read_bytes() == before
            bad = {
                **body,
                "idempotency_key": "malformed",
                "secret_fixture": "never-reflect-private-fixture",
            }
            invalid = await client.post("/v1/core/options/subsystems", json=bad, headers=headers)
            assert (
                invalid.status_code == 422 and "never-reflect-private-fixture" not in invalid.text
            )
            oversized = await client.post(
                "/v1/core/options/subsystems", content=b"x" * 4097, headers=headers
            )
            assert oversized.status_code == 413
    finally:
        native.close()
        store.close()


def test_offline_lookup_uses_the_exact_original_cli_request_without_new_epoch(layout, monkeypatch):
    from services import launcher
    from services.distribution import cli

    saved = installation.reconfigure_section(
        layout, "services", agent=False, converse=False, decisions=False
    )
    before = (layout.data / "core-subsystems.sqlite").read_bytes()
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)
    args = launcher.build_parser().parse_args(
        [
            "setup",
            "services",
            "--lookup-service-change",
            saved["original_request_key"],
        ]
    )
    observed = cli.dispatch(args)
    assert observed["outcome"] == "saved" and observed["receipt"] == saved["core_receipt"]
    assert (layout.data / "core-subsystems.sqlite").read_bytes() == before


def test_full_setup_cannot_silently_override_a_different_core_selection(layout):
    mod = implementation()
    installation.reconfigure_section(
        layout, "services", agent=False, converse=False, decisions=False
    )
    before = layout.config.read_bytes()
    tokens = (layout.data / "service-tokens.json").read_bytes()
    with pytest.raises(installation.InstallError, match="Core.*selection"):
        installation.setup(
            layout,
            endpoint="http://127.0.0.1:1234",
            model="synthetic-new-model",
            allow_existing=True,
        )
    assert layout.config.read_bytes() == before
    assert (layout.data / "service-tokens.json").read_bytes() == tokens
    assert mod.read_selection(layout.data, layout.workspace).mode == "core"


async def test_session_lifespan_retires_the_original_subsystem_settings_owner(layout, tmp_path):
    mod = implementation()
    native = mod.CoreSubsystemSettings(
        lambda: "agent",
        path=layout.data / "core-subsystems.sqlite",
        workspace=layout.workspace,
    )
    store = Store(tmp_path / "journal.sqlite")
    app = create_app(store, Auth({"owner": "o" * 32}), subsystem_settings=native)
    try:
        async with app.router.lifespan_context(app):
            assert not native.closed
        assert native.closed and native._fd is None
        assert app.state.core_subsystems_shutdown_unconfirmed is False
    finally:
        native.close()
        store.close()
