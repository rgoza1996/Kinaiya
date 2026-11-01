"""Bounded native permit inventory and exact owner revocation.

The synchronous broker CAS is the authority transition. Journal publication is
separate metadata, retained after the HTTP caller leaves and retried only with
the frozen original request. Missing history cannot recreate an operation.
"""

import asyncio
import secrets
from dataclasses import dataclass

from packages.contracts.authorizations import (
    AuthorizationIdentity,
    AuthorizationLookup,
    AuthorizationRevoke,
    AuthorizationRevokeReceipt,
    digest,
    revocation_append,
    validate_revocation_entry,
)


class AuthorizationConflict(ValueError):
    pass


@dataclass
class _Revocation:
    request: AuthorizationRevoke
    receipt: AuthorizationRevokeReceipt
    append: object | None = None
    task: asyncio.Task | None = None


class AgentAuthorizations:
    MAX_RECEIPTS = 10000
    MAX_PUBLICATIONS = 32

    def __init__(self, runtime):
        self.runtime, self.policy = runtime, runtime.policy
        self.instance = secrets.token_hex(16)
        self.bindings = {}
        self.operations = {}
        self.tasks = set()
        self._journal_retirement = None

    def _current(self):
        if self.runtime.policy is not self.policy:
            raise RuntimeError("Native authorization broker is unavailable")

    def register(self, rec, approval, tool, args, *, previous=None):
        self._current()
        self._prune_bindings()
        if previous is not None:
            prior = self.bindings.get(previous)
            if prior is None:
                raise RuntimeError("Original native authorization binding is unavailable")
            identity = prior.model_copy(update={"approval_id": approval.approval_id})
        else:
            task = rec.task
            identity = AuthorizationIdentity(
                approval_id=approval.approval_id,
                task_id=task.task_id,
                session_id=task.session_id,
                thread_id=task.thread_id,
                epoch=task.epoch,
                objective_revision=approval.objective_revision,
                tool=tool,
                action_digest=approval.action_digest,
                arguments_sha256=digest(args),
                source_sha256=digest(
                    {
                        "session_id": task.session_id,
                        "thread_id": task.thread_id,
                        "epoch": task.epoch,
                        "task_id": task.task_id,
                        "objective_revision": task.objective_revision,
                        "source_event_ids": list(task.source_event_ids),
                        "source_revisions": dict(rec.source_revisions),
                        "submit_sequence": rec.submit_sequence,
                        "permission_target": self.runtime._permission_decision(rec, tool, args),
                        "harness_version": task.harness_version,
                        "workspace_pin": None
                        if task.workspace_pin is None
                        else task.workspace_pin.model_dump(mode="json"),
                    }
                ),
            )
        self.bindings[approval.approval_id] = identity

    def _prune_bindings(self):
        for key in tuple(self.bindings):
            if self.policy.peek_approval(key) is None:
                del self.bindings[key]

    def _state(self, approval):
        state = self.policy.approval_state(approval.approval_id)
        if state == "pending":
            return "approved_unspent" if approval.owner_reviewed else "requested"
        if state == "retired":
            return approval.retired if approval.retired in {"revoked", "reissued"} else "retired"
        return state

    def snapshot(self, after=None, limit=50):
        self._current()
        self.policy.prune_approvals()
        self._prune_bindings()
        rows = []
        for key in sorted(self.bindings):
            if after is not None and key <= after:
                continue
            approval = self.policy.peek_approval(key)
            if approval is None:
                continue
            rows.append(
                {
                    "identity": self.bindings[key].model_dump(mode="json"),
                    "state": self._state(approval),
                    "revision": approval.revision,
                    # Display seconds use the existing integer-only bounded
                    # permission JSON grammar. Native expiry remains exact.
                    "expires_at": int(approval.expires_at),
                    "origin": "owner_review",
                }
            )
            if len(rows) > limit:
                break
        policy = self.policy
        try:
            policy.check_workspace()
            workspace_available = True
        except Exception:
            workspace_available = False
        return {
            "schema_version": "1.0",
            "broker_instance": self.instance,
            "broad_grants": {
                "source": "service_instance",
                "write_file": "deny"
                if policy.deny_write
                else "allow"
                if policy.effective_write
                else "ask",
                "shell": "deny"
                if policy.deny_shell
                else "allow"
                if policy.effective_shell
                else "ask",
                "network": policy.network,
                "yolo": policy.yolo,
                "workspace_available": workspace_available,
                "readonly_roots": len(policy.readonly_roots),
                "isolation": "required_per_operation",
                "isolation_verified": False,
            },
            "authorizations": rows[:limit],
            "next_after": rows[limit - 1]["identity"]["approval_id"] if len(rows) > limit else None,
            "limits": {
                "live": policy.MAX_LIVE_APPROVALS,
                "closed": policy.MAX_CLOSED_APPROVALS,
                "revocations": self.MAX_RECEIPTS,
                "publications": self.MAX_PUBLICATIONS,
            },
        }

    def _prior(self, request):
        prior = self.operations.get(request.idempotency_key)
        if prior is not None and prior.request != request:
            raise AuthorizationConflict("Revocation key belongs to a different original request")
        return prior

    async def lookup(self, request):
        self._current()
        prior = self._prior(request)
        if prior is None or (
            prior.receipt.status == "revoked" and prior.receipt.journal_status != "confirmed"
        ):
            # Positive journal metadata can recover a receipt without touching
            # a permit. Absence is deliberately not a CAS fence or failure.
            lookup = getattr(self.runtime.journal, "lookup_authorization_revocation", None)
            if lookup is not None:
                proof = await lookup(request)
                proof.validate_request(request)
                if proof.recorded:
                    receipt = AuthorizationRevokeReceipt(
                        schema_version="1.0",
                        status="revoked",
                        request=request,
                        observed_state="revoked",
                        revision=request.expected_revision + 1,
                        journal_status="confirmed",
                    )
                    return AuthorizationLookup(
                        schema_version="1.0",
                        status="observed",
                        broker_instance=self.instance,
                        receipt=receipt,
                    ).model_dump(mode="json")
        return AuthorizationLookup(
            schema_version="1.0",
            status="observed" if prior is not None else "unobserved",
            broker_instance=self.instance,
            receipt=None if prior is None else prior.receipt,
        ).model_dump(mode="json")

    def _publish(self, operation):
        if operation.append is None or operation.receipt.journal_status == "confirmed":
            return
        if operation.task is not None and not operation.task.done():
            return
        if self.runtime._closed or len(self.tasks) >= self.MAX_PUBLICATIONS:
            return

        async def publish():
            operation.receipt = operation.receipt.model_copy(update={"journal_status": "pending"})
            try:
                entry = await self.runtime.journal.append(operation.append)
                validate_revocation_entry(entry, operation.request)
            except (Exception, asyncio.CancelledError):
                operation.receipt = operation.receipt.model_copy(
                    update={"journal_status": "unconfirmed"}
                )
            else:
                operation.receipt = operation.receipt.model_copy(
                    update={"journal_status": "confirmed"}
                )

        operation.task = asyncio.create_task(publish())
        self.tasks.add(operation.task)
        self.runtime._retain_workspace_worker(operation.task)
        operation.task.add_done_callback(self.tasks.discard)

    async def revoke(self, request):
        self._current()
        prior = self._prior(request)
        if prior is not None:
            self._publish(prior)
            return prior.receipt.model_dump(mode="json")
        if request.broker_instance != self.instance:
            raise AuthorizationConflict("Original native authorization broker is unavailable")
        if self.runtime._closed or len(self.operations) >= self.MAX_RECEIPTS:
            raise RuntimeError("Native revocation receipt capacity is unavailable")
        if len(self.tasks) >= self.MAX_PUBLICATIONS:
            raise RuntimeError("Native revocation publication capacity is unavailable")
        identity = request.identity
        if self.bindings.get(identity.approval_id) != identity:
            raise AuthorizationConflict("Original native authorization identity is unavailable")
        rec = self.runtime._tasks.get(identity.task_id)
        if rec is None:
            raise AuthorizationConflict("Original native authorization owner is unavailable")
        async with rec.approval_lock:
            prior = self._prior(request)
            if prior is not None:
                self._publish(prior)
                return prior.receipt.model_dump(mode="json")
            # No await from this final identity/revision check through the
            # one-way broker transition and retained receipt installation.
            self._current()
            if (
                self.runtime._closed
                or len(self.operations) >= self.MAX_RECEIPTS
                or len(self.tasks) >= self.MAX_PUBLICATIONS
            ):
                raise RuntimeError("Native revocation ownership is unavailable")
            if self.bindings.get(identity.approval_id) != identity:
                raise AuthorizationConflict("Original native authorization identity is unavailable")
            approval = self.policy.peek_approval(identity.approval_id)
            if approval is None:
                raise AuthorizationConflict("Native authorization history is unavailable")
            frozen = revocation_append(request)
            if len(frozen.model_dump_json().encode("utf-8")) > 8192:
                raise ValueError("Native revocation event exceeds its bound")
            revoked = self.policy.revoke_approval(identity.approval_id, request.expected_revision)
            receipt = AuthorizationRevokeReceipt(
                schema_version="1.0",
                status="revoked" if revoked else "not_revoked",
                request=request,
                observed_state=self._state(approval),
                revision=approval.revision,
                journal_status="unconfirmed" if revoked else "not_required",
            )
            operation = _Revocation(request, receipt, frozen if revoked else None)
            self.operations[request.idempotency_key] = operation
            if revoked:
                rec.resume.set()
                self._publish(operation)
        return operation.receipt.model_dump(mode="json")

    async def drain(self):
        if self.tasks:
            await asyncio.wait(tuple(self.tasks), timeout=1)

    async def close_journal(self, journal):
        """Keep the real client alive until admitted metadata exchanges retire."""
        if self._journal_retirement is None:

            async def retire():
                while True:
                    tasks = set(self.tasks)
                    for exchange in tuple(getattr(journal, "_submit_exchanges", ())):
                        tasks.add(exchange.task)
                        if exchange.close_task is not None:
                            tasks.add(exchange.close_task)
                    tasks = {task for task in tasks if not task.done()}
                    if not tasks:
                        break
                    await asyncio.gather(
                        *(asyncio.shield(task) for task in tasks), return_exceptions=True
                    )
                await journal.close()

            self._journal_retirement = asyncio.create_task(retire())
            self.runtime._retain_workspace_worker(self._journal_retirement, transport=True)
        await asyncio.wait({self._journal_retirement}, timeout=1)
