"""Small bounded Whistle protocol; no HTTP, credentials, audio files or downloads."""

import json
import os
import re
import socket
import stat
import struct
from pathlib import Path

MAX_CONTROL_BYTES = 4096
MAX_RESPONSE_BYTES = 128 * 1024
VERSION = 1


def socket_path(directory, pid):
    path = Path(directory) / f"{pid}.sock"
    if len(os.fsencode(path)) > 100:
        raise ValueError("Whistle requires a short private Unix socket path")
    return path


def private_directory(directory):
    path = Path(directory)
    info = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Whistle IPC directory must be owner-private")
    return path


def peer_pid(connection, expected_pid):
    pid, uid, _gid = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    )
    if pid != expected_pid or uid != os.geteuid():
        raise ValueError("Whistle IPC peer does not match the owned process")
    return pid


def token(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate IPC field")
        result[key] = value
    return result


def decode_json(raw, limit=MAX_CONTROL_BYTES):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= limit:
        raise ValueError("Whistle IPC message exceeds its bound")

    def invalid(value):
        raise ValueError("Non-finite IPC number")

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=invalid)
    except RecursionError as exc:
        raise ValueError("Whistle IPC nesting exceeds its bound") from exc
    if not isinstance(result, dict):
        raise ValueError("Whistle IPC requires an object")
    return result


def encode_json(value, limit=MAX_CONTROL_BYTES):
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
    if not 1 <= len(raw) <= limit:
        raise ValueError("Whistle IPC message exceeds its bound")
    return raw


def _read_exact(fd, count):
    result = bytearray()
    while len(result) < count:
        chunk = os.read(fd, count - len(result))
        if not chunk:
            raise EOFError("Whistle IPC closed")
        result.extend(chunk)
    return bytes(result)


def read_frame(fd, limit):
    size = struct.unpack("!I", _read_exact(fd, 4))[0]
    if not 1 <= size <= limit:
        raise ValueError("Whistle IPC frame exceeds its bound")
    return _read_exact(fd, size)


def write_frame(fd, raw, limit):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= limit:
        raise ValueError("Whistle IPC frame exceeds its bound")
    view = memoryview(struct.pack("!I", len(raw)) + raw)
    while view:
        sent = os.write(fd, view)
        if sent <= 0:
            raise EOFError("Whistle IPC closed")
        view = view[sent:]


async def receive_frame(reader, limit):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 1 <= size <= limit:
        raise ValueError("Whistle IPC frame exceeds its bound")
    return await reader.readexactly(size)


async def send_frame(writer, raw, limit):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= limit:
        raise ValueError("Whistle IPC frame exceeds its bound")
    writer.write(struct.pack("!I", len(raw)))
    writer.write(raw)
    await writer.drain()
