"""Interface-first Linux CPU oracles for selected media bootstrap v2/v3.

The bootstrap implementation was not read while authoring these tests. Every
subprocess is a disposable direct child and is actually waited/reaped. Neither
a quiet control channel nor a timeout is treated as cleanup or native authority.
"""

import ctypes
import errno
import fcntl
import json
import os
import select
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "packages/resources/_media_bootstrap.py"
_NONCE = "a" * 32
_SOURCE = b"synthetic-original-source\0" + bytes(range(32))
_WAIT = 5
_C_INT_MAX = (1 << (8 * ctypes.sizeof(ctypes.c_int) - 1)) - 1
_SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE

# This trusted helper is the directly owned child itself. It execs the bootstrap
# in the same PID, so collision setup never changes the pytest parent's FDs and
# never leaves a grandchild whose retirement could be guessed from a timeout.
_COLLISION_PROGRAM = r"""
import fcntl
import os
import sys

python, script, layout_text = sys.argv[1:4]
grant, config, nonce, control, source = sys.argv[4:]
originals = [int(grant), int(config), int(control), int(source)]
layout = [int(value) for value in layout_text.split(',')]
temporaries = [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 64) for fd in originals]
for fd in originals:
    os.close(fd)
for temporary, target in zip(temporaries, layout):
    os.dup2(temporary, target, inheritable=True)
for fd in temporaries:
    os.close(fd)
os.execv(python, [python, '-B', script, str(layout[0]), str(layout[1]), nonce,
                  str(layout[2]), str(layout[3])])
"""

_TARGET_PROGRAM = r"""
import errno
import fcntl
import json
import os
import socket
import stat
import sys

marker, originals_text = sys.argv[1:]
with open(marker, 'xb') as file:
    file.write(b'target-executed')
control = socket.socket(fileno=3)
control.sendall(b'READY\n')
command = b''
while len(command) < 4:
    part = control.recv(4 - len(command))
    if not part:
        raise RuntimeError('test parent ended command')
    command += part
if command != b'PING':
    raise RuntimeError('unexpected synthetic command')
position = os.lseek(4, 0, os.SEEK_CUR)
flags = fcntl.fcntl(4, fcntl.F_GETFL)
write_errno = None
try:
    os.write(4, b'forbidden')
except OSError as error:
    write_errno = error.errno
inherited_originals = []
for fd in json.loads(originals_text):
    if fd > 4:
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            inherited_originals.append(fd)
report = {
    'source_hex': os.read(4, 128).hex(), 'source_position': position,
    'source_regular': stat.S_ISREG(os.fstat(4).st_mode),
    'source_readonly': flags & os.O_ACCMODE == os.O_RDONLY,
    'source_write_errno': write_errno, 'control_family': control.family,
    'control_type': control.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE),
    'control_inheritable': os.get_inheritable(3),
    'source_inheritable': os.get_inheritable(4),
    'inherited_originals': inherited_originals,
    'cwd': os.getcwd(), 'env': os.getenv('MEDIA_BOOTSTRAP_TEST'),
}
control.sendall(json.dumps(report, sort_keys=True).encode() + b'\n')
command = b''
while len(command) < 4:
    part = control.recv(4 - len(command))
    if not part:
        raise RuntimeError('test parent ended final command')
    command += part
if command != b'DONE':
    raise RuntimeError('unexpected synthetic final command')
os.close(4)
control.close()
"""


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


