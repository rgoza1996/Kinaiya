"""Trusted stdio relay helpers (stdlib only).

Sits logically between the MCP SDK (ClientSession memory streams) and the
bubblewrap child. No third-party imports: only the standard library, so the
framing and argv-hiding logic is auditable without SDK/anyio behavior.

Responsibilities:
- Byte caps for JSONL frames BEFORE any JSON decode (stdout and stdin).
- Hard byte cap for stderr capture.
- Secure bubblewrap env delivery: secrets travel in an anonymous memfd read
  via ``bwrap --args FD`` (NUL-separated), never in ``argv``/``ps`` output.
- Process-group kill helpers (caller performs the actual kill/reap so the
  async runtime owns the child; functions here only compute argv/FD layout
  and frame decisions).

Caps are intentionally fixed constants so tests can reason about them.
"""

from __future__ import annotations

import os

MAX_STDOUT_FRAME_BYTES = 512 * 1024
MAX_INPUT_FRAME_BYTES = 256 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_CONSECUTIVE_EMPTY_READS = 0  # reserved; relay never blocks forever


def frame_complete(
    buffer: bytearray, cap: int = MAX_STDOUT_FRAME_BYTES
) -> tuple[list[bytes], bool]:
    """Split complete ``\\n``-terminated frames off ``buffer``.

    Returns ``(frames, over_cap)``. ``over_cap`` is True when the buffered
    prefix exceeds ``cap`` without a newline (unterminated/oversized frame):
    the caller must kill the child and NOT forward anything.
    Complete frames longer than ``cap`` are also reported via ``over_cap``
    (caller kills; no partial forwarding).
    """
    frames: list[bytes] = []
    over_cap = False
    while True:
        idx = buffer.find(b"\n")
        if idx < 0:
            if len(buffer) > cap:
                over_cap = True
            break
        line = bytes(buffer[:idx])
        del buffer[: idx + 1]
        if len(line) > cap:
            over_cap = True
            frames.clear()
            break
        if line:
            frames.append(line)
    return frames, over_cap


def build_secret_args(secret_env: dict[str, str]) -> bytes:
    """NUL-joined ``--setenv NAME VALUE`` args for ``bwrap --args FD``."""
    parts: list[bytes] = []
    for name in sorted(secret_env):
        if "\x00" in name or "\x00" in secret_env[name]:
            raise ValueError("NUL is forbidden in environment values")
        parts.extend([b"--setenv", name.encode(), secret_env[name].encode()])
    return b"\x00".join(parts) + (b"\x00" if parts else b"")


def create_args_memfd(secret_env: dict[str, str]) -> int:
    """Create a sealed-ish memfd holding secret args. Caller must close it."""
    payload = build_secret_args(secret_env)
    fd = os.memfd_create("mcp-bwrap-args")
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def argv_contains_secret(argv: list[str], secrets: list[str]) -> str | None:
    """Return the first secret found verbatim in argv, else None."""
    for entry in argv:
        for secret in secrets:
            if secret and secret in entry:
                return secret
    return None
