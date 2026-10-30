"""Run-local Meta admission proxy. Never persists credentials or prompt contents.

Forward authorized Contributor text, free exact counts and bounded Standard
buffered voice. Reserve future text context/output at twice uncached Contributor
rates and voice at its full admissible minute. Preserve uncertain reservations.
Dollar values computed from tokens are estimates, not a provider billing receipt.
Run with the workspace Python; Muse --base-url points at this loopback listener.
"""

import argparse
import email.policy
import fcntl
import io
import json
import math
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from contextlib import contextmanager
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "muse-spark-1.3-contributor"
OUTPUT_CAP = 65536
LOCK = threading.Lock()
STATE = {}
LEDGER = Path(".dev-run/meta-budget.json")
MAX_TOOL_NODES = 2048
MAX_TOOL_DEPTH = 8
VOICE_MODEL = "muse-voice-transcribe-1.0"
VOICE_USD_PER_SECOND = 0.18 / 3600
MAX_AUX_REQUESTS = 10000
MAX_VOICE_BODY = 3_000_000


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON field")
            value[key] = item
        return value

    def invalid(value):
        raise ValueError("Non-finite JSON")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid JSON") from exc


def voice_request(body, content_type):
    """Validate exactly one bounded buffered WAV, never an arbitrary billable route."""
    if (
        not body
        or len(body) > MAX_VOICE_BODY
        or len(content_type) > 256
        or "\r" in content_type
        or "\n" in content_type
    ):
        raise ValueError("Invalid bounded voice request")
    message = BytesParser(policy=email.policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if (
        message.get_content_type() != "multipart/form-data"
        or not message.is_multipart()
        or message.defects
        or len(message.get_params()) != 2
        or not message.get_boundary()
        or not 1 <= len(message.get_boundary()) <= 70
    ):
        raise ValueError("Voice requires multipart form data")
    parts = list(message.iter_parts())
    if len(parts) != 2:
        raise ValueError("Voice requires exactly request and audio")
    fields = {}
    for part in parts:
        name = part.get_param("name", header="content-disposition")
        header_names = [key.lower() for key in part.keys()]
        if (
            part.is_multipart()
            or part.defects
            or name not in {"request", "audio"}
            or name in fields
            or part.get_content_disposition() != "form-data"
            or part.get("Content-Transfer-Encoding") is not None
            or len(set(header_names)) != len(header_names)
            or set(header_names) != {"content-type", "content-disposition"}
            or part.get_content_type() != ("application/json" if name == "request" else "audio/wav")
        ):
            raise ValueError("Invalid voice part")
        fields[name] = part.get_payload(decode=True)
    if len(fields["request"]) > 1024 or strict_json(fields["request"]) != {
        "model": VOICE_MODEL,
        "mode": "PUSH_TO_TALK",
        "audioEncoding": "WAV",
    }:
        raise ValueError("Only the selected buffered voice model is budgeted")
    audio = fields["audio"]
    if (
        len(audio) < 44
        or audio[:4] != b"RIFF"
        or audio[8:12] != b"WAVE"
        or int.from_bytes(audio[4:8], "little") + 8 != len(audio)
    ):
        raise ValueError("Actual PCM WAV is required")
    with wave.open(io.BytesIO(audio), "rb") as wav:
        frames, rate = wav.getnframes(), wav.getframerate()
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getcomptype() != "NONE"
            or rate not in {16000, 24000}
            or not 1 <= frames <= 60 * rate
            or len(wav.readframes(frames + 1)) != frames * 2
        ):
            raise ValueError("Only mono PCM16 WAV up to 60 seconds is budgeted")
    return (frames + rate - 1) // rate


