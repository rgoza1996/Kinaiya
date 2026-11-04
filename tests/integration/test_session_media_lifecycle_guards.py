"""SOURCE-INFORMED C1/C2 native Session lifecycle regression oracles.

Authored from root's selected lifecycle seams and already exposed c2f4 baseline,
before new C1/C2 bodies or their tests were read. Disposable native SQLite,
spool and authenticated ASGI are exercised only by the eventual root run.
A cancelled observer, fenced gate, or teardown is never physical cleanup proof.
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from packages.contracts import media
from packages.contracts.models import AppendRequest, Session
from services.common.auth import Auth
from services.session.agent_control import AgentControlUnavailable
from services.session.api import create_app
from services.session.media_clients import MediaGrantKeyStore, MediaSessionError
from services.session.media_owner import MediaSessionOwner
from services.session.media_spool import MediaSpool, MediaSpoolError
from services.session.store import JournalError, Store

_WAIT = 10
_ORIGIN = "http://synthetic-owner.test"
_CAPABILITY = "X-Kinaiya-Media-Client-Capability"
_NONCE_HEADER = "X-Kinaiya-Media-Bootstrap-Nonce"
_NONCE = bytes(range(32)).hex()
_TOKENS = {
    role: char * 32
    for role, char in zip(
        ("owner", "agent", "converse", "decisions", "resources"), "oacdr", strict=True
    )
}
_OBJECTIVE = "Analyze this synthetic recording as evidence for this owner task."


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
    dropper: object | None
    key_path: Path
    owner: MediaSessionOwner
    db: object

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
            await channel.close()

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


class _ObservedConnection(sqlite3.Connection):
    """Actual native connection; faults occur at its original SQL boundary."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault = None
        self.commit_hits = 0
        self.rollback_hits = 0
        self.positive_rollbacks = 0
        self.update_rejected = False

    def _after_commit(self):
        if self.fault == "commit_after_effect":
            self.fault = None
            self.commit_hits += 1
            raise sqlite3.OperationalError("synthetic lost original commit acknowledgement")

    def _before_rollback(self):
        if self.fault == "rollback_unknown" and self.update_rejected:
            self.rollback_hits += 1
            raise sqlite3.OperationalError("synthetic original rollback uncertainty")

    def commit(self):
        result = super().commit()
        self._after_commit()
        return result

    def rollback(self):
        self._before_rollback()
        active = self.in_transaction
        result = super().rollback()
        if active and not self.in_transaction:
            self.positive_rollbacks += 1
        return result

    def __exit__(self, exc_type, exc_value, traceback):
        # sqlite's C context-manager can bypass Python commit/rollback
        # overrides. Armed faults must observe that original boundary too.
        if self.fault is not None and self.in_transaction:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
            return False
        return super().__exit__(exc_type, exc_value, traceback)

    def execute(self, sql, parameters=()):
        command = sql.lstrip().split(maxsplit=1)[0].upper()
        if (
            self.fault in {"rollback_unknown", "write_refused"}
            and not self.update_rejected
            and re.match(r"\s*UPDATE\s+sessions\b", sql, re.IGNORECASE)
        ):
            self.update_rejected = True
            raise sqlite3.OperationalError("synthetic failed original Session write")
        if command == "ROLLBACK":
            self._before_rollback()
        active = self.in_transaction
        result = super().execute(sql, parameters)
        if command == "ROLLBACK" and active and not self.in_transaction:
            self.positive_rollbacks += 1
        if command in {"COMMIT", "END"}:
            self._after_commit()
        return result


@asynccontextmanager
async def _stack(tmp_path, *, count=1, allow_unknown_shutdown=False):
    # Key custody requires a protected ancestor chain, independent of the
    # checkout's permissions or pytest's configured artifact directory.
    with tempfile.TemporaryDirectory(prefix="kinaiya-test-native-media-", dir="/tmp") as private:
        async with _native_stack(
            tmp_path, Path(private), count=count, allow_unknown_shutdown=allow_unknown_shutdown
        ) as env:
            yield env


