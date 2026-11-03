from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONNECTION_DART = ROOT / "apps" / "kinaiya_flutter" / "lib" / "config" / "connection.dart"
SETUP_DIALOG_DART = ROOT / "apps" / "kinaiya_flutter" / "lib" / "ui" / "setup_dialog.dart"


def test_owner_token_guidance_points_to_owner_login_without_exposing_service_tokens():
    connection_src = CONNECTION_DART.read_text(encoding="utf-8")
    setup_src = SETUP_DIALOG_DART.read_text(encoding="utf-8")

    for src in (connection_src, setup_src):
        assert "kinaiya --data-dir <dir> owner-login" in src
        assert "owner-login.txt" in src
        assert "Load local access token" in src
        assert "KINAIYA_OWNER_TOKEN" not in src
        assert "service-tokens.json" not in src


def test_setup_validation_modal_blocks_background_interaction():
    setup_src = SETUP_DIALOG_DART.read_text(encoding="utf-8")

    assert "ExcludeSemantics(" in setup_src
    assert "ExcludeFocus(" in setup_src
    assert "IgnorePointer(" in setup_src
    assert "ModalBarrier(" in setup_src
    assert "_HitTestOnlyTarget" not in setup_src
    assert "_RenderHitTestOnlyTarget" not in setup_src
