"""The first-party voice bridge must not export internal service credentials."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from packages.providers.speech import SpeechClient


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid",
        "http://example.invalid",
        "http://127.0.0.1.example.invalid",
        "http://owner:secret@127.0.0.1:8765",
        "http://127.0.0.1:8765/v1",
        "http://127.0.0.1:8765?token=synthetic",
        "http://127.0.0.1:8765#fragment",
        "file:///tmp/voice",
        "//example.invalid/voice",
    ],
)
def test_internal_voice_token_requires_a_local_origin(url):
    with pytest.raises(ValueError, match="loopback"):
        SpeechClient(url, "synthetic-service-token")


async def test_voice_redirect_never_receives_service_auth_or_audio():
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://example.invalid/voice"})

    client = SpeechClient(
        "http://127.0.0.1:8765", "synthetic-service-token", transport=httpx.MockTransport(redirect)
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.transcribe(b"public synthetic audio")
        assert len(requests) == 1
        assert requests[0].url.host == "127.0.0.1"
        assert requests[0].headers["Authorization"] == "Bearer synthetic-service-token"
    finally:
        await client.close()


async def test_local_voice_does_not_use_ambient_proxy(monkeypatch):
    class Voice(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/v1/audio/transcriptions"
            self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.dumps({"text": "public synthetic transcript"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    monkeypatch.setenv("HTTP_PROXY", "invalid-scheme://example.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "invalid-scheme://example.invalid")
    monkeypatch.setenv("ALL_PROXY", "invalid-scheme://example.invalid")
    monkeypatch.setenv("NO_PROXY", "")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Voice)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = SpeechClient(f"http://127.0.0.1:{server.server_port}", "synthetic-service-token")
    try:
        result = await client.transcribe(b"public synthetic audio")
        assert result["text"] == "public synthetic transcript"
    finally:
        await client.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