class _Child:
    """Own only local test resources and one actual direct Popen child."""

    def __init__(
        self,
        root,
        *,
        layout=None,
        config_change=None,
        raw_change=None,
        seals=_SEALS,
        config_kind="memfd",
        source_kind="readonly",
        control_kind="stream",
        grant_kind="pipe",
        args_change=None,
    ):
        self.root = root
        self.marker = root / "target-effect"
        self.source_path = root / "synthetic-source"
        self.source_path.write_bytes(_SOURCE)
        self.fds = set()
        self.sockets = []
        self.proc = None
        self.collected = False
        self.grant_writer = None
        try:
            self._setup(
                layout,
                config_change,
                raw_change,
                seals,
                config_kind,
                source_kind,
                control_kind,
                grant_kind,
                args_change,
            )
        except BaseException:
            self.close()
            raise

    def own(self, fd):
        self.fds.add(fd)
        return fd

    def pair(self, kind=socket.SOCK_STREAM):
        pair = socket.socketpair(socket.AF_UNIX, kind)
        self.sockets.extend(pair)
        return pair

    def close_fd(self, fd):
        if fd in self.fds:
            self.fds.remove(fd)
            os.close(fd)

    def _setup(
        self,
        layout,
        config_change,
        raw_change,
        seals,
        config_kind,
        source_kind,
        control_kind,
        grant_kind,
        args_change,
    ):
        grant_read, grant_write = os.pipe2(os.O_CLOEXEC)
        self.grant_fd = self.own(grant_read)
        self.grant_writer = self.own(grant_write)
        if grant_kind == "regular":
            path = self.root / "not-a-grant-pipe"
            path.write_bytes(b"\x01")
            self.grant_fd = self.own(os.open(path, os.O_RDONLY | os.O_CLOEXEC))
        elif grant_kind == "write_pipe":
            self.grant_fd = grant_write
        else:
            assert grant_kind == "pipe"

        self.control, self.control_child = self.pair()
        self.control_fd = self.control_child.fileno()
        if control_kind == "datagram":
            _, endpoint = self.pair(socket.SOCK_DGRAM)
            self.control_fd = endpoint.fileno()
        elif control_kind == "unconnected":
            endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sockets.append(endpoint)
            self.control_fd = endpoint.fileno()
        elif control_kind == "regular":
            self.control_fd = self.own(os.open(self.source_path, os.O_RDONLY | os.O_CLOEXEC))
        else:
            assert control_kind == "stream"

        self.source_fd = self.own(os.open(self.source_path, os.O_RDONLY | os.O_CLOEXEC))
        if source_kind == "writable":
            self.source_fd = self.own(os.open(self.source_path, os.O_RDWR | os.O_CLOEXEC))
        elif source_kind == "opath":
            self.source_fd = self.own(os.open(self.source_path, os.O_PATH | os.O_CLOEXEC))
        elif source_kind == "pipe":
            read, write = os.pipe2(os.O_CLOEXEC)
            self.source_fd = self.own(read)
            self.own(write)
            os.write(write, _SOURCE)
            self.close_fd(write)
        elif source_kind == "directory":
            self.source_fd = self.own(
                os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            )
        elif source_kind == "socket":
            _, endpoint = self.pair()
            self.source_fd = endpoint.fileno()
        else:
            assert source_kind in ("readonly", "config_alias")

        if config_kind == "memfd":
            self.config_fd = self.own(
                os.memfd_create("synthetic-media-config", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            )
        elif config_kind == "regular":
            self.config_fd = self.own(
                os.open(
                    self.root / "unsealed-config",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
            )
        else:
            raise AssertionError("unknown test config kind")
        if source_kind == "config_alias":
            self.source_fd = self.own(
                os.open(f"/proc/self/fd/{self.config_fd}", os.O_RDONLY | os.O_CLOEXEC)
            )

        originals = [self.grant_fd, self.config_fd, self.control_fd, self.source_fd]
        target_originals = list(layout) if layout is not None else originals
        config = {
            "nonce": _NONCE,
            "parent_pid": os.getpid(),
            "argv": [
                sys.executable,
                "-I",
                "-B",
                "-c",
                _TARGET_PROGRAM,
                str(self.marker),
                _json(target_originals).decode(),
            ],
            "cwd": str(self.root),
            "env": {"MEDIA_BOOTSTRAP_TEST": "synthetic-exec-env"},
        }
        if config_change is not None:
            config_change(config)
        raw = _json(config)
        if raw_change is not None:
            raw = raw_change(raw)
        self.config_size = len(raw)
        pending = memoryview(raw)
        while pending:
            written = os.write(self.config_fd, pending)
            assert written > 0
            pending = pending[written:]
        assert os.lseek(self.config_fd, 0, os.SEEK_CUR) == len(raw)
        if config_kind == "memfd" and seals:
            fcntl.fcntl(self.config_fd, fcntl.F_ADD_SEALS, seals)
        arguments = [
            str(originals[0]),
            str(originals[1]),
            _NONCE,
            str(originals[2]),
            str(originals[3]),
        ]
        if args_change is not None:
            arguments = args_change(arguments)
        if layout is None:
            command = [sys.executable, "-B", str(_SCRIPT), *arguments]
        else:
            assert args_change is None
            command = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                _COLLISION_PROGRAM,
                sys.executable,
                str(_SCRIPT),
                ",".join(map(str, layout)),
                *arguments,
            ]
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=tuple(originals),
        )
        self.control_child.close()

    def grant(self, data=b"\x01", *, close=True, allow_early_refusal=False):
        try:
            if data:
                assert os.write(self.grant_writer, data) == len(data)
        except BrokenPipeError:
            if not allow_early_refusal:
                raise
        finally:
            if close:
                self.close_grant()

    def close_grant(self):
        if self.grant_writer is not None:
            fd, self.grant_writer = self.grant_writer, None
            self.close_fd(fd)

    def assert_held(self):
        assert self.proc.poll() is None
        assert select.select([self.control], [], [], 0.15)[0] == []
        assert self.proc.poll() is None
        assert not self.marker.exists()
        assert os.lseek(self.source_fd, 0, os.SEEK_CUR) == 0

    def line(self):
        deadline = time.monotonic() + _WAIT
        data = bytearray()
        while not data.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            assert remaining > 0, "bounded target-reply harness expired"
            self.control.settimeout(remaining)
            part = self.control.recv(1)
            assert part, "control ended without the target's complete reply"
            data.extend(part)
            assert len(data) <= 4096
        return bytes(data)

    def expect_exit(self, expected):
        try:
            stdout, stderr = self.proc.communicate(timeout=_WAIT)
            self.collected = True
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate(timeout=_WAIT)  # Actual wait/reap after kill.
            self.collected = True
            raise AssertionError("owned child failed to exit; killed and actually reaped") from None
        assert self.proc.returncode == expected, (self.proc.returncode, stdout, stderr)
        assert stdout == stderr == b""
        if expected != 0 or not self.marker.exists():
            assert not self.marker.exists()

    def finish_target(self):
        assert self.line() == b"READY\n"
        assert self.marker.read_bytes() == b"target-executed"
        self.control.sendall(b"PING")
        report = json.loads(self.line())
        assert report == {
            "source_hex": _SOURCE.hex(),
            "source_position": 0,
            "source_regular": True,
            "source_readonly": True,
            "source_write_errno": errno.EBADF,
            "control_family": socket.AF_UNIX,
            "control_type": socket.SOCK_STREAM,
            "control_inheritable": True,
            "source_inheritable": True,
            "inherited_originals": [],
            "cwd": str(self.root.resolve()),
            "env": "synthetic-exec-env",
        }
        self.control.sendall(b"DONE")
        self.expect_exit(0)
        assert self.source_path.read_bytes() == _SOURCE
        return report

    def close(self):
        # On every assertion/setup failure retire this directly owned PID, then
        # positively wait it. A timeout is a test failure, never a reap receipt.
        try:
            if self.proc is not None:
                if self.proc.poll() is None:
                    self.proc.kill()
                if self.collected:
                    self.proc.wait(timeout=_WAIT)
                else:
                    self.proc.communicate(timeout=_WAIT)
                    self.collected = True
                assert self.proc.returncode is not None
        finally:
            for endpoint in self.sockets:
                endpoint.close()
            for fd in tuple(self.fds):
                self.close_fd(fd)


