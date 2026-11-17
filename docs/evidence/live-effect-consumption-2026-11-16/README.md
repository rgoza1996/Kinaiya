# Native consumption ledger qualification (#623)

This is the historical ledger-only record at `c6fee11bcefa6b04a05f6894c2b3b8c669236498`.
The later [native Agent consumer record](../live-agent-consumption-2026-11-16/README.md)
qualifies the actual dispatcher and its additional discovery/evidence/source contracts.
The results and limitations below describe the original ledger increment.

This record covers the gated [consumption boundary](../../live-effect-consumption.md),
branch `codex/623-live-consumers`, based on
`04659d966d75afe4afd51c65abafdd4743adf824`. It also carries the parent input
PR's explicit legacy selector and inspection-finish ownership repairs. It does
not establish a running Converse/Agent v2 consumer or complete Adaptive Live.

The broader Python 3.13.18 run passed **582 cases in 131.21s**, with one existing
Starlette warning. Python 3.14.10 passed **152 focused cases in 23.26s**. Both
match the same **903 unchanged Python inputs** in
[python-input-sha256.json](python-input-sha256.json). Counts overlap.
Full Ruff and a 1,181-file format check passed. Schema export preserved all 579
prior JSON files; nine additive roots have the independent pin
`1044614df72ec0f52a38947ce3063745030401ec9baa6d2144cfdbdc13e2daf9`.
The frozen 184-schema aggregate and its original hash pass in both runs.

| Requirement | Actual controls |
| --- | --- |
| Passive reads and one original reservation | Real SQLite row/wake counts, immutable replay, competing consumer identity and role/axis refusal |
| Separate one-use execution start | Atomic executor adoption while unstarted; foreign executor refused after start; lost start ACK observed without mutation replay |
| Unknown ownership remains held | Uncertain capacity retained; no replacement reservation; positive authenticated late evidence resolves uncertainty without a new effect |
| Native scope/action provenance | Revocation, changed text and foreign operation evidence refused; exact native Converse result binding |
| Queues outlive microphone, not later user interrupts | Actual native capture fence; later interrupt from another utterance; delayed older interrupt source order; original started/reserved/uncertain state across SQLite reopen and controller replacement |
| Strict transport and independent old modes | Real ASGI/JournalClient and LocalJournal role parity, cancellation/lost ACK controls, malformed/duplicate/UTF-8/structure bounds, original legacy codec and all prior schema pins |

[verification.json](verification.json) contains exact commands, actual results,
first failures, issue-before-fix comments and relevant source snapshot hashes.
Logs use JSON envelopes: decoding each `content` as UTF-8 reproduces the exact
original bytes and its recorded SHA-256. The originals remain in root `output/`.
First-run input hashes reconstruct from the shared manifest by removing the
listed absent paths and applying the recorded overrides. This avoids duplicating
the complete manifest for every run without losing source identity.

The first failures include actual revocation, evidence-action, parser, capacity,
late-recovery and interrupt faults, plus explicitly identified fixture wake,
attempt-ID and closed-query mistakes. Their original logs and snapshots remain
available. No model-quality gate, output rewrite, reroll, provider/default switch,
physical-audio run or installed-owner change is part of this increment.

This child increment has not run hosted CI. Actual consumers and their bounded
recovery/discovery, native Agent admission and command evidence, Converse
output/playback, Call integration, representative timings and qualified default
rollout remain outstanding. Full #623 remains open.
