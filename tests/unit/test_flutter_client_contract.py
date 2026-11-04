"""Contract tests for Issue #396: trusted local first-run connection in the Flutter owner UI."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLUTTER_LIB = ROOT / "apps/kinaiya_flutter/lib"
FLUTTER_TEST = ROOT / "apps/kinaiya_flutter/test"


def test_connection_config_supports_loopback_local_signin_and_ram_only_tokens() -> None:
    source = (FLUTTER_LIB / "config/connection.dart").read_text(encoding="utf-8")

    assert "'/__kinaiya/local-signin'" in source
    assert "'X-Kinaiya-Local-Handoff'" in source
    assert "class LocalSignInResult" in source
    assert "Future<LocalSignInResult> exchangeLocalSignIn" in source
    assert "String? validateLoopbackOnly()" in source
    assert "local sign-in requires a loopback endpoint" in source
    # Never persist owner tokens in browser localStorage or persistent storage.
    assert "localStorage" not in source
    assert "SharedPreferences" not in source


def test_app_state_preserves_disclosure_and_distinguishes_agent_only_from_broken_host() -> None:
    source = (FLUTTER_LIB / "state/app_state.dart").read_text(encoding="utf-8")

    assert "enum LocalHostSignInState { idle, connecting, connected, unavailable }" in source
    assert "Future<bool> attemptLocalSignIn(" in source
    assert "bool get isAgentOnlyConnected" in source
    assert "String get hostRoleStatusSummary" in source
    assert "Connected (Agent-only)" in source
    assert "Converse optional / not enabled" in source
    assert "Agent host unreachable" in source
    # Preserves BackendProfile.clean by default and never writes owner tokens to storage.
    assert "_preferredBackendProfile = BackendProfile.clean" in source
    assert "localStorage" not in source


def test_setup_dialog_presents_local_host_card_and_preserves_advanced_manual_controls() -> None:
    source = (FLUTTER_LIB / "ui/setup_dialog.dart").read_text(encoding="utf-8")

    # Local Host Connection card and state keys
    assert "Key('local-host-connection-card')" in source
    assert "Key('local-host-status-connecting')" in source
    assert "'Connecting to local Kinaiya host...'" in source
    assert "Key('local-host-status-connected')" in source
    assert "'Connected to local Kinaiya host'" in source
    assert "Key('local-host-status-unavailable')" in source
    assert "'Local automatic sign-in unavailable'" in source
    assert "Key('local-host-retry-signin')" in source
    assert "'Retry local sign-in'" in source
    assert "Key('setup-toggle-endpoint-fields')" in source
    assert "'Advanced connection'" in source
    assert "_showServiceEndpointFields" in source

    # Existing widget keys and labels remain intact for manual setup and regression safety
    for existing_key in (
        "setup-user-guide",
        "setup-endpoints-toggle",
        "setup-endpoints-summary",
        "setup-existing-token-hint",
        "setup-privacy-guide",
        "setup-validation-error",
        "setup-advanced-toggle",
        "setup-advanced-panel",
        "setup-connect-primary-action",
        "setup-connection-outcome",
        "setup-connection-status",
    ):
        assert existing_key in source, f"Missing existing key: {existing_key}"

    for label in (
        "Session service URL",
        "Agent service URL",
        "Converse service URL",
        "Decisions service URL (optional)",
        "Owner access token",
        "Load local access token",
        "Inference disclosure",
    ):
        assert label in source, f"Missing control label: {label}"

    assert "localStorage" not in source


def test_widget_test_covers_local_autoconnect_unavailable_recovery_and_agent_only_states() -> None:
    source = (FLUTTER_TEST / "widget_test.dart").read_text(encoding="utf-8")

    assert "trusted local first-run auto-connects" in source
    assert "unavailable local sign-in shows truthful status" in source
    assert "local sign-in preserves inference disclosure" in source
    assert "local-host-connection-card" in source
    assert "local-host-status-connected" in source
    assert "local-host-status-unavailable" in source
    assert "local-host-retry-signin" in source
    assert "setup-toggle-endpoint-fields" in source