@contextmanager
def _child(root, **options):
    child = _Child(root, **options)
    try:
        yield child
    finally:
        child.close()


def test_original_grant_and_held_writer_gate_actual_exec_and_source_position(tmp_path):
    with _child(tmp_path) as child:
        child.assert_held()
        child.grant(close=False)
        child.assert_held()  # 0x01 alone is not a release while its writer remains open.
        child.close_grant()
        child.finish_target()


def test_initial_grant_eof_exits_zero_without_target_exec(tmp_path):
    with _child(tmp_path) as child:
        child.close_grant()
        child.expect_exit(0)
        assert not child.marker.exists()
        assert os.lseek(child.source_fd, 0, os.SEEK_CUR) == 0


@pytest.mark.parametrize("data", [b"\x00", b"\x02", b"\x01\x00", b"\x01\x01", b"\x01trailing"])
def test_wrong_or_trailing_grant_bytes_refuse_without_exec(tmp_path, data):
    with _child(tmp_path) as child:
        child.grant(data)
        child.expect_exit(2)
        assert os.lseek(child.source_fd, 0, os.SEEK_CUR) == 0


@pytest.mark.parametrize("kind", ["regular", "write_pipe"])
def test_grant_must_be_actual_readonly_pipe_before_its_first_read(tmp_path, kind):
    with _child(tmp_path, grant_kind=kind) as child:
        child.expect_exit(2)  # Original writer is deliberately still held by this fixture.
        if kind == "regular":
            assert os.lseek(child.grant_fd, 0, os.SEEK_CUR) == 0


@pytest.mark.parametrize("layout", [(5, 6, 3, 4), (5, 6, 4, 3), (3, 4, 5, 6), (4, 3, 5, 6)])
def test_collision_layouts_remap_only_inside_the_owned_helper_process(tmp_path, layout):
    with _child(tmp_path, layout=layout) as child:
        child.grant()
        child.finish_target()


