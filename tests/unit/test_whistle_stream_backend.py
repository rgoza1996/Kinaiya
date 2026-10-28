"""Synthetic fake-CDLL streaming seam checks; no real C load or Torch."""

import array
import ctypes
import json
import os
import struct
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from packages.providers import whistle_backend as backend


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def _payload(text, pending, words, received, language=None, pass_ms=5):
    return json.dumps(
        {
            "text": text,
            "words": words,
            "pending": pending,
            "language": language,
            "received": received,
            "pass_ms": pass_ms,
        }
    ).encode()


def _mk(monkeypatch, proc=None, stop=None):
    seen = []
    procs, stops = [], []

    @contextmanager
    def sealed(path, pin, workspace):
        fd = os.memfd_create("synthetic-stream-test")
        os.write(fd, b"public synthetic bytes")
        os.lseek(fd, 0, 0)
        try:
            yield fd
        finally:
            os.close(fd)

    def load(data, count):
        return 0

    def batch(pcm, count, a, b, f, out, cap):
        out.value = b'{"text":"Public fixture"}'
        return 0

    def _proc(pcm, n, lang, kw, out, cap):
        procs.append((list(pcm)[:8], n, lang, kw, cap))
        return proc(pcm, n, lang, kw, out, cap) if proc else 0

    def _stop(out, cap):
        stops.append(cap)
        return stop(out, cap) if stop else 0

    lib = SimpleNamespace(
        needle_load=Function(load),
        needle_transcribe=Function(batch),
        needle_stream_transcribe_process=Function(_proc),
        needle_stream_transcribe_stop=Function(_stop),
    )

    def cdll(path):
        seen.append("cdll")
        return lib

    import resource

    def lim(which, limits):
        assert which == resource.RLIMIT_FSIZE and limits == (0, 0)
        seen.append("files_disabled")

    monkeypatch.setattr(backend, "sealed_asset", sealed)
    monkeypatch.setattr(backend, "MANIFEST", {"weights": {"bytes": 22}, "library": {}})
    monkeypatch.setattr(backend.ctypes, "CDLL", cdll)
    monkeypatch.setattr(resource, "setrlimit", lim)
    monkeypatch.setattr(backend, "decode_wav", lambda d: array.array("f", [0.25, -0.25]))
    eng = backend.WhistleEngine("w", "l", "/synthetic/agent", enable_streaming=True)
    return eng, lib, seen, procs, stops


