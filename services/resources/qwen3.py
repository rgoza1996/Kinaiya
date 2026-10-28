"""Managed dialogue parent: bounded transport and exact owned-child lifecycle."""

import asyncio
import os
import re
import secrets
import stat
import tempfile
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from anyio import CancelScope
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from packages.providers import qwen3_ipc as wire
from packages.providers.qwen3 import PUBLIC_ERROR_CODES, public_event
from packages.providers.whistle_assets import outside_workspace
from packages.resources.processes import WorkerSpec
from services.resources.coordinator import Qwen3AdmissionError


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


@dataclass(eq=False)
class Generation:
    policy: object
    end: float
    identity: object = None
    incarnation: str | None = None
    admission: asyncio.Task | None = None
    work: asyncio.Task | None = None
    monitor: asyncio.Task | None = None
    disconnect: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    failure: asyncio.Future | None = None
    succeeded: bool = False
    cancel_sent: bool = False
    cleanup_confirmed: bool = False
    cleanup_failed: bool = False


class GenerationRefusal(RuntimeError):
    def __init__(self, code):
        self.code = code


class Qwen3Body:
    """First-class owner, including cancellation before the first iteration."""

    def __init__(self, broker, operation):
        self.broker, self.operation = broker, operation
        self.stopped, self.terminal = False, False
        self.take = None

    def __aiter__(self):
        return self

    @property
    def remaining_timeout_s(self):
        return max(0.0, self.operation.end - asyncio.get_running_loop().time())

    async def __anext__(self):
        op = self.operation
        if self.stopped or self.terminal:
            await self.aclose()
            raise StopAsyncIteration
        if self.take is not None:
            raise RuntimeError("Managed Qwen body permits one reader")
        try:
            if not op.failure.done():
                self.broker.current(op)
                self.take = asyncio.create_task(op.queue.get())
                await asyncio.wait((self.take, op.failure), return_when=asyncio.FIRST_COMPLETED)
            if op.failure.done():
                frame = {"kind": "error", "code": op.failure.result()}
            else:
                frame = self.take.result()
                self.broker.current(op)
            self.terminal = frame["kind"] != "delta"
            return wire.encode_json(frame, wire.MAX_FRAME_BYTES) + b"\n"
        except HTTPException:
            self.broker.abort(op, "paused")
            self.terminal = True
            return (
                wire.encode_json({"kind": "error", "code": "paused"}, wire.MAX_FRAME_BYTES) + b"\n"
            )
        except BaseException:
            await self.aclose()
            raise
        finally:
            if self.take is not None:
                if not self.take.done():
                    self.take.cancel()
                await asyncio.gather(self.take, return_exceptions=True)
                self.take = None

    async def aclose(self):
        op = self.operation
        if not self.stopped:
            self.stopped = True
            op.closed.set()
            # A public done is emitted only after native EOF and real IPC close.
            # Otherwise closing even an unstarted body aborts this exact child.
            if self.terminal and op.succeeded and not op.failure.done():
                self.broker.finish(op)
            else:
                self.broker.abort(op, "disconnected")
        if op.cleanup is not None:
            with CancelScope(shield=True):
                await asyncio.wait((op.cleanup,), timeout=1)