@asynccontextmanager
async def _native_stack(tmp_path, native_root, *, count, allow_unknown_shutdown):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spool_root = tmp_path / "spool"
    spool_root.mkdir(mode=0o700)
    spool_root.chmod(0o700)
    connect = sqlite3.connect

    def native_connection(*args, **kwargs):
        assert "factory" not in kwargs
        return connect(*args, factory=_ObservedConnection, **kwargs)

    with patch("services.session.store.sqlite3.connect", side_effect=native_connection):
        store = Store(native_root / "session.sqlite")
    db = store.db
    assert isinstance(db, _ObservedConnection)
    limits = _limits()
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
    app = create_app(store, auth, media_owner=owner)
    uploads, reserve_calls = {}, []
    reserve = spool.reserve

    def observe_reserve(source_id, reserved_bytes):
        original = reserve(source_id, reserved_bytes)
        reserve_calls.append((source_id, reserved_bytes))
        uploads[source_id] = original
        return original

    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        with patch.object(spool, "reserve", side_effect=observe_reserve):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://session.test"
            ) as client:
                envs = []
                for index in range(count):
                    created = await client.post("/v1/sessions", json={}, headers=_headers())
                    assert created.status_code == 200
                    session = {k: created.json()[k] for k in ("session_id", "thread_id", "epoch")}
                    control = await client.get("/v1/agent-control/state", headers=_headers())
                    assert control.status_code == 200
                    backend = control.json()["backend_id"]
                    original = AppendRequest(
                        **session,
                        kind="conversation.user",
                        payload={"text": _OBJECTIVE, "final": True, "revision": 1},
                        idempotency_key="original-native-owner-objective",
                    )
                    response = await client.post(
                        "/v1/transcript/entries",
                        json=original.model_dump(mode="json"),
                        headers=_headers(),
                    )
                    assert response.status_code == 200
                    event = response.json()
                    proposal = _submit_proposal(session, event, task_id=str(uuid4()))
                    submitted = await client.post(
                        "/v1/transcript/agent-submits",
                        json=proposal.model_dump(mode="json"),
                        headers=_headers("agent"),
                    )
                    assert submitted.status_code == 200
                    submit = submitted.json()["entry"]
                    proof_request = {
                        "v": 1,
                        "key": f"proof-{index}",
                        "session": session,
                        "original_admission_id": submit["event_id"],
                    }
                    proof = _typed_receipt(
                        await client.post(
                            "/v1/media/anchors/task-proof",
                            content=_json(proof_request),
                            headers=_headers("agent"),
                        ),
                        "TaskProofRequest",
                        "TaskProof",
                        proof_request,
                    )
                    anchor_request = {
                        "v": 1,
                        "key": f"anchor-{index}",
                        "owner_objective": {
                            "kind": "owner_text",
                            "event_ref": event["event_id"],
                        },
                        "scope": "local_only",
                        "submit": None,
                        "task_admission": {
                            "backend_id": backend,
                            "receipt_id": proof["id"],
                            "sha256": proof["sha256"],
                        },
                    }
                    anchor = _typed_receipt(
                        await client.post(
                            "/v1/media/anchors/admit",
                            content=_json(anchor_request),
                            headers=_headers(),
                        ),
                        "AnchorAdmission",
                        "AnalyzeTaskAnchor",
                        anchor_request,
                    )
                    envs.append(
                        _Env(
                            client,
                            app,
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
                            None,
                            key_path,
                            owner,
                            db,
                        )
                    )
                yield envs[0] if count == 1 else tuple(envs)
    finally:
        # Fault tests deliberately retain native uncertainty. Retiring this
        # disposable fixture's actual handles cannot certify that old outcome.
        db.fault = None
        if db.in_transaction:
            sqlite3.Connection.rollback(db)
        try:
            try:
                await lifespan.__aexit__(None, None, None)
            except RuntimeError:
                if not allow_unknown_shutdown:
                    raise
                assert app.state.media_owner_shutdown_unconfirmed is True
        finally:
            spool.close()
            try:
                store.close()
            except JournalError:
                if not allow_unknown_shutdown:
                    raise
                try:
                    store.agent_control.close()
                except AgentControlUnavailable:
                    pass
                sqlite3.Connection.close(db)
            finally:
                # Explicit disposal of this fixture's actual descriptors is
                # separate from an earlier unconfirmed native owner shutdown.
                keys.close()