def test_signatures_and_fsize_before_cdll(monkeypatch):
    eng, lib, seen, _, _ = _mk(monkeypatch)
    assert seen[:2] == ["files_disabled", "cdll"]
    assert lib.needle_stream_transcribe_process.argtypes == [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    assert lib.needle_stream_transcribe_process.restype is ctypes.c_int
    assert lib.needle_stream_transcribe_stop.argtypes == [ctypes.c_char_p, ctypes.c_int]
    assert lib.needle_stream_transcribe_stop.restype is ctypes.c_int
    with pytest.raises(TypeError):
        backend.WhistleEngine("w", "l", "/synthetic/agent", enable_streaming=1)


def test_pcm_extremes_endian_single_call(monkeypatch):
    got = {}

    def proc(pcm, n, lang, kw, out, cap):
        assert lang is None and kw is None and cap == 128 * 1024
        got["pcm"] = list(pcm)
        got["n"] = n
        out.value = _payload("", "", [], n / 16000.0)
        return 0

    eng, _, _, procs, _ = _mk(monkeypatch, proc=proc)
    assert eng.begin_stream() is None
    raw = struct.pack("<4h", -32768, 32767, 1, 256)
    r = eng.stream_pcm(raw)
    assert r["received_samples"] == 4 and r["final"] is False
    assert got["pcm"][0] == pytest.approx(-1.0) and got["pcm"][1] == pytest.approx(32767 / 32768.0)
    assert got["pcm"][2] == pytest.approx(1 / 32768.0) and got["pcm"][3] == pytest.approx(
        256 / 32768.0
    )
    assert len(procs) == 1 and set(r) == {
        "delta_text",
        "committed_text",
        "pending_text",
        "received_samples",
        "final",
        "native_pass_ms",
        "language",
    }


def test_pending_replacement_and_accumulation_then_stop_next(monkeypatch):
    calls = {"p": 0, "s": 0}

    def proc(pcm, n, lang, kw, out, cap):
        calls["p"] += 1
        if calls["p"] == 1:
            out.value = _payload(
                "hello",
                "hel",
                [{"word": "hello", "start": 0.0, "end": 0.09, "probability": 0.9}],
                0.1,
                "en",
                7,
            )
            return 1
        out.value = _payload(
            "world",
            "hello world?",
            [{"word": "world", "start": 0.1, "end": 0.2, "probability": 0.8}],
            0.2,
            "en",
            8,
        )
        return 1

    def stop(out, cap):
        calls["s"] += 1
        assert cap == 128 * 1024
        out.value = _payload(
            "!", "", [{"word": "x", "start": 0.0, "end": 0.2, "probability": 0.5}], 0.2, "en", 9
        )
        return 0

    eng, _, _, _, stops = _mk(monkeypatch, proc=proc, stop=stop)
    eng.begin_stream()
    c1 = bytes(1600 * 2)
    r1 = eng.stream_pcm(c1)
    assert (r1["delta_text"], r1["committed_text"], r1["pending_text"]) == ("hello", "hello", "hel")
    assert r1["received_samples"] == 1600 and r1["native_pass_ms"] == 7 and r1["language"] == "en"
    r2 = eng.stream_pcm(c1)
    assert (r2["delta_text"], r2["committed_text"], r2["pending_text"]) == (
        "world",
        "hello world",
        "hello world?",
    )
    assert r2["received_samples"] == 3200
    f = eng.finish_stream()
    assert f["final"] is True and f["committed_text"] == "hello world !" and f["pending_text"] == ""
    assert f["received_samples"] == 3200 and calls["s"] == 1 and len(stops) == 1
    eng.begin_stream()
    assert eng.finish_stream()["received_samples"] == 0


def test_empty_finish_makes_no_stop(monkeypatch):
    def stop(out, cap):
        raise AssertionError("stop must not run for empty stream")

    eng, _, _, _, _ = _mk(monkeypatch, stop=stop)
    eng.begin_stream()
    r = eng.finish_stream()
    assert r == {
        "delta_text": "",
        "committed_text": "",
        "pending_text": "",
        "received_samples": 0,
        "final": True,
        "native_pass_ms": 0,
        "language": None,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "dup",
        "nonfinite",
        "unknown",
        "missing",
        "words",
        "received",
        "count",
        "oversize",
        "aggregate",
        "badlang",
    ],
)
def test_process_refusals_taint(monkeypatch, kind):
    n = 160
    rec = n / 16000.0
    if kind == "dup":
        raw = b'{"text":"a","text":"b","words":[],"pending":"","language":null,"received":0.01,"pass_ms":1}'
    elif kind == "nonfinite":
        raw = b'{"text":"a","words":[],"pending":"","language":null,"received":NaN,"pass_ms":1}'
    elif kind == "unknown":
        raw = json.dumps(
            {
                "text": "",
                "words": [],
                "pending": "",
                "language": None,
                "received": rec,
                "pass_ms": 1,
                "extra": 1,
            }
        ).encode()
    elif kind == "missing":
        raw = json.dumps(
            {"text": "", "words": [], "pending": "", "language": None, "received": rec}
        ).encode()
    elif kind == "words":
        raw = _payload("", "", [{"word": "w", "start": 0.5, "end": 0.1, "probability": 2}], rec)
    elif kind == "received":
        raw = _payload("", "", [], rec + 1.0)
    elif kind == "count":
        raw = _payload(
            "hi", "", [{"word": "hi", "start": 0.0, "end": rec, "probability": 0.5}], rec
        )

        def proc(pcm, nn, a, b, out, cap):
            out.value = raw
            return 999

        eng, lib, _, _, _ = _mk(monkeypatch, proc=proc)
        eng.begin_stream()
        with pytest.raises(RuntimeError):
            eng.stream_pcm(bytes(n * 2))
        assert eng._tainted and eng._stream_active
        with pytest.raises(RuntimeError):
            eng.stream_pcm(bytes(n * 2))
        return
    elif kind == "oversize":
        raw = b"x" * (128 * 1024)
    elif kind == "aggregate":
        raw = _payload("x" * 16000, "y", [], rec)
    else:
        raw = _payload("", "", [], rec, "xx", 1)

    def proc(pcm, nn, a, b, out, cap):
        out.value = raw
        return 0

    eng, lib, _, _, _ = _mk(monkeypatch, proc=proc)
    eng.begin_stream()
    with pytest.raises(RuntimeError):
        eng.stream_pcm(bytes(n * 2))
    assert eng._tainted
    for fn in (
        lambda: eng.stream_pcm(bytes(n * 2)),
        eng.finish_stream,
        lambda: eng.transcribe(b"x"),
        eng.begin_stream,
    ):
        with pytest.raises(RuntimeError):
            fn()
    assert eng._stream_active


def test_final_tail_must_be_empty(monkeypatch):
    def proc(pcm, n, a, b, out, cap):
        out.value = _payload("", "", [], n / 16000.0)
        return 0

    def stop(out, cap):
        out.value = _payload("", "tail", [], 0.01)
        return 0

    eng, _, _, _, _ = _mk(monkeypatch, proc=proc, stop=stop)
    eng.begin_stream()
    eng.stream_pcm(bytes(160 * 2))
    with pytest.raises(RuntimeError):
        eng.finish_stream()
    assert eng._tainted


