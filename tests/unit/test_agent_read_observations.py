"""Complete-file observations: real scoped I/O, bounded refusals and races."""

import hashlib
import json
import os
import re
import socket
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.policy import PolicyDenied, ToolPolicy
from packages.contracts.native_tool_schemas import native_tool_definition, native_tool_schema
from services.agent import tools

MAXIMUM = tools.READ_LIMIT * 4
MARKER = "public-synthetic-tail-which-must-be-complete"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def os_proxy(monkeypatch, **replacements):
    """Fault only this tool; never corrupt the shared process's os module."""
    values = {key: getattr(os, key) for key in dir(os)}
    values.update(replacements)
    monkeypatch.setattr(tools, "os", SimpleNamespace(**values))


def read(tmp_path, text, maximum=tools.READ_LIMIT):
    path = tmp_path / "source.txt"
    data = text.encode("utf-8") if isinstance(text, str) else text
    path.write_bytes(data)
    return tools.run_read(ToolPolicy(tmp_path), path.name, maximum, 7)


@pytest.mark.parametrize(
    "text", ["", "a", "hello\r\nworld\n", "café 🌊 日本語", "\ufeffbom\x00tail"]
)
def test_success_preserves_exact_complete_utf8_and_full_data_evidence(tmp_path, text):
    output, evidence = read(tmp_path, text)
    assert output == text
    assert evidence.kind == "read"
    assert evidence.path == str(tmp_path / "source.txt")
    assert evidence.objective_revision == 7
    assert evidence.sha256 == digest(text.encode("utf-8"))
    assert evidence.output == ""


def test_native_output_keeps_json_and_tail_beyond_2000_characters(tmp_path):
    content = {
        "rows": [{"index": i, "text": "public synthetic"} for i in range(120)],
        "tail": MARKER,
    }
    raw = json.dumps(content, ensure_ascii=False, indent=2)
    assert len(raw) > 2000
    output, evidence = read(tmp_path, raw)
    assert output == raw and json.loads(output) == content
    assert MARKER in output[-100:]
    assert evidence.sha256 == digest(raw.encode())


def test_digest_includes_tail_beyond_default_bound(tmp_path):
    first = b"a" * tools.READ_LIMIT + b"-first-tail"
    output, evidence = read(tmp_path, first, MAXIMUM)
    assert output.encode() == first
    assert evidence.sha256 == digest(first) != digest(first[: tools.READ_LIMIT])
    second = first[:-10] + b"other-tail"
    output2, evidence2 = read(tmp_path, second, MAXIMUM)
    assert output2.encode() == second
    assert evidence2.sha256 == digest(second) != evidence.sha256


@pytest.mark.parametrize("size", [1, tools.READ_LIMIT, MAXIMUM])
def test_exact_requested_byte_boundary_succeeds(tmp_path, size):
    raw = b"x" * size
    output, evidence = read(tmp_path, raw, size)
    assert output.encode() == raw and evidence.sha256 == digest(raw)


def test_exact_multibyte_native_boundary_succeeds(tmp_path):
    text = "🌊" * (MAXIMUM // 4)
    assert len(text.encode()) == MAXIMUM
    output, evidence = read(tmp_path, text, MAXIMUM)
    assert output == text and evidence.sha256 == digest(text.encode())


@pytest.mark.parametrize(
    ("raw", "requested"),
    [
        (b"abc", 2),
        ("🌊".encode(), 3),
        (b"x" * (tools.READ_LIMIT + 1), tools.READ_LIMIT),
        (b"x" * MAXIMUM, MAXIMUM - 1),
    ],
)
def test_requested_bound_refuses_without_prefix_and_identifies_complete_source(
    tmp_path, raw, requested
):
    with pytest.raises(tools.ToolFailure) as caught:
        read(tmp_path, raw, requested)
    message = str(caught.value)
    assert f"max_bytes={requested}" in message
    assert f"size_bytes={len(raw)}" in message
    assert f"source_sha256={digest(raw)}" in message
    assert f"Request max_bytes={len(raw)}" in message
    assert "stat_version" not in message
    assert not hasattr(caught.value, "evidence")
    assert len(message) < 400
    output, evidence = tools.run_read(ToolPolicy(tmp_path), "source.txt", len(raw), 7)
    assert output.encode() == raw and evidence.sha256 == digest(raw)


@pytest.mark.parametrize("extra", [1, 100_000])
def test_native_oversize_has_only_honest_stat_version_no_content_digest(
    tmp_path, monkeypatch, extra
):
    raw = b"q" * (MAXIMUM + extra)
    (tmp_path / "source.txt").write_bytes(raw)

    def no_read(*args):
        pytest.fail("Known oversized file must not be read or partially hashed")

    os_proxy(monkeypatch, read=no_read)
    with pytest.raises(tools.ToolFailure) as caught:
        tools.run_read(ToolPolicy(tmp_path), "source.txt", MAXIMUM, 7)
    message = str(caught.value)
    assert f"size_bytes={len(raw)}" in message
    assert re.search(r"stat_version=[0-9a-f]{64}", message)
    assert "metadata identity, not a content hash" in message
    assert "source_sha256" not in message and digest(raw) not in message
    assert "paging and shell fallback are unavailable" in message
    assert not hasattr(caught.value, "evidence") and len(message) < 500


def test_stat_version_changes_after_same_size_oversized_source_replacement(tmp_path):
    target = tmp_path / "source.txt"
    target.write_bytes(b"x" * (MAXIMUM + 1))

    def refused():
        with pytest.raises(tools.ToolFailure) as caught:
            tools.run_read(ToolPolicy(tmp_path), "source.txt", MAXIMUM, 1)
        return re.search(r"stat_version=([0-9a-f]{64})", str(caught.value)).group(1)

    first = refused()
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"y" * (MAXIMUM + 1))
    replacement.replace(target)
    assert refused() != first


