"""First-Session media oracles, with a separate source-informed fixture correction.

Actual temporary SQLite/spool, native authenticated submit admission and ASGI
HTTP/WebSocket are exercised by the eventual root run. Only synthetic bytes and
credentials are used. No provider/model/decoder/task execution is qualified.
Original source-blind cut: 4312fa6. Corrections follow C's sealed F1-F4 review,
root's F5 selection and exposed native lifetime seams; they are not source-blind.
"""

import asyncio
import base64
import copy
import hashlib
import json
import re
import sqlite3
import stat
import struct
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from packages.contracts import media
from packages.contracts.models import AppendRequest, TaskRequest
from services.common.auth import Auth
from services.common.media_wire import MediaWireError
from services.session.agent_control import AgentControlUnavailable
from services.session.api import create_app
from services.session.media_clients import MediaGrantKeyStore
from services.session.media_owner import MediaSessionOwner
from services.session.media_spool import MediaSpool
from services.session.store import JournalError, Store

_WAIT = 10
_ORIGIN = "http://synthetic-owner.test"
_CAPABILITY = "X-Kinaiya-Media-Client-Capability"
_NONCE_HEADER = "X-Kinaiya-Media-Bootstrap-Nonce"
_NONCE = bytes(range(32)).hex()
_RETAINED_FIXTURES = []
_TOKENS = {
    role: char * 32
    for role, char in zip(
        ("owner", "agent", "converse", "decisions", "resources"), "oacdr", strict=True
    )
}
_OBJECTIVE = "Analyze the supplied public synthetic recording as evidence for this task."


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _digest(kind, value):
    return _sha(b"kinaiya.media.v1/" + kind.encode("ascii") + b"\0" + _json(value))


def _headers(role="owner", *, grant=None, nonce=None):
    result = {
        "Authorization": "Bearer " + _TOKENS[role],
        "Content-Type": "application/json",
        "Origin": _ORIGIN,
    }
    if grant is not None:
        result[_CAPABILITY] = grant
    if nonce is not None:
        result[_NONCE_HEADER] = nonce
    return result


def _limits(**changes):
    # Native-selected finite policy; no test changes the universal wire caps.
    raw = {
        "v": 1,
        "source": {
            "file_bytes": 8589934592,
            "file_duration_ms": 21600000,
            "upload_chunk_bytes": 4194304,
            "owner_spool_bytes": 17179869184,
            "global_spool_bytes": 34359738368,
            "unsealed_uploads_per_owner": 2,
            "operations_global": 2,
            "live_spool_bytes": 536870912,
            "live_buffer_target_ms": 120000,
            "sealed_unstarted_ms": 1800000,
        },
        "decode": {
            "segment_ms": 60000,
            "cpu_ms": 120000,
            "wall_ms": 300000,
            "address_space_bytes": 805306368,
            "cpu_affinity_count": 1,
            "children_per_operation": 1,
            "children_global": 2,
            "track_limit": 8,
            "frames_per_track_segment": 3600,
            "pixels_per_frame": 16777216,
            "max_edge": 4096,
            "frame_rate": {"num": 60, "den": 1},
            "probe_metadata_bytes": 1048576,
            "stderr_bytes": 65536,
        },
        "wire": {
            "control_bytes": 65536,
            "body_bytes": 16777216,
            "audio_packet_ms": 1000,
            "image_bytes": 8388608,
            "clip_bytes": 16777216,
            "clip_ms": 10000,
            "consumer_packets": 8,
            "consumer_bytes": 16777216,
            "total_pending_bytes": 67108864,
            "live_backlog_ms": 2000,
        },
    }
    raw["source"].update(changes)
    return media.media_json(_json(raw), media.MediaLimits)


def _profile(limits):
    # Registered metadata only: absent executable/runtime/schema pins are honest.
    raw = {
        "v": 1,
        "id": "a" * 32,
        "generation": 1,
        "driver": "pyav17_timed_fd_v1",
        "implementation": None,
        "executables": [],
        "runtime": None,
        "formats": [
            {
                "kind": "audio",
                "container": "wav",
                "codec": "pcm_s16le",
                "rate": 16000,
                "channels": 1,
            }
        ],
        "transform": {
            "v": 1,
            "kind": "identity",
            "audio_rate": None,
            "audio_channels": None,
            "video_rate": None,
            "width": None,
            "height": None,
            "owner_setting_ref": None,
        },
        "limits": json.loads(media.media_bytes(limits)),
        "protocol": {
            "kind": "decoder_fd_v1",
            "implementation": None,
            "request_schema_sha256": None,
            "response_schema_sha256": None,
            "wire": "u32be_json_then_body",
            "peer": "owned_pid_incarnation_nonce",
            "pts": "original_rational",
            "cleanup": "pidfd_reap",
            "segments": "retained_child",
        },
    }
    raw["profile_sha256"] = _digest("DecoderProfile", raw)
    return media.media_json(_json(raw), media.MediaDecoderProfile)


def _typed_receipt(response, request_kind, body_kind, request):
    # Request and result names differ on admission/control routes.
    assert response.status_code == 200, (response.status_code, response.text)
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert set(value) == {"v", "id", "key", "request_sha256", "body", "sha256"}
    assert value["v"] == 1 and type(value["v"]) is int
    assert re.fullmatch("[a-f0-9]{32}", value["id"])
    assert value["key"] == request["key"]
    assert value["request_sha256"] == _digest(request_kind, request)
    unhashed = {key: item for key, item in value.items() if key != "sha256"}
    assert value["sha256"] == _digest("R<" + body_kind + ">", unhashed)
    assert response.content == _json(value)
    return value


def _error(response, status, code):
    assert response.status_code == status, (response.status_code, response.text)
    assert response.json() == {"v": 1, "error": code}
    assert response.content == _json(response.json())
    assert response.headers["cache-control"] == "no-store"


def _no_secrets(value, *secrets):
    raw = _json(value)
    for secret in (*_TOKENS.values(), *secrets):
        assert secret.encode("ascii") not in raw


class _Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def wall_ms(self):
        return 1762149600000 + round((self.value - 1000.0) * 1000)

    def advance(self, seconds):
        self.value += seconds


class _LostResponse(RuntimeError):
    pass


class _DropFirstAdmit:
    """Drop the actual original ASGI response after the app produced it fully."""

    def __init__(self, app):
        self.app = app
        self.original = None

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["path"] != "/v1/media/sources/admit"
            or self.original is not None
        ):
            return await self.app(scope, receive, send)
        captured = []

        async def capture(message):
            captured.append(copy.deepcopy(message))

        await self.app(scope, receive, capture)
        assert captured[0]["type"] == "http.response.start"
        assert captured[0]["status"] == 200
        assert captured[-1]["type"] == "http.response.body"
        assert captured[-1].get("more_body", False) is False
        self.original = captured
        raise _LostResponse("synthetic lost first response")


class _ASGIWebSocket:
    """Real app/receive/send ownership, without sockets or a fake connected flag."""

    def __init__(self, app):
        self.app = app
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.task = None

    async def connect(self, hello):
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "ws",
            "path": "/v1/events",
            "raw_path": b"/v1/events",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"origin", _ORIGIN.encode())],
            "client": ("127.0.0.1", 12345),
            "server": ("session.test", 80),
            "subprotocols": [],
            "state": {},
        }
        self.task = asyncio.create_task(self.app(scope, self.incoming.get, self.outgoing.put))
        await self.incoming.put({"type": "websocket.connect"})
        accepted = await asyncio.wait_for(self.outgoing.get(), _WAIT)
        assert accepted["type"] == "websocket.accept"
        await self.incoming.put({"type": "websocket.receive", "text": _json(hello).decode()})

    async def receive(self):
        return await asyncio.wait_for(self.outgoing.get(), _WAIT)

    async def receive_json(self):
        message = await self.receive()
        assert message["type"] == "websocket.send", message
        return json.loads(message.get("text") or message["bytes"])

    async def close(self):
        if self.task is None:
            return
        if not self.task.done():
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(asyncio.shield(self.task), _WAIT)
        finally:
            if not self.task.done():
                self.task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(self.task), _WAIT)
                except asyncio.CancelledError:
                    pass
        assert self.task.done(), "Original ASGI owner channel did not retire"