async def _event(event):
    assert await asyncio.to_thread(event.wait, _WAIT), "Required native phase was not observed"


class _HeldSQL:
    """A controlled thread owns the real Store lock and original SQL transaction."""

    def __init__(self, env):
        self.env = env
        self.entered = threading.Event()
        self.release = threading.Event()
        self.retired = threading.Event()
        self.expired = False
        self.errors = []
        self.thread = None

    def _hold(self):
        acquired = False
        try:
            acquired = self.env.store.lock.acquire(timeout=_WAIT)
            assert acquired, "Original fixture SQL lock could not be acquired"
            self.env.db.execute("BEGIN IMMEDIATE")
            try:
                self.entered.set()
                if not self.release.wait(_WAIT):
                    self.expired = True
            finally:
                self.env.db.rollback()
        except BaseException as error:
            self.errors.append(error)
        finally:
            if acquired:
                self.env.store.lock.release()
            self.retired.set()

    async def __aenter__(self):
        self.thread = threading.Thread(target=self._hold)
        self.thread.start()
        try:
            await _event(self.entered)
            assert not self.errors and self.env.db.in_transaction
        except BaseException:
            self.release.set()
            await asyncio.to_thread(self.thread.join, _WAIT)
            assert not self.thread.is_alive()
            raise
        return self

    async def __aexit__(self, *_):
        self.release.set()
        await _event(self.retired)
        await asyncio.to_thread(self.thread.join, _WAIT)
        assert not self.thread.is_alive()
        assert not self.errors
        assert not self.expired, "SQL holder released only by regression safety bound"


async def _api_change(env, reason):
    path = f"/v1/sessions/{env.session['session_id']}"
    if reason == "reset":
        return await env.client.post(path + "/reset", headers=_headers())
    return await env.client.delete(path, headers=_headers())


def _native_change(env, reason, change):
    method = env.store.reset if reason == "reset" else env.store.delete
    return method(env.session["session_id"], _media_change=change)


def _epoch(env):
    with env.store.lock:
        row = env.db.execute(
            "SELECT value FROM sessions WHERE session_id=?", (env.session["session_id"],)
        ).fetchone()
    return None if row is None else json.loads(row[0])["epoch"]