def audio_state(state):
    """Add a separate subledger without altering any legacy text charge/reserve."""
    calls = state.setdefault("audio_calls", [])
    if type(calls) is not list or len(calls) > MAX_AUX_REQUESTS:
        raise ValueError("Invalid audio ledger")
    settled = unsettled = 0.0
    for row in calls:
        reservation = money(row["reservation_usd"])
        if row["state"] == "duration_reported":
            settled += money(row["estimated_usd"])
        else:
            unsettled += reservation
    state["audio_estimated_usd"] = max(money(state.get("audio_estimated_usd", 0)), settled)
    state["audio_reserved_usd"] = max(money(state.get("audio_reserved_usd", 0)), unsettled)
    state["audio_limit_usd"] = 3
    count = state.setdefault("count_requests", 0)
    if type(count) is not int or not 0 <= count <= MAX_AUX_REQUESTS:
        raise ValueError("Invalid count audit counter")


def admission_available(*, text=0, audio=0):
    audio_state(STATE)
    text_spent = money(
        STATE.get("cache_aware_admission_estimated_usd", STATE["admission_estimated_usd"])
    )
    billed = STATE.get("billing_reported_usd")
    billed = 0 if billed is None else money(billed)
    reserved = money(STATE["reserved_usd"])
    audio_spent, audio_reserved = STATE["audio_estimated_usd"], STATE["audio_reserved_usd"]
    return (
        (not text or max(text_spent, billed) + reserved + text <= 22)
        and audio_spent + audio_reserved + audio <= 3
        and max(text_spent + audio_spent, billed) + reserved + audio_reserved + text + audio
        <= min(25, money(STATE["limit_usd"]))
    )