@dataclass
class _Env:
    client: httpx.AsyncClient
    app: object
    store: Store
    spool: MediaSpool
    auth: Auth
    profile: media.MediaDecoderProfile
    clock: _Clock
    session: dict
    backend: str
    anchor: dict
    proof: dict
    owner_event: dict
    submit: dict
    uploads: dict
    reserve_calls: list
    dropper: _DropFirstAdmit | None
    key_path: Path
    owner: MediaSessionOwner
    cleanup_observations: list

    def ref(self, receipt):
        return {
            "backend_id": self.backend,
            "receipt_id": receipt["id"],
            "sha256": receipt["sha256"],
        }

    async def post(self, path, value, *, role="owner", grant=None, nonce=None):
        return await self.client.post(
            path, content=_json(value), headers=_headers(role, grant=grant, nonce=nonce)
        )

    def admit_request(self, *, key="source-a", size=6):
        return {
            "v": 1,
            "key": key,
            "session": self.session,
            "kind": "recording",
            "purpose": "analyze",
            "anchor": self.ref(self.anchor),
            "replaces_source": None,
            "declared_bytes": size,
            "declared_sha256": None,
            "decoder_profile_sha256": self.profile.profile_sha256,
            "scope": "local_only",
            "raw_retain_until_ms": None,
        }

    async def admit(self, *, key="source-a", size=6, nonce=_NONCE):
        request = self.admit_request(key=key, size=size)
        response = await self.post("/v1/media/sources/admit", request, nonce=nonce)
        receipt = _typed_receipt(response, "Admit", "SourceAdmission", request)
        grant = response.headers[_CAPABILITY]
        assert re.fullmatch("[a-f0-9]{64}", grant)
        _no_secrets(receipt, grant, nonce)
        return receipt, grant

    @asynccontextmanager
    async def connected(self, source, grant):
        channel = _ASGIWebSocket(self.app)
        try:
            await channel.connect(
                {
                    "token": _TOKENS["owner"],
                    "after": 0,
                    "session_id": self.session["session_id"],
                    "media": {"v": 1, "source": self.ref(source), "client_capability": grant},
                }
            )
            associated = None
            for _ in range(64):
                observed = await channel.receive_json()
                if observed.get("kind") == "media.client_associated":
                    associated = observed
                    break
                # Existing journal frames may already be queued. They do not
                # establish the media association; its actual frame is required.
                assert observed["session_id"] == self.session["session_id"]
                assert observed["event_id"] and type(observed["sequence"]) is int
            assert associated is not None, "Original media association ACK was not observed"
            assert set(associated) == {
                "v",
                "kind",
                "source",
                "client_instance",
                "connection_instance",
                "fence",
            }
            assert associated["v"] == 1
            assert associated["kind"] == "media.client_associated"
            assert associated["source"] == self.ref(source)
            assert associated["client_instance"] == source["body"]["client_instance"]
            assert associated["fence"] == source["body"]["fence"]
            assert re.fullmatch("[a-f0-9]{32}", associated["connection_instance"])
            _no_secrets(associated, grant)
            yield channel
        finally:
            primary = sys.exception()
            try:
                await channel.close()
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(
                    "Additional original WebSocket retirement error: " + type(error).__name__
                )

    def put_header(self, source, *, key="chunk-a", index=0, offset=0, body=b"abcdef"):
        return {
            "v": 1,
            "key": key,
            "source": self.ref(source),
            "fence": source["body"]["fence"],
            "index": index,
            "offset": offset,
            "body_bytes": len(body),
            "body_sha256": _sha(body),
        }

    async def put(self, header, body, grant, *, cuts=None, trailing=b"", role="owner"):
        raw_header = _json(header)
        frame = struct.pack(">I", len(raw_header)) + raw_header + body + trailing

        async def fragmented():
            start = 0
            for end in cuts or ():
                yield frame[start:end]
                start = end
            yield frame[start:]

        headers = _headers(role, grant=grant)
        headers["Content-Type"] = "application/vnd.kinaiya.media-frame"
        return await self.client.post(
            "/v1/media/uploads/put", content=fragmented(), headers=headers
        )

    def control_request(self, source, command, *, key="control-a", fence=None):
        return {
            "v": 1,
            "key": key,
            "source": self.ref(source),
            "fence": fence if fence is not None else source["body"]["fence"],
            "command": command,
            "position": None,
        }

    async def lookup(self, kind, receipt):
        return await self.post("/v1/media/lookup", {"v": 1, "kind": kind, "ref": self.ref(receipt)})

    async def current_status(self, source):
        response = await self.client.get(
            f"/v1/media/sources/{source['body']['source_id']}/status",
            params={"source_receipt_id": source["id"]},
            headers=_headers(),
        )
        assert response.status_code == 200, (response.status_code, response.text)
        assert response.headers["cache-control"] == "no-store"
        value = response.json()
        assert set(value) == {"v", "id", "key", "request_sha256", "body", "sha256"}
        assert value["sha256"] == _digest(
            "R<Status>", {key: item for key, item in value.items() if key != "sha256"}
        )
        assert value["body"]["source"] == self.ref(source)
        assert response.content == _json(value)
        _no_secrets(value, _NONCE)
        return value

    async def seal(
        self, source, grant, body=b"abcdef", *, key="seal-a", digest=None, role="converse"
    ):
        request = {
            "v": 1,
            "key": key,
            "source": self.ref(source),
            "fence": source["body"]["fence"],
            "final_bytes": len(body),
            "source_sha256": _sha(body) if digest is None else digest,
        }
        return await self.post("/v1/media/sources/seal", request, role=role, grant=grant), request


def _submit_proposal(
    session,
    event,
    *,
    task_id="00000000-0000-4000-8000-000000000204",
    key="one-real-owner-objective",
):
    # Original native submit wire grammar, not insertion of a fabricated DB row.
    logical = {
        **session,
        "objective": _OBJECTIVE,
        "source_event_ids": [event["event_id"]],
        "source_revision": 1,
        "content_scope": "local_only",
    }
    return AppendRequest(
        **session,
        kind="agent.command",
        payload={
            **logical,
            "op": "submit",
            "actor": "owner",
            "task_id": task_id,
            "state": "accepted",
            "objective_revision": 1,
            "requested_content_scope": "local_only",
            "idempotency_key": key,
            "payload_digest": _sha(_json(logical)),
            "harness_version": "seed-v1",
            "model_pin": {
                "schema_version": "1.0",
                "profile_id": "synthetic-admission-only",
                "provider": "synthetic",
                "model": "no-model-is-executed",
                "local": True,
                "reasoning": None,
                "settings_revision": 0,
            },
            "explicit_skill": None,
            "plugin_versions": {},
        },
        causes=[event["event_id"]],
        task_refs=[task_id],
        idempotency_key=f"agent:{task_id}:agent.command:1",
    )


@asynccontextmanager
async def _stack(tmp_path, *, limits=None, lose_first_ack=False, allow_unknown_shutdown=False):
    # The checkout's ancestor permissions cannot stand in for a private native
    # key/DB root. Retain the actual temporary directory on unjoined custody.
    private = tempfile.TemporaryDirectory(prefix="kinaiya-test-native-media-", dir="/tmp")
    custody = {"dispose": False}
    try:
        async with _native_stack(
            Path(private.name),
            limits=limits,
            lose_first_ack=lose_first_ack,
            allow_unknown_shutdown=allow_unknown_shutdown,
            custody=custody,
        ) as env:
            yield env
    finally:
        if custody["dispose"]:
            primary = sys.exception()
            try:
                private.cleanup()
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(
                    "Additional private-directory teardown error: " + type(error).__name__
                )
        else:
            # Do not let TemporaryDirectory's GC/atexit cleanup unlink backing
            # files under a retained original. This is a failed teardown.
            private._finalizer.detach()
            _RETAINED_FIXTURES.append((private, custody))


def _shutdown_exception_snapshot(error):
    if error is None:
        return None
    name = type(error).__name__
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        return {"type": "non_ascii_type_name", "truncated": True}
    return {"type": name[:64], "truncated": len(name) > 64}


def _shutdown_task_snapshot(task, loop):
    if task is None:
        return {"state": "missing"}
    if type(task) is not asyncio.Task:
        return {"state": "not_native_task"}
    if task.get_loop() is not loop:
        return {"state": "foreign_loop"}
    value = {"done": task.done(), "cancelled": task.cancelled()}
    if not value["done"]:
        value["state"] = "pending"
    elif value["cancelled"]:
        value["state"] = "cancelled"
    else:
        error = task.exception()
        if error is not None:
            value.update(state="exception", exception=_shutdown_exception_snapshot(error))
        else:
            result = task.result()
            value.update(
                state="returned",
                result_is_bool=type(result) is bool,
                result_bool=result if type(result) is bool else None,
            )
    return value


