"""Issue #393: automatic one-time local owner sign-in without manual bearer-token entry."""

from __future__ import annotations

import http.client
import json
import stat
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services.common.auth import Auth, load_or_create_tokens, rotate_owner_token
from services.common.owner_signin import (
    HANDOFF_COOKIE_NAME,
    LocalOwnerSignInBroker,
    parse_handoff_cookie,
)
from services.distribution import web
from services.session.api import create_app as create_session_app
from services.session.store import Store
from services.settings.provider_api import router as provider_router


def _start_web_server(root: Path, broker: LocalOwnerSignInBroker):
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.asset_handler(root, broker=broker))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_first_local_launch_handoff_authenticates_session_and_agent_without_manual_token(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    ui_root = tmp_path / "web"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>compiled owner UI</html>")
    (ui_root / "main.dart.js").write_text("// compiled dart bundle")

    tokens = load_or_create_tokens(data_dir)
    long_lived_owner_token = tokens["owner"]

    broker = LocalOwnerSignInBroker(data_dir)
    server, thread = _start_web_server(ui_root, broker)
    port = server.server_port
    origin = f"http://127.0.0.1:{port}"

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        # Step 1: Initial trusted launch GET / seals the grant and sets HttpOnly SameSite=Strict cookie.
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8")
        assert res.status == 200
        assert body == "<html>compiled owner UI</html>"
        set_cookie = res.getheader("Set-Cookie")
        assert set_cookie is not None
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        assert "Path=/__kinaiya/local-signin" in set_cookie
        assert long_lived_owner_token not in set_cookie
        assert long_lived_owner_token not in body

        seal = parse_handoff_cookie(set_cookie.split(";", 1)[0])
        assert seal is not None and len(seal) >= 32

        # Step 2: Same-origin POST /__kinaiya/local-signin with CSRF header and HttpOnly cookie.
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        signin_res = conn.getresponse()
        signin_raw = signin_res.read().decode("utf-8")
        assert signin_res.status == 200, signin_raw
        assert signin_res.getheader("Cache-Control") == "no-store"
        clear_cookie = signin_res.getheader("Set-Cookie") or ""
        assert "Max-Age=0" in clear_cookie

        signin_data = json.loads(signin_raw)
        session_token = signin_data["session_token"]
        assert session_token.startswith("kny_sess_")
        assert session_token != long_lived_owner_token
        assert long_lived_owner_token not in signin_raw
        assert signin_data["role"] == "owner"
        assert signin_data["credential_kind"] == "scoped_owner_session"
        assert signin_data["host_generation"] == broker.host_generation
        assert signin_data["owner_epoch"] == 1

        # Private state files are 0600 and never store raw cookie_seal, raw session_token, or owner token.
        handoff_file = data_dir / "local-owner-handoff.json"
        sessions_file = data_dir / "local-owner-sessions.json"
        for path in (handoff_file, sessions_file):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            content = path.read_text()
            assert long_lived_owner_token not in content
            assert seal not in content
            assert session_token not in content

        # Step 3: Scoped session token authenticates against Session HTTP, WebSocket, and Agent/Provider routes.
        auth = Auth(tokens, allowed_origins=(origin,))
        store = Store(data_dir / "journal.sqlite")
        app = create_session_app(store, auth)
        app.include_router(provider_router(None, auth))

        with TestClient(app) as client:
            created = client.post(
                "/v1/sessions",
                headers={"Authorization": f"Bearer {session_token}", "Origin": origin},
                json={"channel": "agent"},
            )
            assert created.status_code == 200
            session_info = created.json()

            listed = client.get(
                "/v1/sessions",
                headers={"Authorization": f"Bearer {session_token}", "Origin": origin},
            )
            assert listed.status_code == 200

            appended = client.post(
                "/v1/transcript/entries",
                headers={"Authorization": f"Bearer {session_token}", "Origin": origin},
                json={
                    "session_id": session_info["session_id"],
                    "thread_id": session_info["thread_id"],
                    "epoch": session_info["epoch"],
                    "kind": "conversation.user",
                    "payload": {"text": "signed in automatically", "final": True},
                    "idempotency_key": "local-signin-msg-1",
                },
            )
            assert appended.status_code == 200

            with client.websocket_connect("/v1/events", headers={"Origin": origin}) as ws:
                ws.send_json(
                    {
                        "token": session_token,
                        "after": 0,
                        "session_id": session_info["session_id"],
                    }
                )
                event = ws.receive_json()
                assert event["payload"]["text"] == "signed in automatically"
                ws.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_replay_second_tab_and_unsolicited_local_process_fail_closed(tmp_path: Path):
    data_dir = tmp_path / "data"
    ui_root = tmp_path / "web"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>owner UI</html>")
    (ui_root / "main.dart.js").write_text("// js")

    load_or_create_tokens(data_dir)
    broker = LocalOwnerSignInBroker(data_dir)
    server, thread = _start_web_server(ui_root, broker)
    port = server.server_port
    origin = f"http://127.0.0.1:{port}"

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

        # 1. Unsolicited local process calling POST /__kinaiya/local-signin BEFORE GET / fails closed (403).
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "X-Kinaiya-Local-Handoff": "1",
            },
        )
        unsolicited = conn.getresponse()
        _ = unsolicited.read()
        assert unsolicited.status == 403

        # 2. First browser tab GET / obtains the single-use seal cookie.
        conn.request("GET", "/")
        first_get = conn.getresponse()
        _ = first_get.read()
        first_cookie = first_get.getheader("Set-Cookie")
        assert first_cookie is not None
        seal = parse_handoff_cookie(first_cookie.split(";", 1)[0])
        assert seal is not None

        # 3. Second browser tab or process calling GET / after seal receives NO Set-Cookie header!
        conn.request("GET", "/")
        second_get = conn.getresponse()
        _ = second_get.read()
        assert second_get.status == 200
        assert second_get.getheader("Set-Cookie") is None

        # 4. First tab consumes the handoff and succeeds once.
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        first_post = conn.getresponse()
        _ = first_post.read()
        assert first_post.status == 200

        # 5. Replay of the consumed handoff cookie fails closed (403)!
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        replay_post = conn.getresponse()
        _ = replay_post.read()
        assert replay_post.status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_wrong_origin_missing_csrf_and_expired_handoff_fail_closed(tmp_path: Path):
    data_dir = tmp_path / "data"
    ui_root = tmp_path / "web"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>owner UI</html>")
    (ui_root / "main.dart.js").write_text("// js")

    load_or_create_tokens(data_dir)
    now = [1000.0]
    broker = LocalOwnerSignInBroker(data_dir, ttl_seconds=30.0, clock=lambda: now[0])
    server, thread = _start_web_server(ui_root, broker)
    port = server.server_port
    origin = f"http://127.0.0.1:{port}"

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/")
        res = conn.getresponse()
        _ = res.read()
        seal = parse_handoff_cookie((res.getheader("Set-Cookie") or "").split(";", 1)[0])
        assert seal is not None

        # Wrong Origin header fails closed (403) without issuing a token.
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": "http://evil.example:8780",
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        bad_origin = conn.getresponse()
        assert bad_origin.status == 403
        assert "session_token" not in bad_origin.read().decode("utf-8")

        # Wrong localhost port Origin fails closed (403).
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": f"http://127.0.0.1:{port + 1}",
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        bad_port = conn.getresponse()
        _ = bad_port.read()
        assert bad_port.status == 403

        # Missing X-Kinaiya-Local-Handoff CSRF header fails closed (403).
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        no_csrf = conn.getresponse()
        _ = no_csrf.read()
        assert no_csrf.status == 403

        # Cross-site Sec-Fetch-Site header fails closed (403).
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "cross-site",
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        cross_site = conn.getresponse()
        _ = cross_site.read()
        assert cross_site.status == 403

        # Query string in URL fails closed (403).
        conn.request(
            "POST",
            "/__kinaiya/local-signin?token=leak",
            headers={
                "Origin": origin,
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        with_query = conn.getresponse()
        _ = with_query.read()
        assert with_query.status == 403

        # Advance clock past ttl_seconds -> expired handoff fails closed (403).
        now[0] = 1031.0
        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        expired = conn.getresponse()
        _ = expired.read()
        assert expired.status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_stale_host_generation_and_owner_token_rotation_invalidate_session_and_handoff(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    tokens = load_or_create_tokens(data_dir)
    origin = "http://127.0.0.1:8780"
    now = [2000.0]

    broker = LocalOwnerSignInBroker(
        data_dir,
        allowed_origins=(origin,),
        clock=lambda: now[0],
    )
    cookie_header = broker.seal_initial_launch_cookie(client_host="127.0.0.1", origin=origin)
    assert cookie_header is not None
    seal = parse_handoff_cookie(cookie_header.split(";", 1)[0])

    auth = Auth(tokens, allowed_origins=(origin,), session_clock=lambda: now[0])
    store = Store(data_dir / "journal.sqlite")
    app = create_session_app(store, auth)

    with TestClient(app) as client:
        # Exchange via Session service POST /v1/auth/local-exchange
        exchange_res = client.post(
            "/v1/auth/local-exchange",
            headers={
                "Origin": origin,
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
            json={"host_generation": broker.host_generation},
        )
        assert exchange_res.status_code == 200
        session_token_1 = exchange_res.json()["session_token"]

        # Replay on /v1/auth/local-exchange fails closed (403).
        replay = client.post(
            "/v1/auth/local-exchange",
            headers={
                "Origin": origin,
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
            json={"host_generation": broker.host_generation},
        )
        assert replay.status_code == 403

        # Session token 1 works initially.
        assert (
            client.get(
                "/v1/sessions",
                headers={"Authorization": f"Bearer {session_token_1}"},
            ).status_code
            == 200
        )

        # Host restart (new host_generation) immediately invalidates session_token_1!
        broker.rotate_host_generation()
        assert (
            client.get(
                "/v1/sessions",
                headers={"Authorization": f"Bearer {session_token_1}"},
            ).status_code
            == 401
        )

        # Issue a second session token under the new host_generation.
        cookie_2 = broker.seal_initial_launch_cookie(client_host="127.0.0.1", origin=origin)
        seal_2 = parse_handoff_cookie((cookie_2 or "").split(";", 1)[0])
        exchanged_2 = broker.consume_handoff(
            cookie_seal=seal_2,
            origin=origin,
            csrf_header="1",
            client_host="127.0.0.1",
        )
        assert exchanged_2["owner_token"] == exchanged_2["session_token"]
        assert exchanged_2["owner_token"].startswith("kny_sess_")
        session_token_2 = exchanged_2["session_token"]

        created = client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {session_token_2}"},
            json={"channel": "agent"},
        )
        assert created.status_code == 200
        sess_id = created.json()["session_id"]

        # Rotating the owner credential in service-tokens.json immediately invalidates session_token_2
        # on both HTTP and active WebSocket connections!
        with client.websocket_connect("/v1/events") as ws:
            ws.send_json({"token": session_token_2, "after": 0, "session_id": sess_id})
            rotate_owner_token(data_dir)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1008

        assert (
            client.get(
                "/v1/sessions",
                headers={"Authorization": f"Bearer {session_token_2}"},
            ).status_code
            == 401
        )


def test_storage_status_diagnosis_reports_missing_corrupt_and_ready_without_reinitialization(
    tmp_path: Path,
):
    data_dir = tmp_path / "data"
    ui_root = tmp_path / "web"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>owner UI</html>")
    (ui_root / "main.dart.js").write_text("// js")

    # 1. Missing storage
    load_or_create_tokens(data_dir)
    broker = LocalOwnerSignInBroker(data_dir)
    server, thread = _start_web_server(ui_root, broker)
    port = server.server_port

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        # Check initial ready status
        conn.request("GET", "/__kinaiya/storage-status")
        res = conn.getresponse()
        assert res.status == 200
        data = json.loads(res.read().decode("utf-8"))
        assert data["status"] == "ready"
        assert data["owner_epoch"] == 1

        # Delete service-tokens.json -> status becomes missing (503), NEVER silently recreated!
        (data_dir / "service-tokens.json").unlink()
        conn.request("GET", "/__kinaiya/storage-status")
        res = conn.getresponse()
        assert res.status == 503
        data = json.loads(res.read().decode("utf-8"))
        assert data["status"] == "missing"
        assert "Private credential storage does not exist" in data["detail"]
        assert not (data_dir / "service-tokens.json").exists()

        # Write file with insecure permissions (0644)
        (data_dir / "service-tokens.json").write_text("{}")
        (data_dir / "service-tokens.json").chmod(0o644)
        conn.request("GET", "/__kinaiya/storage-status")
        res = conn.getresponse()
        assert res.status == 503
        data = json.loads(res.read().decode("utf-8"))
        assert data["status"] == "permission_denied"

        # Set 0600 mode but corrupt content
        (data_dir / "service-tokens.json").chmod(0o600)
        (data_dir / "service-tokens.json").write_text("NOT_JSON")
        conn.request("GET", "/__kinaiya/storage-status")
        res = conn.getresponse()
        assert res.status == 503
        data = json.loads(res.read().decode("utf-8"))
        assert data["status"] == "corrupt"
        assert "corrupt or malformed" in data["detail"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_recovery_challenge_and_access_recovery_without_credential_rotation(tmp_path: Path):
    data_dir = tmp_path / "data"
    ui_root = tmp_path / "web"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>owner UI</html>")
    (ui_root / "main.dart.js").write_text("// js")

    tokens = load_or_create_tokens(data_dir)
    initial_owner_token = tokens["owner"]
    broker = LocalOwnerSignInBroker(data_dir)
    server, thread = _start_web_server(ui_root, broker)
    port = server.server_port
    origin = f"http://127.0.0.1:{port}"

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

        # 1. Simulate handoff expiration: user didn't consume launch handoff in time.
        # Request recovery challenge via POST /__kinaiya/recovery/challenge
        conn.request(
            "POST",
            "/__kinaiya/recovery/challenge",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
            },
        )
        chal_res = conn.getresponse()
        chal_raw = chal_res.read().decode("utf-8")
        assert chal_res.status == 200, chal_raw
        chal_data = json.loads(chal_raw)
        assert "challenge_id" in chal_data
        assert "challenge_path" in chal_data
        # Security invariant: secret code must NOT be sent over HTTP in response!
        assert "code" not in chal_data
        assert "auth_code" not in chal_data

        # 2. Challenge file exists on disk with 0600 mode and contains code
        chal_file = Path(chal_data["challenge_path"])
        assert chal_file.exists()
        assert stat.S_IMODE(chal_file.stat().st_mode) == 0o600
        chal_disk = json.loads(chal_file.read_text())
        assert chal_disk["code"].startswith("kny_rec_")

        # 3. Recover access by submitting the challenge proof via POST /__kinaiya/recover-access
        conn.request(
            "POST",
            "/__kinaiya/recover-access",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
            body=json.dumps({"authorization_proof": chal_disk["code"]}),
        )
        rec_res = conn.getresponse()
        rec_raw = rec_res.read().decode("utf-8")
        assert rec_res.status == 200, rec_raw
        rec_data = json.loads(rec_raw)
        assert rec_data["status"] == "authenticated"
        assert rec_data["role"] == "owner"
        assert rec_data["credential_kind"] == "scoped_owner_session"
        assert rec_data["proof_kind"] == "challenge"
        new_session_token = rec_data["session_token"]
        assert new_session_token.startswith("kny_sess_")

        # Invariant: Owner credential was NOT rotated!
        current_tokens = load_or_create_tokens(data_dir)
        assert current_tokens["owner"] == initial_owner_token
        assert current_tokens.owner_epoch == 1

        # Challenge file was consumed and deleted upon use
        assert not chal_file.exists()

        # 4. Verify the recovered session token works with Session service
        auth = Auth(current_tokens, allowed_origins=(origin,))
        store = Store(data_dir / "journal.sqlite")
        app = create_session_app(store, auth)
        with TestClient(app) as client:
            res = client.get(
                "/v1/sessions",
                headers={"Authorization": f"Bearer {new_session_token}", "Origin": origin},
            )
            assert res.status_code == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gui_owner_credential_rotation_invalidates_prior_sessions_and_handoffs(tmp_path: Path):
    data_dir = tmp_path / "data"
    ui_root = tmp_path / "web"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>owner UI</html>")
    (ui_root / "main.dart.js").write_text("// js")

    tokens = load_or_create_tokens(data_dir)
    initial_owner = tokens["owner"]
    broker = LocalOwnerSignInBroker(data_dir)
    server, thread = _start_web_server(ui_root, broker)
    port = server.server_port
    origin = f"http://127.0.0.1:{port}"

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

        # 1. Establish an initial session token
        conn.request("GET", "/")
        res = conn.getresponse()
        _ = res.read()
        seal = parse_handoff_cookie((res.getheader("Set-Cookie") or "").split(";", 1)[0])

        conn.request(
            "POST",
            "/__kinaiya/local-signin",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Cookie": f"{HANDOFF_COOKIE_NAME}={seal}",
            },
        )
        signin_res = conn.getresponse()
        signin_data = json.loads(signin_res.read().decode("utf-8"))
        old_session_token = signin_data["session_token"]

        # Create active session in Session service
        auth = Auth(tokens, allowed_origins=(origin,))
        store = Store(data_dir / "journal.sqlite")
        app = create_session_app(store, auth)
        with TestClient(app) as client:
            assert (
                client.get(
                    "/v1/sessions",
                    headers={"Authorization": f"Bearer {old_session_token}", "Origin": origin},
                ).status_code
                == 200
            )

        # 2. Perform GUI credential rotation with explicit confirmation and return_exported_token
        conn.request(
            "POST",
            "/__kinaiya/rotate-owner",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
            body=json.dumps(
                {
                    "authorization_proof": old_session_token,
                    "confirm": True,
                    "return_exported_token": True,
                }
            ),
        )
        rot_res = conn.getresponse()
        rot_raw = rot_res.read().decode("utf-8")
        assert rot_res.status == 200, rot_raw
        rot_data = json.loads(rot_raw)
        assert rot_data["status"] == "rotated"
        assert rot_data["owner_epoch"] == 2
        assert rot_data["proof_kind"] == "scoped_session"
        new_session_token = rot_data["session_token"]
        assert new_session_token != old_session_token
        assert "new_owner_token" in rot_data
        new_owner_token = rot_data["new_owner_token"]
        assert new_owner_token != initial_owner

        # 3. Old session token is immediately rejected on Session service
        with TestClient(app) as client:
            assert (
                client.get(
                    "/v1/sessions",
                    headers={"Authorization": f"Bearer {old_session_token}", "Origin": origin},
                ).status_code
                == 401
            )
            # New session token succeeds with new authority
            assert (
                client.get(
                    "/v1/sessions",
                    headers={"Authorization": f"Bearer {new_session_token}", "Origin": origin},
                ).status_code
                == 200
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_recovery_and_rotation_fail_closed_on_unauthorized_attempts(tmp_path: Path):
    data_dir = tmp_path / "data"
    ui_root = tmp_path / "web"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html>owner UI</html>")
    (ui_root / "main.dart.js").write_text("// js")

    load_or_create_tokens(data_dir)
    broker = LocalOwnerSignInBroker(data_dir)
    server, thread = _start_web_server(ui_root, broker)
    port = server.server_port
    origin = f"http://127.0.0.1:{port}"

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

        # 1. Unconfirmed rotation fails closed (403)
        conn.request(
            "POST",
            "/__kinaiya/rotate-owner",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
            body=json.dumps({"authorization_proof": "fake", "confirm": False}),
        )
        res = conn.getresponse()
        assert res.status == 403
        data = json.loads(res.read().decode("utf-8"))
        assert "explicit confirmation" in data["detail"]

        # 2. Invalid authorization proof fails closed (403)
        conn.request(
            "POST",
            "/__kinaiya/recover-access",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
            body=json.dumps({"authorization_proof": "invalid_code_here"}),
        )
        res = conn.getresponse()
        assert res.status == 403
        data = json.loads(res.read().decode("utf-8"))
        assert data["code"] == "LocalSignInDenied"

        # 3. Wrong origin fails closed (403)
        conn.request(
            "POST",
            "/__kinaiya/recover-access",
            headers={
                "Origin": "http://evil.attacker.com",
                "Sec-Fetch-Site": "same-origin",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
            body=json.dumps({"authorization_proof": "anything"}),
        )
        res = conn.getresponse()
        _ = res.read()
        assert res.status == 403

        # 4. Missing CSRF header fails closed (403)
        conn.request(
            "POST",
            "/__kinaiya/recover-access",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
            },
            body=json.dumps({"authorization_proof": "anything"}),
        )
        res = conn.getresponse()
        _ = res.read()
        assert res.status == 403

        # 5. Cross-site Sec-Fetch-Site fails closed (403)
        conn.request(
            "POST",
            "/__kinaiya/recover-access",
            headers={
                "Origin": origin,
                "Sec-Fetch-Site": "cross-site",
                "X-Kinaiya-Local-Handoff": "1",
                "Content-Type": "application/json",
            },
            body=json.dumps({"authorization_proof": "anything"}),
        )
        res = conn.getresponse()
        _ = res.read()
        assert res.status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
