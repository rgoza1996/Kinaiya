# ADR 0009: Skill controller with task pins and disclosure policy

Implements the controller/pinning portion of issue #28 on top of the
reviewed skills foundation (ADR 0007). New code lives only in
`services/agent/skill_service.py`, with coverage in
`tests/integration/test_skill_service.py` (19 tests). The foundation
(`packages/ecosystem/skills.py`), runtime, commands, providers,
contracts, launcher, seed, and app surfaces are untouched; the lead
integrates those concurrently.

## What the controller owns

- **One live controller per root.** Construction creates the control root
  without following links, takes a non-blocking `fcntl.flock` on a
  no-follow regular `0600` lockfile, then builds `SkillManager(root,
  workspace)`. A second live instance for the same root is rejected;
  `close()` is synchronous, idempotent, and releases the lock.
- **Serialized access.** Every async API holds one `asyncio.Lock`, so
  registry, scope-file, and pin mutations are serialized on the single
  event loop. There is no cross-process mutation protocol beyond the
  lockfile, and no filesystem-plus-SQLite transaction: registry and scope
  writes are each atomic (exclusive temp file, fsync, replace), but they
  are not one atomic unit.
- **Owner scopes.** `local_only` (default), `public_synthetic`, and
  `private_cloud_authorized` persist in `scopes.json` inside the protected
  root (bounded no-follow reads, exclusive temp+fsync+replace). Installs
  and updates may set a scope only when the owner explicitly supplies it;
  scope persistence failure fails closed to `local_only` and raises
  instead of silently permitting disclosure. Unknown or corrupt scope
  state refuses service rather than guessing.
- **Model disclosure policy (trusted code, never prompts).**
  Local-provider models may see enabled, model-invocable skills. Every
  Meta provider counts as Contributor even under aliases, so Contributor
  callers see only `public_synthetic`. Other cloud providers see
  `public_synthetic` or `private_cloud_authorized`. Filtering happens
  before ids, names, or descriptions are exported. Disabled, manual-only,
  and model-disabled skills never appear in the model catalog.
- **Task pins.** The first `load` for a task+skill captures exactly one
  verified snapshot via `manager._verified_blobs(record)` — metadata,
  invocation flags, source provenance, digest, and all resource bytes.
  Later loads for the same task+skill reuse those pinned bytes across
  updates and rollbacks; removal refuses while dependent pins exist, and
  `release_task` drops pins idempotently. Every load rechecks the
  *current* enabled state, author flags, and owner scope/provider policy,
  so revocation blocks subsequent disclosure of existing pins; the pinned
  author flags must allow the invocation as well. Bounds: at most 32 pins
  and 32 MiB retained, refused explicitly before exceeding. Returned
  structures are copies, so callers cannot mutate pins.
- **Bounded outputs.** Owner pages carry at most 8 rows; the model
  catalog carries at most 4 rows with descriptions capped at 80 chars in
  at most 1600 JSON chars. Every `load` response is at most 12000 JSON
  chars; oversized bodies fail with a needs-smaller-resource error naming
  the `resource=` alternative, instead of silent truncation. Resources
  load as UTF-8 text only (with path and SHA-256); binary is refused, and
  nothing here materializes or executes scripts.

## Explicitly not claimed

- **Not full M1A.** No Agent tool/command/API/Flutter exposure, no
  runtime wiring, no provider onboarding: `authority="owner"` and
  `invocation="user"` are internal interfaces for the trusted launcher
  and command integration, never model tool arguments. The lead owns that
  integration and the network-fetch responsibility (this service rejects
  URL-looking sources instead of fetching).
- **No crash recovery.** A crash between the registry write and the scope
  write (or vice versa) can leave the two out of sync; restart refuses
  changed or tampered content (verification fails) but never calls it
  recovered. Task restart must not silently resume an old pin: the
  runtime must `release_task` finished, cancelled, failed, or uncertain
  tasks (it already pauses uncertain recovered tasks) and use fresh task
  ids per incarnation.
- **No upstream conformance.** A valid portable package is not proof of
  OpenClaw/Hermes runtime compatibility; dependency installation and
  execution remain unimplemented and refused by the foundation.
- **Single-controller scope.** The OS lock rejects a second *live*
  controller, but backup, replication, or administrator edits under the
  root are outside the threat model, as with the foundation's control
  directories.

## Verification

`tests/integration/test_skill_service.py`: 19 tests covering synthetic
install/enable/load/resource roundtrips, script/license preservation
without execution, model/user and manual-only restrictions, default
`local_only` with Meta-alias denial before any export, the other-cloud
scope matrix, scope persistence across restart, revocation blocking an
existing pin with re-enable readmission, pin immutability across updates
(new tasks get the new version), removal refusal with dependent listing
and release-gated removal, two-controller rejection, malformed inputs,
no-follow scope/lockfiles, bounded pages and payloads, pin-memory bounds,
and fail-closed scope writes. The 53 reviewed foundation/boundary tests
are untouched and still pass; the full offline suite result is recorded
in the delivery report.