def test_predispatch_invalid_not_taint_nor_advance(monkeypatch):
    eng, _, _, procs, _ = _mk(
        monkeypatch,
        proc=lambda pcm, n, a, b, out, cap: (
            setattr(out, "value", _payload("", "", [], n / 16000.0)),
            0,
        )[1],
    )
    eng.begin_stream()
    for bad in (None, "x", b"", b"x", b"\x01", bytes(16001 * 2)):
        with pytest.raises((TypeError, ValueError, RuntimeError)):
            eng.stream_pcm(bad)
    assert not eng._tainted and eng._received_samples == 0 and len(procs) == 0
    r = eng.stream_pcm(bytes(160 * 2))
    assert r["received_samples"] == 160
    with pytest.raises(RuntimeError):
        eng.begin_stream()
    with pytest.raises(RuntimeError):
        eng.transcribe(b"x")


def test_negative_and_exception_taint_and_block(monkeypatch):
    def proc(pcm, n, a, b, out, cap):
        return -1

    eng, lib, _, _, _ = _mk(monkeypatch, proc=proc)
    eng.begin_stream()
    with pytest.raises(RuntimeError):
        eng.stream_pcm(bytes(160 * 2))
    assert eng._tainted
    nstop = []
    lib.needle_stream_transcribe_stop = Function(lambda o, c: nstop.append(1) or 0)
    for fn in (
        lambda: eng.stream_pcm(bytes(160 * 2)),
        eng.finish_stream,
        lambda: eng.transcribe(b"x"),
        eng.begin_stream,
    ):
        with pytest.raises(RuntimeError):
            fn()
    assert nstop == []


def test_batch_compat_when_disabled(monkeypatch):
    seen = []

    @contextmanager
    def sealed(*a):
        fd = os.memfd_create("synthetic-batch-test")
        os.write(fd, b"public synthetic bytes")
        os.lseek(fd, 0, 0)
        try:
            yield fd
        finally:
            os.close(fd)

    import resource

    monkeypatch.setattr(backend, "sealed_asset", sealed)
    monkeypatch.setattr(backend, "MANIFEST", {"weights": {"bytes": 22}, "library": {}})

    def cdll(p):
        seen.append("cdll")
        return SimpleNamespace(
            needle_load=Function(lambda d, c: 0),
            needle_transcribe=Function(
                lambda pcm, c, a, b, f, o, cap: (
                    setattr(o, "value", b'{"text":"Public fixture"}') or 0
                )
            ),
        )

    monkeypatch.setattr(backend.ctypes, "CDLL", cdll)
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: seen.append("files_disabled"))
    monkeypatch.setattr(backend, "decode_wav", lambda d: array.array("f", [0.25, -0.25]))
    eng = backend.WhistleEngine("w", "l", "/synthetic/agent")
    assert eng.transcribe(b"f") == "Public fixture"
    for fn in (eng.begin_stream, lambda: eng.stream_pcm(b"\x00\x00"), eng.finish_stream):
        with pytest.raises(RuntimeError):
            fn()


@pytest.mark.parametrize("phase", ["push", "finish"])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_every_dispatched_exception_requires_process_replacement(monkeypatch, phase, failure):
    calls = []

    def proc(pcm, count, language, keywords, output, capacity):
        calls.append("push")
        if phase == "push":
            raise failure("synthetic native dispatch interrupted")
        output.value = _payload("", "tail", [], count / 16000)
        return 0

    def stop(output, capacity):
        calls.append("finish")
        raise failure("synthetic native finish interrupted")

    engine, _, _, _, _ = _mk(monkeypatch, proc=proc, stop=stop)
    engine.begin_stream()
    if phase == "finish":
        engine.stream_pcm(bytes(320))
    with pytest.raises(RuntimeError, match="Whistle stream failed"):
        engine.stream_pcm(bytes(320)) if phase == "push" else engine.finish_stream()
    dispatched = list(calls)
    for operation in (
        engine.begin_stream,
        lambda: engine.stream_pcm(bytes(320)),
        engine.finish_stream,
        lambda: engine.transcribe(b"unused"),
    ):
        with pytest.raises(RuntimeError, match="tainted"):
            operation()
    assert calls == dispatched


def test_normal_finish_allows_real_new_stream_and_batch(monkeypatch):
    received = 0
    calls = []

    def proc(pcm, count, language, keywords, output, capacity):
        nonlocal received
        received += count
        calls.append(("push", received))
        output.value = _payload("", "new tail", [], received / 16000)
        return 0

    def stop(output, capacity):
        nonlocal received
        calls.append(("finish", received))
        output.value = _payload("complete", "", [], received / 16000)
        received = 0
        return 0  # stop has no declared positive-word-count requirement

    engine, _, _, _, _ = _mk(monkeypatch, proc=proc, stop=stop)
    for _ in range(2):
        engine.begin_stream()
        assert engine.stream_pcm(bytes(320))["received_samples"] == 160
        final = engine.finish_stream()
        assert final["committed_text"] == "complete"
        assert final["received_samples"] == 160
        with pytest.raises(RuntimeError, match="not active"):
            engine.finish_stream()
    assert calls == [("push", 160), ("finish", 160)] * 2
    assert engine.transcribe(b"synthetic WAV") == "Public fixture"
