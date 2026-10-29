"""Managed dialogue parent: bounded transport and exact owned-child lifecycle."""

import asyncio
import os
import re
import secrets
import stat
import tempfile
import weakref
from contextlib import aclosing, asynccontextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from packages.providers import qwen3_ipc as wire
from packages.providers.qwen3 import PUBLIC_ERROR_CODES, public_event
from packages.providers.whistle_assets import outside_workspace
from packages.resources.processes import WorkerSpec
from services.resources.managed_text import ManagedTextBroker


class ManagedQwen3Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    python: str = Field(min_length=1, max_length=4096)
    model_dir: str = Field(min_length=1, max_length=4096)
    cpu_affinity_count: int = Field(default=2, ge=2, le=2)
    memory_bytes: int = Field(default=16 * 1024**3, ge=8 * 1024**3, le=32 * 1024**3)
    host_memory_reserve_bytes: int = Field(default=8 * 1024**3, ge=8 * 1024**3, le=32 * 1024**3)
    max_input_tokens: int = Field(default=2048, ge=64, le=4096)
    max_new_tokens: int = Field(default=128, ge=16, le=512)
    startup_timeout_s: float = Field(default=120, ge=1, le=300)
    request_timeout_s: float = Field(default=120, ge=1, le=900)


def _snapshot(path, expected=None, *, complete=False, remove=False):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("Invalid owned Qwen snapshot")
    if expected is not None and (info.st_dev, info.st_ino) != expected:
        raise ValueError("Qwen snapshot incarnation changed")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        bound = os.fstat(fd)
        if (bound.st_dev, bound.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("Qwen snapshot changed during inspection")
        names = set(os.listdir(fd))
        if not names <= set(wire.MANIFEST["assets"]) or (
            complete and names != set(wire.MANIFEST["assets"])
        ):
            raise ValueError("Unexpected Qwen snapshot entry")
        for name in names:
            row = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (
                not stat.S_ISLNK(row.st_mode)
                or row.st_uid != os.geteuid()
                or re.fullmatch(r"/proc/self/fd/[0-9]+", os.readlink(name, dir_fd=fd)) is None
            ):
                raise ValueError("Invalid sealed Qwen snapshot link")
        if remove:
            for name in names:
                os.unlink(name, dir_fd=fd)
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino):
                raise ValueError("Qwen snapshot changed before removal")
            path.rmdir()
    finally:
        os.close(fd)
    return info.st_dev, info.st_ino


