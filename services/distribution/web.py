"""Explicit loopback web UI from verified matching-release assets; no Flutter subprocess."""

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
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
            "No matching prebuilt web assets. Developer path: install Flutter SDK, then flutter build web in apps/kinaiya_flutter; see docs/linux-installation.md"
        )
    return root


def asset_handler(root):
    root = root.resolve()

    class Assets(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def send_head(self):
            path = PurePosixPath(unquote(urlsplit(self.path).path))
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


def serve_web(layout):
    with operation_lock(layout.install / "runtime.lock", shared=True):
        receipt = current_receipt(layout)
        if receipt is None:
            raise InstallError("Managed release required; use the explicit Flutter developer path")
        source = (layout.install / "current").resolve() / receipt["source"]
        root = web_root(source)
        with ThreadingHTTPServer(("127.0.0.1", 8780), asset_handler(root)) as server:
            print(
                json.dumps(
                    {
                        "web": "http://127.0.0.1:8780",
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
