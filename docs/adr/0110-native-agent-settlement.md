# ADR0110: Native standing Agent settlement and immutable history

Status: implemented on the working integration branch for #228 / #239;
capacity, broader regressions, final independent review and CI remain gates.
This extends [ADR0109](0109-agent-claim-retirement.md). It does not establish
the scheduler, durable inbox, owner editor or full autonomous-work acceptance.

## Original Agent authority

Before its first standing lookup, the original in-process Agent owner creates
a separate random 32-byte settlement capability and owner identity. It retains
these with the frozen intent and original Journal/Resources dependencies.
After a read-only initial lookup, it registers against the exact prepared claim
before binding Resources, allocating a model or admitting a worker. A retained
registration cannot be adopted by another Agent process. The capability never
appears in public packets, native records, Resources calls or diagnostic state.

The original registration exchange remains owned through its actual send,
read and sole close. A lost acknowledgement can be recovered by exact read-only
lookup only after those originals settle, while the same attempt is still
eligible. Neither timeout nor a provisional null lookup proves absence.
Registered-but-unbound uncertainty remains unresolved; a generic conflict is
not a negative-acquisition certificate.

After permanently fencing fresh effects, Agent joins actual original preflight,
worker, controls, model cleanup and HTTP children. It consumes the complete
matching native Resources receipt before submitting settlement. The separate
finalizer scope cannot join itself. Failed or cancelled actual cleanup stays
negative even when a native settlement was committed before its reply was lost.
Workspace status retains that failure after Journal replacement and shutdown.

Four explicit work dispositions preserve Task truth:

- `launched-outcome-known` binds the complete native outcome request and entry
  through their canonical UTF-8 SHA256 hashes, Task identity and revision.
- `launched-outcome-unconfirmed` records healthy original work retirement
  without inventing an outcome or rewriting a nonterminal Task.
- `created-unlaunched` records accepted work that never consumed its launch.
- `bound-never-admitted` requires a literally null native initial proof.

Raw worker cancellation remains failed retirement. A terminal Task, historical
receipt, empty registry or expired producer lease is insufficient cleanup proof.
Later steering revisions may change the objective; original Task/session,
workspace, model, harness and standing-key pins remain exact.

## Native storage and replay

Session atomically initializes wholly absent v2 storage or upgrades a complete,
strictly healthy v1 family on the same connection and lock. Reads do not migrate
or repair storage. Fresh v2 claims have immutable settlement origin; migrated
claims remain uncovered and cannot acquire new original-process authority.
The default Session composition is v2. Explicit trusted Python composition can
retain v1; there is no HTTP, environment, automatic-negotiation or downgrade path.

Each claim retains its own initial, closure, binding, reservation, Resources
report, Agent registration and immutable settlement. Settlement clears only the
matching active claim in the same transaction. Replaying settled A cannot clear
a later active B. Task identities are unique across retained history and
ordinary native work. Ordinary Agent export validates its relevant native
admission links without treating unrelated settlement bodies as export authority.

Every operation and final pre-commit pass freshly validates bounded storage and
the full relevant semantic joins. Healthy but inapplicable supplied outcome
evidence returns conflict; malformed native data or contradictory retained
history is unavailable. A close observed by the final guard rolls back; a close
after that last guard or during entered COMMIT may leave a complete durable
write with an unavailable acknowledgement. It does not establish absence.

The persisted lifetime history limit is 64 prepared claims. Each reserves
3,080,192 logical bytes before preparation; shared metadata reserves 4,096 bytes,
for a 197,136,384-byte logical body budget. Releasing an active slot does not
delete history or refund that lifetime reservation. There is no silent growth,
eviction, truncation or archival. Otherwise eligible fresh work at a full history
is unavailable; active/duplicate conflicts retain their distinct semantics.
H64, maximum valid graphs and ordinary-service availability remain unqualified
until their dedicated actual storage/transport runs complete.

## Transport

Four Agent-only identity-JSON POSTs share the existing bounded native operation
registry and original Journal exchange ownership. Wire version remains `1.0`;
native storage version is `2`.

| Path under `/v1/standing/tasks` | Request bytes | Maximum reply bytes |
|---|---:|---:|
| `/retirement/register` | 148,480 | 149,632 |
| `/retirement/registration/lookup` | 148,480 | 149,632 or null |
| `/settle` | 972,800 | 973,952 |
| `/settlement/lookup` | 149,760 | 973,952 or null |

Only settlement carries the explicit purpose-separated capability header.
Ambient standing capability headers are refused before client I/O. Strict full
graphs, old nested packet caps, fixed errors, role checks, 25-second facades and
actual retained cleanup remain separate boundaries. A refused facade cannot
replace the original close. An unused replacement Journal remains caller-owned
and receives neither the original claim's requests nor its dependency close.

## Evidence and limits

At clean runtime source `e0721d260675b159a1d3885ee97089b33a3c700b`, 43 native
SQLite/ASGI controls and 27 actual Agent integration controls pass in separate
serialized runs, with all 1,537 tracked inputs and HEAD unchanged. The latter
uses controlled inference responses with actual permitted file work, native
Resources reports and original cleanup owners. It includes lost registration
and settlement acknowledgements and late failed/cancelled reconciliation closes.
These are harness checks, not model accuracy or live-device qualification.

Independent packet/client baselines, native/Agent oracles, source-aware cleanup
controls, original failed runs and later fixture corrections remain distinct.
The schedule fixture now grants genuine future work; HTTPX's early CLOSED flag
is not used as underlying transport-close proof; unused replacement-client
cleanup stays in its caller fixture. None changes original retirement or replay
requirements. PR315's separate CI failures remain unresolved; these local passes
do not qualify that published source or establish an eight-second timing fix.
