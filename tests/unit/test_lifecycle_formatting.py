"""Unit tests for human-readable TUI status formatting (#510)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from services.distribution.cli import print_command_result
from services.distribution.lifecycle_formatting import (
    format_auth_report,
    format_recover_report,
    format_rollback_report,
    format_uninstall_report,
    format_update_report,
    format_version_report,
)


def test_format_update_report_current():
    result = {
        "action": "current",
        "channel": "nightly",
        "version": "0.1.0-nightly.11+6c046873dc34",
        "source_sha": "6c046873dc3441f99c8652c3bc632f865c50060d",
        "release": {
            "version": "0.1.0-nightly.11+6c046873dc34",
            "channel": "nightly",
            "source_sha": "6c046873dc3441f99c8652c3bc632f865c50060d",
        },
        "state_policy": "no migrations; no database rollback",
        "services": "must be stopped explicitly; no task replay",
    }
    rendered = format_update_report(result, no_color=True, is_tty=True)
    assert "Kinaiya Update Check" in rendered
    assert "Update channel: nightly" in rendered
    assert "0.1.0-nightly.11+6c046873dc34" in rendered
    assert "6c046873dc34" in rendered
    assert "✓ Host & sandbox prerequisites verified" in rendered
    assert "✓ Core runtime and service contracts up to date" in rendered
    assert "✓ Prebuilt Web UI assets up to date" in rendered
    assert "✓ Already up to date!" in rendered


def test_format_update_report_upgrade_available():
    result = {
        "action": "upgrade",
        "channel": "stable",
        "version": "0.2.0",
        "source_sha": "abcdef1234567890abcdef1234567890abcdef12",
        "release": {
            "version": "0.2.0",
            "artifacts": [{"platform": "linux", "size": 42 * 1024 * 1024}],
        },
    }
    rendered = format_update_report(result, no_color=True, is_tty=True)
    assert "Kinaiya Update Available (Upgrade)" in rendered
    assert "Target release: 0.2.0" in rendered
    assert "Download size: 42.0 MB" in rendered
    assert "Running local services must be stopped" in rendered
    assert "kinaiya update --yes" in rendered


def test_format_update_report_installed_complete():
    result = {
        "installed": {
            "version": "0.2.0",
            "channel": "stable",
            "source_sha": "1234567890abcdef",
        }
    }
    rendered = format_update_report(result, no_color=True, is_tty=True)
    assert "Kinaiya Update Complete" in rendered
    assert "Applied release: 0.2.0" in rendered
    assert "Successfully updated!" in rendered
    assert "Run 'kinaiya' to start services" in rendered


def test_format_version_report():
    managed_result = {
        "managed": True,
        "install": "/opt/kinaiya",
        "release": {
            "version": "0.1.0",
            "channel": "stable",
            "source_sha": "abcdef1234567890",
            "build_number": 42,
        },
        "ready": "not_checked",
    }
    rendered = format_version_report(managed_result, no_color=True, is_tty=True)
    assert "Kinaiya Distribution Version" in rendered
    assert "Version: 0.1.0" in rendered
    assert "Channel: stable" in rendered
    assert "Build number: 42" in rendered
    assert "Managed install: Yes" in rendered
    assert "Install root: /opt/kinaiya" in rendered
    assert "Run 'kinaiya doctor'" in rendered

    dev_result = {"managed": False, "install": "/home/user/Kinaiya", "release": None}
    rendered_dev = format_version_report(dev_result, no_color=True, is_tty=True)
    assert "Version: development checkout (unmanaged)" in rendered_dev
    assert "Managed install: No" in rendered_dev


def test_format_auth_report():
    result = {
        "action": "owner_token_rotated",
        "owner_epoch": 3,
        "owner_token_sha256": "abcdef0123456789abcdef0123456789",
    }
    rendered = format_auth_report(result, no_color=True, is_tty=True)
    assert "Kinaiya Owner Credential Rotation" in rendered
    assert "Owner credential rotated successfully!" in rendered
    assert "New owner epoch: 3" in rendered
    assert "abcdef0123456789" in rendered
    assert "revoked" in rendered


def test_format_recover_report():
    clean_result = {"action": "nothing_to_recover"}
    rendered_clean = format_recover_report(clean_result, no_color=True, is_tty=True)
    assert "No interrupted transaction found; installation is clean" in rendered_clean

    rec_result = {
        "action": "discarded_stage",
        "generation": "0123456789abcdef0123456789abcdef",
    }
    rendered_rec = format_recover_report(rec_result, no_color=True, is_tty=True)
    assert "0123456789abcdef0123456789abcdef" in rendered_rec
    assert "discarded_stage" in rendered_rec


def test_format_rollback_report():
    result = {"action": "code_rollback", "generation": "fedcba9876543210fedcba9876543210"}
    rendered = format_rollback_report(result, no_color=True, is_tty=True)
    assert "Kinaiya Generation Rollback" in rendered
    assert "fedcba9876543210fedcba9876543210" in rendered
    assert "Code rollback applied safely" in rendered


def test_format_uninstall_report():
    preserved_result = {"action": "uninstalled", "data_deleted": False}
    rendered = format_uninstall_report(preserved_result, no_color=True, is_tty=True)
    assert "Kinaiya Uninstallation Complete" in rendered
    assert "User configuration and private data directories preserved" in rendered

    deleted_result = {"action": "uninstalled", "data_deleted": True}
    rendered_del = format_uninstall_report(deleted_result, no_color=True, is_tty=True)
    assert "Private data and configuration directories deleted" in rendered_del


def test_print_command_result_renders_checklist_on_tty(capsys):
    args = SimpleNamespace(command="update", json=False)
    result = {
        "action": "current",
        "channel": "nightly",
        "version": "0.1.0",
        "source_sha": "abc123",
        "release": {"version": "0.1.0"},
    }
    with patch("sys.stdout.isatty", return_value=True):
        print_command_result(args, result)
    out = capsys.readouterr().out
    assert "Kinaiya Update Check" in out
    assert "Already up to date!" in out


def test_print_command_result_respects_json_flag(capsys):
    args = SimpleNamespace(command="update", json=True)
    result = {"action": "current", "version": "0.1.0"}
    with patch("sys.stdout.isatty", return_value=True):
        print_command_result(args, result)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["action"] == "current"
    assert parsed["version"] == "0.1.0"


def test_print_command_result_defaults_to_json_when_piped(capsys):
    args = SimpleNamespace(command="update", json=False)
    result = {"action": "current", "version": "0.1.0"}
    with patch("sys.stdout.isatty", return_value=False):
        print_command_result(args, result)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["action"] == "current"