def local_function_tools(tools):
    """Accept bounded local function groups, never hosted billable tools.

    Muse groups its ordinary functions in namespace.tools. Only structural
    tool declarations are examined; function parameter schemas are data.
    """
    if not isinstance(tools, list):
        return False
    pending = [(tools, 0)]
    seen = 0
    while pending:
        group, depth = pending.pop()
        if depth > MAX_TOOL_DEPTH:
            return False
        for tool in group:
            seen += 1
            if seen > MAX_TOOL_NODES or not isinstance(tool, dict):
                return False
            kind = tool.get("type")
            if kind == "function":
                continue
            if kind != "namespace" or not isinstance(tool.get("tools"), list):
                return False
            pending.append((tool["tools"], depth + 1))
    return True


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
    """Preserve history while crediting only verified, settled Contributor cache.

    The old uncached estimate is retained verbatim as a separate audit floor.
    Its unexplained aggregate/per-call premiums are never discounted. Neither
    real billing nor an unresolved request reservation is a cache estimate.
    """
    known = cache_aware = unsettled = newly_uncertain = 0.0
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
            if (
                record["state"] == "usage_reported"
                and record.get("model") == MODEL
                and record.get("resolved_model") == MODEL
            ):
                premium = max(0.0, record["admission_estimated_usd"] - costs["admission"])
                cache_aware += 2 * costs["contributor"] + premium
            else:
                # Legacy/mismatched model identity cannot justify a discount.
                cache_aware += record["admission_estimated_usd"]
            if record["state"] != "usage_reported":
                unsettled += reservation
    state["reserved_usd"] = max(money(state["reserved_usd"]), unsettled) + newly_uncertain
    state["admission_estimated_usd"] = max(money(state.get("admission_estimated_usd", 0)), known)
    unexplained = max(0.0, state["admission_estimated_usd"] - known)
    state["cache_aware_admission_estimated_usd"] = max(
        money(state.get("cache_aware_admission_estimated_usd", 0)), cache_aware + unexplained
    )
    state["admission_basis"] = "verified_contributor_cache_v1"
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
        # Rejections can precede body consumption. Never parse a rejected body
        # as the next HTTP request on a persistent connection.
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/budget":
            with LOCK:
                self.local_json(200, STATE)
            return
        if self.path in {"/v1/models", "/muse-code/models"}:
            self.forward()
            return
        self.local_json(405, {"error": "Only model discovery and budget reads are supported"})

    def do_POST(self):
        if self.path in {"/v1/responses/input_tokens", "/v1/asr/transcribe"}:
            self.forward_auxiliary(voice=self.path == "/v1/asr/transcribe")
            return
        if self.path != "/v1/responses":
            self.local_json(405, {"error": "Only the reserved Responses route is supported"})
            return
        self.forward()

    def bounded_body(self, limit):
        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get("Transfer-Encoding")
            or len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
        ):
            raise ValueError("A single bounded Content-Length is required")
        length = int(lengths[0])
        if not 0 < length <= limit:
            raise ValueError("Request exceeds its bound")
        self.connection.settimeout(10)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("Incomplete request")
        return body

    def forward_auxiliary(self, *, voice):
        record = None
        try:
            body = self.bounded_body(MAX_VOICE_BODY if voice else 8_000_000)
            if voice:
                seconds = voice_request(body, self.headers.get("Content-Type", ""))
            else:
                data = strict_json(body)
                if (
                    type(data) is not dict
                    or data.get("model") != MODEL
                    or not local_function_tools(data.get("tools", []))
                    or type(data.get("max_output_tokens")) is not int
                    or not 1 <= data["max_output_tokens"] <= OUTPUT_CAP
                ):
                    raise ValueError("Invalid Contributor count request")
        except (ValueError, UnicodeError, wave.Error, EOFError, OSError):
            self.local_json(400, {"error": "Invalid bounded auxiliary request"})
            return
        with LOCK:
            audio_state(STATE)
            if voice:
                # Reserve a full admissible minute, even for shorter clips.
                # Timeout, partial processing or a lost duration never refunds it.
                reservation = 60 * VOICE_USD_PER_SECOND
                if len(STATE["audio_calls"]) >= MAX_AUX_REQUESTS or not admission_available(
                    audio=reservation
                ):
                    self.local_json(
                        402, {"error": "Run audio/combined budget admission limit reached"}
                    )
                    return
                record = {
                    "id": len(STATE["audio_calls"]) + 1,
                    "path": self.path,
                    "model": VOICE_MODEL,
                    "state": "in_flight",
                    "started_at": time.time(),
                    "uploaded_seconds_ceil": seconds,
                    "reservation_usd": reservation,
                }
                STATE["audio_calls"].append(record)
                STATE["audio_reserved_usd"] += reservation
            else:
                if STATE["count_requests"] >= MAX_AUX_REQUESTS:
                    self.local_json(429, {"error": "Count request audit bound reached"})
                    return
                STATE["count_requests"] += 1
            save()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Authorization": self.headers.get("Authorization", ""),
        }
        if not voice:
            headers["x-tbh-contributor-mode"] = "true"
        request = urllib.request.Request(
            "https://api.meta.ai" + self.path, data=body, headers=headers, method="POST"
        )
        upstream, valid_duration, output = None, None, None
        status, failure = 502, "transport_error_reservation_retained"
        try:
            upstream = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect
            ).open(request, timeout=120)
            status = upstream.status
            if (
                status != 200
                or upstream.headers.get("Content-Encoding", "identity").lower() != "identity"
                or upstream.headers.get("Content-Type", "").split(";")[0].lower()
                != "application/json"
            ):
                raise ValueError("Invalid auxiliary response")
            limit = 128 * 1024 if voice else 4096
            output = upstream.read(limit + 1)
            if len(output) > limit:
                raise ValueError("Auxiliary response exceeds bound")
            value = strict_json(output)
            if voice:
                if (
                    type(value) is not dict
                    or set(value) != {"sessionId", "transcript", "audioDurationMs", "turns"}
                    or type(value["sessionId"]) is not str
                    or not 1 <= len(value["sessionId"].encode("utf-8")) <= 256
                    or type(value["transcript"]) is not str
                    or len(value["transcript"]) > 16000
                    or len(value["transcript"].encode("utf-8")) > 64000
                    or type(value["audioDurationMs"]) is not int
                    or not 0 <= value["audioDurationMs"] <= 60000
                    or value["turns"] != []
                ):
                    raise ValueError("Invalid voice duration response")
                # Never discount a complete uploaded clip because the response
                # reports a smaller duration; round upward conservatively.
                valid_duration = max(seconds, (value["audioDurationMs"] + 999) // 1000)
            elif (
                type(value) is not dict
                or set(value) != {"object", "input_tokens"}
                or value["object"] != "response.input_tokens"
                or type(value["input_tokens"]) is not int
                or not 1 <= value["input_tokens"] <= 2**53 - 1
            ):
                raise ValueError("Invalid count response")
        except urllib.error.HTTPError as exc:
            status, failure, output = exc.code, f"http_{exc.code}_reservation_retained", None
            exc.close()
        except Exception:
            status, output = 502, None
        finally:
            try:
                if upstream is not None:
                    upstream.close()
            except Exception:
                output, valid_duration, status = None, None, 502
            if record is not None:
                with LOCK:
                    record["finished_at"] = time.time()
                    if output is not None and valid_duration is not None:
                        record.update(
                            state="duration_reported",
                            processed_seconds_ceil=valid_duration,
                            estimated_usd=valid_duration * VOICE_USD_PER_SECOND,
                        )
                        STATE["audio_estimated_usd"] += record["estimated_usd"]
                        STATE["audio_reserved_usd"] -= record["reservation_usd"]
                    else:
                        record["state"] = failure
                    audio_state(STATE)
                    save()
        if output is None:
            self.local_json(status, {"error": "Auxiliary provider request unavailable"})
        else:
            # Accounting is committed before delivery; a disconnected client
            # cannot cause a second upload or erase an incurred audio charge.
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(output)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(output)

    def forward(self):
        if ".." in self.path or not self.path.startswith("/"):
            self.local_json(400, {"error": "Invalid path"})
            return
        try:
            body = self.bounded_body(8_000_000) if self.command == "POST" else None
        except (ValueError, OSError):
            self.local_json(400, {"error": "Invalid bounded request"})
            return
        record = None
        if self.command == "POST":
            try:
                data = strict_json(body)
            except (ValueError, TypeError):
                self.local_json(400, {"error": "JSON required"})
                return
            if not isinstance(data, dict):
                self.local_json(400, {"error": "JSON object required"})
                return
            if data.get("model") != MODEL:
                self.local_json(403, {"error": "This run authorizes Contributor only"})
                return
            tools = data.get("tools", [])
            if not local_function_tools(tools):
                # Hosted search has additional per-query fees outside token
                # usage. Muse's local function tools do not incur those fees.
                self.local_json(403, {"error": "Only local function tools are budgeted"})
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
                if not admission_available(text=reservation):
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
            upstream = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect
            ).open(request, timeout=300)
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
                    reconcile(STATE)
                    save()
                    print(
                        json.dumps(
                            {
                                "event": "budget",
                                "call": record["id"],
                                "state": record["state"],
                                "admission_estimated_usd": STATE["admission_estimated_usd"],
                                "cache_aware_admission_estimated_usd": STATE[
                                    "cache_aware_admission_estimated_usd"
                                ],
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
        audio_state(STATE)
        STATE["admission_policy"] = (
            "2x verified Contributor cached/uncached settled cost; full uncached-context "
            "future reservations; uncertain reserves retained; text $22/audio $3/combined $25"
        )
        STATE["pricing_source"] = (
            "https://dev.meta.ai/docs/pricing-rate-limits; "
            "Contributor input 0.10/output 0.20/cached 0.002 USD per million; "
            "Standard voice transcribe 0.18 USD/hour, conservative whole seconds"
        )
        save()
        print(
            f"Budget proxy listening on 127.0.0.1:{args.port}; text $22/audio $3/combined $25",
            flush=True,
        )
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