class Qwen3Endpoint:
    def __init__(self, directory, *, max_input_tokens=2048, max_new_tokens=128):
        self.directory = wire.private_directory(directory)
        self.limits = wire.validate_limits(max_input_tokens, max_new_tokens)
        self.ready = None
        self.bindings = weakref.WeakKeyDictionary()
        self.released_identities = weakref.WeakSet()

    def ready_for(self, identity):
        if self.ready is None or self.ready[0] != identity.pid:
            raise ValueError("Qwen ready identity is unavailable")
        self.bindings[identity] = self.ready
        return self.ready

    async def release_after_stop(self, identity, result):
        if identity is not None and identity in self.released_identities:
            return True
        proof = result.get("stop_evidence") or {}
        if not result.get("ok") or not (
            result.get("superseded")
            or result.get("release_confirmed")
            or result.get("already")
            or (proof.get("reaped") and proof.get("group_empty"))
        ):
            return False
        ready = self.bindings.get(identity) if identity is not None else None
        if ready is None and self.ready is not None:
            if result.get("superseded") or proof.get("pid") != self.ready[0]:
                return False
            ready = self.ready
        if ready is not None:
            self.forget(*ready[:2])
        if identity is not None:
            self.released_identities.add(identity)
        return True

    @asynccontextmanager
    async def connection(self, pid):
        reader, writer = await asyncio.open_unix_connection(
            str(wire.socket_path(self.directory, pid))
        )
        try:
            wire.peer_pid(writer.get_extra_info("socket"), pid)
            yield reader, writer
        finally:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 0.5)

    async def probe(self, spec, process):
        if process.returncode is not None:
            return False
        try:
            async with self.connection(process.pid) as (reader, writer):
                hello = wire.decode_json(
                    await wire.receive_frame(reader, wire.MAX_FRAME_BYTES), wire.MAX_FRAME_BYTES
                )
                incarnation = wire.parse_hello(hello, process.pid, **self.limits)
                nonce = secrets.token_hex(32)
                await wire.send_frame(
                    writer,
                    wire.encode_json(
                        wire.request_message("probe", incarnation, nonce), wire.MAX_REQUEST_BYTES
                    ),
                    wire.MAX_REQUEST_BYTES,
                )
                wire.parse_event(
                    wire.decode_json(
                        await wire.receive_frame(reader, wire.MAX_FRAME_BYTES), wire.MAX_FRAME_BYTES
                    ),
                    process.pid,
                    incarnation,
                    nonce,
                )
            if process.returncode is not None:
                return False
            sock = wire.socket_path(self.directory, process.pid).lstat()
            if not stat.S_ISSOCK(sock.st_mode) or sock.st_uid != os.geteuid():
                return False
            snapshot = _snapshot(wire.snapshot_path(self.directory, process.pid), complete=True)
            previous = self.ready
            if previous is not None and previous[:2] != (process.pid, incarnation):
                self.forget(*previous[:2])
            self.ready = (process.pid, incarnation, sock.st_dev, sock.st_ino, *snapshot)
            return True
        except (
            OSError,
            ValueError,
            RuntimeError,
            EOFError,
            TimeoutError,
            asyncio.IncompleteReadError,
        ):
            return False

    async def stream(self, messages, identity):
        current = self.ready
        if current is None or current[0] != identity.pid:
            raise RuntimeError("Qwen ready identity is unavailable")
        pid, incarnation = current[:2]
        nonce, count, text_bytes, received = secrets.token_hex(32), 0, 0, 0
        request = wire.request_message("generate", incarnation, nonce, messages)
        async with self.connection(pid) as (reader, writer):
            hello = wire.decode_json(
                await wire.receive_frame(reader, wire.MAX_FRAME_BYTES), wire.MAX_FRAME_BYTES
            )
            if wire.parse_hello(hello, pid, **self.limits) != incarnation:
                raise ValueError("Qwen stream incarnation changed")
            await wire.send_frame(
                writer, wire.encode_json(request, wire.MAX_REQUEST_BYTES), wire.MAX_REQUEST_BYTES
            )
            while True:
                raw = await wire.receive_frame(reader, wire.MAX_FRAME_BYTES)
                received += len(raw) + 4
                if received > 2 * 1024**2:
                    raise ValueError("Qwen IPC stream exceeds its wire byte bound")
                value = wire.parse_event(
                    wire.decode_json(raw, wire.MAX_FRAME_BYTES),
                    pid,
                    incarnation,
                    nonce,
                    seq=count,
                    **self.limits,
                )
                count += 1
                if self.ready != current:
                    raise ValueError("Qwen stream readiness changed")
                if value["kind"] == "delta":
                    text_bytes += len(value["text"].encode("utf-8"))
                    if text_bytes > wire.MAX_TEXT_BYTES:
                        raise ValueError("Qwen text exceeds its byte bound")
                    yield value
                else:
                    if await reader.read(1):
                        raise ValueError("Qwen returned data after its terminal")
                    yield value
                    return

    def forget(self, pid, incarnation):
        current = self.ready
        if current is None or current[:2] != (pid, incarnation):
            return
        path, snapshot = (
            wire.socket_path(self.directory, pid),
            wire.snapshot_path(self.directory, pid),
        )
        try:
            _snapshot(snapshot, current[4:])
        except FileNotFoundError:
            pass
        try:
            row = path.lstat()
            if (
                not stat.S_ISSOCK(row.st_mode)
                or row.st_uid != os.geteuid()
                or (row.st_dev, row.st_ino) != current[2:4]
            ):
                raise ValueError("Qwen socket incarnation changed")
        except FileNotFoundError:
            pass
        try:
            _snapshot(snapshot, current[4:], remove=True)
        except FileNotFoundError:
            pass
        path.unlink(missing_ok=True)
        self.ready = None

    def close(self):
        # Caller must first confirm manager.close released every owned group.
        entries = list(self.directory.iterdir())
        if len(entries) > 128:
            raise ValueError("Qwen IPC cleanup exceeds its entry bound")
        for path in entries:
            row = path.lstat()
            if (
                re.fullmatch(r"[1-9][0-9]*\.sock", path.name)
                and stat.S_ISSOCK(row.st_mode)
                and row.st_uid == os.geteuid()
            ):
                path.unlink()
            elif re.fullmatch(r"model-[1-9][0-9]*", path.name):
                _snapshot(path, remove=True)
            else:
                raise ValueError("Unexpected Qwen IPC directory entry")
        self.directory.rmdir()


def validate_qwen3_inputs(settings, workspace):
    """Fresh-start metadata gate; only the fenced child copies/hashes model bytes."""
    outside_workspace(settings.python, workspace)
    model = outside_workspace(settings.model_dir, workspace)
    if not model.is_dir():
        raise ValueError("Qwen requires its fixed local model directory")
    # Parent checks metadata only; the child hashes/seals exact model bytes once.
    for name, pin in wire.MANIFEST["assets"].items():
        path = outside_workspace(model / name, workspace)
        row = path.lstat()
        if not stat.S_ISREG(row.st_mode) or row.st_size != pin["bytes"]:
            raise ValueError("Qwen model input size/type mismatch")
    return model


