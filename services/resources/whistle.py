"""Opt-in managed CPU STT: parent HTTP/policy broker, credential-free native child."""

import asyncio
import os
import secrets
import stat
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from anyio import CancelScope
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from packages.providers.whistle_assets import MANIFEST, PROFILE, outside_workspace, validate_assets
from packages.providers.whistle_audio import MAX_AUDIO_BYTES, inspect_wav
from packages.providers.whistle_backend import MAX_TEXT_CHARS
from packages.providers.whistle_ipc import (
    MAX_CONTROL_BYTES,
    MAX_RESPONSE_BYTES,
    VERSION,
    decode_json,
    encode_json,
    peer_pid,
    private_directory,
    receive_frame,
    send_frame,
    socket_path,
    token,
)
from packages.resources.processes import WorkerSpec
from services.resources.coordinator import WhistleAdmissionError


class ManagedWhistleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    python: str = Field(min_length=1, max_length=4096)
    weights: str = Field(min_length=1, max_length=4096)
    library: str = Field(min_length=1, max_length=4096)
    cpu_affinity_count: int = Field(default=2, ge=1, le=4)
    memory_bytes: int = Field(default=1 << 30, ge=256 * 1024**2, le=4 * 1024**3)
    host_memory_reserve_bytes: int = Field(default=256 * 1024**2, ge=128 * 1024**2, le=4 * 1024**3)
    startup_timeout_s: float = Field(default=60, ge=1, le=300)
    request_timeout_s: float = Field(default=300, ge=30, le=900)


class WhistleEndpoint:
    def __init__(self, directory):
        self.directory = private_directory(directory)
        self.ready = None

    @asynccontextmanager
    async def connection(self, pid):
        reader, writer = await asyncio.open_unix_connection(str(socket_path(self.directory, pid)))
        try:
            peer_pid(writer.get_extra_info("socket"), pid)
            yield reader, writer
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
            except (OSError, TimeoutError):
                pass

    async def exchange(self, pid, incarnation, kind, data=None):
        nonce = secrets.token_hex(32)
        async with self.connection(pid) as (reader, writer):
            hello = decode_json(await receive_frame(reader, MAX_CONTROL_BYTES))
            if (
                set(hello)
                != {
                    "version",
                    "kind",
                    "pid",
                    "incarnation",
                    "profile",
                    "isolation",
                    "weights_sha256",
                    "library_sha256",
                }
                or type(hello["version"]) is not int
                or hello["version"] != VERSION
                or hello["kind"] != "hello"
                or type(hello["pid"]) is not int
                or hello["pid"] != pid
                or not token(hello["incarnation"])
                or hello["profile"] != PROFILE
                or hello["isolation"] != "seccomp-network-v1"
                or hello["weights_sha256"] != MANIFEST["weights"]["sha256"]
                or hello["library_sha256"] != MANIFEST["library"]["sha256"]
                or (incarnation is not None and incarnation != hello["incarnation"])
            ):
                raise ValueError("Whistle readiness identity mismatch")
            incarnation = hello["incarnation"]
            request = {"version": VERSION, "kind": kind, "incarnation": incarnation, "nonce": nonce}
            await send_frame(writer, encode_json(request), MAX_CONTROL_BYTES)
            if data is not None:
                await send_frame(writer, data, MAX_AUDIO_BYTES)
            result = decode_json(
                await receive_frame(reader, MAX_RESPONSE_BYTES), MAX_RESPONSE_BYTES
            )
            required = {"version", "kind", "pid", "incarnation", "nonce"}
            expected_keys = (
                required
                if kind == "probe"
                else required | ({"error"} if "error" in result else {"text"})
            )
            if (
                set(result) != expected_keys
                or type(result["version"]) is not int
                or result["version"] != VERSION
                or result["kind"] != ("ready" if kind == "probe" else "result")
                or type(result["pid"]) is not int
                or result["pid"] != pid
                or result["incarnation"] != incarnation
                or result["nonce"] != nonce
            ):
                raise ValueError("Whistle response identity mismatch")
            if "error" in result:
                raise RuntimeError("Whistle native transcription failed")
            return incarnation, result

    def forget(self, pid, incarnation):
        current = self.ready
        if current is None or current[:2] != (pid, incarnation):
            return
        path = socket_path(self.directory, pid)
        try:
            info = path.lstat()
            if (
                stat.S_ISSOCK(info.st_mode)
                and info.st_uid == os.geteuid()
                and (info.st_dev, info.st_ino) == current[2:]
            ):
                path.unlink()
        except FileNotFoundError:
            pass
        self.ready = None

    async def probe(self, spec, process):
        if process.returncode is not None:
            return False
        try:
            incarnation, _ = await self.exchange(process.pid, None, "probe")
            if process.returncode is not None:
                return False
            info = socket_path(self.directory, process.pid).lstat()
            previous = self.ready
            if previous is not None and previous[:2] != (process.pid, incarnation):
                self.forget(*previous[:2])
            self.ready = (process.pid, incarnation, info.st_dev, info.st_ino)
            return True
        except (OSError, ValueError, RuntimeError, EOFError, asyncio.IncompleteReadError):
            return False

    async def transcribe(self, data, identity):
        current = self.ready
        if current is None or current[0] != identity.pid:
            raise RuntimeError("Whistle worker readiness is unavailable")
        incarnation, result = await self.exchange(identity.pid, current[1], "transcribe", data)
        if self.ready is None or self.ready[:2] != (identity.pid, incarnation):
            raise RuntimeError("Whistle worker incarnation changed")
        text = result.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
            raise ValueError("Invalid bounded Whistle transcript")
        text.encode("utf-8")
        return text

    def close(self):
        # Only called after manager.close confirmed all process/group releases.
        for path in self.directory.iterdir():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("Unexpected Whistle IPC directory entry")
            path.unlink()
        self.directory.rmdir()


