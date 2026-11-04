"""Explicit loopback web UI from verified matching-release assets; no Flutter subprocess."""

import io
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from services.distribution.installation import InstallError, current_receipt, operation_lock


def web_root(source):
    root = source / "apps/kinaiya_flutter/build/web"
    if (
        root.is_symlink()
        or not (root / "index.html").is_file()
        or not (root / "main.dart.js").is_file()
    ):
        raise InstallError(
            "No matching prebuilt web assets. Install a release bundle (kinaiya install) or developer path: install Flutter SDK, then flutter build web in apps/kinaiya_flutter; see docs/linux-installation.md"
        )
    return root


def web_bundle_dir(layout, *, fallback_source: Path | None = None) -> Path:
    """Resolve and verify the matching-release Flutter web bundle directory."""
    if (layout.install / "layout.json").exists():
        receipt = current_receipt(layout)
        if receipt is None:
            raise InstallError(
                "No active managed release. Install a release bundle ('kinaiya install') or build apps/kinaiya_flutter/build/web with 'flutter build web'."
            )
        source = (layout.install / "current").resolve() / receipt["source"]
        return web_root(source)
    return web_root(fallback_source if fallback_source is not None else layout.install)


def asset_handler(root):
    root = root.resolve()

    class Assets(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def send_head(self):
            path = PurePosixPath(unquote(urlsplit(self.path).path))
            if path == PurePosixPath("/health"):
                payload = b'{"service":"web","status":"ready"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                return io.BytesIO(payload)
            if (
                ".." in path.parts
                or "\\" in str(path)
                or any(part.startswith(".") for part in path.parts if part != "/")
            ):
                self.send_error(404)
                return None
            candidate = root.joinpath(*[part for part in path.parts if part != "/"])
            if not candidate.resolve().is_relative_to(root):
                self.send_error(404)
                return None
            return super().send_head()

        def list_directory(self, path):
            self.send_error(404)
            return None

        def log_message(self, *args):
            # URL/query strings may contain owner material. Do not log requests.
            pass

    return Assets


def serve_web(layout, *, port: int = 8780):
    if type(port) is not int or not 1 <= port <= 65535:
        raise InstallError("Web loopback port must be an integer between 1 and 65535")
    with operation_lock(layout.install / "runtime.lock", shared=True):
        receipt = current_receipt(layout)
        if receipt is None:
            raise InstallError("Managed release required; use the explicit Flutter developer path")
        source = (layout.install / "current").resolve() / receipt["source"]
        root = web_root(source)
        with ThreadingHTTPServer(("127.0.0.1", port), asset_handler(root)) as server:
            print(
                json.dumps(
                    {
                        "web": f"http://127.0.0.1:{port}",
                        "source_sha": receipt["release"]["source_sha"],
                        "owner_login": "kinaiya owner-login exports to a private local file; never share with companions",
                    }
                ),
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
