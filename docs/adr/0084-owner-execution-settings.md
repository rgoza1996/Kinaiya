# 0084: Owner execution settings and explicit native denials

Requirement: [#208](https://github.com/rgoza1996/Kinaiya/issues/208), first
increment of the [full Options goal](../options-delivery-plan.md). This extends
ADR0077's traditional embedded workspace; it does not complete the full
permissions, workspace or execution inventory.

## Authority and persistence

`AgentRuntimeSettings` owns a protected `agent-runtime-settings.sqlite` outside
the approved worker workspace, mode 0600. It persists native file-write and
command modes (`ask`, `allow`, `deny`), tool network access, maximum task steps
(1–2000), maximum native command duration (1–120 whole seconds), and an existing
relative shell starting directory (1–512 characters). No credentials, arbitrary
environment, workspace relocation, provider selection or generated code are
accepted. The host selects the approved root and protected/read-only folders.

Until the first owner save, startup host configuration is authoritative. Saved
preferences then load on service startup. A removed starting directory leaves
shell unavailable until repaired; there is no silent root substitution.
YOLO is explicitly process-local: a request may change its current value, and
the historical receipt records that request, but startup does not restore it
from this store. Existing native `/yolo off` revocation remains available.

Ask requires an exact single-use native action approval. Allow still enforces
workspace/protected/read-only boundaries. Deny is checked before both YOLO and
single-use approvals. File-write Deny also mounts the workspace read-only for
native shell commands, including their ancestor self-binds; scratch `/tmp`
remains a private writable sandbox. With file writes set to Ask, an independently
allowed shell command can still write workspace files. The UI explains this
distinction between the native file-write tool and command authorization.
MCP/plugin authority remains with its existing native owner; these are not yet
general per-tool rules.

Denied native actions are advertised as `approval: denied`, never as a broad
grant. They skip speech/action approval requests and do not spend a previously
approved speech permit before refusing. A non-requestable action is not an
authorized action.

## Change boundary and receipts

Task submission/recovery and settings changes share `integration_lock`.
Saving requires every retained task to be terminal, its worker done, any child
process released and retirement cleanup unlocked. Paused/recovered work also
blocks a save. A terminal publication alone does not prove cleanup ended.
An execution settings save retires live MCP transports, then rechecks state
after that await. A failed retirement cannot authorize new settings.

Requests bind both the persisted revision and a SHA-256 state token containing
service-instance identity, actual policy, limits, workspace and folder guards.
This catches native `/yolo` changes that do not increment this store's revision.
One synchronous SQLite transaction stores preferences and their receipt; no
await separates the final check, commit and native apply. Receipts are bounded
at 10,000 and are not silently evicted. Same key/body recovers the original
receipt; a mismatching key/body or stale state refuses.

The receipt status is `settings_saved`, not a claim that its values are still
active. A duplicate/restart receipt read never reapplies historical settings.
Inspection reports active settings, saved settings, source, service-instance
identity, YOLO scope, effective write/command flags and whether active values
match saved values. A write error or lost commit response remains uncertain;
an exact receipt and refreshed active state are separate observations.

## Paths and isolation

The starting directory must remain inside the existing workspace, be a directory
and use no traversal, control characters or symlink aliases. Every command
revalidates the path. Bubblewrap remains mandatory, with the existing process
cleanup, PID isolation, bounded output, protected/read-only mounts and network
namespace rules. The configured command cap bounds the existing requested
command timeout; it is not a whole-task deadline. The environment remains
cleared with only the existing public PATH and HOME declarations.
Path-resolution failures, including Python's symlink-loop `RuntimeError`, are
native directory refusals; an unusable saved directory does not prevent Options
inspection and repair. Workspace mount metadata includes guards covering the
entire workspace and reports unavailable when the starting directory is invalid.

Owner metadata detects the Bubblewrap executable but reports
`isolation_verified: false`: inspection does not execute a process or establish
namespace readiness. No environment secrets are returned. The authenticated
inspection and execution-settings responses use `Cache-Control: no-store`.

## App behavior and remaining gates

The following describes the original increment. [ADR0089](0089-bound-execution-settings-writes.md)
specifies its replacement for owner/backend-scoped pending recovery, durable
writer fencing and actual shared transport ownership; consult its PR evidence
for the later implementation and verification status.

Agent's category has separate Permissions, Workspace and Execution pages.
Their draft is retained while navigating or refreshing. A changed host state
does not overwrite a dirty draft; saving requires reviewing new active values.
Pending writes survive Options close/reopen and same-endpoint reconnect in RAM
(at most 16 endpoint operations, at most two in-flight exact requests each).
Navigation never retries. Timeout/transport uncertainty blocks a different
execution change until reconciliation; receipt lookup and exact retry are
explicit. A confirmed save refreshes active state without claiming a historical
receipt reapplied it. Old hosts remain visibly read-only/unavailable.

Read/per-tool rules, more settings scopes, root relocation, alternate execution
backends, Git worktree creation, whole-task deadlines/concurrency and hardware
limits remain tracked work. This change neither installs dependencies nor calls
models. Implementation evidence belongs in its PR; new local tests were not
added/run for this increment because the current request asked for implementation.
Existing CI checks remain release evidence only for behavior they cover.
