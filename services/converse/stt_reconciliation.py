"""Positive-only retirement of exact durable native STT admissions."""

import asyncio
import hashlib
import re

from packages.contracts.models import STTModelPin
from packages.contracts.stt_settlement import (
    STTAdmissionReconcile,
    STTPublication,
    STTWorkerBinding,
    digest,
    encode,
    settlement_json,
)


def native_client(provider):
    from packages.providers.meta_speech import CloudSTTLocalTTS
    from packages.providers.speech import SpeechClient

    if isinstance(provider, CloudSTTLocalTTS):
        provider = provider.stt
    return provider if isinstance(provider, SpeechClient) and provider.stt is None else None


def identity_digest(identity):
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class STTAdmissionRecovery:
    @staticmethod
    def _encode_binding(binding):
        if binding is None:
            return None
        value = STTWorkerBinding.model_validate(binding).model_dump()
        result = encode(value)
        if len(result.encode("utf-8")) > 8192:
            raise ValueError("STT admission binding exceeds its bound")
        return result

    def _checked_binding(self, binding, identity):
        if binding is None:
            return None
        if type(binding) is not str:
            raise ValueError("Stored STT admission binding exceeds its bound")
        observed = STTWorkerBinding.model_validate(settlement_json(binding.encode("utf-8")))
        if encode(observed.model_dump()) != binding or (
            observed.publication.state == "required"
            and (
                observed.publication.stt_identity_sha256 != identity_digest(identity)
                or observed.publication.audio_sha256 != observed.operation.audio_sha256
            )
        ):
            raise ValueError("Stored STT binding changed its original model or audio identity")
        return observed

    def _admission_details(self):
        identities = self._admissions()
        result = {}
        for key, binding in self.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(id)=32 THEN id END, CASE WHEN binding IS NULL THEN NULL WHEN typeof(binding)='text' AND length(CAST(binding AS BLOB))<=8192 THEN binding ELSE '' END FROM stt_model_admissions LIMIT 65"
        ):
            if key not in identities:
                raise ValueError("STT admission binding identity changed")
            self._checked_binding(binding, identities[key])
            result[key] = identities[key], binding
        if set(result) != set(identities):
            raise ValueError("STT admission history is incomplete")
        return result

    def admit_publication(self, lease, expected):
        if lease.admission_id is None or lease.admission_binding is None:
            return  # Historical/unsupported worker markers stay unreconcilable.
        if lease not in self.leases or not lease.current():
            raise RuntimeError("STT publication lost its original native admission")
        binding = STTWorkerBinding.model_validate(
            settlement_json(lease.admission_binding.encode("utf-8"))
        )
        publication = STTPublication(
            state="required",
            session_id=expected.session_id,
            thread_id=expected.thread_id,
            epoch=expected.epoch,
            idempotency_key=expected.idempotency_key,
            request_sha256=digest(expected.model_dump(mode="json")),
            audio_sha256=binding.operation.audio_sha256,
            stt_identity_sha256=identity_digest(lease.admission_identity),
        )
        if expected.payload.get("audio_sha256") != binding.operation.audio_sha256 or encode(
            expected.payload.get("stt_model")
        ) != encode(lease.pin.model_dump(mode="json")):
            raise ValueError("STT publication changed its admitted audio or model pin")
        if binding.publication.state != "not_admitted":
            raise ValueError("STT publication was already admitted or is unavailable")
        updated = self._encode_binding(
            {**binding.model_dump(), "publication": publication.model_dump()}
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if not lease.current():
                raise RuntimeError("STT publication admission changed")
            changed = self.db.execute(
                "UPDATE stt_model_admissions SET binding=? WHERE id=? AND identity=? AND binding=?",
                (updated, lease.admission_id, lease.admission_identity, lease.admission_binding),
            ).rowcount
            if changed != 1:
                raise RuntimeError("STT original publication binding changed")
            self.db.commit()
            lease.admission_binding = updated
        except BaseException:
            lease.mark_unknown()
            self._rollback()
            raise

    def _active_admission(self, key):
        return any(
            operation.lease.admission_id == key for operation in self.runtime._stt_operations
        )

    def admission_inventory(self):
        self._read()
        admissions = []
        for key, (identity, encoded) in sorted(self._admission_details().items()):
            binding = (
                None
                if encoded is None
                else STTWorkerBinding.model_validate(settlement_json(encoded.encode("utf-8")))
            )
            active = self._active_admission(key)
            client = native_client(self.host_provider)
            supported = (
                binding is not None
                and client is not None
                and client._native_stt_origin == binding.origin
                and client._native_bound()
            )
            admissions.append(
                {
                    "admission_id": key,
                    "identity_sha256": identity_digest(identity),
                    "protocol": None if binding is None else binding.protocol,
                    "worker_instance": None
                    if binding is None
                    else binding.operation.worker_instance,
                    "operation_id": None if binding is None else binding.operation.operation_id,
                    "publication_state": "legacy_unknown"
                    if binding is None
                    else binding.publication.state,
                    "active": active,
                    "reconcilable": supported and not active,
                    "reason": "The original operation is still locally owned"
                    if active
                    else "Exact native worker proof can be checked; no audio is resent"
                    if supported
                    else "This original admission has no supported configured worker proof",
                }
            )
        return {
            "schema_version": "1.0",
            "role": "stt",
            "backend_id": self.backend_id,
            "instance_id": self.instance_id,
            "admissions": admissions,
            "limit": 64,
        }

    def _reconcile_result(
        self,
        request,
        *,
        settled=False,
        worker=False,
        publication=False,
        reason="Original native STT settlement is unknown",
    ):
        return {
            "schema_version": "1.0",
            "role": "stt",
            "status": "settled" if settled else "unknown",
            "backend_id": self.backend_id,
            "admission_id": request.admission_id,
            "identity_sha256": request.identity_sha256,
            "worker_settled": worker,
            "publication_settled": publication,
            "retired": settled,
            "reason": reason,
        }

    def _settlement_record(self, key):
        row = self.db.execute(
            "SELECT CASE WHEN typeof(identity)='text' AND length(CAST(identity AS BLOB))<=2048 THEN identity END, CASE WHEN typeof(binding)='text' AND length(CAST(binding AS BLOB))<=8192 THEN binding END, CASE WHEN typeof(request)='text' AND length(CAST(request AS BLOB))<=4096 THEN request END, CASE WHEN typeof(receipt)='text' AND length(CAST(receipt AS BLOB))<=4096 THEN receipt END FROM stt_model_settlements WHERE id=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        identity, binding, original, receipt = row
        if any(type(item) is not str for item in row):
            raise ValueError("STT settlement metadata exceeds its bound")
        pin = STTModelPin.model_validate(settlement_json(identity.encode("utf-8"), 2048))
        request = STTAdmissionReconcile.model_validate(
            settlement_json(original.encode("utf-8"), 4096)
        )
        observed = settlement_json(receipt.encode("utf-8"), 4096)
        self._checked_binding(binding, identity)
        if (
            pin.source != "host"
            or pin.backend_id != self.backend_id
            or pin.settings_revision > self._read()[0]
            or encode(pin.model_dump()) != identity
            or self._encode_binding(settlement_json(binding.encode("utf-8"))) != binding
            or request.expected_backend != self.backend_id
            or request.admission_id != key
            or request.identity_sha256 != identity_digest(identity)
            or encode(request.model_dump()) != original
            or encode(observed)
            != encode(
                self._reconcile_result(
                    request,
                    settled=True,
                    worker=True,
                    publication=True,
                    reason="Exact native worker and publication settlement confirmed",
                )
            )
        ):
            raise ValueError("Stored native STT settlement proof is invalid")
        return request, observed

    def _validate_settlements(self):
        self._admission_details()
        if self.db.execute("SELECT COUNT(*) FROM stt_model_settlements").fetchone()[0] > 10000:
            raise ValueError("STT settlement history exceeds its bound")
        for (key,) in self.db.execute(
            "SELECT CASE WHEN typeof(id)='text' AND length(id)=32 THEN id END FROM stt_model_settlements LIMIT 10001"
        ):
            if (
                type(key) is not str
                or re.fullmatch(r"[a-f0-9]{32}", key) is None
                or self._settlement_record(key) is None
            ):
                raise ValueError("STT settlement identity is invalid")
            if (
                self.db.execute("SELECT 1 FROM stt_model_admissions WHERE id=?", (key,)).fetchone()
                is not None
            ):
                raise ValueError("An STT admission cannot be both retained and retired")

    def _retain_settlement(self, key, identity, binding):
        if binding is None:
            return None
        request = STTAdmissionReconcile(
            schema_version="1.0",
            expected_backend=self.backend_id,
            admission_id=key,
            identity_sha256=identity_digest(identity),
        )
        receipt = self._reconcile_result(
            request,
            settled=True,
            worker=True,
            publication=True,
            reason="Exact native worker and publication settlement confirmed",
        )
        count = self.db.execute("SELECT COUNT(*) FROM stt_model_settlements").fetchone()[0]
        if count > 10000:
            raise ValueError("STT settlement history exceeds its bound")
        if count == 10000:
            # These rows are positive terminal proofs only. Their eviction
            # never recreates an admission or permits an old worker dispatch.
            self.db.execute(
                "DELETE FROM stt_model_settlements WHERE rowid=(SELECT MIN(rowid) FROM stt_model_settlements)"
            )
        self.db.execute(
            "INSERT INTO stt_model_settlements (id,identity,binding,request,receipt) VALUES (?,?,?,?,?)",
            (key, identity, binding, encode(request.model_dump()), encode(receipt)),
        )
        return receipt

    async def reconcile_admission(self, request):
        request = STTAdmissionReconcile.model_validate(request)
        self._read()
        if request.expected_backend != self.backend_id:
            raise ValueError("STT admission belongs to another native authority")
        prior = self._settlement_record(request.admission_id)
        if prior is not None:
            if prior[0] != request:
                raise ValueError("STT settlement request identity was reused")
            return prior[1]
        running = self._reconciling.get(request.admission_id)
        if running is not None:
            if running[0] != request:
                raise ValueError("STT admission reconciliation identity was reused")
            return await asyncio.shield(running[1])
        if len(self._reconciling) >= 8:
            raise RuntimeError("STT reconciliation capacity is retained")
        task = asyncio.create_task(self._reconcile_admission(request))
        self._reconciling[request.admission_id] = (request, task)

        def done(completed):
            if not completed.cancelled():
                completed.exception()
            self._reconciling.pop(request.admission_id, None)
            self._finish_close()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def _reconcile_admission(self, request):
        original = self._admission_details().get(request.admission_id)
        if original is None:
            return self._reconcile_result(
                request, reason="No original admission or retained settlement receipt is observed"
            )
        identity, encoded = original
        if identity_digest(identity) != request.identity_sha256:
            raise ValueError("STT admission does not match its original pin")
        if self._active_admission(request.admission_id):
            return self._reconcile_result(
                request,
                reason="The original operation still owns local work or publication cleanup",
            )
        client = native_client(self.host_provider)
        if encoded is None or client is None:
            return self._reconcile_result(
                request, reason="This original admission has no supported native worker binding"
            )
        binding = STTWorkerBinding.model_validate(settlement_json(encoded.encode("utf-8")))
        leases = [lease for lease in self.leases if lease.admission_id == request.admission_id]
        if (
            any(
                lease.cleanup_actual_failed or lease.cleanup is None or not lease.cleanup.done()
                for lease in leases
            )
            or client._cleanup_failed
            or client._native_closes
        ):
            return self._reconcile_result(
                request, reason="Actual original transport or adapter cleanup is not confirmed"
            )
        try:
            worker = (await client.lookup_native_stt(binding))["settled"]
            publication = binding.publication.state in {"none", "not_admitted"}
            if worker and not publication:
                publication = await self.runtime.journal.lookup_stt_publication(binding.publication)
        except (Exception, SystemExit):
            return self._reconcile_result(request)
        if not worker or not publication:
            return self._reconcile_result(request, worker=worker, publication=publication)
        if (
            self._active_admission(request.admission_id)
            or client._cleanup_failed
            or client._native_closes
            or getattr(self.runtime.journal, "_submit_cleanup_failed", False)
            or getattr(self.runtime.journal, "_submit_stt_closes", set())
        ):
            return self._reconcile_result(
                request,
                worker=True,
                publication=True,
                reason="Exact metadata is confirmed but actual local cleanup remains owned",
            )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._read()
            if self._admission_details().get(request.admission_id) != original:
                raise RuntimeError("Original STT retirement identity or receipt capacity changed")
            receipt = self._retain_settlement(request.admission_id, identity, encoded)
            if (
                self.db.execute(
                    "DELETE FROM stt_model_admissions WHERE id=? AND identity=? AND binding=?",
                    (request.admission_id, identity, encoded),
                ).rowcount
                != 1
            ):
                raise RuntimeError("Original STT admission retirement changed")
            self.db.commit()
        except BaseException:
            self._rollback()
            raise
        self._native_unsettled.discard(request.admission_id)
        for lease in leases:
            lease.cleanup_unknown = lease.cleanup_failed = False
            lease.settled = True
            self._released(lease)
        return receipt
