"""Issue #394: atomic owner credential rotation, revocation, and recovery."""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services.common.auth import (
    Auth,
    Authenticator,
    CredentialRotationConflict,
    CredentialRotationError,
    ServiceTokenStore,
    load_or_create_tokens,
    rotate_owner_token,
)
from services.distribution import cli as dist_cli
from services.distribution.installation import Layout
from services.launcher import main as launcher_main
from services.launcher import owner_login
from services.session.api import create_app as create_session_app
from services.session.store import Store


def test_initial_and_legacy_token_store_backward_compatibility(tmp_path: Path):
    tokens = load_or_create_tokens(tmp_path)
    assert tokens.owner_epoch == 1
    assert tokens.rotated_at is None
    assert set(tokens.keys()) == {"owner", "agent", "converse", "decisions", "resources"}
    token_file = tmp_path / "service-tokens.json"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    # Verify legacy service-tokens.json (without owner_epoch / rotated_at) loads with epoch=1.
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir(mode=0o700)
    legacy_file = legacy_dir / "service-tokens.json"
    legacy_payload = {
        role: f"{role}-" + ("x" * 30)
        for role in ("owner", "agent", "converse", "decisions", "resources", "session")
    }
    fd = os.open(legacy_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(legacy_payload, stream)

    loaded_legacy = load_or_create_tokens(legacy_dir)
    assert loaded_legacy.owner_epoch == 1
    assert loaded_legacy.rotated_at is None
    assert dict(loaded_legacy) == legacy_payload


def test_rotate_owner_revokes_old_owner_token_and_preserves_service_tokens(tmp_path: Path):
    initial = load_or_create_tokens(tmp_path)
    old_owner = initial["owner"]
    internal_before = {k: v for k, v in initial.items() if k != "owner"}

    auth = Authenticator(initial, allowed_origins=("http://127.0.0.1:8780",))
    store = Store(tmp_path / "journal.sqlite")
    app = create_session_app(store, auth)

    with TestClient(app) as client:
        # Old owner token works before rotation.
        res = client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {old_owner}"},
            json={"channel": "agent"},
        )
        assert res.status_code == 200
        session_id = res.json()["session_id"]

        # Rotate the owner credential on disk.
        report = rotate_owner_token(tmp_path, expected_owner_epoch=1)
        assert report["status"] == "rotated"
        assert report["owner_epoch"] == 2
        assert isinstance(report["rotated_at"], str) and report["rotated_at"].endswith("Z")
        assert old_owner not in json.dumps(report)

        rotated = load_or_create_tokens(tmp_path)
        new_owner = rotated["owner"]
        assert new_owner != old_owner
        assert len(new_owner) >= 32
        assert rotated.owner_epoch == 2
        assert {k: v for k, v in rotated.items() if k != "owner"} == internal_before
        assert new_owner not in json.dumps(report)

        # Old owner token is immediately rejected (401) by the running service!
        denied = client.get(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {old_owner}"},
        )
        assert denied.status_code == 401

        # New owner token immediately succeeds!
        allowed = client.get(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {new_owner}"},
        )
        assert allowed.status_code == 200

        # Internal service tokens (agent, converse, decisions, resources) remain working!
        for role in ("agent", "converse", "decisions", "resources"):
            svc_res = client.get(
                f"/v1/sessions/{session_id}",
                headers={"Authorization": f"Bearer {internal_before[role]}"},
            )
            assert svc_res.status_code == 200


