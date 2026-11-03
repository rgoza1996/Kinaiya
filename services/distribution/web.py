"""Explicit loopback web UI from verified matching-release assets; no Flutter subprocess."""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from services.common.owner_signin import (
    HANDOFF_COOKIE_NAME,
    HANDOFF_PATHS,
    LocalOwnerSignInBroker,
    LocalSignInError,
    parse_handoff_cookie,
)
from services.distribution.installation import InstallError, current_receipt, operation_lock

FORBIDDEN_ASSET_NAMES = frozenset(
    {
        "service-tokens.json",
        "local-owner-handoff.json",
        "local-owner-sessions.json",
        "owner-login.txt",
    }
)


def web_root(source):
    root = source / "apps/kinaiya_flutter/build/web"
    if (
        root.is_symlink()
        or not (root / "index.html").is_file()
        or not (root / "main.dart.js").is_file()
    ):
        raise InstallError(
            "No matching prebuilt web assets. Developer path: install Flutter SDK, then flutter build web in apps/kinaiya_flutter; see docs/linux-installation.md"
        )
    return root


def asset_handler(root, *, broker: LocalOwnerSignInBroker | None = None):
    root = root.resolve()

    class Assets(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            self._pending_headers: list[tuple[str, str]] = []
            super().__init__(*args, directory=str(root), **kwargs)

        def end_headers(self):
            for header_name, header_val in getattr(self, "_pending_headers", ()):
                self.send_header(header_name, header_val)
            self._pending_headers = []
            super().end_headers()

        def _write_json(
            self,
            status_code: int,
            payload: dict[str, Any],
            *,
            extra_headers: tuple[tuple[str, str], ...] = (),
        ) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in extra_headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            parsed_url = urlsplit(self.path)
            raw_path = unquote(parsed_url.path)
            if raw_path not in HANDOFF_PATHS:
                self._write_json(404, {"detail": "Not found"})
                return
            if parsed_url.query or parsed_url.fragment:
                self._write_json(
                    403, {"detail": "Query parameters are not permitted on local sign-in"}
                )
                return
            try:
                content_len = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._write_json(400, {"detail": "Invalid Content-Length"})
                return
            if content_len < 0 or content_len > 4096:
                self._write_json(413, {"detail": "Local sign-in request body too large"})
                return
            if content_len > 0:
                _ = self.rfile.read(content_len)

            if broker is None:
                self._write_json(403, {"detail": "Local sign-in handoff is not configured"})
                return

            client_host = self.client_address[0] if self.client_address else None
            origin = self.headers.get("Origin")
            sec_fetch_site = self.headers.get("Sec-Fetch-Site")
            csrf_header = self.headers.get("X-Kinaiya-Local-Handoff") or self.headers.get(
                "X-Kinaiya-Local-UI"
            )
            cookie_seal = parse_handoff_cookie(self.headers.get("Cookie"))
            server_port = getattr(self.server, "server_port", None)
            clear_cookie = (
                f"{HANDOFF_COOKIE_NAME}=; HttpOnly; SameSite=Strict; "
                "Path=/__kinaiya/local-signin; Max-Age=0"
            )
            try:
                result = broker.consume_handoff(
                    cookie_seal=cookie_seal,
                    origin=origin,
                    csrf_header=csrf_header,
                    client_host=client_host,
                    sec_fetch_site=sec_fetch_site,
                    server_port=server_port,
                )
            except LocalSignInError as exc:
                self._write_json(
                    exc.status_code,
                    {"detail": str(exc)},
                    extra_headers=(("Set-Cookie", clear_cookie),),
                )
                return

            self._write_json(
                200,
                result,
                extra_headers=(("Set-Cookie", clear_cookie),),
            )

        def send_head(self):
            parsed = urlsplit(self.path)
            raw_path = unquote(parsed.path)
            if raw_path in HANDOFF_PATHS:
                self.send_error(405)
                return None
            path = PurePosixPath(raw_path)
            if (
                ".." in path.parts
                or "\\" in str(path)
                or any(part.startswith(".") for part in path.parts if part != "/")
                or any(part in FORBIDDEN_ASSET_NAMES for part in path.parts)
            ):
                self.send_error(404)
                return None
            candidate = root.joinpath(*[part for part in path.parts if part != "/"])
            if not candidate.resolve().is_relative_to(root):
                self.send_error(404)
                return None
            if broker is not None and raw_path in {"/", "/index.html"} and not parsed.query:
                client_host = self.client_address[0] if self.client_address else None
                set_cookie = broker.seal_initial_launch_cookie(
                    client_host=client_host,
                    origin=self.headers.get("Origin"),
                    sec_fetch_site=self.headers.get("Sec-Fetch-Site"),
                    server_port=getattr(self.server, "server_port", None),
                )
                if set_cookie is not None:
                    self._pending_headers.append(("Set-Cookie", set_cookie))
                    self._pending_headers.append(("Cache-Control", "no-store"))
                    self._pending_headers.append(("Referrer-Policy", "no-referrer"))
            return super().send_head()

        def list_directory(self, path):
            self.send_error(404)
            return None

        def log_message(self, *args):
            # URL/query strings may contain owner material. Do not log requests.
            pass

    return Assets


def serve_web(layout):
    with operation_lock(layout.install / "runtime.lock", shared=True):
        receipt = current_receipt(layout)
        if receipt is None:
            raise InstallError("Managed release required; use the explicit Flutter developer path")
        source = (layout.install / "current").resolve() / receipt["source"]
        root = web_root(source)
        broker = LocalOwnerSignInBroker(
            layout.data,
            allowed_origins=("http://127.0.0.1:8780", "http://localhost:8780"),
        )
        with ThreadingHTTPServer(("127.0.0.1", 8780), asset_handler(root, broker=broker)) as server:
            print(
                json.dumps(
                    {
                        "web": "http://127.0.0.1:8780",
                        "source_sha": receipt["release"]["source_sha"],
                        "host_generation": broker.host_generation,
                        "owner_epoch": broker.owner_epoch,
                        "owner_login": "kinaiya owner-login exports to a private local file; never share with companions",
                    }
                ),
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