def _assert_native_error(call, code):
    with pytest.raises(MediaSessionError) as caught:
        call()
    assert caught.value.code == code
    return caught.value


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ("reset", "deleted"))
async def test_ram_fence_and_request_session_precede_original_sql_wait(tmp_path, reason):
    async with _stack(tmp_path, count=2, allow_unknown_shutdown=reason == "deleted") as (
        env,
        other,
    ):
        source, grant = await env.admit()
        other_source, other_grant = await other.admit(
            key="other-source", nonce=bytes(reversed(range(32))).hex()
        )
        native_source = env.owner._sources[source["body"]["source_id"]]
        native_other = env.owner._sources[other_source["body"]["source_id"]]
        prepare = env.owner._admit_prepare
        begin = env.owner.begin_session_change
        prepared = threading.Event()
        prepare_release = threading.Event()
        enrolled = threading.Event()
        original = []
        prepare_expired = []
        pending_request = env.admit_request(key="pre-prepare-original")
        original_reservations = list(env.reserve_calls)

        def hold_prepare(request, principal, nonce, operation):
            if request.key == pending_request["key"]:
                original.append(operation)
                prepared.set()
                if not prepare_release.wait(_WAIT):
                    prepare_expired.append(True)
            return prepare(request, principal, nonce, operation)

        def observe_begin(session_id, selected_reason):
            change = begin(session_id, selected_reason)
            if session_id == env.session["session_id"] and selected_reason == reason:
                enrolled.set()
            return change

        async with env.connected(source, grant), other.connected(other_source, other_grant):
            with patch.object(env.owner, "_admit_prepare", side_effect=hold_prepare):
                admission = asyncio.create_task(
                    env.post(
                        "/v1/media/sources/admit",
                        pending_request,
                        nonce=bytes(range(1, 33)).hex(),
                    )
                )
                operation = None
                changing = None
                try:
                    await _event(prepared)
                    assert len(original) == 1
                    operation = original[0]
                    assert operation is env.owner._operations[pending_request["key"]]
                    await asyncio.wait_for(operation.assigned.wait(), _WAIT)
                    assert isinstance(operation.task, asyncio.Task)
                    assert not operation.task.done()
                    assert operation.source_id is None
                    assert operation.session_id == env.session["session_id"]
                    assert not operation.fenced.is_set()
                    async with _HeldSQL(env) as held:
                        with patch.object(
                            env.owner, "begin_session_change", side_effect=observe_begin
                        ):
                            changing = asyncio.create_task(_api_change(env, reason))
                            await _event(enrolled)
                            assert not held.expired and not held.retired.is_set()
                            assert native_source.fenced.is_set()
                            assert operation.fenced.is_set()
                            assert not native_other.fenced.is_set()
                            responsive = asyncio.Event()
                            asyncio.get_running_loop().call_soon(responsive.set)
                            await asyncio.wait_for(responsive.wait(), _WAIT)
                            assert not held.retired.is_set() and not held.expired
                            assert not changing.done()
                            prepare_release.set()
                    denied = await asyncio.wait_for(asyncio.shield(admission), _WAIT)
                    assert denied.status_code in {409, 503}
                    assert env.reserve_calls == original_reservations
                    result = await asyncio.wait_for(asyncio.shield(changing), _WAIT)
                    if reason == "reset":
                        assert result.status_code == 200
                        assert result.json()["epoch"] == env.session["epoch"] + 1
                    else:
                        assert result.status_code in {200, 409, 503}
                    assert not prepare_expired
                    assert not native_other.fenced.is_set()
                    written = await other.put(
                        other.put_header(other_source), b"abcdef", other_grant
                    )
                    assert written.status_code == 200
                    assert env.uploads[other_source["body"]["source_id"]].next_offset == 6
                finally:
                    prepare_release.set()
                    for task in (admission, changing):
                        if task is not None:
                            await asyncio.wait_for(asyncio.shield(task), _WAIT)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ("reset", "deleted"))
async def test_actual_nested_transaction_is_refused_without_committing_caller(tmp_path, reason):
    async with _stack(tmp_path) as env:
        env.db.execute("CREATE TEMP TABLE caller_marker(value TEXT)")
        env.db.execute("BEGIN IMMEDIATE")
        env.db.execute("INSERT INTO caller_marker VALUES ('uncommitted caller work')")
        try:
            _assert_native_error(lambda: _native_change(env, reason, None), "unavailable")
            assert env.db.in_transaction
            assert env.db.execute("SELECT COUNT(*) FROM caller_marker").fetchone()[0] == 1
            assert _epoch(env) == env.session["epoch"]
        finally:
            env.db.rollback()
        assert env.db.execute("SELECT COUNT(*) FROM caller_marker").fetchone()[0] == 0
        assert _epoch(env) == env.session["epoch"]


@pytest.mark.asyncio
async def test_commit_then_raise_poison_retains_original_and_blocks_new_media(tmp_path):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        change = env.owner.begin_session_change(env.session["session_id"], "reset")
        env.db.fault = "commit_after_effect"
        with pytest.raises(MediaSessionError) as caught:
            await asyncio.to_thread(_native_change, env, "reset", change)
        assert caught.value.code == "unavailable"
        assert env.db.commit_hits == 1
        assert not env.db.in_transaction
        assert _epoch(env) == env.session["epoch"] + 1
        committed = env.db.total_changes
        _assert_native_error(lambda: _native_change(env, "reset", change), "unavailable")
        assert env.db.total_changes == committed
        with env.store.lock:
            try:
                with env.owner.session_change_transaction(change, "reset") as view:
                    assert view.state == "unknown"
                    assert view.result is None
            except MediaSessionError as error:
                assert error.code == "unavailable"
        rejected = await env.post("/v1/media/sources/admit", env.admit_request(), nonce=_NONCE)
        _error(rejected, 503, "unavailable")
        assert env.reserve_calls == [] and env.spool.reserved_bytes == 0
        assert _epoch(env) == env.session["epoch"] + 1


