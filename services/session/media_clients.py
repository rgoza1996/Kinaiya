"""Private media authentication custody and original owner-channel ownership.

These objects confer no decoder, provider, task or physical-cleanup authority.
The protected Session owner alone constructs principals and binds connections.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import WebSocket

from packages.contracts import media

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MAX = media.MAX_MEDIA_INTEGER


class MediaSessionError(ValueError):
    def __init__(self, code="unavailable"):
        self.code = (
            code
            if type(code) is str
            and code
            in {
                "bad_packet",
                "denied",
                "unknown_ref",
                "stale",
                "expired",
                "unsupported",
                "bound",
                "capacity",
                "unavailable",
                "conflict",
            }
            else "unavailable"
        )
        super().__init__("Invalid native media operation")


def refuse(code="unavailable"):
    raise MediaSessionError(code)


def private_header(headers, name, *, required=True):
    """Inspect raw header multiplicity before a normalized mapping can hide it."""
    values = [value for key, value in headers.raw if key.lower() == name.lower().encode("ascii")]
    if not values and not required:
        return None
    if len(values) != 1 or len(values[0]) != 64:
        refuse("bad_packet")
    try:
        value = values[0].decode("ascii")
    except UnicodeError:
        refuse("bad_packet")
    if _HEX.fullmatch(value) is None:
        refuse("bad_packet")
    return value


class _KeyDescriptor:
    def __init__(self):
        self.fd = None
        self.closed = False
        self.unknown = False

    def close(self):
        if self.fd is None:
            return self.closed and not self.unknown
        descriptor, self.fd = self.fd, None
        try:
            os.close(descriptor)
        except BaseException:
            self.unknown = True
            raise
        self.closed = True
        return True


class MediaGrantKeyStore:
    """One exclusive original owner of a persistent native32-byte HMAC key."""

    def __init__(self, path: Path, *, workspace: Path, spool_root: Path):
        self._lock = threading.RLock()
        self._owners = []
        self._key = None
        self._unknown = False
        self._closed = False
        self._file = None
        self._parent = None
        self._name = None
        self._directories = []
        try:
            path = Path(path)
            if not path.is_absolute() or ".." in path.parts or path.name in {"", "."}:
                refuse("bad_packet")
            # These roots are native setup values; never accept an ingress path.
            for root in (Path(workspace).absolute(), Path(spool_root).absolute()):
                if path == root or root in path.parents:
                    refuse("denied")
            current = self._open_directory(Path("/"))
            for component in path.parent.parts[1:]:
                current = self._open_directory(component, current.fd)
            parent = os.fstat(current.fd)
            if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                refuse("unavailable")
            self._parent, self._name = current, path.name
            owner = _KeyDescriptor()
            self._owners.append(owner)  # Before acquisition, including uncertain opens.
            created = False
            flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            try:
                owner.fd = os.open(
                    path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | flags, 0o600, dir_fd=current.fd
                )
                created = True
            except FileExistsError:
                owner.fd = os.open(path.name, os.O_RDONLY | flags, dir_fd=current.fd)
            fcntl.flock(owner.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(owner.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                refuse()
            if stat.S_IMODE(info.st_mode) != 0o600:
                refuse()
            if created:
                key = secrets.token_bytes(32)
                offset = 0
                while offset < 32:
                    count = os.write(owner.fd, key[offset:])
                    if count <= 0:
                        refuse()
                    offset += count
                os.fsync(owner.fd)
                os.fsync(current.fd)
            info = os.fstat(owner.fd)
            if info.st_size != 32:
                refuse()
            raw = os.pread(owner.fd, 33, 0)
            if len(raw) != 32:
                refuse()
            self._file = owner
            self._stamp = self._identity(info)
            self._key = raw
            self.key_sha256 = hashlib.sha256(raw).hexdigest()
            self.key_id = hashlib.sha256(b"kinaiya.media.grant-key.v1\0" + raw).hexdigest()[:32]
            self._check()
        except BaseException as error:
            self._unknown = True
            for owner in reversed(self._owners):
                try:
                    owner.close()
                except BaseException:
                    self._unknown = True
            self._key = None
            if not isinstance(error, Exception):
                raise
            raise MediaSessionError() from None

    @staticmethod
    def _identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _open_directory(self, name, parent=None):
        owner = _KeyDescriptor()
        self._owners.append(owner)
        owner.fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
        )
        info = os.fstat(owner.fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
            refuse()
        if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            refuse()
        self._directories.append((owner, parent, name, (info.st_dev, info.st_ino)))
        return owner

    def _check(self):
        if self._closed or self._unknown or self._key is None or self._file.fd is None:
            refuse()
        for owner, parent, name, identity in self._directories:
            held = os.fstat(owner.fd)
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(linked.st_mode)
                or (held.st_dev, held.st_ino) != identity
                or (linked.st_dev, linked.st_ino) != identity
                or held.st_uid not in {0, os.geteuid()}
                or held.st_mode & 0o022
                and not (held.st_uid == 0 and held.st_mode & stat.S_ISVTX)
            ):
                self._unknown = True
                refuse()
        parent_info = os.fstat(self._parent.fd)
        if parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
            self._unknown = True
            refuse()
        info = os.fstat(self._file.fd)
        current = os.stat(self._name, dir_fd=self._parent.fd, follow_symlinks=False)
        if (
            self._identity(info) != self._stamp
            or self._identity(current) != self._stamp
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            self._unknown = True
            refuse()

    def authenticate_digest(self, domain: bytes, body: bytes) -> str:
        if type(domain) is not bytes or type(body) is not bytes or len(body) > 65_536:
            refuse("bound")
        with self._lock:
            try:
                self._check()
            except BaseException as error:
                self._unknown = True
                if not isinstance(error, Exception):
                    raise
                raise MediaSessionError() from None
            return hmac.new(self._key, domain + body, hashlib.sha256).hexdigest()

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return not self._unknown
            positive = True
            for owner in reversed(self._owners):
                try:
                    positive = owner.close() and positive
                except BaseException as error:
                    self._unknown = True
                    positive = False
                    if not isinstance(error, Exception):
                        raise
            self._key = None
            self._closed = positive
            return positive and not self._unknown


@dataclass(frozen=True, slots=True, repr=False)
class NativeMediaPrincipal:
    issuer: object
    auth_identity: object
    auth_generation: int
    app_instance: str
    role: str
    client_capability: str | None = field(repr=False)


@dataclass(slots=True, eq=False, repr=False)
class MediaConnection:
    instance: str
    ws: WebSocket
    principal: NativeMediaPrincipal
    source: object
    after: int
    source_id: str | None = None
    client_instance: str | None = None
    session_id: str | None = None
    receiver: asyncio.Task | None = None
    sender: asyncio.Task | None = None
    fence_job: asyncio.Task | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    fenced: bool = False
    unknown: bool = False
    joined: bool = False
    disconnected: bool = False
    retirement_task: asyncio.Task | None = None


class MediaOwnerEventHub:
    """Own the original media-mode sender and exactly ONE retained receiver."""

    def __init__(self, owner):
        self._owner = owner
        self._connections = {}
        self._closed = False

    async def serve_authenticated(self, ws, hello):
        connection = None
        try:
            if (
                self._closed
                or type(hello) is not dict
                or set(hello) - {"token", "after", "session_id", "media"}
            ):
                refuse("bad_packet")
            private = hello.get("media")
            if type(private) is not dict or set(private) != {"v", "source", "client_capability"}:
                refuse("bad_packet")
            if type(private["v"]) is not int or private["v"] != 1:
                refuse("bad_packet")
            capability = private["client_capability"]
            if type(capability) is not str or _HEX.fullmatch(capability) is None:
                refuse("bad_packet")
            after = hello.get("after", 0)
            if type(after) is not int or not 0 <= after <= _MAX:
                refuse("bad_packet")
            token, session_id = hello.get("token"), hello.get("session_id")
            if (
                type(token) is not str
                or len(token) > 4096
                or type(session_id) is not str
                or not 0 < len(session_id) <= 128
                or len(session_id.encode("utf-8", "strict")) > 128
            ):
                refuse("bad_packet")
            source_fields = private["source"]
            if type(source_fields) is not dict or set(source_fields) != {
                "backend_id",
                "receipt_id",
                "sha256",
            }:
                refuse("bad_packet")
            if any(
                type(source_fields[name]) is not str or len(source_fields[name]) != size
                for name, size in (("backend_id", 32), ("receipt_id", 32), ("sha256", 64))
            ):
                refuse("bad_packet")
            source = media.MediaNativeRef.model_validate(private["source"], strict=True)
            principal = self._owner.authenticate_websocket(ws, token, capability)
            if len(self._connections) >= 16:
                refuse("capacity")
            connection = MediaConnection(secrets.token_hex(16), ws, principal, source, after)
            self._connections[connection.instance] = connection
            # A retained registry member exists before association or task scheduling.
            ack = await self._owner.run_native(
                self._owner.associate_connection, connection, hello.get("session_id")
            )
            connection.receiver = asyncio.create_task(self._receive(connection))
            connection.sender = asyncio.create_task(self._send(connection))
            await ws.send_json(ack)
            connection.started.set()
            done, _ = await asyncio.wait(
                {connection.receiver, connection.sender}, return_when=asyncio.FIRST_COMPLETED
            )
            for original in done:
                original.result()
        except BaseException as error:
            if connection is not None:
                connection.unknown = not isinstance(error, Exception)
                self.fence(connection.instance, "disconnect")
            if not isinstance(error, Exception):
                raise
        finally:
            if connection is not None:
                self.fence(connection.instance, "disconnect")
                await asyncio.shield(self.retire(connection.instance))
            else:
                try:
                    await ws.close(code=1008)
                except Exception:
                    pass

    async def _receive(self, connection):
        try:
            while not connection.fenced:
                remaining = self._owner.connection_remaining(connection)
                try:
                    event = await asyncio.wait_for(
                        connection.ws.receive(), timeout=min(remaining, 1.0)
                    )
                except TimeoutError:
                    continue
                if event.get("type") == "websocket.disconnect":
                    connection.disconnected = True
                    return
                # No post-hello media WS command grammar has been selected.
                refuse("bad_packet")
        finally:
            self.fence(connection.instance, "disconnect")

    async def _send(self, connection):
        await connection.started.wait()
        while not connection.fenced:
            self._owner.connection_remaining(connection)
            entries = await self._owner.run_native(
                self._owner.store.read,
                after=connection.after,
                limit=100,
                session_id=connection.session_id,
            )
            for event in entries:
                self._owner.connection_remaining(connection)
                await connection.ws.send_json(event.model_dump(mode="json"))
                connection.after = event.sequence
            if not entries:
                await asyncio.sleep(0.1)

    def fence(self, connection_instance, reason):
        connection = self._connections.get(connection_instance)
        if connection is None:
            return
        connection.fenced = True
        self._owner.fence_connection_memory(connection, reason)
        if connection.fence_job is None:
            connection.fence_job = asyncio.create_task(
                self._owner.run_native(self._owner.fence_connection_locked, connection, reason)
            )

    async def retire(self, connection_instance):
        connection = self._connections.get(connection_instance)
        if connection is None:
            return False
        if connection.retirement_task is None:
            connection.retirement_task = asyncio.create_task(self._retire_original(connection))
        return await asyncio.shield(connection.retirement_task)

    async def _retire_original(self, connection):
        connection_instance = connection.instance
        self.fence(connection_instance, "disconnect")
        try:
            await connection.ws.close(code=1008)
        except BaseException:
            connection.unknown = True
        tasks = {
            task
            for task in (connection.receiver, connection.sender)
            if task is not None and task is not asyncio.current_task()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if connection.fence_job is not None:
            try:
                await asyncio.shield(connection.fence_job)
            except BaseException:
                connection.unknown = True
                return False
        if connection.unknown:
            return False
        connection.joined = True
        await self._owner.run_native(self._owner.retire_connection_locked, connection)
        self._connections.pop(connection.instance, None)
        return True

    async def close(self):
        self._closed = True
        originals = tuple(self._connections)
        for instance in originals:
            self.fence(instance, "disconnect")
        results = await asyncio.gather(
            *(self.retire(instance) for instance in originals), return_exceptions=True
        )
        return all(result is True for result in results)