class WouldCoerce:
    def __int__(self):
        pytest.fail("Native read must not coerce caller limits")


class IntSubclass(int):
    pass


@pytest.mark.parametrize(
    "maximum",
    [None, False, True, 0, -1, MAXIMUM + 1, 1.0, "42", [], {}, WouldCoerce(), IntSubclass(1)],
)
def test_noncanonical_maximum_fails_before_any_file_policy_io(tmp_path, maximum):
    policy = ToolPolicy(tmp_path)

    def no_io(*args):
        pytest.fail("Bad max_bytes must fail before filesystem I/O")

    policy.check_read = no_io
    with pytest.raises(tools.ToolFailure) as caught:
        tools.run_read(policy, "source.txt", maximum, 1)
    assert str(caught.value) == "read_file max_bytes must be an integer from 1 to 262144"


@pytest.mark.parametrize(
    "raw", [b"\xff", b"public\xc3", b"a" * 3000 + b"\x80", b"x" * (MAXIMUM - 1) + b"\xff"]
)
def test_invalid_utf8_never_succeeds_with_replacement_or_prefix(tmp_path, raw):
    with pytest.raises(tools.ToolFailure) as caught:
        read(tmp_path, raw, MAXIMUM)
    assert str(caught.value) == "read_file: complete file is not valid UTF-8"
    assert "\ufffd" not in str(caught.value) and not hasattr(caught.value, "evidence")


def test_open_uses_nofollow_nonblocking_cloexec_and_closes_exactly_once(tmp_path, monkeypatch):
    (tmp_path / "source.txt").write_bytes(b"data")
    opened, closed = [], []

    def tracked_open(name, flags, *, dir_fd):
        assert name == "source.txt" and dir_fd >= 0
        assert flags & os.O_NOFOLLOW and flags & os.O_CLOEXEC and flags & os.O_NONBLOCK
        fd = os.open(name, flags, dir_fd=dir_fd)
        assert not os.get_inheritable(fd)
        opened.append(fd)
        return fd

    def tracked_close(fd):
        closed.append(fd)
        os.close(fd)

    os_proxy(monkeypatch, open=tracked_open, close=tracked_close)
    assert tools.run_read(ToolPolicy(tmp_path), "source.txt", 4, 1)[0] == "data"
    assert len(opened) == 1 and closed == opened