@pytest.mark.asyncio
async def test_uncertain_original_rollback_poison_cannot_be_treated_as_no_effect(tmp_path):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        change = env.owner.begin_session_change(env.session["session_id"], "reset")
        env.db.fault = "rollback_unknown"
        with pytest.raises(MediaSessionError) as caught:
            await asyncio.to_thread(_native_change, env, "reset", change)
        assert caught.value.code == "unavailable"
        assert env.db.update_rejected and env.db.rollback_hits >= 1
        assert env.db.in_transaction, "The injected native rollback really remained unconfirmed"
        _assert_native_error(lambda: _native_change(env, "reset", change), "unavailable")
        # This separate fixture-owned rollback makes the SQLite handle quiet;
        # it does not retroactively acknowledge the failed original boundary.
        env.db.fault = None
        sqlite3.Connection.rollback(env.db)
        assert not env.db.in_transaction
        _assert_native_error(lambda: _native_change(env, "reset", change), "unavailable")
        rejected = await env.post("/v1/media/sources/admit", env.admit_request(), nonce=_NONCE)
        _error(rejected, 503, "unavailable")
        assert env.reserve_calls == [] and env.spool.reserved_bytes == 0
        assert _epoch(env) == env.session["epoch"]


@pytest.mark.asyncio
async def test_positive_no_effect_rollback_refuses_original_and_releases_pending_gate(tmp_path):
    async with _stack(tmp_path) as env:
        change = env.owner.begin_session_change(env.session["session_id"], "reset")
        env.db.fault = "write_refused"
        with pytest.raises(MediaSessionError) as caught:
            await asyncio.to_thread(_native_change, env, "reset", change)
        assert caught.value.code == "unavailable"
        assert env.db.update_rejected and env.db.positive_rollbacks >= 1
        assert not env.db.in_transaction
        assert _epoch(env) == env.session["epoch"]
        refused_changes = env.db.total_changes
        _assert_native_error(lambda: _native_change(env, "reset", change), "unavailable")
        assert env.db.total_changes == refused_changes
        assert _epoch(env) == env.session["epoch"]
        env.db.fault = None
        next_change = env.owner.begin_session_change(env.session["session_id"], "reset")
        assert next_change is not change
        result = env.store.reset(env.session["session_id"], _media_change=next_change)
        assert result.epoch == env.session["epoch"] + 1