def test_rotate_owner_Disconnects_active_websocket_and_rejects_old_token(tmp_path: Path):
    initial = load_or_create_tokens(tmp_path)
    old_owner = initial["owner"]
    auth = Auth(initial)
    store = Store(tmp_path / "journal.sqlite")
    app = create_session_app(store, auth)

    with TestClient(app) as client:
        session = client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {old_owner}"},
            json={"channel": "agent"},
        ).json()

        # Connect WebSocket with old owner token, then rotate and verify disconnect on next poll.
        with client.websocket_connect("/v1/events") as ws:
            ws.send_json({"token": old_owner, "after": 0, "session_id": session["session_id"]})
            rotate_owner_token(tmp_path)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1008
            try:
                ws.close()
            except Exception:
                pass

        # Reconnecting with old owner token fails closed immediately (1008).
        with client.websocket_connect("/v1/events") as ws_old:
            ws_old.send_json({"token": old_owner, "after": 0, "session_id": session["session_id"]})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws_old.receive_json()
            assert exc_info.value.code == 1008
            try:
                ws_old.close()
            except Exception:
                pass

        # Connecting with new owner token succeeds and receives events.
        new_owner = load_or_create_tokens(tmp_path)["owner"]
        appended = client.post(
            "/v1/transcript/entries",
            headers={"Authorization": f"Bearer {new_owner}"},
            json={
                "session_id": session["session_id"],
                "thread_id": session["thread_id"],
                "epoch": session["epoch"],
                "kind": "conversation.user",
                "payload": {"text": "after rotation", "final": True},
                "idempotency_key": "rot-msg-1",
            },
        )
        assert appended.status_code == 200
        with client.websocket_connect("/v1/events") as ws_new:
            ws_new.send_json({"token": new_owner, "after": 0, "session_id": session["session_id"]})
            event = ws_new.receive_json()
            assert event["payload"]["text"] == "after rotation"
            try:
                ws_new.close()
            except Exception:
                pass


def test_owner_login_invalidated_on_rotation_and_exports_new_credential(tmp_path: Path, capsys):
    initial = load_or_create_tokens(tmp_path)
    old_owner = initial["owner"]
    login_file = tmp_path / "owner-login.txt"

    owner_login(SimpleNamespace(data_dir=tmp_path, output=None))
    capsys.readouterr()
    assert login_file.exists()
    assert login_file.read_text() == old_owner
    assert stat.S_IMODE(login_file.stat().st_mode) == 0o600

    # Rotating invalidates the stale owner-login.txt file.
    report = rotate_owner_token(tmp_path)
    assert report["owner_login_invalidated"] is True
    assert not login_file.exists()

    # Running owner-login again exports the new owner credential with mode 0600 and no secret on stdout.
    owner_login(SimpleNamespace(data_dir=tmp_path, output=None))
    out, err = capsys.readouterr()
    exported_meta = json.loads(out)
    assert exported_meta["owner_epoch"] == 2
    assert exported_meta["mode"] == "0600"
    new_tokens = load_or_create_tokens(tmp_path)
    assert login_file.read_text() == new_tokens["owner"]
    assert login_file.read_text() != old_owner
    assert stat.S_IMODE(login_file.stat().st_mode) == 0o600
    assert old_owner not in out and old_owner not in err
    assert new_tokens["owner"] not in out and new_tokens["owner"] not in err


def test_cli_rotate_owner_launcher_and_distribution_with_tty_confirmation(
    tmp_path: Path, monkeypatch, capsys
):
    data_dir = tmp_path / "data"
    initial = load_or_create_tokens(data_dir)
    old_owner = initial["owner"]

    # 1. Interactive TTY refusal ('n') leaves credentials untouched.
    class FakeTTY:
        def __init__(self, reply: str):
            self.reply = reply
            self.prompts: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, text: str):
            self.prompts.append(text)

        def flush(self):
            pass

        def readline(self) -> str:
            return self.reply

    monkeypatch.setattr(dist_cli, "open_terminal", lambda: FakeTTY("n\n"))
    with pytest.raises(SystemExit) as exc_info:
        launcher_main(["--data-dir", str(data_dir), "auth", "rotate-owner"])
    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    assert old_owner not in out and old_owner not in err
    assert load_or_create_tokens(data_dir)["owner"] == old_owner
    assert load_or_create_tokens(data_dir).owner_epoch == 1

    # 2. Interactive TTY confirmation ('y') rotates the owner token.
    tty_yes = FakeTTY("y\n")
    monkeypatch.setattr(dist_cli, "open_terminal", lambda: tty_yes)
    launcher_main(["--data-dir", str(data_dir), "auth", "rotate-owner"])
    out, err = capsys.readouterr()
    assert any("(y/n) [n]" in p for p in tty_yes.prompts)
    result = json.loads(out)
    assert result["status"] == "rotated"
    assert result["owner_epoch"] == 2
    rotated_1 = load_or_create_tokens(data_dir)
    assert rotated_1["owner"] != old_owner
    assert old_owner not in out and rotated_1["owner"] not in out

    # 3. Non-interactive --yes via distribution CLI using chosen_layout().
    layout = Layout.for_home(tmp_path / "home")
    load_or_create_tokens(layout.data)
    monkeypatch.setattr(dist_cli, "chosen_layout", lambda: layout)
    dist_cli.main(["auth", "rotate-owner", "--yes"])
    out, err = capsys.readouterr()
    result_dist = json.loads(out)
    assert result_dist["status"] == "rotated"
    assert result_dist["owner_epoch"] == 2
    rotated_dist = load_or_create_tokens(layout.data)
    assert rotated_dist["owner"] not in out and rotated_dist["owner"] not in err


