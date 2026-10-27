"""Run-local Meta admission proxy. Never persists credentials or prompt contents.

Forward only the authorized Contributor model, reserve at the more expensive Spark
rate and full catalog context before every request, and fail closed on missing usage.
Dollar values computed from tokens are estimates, not a provider billing receipt.
Run with the workspace Python; Muse --base-url points at this loopback listener.
"""

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "muse-spark-1.3-contributor"
LOCK = threading.Lock()
STATE = {}
LEDGER = Path(".dev-run/meta-budget.json")


def save():
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, indent=2) + "\n")
    tmp.replace(LEDGER)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def local_json(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/budget":
            with LOCK:
                self.local_json(200, STATE)
            return
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        if ".." in self.path or not self.path.startswith("/"):
            self.local_json(400, {"error": "Invalid path"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8_000_000:
            self.local_json(413, {"error": "Request too large"})
            return
        body = self.rfile.read(length) if length else None
        record = None
        if self.command == "POST":
            try:
                data = json.loads(body)
            except (ValueError, TypeError):
                self.local_json(400, {"error": "JSON required"})
                return
            if data.get("model") != MODEL:
                self.local_json(403, {"error": "This run authorizes Contributor only"})
                return
            data["max_output_tokens"] = min(int(data.get("max_output_tokens", 16000)), 16000)
            body = json.dumps(data).encode()
            # Catalog full context, uncached, at ordinary Spark rates. This covers
            # retained previous_response_id context and reasoning tokens too.
            reservation = (1_007_997 * 1.25 + 16000 * 4.25) / 1_000_000
            with LOCK:
                if STATE["estimated_usd"] + STATE["reserved_usd"] + reservation > 22:
                    self.local_json(402, {"error": "Run budget admission limit reached"})
                    return
                record = {
                    "id": len(STATE["calls"]) + 1,
                    "started_at": time.time(),
                    "path": self.path.split("?")[0],
                    "state": "in_flight",
                    "reservation_usd": reservation,
                    "model": MODEL,
                }
                STATE["reserved_usd"] += reservation
                STATE["calls"].append(record)
                save()

        # The sole upstream is pinned HTTPS. Do not follow redirects with auth.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None

        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}
        }
        headers["Accept-Encoding"] = "identity"
        request = urllib.request.Request(
            "https://api.meta.ai" + self.path, data=body, headers=headers, method=self.command
        )
        try:
            upstream = urllib.request.build_opener(NoRedirect).open(request, timeout=300)
        except urllib.error.HTTPError as exc:
            raw = exc.read(65536)
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            if record:
                with LOCK:
                    record["state"] = f"http_{exc.code}_reservation_retained"
                    save()
            return
        except Exception as exc:
            self.local_json(502, {"error": type(exc).__name__})
            if record:
                with LOCK:
                    record["state"] = "transport_error_reservation_retained"
                    save()
            return
        usage = None
        resolved_model = None
        self.send_response(upstream.status)
        for key in ["Content-Type", "X-Request-Id"]:
            if upstream.headers.get(key):
                self.send_header(key, upstream.headers[key])
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if "text/event-stream" in upstream.headers.get("Content-Type", ""):
                for line in upstream:
                    if line.startswith(b"data: "):
                        try:
                            event = json.loads(line[6:])
                            response = event.get("response", event)
                            usage = response.get("usage") or usage
                            resolved_model = response.get("model") or resolved_model
                        except ValueError:
                            pass
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        # Continue draining the single paid request for accounting.
                        pass
            else:
                raw = upstream.read(8_000_000)
                try:
                    response = json.loads(raw)
                    usage = response.get("usage")
                    resolved_model = response.get("model")
                except ValueError:
                    pass
                self.wfile.write(raw)
        finally:
            upstream.close()
            if record:
                with LOCK:
                    record["finished_at"] = time.time()
                    record["resolved_model"] = resolved_model
                    if usage and isinstance(usage, dict):
                        inputs = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
                        outputs = int(usage.get("output_tokens", usage.get("completion_tokens", 0)))
                        cached = int(
                            (
                                usage.get("input_tokens_details")
                                or usage.get("prompt_tokens_details")
                                or {}
                            ).get("cached_tokens", 0)
                        )
                        record["reported_usage"] = usage
                        # Keep a high estimate even when Contributor is discounted.
                        conservative = (inputs * 1.25 + outputs * 4.25) / 1_000_000
                        contributor = (
                            (inputs - cached) * 0.10 + cached * 0.002 + outputs * 0.20
                        ) / 1_000_000
                        record["contributor_estimated_usd"] = contributor
                        record["conservative_estimated_usd"] = conservative
                        STATE["estimated_usd"] += conservative
                        STATE["contributor_estimated_usd"] += contributor
                        STATE["reported_input_tokens"] += inputs
                        STATE["reported_output_tokens"] += outputs
                        STATE["reported_cached_tokens"] += cached
                        STATE["reserved_usd"] -= record["reservation_usd"]
                        record["state"] = "usage_reported"
                    else:
                        record["state"] = "usage_missing_reservation_retained"
                    save()
                    print(
                        json.dumps(
                            {
                                "event": "budget",
                                "call": record["id"],
                                "state": record["state"],
                                "estimated_usd": STATE["estimated_usd"],
                                "reserved_usd": STATE["reserved_usd"],
                                "contributor_estimated_usd": STATE["contributor_estimated_usd"],
                            }
                        ),
                        flush=True,
                    )


def main():
    global STATE, LEDGER
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9790)
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args()
    LEDGER = args.ledger
    STATE = (
        json.loads(LEDGER.read_text())
        if LEDGER.exists()
        else {
            "limit_usd": 25,
            "admission_limit_usd": 22,
            "billing_reported_usd": None,
            "estimated_usd": 0.0,
            "contributor_estimated_usd": 0.0,
            "reserved_usd": 0.0,
            "reported_input_tokens": 0,
            "reported_output_tokens": 0,
            "reported_cached_tokens": 0,
            "pricing_source": "Muse provider catalog 2026-10-26; conservative ordinary Spark reserve",
            "calls": [],
        }
    )
    save()
    print(f"Budget proxy listening on 127.0.0.1:{args.port}; admission ceiling $22", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
