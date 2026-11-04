"""Opt-in managed CPU STT: parent HTTP/policy broker, credential-free native child."""

import asyncio
import hashlib
import math
import os
import secrets
import stat
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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
from packages.providers.whistle_stream import (
    MAX_CHUNK_SAMPLES,
    MAX_PASS_MS,
    MAX_TOTAL_SAMPLES,
    SUPPORTED_LANGUAGES,
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
    actual_ownership_supported = True

    def __init__(self, directory):
        from packages.contracts.operation_ownership import StreamClosures

        self.directory = private_directory(directory)
        self.ready = None
        self.stream_closes = StreamClosures()

    @asynccontextmanager
    async def connection(self, pid):
        reader, writer = await asyncio.open_unix_connection(str(socket_path(self.directory, pid)))
        try:
            peer_pid(writer.get_extra_info("socket"), pid)
            yield reader, writer
        finally:
            try:
                await self.stream_closes.close(writer)
            except OSError:
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
                frame_limit = MAX_CHUNK_SAMPLES * 2 if kind == "stream_pcm" else MAX_AUDIO_BYTES
                await send_frame(writer, data, frame_limit)
            result = decode_json(
                await receive_frame(reader, MAX_RESPONSE_BYTES), MAX_RESPONSE_BYTES
            )
            required = {"version", "kind", "pid", "incarnation", "nonce"}
            if kind == "probe":
                expected_kind = "ready"
                expected_keys = required
            elif kind == "stream_begin":
                expected_kind = "stream_ready"
                expected_keys = required | ({"error"} if "error" in result else set())
            elif kind in {"stream_pcm", "stream_finish"}:
                expected_kind = "stream_result"
                expected_keys = required | ({"error"} if "error" in result else {"update"})
            else:
                expected_kind = "result"
                expected_keys = required | ({"error"} if "error" in result else {"text"})
            if (
                set(result) != expected_keys
                or type(result["version"]) is not int
                or result["version"] != VERSION
                or result["kind"] != expected_kind
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

    @staticmethod
    def _validate_stream_update(update, *, expected_samples=None, expect_final=False):
        required_keys = {
            "delta_text",
            "committed_text",
            "pending_text",
            "received_samples",
            "final",
            "native_pass_ms",
            "language",
        }
        if not isinstance(update, dict) or set(update) != required_keys:
            raise ValueError("Invalid bounded Whistle stream update")
        for field_name in ("delta_text", "committed_text", "pending_text"):
            val = update[field_name]
            if not isinstance(val, str) or len(val) > MAX_TEXT_CHARS:
                raise ValueError("Invalid bounded Whistle stream text")
            val.encode("utf-8")
        received_samples = update["received_samples"]
        if (
            type(received_samples) is not int
            or not 0 <= received_samples <= MAX_TOTAL_SAMPLES
            or (expected_samples is not None and received_samples != expected_samples)
        ):
            raise ValueError("Invalid Whistle stream sample count")
        if type(update["final"]) is not bool or update["final"] is not expect_final:
            raise ValueError("Invalid Whistle stream final flag")
        if expect_final and update["pending_text"] != "":
            raise ValueError("Final Whistle stream update cannot retain pending_text")
        pass_ms = update["native_pass_ms"]
        if (
            type(pass_ms) is bool
            or not isinstance(pass_ms, (int, float))
            or not math.isfinite(pass_ms)
            or not 0 <= float(pass_ms) <= MAX_PASS_MS
        ):
            raise ValueError("Invalid Whistle stream native_pass_ms")
        language = update["language"]
        if not (
            language is None
            or (isinstance(language, str) and (language == "" or language in SUPPORTED_LANGUAGES))
        ):
            raise ValueError("Invalid Whistle stream language")
        return update

    async def begin_stream(self, identity):
        current = self.ready
        if current is None or current[0] != identity.pid:
            raise RuntimeError("Whistle worker readiness is unavailable")
        incarnation, _ = await self.exchange(identity.pid, current[1], "stream_begin")
        if self.ready is None or self.ready[:2] != (identity.pid, incarnation):
            raise RuntimeError("Whistle worker incarnation changed")
        return incarnation

    async def stream_pcm(self, pcm16, identity, *, expected_samples):
        current = self.ready
        if current is None or current[0] != identity.pid:
            raise RuntimeError("Whistle worker readiness is unavailable")
        incarnation, result = await self.exchange(identity.pid, current[1], "stream_pcm", pcm16)
        if self.ready is None or self.ready[:2] != (identity.pid, incarnation):
            raise RuntimeError("Whistle worker incarnation changed")
        return self._validate_stream_update(
            result.get("update"), expected_samples=expected_samples, expect_final=False
        )

    async def finish_stream(self, identity, *, expected_samples):
        current = self.ready
        if current is None or current[0] != identity.pid:
            raise RuntimeError("Whistle worker readiness is unavailable")
        incarnation, result = await self.exchange(identity.pid, current[1], "stream_finish")
        if self.ready is None or self.ready[:2] != (identity.pid, incarnation):
            raise RuntimeError("Whistle worker incarnation changed")
        return self._validate_stream_update(
            result.get("update"), expected_samples=expected_samples, expect_final=True
        )

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
    work_tasks: list[asyncio.Task] = field(default_factory=list)
    active_request: object | None = None
    cleanup: asyncio.Task | None = None
    cleanup_confirmed: bool = False
    cleanup_failed: bool = False
    stream: dict | None = None


class WhistleStreamStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=(1 << 53) - 1)
    utterance_id: str = Field(min_length=1, max_length=128)
    content_scope: Literal["local_only", "public_synthetic", "private_cloud_authorized"] = (
        "local_only"
    )


class WhistleStreamFinishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stream_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_incarnation: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1, le=(1 << 53) - 1)
    utterance_id: str = Field(min_length=1, max_length=128)
    content_scope: Literal["local_only", "public_synthetic", "private_cloud_authorized"] = (
        "local_only"
    )
    sequence: int = Field(ge=1, le=100000)
    sample_offset: int = Field(ge=0, le=MAX_TOTAL_SAMPLES)


class WhistleStreamCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stream_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_incarnation: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WhistleBroker:
    """One native operation/cleanup owner. Refusal happens before reading audio."""

    actual_ownership_supported = True

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
        ready = self.endpoint.ready
        if (
            self.policy() != operation.policy
            or not self.manager.is_current(operation.identity)
            or (
                operation.incarnation is not None
                and (
                    ready is None
                    or operation.identity is None
                    or ready[:2] != (operation.identity.pid, operation.incarnation)
                )
            )
        ):
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
            and all(task.done() for task in operation.work_tasks)
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
            pending_tasks = [
                task
                for task in (operation.work, *operation.work_tasks)
                if task is not None and not task.done()
            ]
            if pending_tasks:
                await asyncio.wait(pending_tasks, timeout=1)
            self.release(operation)
        except Exception:
            operation.cleanup_failed = True

    def abort(self, operation):
        for task in (operation.work, *operation.work_tasks):
            if task is not None and not task.done():
                task.cancel()
        if operation.cleanup is None or operation.cleanup.done():
            operation.cleanup = asyncio.create_task(self.cleanup(operation))
        from packages.contracts.operation_ownership import current_owner

        owner = current_owner()
        if owner is not None:
            closes = getattr(self.endpoint, "stream_closes", None)
            if closes is None:
                owner.fail()
            else:
                closes.retain(owner)
            for task in (
                operation.admission,
                operation.work,
                *operation.work_tasks,
                operation.cleanup,
            ):
                if task is not None:
                    owner.retain_task(task)
            operation.cleanup.add_done_callback(
                lambda _: (
                    owner.fail()
                    if not operation.cleanup_confirmed or operation.cleanup_failed
                    else None
                )
            )

    def abort_active(self):
        from packages.contracts.operation_ownership import current_owner

        owner = current_owner()
        if owner is not None:
            closes = getattr(self.endpoint, "stream_closes", None)
            if closes is None:
                owner.fail()
            else:
                closes.retain(owner)
        if self.operation is not None:
            self.abort(self.operation)

    async def drain(self):
        operation = self.operation
        if operation is not None:
            self.abort(operation)
            await asyncio.shield(operation.cleanup)
            tasks = [task for task in (operation.work, *operation.work_tasks) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if not operation.cleanup_confirmed or operation.cleanup_failed:
                raise RuntimeError("Whistle cleanup remains retained")

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

    async def _read_control_json(self, request, model):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_CONTROL_BYTES:
                raise HTTPException(413, "Whistle stream control body exceeds bound")
            raw.extend(chunk)
        try:
            parsed = decode_json(bytes(raw), MAX_CONTROL_BYTES)
            return model.model_validate(parsed)
        except Exception as exc:
            raise HTTPException(400, "Invalid Whistle stream control payload") from exc

    async def begin_stream(self, request):
        policy = self.policy()
        if self.operation is not None:
            old = self.operation
            if old.cleanup_failed and old.cleanup is not None and old.cleanup.done():
                self.abort(old)
            raise HTTPException(409, "Whistle inference or retained cleanup is busy")
        operation = Transcription(None, None, policy)
        self.operation = operation
        watcher, success, started = None, False, False
        try:
            async with asyncio.timeout(self.timeout):
                body = await self._read_control_json(request, WhistleStreamStartRequest)
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
                operation.work = asyncio.create_task(self.endpoint.begin_stream(identity))
                await asyncio.wait((operation.work, watcher), return_when=asyncio.FIRST_COMPLETED)
                await asyncio.sleep(0)
                if watcher.done():
                    raise HTTPException(499, "Whistle request disconnected")
                incarnation = operation.work.result()
                if incarnation != operation.incarnation:
                    raise RuntimeError("Whistle worker incarnation changed")
                self.current(operation)
                stream_id = secrets.token_hex(32)
                operation.stream = {
                    "stream_id": stream_id,
                    "session_id": body.session_id,
                    "thread_id": body.thread_id,
                    "epoch": body.epoch,
                    "utterance_id": body.utterance_id,
                    "content_scope": body.content_scope,
                    "sequence": 0,
                    "sample_offset": 0,
                    "committed_text": "",
                    "pending_text": "",
                    "last_pcm_sha256": None,
                    "last_ack": None,
                }
                operation.work = None
                result = {
                    "stream_id": stream_id,
                    "worker_incarnation": operation.incarnation,
                    "session_id": body.session_id,
                    "thread_id": body.thread_id,
                    "epoch": body.epoch,
                    "utterance_id": body.utterance_id,
                    "content_scope": body.content_scope,
                    "sequence": 0,
                    "sample_offset": 0,
                    "provider": "whistle",
                    "profile": PROFILE,
                }
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
            raise HTTPException(503, "Whistle stream startup is unavailable") from exc
        finally:
            if watcher is not None:
                watcher.cancel()
            if not success:
                if started:
                    self.abort(operation)
                    with CancelScope(shield=True):
                        await asyncio.wait((operation.cleanup,), timeout=1)
                elif self.operation is operation:
                    self.operation = None
            if watcher is not None:
                with CancelScope(shield=True):
                    await asyncio.wait((watcher,), timeout=0.2)

    async def append_stream(self, request):
        operation = self.operation
        if (
            operation is None
            or operation.stream is None
            or operation.cleanup is not None
            or operation.work is not None
            or operation.active_request is not None
        ):
            raise HTTPException(409, "No active Whistle incremental stream")
        try:
            self.current(operation)
        except HTTPException:
            self.abort(operation)
            with CancelScope(shield=True):
                await asyncio.wait((operation.cleanup,), timeout=1)
            raise
        stream = operation.stream
        headers = request.headers
        stream_id = headers.get("x-kinaiya-stream-id")
        incarnation = headers.get("x-kinaiya-worker-incarnation")
        session_id = headers.get("x-kinaiya-session-id")
        thread_id = headers.get("x-kinaiya-thread-id")
        epoch_raw = headers.get("x-kinaiya-epoch")
        utterance_id = headers.get("x-kinaiya-utterance-id")
        content_scope = headers.get("x-kinaiya-content-scope")
        seq_raw = headers.get("x-kinaiya-sequence")
        offset_raw = headers.get("x-kinaiya-sample-offset")
        pcm_sha256 = headers.get("x-kinaiya-pcm-sha256")
        if (
            stream_id != stream["stream_id"]
            or incarnation != operation.incarnation
            or session_id != stream["session_id"]
            or thread_id != stream["thread_id"]
            or utterance_id != stream["utterance_id"]
            or content_scope != stream["content_scope"]
            or epoch_raw is None
            or not epoch_raw.isdigit()
            or int(epoch_raw) != stream["epoch"]
            or seq_raw is None
            or not seq_raw.isdigit()
            or offset_raw is None
            or not offset_raw.isdigit()
            or not token(pcm_sha256)
        ):
            raise HTTPException(409, "Whistle stream identity or scope mismatch")
        sequence = int(seq_raw)
        sample_offset = int(offset_raw)
        request_token = object()
        operation.active_request = request_token
        watcher, work_task, success, dispatched, abort_on_exit = None, None, False, False, False
        try:
            async with asyncio.timeout(self.timeout):
                if request.headers.get("content-type", "").split(";", 1)[0].lower() not in {
                    "application/octet-stream",
                    "audio/pcm",
                }:
                    raise HTTPException(415, "Whistle stream accepts raw PCM16 bytes")
                raw = bytearray()
                max_bytes = MAX_CHUNK_SAMPLES * 2
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > max_bytes:
                        raise HTTPException(413, "Whistle PCM chunk exceeds 16000 samples")
                    raw.extend(chunk)
                pcm16 = bytes(raw)
                if len(pcm16) == 0 or len(pcm16) % 2 != 0:
                    raise HTTPException(400, "Whistle stream requires non-empty 16-bit PCM")
                if hashlib.sha256(pcm16).hexdigest() != pcm_sha256:
                    raise HTTPException(400, "Whistle PCM SHA-256 digest mismatch")
                samples = len(pcm16) // 2
                if await request.is_disconnected():
                    raise HTTPException(499, "Whistle request disconnected")
                if (
                    self.operation is not operation
                    or operation.stream is not stream
                    or operation.cleanup is not None
                    or operation.work is not None
                    or operation.active_request is not request_token
                ):
                    raise HTTPException(409, "No active Whistle incremental stream")
                try:
                    self.current(operation)
                except HTTPException:
                    abort_on_exit = True
                    raise
                # Idempotent acknowledgment recovery for exact last committed append
                if (
                    sequence == stream["sequence"]
                    and sample_offset + samples == stream["sample_offset"]
                    and pcm_sha256 == stream["last_pcm_sha256"]
                    and stream["last_ack"] is not None
                ):
                    success = True
                    return {**stream["last_ack"], "replay": True}
                if sequence != stream["sequence"] + 1 or sample_offset != stream["sample_offset"]:
                    raise HTTPException(
                        409, "Whistle stream sequence or sample offset is non-contiguous"
                    )
                if sample_offset + samples > MAX_TOTAL_SAMPLES:
                    raise HTTPException(413, "Whistle stream exceeds 960000 total samples")
                dispatched = True
                watcher = asyncio.create_task(self.disconnected(request))
                work_task = asyncio.create_task(
                    self.endpoint.stream_pcm(
                        pcm16,
                        operation.identity,
                        expected_samples=sample_offset + samples,
                    )
                )
                operation.work = work_task
                operation.work_tasks.append(work_task)
                await asyncio.wait((work_task, watcher), return_when=asyncio.FIRST_COMPLETED)
                await asyncio.sleep(0)
                if watcher.done():
                    raise HTTPException(499, "Whistle request disconnected")
                if (
                    self.operation is not operation
                    or operation.cleanup is not None
                    or operation.active_request is not request_token
                    or operation.work is not work_task
                ):
                    raise HTTPException(409, "No active Whistle incremental stream")
                update = work_task.result()
                operation.work = None
                self.current(operation)
                next_offset = update["received_samples"]
                result = {
                    "stream_id": stream["stream_id"],
                    "worker_incarnation": operation.incarnation,
                    "sequence": sequence,
                    "sample_offset": sample_offset,
                    "received_samples": next_offset,
                    "pcm_sha256": pcm_sha256,
                    "delta_text": update["delta_text"],
                    "committed_text": update["committed_text"],
                    "pending_text": update["pending_text"],
                    "final": False,
                    "native_pass_ms": update["native_pass_ms"],
                    "language": update["language"],
                    "provider": "whistle",
                    "profile": PROFILE,
                }
                encode_json(result, MAX_RESPONSE_BYTES)
                stream["sequence"] = sequence
                stream["sample_offset"] = next_offset
                stream["committed_text"] = update["committed_text"]
                stream["pending_text"] = update["pending_text"]
                stream["last_pcm_sha256"] = pcm_sha256
                stream["last_ack"] = result
                success = True
                return result
        except TimeoutError as exc:
            raise HTTPException(504, "Whistle bounded request deadline exceeded") from exc
        except HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HTTPException(503, "Whistle stream transcription failed") from exc
        finally:
            if operation.active_request is request_token:
                operation.active_request = None
            if watcher is not None:
                watcher.cancel()
            if not success and (dispatched or abort_on_exit):
                self.abort(operation)
                with CancelScope(shield=True):
                    await asyncio.wait((operation.cleanup,), timeout=1)
            if watcher is not None:
                with CancelScope(shield=True):
                    await asyncio.wait((watcher,), timeout=0.2)

    async def finish_stream(self, request):
        operation = self.operation
        if (
            operation is None
            or operation.stream is None
            or operation.cleanup is not None
            or operation.work is not None
            or operation.active_request is not None
        ):
            raise HTTPException(409, "No active Whistle incremental stream")
        try:
            self.current(operation)
        except HTTPException:
            self.abort(operation)
            with CancelScope(shield=True):
                await asyncio.wait((operation.cleanup,), timeout=1)
            raise
        stream = operation.stream
        request_token = object()
        operation.active_request = request_token
        watcher, work_task, success, dispatched, abort_on_exit = None, None, False, False, False
        try:
            async with asyncio.timeout(self.timeout):
                body = await self._read_control_json(request, WhistleStreamFinishRequest)
                if (
                    body.stream_id != stream["stream_id"]
                    or body.worker_incarnation != operation.incarnation
                    or body.session_id != stream["session_id"]
                    or body.thread_id != stream["thread_id"]
                    or body.epoch != stream["epoch"]
                    or body.utterance_id != stream["utterance_id"]
                    or body.content_scope != stream["content_scope"]
                    or body.sequence != stream["sequence"] + 1
                    or body.sample_offset != stream["sample_offset"]
                ):
                    raise HTTPException(409, "Whistle stream finish identity or offset mismatch")
                if await request.is_disconnected():
                    raise HTTPException(499, "Whistle request disconnected")
                if (
                    self.operation is not operation
                    or operation.stream is not stream
                    or operation.cleanup is not None
                    or operation.work is not None
                    or operation.active_request is not request_token
                ):
                    raise HTTPException(409, "No active Whistle incremental stream")
                try:
                    self.current(operation)
                except HTTPException:
                    abort_on_exit = True
                    raise
                if (
                    body.sequence != stream["sequence"] + 1
                    or body.sample_offset != stream["sample_offset"]
                ):
                    raise HTTPException(409, "Whistle stream finish identity or offset mismatch")
                dispatched = True
                watcher = asyncio.create_task(self.disconnected(request))
                work_task = asyncio.create_task(
                    self.endpoint.finish_stream(
                        operation.identity,
                        expected_samples=stream["sample_offset"],
                    )
                )
                operation.work = work_task
                operation.work_tasks.append(work_task)
                await asyncio.wait((work_task, watcher), return_when=asyncio.FIRST_COMPLETED)
                await asyncio.sleep(0)
                if watcher.done():
                    raise HTTPException(499, "Whistle request disconnected")
                if (
                    self.operation is not operation
                    or operation.cleanup is not None
                    or operation.active_request is not request_token
                    or operation.work is not work_task
                ):
                    raise HTTPException(409, "No active Whistle incremental stream")
                update = work_task.result()
                operation.work = None
                self.current(operation)
                result = {
                    "stream_id": stream["stream_id"],
                    "worker_incarnation": operation.incarnation,
                    "sequence": body.sequence,
                    "sample_offset": stream["sample_offset"],
                    "received_samples": update["received_samples"],
                    "delta_text": update["delta_text"],
                    "committed_text": update["committed_text"],
                    "pending_text": update["pending_text"],
                    "final": True,
                    "native_pass_ms": update["native_pass_ms"],
                    "language": update["language"],
                    "provider": "whistle",
                    "profile": PROFILE,
                }
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
            raise HTTPException(503, "Whistle stream finalization failed") from exc
        finally:
            if operation.active_request is request_token:
                operation.active_request = None
            if watcher is not None:
                watcher.cancel()
            if success:
                if self.operation is operation:
                    self.operation = None
            elif dispatched or abort_on_exit:
                self.abort(operation)
                with CancelScope(shield=True):
                    await asyncio.wait((operation.cleanup,), timeout=1)
            if watcher is not None:
                with CancelScope(shield=True):
                    await asyncio.wait((watcher,), timeout=0.2)

    async def cancel_stream(self, request):
        body = await self._read_control_json(request, WhistleStreamCancelRequest)
        operation = self.operation
        if operation is None or operation.stream is None:
            return {"cancelled": False, "cleanup_confirmed": True}
        if body.stream_id != operation.stream["stream_id"] or (
            body.worker_incarnation is not None and body.worker_incarnation != operation.incarnation
        ):
            raise HTTPException(409, "Whistle stream cancel identity mismatch")
        self.abort(operation)
        with CancelScope(shield=True):
            await asyncio.wait((operation.cleanup,), timeout=1)
        return {
            "cancelled": True,
            "cleanup_confirmed": operation.cleanup_confirmed and not operation.cleanup_failed,
        }

    async def close(self):
        self.closed = True
        if self.operation is not None:
            self.abort(self.operation)
            with CancelScope(shield=True):
                await asyncio.wait((self.operation.cleanup,), timeout=1)
