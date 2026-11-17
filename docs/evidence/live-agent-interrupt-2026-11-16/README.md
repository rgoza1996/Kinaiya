# Native interrupt recovery qualification (#671)

This follows PR #672's original `e03eecbe07df38c4f2238e2f5f69bca3300df8a7`
head and its [consumer record](../live-agent-consumption-2026-11-16/README.md).
The native interrupt event already preserved the original request key, target
revision, incoming source IDs/raw revision and causes. Dedicated controls found
that its in-memory replay digest still omitted the authenticated actor and raw
source revision. Reusing a native key with either field changed silently
returned the prior result. Both are now bound to that original key. The separate
frozen administrative-retirement digest remains compatible for owner replay
of an already retired record; no wire schema or permission is changed.

The first five-case run had **one failure/four passes in 1.75s**. The expanded
unchanged-runtime run had **two failures/five passes in 1.98s**, proving both
substitution faults. Both exact logs, original control snapshots and complete
input identities are retained. The faults were recorded in
[#671 before repair](https://github.com/rgoza1996/Kinaiya/issues/671#issuecomment-6934565675),
with the additional actor control recorded in the following issue comment.
All original failing assertions remain intact. The repaired interrupt,
retirement compatibility and completion-race checks passed **63 cases in 8.03s**.

Final broader Python 3.13.18 qualification passed **979 cases in 249.87s**;
Python 3.14.10 passed **312 focused cases in 71.96s**. Each reported one existing
Starlette warning. Both match the same **912 unchanged Python inputs** in
[python-input-sha256.json](python-input-sha256.json). Counts overlap.

The seven dedicated cases use the actual Agent runtime/API and native Store:

- Lost Agent HTTP acknowledgment before/after actual commit: one original
  dispatch, exact command provenance only after commit, replacement never POSTs
  another interrupt; absent proof stays uncertain.
- Original native interrupt journal failure before/after commit: positive
  native evidence resolves only the committed event; repeat observation and
  replacement never repeat the write or command.
- Same-key raw revision or authenticated actor substitution conflicts; an
  identical repeat keeps the one original native command.
- Mutating the caller's request during actual source validation does not alter
  the accepted original key, source IDs/revision, reason or causes.

The broader set also exercises native retirement/authority/recovery, completion
races, standing external control ownership and the preceding consumer/live
contracts. The frozen retirement digest and all **594 schema files** remain
unchanged. A recorded interrupt is evidence of a native command, not proof of
physical stop, cleanup or task success.

[verification.json](verification.json) records commands, log hashes, first
failure manifests and source references. JSON envelopes retain the exact original
UTF-8 bytes. Each earlier input manifest reconstructs from the shared manifest
by removing `absent_from_manifest` and applying `input_hash_overrides`.
The original runtime is available at the pinned public e03eecbe revision; both
original control snapshots are retained byte for byte. Formatting changed only
parentheses around one assertion, preserving its AST.

Full Ruff, the 1,195-file format check and workspace schema export passed; all
594 schema files retain their bytes. The staged-diff result is recorded separately
during publication.
Hosted CI for this follow-up is not claimed by these local results. Normal host,
Call and default Adaptive Live enablement remain off; full #623 is incomplete.
No paid/live model, physical audio or installed-owner promotion was used.
