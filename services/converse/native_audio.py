"""Bounded, host-only ownership of imported native Converse audio.

An authenticated clip is one explicit owner input, not a media-observation
permission. Durable intents survive loss of the process-local PCM cache and
never authorize re-running ASR. Disconnecting an HTTP observer leaves the same
shielded original running; currentness/off and close fence that original.
This module does not enable a catalog adapter, grant Agent work, or prove
physical device/model cleanup. The controller owns qualified reply history
and the original reply across uncertain native publication.
"""

import asyncio
import base64
import binascii
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.contracts.models import ContentScope, SpeechWindow
from packages.providers.realtime_audio import NativeAudioInput, NativeAudioReply
from packages.providers.speech import SpeechProviderError
from packages.providers.text import ProviderCleanupError, ProviderRefusal
from services.converse.embedding import (
    host_model_selected,
    runtime_model_preparation,
    runtime_model_settings,
    runtime_provider_info,
)
from services.converse.model_settings import _storage

INPUT_RATE = 16000
MAX_SECONDS = 30
MAX_WAV_BYTES = 960044
MAX_BASE64 = ((MAX_WAV_BYTES + 2) // 3) * 4
MAX_CLIPS = 32
MAX_RETAINED_BYTES = 16 * 1024 * 1024
REPLY_RESERVATION = 2 * 1024 * 1024
MAX_INTENTS = 4096
_STATES = {"reserved", "asr", "append_pending", "published", "refused", "unknown"}
_SCOPES = {"local_only", "public_synthetic", "private_cloud_authorized"}
_UUID_FIELDS = ("session_id", "thread_id", "utterance_id", "request_id")


def _error(code):
    return SpeechProviderError(code)


def _uuid(value):
    if type(value) is not str or len(value) != 36:
        raise ValueError("Invalid native audio identifier")
    try:
        if str(UUID(value)) != value:
            raise ValueError("Invalid native audio identifier")
    except (ValueError, AttributeError):
        raise ValueError("Invalid native audio identifier") from None
    return value


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _wav(encoded):
    if type(encoded) is not str or not 1 <= len(encoded) <= MAX_BASE64:
        raise ValueError("Invalid bounded native audio")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("Invalid bounded native audio") from None
    if (
        len(raw) < 46
        or len(raw) > MAX_WAV_BYTES
        or base64.b64encode(raw).decode("ascii") != encoded
    ):
        raise ValueError("Invalid bounded native audio")
    fields = struct.unpack("<4sI4s4sIHHIIHH4sI", raw[:44])
    (
        riff,
        size,
        wave,
        fmt,
        fmt_size,
        encoding,
        channels,
        rate,
        byte_rate,
        alignment,
        bits,
        data,
        count,
    ) = fields
    if (
        (riff, wave, fmt, fmt_size, encoding, channels, rate, byte_rate, alignment, bits, data)
        != (b"RIFF", b"WAVE", b"fmt ", 16, 1, 1, INPUT_RATE, INPUT_RATE * 2, 2, 16, b"data")
        or size != len(raw) - 8
        or count != len(raw) - 44
        or count % 2
        or not 0 < count // 2 <= MAX_SECONDS * INPUT_RATE
    ):
        raise ValueError("Invalid bounded native audio")
    return raw


class NativeClipRequest(BaseModel):
    """One owner-submitted canonical PCM16 mono 16 kHz WAV, not a live arm."""

    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")
    session_id: str
    thread_id: str
    epoch: int = Field(strict=True, ge=1)
    utterance_id: str
    request_id: str
    content_scope: ContentScope
    audio_base64: str = Field(max_length=MAX_BASE64)

    _ids = field_validator(*_UUID_FIELDS, mode="before")(_uuid)

    @field_validator("content_scope", mode="before")
    @classmethod
    def strict_scope(cls, value):
        if type(value) is not str or value not in _SCOPES:
            raise ValueError("Invalid native audio scope")
        return value

    @field_validator("audio_base64", mode="before")
    @classmethod
    def audio(cls, value):
        _wav(value)
        return value


@dataclass(eq=False)
class _Clip:
    request_id: str
    request_sha256: str
    session_id: str
    thread_id: str
    epoch: int
    utterance_id: str
    content_scope: str
    wav: bytes
    pcm: bytes
    audio_sha256: str
    capture: tuple
    state: str = "reserved"
    text: str | None = None
    source_id: str | None = None
    task: asyncio.Task | None = None
    assigned: asyncio.Event = field(default_factory=asyncio.Event)
    coroutine: object = None
    fenced: bool = False
    failure: int | None = None

    @property
    def key(self):
        return self.session_id, self.thread_id, self.epoch, self.utterance_id

    @property
    def reserved_bytes(self):
        return len(self.wav) + len(self.pcm) + REPLY_RESERVATION


@dataclass(frozen=True)
class _Reply:
    clip: _Clip
    binding: tuple
    pcm: bytes | None
    rate: int


class NativeAudioOwner:
    def __init__(self, runtime, provider, *, path, workspace):
        self.runtime, self.provider = runtime, provider
        # Keep custody of the constructor's actual adapter even if a caller
        # later replaces the public selection. Cleanup may never retarget.
        self._original_provider = provider
        self._original_provider_close = None
        self._runtime_id = runtime._runtime_id
        self._clips = {}
        self._utterances = {}
        self._replies = {}
        self._bytes = 0
        self._sealed = self._closed = self._failed = self._cleanup_failed = False
        self._close_task = self._provider_close_task = None
        self._owner_fd = None
        self._db_closed = False
        self.db = None
        try:
            self._original_provider_close = provider.close
            if not callable(self._original_provider_close):
                raise ValueError("Native audio close ownership is unavailable")
            path = Path(os.path.abspath(path))
            self.db, created = _storage(path, workspace, report_created=True)
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            self._owner_fd = descriptor
            info, actual = os.fstat(descriptor), path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)
            ):
                raise ValueError("Native audio storage identity changed")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db.execute("PRAGMA busy_timeout=500")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._initialize(created)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        except BaseException:
            if self.db is not None:
                self.db.close()
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None
            raise _error(503) from None

    def _initialize(self, created):
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if created and not tables and version == 0:
            self.db.execute(
                "CREATE TABLE native_audio_intents ("
                "request_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, "
                "session_id TEXT NOT NULL, thread_id TEXT NOT NULL, epoch TEXT NOT NULL, "
                "utterance_id TEXT NOT NULL, content_scope TEXT NOT NULL, "
                "audio_sha256 TEXT NOT NULL, state TEXT NOT NULL, "
                "source_id TEXT, text_sha256 TEXT, "
                "UNIQUE(session_id,thread_id,epoch,utterance_id))"
            )
            self.db.execute("PRAGMA user_version=1")
        elif version != 1 or tables != {"native_audio_intents"}:
            raise ValueError("Native audio storage is unavailable")
        expected = [
            ("request_id", "TEXT", 0, 1),
            ("request_sha256", "TEXT", 1, 0),
            ("session_id", "TEXT", 1, 0),
            ("thread_id", "TEXT", 1, 0),
            ("epoch", "TEXT", 1, 0),
            ("utterance_id", "TEXT", 1, 0),
            ("content_scope", "TEXT", 1, 0),
            ("audio_sha256", "TEXT", 1, 0),
            ("state", "TEXT", 1, 0),
            ("source_id", "TEXT", 0, 0),
            ("text_sha256", "TEXT", 0, 0),
        ]
        actual = [
            (row[1], row[2], row[3], row[5])
            for row in self.db.execute("PRAGMA table_info(native_audio_intents)")
        ]
        if actual != expected:
            raise ValueError("Native audio storage is unavailable")
        rows = self.db.execute("SELECT * FROM native_audio_intents LIMIT 4097").fetchall()
        if len(rows) > MAX_INTENTS:
            raise ValueError("Native audio storage is unavailable")
        for row in rows:
            self._validate_row(row)

    @staticmethod
    def _validate_row(row):
        if len(row) != 11:
            raise ValueError("Invalid native audio intent")
        for index in (0, 2, 3, 5):
            _uuid(row[index])
        for index in (1, 7):
            if (
                type(row[index]) is not str
                or len(row[index]) != 64
                or any(c not in "0123456789abcdef" for c in row[index])
            ):
                raise ValueError("Invalid native audio intent")
        if (
            type(row[4]) is not str
            or not row[4].isascii()
            or not row[4].isdigit()
            or int(row[4]) < 1
            or str(int(row[4])) != row[4]
        ):
            raise ValueError("Invalid native audio intent")
        if row[6] not in _SCOPES or row[8] not in _STATES:
            raise ValueError("Invalid native audio intent")
        if row[9] is not None:
            _uuid(row[9])
        if row[10] is not None and (
            type(row[10]) is not str
            or len(row[10]) != 64
            or any(c not in "0123456789abcdef" for c in row[10])
        ):
            raise ValueError("Invalid native audio intent")
        if row[8] == "published" and (row[9] is None or row[10] is None):
            raise ValueError("Invalid native audio intent")
        return row

    def _transaction(self, operation):
        try:
            if self._db_closed or self.db.in_transaction:
                raise ValueError("Native audio transaction is unavailable")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                result = operation()
                self.db.commit()
                return result
            except BaseException:
                self.db.rollback()
                raise
        except BaseException:
            self._failed = True
            raise _error(503) from None

    def _row(self, request_id):
        try:
            if self._db_closed:
                raise ValueError("Closed native audio storage")
            row = self.db.execute(
                "SELECT * FROM native_audio_intents WHERE request_id=?", (request_id,)
            ).fetchone()
            return None if row is None else self._validate_row(row)
        except (ValueError, TypeError, sqlite3.Error, OverflowError):
            self._failed = True
            raise _error(503) from None

    def _verify_row(self, clip):
        row = self._row(clip.request_id)
        expected = (
            clip.request_id,
            clip.request_sha256,
            clip.session_id,
            clip.thread_id,
            str(clip.epoch),
            clip.utterance_id,
            clip.content_scope,
            clip.audio_sha256,
        )
        if row is None or row[:8] != expected or row[8] != clip.state or row[9] != clip.source_id:
            self._failed = True
            raise _error(503)
        expected_text = None if clip.text is None else _digest(clip.text.encode("utf-8"))
        if row[10] != expected_text:
            self._failed = True
            raise _error(503)

    def _state(self, clip, state, *, source_id=None):
        previous = clip.state
        text_sha = None if clip.text is None else _digest(clip.text.encode("utf-8"))
        target_source = clip.source_id if source_id is None else source_id

        def update():
            row = self._row(clip.request_id)
            if row is None or row[1] != clip.request_sha256 or row[8] != previous:
                raise ValueError("Original native audio intent changed")
            changed = self.db.execute(
                "UPDATE native_audio_intents SET state=?,source_id=?,text_sha256=? "
                "WHERE request_id=? AND request_sha256=? AND state=?",
                (state, target_source, text_sha, clip.request_id, clip.request_sha256, previous),
            )
            if changed.rowcount != 1:
                raise ValueError("Original native audio intent changed")

        self._transaction(update)
        clip.state, clip.source_id = state, target_source

    def _native_stamp(self):
        binding = getattr(self.provider, "binding_token", None)
        selected = (
            binding()
            if callable(binding)
            else (getattr(self.provider, "base_url", None), getattr(self.provider, "model", None))
        )
        return (
            id(self.provider),
            type(self.provider),
            selected,
            getattr(binding, "__func__", None),
            getattr(getattr(self.provider, "transcribe", None), "__func__", None),
            getattr(getattr(self.provider, "respond", None), "__func__", None),
        )

    def _transport_available(self) -> bool:
        check = getattr(self.provider, "transport_available", None)
        if check is None:
            return True
        if not callable(check):
            return False
        try:
            return check() is True
        except Exception:
            return False

    def _capture(self):
        if self._sealed or self._closed or self._failed or self._cleanup_failed:
            raise _error(503)
        try:
            if getattr(self.runtime, "native_audio", None) is not self:
                raise _error(409)
            if self.provider is not self._original_provider:
                raise _error(409)
            close = getattr(self.provider, "close", None)
            original_close = self._original_provider_close
            if getattr(close, "__self__", None) is not getattr(
                original_close, "__self__", None
            ) or getattr(close, "__func__", close) is not getattr(
                original_close, "__func__", original_close
            ):
                raise _error(409)
            if not host_model_selected(self.runtime) or not self.runtime.enabled:
                raise _error(409)
            info = runtime_provider_info(self.runtime)
            if type(info.local) is not bool or info.local is not True:
                raise _error(409)
            if not self._transport_available():
                raise _error(503)
            settings = runtime_model_settings(self.runtime)
            token = None if settings is None else settings.state_token()
            return (
                self.runtime.admission(),
                self.runtime._runtime_id,
                id(self.runtime.provider),
                id(settings),
                token,
                _canonical(info.model_dump(mode="json")),
                self._native_stamp(),
            )
        except SpeechProviderError:
            raise
        except Exception:
            raise _error(503) from None

    def _local(self, clip):
        task = asyncio.current_task()
        # Selected recall is not qualified for this unmeasured audio protocol.
        # Another conversation's independent memory choice cannot revoke this
        # clip; enforce the unsupported choice at its own exact scope only.
        choice = self.runtime.memory.choices.get(clip.key[:3])
        return (
            not clip.fenced
            and not (task is not None and task.cancelling())
            and (choice is None or not choice.enabled)
            and clip.capture == self._capture()
            and self.runtime._runtime_id == self._runtime_id
            and self._clips.get(clip.request_id) is clip
            and self._utterances.get(clip.key) is clip
        )

    @property
    def available(self):
        try:
            self._capture()
            return True
        except Exception:
            return False

    def _readiness_reason(self) -> str:
        if self._closed or self._sealed:
            return "closed"
        if self._failed or self._cleanup_failed:
            return "cleanup_failed"
        try:
            if getattr(self.runtime, "native_audio", None) is not self:
                return "unavailable"
            if self.provider is not self._original_provider:
                return "unavailable"
            close = getattr(self.provider, "close", None)
            original_close = self._original_provider_close
            if getattr(close, "__self__", None) is not getattr(
                original_close, "__self__", None
            ) or getattr(close, "__func__", close) is not getattr(
                original_close, "__func__", original_close
            ):
                return "unavailable"
            if not host_model_selected(self.runtime) or not self.runtime.enabled:
                return "unavailable"
            info = runtime_provider_info(self.runtime)
            if type(info.local) is not bool or info.local is not True:
                return "unavailable"
            if not self._transport_available():
                return "missing_transport"
            self._capture()
        except Exception:
            return "unavailable"
        return "ready"

    def status(self):
        active = sum(
            clip.task is not None and not clip.task.done() for clip in self._clips.values()
        )
        unresolved = any(
            clip.state in {"reserved", "asr", "append_pending"} and clip.failure not in {400, 409}
            for clip in self._clips.values()
        )
        pending = (
            active > 0
            or self._failed
            or self._cleanup_failed
            or unresolved
            or (self._close_task is not None and not self._close_task.done())
        )
        transport_available = self._transport_available()
        readiness_reason = self._readiness_reason()
        return {
            "configured": not self._closed,
            "available": self.available,
            "transport_available": transport_available,
            "readiness_reason": readiness_reason,
            "mode": "buffered_native_audio",
            "input_sample_rate": INPUT_RATE,
            "max_seconds": MAX_SECONDS,
            "pending": bool(pending),
            "active": active,
            "cleanup_failed": self._cleanup_failed,
        }

    def _window(self, clip):
        return SpeechWindow(
            session_id=clip.session_id,
            thread_id=clip.thread_id,
            epoch=clip.epoch,
            utterance_id=clip.utterance_id,
            window_index=0,
            revision=1,
            text=clip.text,
            start_ms=0,
            end_ms=(len(clip.pcm) // 2 * 1000 + INPUT_RATE - 1) // INPUT_RATE,
            user_speaking=False,
            capture_ended=True,
            audio_sha256=clip.audio_sha256,
            content_scope=clip.content_scope,
        )

    def _source_matches(self, clip, source):
        try:
            return (
                clip.text is not None
                and source.kind == "conversation.window"
                and source.origin == "authenticated-user"
                and type(source.event_id) is str
                and source.sequence > 0
                and (source.session_id, source.thread_id, source.epoch) == clip.key[:3]
                and (clip.source_id is None or source.event_id == clip.source_id)
                and SpeechWindow.model_validate(source.payload).model_dump(mode="json")
                == self._window(clip).model_dump(mode="json")
            )
        except (ValueError, TypeError, AttributeError):
            return False

    async def _current(self, clip):
        try:
            if not self._local(clip):
                return False
            self._verify_row(clip)
            session = await self.runtime.journal.session(clip.session_id)
            if (
                not self._local(clip)
                or session.channel != "converse"
                or (session.thread_id, session.epoch) != (clip.thread_id, clip.epoch)
            ):
                return False
            latest = await self.runtime.journal.latest_window(
                clip.session_id, clip.epoch, clip.utterance_id, 0
            )
            if not self._local(clip):
                return False
            if latest is None:
                return clip.source_id is None and clip.state in {
                    "reserved",
                    "asr",
                    "append_pending",
                }
            if not self._source_matches(clip, latest):
                return False
            resolved = await self.runtime.journal.resolve(latest.event_id)
            return (
                self._local(clip)
                and self._source_matches(clip, resolved)
                and resolved.event_id == latest.event_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _published(self, clip):
        if not await self._current(clip):
            raise _error(409)
        source = await self.runtime.journal.latest_window(
            clip.session_id, clip.epoch, clip.utterance_id, 0
        )
        if not self._local(clip) or source is None or not self._source_matches(clip, source):
            raise _error(503)
        if clip.state != "published":
            self._state(clip, "published", source_id=source.event_id)
        return source

    async def _admit_original(self, clip, controller):
        await clip.assigned.wait()
        try:
            with runtime_model_preparation(self.runtime):
                if not await self._current(clip):
                    raise _error(409)
                self._state(clip, "asr")
                audio = NativeAudioInput(pcm16=clip.pcm, rate=INPUT_RATE)

                async def current():
                    return await self._current(clip)

                text = await self.provider.transcribe(audio, current=current)
                if (
                    not await self._current(clip)
                    or type(audio.pcm16) is not bytes
                    or audio.pcm16 != clip.pcm
                    or audio.rate != INPUT_RATE
                ):
                    raise _error(409)
                if type(text) is not str:
                    raise _error(503)
                text = text.strip()
                if not text or len(text) > 16000 or len(text.encode("utf-8")) > 64000:
                    raise _error(503)
                clip.text = text
                self._state(clip, "append_pending")
                # Mapping by scope/utterance already exists before native append.
                # A lost ACK can only reconcile this exact source; never re-ASR.
                source = await controller.ingest(self._window(clip))
                if not self._source_matches(clip, source) or not await self._current(clip):
                    raise _error(409)
                self._state(clip, "published", source_id=source.event_id)
                return source
        except ProviderCleanupError:
            self._cleanup_failed = self._failed = True
            clip.failure = 503
            raise _error(503) from None
        except ProviderRefusal:
            # The protocol adapter distinguishes a completed refusal from
            # unconfirmed cleanup. Preserve this request's no-replay tombstone
            # without disabling unrelated, newly admitted clips.
            self._state(clip, "refused")
            clip.failure = 503
            raise _error(503) from None
        except asyncio.CancelledError:
            clip.fenced = True
            clip.failure = 409
            raise
        except SpeechProviderError as exc:
            clip.failure = exc.status_code
            raise
        except Exception:
            clip.failure = 503
            # append_pending remains reconcilable by the same original lookup.
            if clip.state == "asr":
                self._failed = True
            elif clip.state == "reserved" and not self._failed:
                # Known pre-transport setup refusal is not an uncertain ASR.
                self._state(clip, "refused")
            raise _error(503) from None

    async def admit(self, request: NativeClipRequest, controller):
        try:
            if (
                type(request) is not NativeClipRequest
                or request.__dict__.keys() != NativeClipRequest.model_fields.keys()
            ):
                raise ValueError("Invalid native clip")
            fresh = NativeClipRequest.model_validate(dict(request.__dict__), strict=True)
            wav = _wav(fresh.audio_base64)
            digest = _digest(_canonical(fresh.model_dump(mode="json")))
            capture = self._capture()
        except SpeechProviderError:
            raise
        except Exception:
            raise _error(400) from None
        clip = self._clips.get(fresh.request_id)
        replay = clip is not None
        if clip is not None:
            if clip.request_sha256 != digest:
                raise _error(409)
            self._verify_row(clip)
        else:
            if self._row(fresh.request_id) is not None:
                # Durable original, lost raw cache: explicit refusal after restart.
                raise _error(409)
            key = fresh.session_id, fresh.thread_id, fresh.epoch, fresh.utterance_id
            if key in self._utterances:
                raise _error(409)
            needed = len(wav) + len(wav) - 44 + REPLY_RESERVATION
            if len(self._clips) >= MAX_CLIPS or self._bytes + needed > MAX_RETAINED_BYTES:
                raise _error(503)
            clip = _Clip(
                fresh.request_id,
                digest,
                fresh.session_id,
                fresh.thread_id,
                fresh.epoch,
                fresh.utterance_id,
                fresh.content_scope,
                wav,
                wav[44:],
                _digest(wav),
                capture,
            )

            def reserve():
                if (
                    self.db.execute(
                        "SELECT 1 FROM native_audio_intents WHERE session_id=? AND thread_id=? AND epoch=? AND utterance_id=?",
                        (clip.session_id, clip.thread_id, str(clip.epoch), clip.utterance_id),
                    ).fetchone()
                    is not None
                ):
                    return False
                if (
                    self.db.execute("SELECT count(*) FROM native_audio_intents").fetchone()[0]
                    >= MAX_INTENTS
                ):
                    return None
                self.db.execute(
                    "INSERT INTO native_audio_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        clip.request_id,
                        digest,
                        clip.session_id,
                        clip.thread_id,
                        str(clip.epoch),
                        clip.utterance_id,
                        clip.content_scope,
                        clip.audio_sha256,
                        "reserved",
                        None,
                        None,
                    ),
                )
                return True

            reserved = self._transaction(reserve)
            if reserved is not True:
                raise _error(409 if reserved is False else 503)
            self._clips[clip.request_id] = self._utterances[clip.key] = clip
            self._bytes += clip.reserved_bytes
            coro = self._admit_original(clip, controller)
            clip.coroutine = coro
            try:
                task = asyncio.create_task(coro)
            except BaseException:
                self._failed = self._cleanup_failed = True
                clip.failure = 503
                # Setup may already have dispatched an original. Its assigned
                # gate remains closed and no replacement is constructed.
                raise _error(503) from None
            clip.task = task
            if (
                not isinstance(task, asyncio.Task)
                or task.get_loop() is not asyncio.get_running_loop()
                or task.get_coro() is not coro
            ):
                self._failed = self._cleanup_failed = True
                raise _error(503)
            task.add_done_callback(
                lambda original: original.exception() if not original.cancelled() else None
            )
            clip.assigned.set()
        # Do not retain a second base64 graph in the original operation.
        request = fresh = wav = None
        if clip.task is None:
            raise _error(503)
        try:
            await asyncio.shield(clip.task)
        except asyncio.CancelledError:
            raise  # Observer loss never cancels or recreates the original.
        except Exception:
            if clip.state not in {"append_pending", "published"}:
                raise _error(clip.failure or 503) from None
        with runtime_model_preparation(self.runtime):
            source = await self._published(clip)
        return {"entry": source.model_dump(mode="json"), "replay": replay}

    def _clip(self, source):
        try:
            payload = source.payload
            key = source.session_id, source.thread_id, source.epoch, payload.get("utterance_id")
            clip = self._utterances.get(key)
            if clip is None or not self._local(clip) or not self._source_matches(clip, source):
                raise _error(409)
            self._verify_row(clip)
            return clip
        except SpeechProviderError:
            raise
        except Exception:
            raise _error(409) from None

    def clip_for(self, source):
        clip = self._clip(source)
        return NativeAudioInput(pcm16=clip.pcm, rate=INPUT_RATE)

    def _event_binding(self, event):
        payload = event.payload
        if (
            event.kind != "conversation.converse"
            or event.origin != "converse"
            or type(payload) is not dict
            or type(payload.get("native_audio")) is not bool
            or type(payload.get("text")) is not str
            or type(payload.get("generation_epoch")) is not int
            or len(payload["text"]) > 16000
            or len(payload["text"].encode("utf-8")) > 64000
            or payload["generation_epoch"] < 1
            or payload.get("runtime_id") != self._runtime_id
            or type(payload.get("window_source_id")) is not str
            or type(payload.get("chunk_id")) is not str
            or payload.get("content_scope", "local_only") not in _SCOPES
        ):
            raise _error(409)
        return (
            event.event_id,
            event.session_id,
            event.thread_id,
            event.epoch,
            payload["generation_epoch"],
            payload["runtime_id"],
            payload["window_source_id"],
            payload["chunk_id"],
            payload["text"],
            payload["native_audio"],
            payload.get("native_audio_sha256"),
            payload.get("content_scope", "local_only"),
        )

    def cache_reply(self, prepared, event, reply, *, text_unchanged: bool):
        clip = self._clip(prepared.source)
        try:
            if (
                type(reply) is not NativeAudioReply
                or type(reply.text) is not str
                or type(reply.pcm16) is not bytes
                or type(reply.rate) is not int
            ):
                raise _error(503)
            if (
                not reply.text
                or len(reply.text) > 16000
                or len(reply.text.encode("utf-8")) > 64000
                or not 0 < len(reply.pcm16) <= REPLY_RESERVATION
                or len(reply.pcm16) % 2
                or not 8000 <= reply.rate <= 96000
                or len(reply.pcm16) // 2 > 60 * reply.rate
            ):
                raise _error(503)
            binding = self._event_binding(event)
            if (
                (event.session_id, event.thread_id, event.epoch) != clip.key[:3]
                or event.payload["window_source_id"] != prepared.source.event_id
                or prepared.generation != event.payload["generation_epoch"]
                or getattr(prepared, "native_audio_reply", None) is not reply
                or type(text_unchanged) is not bool
            ):
                raise _error(409)
            keep = (
                text_unchanged
                and event.payload["native_audio"] is True
                and event.payload["text"] == reply.text
            )
            if text_unchanged and not keep:
                raise _error(409)
            if keep and event.payload.get("native_audio_sha256") != _digest(reply.pcm16):
                raise _error(409)
            if not text_unchanged and event.payload["native_audio"] is not False:
                raise _error(409)
            if event.payload.get("native_audio_sha256") is not None and event.payload[
                "native_audio_sha256"
            ] != _digest(reply.pcm16):
                raise _error(409)
            record = _Reply(clip, binding, reply.pcm16 if keep else None, reply.rate)
            old = self._replies.get(event.event_id)
            if old is not None:
                if old != record:
                    raise _error(409)
                return
            if any(stored.clip is clip for stored in self._replies.values()):
                raise _error(409)  # One atomic native reply per admitted clip.
            self._replies[event.event_id] = record
        except SpeechProviderError:
            raise
        except Exception:
            raise _error(503) from None

    def speech_source(self, event, generation):
        try:
            record = self._replies.get(event.event_id)
            binding = self._event_binding(event)
            if (
                record is None
                or record.pcm is None
                or record.binding != binding
                or type(generation) is not int
                or generation != binding[4]
            ):
                raise _error(409)

            def current():
                try:
                    if (
                        not self._local(record.clip)
                        or self._replies.get(event.event_id) is not record
                        or self._event_binding(event) != record.binding
                    ):
                        return False
                    epoch = self.runtime.playback.get(event.session_id)
                    if (
                        epoch is None
                        or epoch.scope != (event.epoch, event.thread_id)
                        or not epoch.accepts(generation)
                    ):
                        return False
                    published = epoch.chunks.get(event.event_id)
                    return (
                        published is not None and self._event_binding(published) == record.binding
                    )
                except Exception:
                    return False

            if not current():
                raise _error(409)

            async def synthesize(spoken):
                # speech_frames normalizes this argument. The original native
                # event text above remains the binding; no second TTS is invoked.
                if type(spoken) is not str:
                    raise _error(409)
                for start in range(0, len(record.pcm), 32768):
                    if not current() or not await self._current(record.clip) or not current():
                        raise _error(409)
                    yield {
                        "type": "audio",
                        "audio": base64.b64encode(record.pcm[start : start + 32768]).decode(
                            "ascii"
                        ),
                        "sample_rate": record.rate,
                        "chunk_index": start // 32768,
                    }

            return synthesize, current
        except SpeechProviderError:
            raise
        except Exception:
            raise _error(503) from None

    async def _close_original(self):
        try:
            for clip in self._clips.values():
                clip.fenced = True
                if clip.task is not None and not clip.task.done():
                    clip.task.cancel()
            if self._provider_close_task is None:
                self._provider_close_task = asyncio.create_task(self._original_provider_close())
            tasks = [clip.task for clip in self._clips.values() if clip.task is not None]
            results = await asyncio.gather(
                *tasks, self._provider_close_task, return_exceptions=True
            )
            if (
                self._failed
                or self._cleanup_failed
                or self._provider_close_task.cancelled()
                or isinstance(results[-1], BaseException)
                or results[-1] is False
            ):
                raise _error(503)
            self.db.close()
            self._db_closed = True
            if self._owner_fd is not None:
                os.close(self._owner_fd)
                self._owner_fd = None
            self._closed = True
            self._clips.clear()
            self._utterances.clear()
            self._replies.clear()
            self._bytes = 0
        except BaseException:
            self._cleanup_failed = True
            raise _error(503) from None

    async def close(self):
        self._sealed = True
        if self._close_task is None:
            try:
                self._close_task = asyncio.create_task(self._close_original())
            except BaseException:
                self._cleanup_failed = True
                raise _error(503) from None
        await asyncio.shield(self._close_task)
