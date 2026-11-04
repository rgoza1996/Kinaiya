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