def _shutdown_snapshot(owner, app):
    # Diagnostic caps only: no authority, cleanup, SQL/OS, clock or retry calls.
    limits = {"bytes": 8192, "list_items": 8, "depth": 8, "string_chars": 64}
    loop = asyncio.get_running_loop()

    def sample(values, describe):
        return [
            describe(item) for _, item in zip(range(limits["list_items"]), values, strict=False)
        ]

    def original_snapshot(original):
        return {
            "unknown": original.unknown is True,
            "fenced": original.fenced.is_set(),
            "task": _shutdown_task_snapshot(original.task, loop),
        }

    def cleanup_snapshot(cleanup):
        if cleanup is None:
            return {"present": False}
        return {
            "present": True,
            "unknown": cleanup.unknown is True,
            "task": _shutdown_task_snapshot(cleanup.task, loop),
            "entered_is_task": type(cleanup.task) is asyncio.Task
            and cleanup.task.get_loop() is loop
            and cleanup.entered_task is cleanup.task,
            "completed_is_task": type(cleanup.task) is asyncio.Task
            and cleanup.task.get_loop() is loop
            and cleanup.completed_task is cleanup.task,
        }

    def source_snapshot(source):
        connection = source.connection
        return {
            "fenced": source.fenced.is_set(),
            "connection_present": connection is not None,
            "connection_joined": connection is not None and connection.joined is True,
            "connection_unknown": connection is not None and connection.unknown is True,
            "delete_task": _shutdown_task_snapshot(source.delete_task, loop),
            "expiry_task": _shutdown_task_snapshot(source.expiry_task, loop),
            "probe": cleanup_snapshot(source.cleanup_probe),
            "continuation": cleanup_snapshot(source.cleanup_continuation),
            "upload_deleted": source.upload.deleted is True,
            "reader_count": len(source.upload._readers),
        }

    operations = app.state.media_operations
    with owner._mutex:
        close = owner._close_operation
        value = {
            "limits": limits,
            "diagnostic_only": True,
            "truncated": False,
            "shutdown_unconfirmed": app.state.media_owner_shutdown_unconfirmed is True,
            "http_close_confirmed": operations._close_confirmed is True,
            "http_original_count": len(operations._operations),
            "owner_closing": owner._closing is True,
            "owner_closed": owner._closed is True,
            "owner_poisoned": owner._poisoned is True,
            "intake_count": len(owner._intakes),
            "native_job_count": len(owner._native_jobs),
            "native_jobs": sample(
                owner._native_jobs.values(), lambda task: _shutdown_task_snapshot(task, loop)
            ),
            "omitted_native_jobs": max(0, len(owner._native_jobs) - limits["list_items"]),
            "original_count": len(owner._operations),
            "originals": sample(owner._operations.values(), original_snapshot),
            "omitted_originals": max(0, len(owner._operations) - limits["list_items"]),
            "source_count": len(owner._sources),
            "sources": sample(owner._sources.values(), source_snapshot),
            "omitted_sources": max(0, len(owner._sources) - limits["list_items"]),
            "close_present": close is not None,
        }
        if close is not None:
            value["close"] = {
                "unknown": close.unknown is True,
                "sealed": close.sealed is True,
                "setup_error": _shutdown_exception_snapshot(close.setup_error),
                "task": _shutdown_task_snapshot(close.task, loop),
                "phases": {
                    name: _shutdown_task_snapshot(close.phases.get(name), loop)
                    for name in ("hub", "spool", "key")
                },
                "retained_task_count": len(close.retained),
                "captured_original_count": len(close.originals),
                "captured_source_count": len(close.sources),
                "captured_sources": sample(close.sources.values(), source_snapshot),
                "omitted_captured_sources": max(0, len(close.sources) - limits["list_items"]),
            }
        value["truncated"] = any(
            value[name] for name in ("omitted_native_jobs", "omitted_originals", "omitted_sources")
        ) or (close is not None and value["close"]["omitted_captured_sources"] > 0)
    encoded = _json(value)
    if len(encoded) > limits["bytes"]:
        # Keep a valid bounded JSON object, explicitly omitting detail on overflow.
        close_value = value.get("close")
        value = {
            "limits": limits,
            "diagnostic_only": True,
            "truncated": True,
            "byte_bound_fallback": True,
            "http_close_confirmed": value["http_close_confirmed"],
            "owner_closed": value["owner_closed"],
            "owner_poisoned": value["owner_poisoned"],
            "close_present": value["close_present"],
            "native_job_count": value["native_job_count"],
            "original_count": value["original_count"],
            "source_count": value["source_count"],
            "omitted_detail": "all_task_source_and_phase_details",
        }
        if close_value is not None:
            value["close"] = {
                name: close_value[name]
                for name in ("unknown", "sealed", "setup_error", "task", "phases")
            }
            value["omitted_detail"] = "source_original_native_job_task_details"
        encoded = _json(value)
    return encoded.decode("utf-8")


async def _finish_stack(owner, app, lifespan, initial_tasks, uploads, allow_unknown, observations):
    errors = []
    confirmed = False
    try:
        await lifespan.__aexit__(None, None, None)
        confirmed = True
        observations.append("native owner shutdown positively confirmed")
    except BaseException as error:
        unknown = getattr(app.state, "media_owner_shutdown_unconfirmed", False) is True
        observations.append("native owner shutdown unknown")
        try:
            observations.append("native shutdown snapshot: " + _shutdown_snapshot(owner, app))
        except BaseException as diagnostic_error:
            observations.append(
                "native shutdown snapshot unavailable: "
                + _json(_shutdown_exception_snapshot(diagnostic_error)).decode("utf-8")
            )
        if not (allow_unknown and unknown and isinstance(error, RuntimeError)):
            errors.append(error)

    # A fixture-intent flag cannot retire a writer, reader or channel. Observe
    # the exact retained Tasks/native thread wrappers without cancelling them.
    with owner._mutex:
        jobs = tuple(owner._native_jobs.values())
    tasks = (set(asyncio.all_tasks()) - initial_tasks) | {job for job in jobs if job is not None}
    tasks.discard(asyncio.current_task())
    pending = {task for task in tasks if not task.done()}
    if pending:
        _, pending = await asyncio.wait(pending, timeout=_WAIT)
    # Joining those Tasks can run native callbacks. Re-observe the retained
    # registries before any synchronous backing-handle disposal.
    with owner._mutex:
        jobs = tuple(owner._native_jobs.values())
    tasks = (set(asyncio.all_tasks()) - initial_tasks) | {job for job in jobs if job is not None}
    tasks.discard(asyncio.current_task())
    pending |= {task for task in tasks if not task.done()}
    readers = any(upload._readers for upload in uploads.values())
    if pending or any(job is None for job in jobs) or readers:
        observations.append("backing custody retained: actual users not positively retired")
        errors.append(RuntimeError("Fixture backing custody still has unjoined native users"))
        return False, errors

    observations.append("fixture Tasks/thread wrappers joined; no registered source readers")
    if not confirmed:
        # There is no decoder/provider launch in this fixture. All actual
        # fixture users above have retired. This disposes their local handles;
        # it does not turn retained native metadata uncertainty into settlement.
        observations.append("disposable handle teardown only; native settlement remains unknown")
    try:
        if owner.spool.close() is not True:
            raise RuntimeError("Disposable spool handle retirement remained unknown")
        try:
            owner.store.close()
        except JournalError:
            if confirmed:
                raise
            try:
                owner.store.agent_control.close()
            except AgentControlUnavailable:
                pass
            sqlite3.Connection.close(owner.store.db)
        if owner.keys.close() is not True:
            raise RuntimeError("Disposable key handle retirement remained unknown")
    except BaseException as error:
        errors.append(error)
        return False, errors
    return True, errors