class Qwen3Broker:
    def __init__(self, coordinator, endpoint, *, request_timeout_s=120):
        from packages.providers.qwen3 import deadline

        self.coordinator, self.manager, self.endpoint = coordinator, coordinator.manager, endpoint
        self.timeout = deadline(request_timeout_s)
        self.operation = None
        self.closed = False

    @property
    def request_timeout_s(self):
        return self.timeout

    def policy(self):
        try:
            if self.closed:
                raise Qwen3AdmissionError()
            return self.coordinator.qwen3_policy()
        except (OSError, ValueError, Qwen3AdmissionError) as exc:
            raise HTTPException(409, "Managed Qwen is paused by owner/resource policy") from exc

    def current(self, op):
        if (
            op.closed.is_set()
            or self.policy() != op.policy
            or not self.manager.is_current(op.identity)
        ):
            raise HTTPException(409, "Managed Qwen resource revision or worker changed")

    def status(self):
        op = self.operation
        return {
            "profile": wire.PROFILE,
            "model": wire.MODEL,
            "manifest_sha256": wire.MANIFEST_SHA256,
            "active_or_retained": op is not None,
            "cleanup_failed": bool(op and op.cleanup_failed),
            "worker_ready": self.manager.owned_identity("qwen3") is not None,
            "request_timeout_s": self.timeout,
            **self.endpoint.limits,
        }

    def release(self, op):
        # Only cleanup's confirmed final transition may make the slot reusable.
        if self.operation is op and op.cleanup_confirmed:
            self.operation = None

    async def cleanup(self, op, *, graceful=False):
        try:
            if not graceful:
                if op.identity is None and op.admission is not None:
                    try:
                        op.identity = await asyncio.shield(op.admission)
                        ready = self.endpoint.ready
                        if ready is not None and ready[0] == op.identity.pid:
                            op.incarnation = ready[1]
                    except Qwen3AdmissionError as exc:
                        if not exc.cleanup_confirmed:
                            raise RuntimeError(
                                "Qwen admission cleanup remains unconfirmed"
                            ) from exc
                if op.identity is not None:
                    result = await self.manager.stop_if_current(op.identity)
                    proof = result.get("stop_evidence") or {}
                    if not result.get("ok") or not (
                        result.get("superseded")
                        or result.get("release_confirmed")
                        or (proof.get("reaped") and proof.get("group_empty"))
                    ):
                        raise RuntimeError("Qwen process/group release is unconfirmed")
                    self.endpoint.forget(op.identity.pid, op.incarnation)
            # Transport/task closure is independent of a bounded HTTP wait.
            # Never release while a held close can still act on this worker.
            if op.monitor is not None:
                await asyncio.gather(op.monitor, return_exceptions=True)
            elif op.disconnect is not None:
                op.disconnect.cancel()
                await asyncio.gather(op.disconnect, return_exceptions=True)
            if op.work is not None:
                await asyncio.gather(op.work, return_exceptions=True)
            op.cleanup_confirmed = True
            op.cleanup_failed = False
            self.release(op)
        except (Exception, asyncio.CancelledError):
            op.cleanup_failed = True

    def finish(self, op):
        op.closed.set()
        if op.cleanup is None:
            op.cleanup = asyncio.create_task(self.cleanup(op, graceful=True))

    def abort(self, op, code):
        if code not in PUBLIC_ERROR_CODES:
            code = "generation_failed"
        if not op.failure.done():
            op.failure.set_result(code)
        op.closed.set()
        if op.work is not None and not op.work.done() and not op.cancel_sent:
            op.cancel_sent = True
            op.work.cancel()
        if op.cleanup is None or (op.cleanup.done() and op.cleanup_failed):
            op.cleanup = asyncio.create_task(self.cleanup(op))

    async def produce(self, op, messages):
        terminal, total, count = None, 0, 0
        async with aclosing(self.endpoint.stream(messages, op.identity)) as source:
            async for value in source:
                self.current(op)
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
                    await op.queue.put(frame)
                else:
                    terminal = frame
        if terminal is None:
            raise ValueError("Qwen ended without a terminal")
        self.current(op)
        if terminal["kind"] == "error":
            raise GenerationRefusal(terminal["code"])
        op.succeeded = True
        await op.queue.put(terminal)

    async def monitor(self, op):
        closed = asyncio.create_task(op.closed.wait())
        work = op.work
        try:
            while True:
                remaining = max(0, op.end - asyncio.get_running_loop().time())
                selected = [op.disconnect, closed] + ([work] if work is not None else [])
                done, _ = await asyncio.wait(
                    selected, timeout=min(remaining, 0.1), return_when=asyncio.FIRST_COMPLETED
                )
                if closed in done:
                    return
                if op.disconnect in done:
                    self.abort(op, "disconnected")
                    return
                if remaining <= 0:
                    self.abort(op, "deadline")
                    return
                try:
                    self.current(op)
                except HTTPException:
                    self.abort(op, "paused")
                    return
                if work in done:
                    try:
                        work.result()
                    except GenerationRefusal as exc:
                        self.abort(op, exc.code)
                        return
                    except HTTPException:
                        self.abort(op, "paused")
                        return
                    except (Exception, asyncio.CancelledError):
                        self.abort(op, "generation_failed")
                        return
                    work = None
        finally:
            op.disconnect.cancel()
            closed.cancel()
            await asyncio.gather(op.disconnect, closed, return_exceptions=True)

    @staticmethod
    async def disconnected(request):
        # Installed only after the complete raw body is consumed. This is the
        # sole receive owner until close, including awaited native admission.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    async def stream(self, request):
        policy = self.policy()
        if self.operation is not None:
            old = self.operation
            if old.cleanup_failed and old.cleanup is not None and old.cleanup.done():
                self.abort(old, "unavailable")
            raise HTTPException(409, "Managed Qwen generation or retained cleanup is busy")
        op = Generation(policy, asyncio.get_running_loop().time() + self.timeout)
        op.failure = asyncio.get_running_loop().create_future()
        self.operation = op
        started = False
        try:
            async with asyncio.timeout(self.timeout):
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].lower()
                    != "application/json"
                ):
                    raise HTTPException(400, "Managed Qwen requires bounded JSON messages")
                length = request.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdecimal():
                        raise HTTPException(400, "Invalid managed Qwen body length")
                    if len(length) > 9 or int(length) > wire.MAX_REQUEST_BYTES:
                        raise HTTPException(413, "Managed Qwen request exceeds its byte bound")
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > wire.MAX_REQUEST_BYTES:
                        raise HTTPException(413, "Managed Qwen request exceeds its byte bound")
                    raw.extend(chunk)
                try:
                    value = wire.decode_json(bytes(raw), wire.MAX_REQUEST_BYTES)
                    if set(value) != {"messages"}:
                        raise ValueError("Unexpected Qwen request field")
                    messages = wire.validate_messages(value["messages"])
                except (ValueError, UnicodeError) as exc:
                    raise HTTPException(400, "Invalid bounded Qwen dialogue") from exc
                # No await separates the last policy check and admission task.
                if self.policy() != policy:
                    raise HTTPException(409, "Managed Qwen admission revision changed")
                started = True
                op.disconnect = asyncio.create_task(self.disconnected(request))
                op.admission = asyncio.create_task(self.coordinator.admit_qwen3(policy))
                done, _ = await asyncio.wait(
                    (op.admission, op.disconnect), return_when=asyncio.FIRST_COMPLETED
                )
                if op.disconnect in done:
                    raise HTTPException(503, "Managed Qwen admission disconnected")
                op.identity = op.admission.result()
                ready = self.endpoint.ready
                if ready is None or ready[0] != op.identity.pid:
                    raise RuntimeError("Qwen readiness is unavailable")
                op.incarnation = ready[1]
                self.current(op)
                op.work = asyncio.create_task(self.produce(op, messages))
                op.monitor = asyncio.create_task(self.monitor(op))
                return Qwen3Body(self, op)
        except BaseException as exc:
            if started:
                self.abort(op, "deadline" if isinstance(exc, TimeoutError) else "unavailable")
                with CancelScope(shield=True):
                    await asyncio.wait((op.cleanup,), timeout=1)
            else:
                op.cleanup_confirmed = True
                self.release(op)
            if isinstance(exc, TimeoutError):
                raise HTTPException(503, "Managed Qwen admission deadline exceeded") from exc
            if isinstance(exc, (Qwen3AdmissionError, RuntimeError, OSError)):
                raise HTTPException(503, "Managed Qwen admission unavailable") from exc
            raise

    async def close(self):
        self.closed = True
        op = self.operation
        if op is not None:
            self.abort(op, "paused")
            with CancelScope(shield=True):
                await asyncio.wait((op.cleanup,), timeout=1)
