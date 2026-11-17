# Native Converse consumption qualification (#623, #676)

This covers the gated [native Converse consumer](../../live-converse-consumption.md)
on `codex/623-live-converse`, based on native Agent consumer revision
`49f94b564d249d0cc8d26adec07489312bef996d`. Normal host, browser Call and default
Adaptive Live rollout remain off; full #623 is not complete.

## Current source

- `issue-676-final-composed-313`: **776 passed, 1 warning in 214.09s (0:03:34)**.
- `issue-676-final-composed-314`: **776 passed, 1 warning in 217.35s (0:03:37)**.

Both composed commands use the same 914 captured Python inputs in
[python-input-sha256.json](python-input-sha256.json), including the original
native loop, actual authenticated HTTP paths, v1 windows, speech authority,
disclosure/history, effect ledger and native Agent controls. Each completed run
reports one existing Starlette/httpx deprecation warning. Counts overlap.
Environments: Python 3.13.18 and 3.14.10; existing locked dependencies, with
`requirements-dev.lock` SHA-256
`22c79f72446b6044bfb532d81133789f25632a7b652a51cdfe3e6d33980e6d38`.

The preceding broader run passed **2,194 cases in 587.55s**, with the same
existing warning. It predates the final #676 wake-read repair. The exact
[production delta analysis](checks/676-production-delta.json) verifies that only
the completed-wake observation block changed afterward; the other 912 input
files retain their bytes. The current composed runs qualify that repair on both
interpreters. A third full local repeat was not run for this isolated block;
complete current-head hosted CI remains required before integration. The earlier
2,192/772 runs predate the separate mode-order repair and remain historical.

## Observable behavior

| Boundary | Actual native controls |
| --- | --- |
| Original generation and wording | Exact authorized input/personality; raw provider output retained; optional empty acknowledgment; no reroll or rewriting |
| Capture and reply ownership | Backchannel followed by full reply in one unchanged capture/utterance; v1 full-response slot remains separate |
| Response hold and routing | Speaking-hint release without second generation; changed text/scope/restoration/fenced capture rejected; later wait and incompatible modes supersede older output |
| Speech authority | Actual runtime validation of cached native chunks; post-publication owner/source/scope/capture/control revocation |
| Cancellation and uncertain delivery | Original start retained through facade cancellation; before/after-commit acknowledgment loss; replacement observes evidence/uncertainty without another producer or write |
| Lifecycle and wake recovery | Actual ASGI cleanup after consumer-close failure; provider completion wakes without cancelling the original read; terminal read failure recovers while original cleanup stays owned and cleanup status remains false until settlement |

The #676 first probe confirmed a terminal failed wake Task remained pinned while
a later actual native publication was ignored. Both dedicated before-repair
variants then failed. The corrected loop clears that completed observation before
reading its result; original cleanup remains enrolled. No model or mutation is
retried as wake recovery. First reports, exact source/control snapshots and
repaired results are retained.

Other initial failures exposed native hint freshness, inherited v1 identity,
premature buffered completion, native wait/mode ordering and speech validation.
Two fixture mistakes are recorded separately: using an unsupported publication
attribute and omitting the existing native owner tick from the completion driver.
Their first logs remain intact. All original 28 assertion ASTs
remain in the expanded 103-assertion controls. No failing assertion
was weakened to obtain a pass.

## Evidence and limits

[verification.json](verification.json) retains exact commands/results, original
receipt hashes, first source snapshots and reconstructable per-run input hashes.
JSON log envelopes preserve original UTF-8 bytes without trimming whitespace.
Reconstruct an older input manifest by removing `absent_from_manifest` from the
shared manifest and applying `input_hash_overrides`. Unchanged original files
reference the pinned public base. Other first snapshots use content-addressed
full files or smaller unified patches in JSON UTF-8 envelopes. Each patch
identifies its exact tracked reference blob and SHA-256; decoding `content` and
applying it without fuzz reconstructs the recorded
original source SHA-256. The reference blobs belong to this change.

Full Ruff, the 1,198-file format check, workspace export and all **594 unchanged
schema files** passed. [Final static commands](checks/final-static.json) retain
their results. The first formatter command used the wrong working directory and
failed without editing files; [its record](checks/format-wrong-cwd.json) explicitly
distinguishes that failure from the later successful check. Final staged-diff
verification is recorded at publication.

This uses synthetic providers, native SQLite and real in-process HTTP/lifespan
boundaries. No live/paid model, physical audio, host-policy change, package/SDK
installation or installed-owner promotion was used. A durable native chunk is
not proof of audible playback or physical cleanup. Complete process replacement,
host/Call/echo/mute/accessibility/no-audio integration, representative timings and
qualified default rollout remain in #623. Model quality is diagnostic, not an
architectural acceptance gate.
