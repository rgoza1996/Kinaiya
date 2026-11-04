"""Contract tests between Flutter OptionsProviderEditor and CustomProvidersEnvStore (#352, #369, #370)."""

from __future__ import annotations

from pathlib import Path

from services.settings.custom_providers_env import CustomProvidersEnvStore

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EDITOR_DART = (
    _REPO_ROOT / "apps" / "kinaiya_flutter" / "lib" / "ui" / "options_provider_editor.dart"
)
_SETTINGS_DART = _REPO_ROOT / "apps" / "kinaiya_flutter" / "lib" / "api" / "provider_settings.dart"


def test_confirm_and_remove_custom_profile_rechecks_owner_and_revision_after_dialog() -> None:
    source = _EDITOR_DART.read_text(encoding="utf-8")
    assert "Future<void> _confirmAndRemoveCustomProfile(" in source

    fn_start = source.index("Future<void> _confirmAndRemoveCustomProfile(")
    dialog_idx = source.index("await showDialog<bool>(", fn_start)
    post_idx = source.index("'action': 'remove_custom_provider'", dialog_idx)

    before_dialog = source[fn_start:dialog_idx]
    between_dialog_and_post = source[dialog_idx:post_idx]

    # Captures owner, active generation fence, and profile/catalog revision BEFORE showDialog
    assert "final api = widget.api;" in before_dialog
    assert "final generation = widget.active;" in before_dialog
    assert "final capturedRevision = _customCatalog.revision;" in before_dialog
    assert "final capturedStateSha256 = _customCatalog.stateSha256;" in before_dialog
    assert "final capturedProfileId = profile.profileId;" in before_dialog

    # Immediately after showDialog and BEFORE sending destructive POST, rechecks mounted,
    # _current(api, generation), and that the captured profile/revision was not replaced
    assert "if (approve != true || !mounted) return;" in between_dialog_and_post
    assert (
        "if (!_current(api, generation) || !_enabled || _customBusy) return;"
        in between_dialog_and_post
    )
    assert "_customCatalog.revision != capturedRevision" in between_dialog_and_post
    assert "_customCatalog.stateSha256 != capturedStateSha256" in between_dialog_and_post
    assert "currentProfile == null" in between_dialog_and_post


def test_monotonic_refresh_seq_guards_custom_provider_get_and_mutations() -> None:
    source = _EDITOR_DART.read_text(encoding="utf-8")
    assert "int _refreshSeq = 0;" in source
    assert "final api = widget.api, seq = ++_refreshSeq;" in source
    assert "if (_current(api, active) && seq == _refreshSeq)" in source
    assert "'expected_revision': expectedRevision," in source
    assert "'client_operation_id': const Uuid().v4()," in source

    # Every mutation/discovery increments _refreshSeq (directly or via _beginCustom/_sendCustom)
    # so older in-flight GET refreshes cannot clobber newer state
    for method_sig in (
        "Future<void> _saveCustomApi()",
        "Future<void> _setGlobalReasoningEffort(",
        "Future<void> _discoverCustomModels(",
        "Future<void> _toggleCustomEnabled(",
        "Future<void> _bindCustomRole(",
        "Future<void> _confirmAndRemoveCustomProfile(",
    ):
        idx = source.index(method_sig)
        window = source[idx : idx + 3500]
        assert "final seq = ++_refreshSeq;" in window or "_beginCustom(" in window


def test_editor_wire_payloads_and_role_status_match_backend_contract(tmp_path: Path) -> None:
    editor_source = _EDITOR_DART.read_text(encoding="utf-8")
    settings_source = _SETTINGS_DART.read_text(encoding="utf-8")

    # Ensure update_custom_provider never coerces null maxOutputTokens to '' string
    assert "'max_output_tokens': maxOutputTokens ?? ''" not in editor_source
    assert "'max_output_tokens': maxOutputTokens," in editor_source

    # Role bindings UI must never claim custom .env role bindings are live/active
    assert "Active custom role bindings:" not in editor_source
    assert "Proposed custom role selections" in editor_source
    assert "proposed_unavailable" in editor_source
    assert "unavailable (needs reselection)" in editor_source

    # Provider settings model parses status, active, unavailable_reason, revision, and state_sha256
    assert "this.status = 'proposed_unavailable'" in settings_source
    assert "this.active = false" in settings_source
    assert "m['unavailable_reason']" in settings_source
    assert "m['state_sha256']" in settings_source

    # Backend store accepts the exact wire payloads emitted by OptionsProviderEditor
    store = CustomProvidersEnvStore(tmp_path / "custom_providers.env")
    snap = store.apply_action(
        {
            "action": "add_custom_provider",
            "name": "Editor Contract Provider",
            "base_url": "https://api.example.com/v1",
            "chat_completions_path": "/chat/completions",
            "models_path": "/models",
            "custom_endpoints": "",
            "max_output_tokens": None,
            "api_key": "sk-contract",
            "enabled": True,
            "selected_model": "org/contract-model",
            "allow_remote_http": False,
        }
    )
    assert snap["revision"] == 1

    snap2 = store.apply_action(
        {
            "action": "update_custom_provider",
            "profile_id": "p01",
            "name": "Editor Contract Provider Updated",
            "base_url": "https://api.example.com/v1",
            "chat_completions_path": "/chat/completions",
            "models_path": "/models",
            "custom_endpoints": "",
            "max_output_tokens": None,
            "clear_max_output_tokens": True,
            "clear_api_key": False,
            "enabled": True,
            "selected_model": "org/contract-model",
            "allow_remote_http": False,
        }
    )
    assert snap2["revision"] == 2
    assert snap2["profiles"][0]["max_output_tokens"] is None
