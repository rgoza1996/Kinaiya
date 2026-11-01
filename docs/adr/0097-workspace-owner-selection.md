# 0097: Native owner workspace selection

November 1, 2026. A bounded increment of [#208](https://github.com/rgoza1996/Kinaiya/issues/208),
following execution inspection (ADR0084) and scoped tool permissions (ADR0096).
The native owner can select an existing worker directory and add read-only
folders. This does not deliver alternate execution backends or Git worktrees.

## Selection and authority

`WorkspaceSelection` contains an absolute canonical existing directory and at
most 32 sorted, unique relative read-only directories. `.` makes the entire
worker directory read-only. Paths reject traversal, controls, aliases and
implicit directory creation. Root paths are bounded to 4096 UTF-8 bytes and
relative paths to 512. An explicit owner Save grants the selected worker root;
model output, a task request and metadata inspection cannot select one.

Host read-only restrictions and protected control, native runtime, provider
credential and evaluation paths remain immutable. A new root must be disjoint
from those protected paths. The launcher includes its native code/import roots,
Python environment and sandbox binary in the guards. The implementation keeps
mandatory Bubblewrap and direct-workspace mode. A detected binary is not a
verified isolation result.

The protected `agent-workspace.sqlite` requires affirmative atomic-new-file
creation for initialization. Existing empty, incomplete or corrupt storage is
unavailable. A lifetime lock, backend/instance identity, revision/state CAS and
at most 10,000 exact receipts prevent another native owner or uncertain retry
from silently applying a new root. Store closure retains its lock and root file
descriptor while actual work or uncertain cleanup remains owned.

## Saved and active state

Inspection reports saved settings and concrete saved-root identity separately
from the actually installed context. Missing directories, symlink aliases and
same-path inode replacement make the active root unavailable. The native
control plane retains saved metadata and exact receipt recovery; a blocked,
never-created bootstrap reference grants no filesystem access. A fresh explicit
Save can authorize a valid replacement, including the same pathname with a new
concrete identity. Historical receipts establish the original save, not current
application, and never reapply themselves.

A change reserves the native transition before its first await, waits for
transport retirement and rechecks actual ownership. New selection requires
settled tasks, workers, processes, request-body admission, submit exchanges,
extension controls, model cleanup and MCP actors. Caller cancellation or a stop
timeout alone cannot release those owners. Prepared policy, MCP, skill and
plugin installers validate before durable commit and apply synchronously after
it. An unconfirmed application leaves active state unavailable; it never
substitutes the host root.

Execution-settings state tokens also bind the native workspace backend/instance,
saved revision/root identity and actual active phase. A same-path root repair
therefore makes an older execution draft stale. This stamp does not recurse
through either owner's state token; exact historical execution receipts still
replay before the current-write check and are never rewritten.

File tools retain a root descriptor. Shell and MCP stdio pass only explicitly
owned descriptors to Bubblewrap FD mounts, retaining them until real spawn
settles. A late child remains owned until actual exit. Root inspection and
request preparation do not run a worker, probe a provider or qualify isolation.

## Captured provenance and compatibility

New native tasks and tool records carry a pathless `WorkspacePin`: the stable
path-derived workspace ID, separate concrete root-identity digest, settings
revision/source, Bubblewrap backend and direct mode. The workspace ID keeps the
existing canonical-path hash, so adding this owner cannot discard earlier
workspace Deny/Ask rules. The independent root identity gates actual effects.
Task-scoped permission rules use the captured pin; an older unpinned task cannot
acquire the current root merely by being opened after a switch.

Historical tasks without this additive field retain their original wire shape.
Their generated-document metadata remains inspectable with an explicitly
unknown historical root and null path. Pinned output records retain their
original relative path and pin. Neither branch resolves an old record through
the current workspace; the library action opens the original Agent session.

## Owner API and embedded editor

Owner routes are `GET/POST /v1/agent/options/workspace-settings`,
`GET /v1/agent/options/workspace-changes/{key}`, and
`POST /v1/agent/options/workspace-changes/lookup`. Changes carry the complete
selection, expected backend/instance/revision/state and original idempotency
key. Requests are bounded to 64 KiB and metadata to 256 KiB. Exact recorded
lookup returns the original request and receipt; missing receipt alone does
not prove that a save failed.

The traditional Workspace page embeds root and read-only folder editing, saved
versus active state, immutable guard inspection, live-operation blockers and
lookup-first uncertain-save recovery. It retains bounded exact pending requests
and drafts across reconnects. Shell starting directory remains a separate
execution preference with its existing Save boundary. Refresh cannot silently
retarget a dirty selection or authorize repair.

A valid new root can be applied even when the retained relative shell starting
directory does not exist there. That directory is reported unavailable and
shell/MCP stdio launch still refuses it; the separate execution Save can repair
it after the new root is active. Root selection never silently resets another
owner's working-directory preference.

## Evidence and remaining scope

Native source review, generated schemas, formatting/static checks, CI, local
preview observation and real isolation qualification are distinct evidence.
Consult the associated PR for what actually ran. This source increment does not
close the full Workspace/Execution acceptance group, establish filesystem
checkpoints or child workspaces, or qualify any model/device. No new automated
test cases, local test runs, paid inference, model downloads or actual workspace
changes are part of this implementation increment.
