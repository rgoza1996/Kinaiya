# ADR 0069: Confirm final Agent success before publishing local success

Status: accepted for the bounded #167 implementation.

The legacy completion path changed its live task to `succeeded` before awaiting
the final outcome append. A lost acknowledgment could leave false local success
or allow contradictory terminal records. Native UI publication had a separate
conservative error path, which did not fix legacy work.

Use one frozen native success append and keep the live task `verifying` until
an exact authenticated receipt is observed. Reconcile append errors with bounded
positive read-back; otherwise expose paused/unconfirmed state without another
terminal write or automatic work replay. A finalization fence rejects new task
mutations promptly and survives local uncertainty. Accepted earlier steering
returns to the task loop; shutdown cannot turn possible success into a new
cancellation. See the [completion contract](../agent-completion-confirmation.md).

This deliberately favors honest uncertainty over guessing whether a transport
failure occurred before commit. Restart recovery supplies durable truth. The
change does not redefine the objective, relax evidence requirements, qualify
model task quality or complete the broader production recovery milestone.