@pytest.mark.asyncio
async def test_two_queued_original_writers_and_later_old_token_reset_only_once(tmp_path):
    async with _stack(tmp_path) as env:
        sid = env.session["session_id"]
        change = env.owner.begin_session_change(sid, "reset")
        assert env.owner.begin_session_change(sid, "reset") is change
        statements = []
        entered = [threading.Event(), threading.Event()]

        def reset(index):
            entered[index].set()
            return env.store.reset(sid, _media_change=change)

        env.db.set_trace_callback(statements.append)
        tasks = []
        try:
            async with _HeldSQL(env):
                tasks = [asyncio.create_task(asyncio.to_thread(reset, i)) for i in range(2)]
                for event in entered:
                    await _event(event)
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), _WAIT)
            successful = [result for result in results if isinstance(result, Session)]
            assert successful
            for result in results:
                if isinstance(result, MediaSessionError):
                    assert result.code == "conflict"
                else:
                    assert isinstance(result, Session)
                    assert result.epoch == env.session["epoch"] + 1
            assert _epoch(env) == env.session["epoch"] + 1
            updates = [s for s in statements if re.match(r"\s*UPDATE\s+sessions\b", s, re.I)]
            assert len(updates) == 1
            replay = env.store.reset(sid, _media_change=change)
            assert replay.model_dump() == successful[0].model_dump()
            assert replay is not successful[0]
            next_change = env.owner.begin_session_change(sid, "reset")
            assert next_change is not change
            latest = env.store.reset(sid, _media_change=next_change)
            assert latest.epoch == env.session["epoch"] + 2
            previous = env.store.reset(sid, _media_change=change)
            assert previous.epoch == env.session["epoch"] + 1
            assert _epoch(env) == latest.epoch
            assert len([s for s in statements if re.match(r"\s*UPDATE\s+sessions\b", s, re.I)]) == 2
            with env.store.lock:
                with env.owner.session_change_transaction(change, "reset") as view:
                    assert view.state == "applied"
                    assert isinstance(view.result, Session)
                    assert view.result.epoch == env.session["epoch"] + 1
                    assert view.result is not previous
                    assert type(view.session) is Session
                    assert view.session.session_id == sid
                    assert view.error_code is None
                    assert view.replayed is True
                    with pytest.raises((AttributeError, TypeError, ValueError)):
                        view.state = "pending"
        finally:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True),
                _WAIT,
            )
            env.db.set_trace_callback(None)


@pytest.mark.asyncio
async def test_actual_other_session_token_is_not_authority_for_this_reset(tmp_path):
    async with _stack(tmp_path, count=2) as (env, other):
        change = other.owner.begin_session_change(other.session["session_id"], "reset")
        async with _HeldSQL(env) as held:
            _assert_native_error(
                lambda: env.owner.validate_session_change(
                    change, env.session["session_id"], "reset"
                ),
                "conflict",
            )
            assert not held.retired.is_set() and not held.expired
        _assert_native_error(lambda: _native_change(env, "reset", change), "conflict")
        assert _epoch(env) == env.session["epoch"]
        assert _epoch(other) == other.session["epoch"]
        actual = other.store.reset(other.session["session_id"], _media_change=change)
        assert actual.epoch == other.session["epoch"] + 1


@pytest.mark.asyncio
async def test_delete_final_claim_refusal_preserves_committed_fence_and_history(tmp_path):
    async with _stack(tmp_path, allow_unknown_shutdown=True) as env:
        source, grant = await env.admit()
        async with env.connected(source, grant):
            assert (await env.put(env.put_header(source), b"abcdef", grant)).status_code == 200
            sealed, request = await env.seal(source, grant)
            _typed_receipt(sealed, "SealRequest", "Seal", request)
            upload = env.uploads[source["body"]["source_id"]]
            reader = upload.open_reader()
            original_entry = env.db.execute(
                "SELECT value FROM entries WHERE event_id=?", (env.owner_event["event_id"],)
            ).fetchone()[0]
            change = env.owner.begin_session_change(env.session["session_id"], "deleted")
            upload_close_returned = asyncio.Event()
            close_results = []
            loop = asyncio.get_running_loop()
            original_close = upload.close

            def observe_original_close():
                result = original_close()
                close_results.append(result)
                # Native retirement runs after the original event-channel join.
                # Observe this exact upload's real close, not incidental progress
                # from status/lookup requests or a fabricated cleanup result.
                loop.call_soon_threadsafe(upload_close_returned.set)
                return result

            close_observer = patch.object(upload, "close", observe_original_close)
            try:
                close_observer.start()
                assert reader.read(3) == b"abc"
                with upload._spool._mutex:
                    assert reader in upload._readers
                    reader_fd = reader._fd
                    original = os.fstat(reader_fd)
                    reader_identity = original.st_dev, original.st_ino
                with pytest.raises(MediaSessionError) as caught:
                    await asyncio.to_thread(_native_change, env, "deleted", change)
                assert caught.value.code in {"conflict", "unavailable"}
                assert _epoch(env) == env.session["epoch"]
                assert env.owner._sources[source["body"]["source_id"]].fenced.is_set()
                status = await env.current_status(source)
                assert status["body"]["fence"] != source["body"]["fence"]
                assert (
                    env.db.execute(
                        "SELECT value FROM entries WHERE event_id=?", (env.owner_event["event_id"],)
                    ).fetchone()[0]
                    == original_entry
                )
                assert (await env.lookup("source", source)).json()["value"] == source
                await asyncio.wait_for(upload_close_returned.wait(), _WAIT)
                assert close_results[0] is False
                assert env.spool.reserved_bytes == 6 and upload.deleted is False
                with upload._spool._mutex:
                    assert reader in upload._readers and reader._fd == reader_fd
                    retained = os.fstat(reader._fd)
                    assert (retained.st_dev, retained.st_ino) == reader_identity
                    assert retained.st_size == 6
                    # Logical revocation prevents reads while the SAME original
                    # descriptor and charge remain owned until reader.close().
                    with pytest.raises(MediaSpoolError) as revoked:
                        reader.read(3)
                    assert revoked.value.code == "expired"
                assert (
                    env.owner.begin_session_change(env.session["session_id"], "deleted") is change
                )
                _assert_native_error(
                    lambda: env.owner.begin_session_change(env.session["session_id"], "reset"),
                    "conflict",
                )
                # Re-observation cannot roll back the already committed fence.
                with pytest.raises(MediaSessionError) as repeated:
                    await asyncio.to_thread(_native_change, env, "deleted", change)
                assert repeated.value.code in {"conflict", "unavailable"}
                assert (await env.current_status(source))["body"]["fence"] == status["body"][
                    "fence"
                ]
            finally:
                try:
                    reader.close()
                finally:
                    close_observer.stop()