def test_concurrent_modification_cas_and_lock_contention_refusal(tmp_path: Path):
    initial = load_or_create_tokens(tmp_path)
    old_owner = initial["owner"]

    # Wrong expected_owner_epoch fails closed without modifying tokens.
    with pytest.raises(CredentialRotationConflict, match="epoch mismatch"):
        rotate_owner_token(tmp_path, expected_owner_epoch=99)
    assert load_or_create_tokens(tmp_path)["owner"] == old_owner

    # Wrong expected_sha256 fails closed.
    with pytest.raises(CredentialRotationConflict, match="SHA-256 mismatch"):
        rotate_owner_token(tmp_path, expected_sha256="0" * 64)
    assert load_or_create_tokens(tmp_path)["owner"] == old_owner

    # Concurrent file modification between initial read and atomic os.replace is caught!
    token_file = tmp_path / "service-tokens.json"
    tampered_payload = dict(initial)
    tampered_payload["owner"] = "concurrent-writer-" + ("z" * 24)
    tampered_payload["owner_epoch"] = 2

    def tamper_before_replace():
        fd = os.open(token_file, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(tampered_payload, stream)
            stream.flush()
            os.fsync(stream.fileno())

    with pytest.raises(CredentialRotationConflict, match="Concurrent modification"):
        rotate_owner_token(tmp_path, _before_replace=tamper_before_replace)
    # Temp staging file was cleaned up and concurrent writer's file was not overwritten.
    assert not list(tmp_path.glob(".service-tokens.json.tmp.*"))
    assert load_or_create_tokens(tmp_path)["owner"] == tampered_payload["owner"]


def test_interrupted_staging_recovery_and_permission_enforcement(tmp_path: Path):
    tokens = load_or_create_tokens(tmp_path)
    orphan_tmp = tmp_path / ".service-tokens.json.tmp.deadbeef"
    orphan_tmp.write_text("partial-interrupted-write")
    orphan_tmp.chmod(0o600)

    store = ServiceTokenStore(tmp_path)
    result = store.rotate_owner(expected_owner_epoch=1)
    assert result["owner_epoch"] == 2
    assert not orphan_tmp.exists()
    token_file = tmp_path / "service-tokens.json"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    # Weakening permissions to 0644 causes both rotate_owner_token and Auth to fail closed.
    auth = Auth(load_or_create_tokens(tmp_path))
    token_file.chmod(0o644)
    with pytest.raises(PermissionError, match="0600"):
        rotate_owner_token(tmp_path)
    with pytest.raises(Exception) as exc_info:
        auth.authenticate(tokens["agent"])
    assert getattr(exc_info.value, "status_code", None) == 401

    # Restore 0600 and verify active_ports refusal when a port is busy.
    token_file.chmod(0o600)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        busy_port = listener.getsockname()[1]
        with pytest.raises(CredentialRotationError, match="Active services are listening"):
            rotate_owner_token(tmp_path, active_ports=(busy_port,))
    assert load_or_create_tokens(tmp_path).owner_epoch == 2


def test_store_rotation_fences_native_media_identity(tmp_path: Path):
    initial = load_or_create_tokens(tmp_path)
    auth = Auth(initial)
    identity = auth.media_identity()
    assert auth.media_identity() is identity

    rotate_owner_token(tmp_path, expected_owner_epoch=1)
    with pytest.raises(ValueError, match="^Native authentication identity is unavailable$"):
        auth.media_identity()
    rotated = load_or_create_tokens(tmp_path)
    assert auth.authenticate(rotated["owner"]) == "owner"
    with pytest.raises(ValueError, match="^Native authentication identity is unavailable$"):
        auth.media_identity()
    assert Auth(rotated).media_identity().instance_id != identity.instance_id