def whistle_worker(settings, root, workspace):
    outside_workspace(settings.python, workspace)
    validate_assets(settings.weights, settings.library, workspace)
    directory = Path(tempfile.mkdtemp(prefix="kinaiya-whistle-", dir="/tmp"))
    try:
        outside_workspace(directory, workspace)
        endpoint = WhistleEndpoint(directory)
        spec = WorkerSpec(
            id="whistle",
            argv=(
                settings.python,
                "-m",
                "packages.providers.whistle_worker",
                "--socket-dir",
                str(directory),
                "--weights",
                settings.weights,
                "--library",
                settings.library,
                "--workspace",
                str(workspace),
                "--cpu-count",
                str(settings.cpu_affinity_count),
                "--memory-bytes",
                str(settings.memory_bytes),
            ),
            cwd=root,
            env={"PYTHONPATH": str(root), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            startup_timeout_s=settings.startup_timeout_s,
        )
        return spec, endpoint
    except BaseException:
        directory.rmdir()
        raise


@dataclass(eq=False)
class Transcription:
    identity: object | None
    incarnation: str | None
    policy: tuple
    admission: asyncio.Task | None = None
    work: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    cleanup_confirmed: bool = False
    cleanup_failed: bool = False


class WhistleBroker:
    """One native operation/cleanup owner. Refusal happens before reading audio."""

    def __init__(self, coordinator, endpoint, *, request_timeout_s=300):
        self.coordinator, self.manager, self.endpoint = coordinator, coordinator.manager, endpoint
        self.timeout = request_timeout_s
        self.operation = None
        self.closed = False

    def policy(self):
        try:
            if self.closed:
                raise WhistleAdmissionError()
            return self.coordinator.whistle_policy()
        except (OSError, ValueError, WhistleAdmissionError) as exc:
            raise HTTPException(409, "Whistle is paused by resource or owner policy") from exc

    def current(self, operation):
        if self.policy() != operation.policy or not self.manager.is_current(operation.identity):
            raise HTTPException(409, "Whistle resource revision or worker changed")

    def status(self):
        operation = self.operation
        return {
            "profile": PROFILE,
            "mode": "independent_buffered_slices",
            "active_or_retained": operation is not None,
            "cleanup_failed": bool(operation and operation.cleanup_failed),
            "worker_ready": self.manager.owned_identity("whistle") is not None,
            "request_timeout_s": self.timeout,
            "max_audio_seconds": 30,
        }

    def release(self, operation):
        if (
            self.operation is operation
            and operation.cleanup_confirmed
            and (operation.work is None or operation.work.done())
        ):
            self.operation = None

    async def cleanup(self, operation):
        try:
            if operation.identity is None and operation.admission is not None:
                try:
                    operation.identity = await asyncio.shield(operation.admission)
                    current = self.endpoint.ready
                    if current is not None and current[0] == operation.identity.pid:
                        operation.incarnation = current[1]
                except WhistleAdmissionError as exc:
                    if not exc.cleanup_confirmed:
                        raise RuntimeError("Whistle startup cleanup is unconfirmed") from exc
                    operation.cleanup_confirmed = True
                    self.release(operation)
                    return
            result = await self.manager.stop_if_current(operation.identity)
            evidence = result.get("stop_evidence")
            if not result.get("ok") or (
                not result.get("superseded")
                and not result.get("release_confirmed")
                and (not evidence or not evidence.get("reaped") or not evidence.get("group_empty"))
            ):
                raise RuntimeError("Whistle process/group cleanup is unconfirmed")
            operation.cleanup_confirmed = True
            self.endpoint.forget(operation.identity.pid, operation.incarnation)
            if operation.work is not None and not operation.work.done():
                await asyncio.wait((operation.work,), timeout=1)
            self.release(operation)
        except Exception:
            operation.cleanup_failed = True

    def abort(self, operation):
        if operation.work is not None and not operation.work.done():
            operation.work.cancel()
        if operation.cleanup is None or operation.cleanup.done():
            operation.cleanup = asyncio.create_task(self.cleanup(operation))

    @staticmethod
    async def disconnected(request):
        while (await request.receive())["type"] != "http.disconnect":
            pass

    async def transcribe(self, request):
        policy = self.policy()
        if self.operation is not None:
            old = self.operation
            if old.cleanup_failed and old.cleanup is not None and old.cleanup.done():
                self.abort(old)  # exact old identity only; no inference retry
            raise HTTPException(409, "Whistle inference or retained cleanup is busy")
        operation = Transcription(None, None, policy)
        self.operation = operation
        watcher, success, started = None, False, False
        try:
            async with asyncio.timeout(self.timeout):
                if request.headers.get("content-type", "").split(";", 1)[0].lower() not in {
                    "audio/wav",
                    "audio/x-wav",
                }:
                    raise HTTPException(415, "Whistle accepts a raw PCM WAV body")
                length = request.headers.get("content-length")
                if length is not None and (
                    not length.isascii() or not length.isdigit() or len(length) > 10
                ):
                    raise HTTPException(400, "Invalid audio content length")
                if length is not None and int(length) > MAX_AUDIO_BYTES:
                    raise HTTPException(413, "Audio exceeds 10 MB")
                data = bytearray()
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_AUDIO_BYTES:
                        raise HTTPException(413, "Audio exceeds 10 MB")
                    data.extend(chunk)
                try:
                    audio = bytes(data)
                    inspect_wav(audio)
                except ValueError as exc:
                    raise HTTPException(400, "Unsupported or malformed bounded PCM WAV") from exc
                if await request.is_disconnected():
                    raise HTTPException(499, "Whistle request disconnected")
                started = True
                watcher = asyncio.create_task(self.disconnected(request))
                operation.admission = asyncio.create_task(self.coordinator.admit_whistle(policy))
                await asyncio.wait(
                    (operation.admission, watcher), return_when=asyncio.FIRST_COMPLETED
                )
                await asyncio.sleep(0)
                if watcher.done():
                    raise HTTPException(499, "Whistle request disconnected")
                operation.identity = operation.admission.result()
                identity = operation.identity
                ready = self.endpoint.ready
                if ready is None or ready[0] != identity.pid:
                    raise RuntimeError("Whistle readiness is unavailable")
                operation.incarnation = ready[1]
                self.current(operation)
                operation.work = asyncio.create_task(self.endpoint.transcribe(audio, identity))
                operation.work.add_done_callback(lambda task: self.release(operation))
                await asyncio.wait((operation.work, watcher), return_when=asyncio.FIRST_COMPLETED)
                await asyncio.sleep(0)
                if watcher.done():
                    raise HTTPException(499, "Whistle request disconnected")
                text = operation.work.result()
                self.current(operation)
                result = {"text": text, "provider": "whistle", "profile": PROFILE}
                encode_json(result, MAX_RESPONSE_BYTES)
                success = True
                return result
        except TimeoutError as exc:
            raise HTTPException(504, "Whistle bounded request deadline exceeded") from exc
        except HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HTTPException(503, "Whistle transcription is unavailable") from exc
        finally:
            if watcher is not None:
                watcher.cancel()
            if not success and started:
                self.abort(operation)
                with CancelScope(shield=True):
                    await asyncio.wait((operation.cleanup,), timeout=1)
            elif self.operation is operation:
                self.operation = None
            if watcher is not None:
                with CancelScope(shield=True):
                    await asyncio.wait((watcher,), timeout=0.2)

    async def close(self):
        self.closed = True
        if self.operation is not None:
            self.abort(self.operation)
            with CancelScope(shield=True):
                await asyncio.wait((self.operation.cleanup,), timeout=1)