@pytest.mark.parametrize("kind", ["directory", "fifo", "socket"])
def test_nonregular_sources_are_refused_before_read_without_fifo_hang(tmp_path, monkeypatch, kind):
    path = tmp_path / "source.txt"
    sock = None
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
    opened, closed = [], []

    def guarded_open(name, flags, *, dir_fd):
        # Assert before calling the real FIFO open, so even a regressed flag
        # fails deterministically instead of hanging this test process.
        assert flags & os.O_NONBLOCK
        fd = os.open(name, flags, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def tracked_close(fd):
        closed.append(fd)
        os.close(fd)

    def no_read(*args):
        pytest.fail("Nonregular input must never be read")

    os_proxy(monkeypatch, open=guarded_open, close=tracked_close, read=no_read)
    try:
        with pytest.raises(tools.ToolFailure):
            tools.run_read(ToolPolicy(tmp_path), path.name, 1, 1)
    finally:
        if sock is not None:
            sock.close()
    assert closed == opened


def test_policy_approved_internal_symlink_reads_its_complete_canonical_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text(MARKER)
    (tmp_path / "alias").symlink_to(target.name)
    output, evidence = tools.run_read(ToolPolicy(tmp_path), "alias", len(MARKER), 1)
    assert output == MARKER and evidence.path == str(target)


@pytest.mark.parametrize("kind", ["escape", "protected", "traversal"])
def test_policy_denials_remain_denials_without_host_paths_or_payloads(tmp_path, kind):
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "secret-host-marker"
    outside.write_text("never return this host data")
    protected = workspace / "protected-private-marker"
    protected.mkdir()
    (protected / "data.txt").write_text("protected")
    (workspace / "alias").symlink_to(outside)
    path = {
        "escape": "alias",
        "protected": "protected-private-marker/data.txt",
        "traversal": "../secret-host-marker",
    }[kind]
    policy = ToolPolicy(workspace, protected_roots=(protected,))
    with pytest.raises(PolicyDenied) as caught:
        tools.run_read(policy, path, 100, 1)
    assert str(caught.value) == "read_file: path is outside the allowed file scope"


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_leaf_substitution_after_policy_check_is_refused_without_read(tmp_path, monkeypatch, kind):
    target = tmp_path / "source.txt"
    target.write_text("original")
    outside = tmp_path.parent / "public-synthetic-outside-marker"
    outside.write_text("never read outside")
    opened, closed = [], []

    def changed_open(name, flags, *, dir_fd):
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        target.unlink()
        if kind == "symlink":
            target.symlink_to(outside)
        else:
            os.mkfifo(target)
        fd = os.open(name, flags, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def tracked_close(fd):
        closed.append(fd)
        os.close(fd)

    def no_read(*args):
        pytest.fail("Substituted source must not be read")

    os_proxy(monkeypatch, open=changed_open, close=tracked_close, read=no_read)
    with pytest.raises(tools.ToolFailure):
        tools.run_read(ToolPolicy(tmp_path), target.name, 100, 1)
    assert closed == opened


@pytest.mark.parametrize(
    "mutation", ["rewrite", "grow", "shrink", "replace", "unlink", "symlink", "directory"]
)
def test_changes_during_read_never_produce_success_or_source_digest(
    tmp_path, monkeypatch, mutation
):
    target = tmp_path / "source.txt"
    target.write_bytes(b"public-original")
    first = True
    opened, closed = [], []

    def tracked_open(name, flags, *, dir_fd):
        fd = os.open(name, flags, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def tracked_close(fd):
        closed.append(fd)
        os.close(fd)

    def changing_read(fd, count):
        nonlocal first
        data = os.read(fd, count)
        if first:
            first = False
            before = target.stat()
            if mutation in {"rewrite", "grow", "shrink"}:
                target.write_bytes(
                    {"rewrite": b"public-replaced", "grow": b"g" * (MAXIMUM + 1), "shrink": b"x"}[
                        mutation
                    ]
                )
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            else:
                target.unlink()
                if mutation == "replace":
                    target.write_bytes(b"public-original")
                elif mutation == "symlink":
                    (tmp_path / "other.txt").write_text("other")
                    target.symlink_to("other.txt")
                elif mutation == "directory":
                    target.mkdir()
        return data

    os_proxy(monkeypatch, open=tracked_open, close=tracked_close, read=changing_read)
    with pytest.raises(tools.ToolFailure) as caught:
        tools.run_read(ToolPolicy(tmp_path), target.name, MAXIMUM, 1)
    assert "source_sha256" not in str(caught.value)
    assert not hasattr(caught.value, "evidence")
    assert len(str(caught.value)) < 150 and closed == opened


@pytest.mark.parametrize(
    "field", ["st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"]
)
def test_each_before_after_identity_field_is_checked(tmp_path, monkeypatch, field):
    (tmp_path / "source.txt").write_bytes(b"data")
    leaf = None
    calls = 0

    def tracked_open(name, flags, *, dir_fd):
        nonlocal leaf
        leaf = os.open(name, flags, dir_fd=dir_fd)
        return leaf

    def changed_stat(fd):
        nonlocal calls
        value = os.fstat(fd)
        if fd != leaf:
            return value
        calls += 1
        if calls != 2:
            return value
        result = {key: getattr(value, key) for key in dir(value) if key.startswith("st_")}
        result[field] += 1
        return SimpleNamespace(**result)

    os_proxy(monkeypatch, open=tracked_open, fstat=changed_stat)
    with pytest.raises(tools.ToolFailure, match="source changed"):
        tools.run_read(ToolPolicy(tmp_path), "source.txt", 4, 1)


def test_parent_directory_replacement_during_read_is_not_a_current_path(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "source.txt").write_text("public-original")
    first = True

    def changing_read(fd, count):
        nonlocal first
        data = os.read(fd, count)
        if first:
            first = False
            parent.rename(tmp_path / "old-parent")
            parent.mkdir()
            (parent / "source.txt").write_text("public-original")
        return data

    os_proxy(monkeypatch, read=changing_read)
    with pytest.raises(tools.ToolFailure, match="source changed"):
        tools.run_read(ToolPolicy(tmp_path), "parent/source.txt", 100, 1)


def test_intermediate_symlink_escape_before_descriptor_walk_is_refused(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "source.txt").write_text("public")
    outside = tmp_path.parent / "public-synthetic-outside-directory"
    outside.mkdir()
    (outside / "source.txt").write_text("outside marker")
    policy = ToolPolicy(tmp_path)
    original = policy.scoped_parent

    @contextmanager
    def replaced_parent(*args, **kwargs):
        parent.rename(tmp_path / "old-parent")
        parent.symlink_to(outside)
        with original(*args, **kwargs) as value:
            yield value

    monkeypatch.setattr(policy, "scoped_parent", replaced_parent)
    with pytest.raises(PolicyDenied, match="outside the allowed file scope"):
        tools.run_read(policy, "parent/source.txt", 100, 1)


def test_short_native_reads_are_collected_to_eof_with_native_maximum_plus_one_bound(
    tmp_path, monkeypatch
):
    raw = ("café" * 1000).encode()
    (tmp_path / "source.txt").write_bytes(raw)
    requests = []

    def short_read(fd, count):
        requests.append(count)
        return os.read(fd, min(count, 17))

    os_proxy(monkeypatch, read=short_read)
    output, evidence = tools.run_read(ToolPolicy(tmp_path), "source.txt", len(raw), 1)
    assert output.encode() == raw and evidence.sha256 == digest(raw)
    assert all(0 < count <= tools.READ_LIMIT for count in requests)


def test_exact_native_bound_still_probes_one_more_byte_before_success(tmp_path, monkeypatch):
    (tmp_path / "source.txt").write_bytes(b"a" * MAXIMUM)
    reads = []

    def recorded_read(fd, count):
        data = os.read(fd, count)
        reads.append((count, len(data)))
        return data

    os_proxy(monkeypatch, read=recorded_read)
    output, _ = tools.run_read(ToolPolicy(tmp_path), "source.txt", MAXIMUM, 1)
    assert len(output) == MAXIMUM
    assert sum(size for _, size in reads) == MAXIMUM
    assert reads[-1] == (1, 0)


def test_known_oversized_file_changed_before_refusal_has_no_version_receipt(tmp_path, monkeypatch):
    target = tmp_path / "source.txt"
    target.write_bytes(b"a" * (MAXIMUM + 1))
    leaf, calls = None, 0

    def tracked_open(name, flags, *, dir_fd):
        nonlocal leaf
        leaf = os.open(name, flags, dir_fd=dir_fd)
        return leaf

    def changing_stat(fd):
        nonlocal calls
        if fd == leaf:
            calls += 1
            if calls == 2:
                target.write_bytes(b"small changed source")
        return os.fstat(fd)

    os_proxy(monkeypatch, open=tracked_open, fstat=changing_stat)
    with pytest.raises(tools.ToolFailure, match="source changed") as caught:
        tools.run_read(ToolPolicy(tmp_path), target.name, MAXIMUM, 1)
    assert "stat_version" not in str(caught.value) and "source_sha256" not in str(caught.value)


@pytest.mark.parametrize("operation", ["open", "read", "fstat", "stat"])
def test_native_io_errors_are_bounded_payload_free_refusals(tmp_path, monkeypatch, operation):
    (tmp_path / "source.txt").write_text("public")
    opened, closed = [], []

    def denied(*args, **kwargs):
        raise OSError("host-private-path-marker: secret-payload-marker")

    def tracked_open(name, flags, *, dir_fd):
        if operation == "open":
            return denied()
        fd = os.open(name, flags, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def tracked_close(fd):
        closed.append(fd)
        os.close(fd)

    failures = {operation: denied} if operation != "open" else {}
    os_proxy(monkeypatch, **failures, open=tracked_open, close=tracked_close)
    with pytest.raises(tools.ToolFailure) as caught:
        tools.run_read(ToolPolicy(tmp_path), "source.txt", 100, 1)
    assert str(caught.value) == "read_file: cannot observe a stable regular workspace file"
    assert closed == opened


def test_logical_read_schema_describes_whole_or_refuse_native_semantics():
    schema = native_tool_schema("read_file")
    limit = schema["properties"]["max_bytes"]
    assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, MAXIMUM, tools.READ_LIMIT)
    assert "never clipped" in limit["description"]
    definition = native_tool_definition("read_file")
    assert "complete" in definition["description"]
    assert any("not a content hash" in line for line in definition["constraints"])
    assert not any("int-coerces" in line for line in definition["legacy_behavior"])
