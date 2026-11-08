"""Unit and integration tests for device refresh tokens and RFC 6265 cookie scoping (#500, #507)."""

from __future__ import annotations

import hashlib
import http.client
import json
import stat
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from services.common.auth import (
    load_or_create_tokens,
    register_rotation_revoker,
    rotate_owner_token,
    unregister_rotation_revoker,
)
from services.common.owner_signin import (
    DEVICE_REFRESH_TOKENS_FILE_NAME,
    DeviceRefreshTokenManager,
    LocalOwnerSignInBroker,
    LocalSignInDenied,
    LocalSignInExpired,
    LocalSignInStale,
    SessionTokenManager,
    data_dir_cookie_suffix,
    get_handoff_cookie_name,
    parse_handoff_cookie,
)
from services.distribution import web


def _start_web_server(root: Path, broker: LocalOwnerSignInBroker):
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.asset_handler(root, broker=broker))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_device_refresh_token_manager_digest_only_storage(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    load_or_create_tokens(data_dir)

    manager = DeviceRefreshTokenManager(data_dir)
    issued = manager.issue_refresh_token(owner_epoch=1, device_label="laptop")

    raw_token = issued["refresh_token"]
    assert raw_token.startswith("kny_refr_")
    assert len(raw_token) >= 32

    # Verify file security: 0600 mode
    token_file = data_dir / DEVICE_REFRESH_TOKENS_FILE_NAME
    assert token_file.exists()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    # Verify digest-only storage: raw token NEVER written to disk
    raw_content = token_file.read_text(encoding="utf-8")
    assert raw_token not in raw_content

    expected_sha = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert expected_sha in raw_content

    parsed = json.loads(raw_content)
    assert parsed["owner_epoch"] == 1
    assert expected_sha in parsed["tokens"]
    entry = parsed["tokens"][expected_sha]
    assert entry["device_label"] == "laptop"
    assert entry["owner_epoch"] == 1


def test_device_refresh_token_exchange_and_sliding_expiry(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    tokens = load_or_create_tokens(data_dir)
    owner_sha = hashlib.sha256(tokens["owner"].encode("utf-8")).hexdigest()

    simulated_time = 1000.0

    def clock():
        return simulated_time

    manager = DeviceRefreshTokenManager(data_dir, clock=clock)
    session_manager = SessionTokenManager(data_dir, clock=clock)

    issued = manager.issue_refresh_token(owner_epoch=1, device_label="browser", ttl_seconds=100.0)
    raw_token = issued["refresh_token"]
    assert issued["expires_at"] == 1100.0

    # Advance time within TTL
    simulated_time = 1050.0
    exchange_result = manager.exchange_refresh_token(
        raw_token,
        current_owner_epoch=1,
        session_manager=session_manager,
        host_generation="gen1",
        owner_token_sha256=owner_sha,
        session_ttl_seconds=3600.0,
        sliding_ttl_seconds=100.0,
    )

    assert exchange_result["status"] == "authenticated"
    assert exchange_result["session_token"].startswith("kny_sess_")
    # Sliding expiry pushed expiration to now + sliding_ttl_seconds (1050 + 100 = 1150)
    assert exchange_result["refresh_expires_at"] == 1150.0

    # Verify unknown token rejected
    with pytest.raises(LocalSignInDenied, match="Unknown or revoked"):
        manager.exchange_refresh_token(
            "kny_refr_" + "x" * 40,
            current_owner_epoch=1,
            session_manager=session_manager,
            host_generation="gen1",
            owner_token_sha256=owner_sha,
        )

    # Advance time beyond new expiry
    simulated_time = 1200.0
    with pytest.raises(LocalSignInExpired, match="expired"):
        manager.exchange_refresh_token(
            raw_token,
            current_owner_epoch=1,
            session_manager=session_manager,
            host_generation="gen1",
            owner_token_sha256=owner_sha,
        )


def test_device_refresh_token_revoked_on_owner_epoch_mismatch(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    tokens = load_or_create_tokens(data_dir)
    owner_sha = hashlib.sha256(tokens["owner"].encode("utf-8")).hexdigest()

    manager = DeviceRefreshTokenManager(data_dir)
    session_manager = SessionTokenManager(data_dir)

    issued = manager.issue_refresh_token(owner_epoch=1, device_label="browser")
    raw_token = issued["refresh_token"]

    # When owner_epoch advances to 2, presenting old token raises LocalSignInStale
    with pytest.raises(LocalSignInStale, match="Owner epoch changed"):
        manager.exchange_refresh_token(
            raw_token,
            current_owner_epoch=2,
            session_manager=session_manager,
            host_generation="gen1",
            owner_token_sha256=owner_sha,
        )


def test_owner_credential_rotation_wipes_device_refresh_tokens_and_triggers_revoker(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    load_or_create_tokens(data_dir)

    manager = DeviceRefreshTokenManager(data_dir)
    manager.issue_refresh_token(owner_epoch=1, device_label="device1")

    token_file = data_dir / DEVICE_REFRESH_TOKENS_FILE_NAME
    assert token_file.exists()

    revoker_called = []

    def revoker():
        revoker_called.append(True)

    register_rotation_revoker(revoker)
    try:
        # Rotate owner token
        rotate_owner_token(data_dir)
        assert len(revoker_called) == 1
        # Token file should be wiped on rotation
        assert not token_file.exists()
    finally:
        unregister_rotation_revoker(revoker)


def test_web_refresh_session_endpoint_integration(tmp_path: Path):
    data_dir = tmp_path / "data"
    ui_root = tmp_path / "web"
    ui_root.mkdir(parents=True, exist_ok=True)
    (ui_root / "index.html").write_text("<html>Kinaiya</html>")
    (ui_root / "main.dart.js").write_text("// JS")

    load_or_create_tokens(data_dir)
    broker = LocalOwnerSignInBroker(data_dir)
    server, thread = _start_web_server(ui_root, broker)
    port = server.server_port
    origin = f"http://127.0.0.1:{port}"

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

        # 1. First-run GET / and consume handoff
        conn.request("GET", "/")
        res = conn.getresponse()
        _ = res.read()
        set_cookie = res.getheader("Set-Cookie") or ""
        seal = parse_handoff_cookie(set_cookie.split(";", 1)[0], data_dir=data_dir)
        assert seal is not None

        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{get_handoff_cookie_name(data_dir)}={seal}",
            },
        )
        signin_res = conn.getresponse()
        signin_data = json.loads(signin_res.read().decode("utf-8"))
        assert signin_res.status == 200
        # Verified receipt includes persistent device refresh token
        assert "refresh_token" in signin_data
        refresh_token = signin_data["refresh_token"]
        assert refresh_token.startswith("kny_refr_")

        # 2. Later session refresh via POST /__kinaiya/refresh-session
        conn.request(
            "POST",
            "/__kinaiya/refresh-session",
            body=json.dumps({"refresh_token": refresh_token}),
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
        )
        refresh_res = conn.getresponse()
        refresh_data = json.loads(refresh_res.read().decode("utf-8"))
        assert refresh_res.status == 200
        assert refresh_data["status"] == "authenticated"
        assert refresh_data["session_token"].startswith("kny_sess_")
        assert refresh_data["role"] == "owner"
        assert refresh_data["refresh_token"] == refresh_token

        # 3. Cross-origin / invalid CSRF refresh is rejected (403)
        conn.request(
            "POST",
            "/__kinaiya/refresh-session",
            body=json.dumps({"refresh_token": refresh_token}),
            headers={
                "Origin": "http://evil.com",
                "Sec-Fetch-Site": "cross-site",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
        )
        evil_res = conn.getresponse()
        _ = evil_res.read()
        assert evil_res.status == 403

        # 4. Unknown refresh token is rejected (403)
        conn.request(
            "POST",
            "/__kinaiya/refresh-session",
            body=json.dumps({"refresh_token": "kny_refr_unknown_fake_token_12345"}),
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
        )
        unknown_res = conn.getresponse()
        _ = unknown_res.read()
        assert unknown_res.status == 403
    finally:
        server.shutdown()
        server.server_close()


def test_rfc6265_data_dir_cookie_scoping_prevents_collision(tmp_path: Path):
    data_dir1 = tmp_path / "data1"
    data_dir2 = tmp_path / "data2"
    data_dir1.mkdir(parents=True, exist_ok=True)
    data_dir2.mkdir(parents=True, exist_ok=True)

    suffix1 = data_dir_cookie_suffix(data_dir1)
    suffix2 = data_dir_cookie_suffix(data_dir2)
    assert len(suffix1) == 16
    assert len(suffix2) == 16
    assert suffix1 != suffix2

    cookie_name1 = get_handoff_cookie_name(data_dir1)
    cookie_name2 = get_handoff_cookie_name(data_dir2)
    assert cookie_name1 != cookie_name2
    assert cookie_name1 == f"kinaiya_local_handoff_{suffix1}"
    assert cookie_name2 == f"kinaiya_local_handoff_{suffix2}"

    # In a shared browser jar, both cookies can coexist without overwriting
    cookie_header = f"{cookie_name1}=seal_alpha; {cookie_name2}=seal_beta"
    assert parse_handoff_cookie(cookie_header, data_dir=data_dir1) == "seal_alpha"
    assert parse_handoff_cookie(cookie_header, data_dir=data_dir2) == "seal_beta"
