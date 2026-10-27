# ADR 0016: durable isolated JSON compute plugins

Issue: [#42](https://github.com/rgoza1996/Kinaiya/issues/42). Implements a bounded
part of Roadmap §10.5 and §31.6–31.7 and `clarification.md`: an acquired callable
operation can execute ordinary software without language-model inference. This
is separate from changing the context-selector harness or proving benchmark gains.

## Contract and containment

The optional `plugins` host supports one `python_json.v1` function per immutable
package: `def run(arguments) -> JSON`. The strict manifest identifies the plugin,
author, license, input/output JSON schemas, `entrypoint: run`, capabilities exactly
`["compute"]`, and no dependencies. Runtime/schema/entrypoint/capabilities have
fixed defaults; unsupported versions, dependencies and capabilities are refused.
The profile preloads a declared standard-library set, including CSV, decimal,
statistics, datetime and hashing. It is not an arbitrary package installer or a
foreign plugin adapter. Existing `context_selector.v1` isolation is unchanged.

The host captures at most 64 KiB source, hashes source plus canonical manifest,
and never imports it into the supervisor. Linux bubblewrap supplies user, PID,
network and UTS namespaces, a cleared environment and no workspace, core, owner
suite or credential mounts. A required seccomp allowlist denies file opens,
process/thread creation and sockets before any candidate module executes. The
trusted runner applies CPU 10 s, address-space 256 MiB and file/core limits;
the parent enforces wall 10 s and 64 KiB pipe bounds. Missing isolation fails
closed. AST restrictions provide additional screening, not the execution boundary.
Cancellation kills and reaps the isolated process before releasing execution
admission; repeated cancellation does not abandon its worker thread.

Schemas use a bounded JSON Schema 2020-12 profile: no references, regex or external
schemas; unsupported keywords are refused. Schemas are limited to 16 KiB, 512
nodes and depth 12; input/output values to 64 KiB, 4096 nodes and depth 32, with
finite numbers. Selected model schemas/results have a stricter context bound.

## Authority, evaluation and lifecycle

Owner configuration maps plugin IDs to independently held JSON acceptance suites
outside the worker workspace and registry. A suite contains up to 64 exact-output
cases in 256 KiB. Candidate output cannot select criteria or report its own pass.
Reports bind the source, manifest and canonical suite digests. Execution requires
a stored passing report plus explicit owner-reviewed promotion; registering or
evaluating alone does not activate code. Invocation, rollback and promotion
recheck approval and suite bindings. Native receipts bind source, arguments and
output hashes; they do not prove domain correctness or external effects.

The protected registry uses a lifecycle file lock before startup writes, bounded
no-follow regular-file reads, immutable content addresses and atomic state files.
Each task pins an exact approved version before its first call. Updates affect
new tasks; older approved pins continue unchanged. Pins use durable task identities,
so one task releasing a version cannot release another task's reference. Native
`plugin.pinned` journal entries retain the mapping during recovery. Uncertain tasks
recover paused without reexecuting code, and block removal until resolved.
Disable revokes old pins as well as the current pointer; a result is rechecked
after execution. Removal refuses active/history-dependent/live-task references.
Rollback restores only an intact previously approved version under current criteria.

Owner-only `/v1/plugins` inspection and `/v1/plugins/control` share implementation
with Agent's `/plugins` runtime command. The latter supports `list [after]`,
`inspect <version>` and `control '<JSON>'`. Commands never reach Converse. Models
receive only `plugins_list` and `plugin_call`, with no lifecycle authority. Tool
failure cannot be hidden by an unrelated later write/read, and an extension
receipt alone cannot pass the native completion-evidence gate.

This adds optional tools to the experimental 1.0 `ToolAction` enum and publishes
the `PluginControl` JSON schema. Upgrade strict host/client schema consumers
together; compatibility with older strict tool-action validators is not claimed.
The existing Task wire shape is unchanged; native pins use journal metadata.

Every lifecycle/control POST requires an idempotency key. A separate protected
SQLite record commits intent before effects, then retains the outcome. A duplicate
returns the same result; changed input conflicts; a pending record after restart
requires inspection instead of replay. Multi-file registry/audit changes are not
a single transaction: errors after a pointer write require owner reconciliation.
The host does not promise automatic rollback of every interrupted filesystem write.

## Disclosure and setup

Install the optional `plugins` extra using the project's locked dependencies;
Linux bubblewrap, system Python and libseccomp are required. Configure:

```json
{"plugins":{"enabled":true,"suites":{"public_stats":"/owner/suites/stats.json"}}}
```

The launcher stores registry/control files under its protected data directory,
disjoint from the granted worker workspace. Content scope is version-specific and
defaults to `local_only`. Only a reviewed owner choice can grant `public_synthetic`
or `private_cloud_authorized`. Contributor receives only public/synthetic versions;
private-cloud scope never authorizes Contributor. Discovery filters before paging,
and retained pins are checked before later model requests. A pending scope change
blocks cloud disclosure until reconciled. These scopes cover extension metadata and
results; normal task/provider disclosure grants still apply independently.

## Verification and limits

Focused tests exercise actual kernel denial, conventional computation, raw descriptor
output bounds, timeout/memory/cancellation, stdout forgery, immutable source/report
bindings, distinct durable pins, owner authentication, control replay/uncertainty,
restart recovery, Contributor filtering, Agent invocation and native file evidence.
`scripts/live_compute_plugin.py` is a separately opted-in public/synthetic acquisition
probe; deterministic tests do not call paid inference.

The [recorded live probe](../../evaluation/evidence/compute-plugins-2026-10-27.json)
used two real Contributor calls to author a CSV algorithm and five to use it.
Four owner cases passed; direct owner invocation and reopening used zero model
calls. A deliberately wrong update failed three cases; an independently reviewed
equivalent revision was promoted and rolled back. The resulting file hash was
`228c610e3cc8907514b9b40965f47c323a1ad170a031cef1b9097f85aebc0825`.
The retained source/manifest/suite replay in deterministic CI. This is functional
screening, not proof of general CSV correctness or a harness improvement. The first
probe failed with a placeholder credential; its unresolved reservation is retained.

This does not complete M4A: general hooks, plugin-defined command registration,
aliases, dependency installation/locks, hardware/GPU capabilities, foreign runtime
adapters and equal-tool harness-composition experiments remain open. Pure JSON
computation has no arbitrary filesystem/network/hardware privileges to widen.
Owner-reviewed activation is mandatory in this profile; autonomous discovery,
installation and general automatic promotion remain future work.
