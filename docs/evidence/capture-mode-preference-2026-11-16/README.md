# Converse Capture Mode preference (#623, #651)

This qualifies the native preference and owner UI foundation. Adaptive Live is
feature-gated off and **#623 remains incomplete**. The streaming FIFO, versioned
five-outcome Decisions contract, completion-driven scheduler, backchannel
playback and rollout still need implementation/qualification.

The actual protected SQLite owner passes persistence/restart, all three saved
modes, default pointers, exact replay, lost/absent commit acknowledgments,
replacement authority, immutable history, corruption, safe-file identity and
concurrent CAS controls. Owner HTTP inspection/save/lookup stays available while
inference is off, records no transcript and makes zero provider calls. Invalid,
duplicate-key, floating, oversized and unauthorized requests refuse. Runtime
retirement closes this owner even when dependency cleanup fails.

The actual Dart/native Converse fixture uses authenticated loopback HTTP and
SQLite on the original server thread. It saves all three modes, verifies exact
replay/lookup, stale CAS, host reset and non-owner refusal, then joins the original
server thread and closes its owners. No model/audio worker is started.

Python3.13.18 passes **118 related cases in18.05s** and the separate actual
Dart/native case in7.29s. Python3.14.10 passes the complete combined **119 cases
in21.74s** from the same hashed development lock. Counts overlap deliberately.
The export adds five independently pinned schemas; every one of the **523**
previous schema files remains byte-identical.

The private Flutter project passes **1,516 tests, two opt-in native skips, in69s**.
The actual Capture Mode native case is qualified above; the separate existing
Core native case was already qualified in the parent stack. All418 declared
private application inputs remain unchanged from the observed near-start pins
to completion. The copy includes the #652 Core test-only correction before its
source integration; no production override is present. A later brace-only lint
correction and explicit320px/2x selector refinement pass separately. The narrow
case exercises a lost save ACK, exact read-only recovery and the unchanged
current capture without resending. A retained first narrow test tried to find an
unbuilt menu item before scrolling; it is corrected to exercise the real menu
scroll, with the original save/current/lookup assertions retained.

The release web build passes in27.9s, including its Wasm dry run. Its
CupertinoIcons font warning also appears in the earlier Core/readiness build
logs; no font asset changes are included. Analysis, formatting, Ruff and diff
checks are recorded with the final source before publication.

## First failures and corrective scope

The first complete Flutter run retains1,472 passes, one opt-in skip and31 failed
cases. Most failures came from older synthetic voice transports without the new
explicit settings endpoint. Their private transports now supply a native-shaped
snapshot; the production transport still requires its independent client.
Existing microphone, stop, session, draft, consent and lifecycle assertions are
retained. The Dart AST audit covers791 existing expectations in12 changed files; only two explicit legacy display expectations are deliberately renamed to Legacy Silence.

The original optional-output oracle also exposes #651: an insertion nested
Capture Mode under the optional output predicate and rendered output controls
unconditionally. The correction renders the two independently, retains generic
Call/Tell help for an unobserved mode, and exposes end-silence controls only for
an observed Legacy choice. Two explicit independent-panel cases and the original
optional-output oracle verify this. Options never warms the microphone.

A second complete run retains1,506 passes, two opt-in skips and two failures.
One scrolling fixture incorrectly waited for an active Call's animation to
settle; it now waits only for its scroll. The unrelated Core inspector's20ms
fixture budget fails before its intended original save. #652 retains that first
failure and its deterministic acquisition control in the parent stack; all29
original expectations remain. No native deadline or application source changes
for that follow-up.

Raw first/final logs and exact commands/hashes are retained in
`verification.json` and local `output/issue-623-*`. An earlier pytest command
used a nonexistent launcher filename and collected no tests; the corrected
command above runs the real launcher module. Routine import/brace formatting
errors were corrected; the passing checks describe only their actual scope.
Native hardware delivery and model quality are not established by these checks.