def test_sealed_config_at_exact_media_cap_is_seeked_from_end_and_executes(tmp_path):
    def pad(raw):
        assert 0 < len(raw) < 65536
        return raw + b" " * (65536 - len(raw))

    with _child(tmp_path, raw_change=pad) as child:
        assert child.config_size == 65536
        child.grant()
        child.finish_target()


@pytest.mark.parametrize(
    "missing", [fcntl.F_SEAL_SEAL, fcntl.F_SEAL_SHRINK, fcntl.F_SEAL_GROW, fcntl.F_SEAL_WRITE]
)
def test_config_requires_each_of_the_four_kernel_seals(tmp_path, missing):
    with _child(tmp_path, seals=_SEALS & ~missing) as child:
        child.grant()
        child.expect_exit(2)


def test_regular_config_file_cannot_impersonate_native_sealed_memfd(tmp_path):
    with _child(tmp_path, config_kind="regular") as child:
        child.grant()
        child.expect_exit(2)


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "oversized",
        "invalid_utf8",
        "duplicate",
        "duplicate_env",
        "nonfinite",
        "trailing_json",
    ],
)
def test_malformed_complete_config_refuses_silently_without_target_exec(tmp_path, kind):
    def change(raw):
        if kind == "empty":
            return b""
        if kind == "oversized":
            return raw + b" " * (65537 - len(raw))
        if kind == "invalid_utf8":
            return b"\xff"
        if kind == "duplicate":
            return raw[:-1] + b',"nonce":"' + _NONCE.encode() + b'"}'
        if kind == "duplicate_env":
            return raw.replace(
                b'"MEDIA_BOOTSTRAP_TEST":"synthetic-exec-env"',
                b'"MEDIA_BOOTSTRAP_TEST":"first","MEDIA_BOOTSTRAP_TEST":"synthetic-exec-env"',
            )
        if kind == "nonfinite":
            return raw.replace(b'"parent_pid":' + str(os.getpid()).encode(), b'"parent_pid":NaN', 1)
        assert kind == "trailing_json"
        return raw + b"{}"

    with _child(tmp_path, raw_change=change) as child:
        child.grant()
        child.expect_exit(2)


@pytest.mark.parametrize("field", ["argv", "cwd", "env_key", "env_value"])
def test_escaped_lone_surrogates_are_rejected_as_utf8_scalars_before_exec(tmp_path, field):
    def change(config):
        if field == "argv":
            config["argv"].append("\ud800")
        elif field == "cwd":
            config["cwd"] = "\ud800"
        elif field == "env_key":
            config["env"] = {"\ud800": "synthetic"}
        else:
            config["env"] = {"MEDIA_BOOTSTRAP_TEST": "\ud800"}

    with _child(tmp_path, config_change=change) as child:
        child.grant()
        child.expect_exit(2)


@pytest.mark.parametrize(
    "case",
    [
        "extra",
        "nonce",
        "parent",
        "parent_bool",
        "parent_float",
        "argv_scalar",
        "argv_item",
        "cwd_type",
        "env_type",
        "env_key",
        "env_value",
        "exec_missing",
        "cwd_missing",
    ],
)
def test_closed_strict_parent_and_exec_config_refusals(tmp_path, case):
    def change(config):
        if case == "extra":
            config["owner_authorized"] = True
        elif case == "nonce":
            config["nonce"] = "b" * 32
        elif case == "parent":
            config["parent_pid"] = os.getpid() + 1
        elif case == "parent_bool":
            config["parent_pid"] = True
        elif case == "parent_float":
            config["parent_pid"] = float(os.getpid())
        elif case == "argv_scalar":
            config["argv"] = "not-an-argv-list"
        elif case == "argv_item":
            config["argv"].append(1)
        elif case == "cwd_type":
            config["cwd"] = None
        elif case == "env_type":
            config["env"] = []
        elif case == "env_key":
            config["env"] = {"bad\0key": "synthetic"}
        elif case == "env_value":
            config["env"] = {"MEDIA_BOOTSTRAP_TEST": 1}
        elif case == "exec_missing":
            config["argv"][0] = str(tmp_path / "no-such-executable")
        else:
            config["cwd"] = str(tmp_path / "no-such-directory")

    with _child(tmp_path, config_change=change) as child:
        child.grant()
        child.expect_exit(2)


