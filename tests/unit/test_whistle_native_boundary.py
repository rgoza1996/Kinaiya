"""Binding/isolation tripwires with synthetic bytes; no native Whistle load."""

import array
import ctypes
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from packages.providers import whistle_backend as backend
from packages.providers import whistle_isolation as isolation


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


def test_explicit_fd_load_and_exact_c_api_use_only_verified_sealed_bytes(monkeypatch):
    import resource

    active, seen = {}, []

    @contextmanager
    def sealed(path, pin, workspace):
        assert workspace == "/synthetic/agent"
        assert pin is backend.MANIFEST["weights" if path == "weights" else "library"]
        fd = os.memfd_create("synthetic-whistle-test")
        raw = b"public synthetic bytes"
        os.write(fd, raw)
        os.lseek(fd, 0, 0)
        active[path] = fd
        try:
            yield fd
        finally:
            os.close(fd)
            del active[path]

    def load(data, count):
        assert active.keys() == {"weights", "library"}
        assert data == b"public synthetic bytes" and count == len(data)
        seen.append("load")
        return 0

    def transcribe(pcm, count, prompt, language, flags, output, capacity):
        assert count == 2 and list(pcm) == [0.25, -0.25]
        assert prompt is language is None and flags == 0
        assert capacity == 128 * 1024
        output.value = b'{"text":"Public fixture"}'
        seen.append("batch")
        return 0

    lib = SimpleNamespace(needle_load=Function(load), needle_transcribe=Function(transcribe))

    def cdll(path):
        assert seen == ["files_disabled"]
        assert path == f"/proc/self/fd/{active['library']}"
        assert active.keys() == {"weights", "library"}
        seen.append("explicit_cdll")
        return lib

    monkeypatch.setattr(backend, "sealed_asset", sealed)
    monkeypatch.setattr(backend, "MANIFEST", {"weights": {"bytes": 22}, "library": {}})
    monkeypatch.setattr(backend.ctypes, "CDLL", cdll)

    def disable_files(which, limits):
        assert which == resource.RLIMIT_FSIZE and limits == (0, 0)
        seen.append("files_disabled")

    monkeypatch.setattr(resource, "setrlimit", disable_files)
    monkeypatch.setattr(backend, "decode_wav", lambda data: array.array("f", [0.25, -0.25]))
    engine = backend.WhistleEngine("weights", "library", "/synthetic/agent")
    assert active == {}
    assert lib.needle_load.argtypes == [ctypes.c_char_p, ctypes.c_uint64]
    assert lib.needle_transcribe.restype is ctypes.c_int
    assert engine.transcribe(b"fixture") == "Public fixture"
    assert seen == ["files_disabled", "explicit_cdll", "load", "batch"]


def test_file_limit_failure_closes_both_inputs_and_refuses_native_load(monkeypatch):
    import resource

    active, descriptors = set(), []

    @contextmanager
    def sealed(*args):
        fd = os.memfd_create("synthetic-limit-failure")
        active.add(fd)
        descriptors.append(fd)
        try:
            yield fd
        finally:
            active.remove(fd)
            os.close(fd)

    def refused(which, limits):
        assert len(active) == 2 and which == resource.RLIMIT_FSIZE and limits == (0, 0)
        raise PermissionError("Synthetic limit refusal")

    monkeypatch.setattr(backend, "sealed_asset", sealed)
    monkeypatch.setattr(resource, "setrlimit", refused)
    monkeypatch.setattr(
        backend.ctypes, "CDLL", lambda path: pytest.fail("Native load after refusal")
    )
    with pytest.raises(PermissionError, match="limit refusal"):
        backend.WhistleEngine("weights", "library", "/synthetic/agent")
    assert not active and len(descriptors) == 2
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_failed_asset_verification_never_reaches_cdll(monkeypatch):
    @contextmanager
    def refused(*args):
        raise ValueError("Synthetic digest refusal")
        yield  # pragma: no cover

    monkeypatch.setattr(backend, "sealed_asset", refused)
    monkeypatch.setattr(backend.ctypes, "CDLL", lambda path: pytest.fail("Unverified native load"))
    with pytest.raises(ValueError, match="digest"):
        backend.WhistleEngine("weights", "library", "/synthetic/agent")


def test_missing_required_isolation_syscall_refuses_before_filter_load(monkeypatch):
    seen = []
    libc = SimpleNamespace(prctl=lambda *args: 0)
    seccomp = SimpleNamespace(
        seccomp_init=Function(lambda action: 123),
        seccomp_syscall_resolve_name=Function(lambda name: -1),
        seccomp_rule_add_array=Function(lambda *args: pytest.fail("Missing syscall used")),
        seccomp_attr_set=Function(lambda *args: 0),
        seccomp_load=Function(lambda *args: pytest.fail("Incomplete filter installed")),
        seccomp_release=Function(lambda *args: seen.append("released")),
    )
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        isolation.ctypes, "CDLL", lambda name, **kw: libc if name is None else seccomp
    )
    with pytest.raises(RuntimeError, match="Required Whistle isolation"):
        isolation.deny_network()
    assert seen == ["released"]
