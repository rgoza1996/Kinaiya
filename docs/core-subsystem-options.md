# Core subsystem selection in Options (#566 / #567)

**Options → Core → Subsystems** uses the native next-launch selector described
in [subsystem topology](subsystem-topology.md). It offers host defaults and the
six valid graphs. Converse always includes Decisions. This selector does not
change role models, credentials, prompts, permissions, running processes or
Converse's separate Voice Capture Mode (#623).

The view distinguishes selection source, selected next-launch graph, host
defaults, parent's startup declaration and unobserved running services. A save
or historical receipt does not establish restart or readiness. An unavailable
or incompatible Core endpoint stays unknown; the client does not invent an
editable default or require Agent availability to inspect Core.

## Native ownership and recovery

`CoreSubsystemApi` uses a dedicated Session transport with the existing 4 KiB
request, 16 KiB response, ten-second result and one-second cleanup bounds.
It rejects redirects, compression, malformed UTF-8/JSON, duplicate keys, unsafe
numbers and mismatched proofs. Credentials stay private to the transport.
Normal and mock facades require their own injection; neither uses shared HTTP
or the Agent endpoint for these settings.

Strict immutable adapters validate `core_subsystems.v1`, supported graphs,
selection/source consistency, backend/controller identity, bounded generation
and revision, state token and next-launch boundary. Canonical request bytes and
SHA-256 match the actual Python owner. Positive receipts bind every original
observation and selection; lookups bind the whole original digest.

Drafts and pending originals are scoped to canonical Session URL, private
credential and native backend. At most sixteen scopes are retained. Genuine
transport ownership remains bounded after timeout/navigation. Dirty drafts
retain their observed base; metadata cannot silently rebase them. Known
replacement backends suppress old writes/lookups. Changed controller/state
observed before dispatch prevents that original from being sent.

Each UI original is dispatched once. An uncertain dispatched save blocks a
replacement save and offers exact read-only lookup, including after navigation
and same-owner reconnect. Unobserved absence alone cannot settle it. Only a
matching historical positive receipt or native permanent fence resolves it.
A receipt does not replace current inspected values. After a retired original
is fenced, the owner explicitly adopts inspected state before a fresh save.

The native selector and receipts persist on the host. UI drafts and unresolved
originals are RAM-only; navigation/reconnect recovery does not claim recovery
of those records after an application-process restart. The CLI separately
retains its own original requests durably. No credential or global profile is
copied into a draft.

## Verification

The [evidence](evidence/core-subsystem-options-2026-11-15/README.md) records
1,495 passing Flutter tests and one environment-gated live case skipped in that
run. The actual Dart/native integration separately passes. Analysis, web build,
Ruff and unchanged schema export checks pass.

The first PR #632 CI run exposed [#634](https://github.com/rgoza1996/Kinaiya/issues/634):
the Dart interoperability case exited zero, but its Python wrapper expected the
local reporter's success wording. GitHub Actions selects a different default
reporter. The fixture now explicitly selects Flutter's supported expanded
reporter, retaining the exit-code check and every native request, receipt,
unchanged-host and joined-owner assertion. The original CI failure and a local
CI-environment reproduction are retained. The corrected case passes under both
normal and GitHub Actions environment selection; exact-head CI remains required.

The built app saves all six modes and a fresh host-default reset through the
native API at desktop and narrow widths. A real SIGTERM interruption leaves
its exact original unconfirmed. A replacement controller on the same private
store permanently fences it before a new save. Seven committed native receipts,
source pins, screenshots and clean replacement-process exit are retained.
The interrupted first process's final cleanup receipt is unavailable.

This completes the Options selector increment. Separate Decisions consumer
policies, the full service/resource/external-Agent lifecycle matrix (#566), and
the broader settings-ownership audit/migration (#567) remain unfinished.
No model inference, model-quality gate or owner deployment is part of this work.