def qwen3_worker(settings, root, workspace):
    validate_qwen3_inputs(settings, workspace)
    directory = Path(tempfile.mkdtemp(prefix="kinaiya-qwen3-", dir="/tmp"))
    try:
        outside_workspace(directory, workspace)
        endpoint = Qwen3Endpoint(
            directory,
            max_input_tokens=settings.max_input_tokens,
            max_new_tokens=settings.max_new_tokens,
        )
        spec = WorkerSpec(
            id="qwen3",
            argv=(
                settings.python,
                "-m",
                "packages.providers.qwen3_worker",
                "--socket-dir",
                str(directory),
                "--model-dir",
                settings.model_dir,
                "--workspace",
                str(workspace),
                "--cpu-count",
                str(settings.cpu_affinity_count),
                "--memory-bytes",
                str(settings.memory_bytes),
                "--max-input-tokens",
                str(settings.max_input_tokens),
                "--max-new-tokens",
                str(settings.max_new_tokens),
            ),
            cwd=root,
            startup_timeout_s=settings.startup_timeout_s,
            env={
                "PYTHONPATH": str(root),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "TOKENIZERS_PARALLELISM": "false",
            },
        )
        return spec, endpoint
    except BaseException:
        directory.rmdir()
        raise


class Qwen3Protocol:
    worker_id = "qwen3"
    max_request_bytes = wire.MAX_REQUEST_BYTES

    def __init__(self, endpoint):
        self.endpoint = endpoint

    def policy(self, coordinator):
        return coordinator.qwen3_policy()

    async def admit(self, coordinator, policy):
        return await coordinator.admit_qwen3(policy)

    def ready_for(self, identity):
        return self.endpoint.ready_for(identity)

    def current(self, lease, identity):
        if lease is None or self.endpoint.ready != lease or lease[0] != identity.pid:
            raise ValueError("Qwen endpoint incarnation changed")

    def status(self):
        return {
            "profile": wire.PROFILE,
            "model": wire.MODEL,
            "manifest_sha256": wire.MANIFEST_SHA256,
            **self.endpoint.limits,
        }

    def parse(self, raw):
        value = wire.decode_json(raw, wire.MAX_REQUEST_BYTES)
        if set(value) != {"messages"}:
            raise ValueError("Unexpected Qwen request field")
        return wire.validate_messages(value["messages"])

    def error(self, code):
        if code not in PUBLIC_ERROR_CODES:
            code = "generation_failed"
        return wire.encode_json({"kind": "error", "code": code}, wire.MAX_FRAME_BYTES) + b"\n"

    async def stop_legacy(self, manager, identity, lease):
        result = await manager.stop_if_current(identity)
        proof = result.get("stop_evidence") or {}
        if not result.get("ok") or not (
            result.get("superseded")
            or result.get("release_confirmed")
            or (proof.get("reaped") and proof.get("group_empty"))
        ):
            raise RuntimeError("Qwen process/group release is unconfirmed")
        ready = lease if lease is not None else self.endpoint.ready
        if ready is not None and ready[0] == identity.pid:
            self.endpoint.forget(identity.pid, ready[1])

    async def stream(self, messages, identity, lease):
        from services.resources.managed_text import GenerationRefusal

        terminal, total, count = None, 0, 0
        async with aclosing(self.endpoint.stream(messages, identity)) as source:
            async for value in source:
                if terminal is not None:
                    raise ValueError("Qwen returned data after a terminal")
                kind = value["kind"]
                fields = (
                    {"kind", "text"}
                    if kind == "delta"
                    else (
                        {"kind", "code"}
                        if kind == "error"
                        else {"kind", "finish_reason", "input_tokens", "output_tokens"}
                    )
                )
                frame = public_event(
                    {key: value[key] for key in fields}, count, **self.endpoint.limits
                )
                count += 1
                if kind == "delta":
                    total += len(frame["text"].encode("utf-8"))
                    if total > wire.MAX_TEXT_BYTES:
                        raise ValueError("Qwen aggregate output exceeds its bound")
                    yield wire.encode_json(frame, wire.MAX_FRAME_BYTES) + b"\n"
                else:
                    terminal = frame
        if terminal is None:
            raise ValueError("Qwen ended without a terminal")
        if terminal["kind"] == "error":
            raise GenerationRefusal(terminal["code"])
        yield wire.encode_json(terminal, wire.MAX_FRAME_BYTES) + b"\n"


class Qwen3Broker(ManagedTextBroker):
    def __init__(self, coordinator, endpoint, *, request_timeout_s=120):
        super().__init__(
            coordinator, endpoint, Qwen3Protocol(endpoint), request_timeout_s=request_timeout_s
        )
