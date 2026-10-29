"""Managed GGUF endpoint; native evidence never impersonates the Qwen IPC."""

import asyncio
import json
import os
import stat
import tempfile
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.providers.owned_unix_http import OwnedUnixTransport
from packages.providers.text import (
    ProviderCleanupError,
    _iter_compatible_events,
    _OwnedHTTPStream,
    _reject_compressed_response,
)
from packages.providers.whistle_assets import outside_workspace
from packages.resources.processes import WorkerSpec, _proc_stat_full, _scan_group
from services.resources.gguf_peer import NativeLease

_MAX_COUNT_BYTES = 4096
_MAX_SAFE_COUNT = 2**53 - 1


def _parse_prompt_count(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Native prompt count is invalid") from exc

    def _invalid(value):
        raise ValueError("Native prompt count is invalid")

    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Native prompt count is invalid")
            result[key] = value
        return result

    try:
        value = json.loads(text, parse_constant=_invalid, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Native prompt count is invalid") from exc
    if type(value) is not dict or set(value) != {"input_tokens", "object"}:
        raise ValueError("Native prompt count is invalid")
    if value["object"] != "response.input_tokens":
        raise ValueError("Native prompt count is invalid")
    count = value["input_tokens"]
    if type(count) is not int or not 1 <= count <= _MAX_SAFE_COUNT:
        raise ValueError("Native prompt count is invalid")
    return count


class ManagedGGUFConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    python: str = Field(min_length=1, max_length=4096)
    manifest: str = Field(min_length=1, max_length=4096)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_tokens: int = Field(default=2048, ge=64, le=32768)
    max_new_tokens: int = Field(default=128, ge=16, le=2048)
    cpu_affinity_count: int = Field(default=2, ge=2, le=2)
    memory_bytes: int = Field(default=16 * 1024**3, ge=8 * 1024**3, le=32 * 1024**3)
    host_memory_reserve_bytes: int = Field(default=8 * 1024**3, ge=8 * 1024**3, le=32 * 1024**3)
    startup_timeout_s: float = Field(default=120, ge=1, le=300)
    request_timeout_s: float = Field(default=120, ge=1, le=900)

    @model_validator(mode="after")
    def output_fits_context(self):
        if self.max_new_tokens >= self.context_tokens:
            raise ValueError("Native output must leave space for a prompt")
        return self


def configured_manifest(settings, workspace):
    from packages.providers.gguf_assets import load_manifest

    outside_workspace(settings.python, workspace)
    return load_manifest(settings.manifest, settings.manifest_sha256, workspace)


def configured_binding(settings, manifest, resources_url):
    return {
        "provider": "managed-gguf",
        "model": manifest["model"],
        "profile": manifest["profile"],
        "manifest_sha256": settings.manifest_sha256,
        "resources_url": resources_url,
        "context_tokens": settings.context_tokens,
        "max_new_tokens": settings.max_new_tokens,
    }


def _directory_identity(path):
    row = path.lstat()
    if (
        not stat.S_ISDIR(row.st_mode)
        or row.st_uid != os.geteuid()
        or row.st_mode & 0o077
        or path.resolve(strict=True) != path
    ):
        raise ValueError("Native IPC directory is not private/canonical")
    return row.st_dev, row.st_ino


async def _join(task):
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


@dataclass(eq=False)
class _Exchange:
    lease: NativeLease
    client: httpx.AsyncClient
    transport: OwnedUnixTransport
    cleanup: asyncio.Task | None = None

    async def close(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self.client.aclose())
        await _join(self.cleanup)
        if self.transport.cleanup_pending:
            raise ProviderCleanupError("Native socket cleanup remains retained")


class GGUFEndpoint:
    def __init__(
        self, directory, manifest, *, context_tokens=2048, max_new_tokens=128, request_timeout_s=120
    ):
        self.directory = Path(directory)
        self.ipc = self.directory / "ipc"
        self.parent_inode = _directory_identity(self.directory)
        self.ipc_inode = _directory_identity(self.ipc)
        self.manifest = manifest
        self.limits = {"context_tokens": context_tokens, "max_new_tokens": max_new_tokens}
        self.timeout = request_timeout_s
        self.executable_pin = next(
            row for row in manifest["assets"] if row["guest"] == "/runtime/llama-server"
        )
        if not 0 < self.executable_pin["bytes"] <= 64 * 1024**2:
            raise ValueError("Native executable verification exceeds its bound")
        self.ready = None
        self.leases = {}
        self.exchanges = set()
        self.released_identities = weakref.WeakSet()
        self.release_lock = asyncio.Lock()

    def check_directories(self):
        if (
            _directory_identity(self.directory) != self.parent_inode
            or _directory_identity(self.ipc) != self.ipc_inode
        ):
            raise ValueError("Native IPC directory incarnation changed")

    def _phase(self, pid):
        from packages.providers.gguf_assets import _pairs

        self.check_directories()
        path = self.directory / "phase.json"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            row = os.fstat(fd)
            if (
                not stat.S_ISREG(row.st_mode)
                or row.st_uid != os.geteuid()
                or row.st_nlink != 1
                or row.st_mode & 0o077
                or not 0 < row.st_size <= 1024
            ):
                raise ValueError("Native startup phase is unavailable")
            raw = os.read(fd, 1025)
            value = json.loads(raw, object_pairs_hook=_pairs)
            if (
                len(raw) != row.st_size
                or type(value) is not dict
                or set(value) != {"version", "pid", "phase"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or type(value["pid"]) is not int
                or value["pid"] != pid
                or type(value["phase"]) is not str
                or value["phase"] not in {"preparing", "exec_pending", "pre_exec_failed"}
            ):
                raise ValueError("Native startup phase differs")
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (row.st_dev, row.st_ino):
                raise ValueError("Native startup phase changed")
            return value["phase"], (row.st_dev, row.st_ino)
        finally:
            os.close(fd)

    @asynccontextmanager
    async def connection(self, lease):
        self.check_directories()
        if self.exchanges:
            raise ProviderCleanupError("Native HTTP exchange or cleanup is already retained")

        async def verify(peer):
            self.check_directories()
            lease.verify_peer(peer)

        transport = OwnedUnixTransport(lease.socket_path, verify)
        client = httpx.AsyncClient(
            base_url="http://localhost",
            transport=transport,
            timeout=self.timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        )
        exchange = _Exchange(lease, client, transport)
        self.exchanges.add(exchange)
        try:
            yield client
        finally:
            await exchange.close()
            self.exchanges.discard(exchange)

    async def probe(self, spec, process):
        if process.returncode is not None:
            return False
        try:
            self.check_directories()
            row = _proc_stat_full(process.pid)
            if row is None or row[2] != row[3] or row[2] != process.pid:
                return False
            lease = self.leases.get(process.pid)
            if lease is None:
                if self.leases:
                    return False
                lease = NativeLease(
                    process.pid, row[4], self.executable_pin, self.ipc / "model.sock"
                )
                self.leases[process.pid] = lease
            if lease.start_time != row[4]:
                return False
            # Observe startup descendants even before the listener is ready.
            # Failure retains partial handles rather than inventing extinction.
            group, scanned = _scan_group(process.pid)
            if not scanned or len(group) > 16:
                return False
            for pid in group:
                try:
                    lease.observe(pid)
                    lease.observe_native(pid)
                except (OSError, ValueError):
                    continue
            async with self.connection(lease) as client:
                async with client.stream("GET", "/health") as response:
                    response.stream = _OwnedHTTPStream(response.stream)
                    _reject_compressed_response(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 4096:
                            raise ValueError("Native health exceeds its bound")
                        raw.extend(chunk)
                    if response.status_code != 200:
                        return False
            if process.returncode is not None or not lease.leader_current():
                return False
            self.ready = lease
            return True
        except (OSError, ValueError, RuntimeError, httpx.HTTPError):
            return False

    def ready_for(self, identity):
        if self.ready is None:
            raise ValueError("Native ready lease is unavailable")
        return self.ready.bind(identity)

    def current(self, lease, identity):
        self.check_directories()
        if self.ready is not lease:
            raise ValueError("Native ready lease changed")
        lease.current(identity)

    def _body(self, messages):
        return {
            "model": self.manifest["alias"],
            "messages": [dict(row) for row in messages],
            "max_tokens": self.limits["max_new_tokens"],
            "stream": True,
        }

    async def stream(self, messages, lease):
        self.current(lease, lease.identity)
        terminal, forwarded = None, 0
        async with self.connection(lease) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json=self._body(messages),
            ) as response:
                response.stream = _OwnedHTTPStream(response.stream)
                if response.status_code != 200:
                    raise ValueError("Native generation request was refused")
                _reject_compressed_response(response)
                async for event in _iter_compatible_events(response):
                    self.current(lease, lease.identity)
                    encoded = (
                        b"".join(
                            b"data: " + line.encode("utf-8") + b"\n"
                            for line in event.data.split("\n")
                        )
                        + b"\n"
                    )
                    forwarded += len(encoded)
                    if forwarded > 1024**2:
                        raise ValueError("Native public SSE exceeds its byte bound")
                    if event.done:
                        terminal = encoded
                    else:
                        yield encoded
        # DONE is actual native data, exposed only after source EOF and real
        # response/client/socket close. No generic stop is converted to EOS.
        self.current(lease, lease.identity)
        if terminal is None:
            raise ValueError("Native generation ended without DONE")
        yield terminal

    async def measure_prompt(self, messages, lease):
        self.current(lease, lease.identity)
        body = self._body(messages)
        status = None
        raw = bytearray()
        async with self.connection(lease) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions/input_tokens",
                json=body,
            ) as response:
                response.stream = _OwnedHTTPStream(response.stream)
                _reject_compressed_response(response)
                status = response.status_code
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > _MAX_COUNT_BYTES:
                        raise ValueError("Native prompt count exceeds its bound")
                    raw.extend(chunk)
        self.current(lease, lease.identity)
        if status in (404, 405, 501):
            return None
        if status != 200:
            raise ValueError("Native prompt count request was refused")
        return _parse_prompt_count(bytes(raw))

    async def release_after_stop(self, identity, result):
        async with self.release_lock:
            return await self._release_after_stop(identity, result)

    async def _release_after_stop(self, identity, result):
        if identity is not None and identity in self.released_identities:
            return True
        if result.get("ok") is not True:
            return False
        selected = [
            lease
            for lease in self.leases.values()
            if identity is None
            or (lease.outer_pid, lease.start_time) == (identity.pid, identity.start_time)
        ]
        if not selected:
            # An unknown superseded binding may not borrow a new lease's proof.
            if identity is not None:
                return False
            self.check_directories()
            # The trusted child publishes a parent-only phase receipt before
            # creating/sealing/executing native inputs. It cannot remove that
            # receipt after exec. Absence plus empty IPC means never launched
            # or a previously confirmed/pruned release, never an adopted peer.
            if not os.path.lexists(self.directory / "phase.json"):
                return not self.exchanges and not list(self.ipc.iterdir())
            proof = result.get("stop_evidence") or {}
            if proof.get("pid") is not None:
                phase, phase_inode = self._phase(proof["pid"])
                if (
                    phase != "pre_exec_failed"
                    or not proof.get("reaped")
                    or not proof.get("group_empty")
                    or proof.get("ownership_verified") is not True
                ):
                    return False
                self._prune_phase(phase_inode)
                return not self.exchanges
            return False
        for lease in selected:
            if (
                identity is not None
                and lease.identity is not None
                and lease.identity is not identity
            ):
                return False
            proof = result.get("stop_evidence") or {}
            if (
                not result.get("ok")
                or proof.get("pid") != lease.outer_pid
                or not proof.get("reaped")
                or not proof.get("group_empty")
                or proof.get("ownership_verified") is not True
            ):
                return False
            exchanges = [item for item in self.exchanges if item.lease is lease]
            results = await asyncio.gather(
                *(item.close() for item in exchanges), return_exceptions=True
            )
            if any(isinstance(item, BaseException) for item in results):
                return False
            for item in exchanges:
                self.exchanges.discard(item)
            phase, phase_inode = self._phase(lease.outer_pid)
            if lease.native_pid is None or lease.init_pid is None:
                if phase != "pre_exec_failed" or not lease.handles:
                    return False
                from packages.resources.processes import _pidfd_pid

                if not all(_pidfd_pid(fd) == -1 for fd in lease.handles.values()):
                    return False
            elif not await lease.extinct():
                return False
            lease.forget()
            self._prune_phase(phase_inode)
            released_identity = identity if identity is not None else lease.identity
            if released_identity is not None:
                self.released_identities.add(released_identity)
            del self.leases[lease.outer_pid]
            if self.ready is lease:
                self.ready = None
        return True

    def _prune_phase(self, expected):
        self.check_directories()
        path = self.directory / "phase.json"
        row = path.lstat()
        if (row.st_dev, row.st_ino) != expected:
            raise ValueError("Native startup phase changed before pruning")
        path.unlink()

    def close(self):
        self.check_directories()
        if self.leases or self.exchanges or self.ready is not None:
            raise ValueError("Native owned cleanup remains retained")
        if list(self.ipc.iterdir()) or set(path.name for path in self.directory.iterdir()) != {
            "ipc"
        }:
            raise ValueError("Unexpected native IPC entry")
        self.ipc.rmdir()
        self.directory.rmdir()


def gguf_worker(settings, root, workspace):
    manifest = configured_manifest(settings, workspace)
    directory = Path(tempfile.mkdtemp(prefix="kinaiya-gguf-"))
    try:
        outside_workspace(directory, workspace)
        (directory / "ipc").mkdir(mode=0o700)
        endpoint = GGUFEndpoint(
            directory,
            manifest,
            context_tokens=settings.context_tokens,
            max_new_tokens=settings.max_new_tokens,
            request_timeout_s=settings.request_timeout_s,
        )
        spec = WorkerSpec(
            id="gguf",
            argv=(
                settings.python,
                "-m",
                "packages.providers.gguf_worker",
                "--manifest",
                settings.manifest,
                "--manifest-sha256",
                settings.manifest_sha256,
                "--socket-dir",
                str(directory),
                "--workspace",
                str(workspace),
                "--ctx-size",
                str(settings.context_tokens),
                "--max-new-tokens",
                str(settings.max_new_tokens),
                "--cpu-count",
                "2",
                "--memory-bytes",
                str(settings.memory_bytes),
            ),
            cwd=root,
            env={"PYTHONPATH": str(root), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            startup_timeout_s=settings.startup_timeout_s,
            stop_timeout_s=3,
        )
        return spec, endpoint
    except BaseException:
        (directory / "ipc").rmdir()
        directory.rmdir()
        raise


class GGUFProtocol:
    worker_id = "gguf"
    max_request_bytes = 512 * 1024

    def __init__(self, endpoint, manifest_sha256):
        self.endpoint, self.manifest_sha256 = endpoint, manifest_sha256

    def policy(self, coordinator):
        return coordinator.text_policy()

    async def admit(self, coordinator, policy):
        return await coordinator.admit_text(policy)

    def ready_for(self, identity):
        return self.endpoint.ready_for(identity)

    def current(self, lease, identity):
        self.endpoint.current(lease, identity)

    def status(self):
        return {
            "profile": self.endpoint.manifest["profile"],
            "model": self.endpoint.manifest["model"],
            "manifest_sha256": self.manifest_sha256,
            **self.endpoint.limits,
            "native_http_owners": len(self.endpoint.exchanges),
            "native_leases_retained": len(self.endpoint.leases),
        }

    def parse(self, raw):
        from packages.providers.qwen3_ipc import decode_json, validate_messages

        value = decode_json(raw, self.max_request_bytes)
        if (
            set(value) != {"model", "messages", "max_tokens", "stream"}
            or value["model"] != self.endpoint.manifest["alias"]
            or type(value["max_tokens"]) is not int
            or value["max_tokens"] != self.endpoint.limits["max_new_tokens"]
            or value["stream"] is not True
        ):
            raise ValueError("Native request differs from its protected profile")
        return validate_messages(value["messages"])

    def error(self, code):
        # Compatible clients refuse this trusted-host error payload. It is not
        # a model delta, a native completion or a fabricated usage report.
        return b'event: error\ndata: {"error":{"code":"' + code.encode("ascii") + b'"}}\n\n'

    async def stop_legacy(self, manager, identity, lease):
        raise RuntimeError("GGUF requires its registered resource lifecycle")

    def resident(self, coordinator, policy):
        return coordinator.resident_text(policy)

    def _measurement_binding(self):
        return {
            "provider": "managed-gguf",
            "model": self.endpoint.manifest["model"],
            "profile": self.endpoint.manifest["profile"],
            "manifest_sha256": self.manifest_sha256,
            "context_tokens": self.endpoint.limits["context_tokens"],
            "max_new_tokens": self.endpoint.limits["max_new_tokens"],
        }

    async def measure_prompt(self, messages, identity, lease):
        from packages.providers.prompt_budget import (
            PromptTokenCount,
            binding_digest,
            request_digest,
        )

        endpoint = self.endpoint
        body, binding = endpoint._body(messages), self._measurement_binding()
        request_sha256, binding_sha256 = request_digest(body), binding_digest(binding)
        # Preserve the exact request before dispatch. HTTP close may await long
        # enough for the caller or a replacement configuration to change.
        count = await endpoint.measure_prompt(body["messages"], lease)
        if (
            self.endpoint is not endpoint
            or endpoint._body(messages) != body
            or self._measurement_binding() != binding
        ):
            raise ValueError("Native prompt measurement binding changed")
        self.current(lease, identity)
        if count is None:
            return None
        return PromptTokenCount(
            input_tokens=count,
            context_tokens=binding["context_tokens"],
            reserved_output_tokens=binding["max_new_tokens"],
            request_sha256=request_sha256,
            binding_sha256=binding_sha256,
        )

    async def stream(self, messages, identity, lease):
        from contextlib import aclosing

        async with aclosing(self.endpoint.stream(messages, lease)) as source:
            async for data in source:
                yield data