@pytest.mark.asyncio
async def test_actual_final_session_delete_releases_gate_only_after_row_is_gone(tmp_path):
    async with _stack(tmp_path) as env:
        sid = env.session["session_id"]
        change = env.owner.begin_session_change(sid, "deleted")
        assert await asyncio.to_thread(env.store.delete, sid, _media_change=change) is None
        assert _epoch(env) is None
        # A fresh conflicting command can now reach known-absent resolution,
        # instead of staying blocked by a gate whose delete has committed.
        new_change = env.owner.begin_session_change(sid, "reset")
        _assert_native_error(lambda: env.store.reset(sid, _media_change=new_change), "unknown_ref")


@pytest.mark.asyncio
async def test_cancelled_http_observer_retains_one_original_native_reset_worker(tmp_path):
    async with _stack(tmp_path) as env:
        native_reset = env.store.reset
        begin = env.owner.begin_session_change
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        reenrolled = threading.Event()
        calls, enrollments, outcomes, expired = [], [], [], []

        def observe_begin(sid, reason):
            change = begin(sid, reason)
            if sid == env.session["session_id"] and reason == "reset":
                enrollments.append(change)
                if len(enrollments) >= 2:
                    reenrolled.set()
            return change

        def hold_reset(sid, *, _media_change=None):
            calls.append(_media_change)
            entered.set()
            if not release.wait(_WAIT):
                expired.append(True)
            try:
                result = native_reset(sid, _media_change=_media_change)
                outcomes.append(result)
                return result
            finally:
                completed.set()

        original = replay = None
        with patch.object(env.owner, "begin_session_change", side_effect=observe_begin):
            with patch.object(env.store, "reset", side_effect=hold_reset):
                try:
                    original = asyncio.create_task(_api_change(env, "reset"))
                    await _event(entered)
                    assert len(calls) == 1 and calls[0] is not None
                    original.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await original
                    assert not completed.is_set() and _epoch(env) == env.session["epoch"]
                    replay = asyncio.create_task(_api_change(env, "reset"))
                    await _event(reenrolled)
                    assert all(change is calls[0] for change in enrollments)
                    _assert_native_error(
                        lambda: begin(env.session["session_id"], "deleted"), "conflict"
                    )
                    release.set()
                    await _event(completed)
                    response = await asyncio.wait_for(asyncio.shield(replay), _WAIT)
                    if response.status_code == 409:
                        _error(response, 409, "conflict")
                    else:
                        assert response.status_code == 200
                        assert response.json()["epoch"] == env.session["epoch"] + 1
                    assert len(calls) == 1 and len(outcomes) == 1
                    assert not expired
                    assert _epoch(env) == env.session["epoch"] + 1
                finally:
                    release.set()
                    await _event(completed)
                    if replay is not None:
                        await asyncio.wait_for(asyncio.shield(replay), _WAIT)
        result = native_reset(env.session["session_id"], _media_change=calls[0])
        assert result.epoch == env.session["epoch"] + 1
        assert _epoch(env) == result.epoch


