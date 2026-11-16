# Incremental native ownership qualification (#654, #656)

This is the native lifecycle increment for #623, separate from the optional
browser adapter in #655. Adaptive Live remains off. The source-pinned receipt is
[verification.json](verification.json); the contract and remaining limits are in
[incremental-stream-lifecycle.md](../../incremental-stream-lifecycle.md).

The retained first controls complete their real authenticated ASGI/Whistle IPC/
SQLite effects with synthetic engine/manager fixtures, then fail five exact
lookups with404. They cover lost owner/upstream startup acknowledgments, consumed
PCM with a committed or unobserved publication, and native finish followed by an
unconfirmed final publication. The successful baseline passes. The preliminary
Store.read argument mistake is retained separately and is not the target failure.
[The frozen control](first-lifecycle-control.py.txt) predates the repair. All18
original assertions remain; eight further observations and metadata-only input
were added. There is no PCM/STT replay or replacement append in lookup/reconcile.

Final focused qualification passes **118 cases on Python3.13.18 in9.30s** and the
same **118 on Python3.14.10 in9.51s**, using the existing hashed development lock.
It covers exact reservations/ACKs, startup/PCM/finish uncertainty, original HTTP
wait and close ownership, malformed input, owner/session/incarnation/model fences,
concurrent input, actual protected STT SQLite leases/model pins and positive-only
Session publication lookup. The final review also covers native policy refusal
before entry and corrupt event metadata bounded at the SQLite/Python boundary.
Expiry does not retire an active stream. Explicit
cancel fences a pending reserved body, while actual lease retirement still waits
for the original HTTP exchange. Missing state remains unknown.

Read-only lookup never retries cancellation, acknowledges the broker, dispatches
audio or appends another transcript. Explicit reconciliation acknowledges only a
proven frozen result. A lost final append ACK can therefore recover from the real
stored entry and retire without a second append or recognizer call. Repeated
inspection does not accumulate completed task history. A valid recognizer result
cannot erase an independent response-close failure.

#656's separate [first control](first-cleanup-retry-control.py.txt) fails after the
same original's first stop is unconfirmed and its second stop has positive fixture
release evidence. The old attempt's cleanup_failed flag remains set. The repair
clears only that old attempt after its exact new cleanup succeeds, while native
work and independent IPC/HTTP-close guards remain. Explicit owner retry succeeds;
read-only observation never retries it. The negative IPC-close control remains
unreleased. This is synthetic proof-contract evidence, not actual ASR reaping.

The final broader audio/voice/Core/capture regression passes **1,190 cases in
212.86s**. All847 declared Python inputs remain unchanged over its interval.
Earlier1,172-,1,187- and1,188-case runs are preserved as earlier-source evidence,
not substituted for this final run. Actual Dart/native Core and Capture Mode transports are
included. Exact commands, input/log hashes and first failures are in the receipt.

The [policy-refusal control](first-policy-refusal-control.py.txt) first fails
with unknown state despite the sole producer exiting before native admission.
The repair classifies that consumed original as never dispatched without replay
or process-release claims. The [publication-bounds control](first-publication-bounds-control.py.txt)
first fails twice because129 ASCII bytes or130 multibyte UTF-8 bytes cross the
SQLite boundary before validation. The repair bounds those columns in SQLite.
A preliminary sequence-corruption fixture is preserved separately: SQLite already
rejects its noninteger primary key, so that fixture error is not target evidence.

Ten additive schemas have their own frozen hash. All528 prior schema files remain
unchanged. The moved Whistle update validator retains its original argument and
body AST. Formatting, Ruff, export and diff checks are recorded separately.
No SDK/dependency, native host policy, installed-owner, model/provider default or
inference-quality change is part of this increment.

Limits: no real ASR model, microphone, physical-device/ASR-process-release or full
Adaptive Live qualification is claimed. Stream originals live in this controller
instance; a restart cannot synthesize their state. The protected NULL-bound native
admission remains conservatively unknown after restart. Unobserved/unpublishable
transcript results remain retained and paused. Full #623 still needs segment
rollover, versioned Decisions, fresh-source/completion scheduling, backchannel/
echo/mute ownership, stage timings and rollout. Model answer quality is outside
these architectural acceptance checks.