@asynccontextmanager
async def _native_stack(native_root, *, limits, lose_first_ack, allow_unknown_shutdown, custody):
    initial_tasks = set(asyncio.all_tasks())
    workspace = native_root / "workspace"
    workspace.mkdir()
    spool_root = native_root / "spool"
    spool_root.mkdir(mode=0o700)
    spool_root.chmod(0o700)
    store = Store(native_root / "session.sqlite")
    limits = limits if limits is not None else _limits()
    profile = _profile(limits)
    spool = MediaSpool(spool_root, limits)
    auth = Auth(dict(_TOKENS), allowed_origins=(_ORIGIN,))
    clock = _Clock()
    key_path = native_root / "media-grant.key"
    keys = MediaGrantKeyStore(key_path, workspace=workspace, spool_root=spool_root)
    owner = MediaSessionOwner(
        store,
        store.agent_control,
        spool,
        auth=auth,
        selected_limits=limits,
        selected_profile=profile,
        grant_key_store=keys,
        clock=clock,
        wall_clock_ms=clock.wall_ms,
    )
    # create_app binds its actual newly registered control to owner.bind_app.
    # No test pre-registers a control or supplies a native principal/connected flag.
    app = create_app(store, auth, media_owner=owner)
    dropper = _DropFirstAdmit(app) if lose_first_ack else None
    exposed_app = dropper if dropper is not None else app
    uploads = {}
    reserve_calls = []
    native_reserve = spool.reserve
    observations = []
    custody.update(owner=owner, store=store, spool=spool, keys=keys)

    def observe_reserve(source_id, reserved_bytes):
        reserve_calls.append((source_id, reserved_bytes))
        original = native_reserve(source_id, reserved_bytes)
        uploads[source_id] = original
        return original

    lifespan = app.router.lifespan_context(app)
    entered = False
    primary = None
    try:
        with patch.object(spool, "reserve", side_effect=observe_reserve):
            await lifespan.__aenter__()
            entered = True
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=exposed_app),
                base_url="http://session.test",
            ) as client:
                created = await client.post("/v1/sessions", json={}, headers=_headers())
                assert created.status_code == 200
                session = {
                    name: created.json()[name] for name in ("session_id", "thread_id", "epoch")
                }
                control = await client.get("/v1/agent-control/state", headers=_headers())
                assert control.status_code == 200
                backend = control.json()["backend_id"]
                owner_request = AppendRequest(
                    **session,
                    kind="conversation.user",
                    payload={"text": _OBJECTIVE, "final": True, "revision": 1},
                    idempotency_key="authenticated-owner-objective",
                )
                written = await client.post(
                    "/v1/transcript/entries",
                    json=owner_request.model_dump(mode="json"),
                    headers=_headers(),
                )
                assert written.status_code == 200
                event = written.json()
                original = _submit_proposal(session, event)
                submitted = await client.post(
                    "/v1/transcript/agent-submits",
                    json=original.model_dump(mode="json"),
                    headers=_headers("agent"),
                )
                assert submitted.status_code == 200
                submit = submitted.json()["entry"]
                proof_request = {
                    "v": 1,
                    "key": "task-proof",
                    "session": session,
                    "original_admission_id": submit["event_id"],
                }
                proof_response = await client.post(
                    "/v1/media/anchors/task-proof",
                    content=_json(proof_request),
                    headers=_headers("agent"),
                )
                proof = _typed_receipt(
                    proof_response, "TaskProofRequest", "TaskProof", proof_request
                )
                proof_ref = {
                    "backend_id": backend,
                    "receipt_id": proof["id"],
                    "sha256": proof["sha256"],
                }
                anchor_request = {
                    "v": 1,
                    "key": "one-analyze-anchor",
                    "owner_objective": {"kind": "owner_text", "event_ref": event["event_id"]},
                    "scope": "local_only",
                    "submit": None,
                    "task_admission": proof_ref,
                }
                anchor_response = await client.post(
                    "/v1/media/anchors/admit", content=_json(anchor_request), headers=_headers()
                )
                anchor = _typed_receipt(
                    anchor_response, "AnchorAdmission", "AnalyzeTaskAnchor", anchor_request
                )
                env = _Env(
                    client,
                    exposed_app,
                    store,
                    spool,
                    auth,
                    profile,
                    clock,
                    session,
                    backend,
                    anchor,
                    proof,
                    event,
                    submit,
                    uploads,
                    reserve_calls,
                    dropper,
                    key_path,
                    owner,
                    observations,
                )
                try:
                    yield env
                except BaseException as error:
                    primary = error
                    raise
    finally:
        active = sys.exception()
        primary = primary if primary is not None else active
        errors = []
        if entered:
            try:
                custody["dispose"], errors = await _finish_stack(
                    owner,
                    app,
                    lifespan,
                    initial_tasks,
                    uploads,
                    allow_unknown_shutdown,
                    observations,
                )
            except BaseException as error:
                errors.append(error)
        else:
            errors.append(RuntimeError("Fixture setup did not establish native shutdown ownership"))
        if primary is not None:
            for observation in observations:
                primary.add_note("Fixture teardown: " + observation)
            for error in errors:
                primary.add_note("Additional fixture teardown error: " + type(error).__name__)
            if active is not None and active is not primary:
                primary.add_note(
                    "Additional fixture transport exit error: " + type(active).__name__
                )
                raise primary
        elif errors:
            group = BaseExceptionGroup("Native media fixture teardown failed", errors)
            for observation in observations:
                group.add_note("Fixture teardown: " + observation)
            raise group


def _submits(env):
    return [
        entry
        for entry in env.store.read(kinds=["agent.command"])
        if entry.payload.get("op") == "submit"
    ]


async def test_actual_native_task_proof_one_anchor_and_historical_replay(tmp_path):
    async with _stack(tmp_path) as env:
        proof = env.proof["body"]
        assert proof["session"] == env.session
        assert proof["state"] == "accepted"
        assert proof["task_id"] == env.submit["payload"]["task_id"]
        assert proof["objective_revision"] == 1
        assert proof["owner_objective"] == {
            "kind": "owner_text",
            "event_ref": env.owner_event["event_id"],
        }
        with env.store.lock:
            original = env.store.db.execute(
                "SELECT request FROM entries WHERE event_id=?", (env.submit["event_id"],)
            ).fetchone()[0]
        assert proof["request_sha256"] == _sha(original.encode())
        request = {
            "v": 1,
            "key": "one-analyze-anchor",
            "owner_objective": proof["owner_objective"],
            "scope": "local_only",
            "submit": None,
            "task_admission": env.ref(env.proof),
        }
        same = await env.post("/v1/media/anchors/admit", request)
        assert same.content == _json(env.anchor)
        changed_key = await env.post(
            "/v1/media/anchors/admit", {**request, "key": "not-a-new-objective"}
        )
        _error(changed_key, 409, "conflict")
        looked_up = await env.lookup("anchor", env.anchor)
        assert looked_up.json() == {"v": 1, "found": True, "value": env.anchor}
        assert len(_submits(env)) == 1
        assert env.anchor["body"]["allowed_operation"] == "evidence_only"
        assert env.anchor["body"]["task_admission"] == env.ref(env.proof)


