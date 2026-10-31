# ADR 0076: Readable, immutable workspace references

Status: experimental implementation (#183); exact-head release checks remain required.

## Requirement

The fresh M1 repository workflow needs Agent to inspect public README/tests while
keeping those references immutable during generated-code execution. Existing
`protected_roots` rejects native reads and writes, so it cannot express that
scope. Post-command hash comparison detects mutations; it is not write protection.

## Native contract

`ToolPolicy(..., readonly_roots=())` adds a separate owner-configured scope.
Relative roots resolve against the workspace; roots configured through symlink
aliases are refused. Native read_file and search retain
their existing workspace/UTF-8/stability limits. Native writes to a reference,
its descendants, or its ancestors fail before an approval is consumed, including
with broad write grants and YOLO. Both the original lexical pathname and its
canonical target are checked; descriptor walks recheck the scope when creating a
write parent. Existing `protected_roots` continues to deny reads and writes.
There are no model-set permissions, settings, API grants or approval changes.
Agent includes the new root tuple in its inference-currentness snapshot, so
changing it in an awaited request hook refuses the previous count or generation
before transport. This neither grants a write nor changes approval handling.

For native shell execution, BWrap read-only mounts cover the selected references.
A root containing the entire workspace covers the workspace mount; unrelated
external roots expose no new host files and are not mounted.
Sources and all workspace-relative ancestors are opened through no-follow directory
descriptors and inherited only until BWrap consumes its `--bind-fd`/`--ro-bind-fd`
arguments. Writable self-bind mountpoints for ancestors prevent renaming a parent
and replacing the configured pathname, while unrelated files remain writable.
All writable ancestor mounts precede protected/read-only overmounts. Redundant
nested reference roots are coalesced. Missing/inaccessible sources and unsupported
BWrap options fail closed; no less isolated command is attempted.
The integration suite exercises the actual pinned reference mounts after checking
basic BWrap availability. Missing reference-mount capabilities fail those tests;
version numbers alone cannot establish that distribution backports expose the
required options.

A pre-existing hardlink outside a reference could bypass a read-only path mount;
a symlink within a reference could target a writable subtree. Shell preparation
therefore refuses symlinks, nonregular entries and multiply linked files within
read-only references. The complete structural scan is bounded to 4,096 entries
and depth 32; exceeding either bound refuses execution. This is a native resource
limit, not a claim about filesystem or model capacity. New cross-mount hardlinks,
rename/replacement, writable remounts and inherited-descriptor aliases cannot
supply a writable reference in the tested sandbox. The workspace remains owned
by the trusted supervisor; this is not protection from a device administrator or
an unrelated host process authorized to change it during launch.

`build_bwrap_args` remains compatible for policies without read-only references.
A policy with nested read-only roots requires the pinned bindings prepared by
native run_shell; constructing an argv without them refuses execution. Descriptors
close on setup failure, launch failure, cancellation and successful launch. The
existing native child-group cleanup and mandatory OS-isolation behavior remain.

## Evidence and limits

`tests/unit/test_readonly_references.py` covers native reads/search, grants and
owner approvals, relative paths, canonical/lexical aliases, write-parent races,
missing/unsafe reference trees, descriptor ownership and structural refusal.
`tests/integration/test_readonly_reference_sandbox.py` runs actual BWrap commands
that try write/unlink/replace, parent rename, hardlink/symlink aliases, direct and
nested-namespace remounts, and descriptor inspection. It also checks ordinary
source writes, native launch failure and cancellation/reaping. Sandbox tests may
skip when the host cannot run BWrap; such a skip is not an isolation pass.

This enables a later separately frozen M1 trial. It does not claim Agent
completion, model reliability, heldout success or a paid inference result.
