"""Release-matched web assets and a loopback-only static owner UI entrypoint."""

import http.client
import importlib
import threading
from http.server import ThreadingHTTPServer

import pytest
from test_linux_distribution import distribution


def test_static_ui_never_serves_parent_files_or_directory_listing(tmp_path):
    try:
        web = importlib.import_module("services.distribution.web")
    except ModuleNotFoundError:
        pytest.fail("explicit managed web integration missing")
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("compiled owner UI")
    (tmp_path / "service-tokens.json").write_text("never serve this")
    (root / "empty").mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.asset_handler(root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200 and response.read() == b"compiled owner UI"
        for path in (
            "/../service-tokens.json",
            "/%2e%2e/service-tokens.json",
            "/empty/",
            "/.hidden",
        ):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 404
            assert b"never serve this" not in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_without_prebuilt_assets_refuses_without_build_or_download(tmp_path):
    try:
        web = importlib.import_module("services.distribution.web")
    except ModuleNotFoundError:
        pytest.fail("explicit managed web integration missing")
    with pytest.raises(distribution().InstallError, match="Flutter|prebuilt"):
        web.web_root(tmp_path)