async def test_source_metadata_is_native_reserved_not_decoder_or_task_completion(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        body = source["body"]
        assert body["session"] == env.session
        assert body["source_revision"] == 1
        assert body["source_equivalence_id"] is None
        assert body["anchor"] == env.ref(env.anchor)
        assert body["purpose"] == "analyze" and body["kind"] == "recording"
        assert body["capture_instance"] is None
        assert body["decoder_profile_sha256"] == env.profile.profile_sha256
        assert body["lease_expires_at_ms"] == env.clock.wall_ms() + 30000
        assert body["fence"] == {
            "session": env.session,
            "source_revision": 1,
            "generation": 1,
            "lease_generation": 1,
        }
        assert body["reservation"]["owner_spool_bytes"] == 6
        assert body["reservation"]["global_spool_bytes"] == 6
        assert env.spool.reserved_bytes == 6
        assert env.reserve_calls == [(body["source_id"], 6)]
        assert stat.S_IMODE(env.key_path.stat().st_mode) == 0o600
        assert env.key_path.stat().st_nlink == 1
        async with env.connected(source, grant):
            request = env.control_request(source, "start")
            _error(await env.post("/v1/media/control", request, grant=grant), 503, "unavailable")
        assert len(_submits(env)) == 1
        assert env.store.read(kinds=["agent.outcome"]) == []


@pytest.mark.parametrize("role", ["agent", "converse", "decisions", "resources"])
async def test_service_role_alone_cannot_mint_owner_source_or_client(tmp_path, role):
    async with _stack(tmp_path) as env:
        response = await env.post(
            "/v1/media/sources/admit", env.admit_request(), role=role, nonce=_NONCE
        )
        assert response.status_code == 403
        assert _CAPABILITY.lower() not in response.headers
        assert env.reserve_calls == [] and env.spool.reserved_bytes == 0


@pytest.mark.parametrize("nonce", ["", "0" * 63, "0" * 65, "A" * 64, "g" * 64])
async def test_private_nonce_shape_is_rejected_before_reservation(tmp_path, nonce):
    async with _stack(tmp_path) as env:
        response = await env.post("/v1/media/sources/admit", env.admit_request(), nonce=nonce)
        _error(response, 400, "bad_packet")
        assert env.reserve_calls == [] and env.spool.reserved_bytes == 0


async def test_duplicate_private_nonce_header_and_public_nonce_field_refused(tmp_path):
    async with _stack(tmp_path) as env:
        headers = list(_headers(nonce=_NONCE).items()) + [(_NONCE_HEADER, _NONCE)]
        duplicate = await env.client.post(
            "/v1/media/sources/admit", content=_json(env.admit_request()), headers=headers
        )
        _error(duplicate, 400, "bad_packet")
        extra = await env.post(
            "/v1/media/sources/admit",
            {**env.admit_request(), "client_instance": "a" * 32},
            nonce=_NONCE,
        )
        _error(extra, 400, "bad_packet")
        assert env.reserve_calls == []


async def test_lost_first_response_recovers_same_original_grant_without_lease_reset(tmp_path):
    async with _stack(tmp_path, lose_first_ack=True) as env:
        request = env.admit_request()
        with pytest.raises(_LostResponse):
            await env.post("/v1/media/sources/admit", request, nonce=_NONCE)
        assert env.dropper.original is not None
        original_headers = dict(env.dropper.original[0]["headers"])
        original_grant = original_headers[_CAPABILITY.lower().encode()].decode()
        original_body = b"".join(
            part.get("body", b"")
            for part in env.dropper.original
            if part["type"] == "http.response.body"
        )
        assert len(env.reserve_calls) == 1 and env.spool.reserved_bytes == 6
        env.clock.advance(7)
        replay = await env.post("/v1/media/sources/admit", request, nonce=_NONCE)
        source = _typed_receipt(replay, "Admit", "SourceAdmission", request)
        assert replay.content == original_body
        assert replay.headers[_CAPABILITY] == original_grant
        assert source["body"]["lease_expires_at_ms"] == env.clock.wall_ms() + 23000
        assert len(env.reserve_calls) == 1 and env.spool.reserved_bytes == 6
        _no_secrets(source, original_grant, _NONCE)
        async with env.connected(source, original_grant):
            put = env.put_header(source)
            response = await env.put(put, b"abcdef", original_grant)
            _typed_receipt(response, "UploadPut", "UploadAck", put)


async def test_changed_nonce_or_request_never_rotates_original_client(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        original = env.admit_request()
        wrong_nonce = await env.post("/v1/media/sources/admit", original, nonce="ff" * 32)
        assert wrong_nonce.status_code in {403, 409}
        assert _CAPABILITY.lower() not in wrong_nonce.headers
        changed = await env.post(
            "/v1/media/sources/admit", {**original, "declared_bytes": 7}, nonce=_NONCE
        )
        _error(changed, 409, "conflict")
        historical = await env.lookup("source", source)
        assert historical.json() == {"v": 1, "found": True, "value": source}
        assert len(env.reserve_calls) == 1 and env.spool.reserved_bytes == 6
        async with env.connected(source, grant):
            assert (await env.put(env.put_header(source), b"abcdef", grant)).status_code == 200


async def test_upload_requires_actual_original_websocket_association(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        header = env.put_header(source)
        _error(await env.put(header, b"abcdef", grant), 409, "stale")
        assert env.uploads[source["body"]["source_id"]].next_offset == 0
        async with env.connected(source, grant):
            _typed_receipt(
                await env.put(header, b"abcdef", grant), "UploadPut", "UploadAck", header
            )


async def test_fragmented_upload_exact_replay_key_lookup_and_lookup_kind(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            header = env.put_header(source)
            accepted = await env.put(header, b"abcdef", grant, cuts=[1, 3, 4, 9, 31])
            ack = _typed_receipt(accepted, "UploadPut", "UploadAck", header)
            assert ack["body"] == {
                "v": 1,
                "source": env.ref(source),
                "index": 0,
                "offset": 0,
                "body_bytes": 6,
                "body_sha256": _sha(b"abcdef"),
                "next_offset": 6,
            }
            duplicate = await env.put(header, b"abcdef", grant)
            assert duplicate.content == accepted.content
            lookup = await env.post(
                "/v1/media/lookup-key",
                {
                    "v": 1,
                    "kind": "upload_ack",
                    "key": header["key"],
                    "request_sha256": _digest("UploadPut", header),
                },
            )
            assert lookup.json() == {"v": 1, "found": True, "value": ack}
            wrong_kind = await env.lookup("seal", ack)
            _error(wrong_kind, 404, "unknown_ref")
            assert env.uploads[source["body"]["source_id"]].next_offset == 6


@pytest.mark.parametrize(
    "failure", ["trailing", "hash", "partial", "out_of_order", "sparse", "over_reservation"]
)
async def test_invalid_body_or_order_has_no_complete_ack_or_spool_progress(tmp_path, failure):
    async with _stack(
        tmp_path, allow_unknown_shutdown=failure in {"trailing", "hash", "partial"}
    ) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            header = env.put_header(source)
            body, trailing = b"abcdef", b""
            if failure == "trailing":
                trailing = b"x"
            elif failure == "hash":
                body = b"abcdeg"
            elif failure == "partial":
                body = b"abc"
            elif failure == "out_of_order":
                header["index"] = 1
            else:
                header["offset"] = 1
                if failure == "sparse":
                    # Isolate ordering from the independent reservation bound.
                    body = b"abcde"
                    header["body_bytes"] = len(body)
                    header["body_sha256"] = _sha(body)
            response = await env.put(header, body, grant, trailing=trailing)
            if failure == "over_reservation":
                # Preserve the original offset1 + six-byte overrun unchanged.
                _error(response, 413, "bound")
            else:
                code = "conflict" if failure in {"out_of_order", "sparse"} else "bad_packet"
                _error(response, 409 if code == "conflict" else 400, code)
            assert env.uploads[source["body"]["source_id"]].next_offset == 0
            assert env.spool.reserved_bytes == 6


@pytest.mark.parametrize("part", ["source_revision", "generation", "lease_generation", "epoch"])
async def test_current_fence_components_are_checked_before_write(tmp_path, part):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            header = copy.deepcopy(env.put_header(source))
            if part == "epoch":
                header["fence"]["session"]["epoch"] += 1
            else:
                header["fence"][part] += 1
            _error(await env.put(header, b"abcdef", grant), 409, "stale")
            assert env.uploads[source["body"]["source_id"]].next_offset == 0


async def test_other_original_source_grant_is_not_a_bearer_for_this_source(tmp_path):
    async with _stack(tmp_path) as env:
        first, first_grant = await env.admit(key="source-a", nonce=_NONCE)
        second, second_grant = await env.admit(key="source-b", nonce="fe" * 32)
        assert first["body"]["client_instance"] != second["body"]["client_instance"]
        async with env.connected(first, first_grant):
            _error(await env.put(env.put_header(first), b"abcdef", second_grant), 403, "denied")
            assert env.uploads[first["body"]["source_id"]].next_offset == 0


async def test_all_index_ack_history_survives_recent_cache_boundary(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit(size=66)
        async with env.connected(source, grant):
            first_response = None
            first_header = None
            for index in range(66):
                header = env.put_header(
                    source, key=f"chunk-{index}", index=index, offset=index, body=bytes([index])
                )
                response = await env.put(header, bytes([index]), grant)
                _typed_receipt(response, "UploadPut", "UploadAck", header)
                if index == 0:
                    first_response, first_header = response, header
            assert first_response is not None and first_header is not None
            replay = await env.put(first_header, b"\0", grant)
            assert replay.content == first_response.content
            changed = {**first_header, "body_sha256": _sha(b"x")}
            _error(await env.put(changed, b"x", grant), 409, "conflict")
            assert env.uploads[source["body"]["source_id"]].next_offset == 66


async def test_upload_four_mib_boundary_is_conjunctive_with_reservation(tmp_path):
    async with _stack(tmp_path) as env:
        body = b"s" * 4194304
        source, grant = await env.admit(size=len(body))
        async with env.connected(source, grant):
            header = env.put_header(source, body=body)
            _typed_receipt(await env.put(header, body, grant), "UploadPut", "UploadAck", header)
            assert env.uploads[source["body"]["source_id"]].next_offset == len(body)
            too_large = env.put_header(source, key="over-hard-cap", body=b"x")
            too_large["body_bytes"] = 4194305
            _error(await env.put(too_large, b"x", grant), 413, "bound")


async def test_source_reservation_refuses_capacity_without_eviction(tmp_path):
    limits = _limits(file_bytes=8, owner_spool_bytes=8, global_spool_bytes=8)
    async with _stack(tmp_path, limits=limits) as env:
        first, _ = await env.admit(size=8)
        second = env.admit_request(key="second-source", size=1)
        _error(await env.post("/v1/media/sources/admit", second, nonce="fe" * 32), 429, "capacity")
        assert env.spool.reserved_bytes == 8 and len(env.reserve_calls) == 1
        assert env.uploads[first["body"]["source_id"]].deleted is False


async def test_native_seal_whole_hash_owner_denial_and_original_reader(tmp_path):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            assert (await env.put(env.put_header(source), b"abcdef", grant)).status_code == 200
            denied, _ = await env.seal(source, grant, role="owner")
            assert denied.status_code == 403
            wrong, _ = await env.seal(source, grant, key="wrong-hash", digest="0" * 64)
            _error(wrong, 409, "conflict")
            assert env.uploads[source["body"]["source_id"]].sealed is False
            sealed_response, request = await env.seal(source, grant)
            sealed = _typed_receipt(sealed_response, "SealRequest", "Seal", request)
            assert sealed["body"]["source"] == env.ref(source)
            assert sealed["body"]["bytes"] == 6
            assert sealed["body"]["source_sha256"] == _sha(b"abcdef")
            assert sealed["body"]["state"] == "sealed"
            assert source["body"]["source_equivalence_id"] is None
            original = env.uploads[source["body"]["source_id"]]
            reader = original.open_reader()
            try:
                assert reader.read(65536) == b"abcdef"
                assert reader.read(65536) == b""
            finally:
                reader.close()
            repeated, _ = await env.seal(source, grant)
            assert repeated.content == sealed_response.content
            assert env.spool.reserved_bytes == 6


async def test_same_owner_objective_verified_bytes_reimport_preserves_equivalence(tmp_path):
    async with _stack(tmp_path) as env:
        equivalences = []
        for key, nonce in (("recording-a", _NONCE), ("recording-b", "fe" * 32)):
            source, grant = await env.admit(key=key, nonce=nonce)
            async with env.connected(source, grant):
                header = env.put_header(source, key=f"{key}-chunk")
                assert (await env.put(header, b"abcdef", grant)).status_code == 200
                response, request = await env.seal(source, grant, key=f"{key}-seal")
                sealed = _typed_receipt(response, "SealRequest", "Seal", request)
                equivalences.append(sealed["body"]["source_equivalence_id"])
        assert equivalences[0] == equivalences[1]
        assert len(_submits(env)) == 1
        assert len(env.reserve_calls) == 2


async def test_actual_disconnect_fences_old_grant_without_new_websocket_claim(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            pass
        # connected() positively joins the actual original ASGI task after its
        # disconnect; a timeout is never used as proof of fencing/retirement.
        _error(await env.put(env.put_header(source), b"abcdef", grant), 409, "stale")
        replacement = _ASGIWebSocket(env.app)
        try:
            await replacement.connect(
                {
                    "token": _TOKENS["owner"],
                    "after": 0,
                    "session_id": env.session["session_id"],
                    "media": {"v": 1, "source": env.ref(source), "client_capability": grant},
                }
            )
            refused = await replacement.receive()
            assert refused["type"] == "websocket.close"
        finally:
            await replacement.close()
        assert env.spool.reserved_bytes == 6
        old_stop = env.control_request(source, "stop", key="new-stop-old-fence")
        _error(await env.post("/v1/media/control", old_stop), 409, "stale")
        status = await env.current_status(source)
        assert status["body"]["fence"] != source["body"]["fence"]
        stop = env.control_request(
            source, "stop", key="new-stop-current-fence", fence=status["body"]["fence"]
        )
        stopped = _typed_receipt(
            await env.post("/v1/media/control", stop), "Control", "Status", stop
        )
        assert stopped["body"]["desired"] == "stopped"
        native_source = env.owner._sources[source["body"]["source_id"]]
        original_cleanup = native_source.delete_task
        assert isinstance(original_cleanup, asyncio.Task)
        assert await asyncio.wait_for(asyncio.shield(original_cleanup), _WAIT) is True
        # The stop's actual original cleanup has positively settled; the next
        # authorized request observes it rather than assuming a scheduled task
        # already completed just because the previous HTTP response returned.
        current = await env.current_status(source)
        deletion = {
            "v": 1,
            "key": "disconnected-owner-delete",
            "source": env.ref(source),
            "fence": current["body"]["fence"],
        }
        deleted = _typed_receipt(
            await env.post("/v1/media/delete", deletion), "DeleteRequest", "DeleteReceipt", deletion
        )
        assert deleted["body"]["state"] == "local_deleted"
        assert env.spool.reserved_bytes == 0


async def test_heartbeat_replay_does_not_extend_lease(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            env.clock.advance(10)
            heartbeat = env.control_request(source, "heartbeat", key="original-heartbeat")
            first = await env.post("/v1/media/control", heartbeat, grant=grant)
            _typed_receipt(first, "Control", "Status", heartbeat)
            env.clock.advance(20)
            replay = await env.post("/v1/media/control", heartbeat, grant=grant)
            assert replay.content == first.content
            env.clock.advance(11)
            _error(await env.put(env.put_header(source), b"abcdef", grant), 409, "stale")
            assert env.uploads[source["body"]["source_id"]].next_offset == 0
            assert env.spool.reserved_bytes == 6


async def test_session_reset_fences_original_source_and_retains_historical_receipt(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            reset = await env.client.post(
                f"/v1/sessions/{env.session['session_id']}/reset", headers=_headers()
            )
            assert reset.status_code == 200 and reset.json()["epoch"] == env.session["epoch"] + 1
            _error(await env.put(env.put_header(source), b"abcdef", grant), 409, "stale")
            original = await env.lookup("source", source)
            assert original.json() == {"v": 1, "found": True, "value": source}
            assert env.spool.reserved_bytes == 6


async def test_live_and_inline_paths_remain_explicitly_unavailable(tmp_path):
    async with _stack(tmp_path) as env:
        live = {
            **env.admit_request(),
            "kind": "live",
            "purpose": "owner_voice",
            "anchor": None,
            "declared_bytes": None,
            "declared_sha256": None,
        }
        _error(await env.post("/v1/media/sources/admit", live, nonce=_NONCE), 503, "unavailable")
        next_objective = _OBJECTIVE + " A separate genuine owner objective."
        event = AppendRequest(
            **env.session,
            kind="conversation.user",
            payload={"text": next_objective, "final": True, "revision": 1},
            idempotency_key="next-authenticated-objective",
        )
        written = await env.client.post(
            "/v1/transcript/entries", json=event.model_dump(mode="json"), headers=_headers()
        )
        assert written.status_code == 200
        event_id = written.json()["event_id"]
        task = TaskRequest(
            **env.session,
            objective=next_objective,
            idempotency_key="not-submitted-inline",
            source_event_ids=[event_id],
            source_revision=1,
            content_scope="local_only",
        )
        legacy = _json(task.model_dump(mode="json"))
        inline = {
            "v": 1,
            "key": "unsupported-inline-anchor",
            "owner_objective": {"kind": "owner_text", "event_ref": event_id},
            "scope": "local_only",
            "task_admission": None,
            "submit": {
                "contract": "TaskRequest",
                "canonical_utf8_b64": base64.b64encode(legacy).decode("ascii"),
                "sha256": _sha(legacy),
            },
        }
        _error(await env.post("/v1/media/anchors/admit", inline), 503, "unavailable")
        assert env.reserve_calls == [] and len(_submits(env)) == 1


async def test_missing_nonce_and_disallowed_browser_origin_do_not_allocate(tmp_path):
    async with _stack(tmp_path) as env:
        _error(await env.post("/v1/media/sources/admit", env.admit_request()), 400, "bad_packet")
        headers = {**_headers(nonce=_NONCE), "Origin": "https://untrusted.invalid"}
        denied = await env.client.post(
            "/v1/media/sources/admit", content=_json(env.admit_request()), headers=headers
        )
        assert denied.status_code == 403
        assert env.reserve_calls == [] and env.spool.reserved_bytes == 0


async def test_old_events_hello_still_delivers_real_journal_without_media_grant(tmp_path):
    async with _stack(tmp_path) as env:
        channel = _ASGIWebSocket(env.app)
        try:
            await channel.connect(
                {"token": _TOKENS["owner"], "after": 0, "session_id": env.session["session_id"]}
            )
            first = await channel.receive_json()
            assert first == env.owner_event
        finally:
            await channel.close()


@pytest.mark.parametrize("complete_after_cancel", [False, True])
async def test_observer_cancellation_keeps_original_body_outcome_truthful(
    tmp_path, complete_after_cancel
):
    async with _stack(tmp_path, allow_unknown_shutdown=not complete_after_cancel) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            header = env.put_header(source)
            encoded = _json(header)
            entered, release, producer_retired = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def partial():
                try:
                    yield struct.pack(">I", len(encoded)) + encoded + b"abc"
                    entered.set()
                    await release.wait()
                    if complete_after_cancel:
                        yield b"def"
                finally:
                    producer_retired.set()

            headers = _headers(grant=grant)
            headers["Content-Type"] = "application/vnd.kinaiya.media-frame"
            original = asyncio.create_task(
                env.client.post("/v1/media/uploads/put", content=partial(), headers=headers)
            )
            try:
                await asyncio.wait_for(entered.wait(), _WAIT)
                # This is the actual retained native intake Task, not the
                # cancelled HTTP observer or a reconstructed owner record.
                intakes = [
                    intake
                    for intake in env.owner._intakes.values()
                    if intake["source_id"] == source["body"]["source_id"]
                ]
                assert len(intakes) == 1
                body_owner = intakes[0]["task"]
                assert isinstance(body_owner, asyncio.Task) and body_owner is not original
                original.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(original), _WAIT)
                # Observer cancellation cannot decide the original producer's
                # outcome. Now let it reach a genuinely incomplete or complete
                # EOF, and join that exact retained owner before observing it.
                release.set()
                await asyncio.wait_for(producer_retired.wait(), _WAIT)
                if complete_after_cancel:
                    response = await asyncio.wait_for(asyncio.shield(body_owner), _WAIT)
                    assert response.status_code == 200
                else:
                    with pytest.raises(MediaWireError) as refused:
                        await asyncio.wait_for(asyncio.shield(body_owner), _WAIT)
                    assert refused.value.code == "bad_packet"
                assert body_owner.done()
                if complete_after_cancel:
                    assert env.uploads[source["body"]["source_id"]].next_offset == 6
                else:
                    assert env.uploads[source["body"]["source_id"]].next_offset == 0
                assert env.spool.reserved_bytes == 6
                lookup = await env.post(
                    "/v1/media/lookup-key",
                    {
                        "v": 1,
                        "kind": "upload_ack",
                        "key": header["key"],
                        "request_sha256": _digest("UploadPut", header),
                    },
                )
                assert lookup.status_code == 200
                if complete_after_cancel:
                    found = lookup.json()
                    assert found["v"] == 1 and found["found"] is True
                    ack = found["value"]
                    assert ack["key"] == header["key"]
                    assert ack["request_sha256"] == _digest("UploadPut", header)
                    assert ack["body"] == {
                        "v": 1,
                        "source": env.ref(source),
                        "index": 0,
                        "offset": 0,
                        "body_bytes": 6,
                        "body_sha256": _sha(b"abcdef"),
                        "next_offset": 6,
                    }
                    assert ack["sha256"] == _digest(
                        "R<UploadAck>",
                        {key: value for key, value in ack.items() if key != "sha256"},
                    )
                    assert lookup.content == _json(found)
                else:
                    assert lookup.json() == {"v": 1, "found": False, "value": None}
                # Neither a historical ACK nor its absence grants new ingress
                # authority or proves physical cleanup after observer loss.
            finally:
                release.set()
                if not original.done():
                    original.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(original), _WAIT)
                    except asyncio.CancelledError:
                        pass


async def test_stop_while_original_append_is_blocked_retains_charge_and_no_new_attempt(tmp_path):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        source, grant = await env.admit()
        upload = env.uploads[source["body"]["source_id"]]
        native_append = upload.append
        entered, release = threading.Event(), threading.Event()
        calls = []
        progress = []

        def held_append(*args):
            calls.append(args)
            entered.set()
            assert release.wait(_WAIT), "Synthetic original append was not released"
            result = native_append(*args)
            progress.append(result)
            return result

        async with env.connected(source, grant):
            with patch.object(upload, "append", side_effect=held_append):
                header = env.put_header(source)
                original = asyncio.create_task(env.put(header, b"abcdef", grant))
                try:
                    assert await asyncio.to_thread(entered.wait, _WAIT)
                    stop = env.control_request(source, "stop", key="stop-original-intake")
                    stopped = await env.post("/v1/media/control", stop, grant=grant)
                    status = _typed_receipt(stopped, "Control", "Status", stop)
                    assert status["body"]["desired"] == "stopped"
                    assert status["body"]["local_actual"] in {"fenced", "closing", "unknown"}
                    assert env.spool.reserved_bytes == 6
                    assert len(calls) == 1
                    release.set()
                    result = await asyncio.wait_for(asyncio.shield(original), _WAIT)
                    assert result.status_code in {409, 503}
                    # Stop fenced publication after this original native call
                    # had already entered. Its real successful append remains
                    # physical history, without a current successful ACK.
                    assert len(progress) == 1 and progress[0].next_offset == 6
                    assert upload.next_offset == progress[0].next_offset
                    assert env.spool.reserved_bytes == 6
                    # Same original header cannot start another append after a fence.
                    replay = await env.put(header, b"abcdef", grant)
                    assert replay.status_code in {409, 503}
                    assert len(calls) == 1
                finally:
                    release.set()
                    if not original.done():
                        await asyncio.wait_for(asyncio.shield(original), _WAIT)


async def test_actual_reader_holds_native_delete_and_session_delete_until_positive_close(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            assert (await env.put(env.put_header(source), b"abcdef", grant)).status_code == 200
            sealed, request = await env.seal(source, grant)
            _typed_receipt(sealed, "SealRequest", "Seal", request)
            upload = env.uploads[source["body"]["source_id"]]
            native_source = env.owner._sources[source["body"]["source_id"]]
            begin_change = env.owner.begin_session_change
            changes = []

            def observe_change(*args, **kwargs):
                change = begin_change(*args, **kwargs)
                changes.append(change)
                return change

            reader = upload.open_reader()
            with patch.object(env.owner, "begin_session_change", side_effect=observe_change):
                try:
                    assert reader.read(3) == b"abc"
                    deletion = {
                        "v": 1,
                        "key": "delete-with-reader",
                        "source": env.ref(source),
                        "fence": source["body"]["fence"],
                    }
                    pending = _typed_receipt(
                        await env.post("/v1/media/delete", deletion, grant=grant),
                        "DeleteRequest",
                        "DeleteReceipt",
                        deletion,
                    )
                    assert pending["body"]["state"] == "local_delete_pending"
                    assert pending["body"]["local_spool_unlinked"] is False
                    assert pending["body"]["local_processing_reservation_released"] is False
                    assert upload.deleted is False and env.spool.reserved_bytes == 6
                    session_delete = await env.client.delete(
                        f"/v1/sessions/{env.session['session_id']}", headers=_headers()
                    )
                    assert session_delete.status_code in {409, 503}
                    assert (
                        env.store.session(env.session["session_id"]).epoch == env.session["epoch"]
                    )
                    assert len(changes) == 1
                    original_change = changes[0]
                    checkpoint_task = native_source.delete_task
                    assert isinstance(checkpoint_task, asyncio.Task)
                    assert await asyncio.wait_for(asyncio.shield(checkpoint_task), _WAIT) is False
                    assert reader in upload._readers
                    assert env.owner._sources[source["body"]["source_id"]] is native_source
                    assert upload.deleted is False and env.spool.reserved_bytes == 6
                finally:
                    reader.close()

                # The committed Session fence still owns the pending change.
                # A fresh authenticated Session DELETE must reconcile that SAME
                # actual checkpoint; it cannot require new media admission.
                recovery = await env.client.delete(
                    f"/v1/sessions/{env.session['session_id']}", headers=_headers()
                )
                assert recovery.status_code in {200, 409}
                assert changes and all(change is original_change for change in changes)
                continuation = native_source.cleanup_continuation
                assert continuation is not None
                assert continuation.checkpoint.task is checkpoint_task
                assert continuation.authority.change is original_change
                assert continuation.original is original_change
                assert continuation.authority.dispatch is original_change.dispatch
                continuation_task = continuation.task
                assert isinstance(continuation_task, asyncio.Task)
                assert await asyncio.wait_for(asyncio.shield(continuation_task), _WAIT) is True
                assert continuation.entered_task is continuation_task
                assert continuation.completed_task is continuation_task
                assert continuation.unknown is False
                assert pending["body"]["state"] == "local_delete_pending"
                assert upload.deleted is True and env.spool.reserved_bytes == 0
                if recovery.status_code != 200:
                    final_session_delete = await env.client.delete(
                        f"/v1/sessions/{env.session['session_id']}", headers=_headers()
                    )
                else:
                    final_session_delete = recovery
                assert final_session_delete.status_code == 200
                assert all(change is original_change for change in changes)


async def test_reader_checkpoint_needs_new_authorized_media_delete_and_actual_continuation(
    tmp_path,
):
    """Source-aware addition: pure media deletion, without a pending Session gate."""
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            assert (await env.put(env.put_header(source), b"abcdef", grant)).status_code == 200
            sealed, request = await env.seal(source, grant)
            _typed_receipt(sealed, "SealRequest", "Seal", request)
            upload = env.uploads[source["body"]["source_id"]]
            native_source = env.owner._sources[source["body"]["source_id"]]
            reader = upload.open_reader()
            try:
                assert reader.read(3) == b"abc"
                deletion = {
                    "v": 1,
                    "key": "media-delete-with-reader",
                    "source": env.ref(source),
                    "fence": source["body"]["fence"],
                }
                pending = _typed_receipt(
                    await env.post("/v1/media/delete", deletion, grant=grant),
                    "DeleteRequest",
                    "DeleteReceipt",
                    deletion,
                )
                assert pending["body"]["state"] == "local_delete_pending"
                assert pending["body"]["local_spool_unlinked"] is False
                assert pending["body"]["local_processing_reservation_released"] is False
                checkpoint_task = native_source.delete_task
                assert isinstance(checkpoint_task, asyncio.Task)
                assert await asyncio.wait_for(asyncio.shield(checkpoint_task), _WAIT) is False
                assert upload.deleted is False and env.spool.reserved_bytes == 6
            finally:
                reader.close()

            current = await env.current_status(source)
            continued_request = {
                **deletion,
                "key": "media-delete-after-reader-close",
                "fence": current["body"]["fence"],
            }
            continued = _typed_receipt(
                await env.post("/v1/media/delete", continued_request),
                "DeleteRequest",
                "DeleteReceipt",
                continued_request,
            )
            assert continued["body"]["state"] in {"local_delete_pending", "local_deleted"}
            continuation = native_source.cleanup_continuation
            assert continuation is not None
            assert continuation.checkpoint.task is checkpoint_task
            assert continuation.original is env.owner._operations[continued_request["key"]]
            assert continuation.authority.original is continuation.original
            continuation_task = continuation.task
            assert isinstance(continuation_task, asyncio.Task)
            assert await asyncio.wait_for(asyncio.shield(continuation_task), _WAIT) is True
            assert continuation.entered_task is continuation_task
            assert continuation.completed_task is continuation_task
            assert continuation.unknown is False
            assert upload.deleted is True and env.spool.reserved_bytes == 0
            # The first pending receipt remains its original snapshot. A new
            # authorized current-fence request obtains the completed snapshot.
            assert pending["body"]["state"] == "local_delete_pending"
            current = await env.current_status(source)
            completed_request = {
                **deletion,
                "key": "media-delete-settled-snapshot",
                "fence": current["body"]["fence"],
            }
            completed = _typed_receipt(
                await env.post("/v1/media/delete", completed_request),
                "DeleteRequest",
                "DeleteReceipt",
                completed_request,
            )
            assert completed["body"]["state"] == "local_deleted"
            assert completed["body"]["local_children_settled"] is True
            assert completed["body"]["local_spool_unlinked"] is True
            assert completed["body"]["local_processing_reservation_released"] is True
            assert completed["body"]["remote_state"] == "none"
            assert upload.deleted is True and env.spool.reserved_bytes == 0
            assert native_source.delete_task is continuation_task
            final_session_delete = await env.client.delete(
                f"/v1/sessions/{env.session['session_id']}", headers=_headers()
            )
            assert final_session_delete.status_code == 200
            # Session fencing retains the same positively completed original;
            # it must not replace that proof with another physical cleanup.
            assert native_source.delete_task is continuation_task
            assert continuation.completion_task is continuation_task


@pytest.mark.parametrize("facade_kind", ["future", "task"])
async def test_cleanup_scheduler_facade_cannot_release_original_custody(tmp_path, facade_kind):
    """Source-informed fault regression; a scheduler facade is no native receipt."""
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            assert (await env.put(env.put_header(source), b"abcdef", grant)).status_code == 200
            sealed, request = await env.seal(source, grant)
            _typed_receipt(sealed, "SealRequest", "Seal", request)
            upload = env.uploads[source["body"]["source_id"]]
            native_source = env.owner._sources[source["body"]["source_id"]]
            reader = upload.open_reader()
            try:
                assert reader.read(3) == b"abc"
                deletion = {
                    "v": 1,
                    "key": "facade-reader-delete",
                    "source": env.ref(source),
                    "fence": source["body"]["fence"],
                }
                pending = _typed_receipt(
                    await env.post("/v1/media/delete", deletion, grant=grant),
                    "DeleteRequest",
                    "DeleteReceipt",
                    deletion,
                )
                assert pending["body"]["state"] == "local_delete_pending"
                checkpoint_task = native_source.delete_task
                assert isinstance(checkpoint_task, asyncio.Task)
                assert await asyncio.wait_for(asyncio.shield(checkpoint_task), _WAIT) is False
                assert upload.deleted is False and env.spool.reserved_bytes == 6
            finally:
                reader.close()

            create_task = asyncio.create_task

            async def completed_facade():
                return True

            if facade_kind == "future":
                facade = asyncio.get_running_loop().create_future()
                facade.set_result(True)
            else:
                facade = create_task(completed_facade())
                assert await asyncio.wait_for(asyncio.shield(facade), _WAIT) is True
            assert facade.done() and facade.result() is True
            dispatched = []

            def dispatch_but_return_facade(coroutine, *args, **kwargs):
                # Only the selected native continuation is intercepted. Its
                # actual coroutine is dispatched and retained by this fixture;
                # every other native task uses the real scheduler unchanged.
                if (
                    getattr(getattr(coroutine, "cr_code", None), "co_name", None)
                    == "_continue_cleanup"
                ):
                    actual = create_task(coroutine, *args, **kwargs)
                    dispatched.append(actual)
                    return facade
                return create_task(coroutine, *args, **kwargs)

            try:
                current = await env.current_status(source)
                continued_request = {
                    **deletion,
                    "key": "facade-authorized-reader-recovery",
                    "fence": current["body"]["fence"],
                }
                with patch("asyncio.create_task", side_effect=dispatch_but_return_facade):
                    response = await env.post("/v1/media/delete", continued_request)
                    assert response.status_code in {200, 409, 503}
                    assert len(dispatched) == 1
                    assert isinstance(dispatched[0], asyncio.Task) and dispatched[0] is not facade
                    continuation = native_source.cleanup_continuation
                    assert continuation is not None
                    assert continuation.checkpoint.task is checkpoint_task
                    assert continuation.assigned.is_set() is False
                    assert continuation.unknown is True
                    if response.status_code == 200:
                        observed = _typed_receipt(
                            response, "DeleteRequest", "DeleteReceipt", continued_request
                        )
                        assert observed["body"]["state"] == "local_delete_pending"
                        assert observed["body"]["local_children_settled"] is False
                        assert observed["body"]["local_spool_unlinked"] is False
                        assert observed["body"]["local_processing_reservation_released"] is False
                    assert upload.deleted is False and env.spool.reserved_bytes == 6
                    session_delete = await env.client.delete(
                        f"/v1/sessions/{env.session['session_id']}", headers=_headers()
                    )
                    assert session_delete.status_code in {409, 503}
                    assert (
                        env.store.session(env.session["session_id"]).epoch == env.session["epoch"]
                    )
                    assert await env.owner.close() is False
                    assert upload.deleted is False and env.spool.reserved_bytes == 6
                    assert len(dispatched) == 1
                    assert continuation.assigned.is_set() is False
                    assert continuation.unknown is True
            finally:
                # This retires only the exact hidden synthetic scheduler task,
                # AFTER the negative native ownership assertions. It grants no
                # settlement to the native owner, which remains unknown.
                for actual in dispatched:
                    if not actual.done():
                        actual.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(actual), _WAIT)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        if not actual.done():
                            raise
                    assert actual.done()


async def test_credential_change_invalidates_old_media_context_not_just_old_bearer(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            env.auth.tokens["owner"] = "n" * 32
            old = await env.put(env.put_header(source), b"abcdef", grant)
            _error(old, 403, "denied")
            header = env.put_header(source, key="new-bearer-old-client")
            encoded = _json(header)
            headers = _headers(grant=grant)
            headers["Authorization"] = "Bearer " + "n" * 32
            headers["Content-Type"] = "application/vnd.kinaiya.media-frame"
            fresh_bearer = await env.client.post(
                "/v1/media/uploads/put",
                content=struct.pack(">I", len(encoded)) + encoded + b"abcdef",
                headers=headers,
            )
            assert fresh_bearer.status_code in {403, 409, 503}
            assert env.uploads[source["body"]["source_id"]].next_offset == 0
            assert env.spool.reserved_bytes == 6


async def test_valid_original_operations_use_serial_non_nested_transactions(tmp_path):
    async with _stack(tmp_path) as env:
        statements = []
        env.store.db.set_trace_callback(statements.append)
        try:
            source, grant = await env.admit()
            async with env.connected(source, grant):
                header = env.put_header(source)
                _typed_receipt(
                    await env.put(header, b"abcdef", grant), "UploadPut", "UploadAck", header
                )
                response, request = await env.seal(source, grant)
                _typed_receipt(response, "SealRequest", "Seal", request)
                found = await env.lookup("source", source)
                assert found.json()["value"] == source
        finally:
            env.store.db.set_trace_callback(None)
        depth, beginnings = 0, 0
        for sql in statements:
            first = sql.strip().split(maxsplit=1)[0].upper()
            if first == "BEGIN":
                assert depth == 0, "Nested transaction appeared at actual native SQLite seam"
                depth = 1
                beginnings += 1
            elif first in {"COMMIT", "ROLLBACK"}:
                depth = 0
        assert beginnings > 0 and depth == 0


async def test_converse_authenticated_user_label_is_not_a_new_owner_objective_proof(tmp_path):
    async with _stack(tmp_path) as env:
        claimed = AppendRequest(
            **env.session,
            kind="conversation.user",
            payload={"text": _OBJECTIVE, "final": True, "revision": 1},
            idempotency_key="converse-recording-narration",
        )
        written = await env.client.post(
            "/v1/transcript/entries",
            json=claimed.model_dump(mode="json"),
            headers=_headers("converse"),
        )
        assert written.status_code == 200
        assert written.json()["origin"] == "authenticated-user"
        request = _submit_proposal(
            env.session,
            written.json(),
            task_id="00000000-0000-4000-8000-000000000205",
            key="native-admission-with-unowned-source",
        )
        admitted = await env.client.post(
            "/v1/transcript/agent-submits",
            json=request.model_dump(mode="json"),
            headers=_headers("agent"),
        )
        assert admitted.status_code == 200
        proof_request = {
            "v": 1,
            "key": "not-a-real-owner-proof",
            "session": env.session,
            "original_admission_id": admitted.json()["entry"]["event_id"],
        }
        _error(
            await env.post("/v1/media/anchors/task-proof", proof_request, role="agent"),
            403,
            "denied",
        )
        assert env.reserve_calls == []
        # The fixture itself made two ordinary native admissions; the rejected
        # media proof must create no third task or alter their durable identities.
        assert len(_submits(env)) == 2


async def test_generic_journal_append_cannot_publish_media_arming_authority(tmp_path):
    async with _stack(tmp_path) as env:
        forged = AppendRequest(
            **env.session,
            kind="media.owner_capture_arming",
            payload={"owner_event": env.owner_event["event_id"], "scope": "local_only"},
            idempotency_key="untrusted-media-authority-label",
        )
        refused = await env.client.post(
            "/v1/transcript/entries",
            json=forged.model_dump(mode="json"),
            headers=_headers("converse"),
        )
        assert refused.status_code == 403
        assert env.store.read(kinds=["media.owner_capture_arming"]) == []
        assert len(_submits(env)) == 1


async def test_upload_with_valid_hash_cannot_exceed_original_byte_reservation(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit(size=6)
        async with env.connected(source, grant):
            body = b"abcdefg"
            _error(await env.put(env.put_header(source, body=body), body, grant), 413, "bound")
            assert env.uploads[source["body"]["source_id"]].next_offset == 0
            assert env.spool.reserved_bytes == 6


async def test_current_status_is_read_only_and_never_renews_original_lease(tmp_path):
    async with _stack(tmp_path) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            first = await env.current_status(source)
            env.clock.advance(20)
            same = await env.current_status(source)
            assert same == first
            _no_secrets(same, grant)
            env.clock.advance(11)
            expired = await env.current_status(source)
            assert expired["body"]["reason"] == "lease_expired"
            assert expired["body"]["fence"] != first["body"]["fence"]
            _error(await env.put(env.put_header(source), b"abcdef", grant), 409, "stale")
            assert env.spool.reserved_bytes == 6