@pytest.mark.parametrize("field", ["nonce", "parent_pid", "argv", "cwd", "env"])
def test_every_config_field_is_required(tmp_path, field):
    with _child(tmp_path, config_change=lambda config: config.pop(field)) as child:
        child.grant()
        child.expect_exit(2)


def test_distinct_config_source_fds_with_same_inode_refuse_before_config_seek(tmp_path):
    with _child(tmp_path, source_kind="config_alias") as child:
        assert child.config_fd != child.source_fd
        config, source = os.fstat(child.config_fd), os.fstat(child.source_fd)
        assert (config.st_dev, config.st_ino) == (source.st_dev, source.st_ino)
        assert fcntl.fcntl(child.source_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        assert child.config_size > 1
        before = os.lseek(child.config_fd, 1, os.SEEK_SET)
        assert before == 1  # Reading the complete config would move this shared cursor.
        child.grant()
        child.expect_exit(2)
        assert os.lseek(child.config_fd, 0, os.SEEK_CUR) == before


@pytest.mark.parametrize("kind", ["writable", "opath", "pipe", "directory", "socket"])
def test_source_requires_a_readable_readonly_regular_original_descriptor(tmp_path, kind):
    with _child(tmp_path, source_kind=kind) as child:
        child.grant()
        child.expect_exit(2)


@pytest.mark.parametrize("kind", ["datagram", "unconnected", "regular"])
def test_control_requires_a_connected_unix_stream_socket(tmp_path, kind):
    with _child(tmp_path, control_kind=kind) as child:
        child.grant()
        child.expect_exit(2)


@pytest.mark.parametrize(
    "value", ["0", "1", "2", "03", "+3", "-3", "3.0", "٣", "00000000003", "99999999999"]
)
def test_descriptor_spelling_range_or_actual_invalid_fd_is_silent_refusal(tmp_path, value):
    def change(arguments):
        arguments[0] = value
        return arguments

    with _child(tmp_path, args_change=change) as child:
        child.expect_exit(2)


@pytest.mark.parametrize("position", [0, 1, 3, 4])
@pytest.mark.parametrize("value", [str(_C_INT_MAX + 1), "9999999999"])
@pytest.mark.parametrize("grant_state", ["held", "eof", "released"])
def test_every_descriptor_c_int_bound_precedes_any_grant_read(
    tmp_path, position, value, grant_state
):
    def change(arguments):
        arguments[position] = value
        return arguments

    with _child(tmp_path, args_change=change) as child:
        if grant_state == "eof":
            child.close_grant()
        elif grant_state == "released":
            child.grant(allow_early_refusal=True)
        child.expect_exit(2)


@pytest.mark.parametrize("position", [1, 3, 4])
def test_c_int_max_is_structurally_valid_before_initial_eof_without_exec(tmp_path, position):
    def change(arguments):
        arguments[position] = str(_C_INT_MAX)
        return arguments

    with _child(tmp_path, args_change=change) as child:
        child.close_grant()
        child.expect_exit(0)
        assert not child.marker.exists()


def test_structurally_valid_but_unbound_grant_fd_is_refused_before_read(tmp_path):
    def change(arguments):
        arguments[0] = str(_C_INT_MAX)
        return arguments

    with _child(tmp_path, args_change=change) as child:
        child.expect_exit(2)


@pytest.mark.parametrize("position", [1, 3, 4])
def test_unbound_non_grant_descriptor_refuses_after_an_exact_grant(tmp_path, position):
    def change(arguments):
        arguments[position] = str(_C_INT_MAX)
        return arguments

    with _child(tmp_path, args_change=change) as child:
        child.grant(allow_early_refusal=True)
        child.expect_exit(2)


@pytest.mark.parametrize("index,other", [(0, 1), (1, 0), (3, 4), (4, 3)])
def test_four_descriptor_arguments_must_be_distinct(tmp_path, index, other):
    def change(arguments):
        arguments[index] = arguments[other]
        return arguments

    with _child(tmp_path, args_change=change) as child:
        child.expect_exit(2)


@pytest.mark.parametrize("nonce", ["A" * 32, "g" * 32, "a" * 31, "a" * 33])
def test_nonce_argument_is_exact_lowercase_hex(tmp_path, nonce):
    def change(arguments):
        arguments[2] = nonce
        return arguments

    with _child(tmp_path, args_change=change) as child:
        child.expect_exit(2)


@pytest.mark.parametrize("extra", [False, True])
def test_bootstrap_requires_exactly_five_arguments(tmp_path, extra):
    with _child(
        tmp_path, args_change=lambda arguments: arguments + ["extra"] if extra else arguments[:-1]
    ) as child:
        child.expect_exit(2)
