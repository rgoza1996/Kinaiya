"""Original native task, reader, exchange and publication custody for local PCM.

This optional component implements one bounded passthrough packet, not decoder
or capture availability. It installs no public route and does not start/resume
the general media pipeline. A real Session source, sealed spool, owner channel,
task anchor and selected model lease are required even with an offline HTTPX
transport. Public references and reconstructed DTOs confer no native authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import struct
import threading
from dataclasses import dataclass, field

from packages.contracts import media as m
from packages.contracts.models import AppendRequest
from packages.providers.llama_media import (
    LlamaExchangeRegistration,
    LlamaExchangeSettlementBundle,
    LlamaMediaPart,
    LocalBodyClose,
    LocalBodyOwner,
    LocalCloseObservation,
    LocalExchangeCleanupV2,
    NativeLlamaMediaProvider,
)
from services.converse import native_media as converse
from services.converse.model_settings import ConverseModelLease, ConverseModelSettings
from services.decisions import native_media as decisions
from services.decisions.media_classifier import MediaRangeBinding, project_media_state
from services.decisions.model_settings import DecisionsModelLease, DecisionsModelSettings
from services.session.media_clients import refuse
from services.session.media_owner import MediaSessionOwner

_ATTEMPTS = 64
_HOLD = 1_048_576
_RECORDS = 32
_PACKET = 32_044  # One second of canonical PCM16 mono16k WAV, including header.
_PROTOCOL = hashlib.sha256(b"kinaiya.native.pcm16-mono16k-one-packet.v2").hexdigest()
_DDL = (
    "CREATE TABLE native_media_v2_attempts(key TEXT PRIMARY KEY,request BLOB NOT NULL,source_id TEXT NOT NULL,app_instance TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,state TEXT NOT NULL CHECK(state IN('active','settled','unknown')),event_id TEXT)",
    "CREATE TABLE native_media_v2_records(id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,kind TEXT NOT NULL,value BLOB NOT NULL)",
)
_TYPES = {
    ("MediaRange" if kind is m.MediaRange else kind.__name__.removeprefix("Media")): kind
    for kind in (
        m.MediaConsumer,
        m.MediaBlob,
        m.MediaManifestPage,
        m.MediaRange,
        m.MediaChildOwner,
        m.MediaWorkerCleanup,
        m.MediaLocalBodyOwnerV2,
        m.MediaLocalExchangeOwnerV2,
        m.MediaLocalBodyCloseV2,
        m.MediaLocalCloseObservationV2,
        m.MediaLocalExchangeCleanupV2,
    )
}


def _json(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _value(value):
    return json.loads(m.media_bytes(value))


def _id():
    return secrets.token_hex(16)


@dataclass(eq=False, slots=True)
class _Attempt:
    key: str
    request: bytes
    source: object
    reference: m.MediaNativeRef
    fence: m.MediaFence
    principal: object
    lease: object
    pin: m.MediaCanonicalModelPinV2
    provider: NativeLlamaMediaProvider
    loop: asyncio.AbstractEventLoop
    deadline: float
    owner_event: object
    system_prompt: str | None
    caller: object
    caller_stamp: object
    operation_id: str = field(default_factory=_id)
    instance: str = field(default_factory=_id)
    task_id: str = field(default_factory=_id)
    transport_id: str = field(default_factory=_id)
    body_id: str = field(default_factory=_id)
    wire_reader_id: str = field(default_factory=_id)
    publication_id: str = field(default_factory=_id)
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    fenced: threading.Event = field(default_factory=threading.Event)
    worker: asyncio.Task | None = None
    coroutine: object = None
    reconciliation: asyncio.Task | None = None
    cancel_baseline: int = 0
    reader: object = None
    reader_open_started: bool = False
    reader_open_finished: bool = False
    reader_closed: bool = False
    registration: LlamaExchangeRegistration | None = None
    worker_owner: m.MediaNativeRef | None = None
    worker_binding: str | None = None
    worker_cleanup: m.MediaNativeRef | None = None
    prepared: object = None
    staged: object = None
    state: object = None
    consumer: object = None
    range_ref: object = None
    blob_ref: object = None
    request_sha256: str | None = None
    transport_closed: bool = False
    wire_retired: bool = False
    publication_finished: bool = False
    publication_unknown: bool = False
    event: object = None
    outcome: str = "not_started"
    settled: bool = False
    unknown: bool = False
    snapshots: list = field(default_factory=list)


class _Bridge:
    """Private callbacks bound to the same original native task and request."""

    def __init__(self, owner, attempt):
        self.owner, self.attempt = owner, attempt

    def _original(self, registration=None):
        a = self.attempt
        if (
            self.owner._attempts.get(a.key) is not a
            or asyncio.current_task() is not a.worker
            or a.worker.get_coro() is not a.coroutine
            or registration is not None
            and registration is not a.registration
        ):
            refuse("denied")
        return a

    def register_exchange(self, *, prepared, mode, model):
        a = self._original()
        self.owner._guard(a)
        if (
            a.registration is not None
            or a.prepared is None
            or prepared.raw_body != a.prepared.raw_body
            or prepared.provenance is not a.prepared.provenance
            or (mode, model) != (a.provider.mode, a.provider.model)
        ):
            refuse("conflict")
        # The provider rebuilt and validated the same original encoded wire.
        # Retain its exact validated object for the following private callbacks.
        a.prepared = prepared
        body = LocalBodyOwner(
            v=1,
            exchange_id=_id(),
            body_owner_id=a.body_id,
            source=a.reference,
            consumer=a.consumer,
            request_sha256=prepared.request_sha256,
            body_bytes=len(prepared.raw_body),
            body_sha256=prepared.request_sha256,
            reader_owner_ids=(a.wire_reader_id,),
            body={"kind": "wire_buffer"},
        )
        a.request_sha256 = prepared.request_sha256
        binding, child = a.worker_binding, a.worker_owner
        if binding is None or child is None:
            refuse()
        with self.owner.session_owner._boundary():
            body_ref = self.owner._record(
                a,
                m.MediaLocalBodyOwnerV2(
                    v=1,
                    exchange_id=body.exchange_id,
                    body_owner_id=a.body_id,
                    source=a.reference,
                    consumer=a.consumer,
                    request_sha256=prepared.request_sha256,
                    body_bytes=body.body_bytes,
                    body_sha256=body.body_sha256,
                    reader_owner_ids=[a.wire_reader_id],
                    body=m.MediaWireBodyStorageV2(kind="wire_buffer"),
                ),
            )
            exchange = self.owner._record(
                a,
                m.MediaLocalExchangeOwnerV2(
                    v=1,
                    exchange_id=body.exchange_id,
                    source=a.reference,
                    consumer=a.consumer,
                    child_owner=child,
                    local_task_id=a.task_id,
                    transport_owner_id=a.transport_id,
                    body_owner_id=a.body_id,
                    reader_owner_ids=[a.wire_reader_id],
                    publication_owner_ids=[a.publication_id],
                    request_sha256=prepared.request_sha256,
                    model_pin=a.pin,
                    protocol_sha256=_PROTOCOL,
                ),
            )
        a.registration = LlamaExchangeRegistration(
            operation_id=a.operation_id,
            child_instance=a.instance,
            binding_sha256=binding,
            original_owner=child,
            exchange_owner=exchange,
            body_owner_ref=body_ref,
            body_owner=body,
            local_task_id=a.task_id,
            transport_owner_id=a.transport_id,
            publication_owner_ids=(a.publication_id,),
        )
        return a.registration

    def deliver_candidate(self, *, registration, candidate):
        a = self._original(registration)
        self.owner._guard(a)
        if a.staged is not None:
            refuse("conflict")
        # No journal, routing, Agent call or positive publication observation.
        a.staged = candidate

    def settle_exchange(
        self,
        *,
        registration,
        prepared,
        outcome,
        transport_sole_closed,
        wire_buffer_retired,
        candidate,
    ):
        a = self._original(registration)
        if prepared is not a.prepared or a.snapshots:
            refuse("conflict")
        # These observations arrive only from the retained actual provider call,
        # after its sole original _TrackedOwnedHTTPStream close. No public route
        # accepts caller booleans or a reconstructed registration as this bridge.
        a.transport_closed = transport_sole_closed is True
        a.wire_retired = wire_buffer_retired is True and a.transport_closed
        return self.owner._snapshot(a, joined=False)


class NativeBufferedMediaOwner:
    """Optional Session extension; at most two active,64 lifetime originals.

    The sealed one-packet WAV lane performs no subprocess, decompression,
    resampling, cloud call or command admission. General media start/resume
    remains unavailable. Installing this component is a native composition
    decision, never a request flag or an assertion that a model is qualified.
    """

    def __init__(self, session_owner):
        if type(session_owner) is not MediaSessionOwner:
            refuse("denied")
        self.session_owner = session_owner
        self._attempts = {}
        self._publication_lock = asyncio.Lock()
        self._closed = False
        self._unknown = False
        with session_owner._boundary():
            if session_owner._native_media is not None:
                refuse("conflict")
            if session_owner._metadata_hold + _ATTEMPTS * _HOLD > 1_073_741_824:
                refuse("capacity")
            session_owner._metadata_hold += _ATTEMPTS * _HOLD
            for ddl in _DDL:
                name = ddl.split("(", 1)[0].split()[-1]
                row = session_owner._sql(
                    "SELECT sql FROM sqlite_schema WHERE name=?", (name,)
                ).fetchone()
                if row is None:
                    session_owner._sql(ddl)
                elif row[0] != ddl:
                    refuse()
            rows = session_owner._sql("SELECT * FROM native_media_v2_attempts").fetchall()
            if len(rows) > _ATTEMPTS:
                refuse()
            # A new incarnation cannot join a missing original task/reader or
            # turn an absent response into proof of non-publication.
            session_owner._sql(
                "UPDATE native_media_v2_attempts SET state='unknown' WHERE state!='settled'"
            )
            self._unknown = any(row["state"] != "settled" for row in rows)
            self._audit()
        session_owner._native_media = self

    def _audit(self):
        owner = self.session_owner
        token = owner._local.token
        rows = owner._sql("SELECT * FROM native_media_v2_attempts").fetchall()
        if (
            len(rows) > _ATTEMPTS
            or owner._sql(
                "SELECT 1 FROM native_media_v2_records r LEFT JOIN native_media_v2_attempts a ON a.operation_id=r.operation_id WHERE a.key IS NULL LIMIT 1"
            ).fetchone()
        ):
            refuse()
        for row in rows:
            raw = bytes(row["request"])
            if len(raw) > 65_536:
                refuse()
            m._lexical_bounds(raw.decode("utf-8"))
            request = json.loads(
                raw,
                object_pairs_hook=m._pairs,
                parse_int=m._integer,
                parse_float=m._reject_number,
                parse_constant=m._reject_number,
            )
            if (
                type(request) is not dict
                or request.keys()
                != {
                    "v",
                    "key",
                    "source",
                    "fence",
                    "model_pin",
                    "timeout_us",
                    "protocol_sha256",
                    "caller_stamp",
                    "system_prompt_sha256",
                }
                or _json(request) != raw
            ):
                refuse()
            if (
                type(request["v"]) is not int
                or request["v"] != 2
                or request["key"] != row["key"]
                or (
                    type(request["timeout_us"]) is not int
                    or not 1 <= request["timeout_us"] <= 120_000_000
                )
                or request["protocol_sha256"] != _PROTOCOL
            ):
                refuse()
            pin = m.media_json(_json(request["model_pin"]), m.MediaCanonicalModelPinV2)
            source_ref = m.media_json(_json(request["source"]), m.MediaNativeRef)
            source = owner._lookup_ref(token, source_ref, "source").body
            fence = m.media_json(_json(request["fence"]), m.MediaFence)
            if source.source_id != row["source_id"] or source.fence != fence:
                refuse()
            for identity in (row["operation_id"], row["app_instance"]):
                if type(identity) is not str or re.fullmatch("[a-f0-9]{32}", identity) is None:
                    refuse()
            stamp = request["caller_stamp"]
            if pin.role == "converse":
                if (
                    type(stamp) is not list
                    or len(stamp) != 2
                    or any(type(n) is not int or not 0 <= n <= m.MAX_MEDIA_INTEGER for n in stamp)
                ):
                    refuse()
            elif stamp is not None:
                refuse()
            records = owner._sql(
                "SELECT id,kind,value FROM native_media_v2_records WHERE operation_id=?",
                (row["operation_id"],),
            ).fetchall()
            if len(records) > _RECORDS or sum(len(record["value"]) for record in records) > 524_288:
                refuse()
            parsed = {}
            for record in records:
                kind = _TYPES.get(record["kind"])
                if kind is None:
                    refuse()
                receipt = m.media_json(bytes(record["value"]), m.MediaReceipt[kind])
                if (
                    receipt.id != record["id"]
                    or receipt.request_sha256 != hashlib.sha256(raw).hexdigest()
                    or m.media_bytes(receipt) != bytes(record["value"])
                ):
                    refuse()
                parsed[receipt.id] = receipt

            def resolve(reference, kind, records=parsed):
                found = records.get(reference.receipt_id)
                if (
                    reference.backend_id != owner.backend_id
                    or found is None
                    or (type(found.body) is not kind or found.sha256 != reference.sha256)
                ):
                    refuse()
                return found.body

            children = [
                receipt.body
                for receipt in parsed.values()
                if type(receipt.body) is m.MediaChildOwner
            ]
            if len(children) != 1:
                refuse()
            child = children[0]
            binding = hashlib.sha256(b"kinaiya.native.media-worker.v2\0" + raw).hexdigest()
            if (
                child.source != source_ref
                or child.operation_id != row["operation_id"]
                or child.consumer is not None
                or child.backend_id != owner.backend_id
                or (
                    type(child.peer) is not m.MediaNativeTaskPeer
                    or child.peer.backend_instance != row["app_instance"]
                    or child.peer.request_sha256 != hashlib.sha256(raw).hexdigest()
                    or child.binding_sha256 != binding
                    or child.kind
                    != ("converse_provider" if pin.role == "converse" else "decisions_provider")
                )
            ):
                refuse()
            positives = []
            for receipt in parsed.values():
                body = receipt.body
                if type(body) is m.MediaWorkerCleanup:
                    original = resolve(body.original_owner, m.MediaChildOwner)
                    if (
                        original != child
                        or body.operation_id != child.operation_id
                        or body.child_instance != child.instance
                        or body.binding_sha256 != binding
                        or body.scope != "native_task"
                    ):
                        refuse()
                    positives.append(body.state == "settled")
                elif type(body) is m.MediaLocalExchangeOwnerV2:
                    if (
                        resolve(body.child_owner, m.MediaChildOwner) != child
                        or body.source != source_ref
                        or body.model_pin != pin
                        or body.protocol_sha256 != _PROTOCOL
                        or body.local_task_id != child.peer.task_id
                    ):
                        refuse()
                elif type(body) is m.MediaLocalExchangeCleanupV2:
                    exchange = resolve(body.exchange_owner, m.MediaLocalExchangeOwnerV2)
                    original = resolve(body.original_owner, m.MediaChildOwner)
                    encoded = resolve(body.body_owner, m.MediaLocalBodyOwnerV2)
                    close = resolve(body.body_observation, m.MediaLocalBodyCloseV2)
                    observations = [
                        resolve(ref, m.MediaLocalCloseObservationV2) for ref in body.observations
                    ]
                    expected = {
                        ("local_task", exchange.local_task_id): "task_join",
                        ("transport", exchange.transport_owner_id): "sole_transport_close",
                        **{("reader", rid): "reader_join" for rid in exchange.reader_owner_ids},
                        **{
                            ("publication", pid): "publication_settlement"
                            for pid in exchange.publication_owner_ids
                        },
                    }
                    if (
                        original != child
                        or body.operation_id != child.operation_id
                        or body.child_instance != child.instance
                        or body.binding_sha256 != binding
                        or close.body_owner != body.body_owner
                        or encoded.body_owner_id != exchange.body_owner_id
                        or encoded.exchange_id != exchange.exchange_id
                        or (
                            encoded.request_sha256 != exchange.request_sha256
                            or encoded.consumer != exchange.consumer
                            or encoded.source != source_ref
                            or encoded.reader_owner_ids != exchange.reader_owner_ids
                        )
                        or set((o.component, o.owner_id) for o in observations) != set(expected)
                        or len(observations) != len(expected)
                    ):
                        refuse()
                    for observation in [close, *observations]:
                        if (
                            observation.request_sha256 != exchange.request_sha256
                            or observation.binding_sha256 != binding
                        ):
                            refuse()
                    if any(o.exchange_owner != body.exchange_owner for o in observations):
                        refuse()
                    by_owner = {
                        (o.component, o.owner_id): o.state == "settled"
                        and o.proof == expected[(o.component, o.owner_id)]
                        for o in observations
                    }
                    if (
                        body.local_task_joined != by_owner[("local_task", exchange.local_task_id)]
                        or body.transport_sole_closed
                        != by_owner[("transport", exchange.transport_owner_id)]
                        or body.all_readers_settled
                        != all(by_owner[("reader", rid)] for rid in exchange.reader_owner_ids)
                        or body.publications_settled
                        != all(
                            by_owner[("publication", pid)] for pid in exchange.publication_owner_ids
                        )
                        or body.body_settled
                        != (close.state == "settled" and close.proof == "wire_buffer_retired")
                    ):
                        refuse()
            if row["state"] == "settled" and positives != [True]:
                refuse()
            if row["event_id"] is not None:
                event = owner.store.entry(row["event_id"])
                if (
                    (event.session_id, event.thread_id, event.epoch)
                    != (fence.session.session_id, fence.session.thread_id, fence.session.epoch)
                    or event.kind
                    != ("conversation.converse" if pin.role == "converse" else "routing.decided")
                    or event.idempotency_key != f"native-media-v2:{row['operation_id']}"
                ):
                    refuse()

    def _record(self, a, body):
        owner = self.session_owner
        short = (
            "MediaRange"
            if type(body) is m.MediaRange
            else type(body).__name__.removeprefix("Media")
        )
        if _TYPES.get(short) is not type(body) or not owner.store.db.in_transaction:
            refuse()
        fields = {
            "v": 1,
            "id": _id(),
            "key": f"native-v2:{a.operation_id}:{_id()}",
            "request_sha256": hashlib.sha256(a.request).hexdigest(),
            "body": body,
        }
        receipt = m.MediaReceipt[type(body)](**fields, sha256=m.media_digest(f"R<{short}>", fields))
        raw = m.media_bytes(receipt)
        count, size = owner._sql(
            "SELECT COUNT(*),COALESCE(SUM(length(value)),0) FROM native_media_v2_records WHERE operation_id=?",
            (a.operation_id,),
        ).fetchone()
        if count >= _RECORDS or size + len(raw) > 524_288:
            refuse("capacity")
        owner._sql(
            "INSERT INTO native_media_v2_records VALUES(?,?,?,?)",
            (receipt.id, a.operation_id, short, raw),
        )
        return m.MediaNativeRef(
            backend_id=owner.backend_id, receipt_id=receipt.id, sha256=receipt.sha256
        )

    def _guard(self, a, token=None):
        owner = self.session_owner
        if (
            self._closed
            or self._unknown
            or a.unknown
            or a.fenced.is_set()
            or self._attempts.get(a.key) is not a
            or a.worker is None
            or a.worker.get_coro() is not a.coroutine
            or a.worker.cancelling() > a.cancel_baseline
        ):
            raise asyncio.CancelledError
        if a.loop.time() >= a.deadline:
            raise TimeoutError("Native media deadline expired")
        if a.lease.current() is not True or m.canonical_media_model_pin_v2(a.lease.pin) != a.pin:
            refuse("stale")
        if a.caller.native_media_owner is not self or a.caller.model_settings is not a.lease.owner:
            refuse("stale")
        if a.pin.role == "converse":
            if (
                not a.caller.admission_current(a.caller_stamp)
                or a.caller.personality != a.system_prompt
            ):
                refuse("stale")
        elif a.caller.enabled is not True:
            refuse("stale")
        if token is None:
            with owner._transaction(a.principal, {"owner"}) as original:
                self._guard_source(a, original)
        else:
            self._guard_source(a, token)

    def _guard_source(self, a, token):
        owner = self.session_owner
        owner._principal(a.principal, {"owner"})
        _, source = owner._source_guard(token, a.reference, a.fence, a.principal)
        if source is not a.source:
            refuse("stale")

    def _admit(self, reference, fence, principal, lease, key, timeout, system_prompt, caller):
        from services.converse.runtime import ConverseRuntime
        from services.decisions.runtime import DecisionsWorker

        owner = self.session_owner
        if self._closed or self._unknown:
            refuse()
        if type(key) is not str or not 1 <= len(key.encode("utf-8")) <= 128:
            refuse("bad_packet")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            refuse("bound")
        if type(lease) not in (ConverseModelLease, DecisionsModelLease):
            refuse("denied")
        if type(lease.owner) is not (
            ConverseModelSettings if type(lease) is ConverseModelLease else DecisionsModelSettings
        ):
            refuse("denied")
        if system_prompt is not None and (
            type(system_prompt) is not str or not 1 <= len(system_prompt.encode("utf-8")) <= 16384
        ):
            refuse("bound")
        provider = lease.provider
        role = "converse" if type(lease) is ConverseModelLease else "decisions"
        if type(caller) is not (ConverseRuntime if role == "converse" else DecisionsWorker) or (
            caller.native_media_owner is not self or caller.model_settings is not lease.owner
        ):
            refuse("denied")
        caller_stamp = caller.admission() if role == "converse" else None
        if role == "decisions" and caller.enabled is not True:
            refuse("stale")
        if (
            type(provider) is not NativeLlamaMediaProvider
            or provider.mode != ("converse_chat" if role == "converse" else "decisions_systemone")
            or lease.current() is not True
            or lease.pin.role != role
            or lease.pin.local is not True
            or lease.pin.provider != "native_llama_media"
            or lease.pin.model != provider.model
        ):
            refuse("unsupported")
        pin = m.canonical_media_model_pin_v2(lease.pin)
        reference = m.media_json(m.media_bytes(reference), m.MediaNativeRef)
        fence = m.media_json(m.media_bytes(fence), m.MediaFence)
        request = _json(
            {
                "v": 2,
                "key": key,
                "source": _value(reference),
                "fence": _value(fence),
                "model_pin": _value(pin),
                "timeout_us": int(timeout * 1_000_000),
                "protocol_sha256": _PROTOCOL,
                "caller_stamp": caller_stamp,
                "system_prompt_sha256": None
                if system_prompt is None
                else hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
            }
        )
        with owner._transaction(principal, {"owner"}) as token:
            row, source = owner._source_guard(token, reference, fence, principal)
            if source.sealed_result is None or source.admission.scope != "local_only":
                refuse("unsupported")
            if source.admission.kind != "recording" or source.admission.purpose not in {
                "analyze",
                "observe",
            }:
                refuse("unsupported")
            old = owner._sql(
                "SELECT * FROM native_media_v2_attempts WHERE key=?", (key,)
            ).fetchone()
            if old is not None:
                if bytes(old["request"]) != request:
                    refuse("conflict")
                a = self._attempts.get(key)
                if a is None:
                    refuse()  # Historical refs cannot reconstruct original authority.
                return a, False
            if (
                owner._sql("SELECT COUNT(*) FROM native_media_v2_attempts").fetchone()[0]
                >= _ATTEMPTS
            ):
                refuse("capacity")
            if (
                sum(not a.settled for a in self._attempts.values()) >= 2
                or owner._sql(
                    "SELECT 1 FROM native_media_v2_attempts WHERE source_id=? AND state='unknown'",
                    (source.admission.source_id,),
                ).fetchone()
            ):
                refuse("capacity")
            anchor = owner._lookup_ref(token, source.admission.anchor, "anchor").body
            if anchor.owner_objective.kind != "owner_text":
                refuse("unsupported")
            event = owner._owner_event(anchor.owner_objective.event_ref, source.admission.session)
            loop = asyncio.get_running_loop()
            a = _Attempt(
                key,
                request,
                source,
                reference,
                fence,
                principal,
                lease,
                pin,
                provider,
                loop,
                loop.time() + timeout,
                event,
                system_prompt,
                caller,
                caller_stamp,
            )
            a.coroutine = self._worker(a)
            a.worker = asyncio.create_task(a.coroutine)
            # The real task is held before source/reader/HTTP/publication use.
            self._attempts[key] = a
            a.worker.add_done_callback(lambda task: self._done(a, task))
            owner._sql(
                "INSERT INTO native_media_v2_attempts VALUES(?,?,?,?,?,'active',NULL)",
                (key, request, row["source_id"], owner.app_instance, a.operation_id),
            )
            a.worker_binding = hashlib.sha256(
                b"kinaiya.native.media-worker.v2\0" + request
            ).hexdigest()
            a.worker_owner = self._record(
                a,
                m.MediaChildOwner(
                    v=1,
                    kind="converse_provider" if role == "converse" else "decisions_provider",
                    instance=a.instance,
                    operation_id=a.operation_id,
                    source=a.reference,
                    consumer=None,
                    binding_sha256=a.worker_binding,
                    backend_id=owner.backend_id,
                    peer=m.MediaNativeTaskPeer(
                        kind="native_task",
                        backend_instance=owner.app_instance,
                        task_id=a.task_id,
                        request_sha256=hashlib.sha256(request).hexdigest(),
                    ),
                ),
            )
        a.gate.set()
        return a, True

    async def run(
        self,
        reference,
        fence,
        principal,
        lease,
        *,
        key,
        timeout=30.0,
        system_prompt=None,
        caller=None,
    ):
        """Observe the same original; cancellation fences before returning."""
        try:
            a, fresh = self._admit(
                reference, fence, principal, lease, key, timeout, system_prompt, caller
            )
        except BaseException:
            original = self._attempts.get(key) if type(key) is str else None
            if original is not None and original.lease is lease:
                original.unknown = True
                self._fence(original)
                original.gate.set()
            else:
                await lease.close()
            raise
        if not fresh and lease is not a.lease:
            await lease.close()
        observer = asyncio.current_task()
        baseline = observer.cancelling()
        try:
            done, _ = await asyncio.wait({a.worker}, timeout=max(0, a.deadline - a.loop.time()))
            if not done:
                self._fence(a)
                raise TimeoutError("Native media deadline expired")
            if observer.cancelling() > baseline:
                self._fence(a)
                raise asyncio.CancelledError
            return a.worker.result()
        except asyncio.CancelledError:
            self._fence(a)
            raise

    @staticmethod
    def _fence(a):
        a.fenced.set()
        if a.worker is not None and not a.worker.done():
            a.worker.cancel()

    async def _read_packet(self, a):
        owner = self.session_owner
        self._guard(a)

        # The extension's promised original is already durable before this FD.
        # Native open/read/close retain their actual threads through cancellation.
        def open_original():
            a.reader = a.source.upload.open_reader()
            a.reader_open_finished = True

        a.reader_open_started = True
        await owner.run_native(open_original)
        self._guard(a)
        chunks, size = [], 0
        while True:
            body = await owner.run_native(a.reader.read, _PACKET + 1)
            self._guard(a)
            if not body:
                break
            size += len(body)
            if size > _PACKET:
                refuse("bound")
            chunks.append(body)
        raw = b"".join(chunks)
        if len(raw) < 46:
            refuse("unsupported")
        fields = struct.unpack("<4sI4s4sIHHIIHH4sI", raw[:44])
        samples = (len(raw) - 44) // 2
        if (
            fields
            != (
                b"RIFF",
                len(raw) - 8,
                b"WAVE",
                b"fmt ",
                16,
                1,
                1,
                16000,
                32000,
                2,
                16,
                b"data",
                samples * 2,
            )
            or len(raw) != 44 + samples * 2
            or not 1 <= samples <= 16000
        ):
            refuse("unsupported")
        if a.source.sealed_result != (len(raw), hashlib.sha256(raw).hexdigest()):
            refuse("conflict")
        fmt = m.MediaAudioFormat(
            kind="audio", container="wav", codec="pcm_s16le", rate=16000, channels=1
        )
        if m.media_bytes(fmt) not in {m.media_bytes(item) for item in owner.profile.formats}:
            refuse("unsupported")
        divisor = math.gcd(samples, 16000)
        start, end = m.MediaQ(num=0, den=1), m.MediaQ(num=samples // divisor, den=16000 // divisor)
        track = _id()
        pts = m.MediaSourcePTS(
            pts=0,
            time_base_num=1,
            time_base_den=16000,
            sample_start=0,
            sample_count=samples,
            frame_index=None,
        )
        tracks = [
            m.MediaTrackRange(
                track_id=track,
                first_track_seq=1,
                through_track_seq=1,
                first_sample=0,
                sample_count=samples,
                first_frame=None,
                frame_count=None,
            )
        ]
        with owner._transaction(a.principal, {"owner"}) as token:
            owner._source_guard(token, a.reference, a.fence, a.principal)
            row = owner._sql(
                "SELECT r.* FROM media_receipts r JOIN media_operations o ON o.result_receipt_id=r.receipt_id WHERE o.kind='seal' AND o.source_id=? AND o.phase='committed' ORDER BY r.receipt_id LIMIT 1",
                (a.source.admission.source_id,),
            ).fetchone()
            if row is None:
                refuse()
            seal_body = owner._read_receipt(row).body
            if (seal_body.bytes, seal_body.source_sha256) != a.source.sealed_result:
                refuse()
            a.blob_ref = self._record(
                a,
                m.MediaBlob(
                    v=1,
                    source=a.reference,
                    generation=a.fence.generation,
                    track_id=track,
                    source_seq=1,
                    track_seq=1,
                    pts=pts,
                    start=start,
                    end=end,
                    format_sha256=m.media_digest("AudioFormat", fmt),
                    body_bytes=len(raw),
                    body_sha256=hashlib.sha256(raw).hexdigest(),
                    raw_state="buffered",
                ),
            )
            manifest = _id()
            page = self._record(
                a,
                m.MediaManifestPage(
                    v=1,
                    manifest_id=manifest,
                    source=a.reference,
                    generation=a.fence.generation,
                    index=0,
                    previous_page=None,
                    units=[
                        m.MediaUnit(
                            unit_id=_id(),
                            start=start,
                            end=end,
                            first_source_seq=1,
                            through_source_seq=1,
                            tracks=tracks,
                        )
                    ],
                ),
            )
            a.range_ref = self._record(
                a,
                m.MediaRange(
                    v=1,
                    range_id=_id(),
                    source=a.reference,
                    source_equivalence_id=seal_body.source_equivalence_id,
                    source_revision=a.fence.source_revision,
                    generation=a.fence.generation,
                    manifest_id=manifest,
                    through_page=page,
                    tracks=tracks,
                    start=start,
                    end=end,
                    first_source_seq=1,
                    through_source_seq=1,
                ),
            )
            a.consumer = m.MediaConsumerRef(consumer_id=_id(), generation=1)
            consumer = {
                "v": 1,
                "consumer": a.consumer,
                "source": a.reference,
                "fence": a.fence,
                "role": a.pin.role,
                "instance": a.instance,
                "capability_sha256": None,
                "decoder_profile_sha256": None,
                "scope": "local_only",
                "disclosure": None,
                "anchor": a.source.admission.anchor,
                "task_admission": None,
                "owner_intent": None,
                "ranges": [a.range_ref],
            }
            self._record(
                a, m.MediaConsumer(**consumer, permit_sha256=m.media_digest("Consumer", consumer))
            )
        a.state = project_media_state(
            purpose=a.source.admission.purpose,
            allowed_operation="evidence_only",
            source=a.reference,
            fence=a.fence,
            consumer=a.consumer,
            ranges=[
                MediaRangeBinding(
                    a.range_ref, start, end, 1, 1, pts=pts, track_id=track, kind="audio"
                )
            ],
            anchor=a.source.admission.anchor,
            context=[{"role": "user", "text": a.owner_event.payload["text"]}],
        )
        return LlamaMediaPart(
            kind="audio",
            data=raw,
            mime_type="audio/wav",
            source=a.reference,
            track_id=track,
            source_seq=1,
            through_source_seq=1,
            pts=pts,
            start=start,
            end=end,
            body_sha256=hashlib.sha256(raw).hexdigest(),
        )

    async def _worker(self, a):
        try:
            await a.gate.wait()
            a.cancel_baseline = a.worker.cancelling()
            async with asyncio.timeout_at(a.deadline):
                part = await self._read_packet(a)
                self._guard(a)
                a.prepared = (
                    converse.prepare_request(
                        a.provider.model, part, a.owner_event.payload["text"], a.system_prompt
                    )
                    if a.pin.role == "converse"
                    else decisions.prepare_request(a.provider.model, part, a.state)
                )
                a.outcome = "failed"
                result = await a.provider.complete_media(
                    a.prepared,
                    current=lambda: self._current(a),
                    exchange_owner_bridge=_Bridge(self, a),
                )
                self._guard(a)
                if a.staged is not result:
                    refuse()
                event = await self._publish(a, result)
                self._guard(a)
                a.outcome = "completed"
                return event
        except asyncio.CancelledError:
            a.outcome = "cancelled"
            raise
        finally:
            # Joining the actual worker is still required after these flags.
            a.publication_finished = True
            a.staged = a.prepared = None
            if a.reader is not None:
                try:
                    await self.session_owner.run_native(a.reader.close)
                    a.reader_closed = True
                except BaseException:
                    a.unknown = True
                    raise
            else:
                a.reader_closed = not a.reader_open_started
                a.unknown = a.unknown or a.reader_open_started

    def _current(self, a):
        self._guard(a)
        return True

    async def _publish(self, a, result):
        provenance = {
            "v": 2,
            "source": _value(a.reference),
            "fence": _value(a.fence),
            "consumer": _value(a.consumer),
            "range": _value(a.range_ref),
            "blob": _value(a.blob_ref),
            "exchange": _value(a.registration.exchange_owner),
            "model_pin": _value(a.pin),
            "request_sha256": result.request_sha256,
            "response_sha256": result.response_sha256,
            "allowed_operation": "evidence_only",
        }
        payload = (
            converse.reply_payload(result, provenance)
            if a.pin.role == "converse"
            else decisions.routing_payload(result, a.state, provenance, a.owner_event, a.key)
        )
        request = AppendRequest(
            **_value(a.fence.session),
            kind="conversation.converse" if a.pin.role == "converse" else "routing.decided",
            payload=payload,
            causes=[a.owner_event.event_id],
            idempotency_key=f"native-media-v2:{a.operation_id}",
        )
        async with self._publication_lock:
            # All native awaits precede the commit guard. Holding the original
            # source fence lock makes stop/reset/disconnect linearize before
            # this commit or after its retained receipt, never between them.
            self._guard(a)
            owner = self.session_owner
            with owner._transaction(a.principal, {"owner"}) as token:
                with owner._mutex, a.source.fenced.publication_lock:
                    self._guard(a, token)
                    owner._source_guard(token, a.reference, a.fence, a.principal)
                    try:
                        event = owner.store._append_entry_locked(request, a.pin.role)
                        owner._sql(
                            "UPDATE native_media_v2_attempts SET event_id=? WHERE key=?",
                            (event.event_id, a.key),
                        )
                    except BaseException:
                        a.publication_unknown = True
                        raise
            a.event = event
            return event

    def _snapshot(self, a, *, joined):
        r = a.registration
        if r is None or len(a.snapshots) >= 2:
            refuse()
        task_joined = bool(joined and a.worker.done() and a.worker.get_coro() is a.coroutine)
        publications = task_joined and a.publication_finished and not a.publication_unknown
        body_settled = a.wire_retired and a.transport_closed
        all_readers = body_settled  # The original encoded-wire reader only.
        observations, refs = [], []
        with self.session_owner._boundary():
            body_close = LocalBodyClose(
                v=1,
                body_owner=r.body_owner_ref,
                state="settled" if body_settled else "unknown",
                proof="wire_buffer_retired" if body_settled else "none",
                request_sha256=a.request_sha256,
                binding_sha256=r.binding_sha256,
            )
            body_ref = self._record(
                a,
                m.MediaLocalBodyCloseV2(
                    v=1,
                    body_owner=r.body_owner_ref,
                    state=body_close.state,
                    proof=body_close.proof,
                    request_sha256=a.request_sha256,
                    binding_sha256=r.binding_sha256,
                ),
            )
            for component, owner_id, settled, proof in (
                ("local_task", a.task_id, task_joined, "task_join"),
                ("transport", a.transport_id, a.transport_closed, "sole_transport_close"),
                ("reader", a.wire_reader_id, all_readers, "reader_join"),
                ("publication", a.publication_id, publications, "publication_settlement"),
            ):
                fields = {
                    "v": 1,
                    "exchange_owner": r.exchange_owner,
                    "component": component,
                    "owner_id": owner_id,
                    "state": "settled" if settled else "unknown",
                    "proof": proof if settled else "none",
                    "request_sha256": a.request_sha256,
                    "binding_sha256": r.binding_sha256,
                }
                observations.append(LocalCloseObservation(**fields))
                refs.append(self._record(a, m.MediaLocalCloseObservationV2(**fields)))
            settled = bool(task_joined and publications and body_settled and all_readers)
            fields = {
                "v": 1,
                "kind": "local_exchange_v2",
                "operation_id": a.operation_id,
                "child_instance": a.instance,
                "binding_sha256": r.binding_sha256,
                "original_owner": r.original_owner,
                "exchange_owner": r.exchange_owner,
                "body_owner": r.body_owner_ref,
                "body_observation": body_ref,
                "state": "settled" if settled else "unknown",
                "outcome": a.outcome if settled else None,
                "local_task_joined": task_joined,
                "transport_sole_closed": a.transport_closed,
                "body_settled": body_settled,
                "all_readers_settled": all_readers,
                "publications_settled": publications,
                "observations": tuple(refs),
                "liability": {"kind": "local_no_remote"},
            }
            cleanup = LocalExchangeCleanupV2(**fields)
            self._record(
                a,
                m.MediaLocalExchangeCleanupV2(
                    **{
                        **fields,
                        "observations": refs,
                        "liability": m.MediaLocalNoRemoteV2(kind="local_no_remote"),
                    }
                ),
            )
        bundle = LlamaExchangeSettlementBundle(
            body_close, body_ref, tuple(observations), tuple(refs), cleanup
        )
        a.snapshots.append(bundle)
        return bundle

    def _done(self, a, task):
        if task is not a.worker or task.get_coro() is not a.coroutine:
            self._unknown = True
            return
        if not task.cancelled():
            task.exception()
        a.reconciliation = asyncio.create_task(self._reconcile(a))
        a.reconciliation.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )

    async def _reconcile(self, a):
        try:
            if a.registration is not None:
                bundle = self._snapshot(a, joined=True)
                positive = bundle.cleanup.state == "settled"
            else:
                positive = (
                    a.worker.done()
                    and (a.reader_closed or not a.reader_open_started)
                    and not a.publication_unknown
                )
            positive = bool(
                positive and not a.unknown and (a.reader_closed or not a.reader_open_started)
            )
            publication_finished = a.publication_finished or (
                a.worker.done() and not a.reader_open_started
            )
            positive = positive and publication_finished
            if positive:
                await a.lease.close()
            a.settled = positive
            with self.session_owner._boundary():
                a.worker_cleanup = self._record(
                    a,
                    m.MediaWorkerCleanup(
                        v=1,
                        kind="native_owner",
                        operation_id=a.operation_id,
                        child_instance=a.instance,
                        binding_sha256=a.worker_binding,
                        original_owner=a.worker_owner,
                        state="settled" if positive else "unknown",
                        outcome=a.outcome if positive else None,
                        proof="local_join" if positive else "none",
                        publication="settled"
                        if publication_finished and not a.publication_unknown
                        else "unknown",
                        scope="native_task",
                    ),
                )
                self.session_owner._sql(
                    "UPDATE native_media_v2_attempts SET state=? WHERE key=?",
                    ("settled" if a.settled else "unknown", a.key),
                )
            if not a.settled:
                # Retain the actual model operation's capacity and original
                # source dependencies. No replacement is dispatched on replay.
                a.unknown = True
                if type(a.lease) is ConverseModelLease:
                    a.lease.cleanup_failed = True
                else:
                    a.lease.failed = True
        except BaseException:
            a.unknown = True
            a.settled = False
            raise

    def source_settled(self, source):
        originals = [a for a in self._attempts.values() if a.source is source]
        if any(
            not a.settled
            or a.reconciliation is None
            or not a.reconciliation.done()
            or a.reconciliation.cancelled()
            or a.reconciliation.exception() is not None
            for a in originals
        ):
            return False
        with self.session_owner.store.lock:
            return (
                self.session_owner._sql(
                    "SELECT 1 FROM native_media_v2_attempts WHERE source_id=? AND state!='settled' LIMIT 1",
                    (source.admission.source_id,),
                ).fetchone()
                is None
            )

    async def retire_source(self, source):
        originals = [a for a in self._attempts.values() if a.source is source]
        for a in originals:
            self._fence(a)
        tasks = {a.worker for a in originals}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            if pending:
                return False
        tasks = {a.reconciliation for a in originals if a.reconciliation is not None}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            if pending:
                return False
        return self.source_settled(source)
