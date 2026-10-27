"""Hold-before-exec bootstrap for owner-controlled workers.

Trusted immutable first-party package code (not generated worker content).
Launched as ``sys.executable <abs path> <grant_fd> <config_fd> <nonce>`` with
a private grant pipe read end and a sealed config fd. Waits for one grant
byte; EOF/close/mismatch/invalid config exits without exec. After the grant,
validates bounded JSON/nonce, closes control fds, chdir, then ``os.execve``.
No shell, no inherited parent env/secrets on the command line.
"""

from __future__ import annotations

import json
import os
import re
import sys

MAX_CONFIG_BYTES = 1048576
GRANT_BYTE = b"\x01"
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,255}")


def _die(code: int) -> None:
    try:
        os._exit(code)
    except Exception:
        sys.exit(code)


def _valid_cfg(cfg: object, nonce: str) -> tuple[list[str], str, dict[str, str]] | None:
    if not isinstance(cfg, dict):
        return None
    try:
        if cfg.get("nonce") != nonce:
            return None
        argv = cfg.get("argv")
        cwd = cfg.get("cwd")
        env = cfg.get("env")
        if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
            return None
        if any(not isinstance(a, str) or not a or len(a) > 32768 or "\x00" in a for a in argv):
            return None
        if sum(map(len, argv)) > 262144:
            return None
        if not (os.path.isabs(argv[0]) and os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
            return None
        if (
            not isinstance(cwd, str)
            or "\x00" in cwd
            or not os.path.isabs(cwd)
            or not os.path.isdir(cwd)
        ):
            return None
        if not isinstance(env, dict) or len(env) > 128:
            return None
        total = 0
        for k, v in env.items():
            if not isinstance(k, str) or not _ENV_KEY_RE.fullmatch(k) or "\x00" in k:
                return None
            if not isinstance(v, str) or len(v) > 65536 or "\x00" in v:
                return None
            total += len(k.encode("utf-8")) + len(v.encode("utf-8"))
        if total > 262144:
            return None
        return list(argv), cwd, dict(env)
    except Exception:
        return None


def main() -> None:
    if len(sys.argv) != 4:
        _die(2)
    _, grant_s, config_s, nonce = sys.argv
    try:
        grant_fd, config_fd = int(grant_s), int(config_s)
    except ValueError:
        _die(2)
    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce.lower()):
        _die(2)
    try:
        data = os.read(grant_fd, 1)
    except OSError:
        _die(0)
    if data != GRANT_BYTE:
        _die(0 if data == b"" else 2)
    try:
        try:
            os.lseek(config_fd, 0, os.SEEK_SET)
        except OSError:
            pass
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(config_fd, 65536)
            except OSError:
                _die(2)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                _die(2)
            chunks.append(chunk)
        cfg = json.loads(b"".join(chunks).decode("utf-8"))
    except SystemExit:
        raise
    except BaseException:
        _die(2)
    validated = _valid_cfg(cfg, nonce)
    if validated is None:
        _die(2)
    argv, cwd, env = validated
    for fd in (grant_fd, config_fd):
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        os.chdir(cwd)
    except OSError:
        _die(2)
    try:
        os.execve(argv[0], argv, env)
    except OSError:
        _die(2)
    _die(2)


if __name__ == "__main__":
    main()
