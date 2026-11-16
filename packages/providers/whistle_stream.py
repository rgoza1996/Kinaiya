"""Bounded untrusted native stream parser; no native load, no I/O.

Single-thread/process-global ownership: the isolated child serializes all
native stream calls. There is no lock, no handle, no start/abort.
"""

import math

from packages.providers.whistle_ipc import decode_json

MAX_RESPONSE_BYTES = 128 * 1024
MAX_TEXT_CHARS = 16000
MAX_AGG_UTF8 = 64 * 1024
MAX_WORDS = 4096
MAX_WORD_CHARS = 256
MAX_PASS_MS = 900000
SAMPLE_RATE = 16000
MAX_CHUNK_SAMPLES = 16000
MAX_TOTAL_SAMPLES = 960000
SUPPORTED_LANGUAGES = frozenset({"en", "de", "fr", "es", "it", "nl", "pl"})
NATIVE_KEYS = frozenset({"text", "words", "pending", "language", "received", "pass_ms"})
TOL = 1.0 / SAMPLE_RATE


def _is_num(x):
    try:
        return type(x) in (int, float) and math.isfinite(x)
    except OverflowError:
        return False


def parse_native_result(raw, *, total_samples, prev_committed, return_code, is_final):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RESPONSE_BYTES - 1:
        raise ValueError("Invalid bounded Whistle stream result")
    result = decode_json(raw, MAX_RESPONSE_BYTES)
    if not isinstance(result, dict) or set(result) != NATIVE_KEYS:
        raise ValueError("Invalid bounded Whistle stream result")
    text = result["text"]
    pending = result["pending"]
    language = result["language"]
    received = result["received"]
    pass_ms = result["pass_ms"]
    words = result["words"]
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise ValueError("Invalid bounded Whistle stream result")
    if not isinstance(pending, str) or len(pending) > MAX_TEXT_CHARS:
        raise ValueError("Invalid bounded Whistle stream result")
    try:
        text.encode("utf-8")
        pending.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid bounded Whistle stream result") from None
    if not (
        language is None
        or (isinstance(language, str) and (language == "" or language in SUPPORTED_LANGUAGES))
    ):
        raise ValueError("Invalid bounded Whistle stream result")
    if type(received) is bool or not _is_num(received) or received < 0:
        raise ValueError("Invalid bounded Whistle stream result")
    expected = total_samples / SAMPLE_RATE
    if abs(float(received) - expected) > TOL:
        raise ValueError("Invalid bounded Whistle stream result")
    if type(pass_ms) is bool or not _is_num(pass_ms) or not 0 <= float(pass_ms) <= MAX_PASS_MS:
        raise ValueError("Invalid bounded Whistle stream result")
    if not isinstance(words, list) or len(words) > MAX_WORDS:
        raise ValueError("Invalid bounded Whistle stream result")
    for w in words:
        if not isinstance(w, dict) or set(w) != {"word", "start", "end", "probability"}:
            raise ValueError("Invalid bounded Whistle stream result")
        ws, st, en, pr = w["word"], w["start"], w["end"], w["probability"]
        if not isinstance(ws, str) or len(ws) > MAX_WORD_CHARS:
            raise ValueError("Invalid bounded Whistle stream result")
        try:
            ws.encode("utf-8")
        except UnicodeError:
            raise ValueError("Invalid bounded Whistle stream result") from None
        if type(st) is bool or type(en) is bool or type(pr) is bool:
            raise ValueError("Invalid bounded Whistle stream result")
        if not _is_num(st) or not _is_num(en) or not _is_num(pr):
            raise ValueError("Invalid bounded Whistle stream result")
        st, en, pr = float(st), float(en), float(pr)
        if st < 0 or en < 0:
            raise ValueError("Invalid bounded Whistle stream result")
        if st > en:
            raise ValueError("Invalid bounded Whistle stream result")
        if en > float(received) + TOL:
            raise ValueError("Invalid bounded Whistle stream result")
        if not 0.0 <= pr <= 1.0:
            raise ValueError("Invalid bounded Whistle stream result")
    if type(return_code) is not int or return_code < 0:
        raise ValueError("Invalid bounded Whistle stream result")
    if not is_final and return_code != len(words):
        raise ValueError("Invalid bounded Whistle stream result")
    if not isinstance(prev_committed, str):
        raise ValueError("Invalid bounded Whistle stream result")
    if prev_committed and text:
        candidate = prev_committed + " " + text
    else:
        candidate = prev_committed + text
    if len(candidate) > MAX_TEXT_CHARS:
        raise ValueError("Invalid bounded Whistle stream result")
    if candidate and pending:
        agg = candidate + " " + pending
    else:
        agg = candidate + pending
    if len(agg) > MAX_TEXT_CHARS:
        raise ValueError("Invalid bounded Whistle stream result")
    try:
        agg_utf8 = agg.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid bounded Whistle stream result") from None
    if len(agg_utf8) > MAX_AGG_UTF8:
        raise ValueError("Invalid bounded Whistle stream result")
    if is_final and pending != "":
        raise ValueError("Invalid bounded Whistle stream result")
    return {
        "delta_text": text,
        "pending_text": pending,
        "language": language,
        "pass_ms": pass_ms,
        "candidate": candidate,
    }


def validate_stream_update(update, *, expected_samples=None, expect_final=False):
    required_keys = {
        "delta_text",
        "committed_text",
        "pending_text",
        "received_samples",
        "final",
        "native_pass_ms",
        "language",
    }
    if not isinstance(update, dict) or set(update) != required_keys:
        raise ValueError("Invalid bounded Whistle stream update")
    for field_name in ("delta_text", "committed_text", "pending_text"):
        val = update[field_name]
        if not isinstance(val, str) or len(val) > MAX_TEXT_CHARS:
            raise ValueError("Invalid bounded Whistle stream text")
        val.encode("utf-8")
    received_samples = update["received_samples"]
    if (
        type(received_samples) is not int
        or not 0 <= received_samples <= MAX_TOTAL_SAMPLES
        or (expected_samples is not None and received_samples != expected_samples)
    ):
        raise ValueError("Invalid Whistle stream sample count")
    if type(update["final"]) is not bool or update["final"] is not expect_final:
        raise ValueError("Invalid Whistle stream final flag")
    if expect_final and update["pending_text"] != "":
        raise ValueError("Final Whistle stream update cannot retain pending_text")
    pass_ms = update["native_pass_ms"]
    if (
        type(pass_ms) is bool
        or not isinstance(pass_ms, (int, float))
        or not math.isfinite(pass_ms)
        or not 0 <= float(pass_ms) <= MAX_PASS_MS
    ):
        raise ValueError("Invalid Whistle stream native_pass_ms")
    language = update["language"]
    if not (
        language is None
        or (isinstance(language, str) and (language == "" or language in SUPPORTED_LANGUAGES))
    ):
        raise ValueError("Invalid Whistle stream language")
    return update
