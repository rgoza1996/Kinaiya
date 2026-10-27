"""Run-local Meta admission proxy. Never persists credentials or prompt contents.

Forward only the authorized Contributor model. Reserve full context and bounded
output at twice the verified Contributor rates, ignoring cache discounts for
admission. Keep the ordinary-Spark comparison separately. Fail closed on missing usage.
Dollar values computed from tokens are estimates, not a provider billing receipt.
Run with the workspace Python; Muse --base-url points at this loopback listener.
"""

import argparse
import fcntl
import json
import math
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "muse-spark-1.3-contributor"
OUTPUT_CAP = 65536
LOCK = threading.Lock()
STATE = {}
LEDGER = Path(".dev-run/meta-budget.json")


def measured_usage(usage):
    """Normalize supported token fields; incomplete/invalid usage is uncertain."""
    if not isinstance(usage, dict):
        raise ValueError("Missing usage")

    def counter(primary, alias):
        values = [usage[k] for k in (primary, alias) if k in usage]
        if not values or any(type(v) is not int or v < 0 for v in values):
            raise ValueError("Invalid token counter")
        if len(set(values)) != 1:
            raise ValueError("Conflicting token counters")
        return values[0]

    inputs = counter("input_tokens", "prompt_tokens")
    outputs = counter("output_tokens", "completion_tokens")
    cache_counts = []
    for key in ("input_tokens_details", "prompt_tokens_details"):
        if key not in usage:
            continue
        details = usage[key]
        if not isinstance(details, dict):
            raise ValueError("Invalid cache details")
        cached = details.get("cached_tokens", 0)
        if type(cached) is not int or not 0 <= cached <= inputs:
            raise ValueError("Invalid cached-token counter")
        cache_counts.append(cached)
    if len(set(cache_counts)) > 1:
        raise ValueError("Conflicting cached-token counters")
    cached = cache_counts[0] if cache_counts else 0
    normalized = {
        "input_tokens": inputs,
        "output_tokens": outputs,
        "input_tokens_details": {"cached_tokens": cached},
    }
    return normalized, {
        "contributor": ((inputs - cached) * 0.10 + cached * 0.002 + outputs * 0.20) / 1_000_000,
        "admission": (inputs * 0.20 + outputs * 0.40) / 1_000_000,
        "comparison": (inputs * 1.25 + outputs * 4.25) / 1_000_000,
    }


def money(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid budget counter")
    return value


def reconcile(state):
    """Migration never refunds an earlier charge or unresolved reservation."""
    known = unsettled = newly_uncertain = 0.0
    for record in state["calls"]:
        reservation = money(record["reservation_usd"])
        try:
            normalized, costs = measured_usage(record.get("reported_usage"))
        except ValueError:
            if record["state"] == "usage_reported":
                newly_uncertain += reservation
                record["state"] = "usage_uncertain_reservation_retained"
            else:
                unsettled += reservation
        else:
            record["reported_usage"] = normalized
            record["admission_estimated_usd"] = max(
                money(record.get("admission_estimated_usd", 0)), costs["admission"]
            )
            known += record["admission_estimated_usd"]
            if record["state"] != "usage_reported":
                unsettled += reservation
    state["reserved_usd"] = max(money(state["reserved_usd"]), unsettled) + newly_uncertain
    state["admission_estimated_usd"] = max(money(state.get("admission_estimated_usd", 0)), known)
    state["ordinary_spark_comparison_usd"] = money(state["estimated_usd"])
    return state


@contextmanager
def ledger_owner(path):
    """An OS lock prevents duplicate gateways from independently spending one ledger."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("Unsafe budget lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def save():
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".budget-", dir=LEDGER.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(STATE, indent=2, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, LEDGER)
    finally:
        if os.path.exists(name):
            os.unlink(name)


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
        if self.path == "/v1/models":
            self.forward()
            return
        self.local_json(405, {"error": "Only model discovery and budget reads are supported"})

    def do_POST(self):
        if self.path != "/v1/responses":
            self.local_json(405, {"error": "Only the reserved Responses route is supported"})
            return
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
            if not isinstance(data, dict):
                self.local_json(400, {"error": "JSON object required"})
                return
            if data.get("model") != MODEL:
                self.local_json(403, {"error": "This run authorizes Contributor only"})
                return
            output = data.get("max_output_tokens", OUTPUT_CAP)
            if type(output) is not int or output <= 0:
                self.local_json(400, {"error": "Positive integer output bound required"})
                return
            data["max_output_tokens"] = min(output, OUTPUT_CAP)
            body = json.dumps(data).encode()
            # Full catalog context, uncached, twice verified Contributor rates.
            # Includes retained response context and reasoning output.
            reservation = (1_007_997 * 0.20 + OUTPUT_CAP * 0.40) / 1_000_000
            with LOCK:
                billed = STATE.get("billing_reported_usd")
                spent = max(
                    STATE["admission_estimated_usd"], 0 if billed is None else money(billed)
                )
                if spent + STATE["reserved_usd"] + reservation > 22:
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
                    try:
                        normalized, costs = measured_usage(usage)
                    except ValueError:
                        normalized = None
                    if normalized is not None:
                        inputs = normalized["input_tokens"]
                        outputs = normalized["output_tokens"]
                        cached = normalized["input_tokens_details"]["cached_tokens"]
                        record["reported_usage"] = normalized
                        # Keep a high estimate even when Contributor is discounted.
                        conservative = costs["comparison"]
                        contributor = costs["contributor"]
                        record["contributor_estimated_usd"] = contributor
                        record["conservative_estimated_usd"] = conservative
                        STATE["estimated_usd"] += conservative
                        STATE["ordinary_spark_comparison_usd"] = STATE["estimated_usd"]
                        admission = costs["admission"]
                        record["admission_estimated_usd"] = admission
                        STATE["admission_estimated_usd"] += admission
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
                                "admission_estimated_usd": STATE["admission_estimated_usd"],
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
    with ledger_owner(LEDGER):
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
        reconcile(STATE)
        STATE["admission_policy"] = (
            "2x Contributor catalog rates; uncached input; full-context reservations; $3 buffer"
        )
        STATE["pricing_source"] = (
            "Muse provider catalog 2026-10-26; Contributor input 0.10/output 0.20/cached 0.002 USD per million"
        )
        save()
        print(f"Budget proxy listening on 127.0.0.1:{args.port}; admission ceiling $22", flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