class _StrSubclass(str):
    pass


@pytest.mark.asyncio
async def test_invalid_scalar_and_utf8_bounds_refuse_before_lifecycle_enrollment(tmp_path):
    async with _stack(tmp_path) as env:
        invalid = (
            None,
            True,
            False,
            1,
            1.0,
            [],
            {},
            _StrSubclass("session"),
            "",
            "x" * 129,
            "é" * 65,
            "\ud800",
        )
        for sid in invalid * 2:
            _assert_native_error(
                lambda sid=sid: env.owner.begin_session_change(sid, "reset"), "bad_packet"
            )
        for reason in (None, True, "delete", "RESET", "", _StrSubclass("reset")):
            _assert_native_error(
                lambda reason=reason: env.owner.begin_session_change(
                    env.session["session_id"], reason
                ),
                "bad_packet",
            )
        change = env.owner.begin_session_change(env.session["session_id"], "reset")
        assert (
            env.store.reset(env.session["session_id"], _media_change=change).epoch
            == env.session["epoch"] + 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ("reset", "deleted"))
async def test_more_than_sixteen_known_absent_changes_release_no_effect_gates(tmp_path, reason):
    async with _stack(tmp_path) as env:
        method = env.store.reset if reason == "reset" else env.store.delete
        for index in range(20):
            sid = f"known-absent-{index}"
            change = env.owner.begin_session_change(sid, reason)
            _assert_native_error(
                lambda sid=sid, change=change: method(sid, _media_change=change), "unknown_ref"
            )
        # _Ref's length/UTF8 rules do not create a UUID/hex/strip/path grammar.
        for sid in (" ", "relative/path", "control\nvalue", "a\x00b", "x" * 128, "é" * 64):
            change = env.owner.begin_session_change(sid, reason)
            _assert_native_error(
                lambda sid=sid, change=change: method(sid, _media_change=change), "unknown_ref"
            )
        actual = env.store.reset(env.session["session_id"])
        assert actual.epoch == env.session["epoch"] + 1


@pytest.mark.asyncio
async def test_pending_sixteen_slot_cap_reuse_conflict_and_known_no_effect_release(tmp_path):
    async with _stack(tmp_path) as env:
        pending = []
        try:
            for index in range(16):
                sid = f"pending-absent-{index}"
                pending.append((sid, env.owner.begin_session_change(sid, "reset")))
            sid, first = pending[0]
            assert env.owner.begin_session_change(sid, "reset") is first
            _assert_native_error(lambda: env.owner.begin_session_change(sid, "deleted"), "conflict")
            _assert_native_error(
                lambda: env.owner.begin_session_change("seventeenth", "reset"), "capacity"
            )
            _assert_native_error(lambda: env.store.reset(sid, _media_change=first), "unknown_ref")
            pending.pop(0)
            admitted = env.owner.begin_session_change("seventeenth", "reset")
            pending.append(("seventeenth", admitted))
            _assert_native_error(
                lambda: env.store.reset("seventeenth", _media_change=admitted), "unknown_ref"
            )
            pending.pop()
        finally:
            for sid, change in pending:
                _assert_native_error(
                    lambda sid=sid, change=change: env.store.reset(sid, _media_change=change),
                    "unknown_ref",
                )
        change = env.owner.begin_session_change(env.session["session_id"], "reset")
        assert (
            env.store.reset(env.session["session_id"], _media_change=change).epoch
            == env.session["epoch"] + 1
        )
