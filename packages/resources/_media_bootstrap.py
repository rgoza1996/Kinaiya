"""Trusted media hold-before-exec helper, without lifecycle authority.

The native owner supplies grant/config/control/read-only source descriptors.
Only the control and source survive as FD3/FD4 after the exact grant and all
checks. The parent still owns process, descriptor and publication settlement;
this helper neither reads media nor qualifies its eventual sandbox target.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import re
import signal
import socket
import stat
import sys

MAX_CONFIG_BYTES = 65_536
_FD = re.compile(r"[1-9][0-9]{0,9}\Z")
_FD_MAX = (1 << (ctypes.sizeof(ctypes.c_int) * 8 - 1)) - 1
_NONCE = re.compile(r"[0-9a-f]{32}\Z")
_CONFIG_KEYS = frozenset({"nonce", "parent_pid", "argv", "cwd", "env"})
_SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE


def _descriptor(value: str) -> int:
    if _FD.fullmatch(value) is None:
        raise ValueError
    fd = int(value)
    if not 2 < fd <= _FD_MAX:
        raise ValueError
    return fd


def _read_only(fd: int) -> bool:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    return flags & os.O_ACCMODE == os.O_RDONLY and not flags & os.O_PATH


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_number(_: str) -> None:
    raise ValueError


def _config(fd: int, size: int, nonce: str, parent: int):
    if os.lseek(fd, 0, os.SEEK_SET) != 0:
        raise ValueError
    parts, total = [], 0
    while total < size:
        chunk = os.read(fd, min(MAX_CONFIG_BYTES, size - total))
        if not chunk:
            raise ValueError
        total += len(chunk)
        if total > size or total > MAX_CONFIG_BYTES:
            raise ValueError
        parts.append(chunk)
    if os.read(fd, 1) != b"":
        raise ValueError
    cfg = json.loads(
        b"".join(parts).decode("utf-8", "strict"),
        object_pairs_hook=_pairs,
        parse_constant=_reject_number,
        parse_float=_reject_number,
    )
    if type(cfg) is not dict or cfg.keys() != _CONFIG_KEYS:
        raise ValueError
    if type(cfg["nonce"]) is not str or cfg["nonce"] != nonce:
        raise ValueError
    if type(cfg["parent_pid"]) is not int or cfg["parent_pid"] <= 0:
        raise ValueError
    if cfg["parent_pid"] != parent or os.getppid() != parent:
        raise ValueError
    if type(cfg["argv"]) is not list or type(cfg["env"]) is not dict:
        raise ValueError
    # Validate every scalar before the generic validator can touch a path.
    for value in (cfg["cwd"], *cfg["argv"], *cfg["env"].keys(), *cfg["env"].values()):
        if type(value) is not str:
            raise ValueError
        value.encode("utf-8", "strict")
    if __package__:
        from ._bootstrap import _valid_cfg
    else:
        # Absolute script invocation selects this immutable sibling directory;
        # no caller-provided import path or working-directory insertion.
        from _bootstrap import _valid_cfg

    validated = _valid_cfg(cfg, nonce)
    if validated is None:
        raise ValueError
    return validated


def _control(fd: int) -> None:
    control = socket.socket(fileno=fd)
    try:
        if (
            control.family != socket.AF_UNIX
            or control.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
        ):
            raise ValueError
        control.getpeername()
    finally:
        # Inspection borrows the exact original, never a second close owner.
        control.detach()


def _enrollment_fd(value: str) -> int:
    return _descriptor(value)


def _validate_enrollment(fd: int) -> None:
    # The outer bootstrap refusal handler also catches OS/ctypes failures.
    # Keep these checks before any grant wait or target/config/source read.
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    if flags & os.O_ACCMODE != os.O_WRONLY or flags & getattr(os, "O_PATH", 0):
        raise ValueError
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError
    link = os.readlink(f"/proc/self/fd/{fd}")
    if not link.endswith("/cgroup.procs"):
        raise ValueError
    import ctypes

    class _Statfs(ctypes.Structure):
        _fields_ = [("f_type", ctypes.c_long), ("_pad", ctypes.c_char * 512)]

    buffer = _Statfs()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(fd, ctypes.byref(buffer)) != 0 or buffer.f_type != 0x63677270:
        raise ValueError


def _run() -> int:
    if len(sys.argv) not in (6, 8):
        raise ValueError
    if len(sys.argv) == 8:
        if sys.argv[6] != "--kinaiya-cgroup-v1":
            raise ValueError
        _, grant_s, config_s, nonce, control_s, source_s, _, enroll_s = sys.argv
        grant_fd, config_fd, control_fd, source_fd = (
            _descriptor(value) for value in (grant_s, config_s, control_s, source_s)
        )
        enroll_fd = _enrollment_fd(enroll_s)
        originals5 = (grant_fd, config_fd, control_fd, source_fd, enroll_fd)
        if len(set(originals5)) != 5 or _NONCE.fullmatch(nonce) is None:
            raise ValueError
        # Validate owner-provided write-only cgroup2 cgroup.procs before grant.
        _validate_enrollment(enroll_fd)
        try:
            if os.write(enroll_fd, b"0") != 1:
                raise ValueError
        except BaseException:
            try:
                os.close(enroll_fd)
            except BaseException:
                # Preserve the original enrollment failure. Process exit owns
                # all remaining descriptors; never retry an ambiguous integer.
                pass
            raise
        # A successful write still needs positive retirement before grant/exec.
        # Any close error refuses execution; the possibly reused fd is not retried.
        os.close(enroll_fd)
        _, grant_s, config_s, nonce, control_s, source_s = sys.argv[0:6]
        grant_fd, config_fd, control_fd, source_fd = (
            _descriptor(value) for value in (grant_s, config_s, control_s, source_s)
        )
        originals = (grant_fd, config_fd, control_fd, source_fd)
        if len(set(originals)) != 4 or _NONCE.fullmatch(nonce) is None:
            raise ValueError
    else:
        _, grant_s, config_s, nonce, control_s, source_s = sys.argv
        grant_fd, config_fd, control_fd, source_fd = (
            _descriptor(value) for value in (grant_s, config_s, control_s, source_s)
        )
        originals = (grant_fd, config_fd, control_fd, source_fd)
        if len(set(originals)) != 4 or _NONCE.fullmatch(nonce) is None:
            raise ValueError
    parent = os.getppid()
    if not stat.S_ISFIFO(os.fstat(grant_fd).st_mode) or not _read_only(grant_fd):
        raise ValueError
    grant = os.read(grant_fd, 1)
    if grant == b"":
        return 0
    if grant != b"\x01" or os.read(grant_fd, 1) != b"":
        raise ValueError

    config_info, source_info = os.fstat(config_fd), os.fstat(source_fd)
    if (config_info.st_dev, config_info.st_ino) == (source_info.st_dev, source_info.st_ino):
        raise ValueError
    if not stat.S_ISREG(config_info.st_mode) or not 0 < config_info.st_size <= MAX_CONFIG_BYTES:
        raise ValueError
    if fcntl.fcntl(config_fd, fcntl.F_GET_SEALS) & _SEALS != _SEALS:
        raise ValueError
    if not stat.S_ISREG(source_info.st_mode) or not _read_only(source_fd):
        raise ValueError
    _control(control_fd)
    argv, cwd, env = _config(config_fd, config_info.st_size, nonce, parent)

    # Check the original parent around arming, including death during grant wait.
    if os.getppid() != parent:
        raise ValueError
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0 or os.getppid() != parent:
        raise ValueError

    # Preserve BOTH candidates above the targets before closing any original;
    # grant/config may occupy3/4, and control/source may themselves be swapped.
    control_tmp = fcntl.fcntl(control_fd, fcntl.F_DUPFD_CLOEXEC, 5)
    source_tmp = fcntl.fcntl(source_fd, fcntl.F_DUPFD_CLOEXEC, 5)
    for fd in originals:
        os.close(fd)
    os.dup2(control_tmp, 3, inheritable=True)
    os.dup2(source_tmp, 4, inheritable=True)
    os.close(control_tmp)
    os.close(source_tmp)
    os.chdir(cwd)
    os.execve(argv[0], argv, env)
    return 2


def main() -> None:
    try:
        code = _run()
    except BaseException:
        # Even overflow of a ten-digit fd, close ambiguity or partial remap must
        # refuse quietly. Process exit, not a reused integer retry, closes it.
        code = 2
    os._exit(code)


if __name__ == "__main__":
    main()
